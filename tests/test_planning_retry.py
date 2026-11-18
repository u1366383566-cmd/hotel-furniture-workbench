import pytest
from fastapi import HTTPException

from backend.app.auto_planning_service import process_next_plan
from backend.app.database import get_db
from backend.app.image_adapter import TemporaryPlanningError
from backend.app.routers import smart_planning
from backend.app.budget_service import BudgetHardLimitExceededError
from backend.app.routers import batches


@pytest.mark.parametrize("temporary", [True, False])
def test_planning_retries_only_temporary_failures(client, monkeypatch, temporary):
    response = client.post('/api/batches', headers={'X-User-Role': 'admin'}, json={
        'name': '规划失败重试', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'smart_planning': True, 'auto_workflow': True,
        'auto_start_panorama': False,
        'series_counts': {'modern_business': {'primary': 1, 'standby': 0}},
    })
    assert response.status_code == 200, response.text
    bid = response.json()['id']
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active' WHERE id=?", (bid,))

    def fail(*args, **kwargs):
        if temporary:
            try:
                raise TemporaryPlanningError('temporary')
            except TemporaryPlanningError as exc:
                raise HTTPException(502, 'temporary') from exc
        raise HTTPException(400, 'configuration error')

    monkeypatch.setattr(smart_planning, 'generate', fail)
    for attempt in range(3 if temporary else 1):
        assert process_next_plan()
        with get_db() as conn:
            row = conn.execute('SELECT status,technical_retry_count,retry_after,started_at FROM planning_jobs WHERE batch_id=?', (bid,)).fetchone()
        if temporary and attempt < 2:
            assert row['status'] == 'queued'
            assert row['technical_retry_count'] == attempt + 1
            assert row['retry_after']
            assert row['started_at']
            assert not process_next_plan()
            with get_db(immediate=True) as conn:
                conn.execute("UPDATE planning_jobs SET retry_after=NULL WHERE batch_id=?", (bid,))
        else:
            assert row['status'] == 'failed'


def test_completed_planning_is_retained_when_budget_blocks_panorama(client, monkeypatch):
    response = client.post('/api/batches', headers={'X-User-Role': 'admin'}, json={
        'name': '规划已完成预算阻塞', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'smart_planning': True, 'auto_workflow': True,
        'auto_start_panorama': False,
        'series_counts': {'modern_business': {'primary': 1, 'standby': 0}},
    })
    assert response.status_code == 200, response.text
    bid = response.json()['id']
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active' WHERE id=?", (bid,))
        conn.execute("UPDATE designs SET planning_status='ready' WHERE batch_id=?", (bid,))
    monkeypatch.setattr(smart_planning, 'generate', lambda *a, **k: pytest.fail('existing plan must be reused'))
    def blocked(*args, **kwargs):
        raise BudgetHardLimitExceededError('budget')
    monkeypatch.setattr(batches, '_queue_initial_panorama', blocked)
    assert process_next_plan()
    with get_db() as conn:
        row = conn.execute("SELECT p.status,p.retry_after,d.planning_status FROM planning_jobs p JOIN designs d ON d.id=p.design_id WHERE p.batch_id=?", (bid,)).fetchone()
    assert row['status'] == 'queued'
    assert row['retry_after']
    assert row['planning_status'] == 'ready'


def test_manual_retry_resets_only_technical_backoff(client):
    response = client.post('/api/batches', headers={'X-User-Role': 'admin'}, json={
        'name': '手动重试规划', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'smart_planning': True, 'auto_workflow': True,
        'auto_start_panorama': False,
        'series_counts': {'modern_business': {'primary': 1, 'standby': 0}},
    })
    bid = response.json()['id']
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active' WHERE id=?", (bid,))
        conn.execute("""UPDATE planning_jobs SET status='failed',technical_retry_count=2,
            retry_after=datetime('now','localtime','+60 seconds'),replan_count=2 WHERE batch_id=?""", (bid,))
    retried = client.post(f'/api/workbench/batches/{bid}/retry-planning', headers={'X-User-Role':'leader'})
    assert retried.status_code == 200, retried.text
    with get_db() as conn:
        row = conn.execute("SELECT status,technical_retry_count,retry_after,replan_count FROM planning_jobs WHERE batch_id=?", (bid,)).fetchone()
    assert (row['status'], row['technical_retry_count'], row['retry_after'], row['replan_count']) == ('queued', 0, None, 2)


def test_manual_planning_retry_is_allowed_while_images_wait_for_unknown(client):
    response = client.post('/api/batches', headers={'X-User-Role': 'admin'}, json={
        'name': '未知结果下重试规划', 'enabled_series': ['modern_business'],
        'target_archived_count': 1, 'smart_planning': True, 'auto_workflow': True,
        'auto_start_panorama': False,
        'series_counts': {'modern_business': {'primary': 1, 'standby': 0}},
    })
    assert response.status_code == 200, response.text
    bid = response.json()['id']
    with get_db(immediate=True) as conn:
        conn.execute(
            """UPDATE batches SET status='paused',pause_reason='PAUSE_UNKNOWN_PENDING'
               WHERE id=?""",
            (bid,),
        )
        conn.execute(
            """UPDATE planning_jobs SET status='failed',technical_retry_count=2,
               retry_after=datetime('now','localtime','+60 seconds')
               WHERE batch_id=?""",
            (bid,),
        )

    retried = client.post(
        f'/api/workbench/batches/{bid}/retry-planning',
        headers={'X-User-Role': 'admin'},
    )

    assert retried.status_code == 200, retried.text
    with get_db() as conn:
        row = conn.execute(
            'SELECT status,technical_retry_count,retry_after FROM planning_jobs WHERE batch_id=?',
            (bid,),
        ).fetchone()
        batch = conn.execute(
            'SELECT status,pause_reason FROM batches WHERE id=?',
            (bid,),
        ).fetchone()
    assert (row['status'], row['technical_retry_count'], row['retry_after']) == ('queued', 0, None)
    assert (batch['status'], batch['pause_reason']) == ('paused', 'PAUSE_UNKNOWN_PENDING')


