import json
import threading
import io
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

from backend.app.database import get_db
from backend.app.task_worker import worker_instance


def test_expand_batch_preserves_rules_and_creates_28_planning_jobs(client, monkeypatch):
    headers = {'X-User-Role': 'admin'}
    batch = client.post('/api/batches', headers=headers, json={
        'name': '扩充测试', 'enabled_series': ['modern_business'],
        'target_archived_count': 2, 'auto_start_panorama': False,
        'smart_planning': True, 'auto_workflow': True,
        'series_counts': {'modern_business': {'primary': 2, 'standby': 0}},
    }).json()
    bid = batch['id']
    with get_db(immediate=True) as conn:
        original = conn.execute('SELECT id,structured_spec FROM designs WHERE batch_id=?', (bid,)).fetchall()
        original_rule = conn.execute("SELECT rule_json,template_version,updated_by FROM series_rule_configs WHERE series_id='modern_business'").fetchone()
        conn.execute("UPDATE batches SET status='completed' WHERE id=?", (bid,))
    updated = client.put('/api/system/series_rules/modern_business', headers=headers, json={'headboard_types': ['扩充时的新床屏']})
    assert updated.status_code == 200
    payload = {'series_counts': {'modern_business': 30}, 'expected_series_counts': {'modern_business': 2}}
    response = client.put(f'/api/batches/{bid}/series-counts', headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['added'] == 28
    assert client.put(f'/api/batches/{bid}/series-counts', headers=headers, json=payload).status_code == 409
    with get_db() as conn:
        result = conn.execute('SELECT * FROM batches WHERE id=?', (bid,)).fetchone()
        designs = conn.execute('SELECT * FROM designs WHERE batch_id=?', (bid,)).fetchall()
        assert result['target_archived_count'] == 30 and result['status'] == 'active'
        assert result['warning_budget'] == 150 and result['hard_limit_budget'] == 180
        assert len(designs) == len({row['design_code'] for row in designs}) == 30
        assert conn.execute('SELECT COUNT(*) FROM planning_jobs WHERE batch_id=?', (bid,)).fetchone()[0] == 30
        assert {row['id']: row['structured_spec'] for row in designs if not row['series_rule_snapshot_json']} == {row['id']: row['structured_spec'] for row in original}
        assert all(json.loads(row['series_rule_snapshot_json'])['headboard_types'] == ['扩充时的新床屏'] for row in designs if row['series_rule_snapshot_json'])
        added_id = next(row['id'] for row in designs if row['series_rule_snapshot_json'])
    assert client.put('/api/system/text_api', headers=headers, json={'base_url':'http://test', 'model':'test', 'api_key':'test', 'timeout_seconds':10}).status_code == 200
    contexts = []
    def fake_chat(cfg, system, user, model, timeout):
        contexts.append(json.loads(user))
        return json.dumps({'headboard':'床屏','bed_frame':'床体','nightstand':'床头柜','wardrobe':'衣柜', 'main_materials':['木'], 'color_scheme':['灰'], 'companion_items':['椅'], 'soft_furnishings':'床品','design_difference':'造型','panorama_prompt':'全景'}, ensure_ascii=False)
    monkeypatch.setattr('backend.app.routers.smart_planning.chat_completion_real', fake_chat)
    assert client.post(f'/api/planning/design/{added_id}/generate', headers=headers, json={}).status_code == 200
    assert contexts[0]['series_rule']['headboard_types'] == ['扩充时的新床屏']
    with get_db(immediate=True) as conn:
        from backend.app.auto_planning_service import queue_replacement
        source = conn.execute('SELECT id,series_rule_snapshot_json FROM designs WHERE batch_id=? AND series_rule_snapshot_json IS NOT NULL LIMIT 1', (bid,)).fetchone()
        replacement = queue_replacement(conn, bid, 'modern_business', source['id'])
        rule = conn.execute('SELECT series_rule_snapshot_json FROM designs WHERE id=?', (replacement,)).fetchone()
        assert json.loads(rule['series_rule_snapshot_json']) == json.loads(source['series_rule_snapshot_json'])
        conn.execute("UPDATE series_rule_configs SET rule_json=?,template_version=?,updated_by=? WHERE series_id='modern_business'", tuple(original_rule))


def test_two_workers_claim_distinct_tasks_concurrently(client, monkeypatch):
    from backend.app.routers.batches import _queue_initial_panorama
    headers = {'X-User-Role': 'admin'}
    batch = client.post('/api/batches', headers=headers, json={
        'name': '并发测试', 'enabled_series': ['modern_business'], 'target_archived_count': 30,
        'smart_planning': True, 'auto_workflow': True, 'auto_start_panorama': False,
        'series_counts': {'modern_business': {'primary': 30, 'standby': 0}},
    }).json()
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active' WHERE id=?", (batch['id'],))
        for row in conn.execute('SELECT id,design_code FROM designs WHERE batch_id=?', (batch['id'],)).fetchall():
            _queue_initial_panorama(conn, batch['id'], row['id'], row['design_code'])
    barrier = threading.Barrier(2, timeout=5)
    claimed = []
    def execute(row):
        claimed.append(row['id'])
        barrier.wait()
        with get_db(immediate=True) as conn:
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (row['id'],))
    monkeypatch.setattr(worker_instance, '_execute_single_task', execute)
    def work():
        for _ in range(15):
            assert worker_instance.process_next_task()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: work(), range(2)))
    assert len(claimed) == len(set(claimed)) == 30


