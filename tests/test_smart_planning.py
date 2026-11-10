import json
import io
from PIL import Image

from backend.app.database import get_db
from backend.app.planning_service import build_real_generation_prompt
from backend.app.task_worker import worker_instance

LEADER = {'X-User-Role': 'leader', 'X-User-ID': 'planning-test'}
OPERATOR = {'X-User-Role': 'operator'}


def proposal(headboard='横向分层木饰面床屏'):
    return {'headboard': headboard, 'bed_frame': '矮悬浮木床体', 'nightstand': '双抽屉悬挂柜',
            'wardrobe': '开放格衣柜', 'main_materials': ['胡桃木'], 'color_scheme': ['暖灰'],
            'companion_items': ['布艺沙发'], 'soft_furnishings': '米白床品',
            'design_difference': '横向分层床屏', 'panorama_prompt': '完整的酒店客房：横向床屏与胡桃木家具。'}


def create(client, name='智能规划测试', counts=None):
    counts = counts or {'modern_business': {'primary': 2, 'standby': 1}}
    response = client.post('/api/batches', headers=LEADER, json={
        'name': name, 'enabled_series': list(counts), 'series_counts': counts,
        'target_archived_count': 2, 'warning_budget': 20, 'hard_limit_budget': 25,
        'auto_start_panorama': False, 'smart_planning': True, 'planning_brief': '暖色'} )
    assert response.status_code == 200, response.text
    return response.json()['id']


def configure(client):
    assert client.put('/api/system/text_api', headers=LEADER, json={
        'base_url': 'http://text.test', 'model': 'planner', 'api_key': 'test-key', 'timeout_seconds': 10}).status_code == 200


def confirm(client, batch, appended=False):
    plans = client.get(f'/api/planning/batch/{batch}').json()['designs']
    if appended:
        plans = [p for p in plans if p['status'] in ('pending_append', 'ready_append')]
    payload = {'expected_specs': {p['id']: p['spec'] for p in plans}}
    action = 'confirm_appended' if appended else 'confirm'
    return client.post(f'/api/planning/batch/{batch}/{action}', headers=LEADER, json=payload)


def test_planning_confirm_start_retry_and_review(client, monkeypatch):
    configure(client)
    batch = create(client)
    plans = client.get(f'/api/planning/batch/{batch}').json()['designs']
    assert len(plans) == 3 and sum(p['standby'] for p in plans) == 1
    assert client.post(f'/api/batches/{batch}/start', headers=LEADER).status_code == 409
    assert confirm(client, batch).status_code == 409
    with get_db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks WHERE batch_id=?', (batch,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM budget_ledger WHERE batch_id=? AND category='planning_model'", (batch,)).fetchone()[0] == 0
    captured = []
    def mock_chat(cfg, system, user, model, timeout):
        captured.append(json.loads(user))
        return json.dumps(proposal(), ensure_ascii=False)
    monkeypatch.setattr('backend.app.routers.smart_planning.chat_completion_real', mock_chat)
    first = plans[0]['id']
    assert client.post(f'/api/planning/design/{first}/generate', headers=OPERATOR, json={}).status_code == 403
    assert client.post(f'/api/planning/design/{first}/generate', headers=LEADER, json={}).status_code == 200
    monkeypatch.setattr('backend.app.routers.smart_planning.chat_completion_real', lambda *args: '{invalid}')
    assert client.post(f'/api/planning/design/{plans[1]["id"]}/generate', headers=LEADER, json={}).status_code == 502
    after_failure = client.get(f'/api/planning/batch/{batch}').json()['designs']
    assert [p['status'] for p in after_failure].count('ready') == 1
    monkeypatch.setattr('backend.app.routers.smart_planning.chat_completion_real', mock_chat)
    for p in plans[1:]:
        assert client.post(f'/api/planning/design/{p["id"]}/generate', headers=LEADER, json={}).status_code == 200
    assert captured[-1]['existing_designs']
    ready = client.get(f'/api/planning/batch/{batch}').json()['designs']
    assert ready[0]['similarities']
    current = ready[0]['spec']
    changed = {**current, 'panorama_prompt': '人工修改后的提示词'}
    edit = {'spec': changed, 'expected_spec': current, 'prompt_checked': False}
    assert client.put(f'/api/planning/design/{first}', headers=LEADER, json=edit).status_code == 400
    edit['prompt_checked'] = True
    assert client.put(f'/api/planning/design/{first}', headers=LEADER, json=edit).status_code == 200
    assert client.put(f'/api/planning/design/{first}', headers=LEADER, json=edit).status_code == 409
    prompt = build_real_generation_prompt('panorama', changed, 'TEST')
    assert prompt.startswith('人工修改后的提示词')
    assert '正方形 1:1' in prompt
    assert '清晰呈现本方案指定的床屏' in prompt
    assert client.post(f'/api/planning/batch/{batch}/confirm', headers=LEADER, json={'expected_specs': {}}).status_code == 409
    assert confirm(client, batch).status_code == 200
    assert client.post(f'/api/planning/design/{first}/generate', headers=LEADER, json={}).status_code == 409
    assert client.post(f'/api/batches/{batch}/start', headers=LEADER).status_code == 200
    assert client.post(f'/api/batches/{batch}/start', headers=LEADER).status_code == 409
    worker_instance.process_next_task()
    d = client.get(f'/api/designs/{first}').json()
    version = d['slots']['panorama']['current_version']['id']
    assert client.put(f'/api/planning/design/{first}/features', headers=OPERATOR, json={'expected_version_id': 'old', 'features': {'headboard':'成图床屏'}}).status_code == 409
    assert client.put(f'/api/planning/design/{first}/features', headers=OPERATOR, json={'expected_version_id': version, 'features': {'headboard':'成图床屏'}}).status_code == 200
    assert client.get(f'/api/planning/design/{first}/features').json()['verified_features']['headboard'] == '成图床屏'
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE designs SET status='archived' WHERE id=?", (first,))
        audit = conn.execute("SELECT detail FROM audit_logs WHERE batch_id=? AND action='confirm_plans'", (batch,)).fetchone()
        assert json.loads(audit['detail'])['similarities']
    other = create(client, name='查重历史')
    target = client.get(f'/api/planning/batch/{other}').json()['designs'][0]['id']
    monkeypatch.setattr('backend.app.routers.smart_planning.chat_completion_real', lambda *args: json.dumps(proposal('成图床屏'), ensure_ascii=False))
    assert client.post(f'/api/planning/design/{target}/generate', headers=LEADER, json={}).status_code == 200
    candidates = client.get(f'/api/planning/design/{target}/similarities').json()
    assert any(c['id'] == first and c['feature_source'] == '人工核实' and c['preview_version_id'] == version for c in candidates)


