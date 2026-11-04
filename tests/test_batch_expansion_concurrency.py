import json
import threading
from concurrent.futures import ThreadPoolExecutor

from backend.app.database import get_db
from backend.app.task_worker import worker_instance


def test_expand_batch_preserves_rules_and_creates_28_planning_jobs(client):
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
    with get_db(immediate=True) as conn:
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