def test_expansion_keeps_pause_and_allows_new_series(client):
    headers = {'X-User-Role': 'artist'}
    batch = client.post('/api/batches', headers=headers, json={
        'name': '暂停扩充', 'enabled_series': ['modern_business'], 'target_archived_count': 1,
        'smart_planning': True, 'auto_workflow': True, 'auto_start_panorama': False,
        'series_counts': {'modern_business': {'primary': 1, 'standby': 0}},
    }).json()
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='paused',pause_reason='PAUSE_UNKNOWN_PENDING' WHERE id=?", (batch['id'],))
    response = client.put(f"/api/batches/{batch['id']}/series-counts", headers=headers, json={
        'expected_series_counts': {'modern_business': 1},
        'series_counts': {'modern_business': 1, 'natural_wood': 2},
    })
    assert response.status_code == 200, response.text
    with get_db() as conn:
        row = conn.execute('SELECT * FROM batches WHERE id=?', (batch['id'],)).fetchone()
        assert row['status'] == 'paused' and row['pause_reason'] == 'PAUSE_UNKNOWN_PENDING'
        assert 'natural_wood' in json.loads(row['approved_series_rules_json'])
        assert conn.execute("SELECT COUNT(*) FROM designs WHERE batch_id=? AND series_id='natural_wood' AND series_rule_snapshot_json IS NOT NULL", (batch['id'],)).fetchone()[0] == 2


def test_concurrent_unknown_pauses_batch_without_losing_other_result(client, monkeypatch):
    from backend.app.routers.batches import _queue_initial_panorama
    batch = client.post('/api/batches', headers={'X-User-Role':'admin'}, json={
        'name':'并发异常', 'enabled_series':['modern_business'], 'target_archived_count':2,
        'designs_per_series':2, 'standby_designs_per_series':0, 'auto_start_panorama':False,
    }).json()
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active' WHERE id=?", (batch['id'],))
        conn.execute("UPDATE image_api_configs SET enabled=1,base_url='http://mock',model='test',api_key='test' WHERE id='default'")
        for row in conn.execute('SELECT id,design_code FROM designs WHERE batch_id=?', (batch['id'],)).fetchall():
            _queue_initial_panorama(conn, batch['id'], row['id'], row['design_code'])
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    calls = []
    picture = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(picture, format='PNG')
    def generate(cfg, prompt):
        with lock:
            index = len(calls)
            calls.append(index)
        barrier.wait()
        if index == 0:
            raise TimeoutError('simulated timeout')
        return picture.getvalue(), 'mock-id'
    monkeypatch.setattr('backend.app.task_worker.generate_image_real', generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(lambda _: worker_instance.process_next_task(), range(2)))
    with get_db() as conn:
        state = conn.execute('SELECT status,pause_reason FROM batches WHERE id=?', (batch['id'],)).fetchone()
        statuses = [r['status'] for r in conn.execute('SELECT status FROM tasks WHERE batch_id=?', (batch['id'],))]
        assert state['status'] == 'paused' and state['pause_reason'] == 'PAUSE_UNKNOWN_PENDING'
        assert sorted(statuses) == ['completed','unknown']
        assert conn.execute("SELECT COUNT(*) FROM budget_ledger WHERE batch_id=? AND status='unknown_held'", (batch['id'],)).fetchone()[0] == 1