def test_scope_and_per_series_counts(client, monkeypatch):
    configure(client)
    monkeypatch.setattr('backend.app.routers.smart_planning.chat_completion_real', lambda *args: json.dumps(proposal(), ensure_ascii=False))
    batch = create(client, counts={'modern_business': {'primary': 1, 'standby': 1}, 'natural_wood': {'primary': 2, 'standby': 0}})
    plans = client.get(f'/api/planning/batch/{batch}').json()['designs']
    assert len(plans) == 4 and sum(p['standby'] for p in plans) == 1
    for p in plans:
        assert client.post(f'/api/planning/design/{p["id"]}/generate', headers=LEADER, json={}).status_code == 200
    assert confirm(client, batch).status_code == 200
    other = create(client, name='另一批')
    other_plan = client.get(f'/api/planning/batch/{other}').json()['designs'][0]
    assert client.post(f'/api/planning/design/{other_plan["id"]}/generate', headers=LEADER, json={}).status_code == 200
    matches = client.get(f'/api/planning/design/{other_plan["id"]}/similarities').json()
    assert any(m['batch_id'] == batch for m in matches)


def test_model_text_list_fields_are_normalized(client, monkeypatch):
    configure(client)
    result = proposal()
    result.update(main_materials='胡桃木、金属', color_scheme='暖灰，米白', companion_items='沙发；书桌')
    monkeypatch.setattr('backend.app.routers.smart_planning.chat_completion_real', lambda *args: json.dumps(result, ensure_ascii=False))
    batch = create(client, counts={'modern_business': {'primary': 1, 'standby': 0}})
    item = client.get(f'/api/planning/batch/{batch}').json()['designs'][0]
    response = client.post(f'/api/planning/design/{item["id"]}/generate', headers=LEADER, json={})
    assert response.status_code == 200, response.text
    spec = client.get(f'/api/planning/batch/{batch}').json()['designs'][0]['spec']
    assert spec['color_scheme'] == ['暖灰', '米白']
    result['color_scheme'] = {'主色': '暖灰', '辅助色': '米白'}
    response = client.post(f'/api/planning/design/{item["id"]}/generate', headers=LEADER, json={})
    assert response.status_code == 200, response.text
    spec = client.get(f'/api/planning/batch/{batch}').json()['designs'][0]['spec']
    assert spec['color_scheme'] == ['主色：暖灰', '辅助色：米白']


