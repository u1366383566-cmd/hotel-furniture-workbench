from backend.app.database import get_db
from backend.app.image_adapter import ImageResultDownloadError
from backend.app import task_worker


def test_two_download_failures_pause_and_resolution_resumes(client, monkeypatch):
    response = client.post('/api/batches', headers={'X-User-Role': 'leader'}, json={
        'name': '下载隔离', 'enabled_series': ['modern_business'],
        'target_archived_count': 2, 'designs_per_series': 2,
        'standby_designs_per_series': 0, 'auto_start_panorama': True,
    })
    assert response.status_code == 200, response.text
    bid = response.json()['id']
    monkeypatch.setattr(task_worker, 'load_image_api_config', lambda conn: {'enabled': True})
    def fail(*args, **kwargs):
        raise ImageResultDownloadError('下载失败')
    monkeypatch.setattr(task_worker, 'generate_image_real', fail)
    assert task_worker.worker_instance.process_next_task()
    with get_db() as conn:
        assert conn.execute('SELECT status FROM batches WHERE id=?', (bid,)).fetchone()[0] == 'active'
    assert task_worker.worker_instance.process_next_task()
    with get_db() as conn:
        assert conn.execute('SELECT pause_reason FROM batches WHERE id=?', (bid,)).fetchone()[0] == 'PAUSE_RESULT_DOWNLOAD'
        tasks = conn.execute("SELECT id FROM tasks WHERE batch_id=? AND status='unknown'", (bid,)).fetchall()
        assert len(tasks) == 2
    resolved = client.post('/api/system/resolve_unknown', headers={'X-User-Role':'leader'}, json={
        'task_id': tasks[0]['id'], 'resolution':'confirm_failure',
        'evidence':'账单核对无消费', 'billing_reference':'BILL12345', 'attests_no_charge':True,
    })
    assert resolved.status_code == 200, resolved.text
    with get_db() as conn:
        state = conn.execute('SELECT status,pause_reason FROM batches WHERE id=?', (bid,)).fetchone()
        assert state['status'] == 'active' and state['pause_reason'] is None


def test_download_failure_does_not_override_manual_pause(client, monkeypatch):
    response = client.post('/api/batches', headers={'X-User-Role': 'leader'}, json={
        'name': '人工暂停保护', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'designs_per_series': 1,
        'standby_designs_per_series': 0, 'auto_start_panorama': True,
    })
    bid = response.json()['id']
    monkeypatch.setattr(task_worker, 'load_image_api_config', lambda conn: {'enabled': True})
    def fail(*args, **kwargs):
        with get_db(immediate=True) as conn:
            conn.execute("UPDATE batches SET status='paused',pause_reason='manual' WHERE id=?", (bid,))
        raise ImageResultDownloadError('下载失败')
    monkeypatch.setattr(task_worker, 'generate_image_real', fail)
    assert task_worker.worker_instance.process_next_task()
    with get_db() as conn:
        state = conn.execute('SELECT status,pause_reason FROM batches WHERE id=?', (bid,)).fetchone()
    assert (state['status'], state['pause_reason']) == ('paused', 'manual')


def test_auth_failure_keeps_manual_pause_and_records_circuit(client, monkeypatch):
    response = client.post('/api/batches', headers={'X-User-Role': 'leader'}, json={
        'name': '人工暂停及鉴权故障', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'designs_per_series': 1,
        'standby_designs_per_series': 0, 'auto_start_panorama': True,
    })
    bid = response.json()['id']
    monkeypatch.setattr(task_worker, 'load_image_api_config', lambda conn: {'enabled': True})
    def fail(*args, **kwargs):
        with get_db(immediate=True) as conn:
            conn.execute("UPDATE batches SET status='paused',pause_reason='manual' WHERE id=?", (bid,))
        raise PermissionError('auth rejected')
    monkeypatch.setattr(task_worker, 'generate_image_real', fail)
    assert task_worker.worker_instance.process_next_task()
    with get_db() as conn:
        state = conn.execute('SELECT status,pause_reason,is_circuit_broken FROM batches WHERE id=?', (bid,)).fetchone()
    assert (state['status'], state['pause_reason'], state['is_circuit_broken']) == ('paused', 'manual', 1)


def test_old_request_failure_does_not_clear_new_slot_version(client, monkeypatch):
    response = client.post('/api/batches', headers={'X-User-Role': 'leader'}, json={
        'name': '旧任务回写保护', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'designs_per_series': 1,
        'standby_designs_per_series': 0, 'auto_start_panorama': True,
    })
    bid = response.json()['id']
    monkeypatch.setattr(task_worker, 'load_image_api_config', lambda conn: {'enabled': True})
    def fail(*args, **kwargs):
        with get_db(immediate=True) as conn:
            row = conn.execute("""SELECT t.slot_id,t.slot_version_id,t.design_id
                FROM tasks t WHERE t.batch_id=? AND t.status='running'""", (bid,)).fetchone()
            conn.execute("""INSERT INTO slot_versions(id,slot_id,design_id,version_num,prompt,process_status)
                VALUES ('new_version',?,?,2,'new','completed')""", (row['slot_id'], row['design_id']))
            conn.execute("UPDATE slots SET current_version_id='new_version',status='ready' WHERE id=?", (row['slot_id'],))
        raise PermissionError('auth rejected')
    monkeypatch.setattr(task_worker, 'generate_image_real', fail)
    assert task_worker.worker_instance.process_next_task()
    with get_db() as conn:
        slot = conn.execute("""SELECT s.current_version_id,s.status FROM slots s
            JOIN designs d ON d.id=s.design_id WHERE d.batch_id=? AND s.slot_type='panorama'""", (bid,)).fetchone()
    assert (slot['current_version_id'], slot['status']) == ('new_version', 'ready')


def test_result_resolution_does_not_resume_when_other_unknown_exists(client, monkeypatch):
    response = client.post('/api/batches', headers={'X-User-Role': 'leader'}, json={
        'name': '多种未知保护', 'enabled_series': ['modern_business'],
        'target_archived_count': 2, 'designs_per_series': 2,
        'standby_designs_per_series': 0, 'auto_start_panorama': True,
    })
    bid = response.json()['id']
    monkeypatch.setattr(task_worker, 'load_image_api_config', lambda conn: {'enabled': True})
    def fail(*args, **kwargs):
        raise ImageResultDownloadError('下载失败')
    monkeypatch.setattr(task_worker, 'generate_image_real', fail)
    assert task_worker.worker_instance.process_next_task()
    assert task_worker.worker_instance.process_next_task()
    with get_db(immediate=True) as conn:
        tasks = conn.execute("SELECT id FROM tasks WHERE batch_id=? ORDER BY id", (bid,)).fetchall()
        conn.execute("UPDATE tasks SET failure_stage='unknown' WHERE id=?", (tasks[1]['id'],))
    resolved = client.post('/api/system/resolve_unknown', headers={'X-User-Role':'leader'}, json={
        'task_id': tasks[0]['id'], 'resolution':'confirm_failure',
        'evidence':'账单核对无消费', 'billing_reference':'BILL12345', 'attests_no_charge':True,
    })
    assert resolved.status_code == 200, resolved.text
    with get_db() as conn:
        assert conn.execute("SELECT status FROM batches WHERE id=?", (bid,)).fetchone()[0] == 'paused'
