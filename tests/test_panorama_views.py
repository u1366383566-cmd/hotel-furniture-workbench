import json

from backend.app.database import get_db

from backend.app.planning_service import PANORAMA_VIEWS, build_real_generation_prompt, choose_panorama_view
from backend.app.task_worker import worker_instance


def _create(client, count=1):
    response = client.post('/api/batches', headers={'X-User-Role': 'leader'}, json={
        'name': '全景视角测试', 'enabled_series': ['modern_business'], 'target_archived_count': count,
        'designs_per_series': count, 'standby_designs_per_series': 0,
        'warning_budget': 20, 'hard_limit_budget': 25, 'auto_start_panorama': True})
    assert response.status_code == 200, response.text
    return response.json()['id']


def test_views_balanced_and_legacy_history_ignored(client):
    batch = _create(client, 5)
    designs = client.get(f'/api/designs/batch/{batch}').json()
    ids = {d['structured_spec']['panorama_view']['id'] for d in designs}
    assert ids == set(PANORAMA_VIEWS)
    history = [{'_batch_id': 'old', 'panorama_view': {'id': 'front'}}, {'headboard': 'legacy'}]
    assert choose_panorama_view(history, 'new')['id'] != 'front'


def test_generation_and_redo_view_is_versioned(client):
    batch = _create(client)
    assert worker_instance.process_next_task()
    d = client.get(f'/api/designs/batch/{batch}').json()[0]
    p = d['slots']['panorama']; first = p['current_version']; initial = d['structured_spec']['panorama_view']
    assert initial['instruction'] in build_real_generation_prompt('panorama', d['structured_spec'], d['design_code'])
    resp = client.post(f"/api/designs/{d['id']}/review_panorama", json={'action': 'redo', 'expected_version_id': first['id'], 'reason_category': '构图不符', 'detail_comment': '保持视角，只调整光线'})
    assert resp.status_code == 200, resp.text
    assert worker_instance.process_next_task()
    d = client.get(f'/api/designs/{d["id"]}').json(); p = d['slots']['panorama']; current = p['current_version']
    assert current['prompt_metadata']['panorama_view'] == initial
    resp = client.post(f"/api/slots/{p['id']}/redo", json={'slot_id': p['id'], 'expected_version_id': current['id'], 'reason_category': '构图不符', 'detail_comment': '请换一个视角'})
    assert resp.status_code == 200, resp.text
    d = client.get(f'/api/designs/{d["id"]}').json(); new = d['slots']['panorama']['current_version']
    assert new['prompt_metadata']['panorama_view']['id'] != initial['id']
    assert d['structured_spec']['panorama_view'] == new['prompt_metadata']['panorama_view']
    assert first['prompt_metadata']['panorama_view'] == initial


def test_legacy_prompt_is_not_assigned_a_view():
    spec = {'panorama_prompt': '镜头设定：45度斜向；设计语言：木制家具。'}
    prompt = build_real_generation_prompt('panorama', spec, 'OLD')
    assert prompt.startswith(spec['panorama_prompt'])
    assert '本次全景镜头要求' not in prompt


def test_legacy_redo_assigns_view_only_when_requested(client):
    batch = _create(client)
    assert worker_instance.process_next_task()
    design = client.get(f'/api/designs/batch/{batch}').json()[0]
    slot = design['slots']['panorama']; version = slot['current_version']['id']
    with get_db(immediate=True) as conn:
        spec = design['structured_spec']; spec.pop('panorama_view')
        conn.execute('UPDATE designs SET structured_spec=? WHERE id=?', (json.dumps(spec), design['id']))
        meta = slot['current_version']['prompt_metadata']; meta.pop('panorama_view')
        conn.execute('UPDATE slot_versions SET prompt_metadata=? WHERE id=?', (json.dumps(meta), version))
    resp = client.post(f"/api/designs/{design['id']}/review_panorama", json={'action': 'redo', 'expected_version_id': version, 'reason_category': '构图不符', 'detail_comment': '调整灯光'})
    assert resp.status_code == 200
    assert worker_instance.process_next_task()
    current = client.get(f'/api/designs/{design["id"]}').json()
    assert 'panorama_view' not in current['structured_spec']
    assert 'panorama_view' not in current['slots']['panorama']['current_version']['prompt_metadata']
    resp = client.post(f"/api/designs/{design['id']}/review_panorama", json={'action': 'redo', 'expected_version_id': current['slots']['panorama']['current_version']['id'], 'reason_category': '构图不符', 'detail_comment': '换个视角'})
    assert resp.status_code == 200
    assert client.get(f'/api/designs/{design["id"]}').json()['structured_spec']['panorama_view']['id'] in PANORAMA_VIEWS
