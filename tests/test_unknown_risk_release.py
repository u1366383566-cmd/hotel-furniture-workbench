import json
import pytest

from backend.app.database import get_db
from backend.app.image_adapter import ImageRequestTimeout, ImageRequestServerError
import backend.app.task_worker as worker_module


def test_restart_unknown_isolation_resumes_other_design_without_releasing(client, monkeypatch):
    batch=client.post('/api/batches',json={'name':'restart isolation','enabled_series':['modern_business'],'target_archived_count':2,'designs_per_series':2,'standby_designs_per_series':0,'auto_start_panorama':True},headers={'X-User-Role':'leader'}).json()
    with get_db(immediate=True) as conn:
        task=conn.execute('SELECT * FROM tasks WHERE batch_id=? ORDER BY id LIMIT 1',(batch['id'],)).fetchone()
        conn.execute("UPDATE tasks SET status='unknown',last_error='Interrupted by restart, unverified upstream state' WHERE id=?",(task['id'],))
        conn.execute("UPDATE slot_versions SET process_status='unknown_pending_confirmation',is_unknown=1 WHERE id=?",(task['slot_version_id'],))
        conn.execute("UPDATE budget_ledger SET status='unknown_held' WHERE reference_task_id=?",(task['id'],))
        conn.execute("UPDATE batches SET status='paused',pause_reason='PAUSE_UNKNOWN_PENDING' WHERE id=?",(batch['id'],))
    url=f"/api/workbench/batches/{batch['id']}/isolate-restart-unknowns"
    assert client.post(url,headers={'X-User-Role':'operator'}).status_code==403
    response=client.post(url,headers={'X-User-Role':'admin'})
    assert response.status_code==200 and response.json()['isolated']==1
    assert worker_module.worker_instance.process_next_task()
    with get_db() as conn:
        assert conn.execute('SELECT status FROM budget_ledger WHERE reference_task_id=?',(task['id'],)).fetchone()[0]=='unknown_held'
        assert conn.execute('SELECT status FROM tasks WHERE id=?',(task['id'],)).fetchone()[0]=='unknown'
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE batch_id=? AND status='completed'",(batch['id'],)).fetchone()[0]==1


def make_task(client, monkeypatch, error, enabled):
    batch=client.post('/api/batches',json={'name':'risk test','enabled_series':['modern_business'],'target_archived_count':1,'designs_per_series':1,'standby_designs_per_series':0,'auto_start_panorama':True},headers={'X-User-Role':'leader'}).json()
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE workbench_settings SET auto_release_unknown=? WHERE id='default'",(int(enabled),))
    monkeypatch.setattr(worker_module,'load_image_api_config',lambda conn:{'enabled':True})
    def fail(*args,**kwargs):raise error
    monkeypatch.setattr(worker_module,'generate_image_real',fail)
    worker_module.worker_instance.process_next_task()
    with get_db() as conn:
        task=conn.execute('SELECT * FROM tasks WHERE batch_id=?',(batch['id'],)).fetchone()
        ledger=conn.execute('SELECT * FROM budget_ledger WHERE reference_task_id=? AND category=\'image_generation\'',(task['id'],)).fetchone()
        state=conn.execute('SELECT status,pause_reason FROM batches WHERE id=?',(batch['id'],)).fetchone()
        audit=conn.execute("SELECT detail FROM audit_logs WHERE batch_id=? AND action='auto_release_unknown'",(batch['id'],)).fetchone()
        return batch,dict(task),dict(ledger),dict(state),dict(audit) if audit else None


@pytest.mark.parametrize('error',[ImageRequestTimeout('生图上游超时: timeout'),ImageRequestServerError('生图上游服务端错误 HTTP 502')])
def test_accepted_request_risk_releases_and_audits(client,monkeypatch,error):
    batch,task,ledger,state,audit=make_task(client,monkeypatch,error,True)
    assert task['status']=='failed' and ledger['status']=='released' and state['status']=='active'
    assert json.loads(audit['detail'])['upstream_charge']=='unknown'


