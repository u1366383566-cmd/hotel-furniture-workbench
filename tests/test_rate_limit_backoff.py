from backend.app.database import get_db
from backend.app.image_adapter import ExplicitRateLimitError
from backend.app import task_worker


def _batch(client):
    response = client.post('/api/batches', headers={'X-User-Role': 'leader'}, json={
        'name': '429 backoff', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'designs_per_series': 1,
        'standby_designs_per_series': 0, 'auto_start_panorama': True,
    })
    assert response.status_code == 200, response.text
    return response.json()['id']


def test_confirmed_rate_limit_delays_without_new_reservation(client, monkeypatch):
    bid = _batch(client)
    monkeypatch.setenv('WORKBENCH_429_CONFIRMED_UNACCEPTED', '1')
    monkeypatch.setattr(task_worker, 'load_image_api_config', lambda conn: {'enabled': True})
    monkeypatch.setattr(task_worker.random, 'randint', lambda a, b: 0)
    def fail(*args, **kwargs):
        raise ExplicitRateLimitError('limited', '120')
    monkeypatch.setattr(task_worker, 'generate_image_real', fail)
    assert task_worker.worker_instance.process_next_task()
    assert not task_worker.worker_instance.process_next_task()
    with get_db() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE batch_id=?", (bid,)).fetchone()
        remaining = conn.execute("SELECT (julianday(?) - julianday('now','localtime')) * 86400", (task['retry_after'],)).fetchone()[0]
        ledger = conn.execute("SELECT status FROM budget_ledger WHERE reference_task_id=? AND ledger_event_type='generation_request'", (task['id'],)).fetchall()
    assert task['status'] == 'queued' and task['retry_count'] == 1
    assert 115 <= remaining <= 120
    assert len(ledger) == 1 and ledger[0]['status'] == 'reserved'


def test_unconfirmed_rate_limit_is_not_reissued(client, monkeypatch):
    bid = _batch(client)
    monkeypatch.delenv('WORKBENCH_429_CONFIRMED_UNACCEPTED', raising=False)
    monkeypatch.setattr(task_worker, 'load_image_api_config', lambda conn: {'enabled': True})
    def fail(*args, **kwargs):
        raise ExplicitRateLimitError('limited')
    monkeypatch.setattr(task_worker, 'generate_image_real', fail)
    assert task_worker.worker_instance.process_next_task()
    assert not task_worker.worker_instance.process_next_task()
    with get_db() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE batch_id=?", (bid,)).fetchone()
        ledger = conn.execute("SELECT status FROM budget_ledger WHERE reference_task_id=? AND ledger_event_type='generation_request'", (task['id'],)).fetchone()
    assert task['status'] == 'unknown'
    assert ledger['status'] == 'unknown_held'


def test_delayed_retry_survives_worker_recovery(client, monkeypatch):
    bid = _batch(client)
    monkeypatch.setenv('WORKBENCH_429_CONFIRMED_UNACCEPTED', '1')
    monkeypatch.setattr(task_worker, 'load_image_api_config', lambda conn: {'enabled': True})
    def fail(*args, **kwargs):
        raise ExplicitRateLimitError('limited', '120')
    monkeypatch.setattr(task_worker, 'generate_image_real', fail)
    assert task_worker.worker_instance.process_next_task()
    task_worker.worker_instance.recover_interrupted_tasks()
    assert not task_worker.worker_instance.process_next_task()
    with get_db() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE batch_id=?", (bid,)).fetchone()
        ledger = conn.execute("SELECT status FROM budget_ledger WHERE reference_task_id=? AND ledger_event_type='generation_request'", (task['id'],)).fetchall()
    assert task['status'] == 'queued' and task['retry_count'] == 1 and task['retry_after']
    assert len(ledger) == 1 and ledger[0]['status'] == 'reserved'


def test_stale_unsent_task_releases_reservation_but_retried_task_does_not(client):
    bid = _batch(client)
    with get_db(immediate=True) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE batch_id=?", (bid,)).fetchone()
        conn.execute("UPDATE slots SET current_version_id=NULL WHERE id=?", (task['slot_id'],))
    assert not task_worker.worker_instance.process_next_task()
    with get_db() as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (task['id'],)).fetchone()[0] == 'cancelled'
        assert conn.execute("SELECT status FROM budget_ledger WHERE reference_task_id=? AND ledger_event_type='generation_request'", (task['id'],)).fetchone()[0] == 'released'

    bid2 = _batch(client)
    with get_db(immediate=True) as conn:
        task2 = conn.execute("SELECT * FROM tasks WHERE batch_id=?", (bid2,)).fetchone()
        conn.execute("UPDATE slots SET current_version_id=NULL WHERE id=?", (task2['slot_id'],))
        conn.execute("UPDATE tasks SET retry_count=1 WHERE id=?", (task2['id'],))
    assert not task_worker.worker_instance.process_next_task()
    with get_db() as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (task2['id'],)).fetchone()[0] == 'queued'
        assert conn.execute("SELECT status FROM budget_ledger WHERE reference_task_id=? AND ledger_event_type='generation_request'", (task2['id'],)).fetchone()[0] == 'reserved'