def test_confirmed_prompt_is_sent_to_image_adapter(client, monkeypatch):
    configure(client)
    monkeypatch.setattr('backend.app.routers.smart_planning.chat_completion_real', lambda *args: json.dumps(proposal(), ensure_ascii=False))
    batch = create(client, counts={'modern_business': {'primary': 1, 'standby': 0}})
    design = client.get(f'/api/planning/batch/{batch}').json()['designs'][0]
    assert client.post(f'/api/planning/design/{design["id"]}/generate', headers=LEADER, json={}).status_code == 200
    assert confirm(client, batch).status_code == 200
    assert client.post(f'/api/batches/{batch}/start', headers=LEADER).status_code == 200
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE image_api_configs SET enabled=1, base_url='http://image.test', model='test', api_key='key'")
    image = io.BytesIO()
    Image.new('RGB', (64, 64), 'white').save(image, format='PNG')
    calls = []
    def generate(cfg, prompt):
        calls.append(prompt)
        return image.getvalue(), 'test-request'
    monkeypatch.setattr('backend.app.task_worker.generate_image_real', generate)
    assert worker_instance.process_next_task()
    assert calls and calls[0].startswith(proposal()['panorama_prompt'])
    assert calls[0].index('清晰呈现本方案指定的床屏') < calls[0].index('家具须作为统一系列完整呈现')
    with get_db() as conn:
        version = conn.execute('SELECT prompt_metadata FROM slot_versions WHERE design_id=?', (design['id'],)).fetchone()
        assert json.loads(version['prompt_metadata'])['real_prompt'] == calls[0]


def test_rule_based_panorama_has_anchor():
    prompt = build_real_generation_prompt('panorama', {'headboard': '木饰面床屏'}, 'TEST')
    assert '清晰呈现本方案指定的床屏' in prompt


def test_panorama_uses_actual_series_restrictions():
    spec = {'panorama_prompt': '窗边的蓝色床屏', 'forbidden_elements': ['雕花'], 'forbidden_processes': ['高光烤漆']}
    prompt = build_real_generation_prompt('panorama', spec, 'TEST')
    assert '本系列禁止元素：雕花；禁止工艺：高光烤漆' in prompt
    assert '禁止引入规则之外的造型元素（雕花、异形装饰线条' not in prompt


def test_panorama_coverage_line_lists_companion_items():
    spec = {'headboard': '木饰面床屏', 'companion_items': ['深胡桃木行李架', '暖灰色科技皮革床尾凳']}
    prompt = build_real_generation_prompt('panorama', spec, 'TEST')
    assert '家具覆盖要求' in prompt
    assert '左右两只床头柜必须全部可见' in prompt
    assert '深胡桃木行李架' in prompt and '暖灰色科技皮革床尾凳' in prompt


def test_appended_standby_requires_its_own_review(client, monkeypatch):
    configure(client)
    monkeypatch.setattr('backend.app.routers.smart_planning.chat_completion_real', lambda *args: json.dumps(proposal(), ensure_ascii=False))
    batch = create(client, counts={'modern_business': {'primary': 1, 'standby': 0}})
    first = client.get(f'/api/planning/batch/{batch}').json()['designs'][0]['id']
    assert client.post(f'/api/planning/design/{first}/generate', headers=LEADER, json={}).status_code == 200
    assert confirm(client, batch).status_code == 200
    assert client.post(f'/api/batches/{batch}/start', headers=LEADER).status_code == 200
    worker_instance.process_next_task()
    version = client.get(f'/api/designs/{first}').json()['slots']['panorama']['current_version']['id']
    assert client.post(f'/api/designs/{first}/review_panorama', headers=OPERATOR, json={'action': 'eliminate', 'expected_version_id': version, 'reason_category': '风格不符'}).status_code == 200
    assert client.post(f'/api/batches/{batch}/append_standby', headers=LEADER).status_code == 200
    appended = next(p for p in client.get(f'/api/planning/batch/{batch}').json()['designs'] if p['standby'])
    assert appended['status'] == 'pending_append'
    assert client.post(f'/api/planning/design/{appended["id"]}/generate', headers=LEADER, json={}).status_code == 200
    assert client.post(f'/api/batches/{batch}/approve_warning', headers=LEADER).status_code == 200
    with get_db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks WHERE design_id=?', (appended['id'],)).fetchone()[0] == 0
    assert confirm(client, batch, appended=True).status_code == 200
    with get_db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks WHERE design_id=?', (appended['id'],)).fetchone()[0] == 1