def test_unknown_charge_release_never_automatically_reissues(client, monkeypatch):
    batch = client.post('/api/batches', json={
        'name':'unknown no resend','enabled_series':['modern_business'],
        'target_archived_count':1,'designs_per_series':1,
        'standby_designs_per_series':0,'auto_start_panorama':True,
    }, headers={'X-User-Role':'leader'}).json()
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE workbench_settings SET auto_release_unknown=1 WHERE id='default'")
    calls = []
    monkeypatch.setattr(worker_module, 'load_image_api_config', lambda conn: {'enabled':True})
    def fail(*args, **kwargs):
        calls.append(1)
        raise ImageRequestTimeout('生图上游超时: timeout')
    monkeypatch.setattr(worker_module, 'generate_image_real', fail)
    assert worker_module.worker_instance.process_next_task()
    assert not worker_module.worker_instance.process_next_task()
    worker_module.worker_instance.recover_interrupted_tasks()
    assert not worker_module.worker_instance.process_next_task()
    assert len(calls) == 1
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE batch_id=?", (batch['id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('error,enabled',[(ImageRequestTimeout('生图上游超时: timeout'),False),(TimeoutError('下载生图结果超时'),True),(ConnectionResetError('响应非 JSON'),True)])
def test_noneligible_or_disabled_stays_unknown(client,monkeypatch,error,enabled):
    batch,task,ledger,state,audit=make_task(client,monkeypatch,error,enabled)
    assert task['status']=='unknown' and ledger['status']=='unknown_held' and state['pause_reason']=='PAUSE_UNKNOWN_PENDING' and audit is None


def test_explicit_http_4xx_is_a_known_failure_not_unknown(client, monkeypatch):
    batch, task, ledger, state, audit = make_task(
        client,
        monkeypatch,
        ValueError('HTTP 400: generations request rejected (Free quota exhausted, retry in 8 hours)'),
        False,
    )
    assert task['status'] == 'failed'
    assert task['failure_stage'] == 'request_rejected'
    assert task['retry_status'] == 'explicit_http_rejection'
    assert ledger['status'] == 'released'
    assert state['status'] == 'active' and state['pause_reason'] is None
    assert audit is None