def test_planner_prepares_plans_but_defers_panoramas_during_unknown_pause(client, monkeypatch):
    response = client.post('/api/batches', headers={'X-User-Role': 'admin'}, json={
        'name': '暂停期间继续规划', 'enabled_series': ['modern_business'],
        'target_archived_count': 2, 'smart_planning': True, 'auto_workflow': True,
        'auto_start_panorama': False,
        'series_counts': {'modern_business': {'primary': 2, 'standby': 0}},
    })
    assert response.status_code == 200, response.text
    bid = response.json()['id']
    with get_db(immediate=True) as conn:
        jobs = conn.execute(
            'SELECT design_id FROM planning_jobs WHERE batch_id=?',
            (bid,),
        ).fetchall()
        conn.execute(
            """UPDATE batches SET status='paused',pause_reason='PAUSE_UNKNOWN_PENDING'
               WHERE id=?""",
            (bid,),
        )

    calls = []

    def prepare(design_id, *args, **kwargs):
        calls.append(design_id)
        with get_db(immediate=True) as conn:
            conn.execute(
                "UPDATE designs SET planning_status='ready' WHERE id=?",
                (design_id,),
            )

    monkeypatch.setattr(smart_planning, 'generate', prepare)
    assert process_next_plan()
    assert process_next_plan()

    with get_db() as conn:
        job_rows = conn.execute(
            'SELECT status,error,retry_after FROM planning_jobs WHERE batch_id=?',
            (bid,),
        ).fetchall()
        designs = conn.execute(
            'SELECT planning_status FROM designs WHERE id IN (?,?)',
            (jobs[0]['design_id'], jobs[1]['design_id']),
        ).fetchall()
        task_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE batch_id=? AND task_type='generate_panorama'",
            (bid,),
        ).fetchone()['n']
    assert set(calls) == {job['design_id'] for job in jobs}
    assert all(design['planning_status'] == 'ready' for design in designs)
    assert all(
        (job['status'], job['error'], job['retry_after']) == (
            'ready_to_dispatch',
            '方案已就绪，等待图片任务恢复后自动派发全景',
            None,
        )
        for job in job_rows
    )
    assert task_count == 0

    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE batches SET status='active',pause_reason=NULL WHERE id=?",
            (bid,),
        )
    assert process_next_plan()
    with get_db() as conn:
        task_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE batch_id=? AND task_type='generate_panorama'",
            (bid,),
        ).fetchone()['n']
        done_count = conn.execute(
            "SELECT COUNT(*) AS n FROM planning_jobs WHERE batch_id=? AND status='done'",
            (bid,),
        ).fetchone()['n']
    assert task_count == 1
    assert done_count == 1


def test_planner_stop_waits_for_inflight_work(client, monkeypatch):
    import asyncio
    from threading import Event
    from backend.app import auto_planning_service
    started, release = Event(), Event()
    monkeypatch.delenv('WORKBENCH_DISABLE_WORKER_LOOP', raising=False)
    monkeypatch.setenv('WORKBENCH_PLANNING_CONCURRENCY', '2')
    def work():
        started.set()
        assert release.wait(5)
        return False
    monkeypatch.setattr(auto_planning_service, 'process_next_plan', work)
    async def exercise():
        worker = auto_planning_service.PlannerWorker()
        await worker.start()
        assert await asyncio.to_thread(started.wait, 3)
        stopping = asyncio.create_task(worker.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, 5)
        assert worker.tasks == []
    asyncio.run(exercise())


def test_two_planners_claim_distinct_jobs(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    response = client.post('/api/batches', headers={'X-User-Role': 'admin'}, json={
        'name': '双规划并发', 'enabled_series': ['modern_business'],
        'target_archived_count': 2, 'smart_planning': True, 'auto_workflow': True,
        'auto_start_panorama': False,
        'series_counts': {'modern_business': {'primary': 2, 'standby': 0}},
    })
    bid = response.json()['id']
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active' WHERE id=?", (bid,))
    gate, lock, seen = Barrier(2), Lock(), []
    def fail(design_id, *args, **kwargs):
        with lock:
            seen.append(design_id)
        gate.wait(timeout=5)
        raise HTTPException(400, 'controlled failure')
    monkeypatch.setattr(smart_planning, 'generate', fail)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(process_next_plan) for _ in range(2)]
        assert all(f.result(timeout=10) for f in futures)
    assert len(seen) == 2 and len(set(seen)) == 2
