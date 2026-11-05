import io
import json
from PIL import Image

from backend.app.database import get_db
from backend.app.task_worker import worker_instance
from test_dependency_chain_dispatch import _full_loop_batch


def test_custom_white_prompt_records_actual_and_preserves_history(client, monkeypatch):
    batch_id, design_id = _full_loop_batch(client)
    for _ in range(3):
        assert worker_instance.process_next_task()
    design = client.get(f'/api/designs/{design_id}').json()
    slot = design['slots']['white_isolated']
    old_id = slot['current_version']['id']
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE image_api_configs SET enabled=1,base_url='http://mock',model='test',api_key='test' WHERE id='default'")
    image = io.BytesIO(); Image.new('RGB', (32, 32), 'white').save(image, format='PNG')
    calls = []
    def edit(cfg, prompt, reference, filename):
        calls.append((prompt, reference))
        return image.getvalue(), 'mock-image'
    monkeypatch.setattr('backend.app.task_worker.edit_image_real', edit)
    response = client.post(f"/api/slots/{slot['id']}/redo", json={'slot_id':slot['id'], 'expected_version_id':old_id, 'reason_category':'构图不符', 'custom_prompt':'自定义白底提示词'}, headers={'X-User-Role':'artist','X-User-ID':'artist-test'})
    assert response.status_code == 200, response.text
    assert worker_instance.process_next_task()
    slot = client.get(f'/api/designs/{design_id}').json()['slots']['white_isolated']
    metadata = slot['current_version']['prompt_metadata']
    assert calls and calls[0][0] == '自定义白底提示词' and calls[0][1]
    assert metadata['real_prompt'] == metadata['prepared_prompt'] == '自定义白底提示词'
    assert metadata['prompt_author'] == 'artist-test' and metadata['prompt_status'] == 'completed'
    assert metadata['reference_version_id'] == design['slots']['panorama']['current_version_id']
    assert any(v['id'] == old_id for v in slot['history_versions'])
    assert client.post(f"/api/slots/{slot['id']}/redo", json={'slot_id':slot['id'], 'expected_version_id':old_id, 'reason_category':'构图不符','custom_prompt':'重复'}, headers={'X-User-Role':'artist'}).status_code == 409


def test_failed_redo_keeps_prepared_prompt(client, monkeypatch):
    batch_id, design_id = _full_loop_batch(client)
    for _ in range(3): assert worker_instance.process_next_task()
    slot = client.get(f'/api/designs/{design_id}').json()['slots']['white_isolated']
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE image_api_configs SET enabled=1,base_url='http://mock',model='test',api_key='test' WHERE id='default'")
    def fail(*args, **kwargs): raise TimeoutError('mock timeout')
    monkeypatch.setattr('backend.app.task_worker.edit_image_real', fail)
    response = client.post(f"/api/slots/{slot['id']}/redo", json={'slot_id':slot['id'],'expected_version_id':slot['current_version_id'],'reason_category':'构图不符','custom_prompt':'本次尝试'}, headers={'X-User-Role':'artist'})
    assert response.status_code == 200, response.text
    assert worker_instance.process_next_task()
    version = client.get(f'/api/designs/{design_id}').json()['slots']['white_isolated']['current_version']
    assert version['prompt_metadata']['prepared_prompt'] == '本次尝试'
    assert version['prompt_metadata']['prompt_status'] == 'prepared'
    assert 'real_prompt' not in version['prompt_metadata']


def test_queued_twin_uses_frozen_white_reference_after_selection_changes(client, monkeypatch):
    batch_id, design_id = _full_loop_batch(client)
    for _ in range(3): assert worker_instance.process_next_task()
    design = client.get(f'/api/designs/{design_id}').json()
    white = design['slots']['white_isolated']['current_version_id']
    twin = design['slots']['twin_bed']
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE image_api_configs SET enabled=1,base_url='http://mock',model='test',api_key='test' WHERE id='default'")
    result = client.post(f"/api/slots/{twin['id']}/redo", json={'slot_id':twin['id'],'expected_version_id':twin['current_version_id'],'reason_category':'构图不符','custom_prompt':'双床调整'}, headers={'X-User-Role':'artist'})
    assert result.status_code == 200, result.text
    with get_db(immediate=True) as conn:
        row = conn.execute('SELECT * FROM slot_versions WHERE id=?', (white,)).fetchone()
        conn.execute("INSERT INTO slot_versions (id,slot_id,design_id,version_num,attempt_num,prompt,process_status,raw_image_path,preview_image_path,depends_on_panorama_version_id) VALUES ('alternate-white',?,?,99,99,?,'completed',?,?,?)", (twin and design['slots']['white_isolated']['id'],design_id,row['prompt'],row['raw_image_path'],row['preview_image_path'],row['depends_on_panorama_version_id']))
        conn.execute("UPDATE slots SET current_version_id='alternate-white' WHERE id=?", (design['slots']['white_isolated']['id'],))
    image = io.BytesIO(); Image.new('RGB',(32,32),'white').save(image,format='PNG')
    monkeypatch.setattr('backend.app.task_worker.edit_image_real', lambda *a,**kw:(image.getvalue(),'mock-image'))
    assert worker_instance.process_next_task()
    updated = client.get(f'/api/designs/{design_id}').json()['slots']['twin_bed']['current_version']
    assert updated['prompt_metadata']['reference_version_id'] == white
    assert updated['prompt_metadata']['real_prompt'] == '双床调整'