def test_existing_http_4xx_unknown_can_be_resolved_and_requeued_without_billing_attestation(client, monkeypatch):
    batch, task, ledger, state, audit = make_task(
        client,
        monkeypatch,
        ValueError('HTTP 400: generations request rejected (Free quota exhausted, retry in 8 hours)'),
        False,
    )
    with get_db(immediate=True) as conn:
        conn.execute(
            """UPDATE tasks SET status='unknown',failure_stage=NULL,retry_status=NULL,
               last_error='HTTP 400: generations request rejected (Free quota exhausted, retry in 8 hours)'
               WHERE id=?""",
            (task['id'],),
        )
        conn.execute(
            """UPDATE slot_versions
               SET process_status='unknown_pending_confirmation',is_unknown=1,
                   error_message='HTTP 400: generations request rejected (Free quota exhausted, retry in 8 hours)'
               WHERE id=?""",
            (task['slot_version_id'],),
        )
        conn.execute(
            "UPDATE budget_ledger SET status='unknown_held',delta_reserved=1,delta_used=0 WHERE id=?",
            (ledger['id'],),
        )
        conn.execute(
            "UPDATE batches SET status='paused',pause_reason='PAUSE_UNKNOWN_PENDING' WHERE id=?",
            (batch['id'],),
        )

    listing = client.get(
        f"/api/system/batches/{batch['id']}/unknown_tasks",
        headers={'X-User-Role':'admin'},
    )
    assert listing.status_code == 200
    assert listing.json()[0]['explicit_rejection'] is True

    resolved = client.post(
        '/api/system/resolve_unknown',
        headers={'X-User-Role':'admin'},
        json={'task_id':task['id'],'resolution':'confirm_request_rejected'},
    )
    assert resolved.status_code == 200, resolved.text

    failures = client.get(
        f"/api/workbench/batches/{batch['id']}/technical-failures",
        headers={'X-User-Role':'admin'},
    )
    assert failures.status_code == 200
    item = failures.json()['items'][0]
    assert item['retryable'] is True and item['requires_risk_ack'] is False

    retried = client.post(
        f"/api/workbench/batches/{batch['id']}/retry-technical-failures",
        headers={'X-User-Role':'admin'},
        json={'task_ids':[task['id']],'accept_unknown_charge':False},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()['results'][0]['ok'] is True
    with get_db() as conn:
        row = conn.execute(
            'SELECT status,failure_stage,retry_status FROM tasks WHERE id=?',
            (task['id'],),
        ).fetchone()
        batch_after = conn.execute(
            'SELECT status,pause_reason FROM batches WHERE id=?',
            (batch['id'],),
        ).fetchone()
    assert (row['status'],row['failure_stage'],row['retry_status']) == (
        'queued',
        'request_retryable',
        'manual_retry_queued',
    )
    assert (batch_after['status'],batch_after['pause_reason']) == ('active',None)


def test_manual_risk_release_requires_explicit_ack_and_is_idempotent(client,monkeypatch):
    batch,task,ledger,state,audit=make_task(client,monkeypatch,ImageRequestTimeout('生图上游超时: timeout'),False)
    url='/api/system/release_unknown_risk'
    assert client.post(url,json={'task_id':task['id'],'accept_unknown_charge':True},headers={'X-User-Role':'operator'}).status_code==403
    assert client.post(url,json={'task_id':task['id'],'accept_unknown_charge':False},headers={'X-User-Role':'admin'}).status_code==400
    response=client.post(url,json={'task_id':task['id'],'accept_unknown_charge':True},headers={'X-User-Role':'admin'})
    assert response.status_code==200 and response.json()['upstream_charge']=='unknown' and response.json()['resumed']
    assert client.post(url,json={'task_id':task['id'],'accept_unknown_charge':True},headers={'X-User-Role':'admin'}).status_code==409
    with get_db() as conn:
        assert conn.execute('SELECT status FROM budget_ledger WHERE id=?',(ledger['id'],)).fetchone()['status']=='released'
        details=json.loads(conn.execute("SELECT detail FROM audit_logs WHERE action='risk_release_unknown'").fetchone()['detail'])
        assert details['risk_accepted'] and details['upstream_charge']=='unknown'


def test_legacy_risk_backfill_uses_audit_evidence(client, monkeypatch):
    from backend.app.database import _ensure_columns
    batch, task, ledger, state, audit = make_task(client, monkeypatch, ImageRequestTimeout('生图上游超时: timeout'), True)
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE tasks SET failure_stage=NULL WHERE id=?", (task['id'],))
        _ensure_columns(conn)
        assert conn.execute("SELECT failure_stage FROM tasks WHERE id=?", (task['id'],)).fetchone()[0] == 'request_charge_unknown'
        conn.execute("UPDATE tasks SET failure_stage=NULL WHERE id=?", (task['id'],))
        conn.execute("DELETE FROM audit_logs WHERE batch_id=? AND action='auto_release_unknown'", (batch['id'],))
        _ensure_columns(conn)
        assert conn.execute("SELECT failure_stage FROM tasks WHERE id=?", (task['id'],)).fetchone()[0] is None


def test_automatic_release_does_not_override_other_pause(client,monkeypatch):
    batch=client.post('/api/batches',json={'name':'pause test','enabled_series':['modern_business'],'target_archived_count':1,'designs_per_series':1,'standby_designs_per_series':0,'auto_start_panorama':True},headers={'X-User-Role':'leader'}).json()
    with get_db(immediate=True) as conn:conn.execute("UPDATE workbench_settings SET auto_release_unknown=1 WHERE id='default'")
    monkeypatch.setattr(worker_module,'load_image_api_config',lambda conn:{'enabled':True})
    def fail(*args,**kwargs):
        with get_db(immediate=True) as conn:conn.execute("UPDATE batches SET status='paused',pause_reason='manual' WHERE id=?",(batch['id'],))
        raise ImageRequestTimeout('生图上游超时: timeout')
    monkeypatch.setattr(worker_module,'generate_image_real',fail)
    worker_module.worker_instance.process_next_task()
    with get_db() as conn:
        state=conn.execute('SELECT status,pause_reason FROM batches WHERE id=?',(batch['id'],)).fetchone()
        assert (state['status'],state['pause_reason'])==('paused','manual')


def test_manual_risk_release_refuses_existing_image(client,monkeypatch):
    batch,task,ledger,state,audit=make_task(client,monkeypatch,ImageRequestTimeout('生图上游超时: timeout'),False)
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE slot_versions SET raw_image_path='exists.png' WHERE id=?",(task['slot_version_id'],))
    response=client.post('/api/system/release_unknown_risk',json={'task_id':task['id'],'accept_unknown_charge':True},headers={'X-User-Role':'admin'})
    assert response.status_code==409
    with get_db() as conn:
        assert conn.execute('SELECT status FROM budget_ledger WHERE id=?',(ledger['id'],)).fetchone()['status']=='unknown_held'


def test_manual_risk_release_keeps_batch_paused_for_another_unknown(client,monkeypatch):
    batch,task,ledger,state,audit=make_task(client,monkeypatch,ImageRequestTimeout('生图上游超时: timeout'),False)
    with get_db(immediate=True) as conn:
        conn.execute("INSERT INTO slot_versions(id,slot_id,design_id,prompt,process_status,is_unknown) SELECT 'other-unknown',slot_id,design_id,'x','unknown_pending_confirmation',1 FROM tasks WHERE id=?",(task['id'],))
    response=client.post('/api/system/release_unknown_risk',json={'task_id':task['id'],'accept_unknown_charge':True},headers={'X-User-Role':'admin'})
    assert response.status_code==200 and not response.json()['resumed']
    with get_db() as conn:
        row=conn.execute('SELECT status,pause_reason FROM batches WHERE id=?',(batch['id'],)).fetchone()
        assert row['status']=='paused' and row['pause_reason']=='PAUSE_UNKNOWN_PENDING'


def test_concurrent_request_timeouts_each_release_once(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    batch=client.post('/api/batches',json={'name':'parallel risk','enabled_series':['modern_business'],'target_archived_count':2,'designs_per_series':2,'standby_designs_per_series':0,'auto_start_panorama':True},headers={'X-User-Role':'leader'}).json()
    with get_db(immediate=True) as conn:conn.execute("UPDATE workbench_settings SET auto_release_unknown=1 WHERE id='default'")
    monkeypatch.setattr(worker_module,'load_image_api_config',lambda conn:{'enabled':True})
    barrier=Barrier(2,timeout=10)
    def fail(*args,**kwargs):
        barrier.wait()
        raise ImageRequestTimeout('生图上游超时: timeout')
    monkeypatch.setattr(worker_module,'generate_image_real',fail)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _:worker_module.worker_instance.process_next_task(),range(2)))==[True,True]
    with get_db() as conn:
        assert conn.execute("SELECT count(*) FROM tasks WHERE batch_id=? AND status='failed'",(batch['id'],)).fetchone()[0]==2
        assert conn.execute("SELECT count(*) FROM audit_logs WHERE batch_id=? AND action='auto_release_unknown'",(batch['id'],)).fetchone()[0]==2
        assert conn.execute("SELECT count(*) FROM budget_ledger WHERE batch_id=? AND category='image_generation' AND status='released'",(batch['id'],)).fetchone()[0]==2


def test_admin_settings_control_new_task_budget_and_preserve_legacy_put(client):
    url='/api/workbench/budget-settings'
    payload={'warning_per_group':8,'hard_limit_per_group':10,'auto_release_unknown':True}
    assert client.put(url,json=payload,headers={'X-User-Role':'operator'}).status_code==403
    assert client.put(url,json=payload,headers={'X-User-Role':'admin'}).status_code==200
    assert client.get(url,headers={'X-User-Role':'admin'}).json()=={
        **payload,'auto_retry_request_failures':False
    }
    assert client.put(url,json={'warning_per_group':8,'hard_limit_per_group':10},headers={'X-User-Role':'admin'}).status_code==200
    assert client.get(url,headers={'X-User-Role':'admin'}).json()['auto_release_unknown'] is True
    response=client.post('/api/batches',headers={'X-User-Role':'leader'},json={'name':'automatic budget','enabled_series':['modern_business'],'target_archived_count':2,'auto_workflow':True,'smart_planning':True,'auto_start_panorama':False,'series_counts':{'modern_business':{'primary':2,'standby':0}}})
    assert response.status_code==200, response.text
    assert response.json()['warning_budget']==16 and response.json()['hard_limit_budget']==20
