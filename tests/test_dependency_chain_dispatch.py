"""白底依赖链派发顺序保险测试。

双床/床头近景以白底清单图（white_isolated 当前版本）为以图生图参考：
白底清单没有已完成成品时，这两个槽位的任务必须原地等待（不领取、不退化用全景），
白底清单完成后才允许派发。
"""
from backend.app.database import get_db
from backend.app.task_worker import worker_instance


def _full_loop_batch(client):
    resp = client.post("/api/batches", json={
        "name": "依赖链顺序批次", "enabled_series": ["modern_business"],
        "target_archived_count": 1, "designs_per_series": 1,
        "standby_designs_per_series": 0, "auto_start_panorama": True,
    }, headers={"X-User-Role": "leader"})
    assert resp.status_code == 200, resp.text
    batch_id = resp.json()["id"]
    worker_instance.process_next_task()  # 完成全景（mock）
    designs = client.get(f"/api/designs/batch/{batch_id}").json()
    design = designs[0]
    pano_vid = design["slots"]["panorama"]["current_version"]["id"]
    approved = client.post(f"/api/designs/{design['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": pano_vid,
    })
    assert approved.status_code == 200, approved.text
    started = client.post(f"/api/designs/{design['id']}/start_downstream")
    assert started.status_code == 200, started.text
    return batch_id, design["id"]


def _task_statuses(batch_id):
    with get_db(immediate=False) as conn:
        rows = conn.execute("""
            SELECT t.id, t.status, s.slot_type
            FROM tasks t JOIN slots s ON t.slot_id = s.id
            WHERE t.batch_id = ?
            ORDER BY t.created_at
        """, (batch_id,)).fetchall()
    return {r["slot_type"]: r["status"] for r in rows}


def test_twin_and_nightstand_wait_for_white_isolated_completion(client):
    batch_id, design_id = _full_loop_batch(client)

    # 模拟白底清单任务失败（无成品落盘）
    with get_db(immediate=True) as conn:
        conn.execute("""
            UPDATE tasks SET status = 'failed',
                last_error = 'simulated white_isolated failure'
            WHERE batch_id = ? AND task_type = 'generate_downstream'
              AND slot_id = (SELECT id FROM slots WHERE design_id = ? AND slot_type = 'white_isolated')
        """, (batch_id, design_id))
        conn.execute("""
            UPDATE slot_versions SET process_status = 'failed', raw_image_path = NULL
            WHERE id = (SELECT current_version_id FROM slots
                        WHERE design_id = ? AND slot_type = 'white_isolated')
        """, (design_id,))

    # 白底清单无成品：双床/床头近景必须不被领取（worker 无任务可领）
    assert worker_instance.process_next_task() is False
    statuses = _task_statuses(batch_id)
    assert statuses["twin_bed"] == "queued", statuses
    assert statuses["nightstand_closeup"] == "queued", statuses

    # 模拟白底清单重做完成（成品落盘）→ 双床/床头近景恢复可派发
    with get_db(immediate=True) as conn:
        conn.execute("""
            UPDATE tasks SET status = 'completed'
            WHERE batch_id = ? AND task_type = 'generate_downstream'
              AND slot_id = (SELECT id FROM slots WHERE design_id = ? AND slot_type = 'white_isolated')
        """, (batch_id, design_id))
        conn.execute("""
            UPDATE slot_versions SET process_status = 'completed',
                raw_image_path = 'simulated_white_raw.png'
            WHERE id = (SELECT current_version_id FROM slots
                        WHERE design_id = ? AND slot_type = 'white_isolated')
        """, (design_id,))

    assert worker_instance.process_next_task() is True
    statuses = _task_statuses(batch_id)
    assert statuses["twin_bed"] in ("running", "completed"), statuses


def test_failed_white_redo_unblocks_queued_dependent_versions(client):
    import json
    batch_id, design_id = _full_loop_batch(client)
    design = client.get(f'/api/designs/{design_id}').json()
    white = design['slots']['white_isolated']
    old_version = white['current_version_id']
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE tasks SET status='failed' WHERE slot_version_id=?", (old_version,))
        conn.execute("UPDATE slot_versions SET process_status='failed' WHERE id=?", (old_version,))
        conn.execute("UPDATE slots SET status='empty' WHERE id=?", (white['id'],))
    response = client.post(f"/api/slots/{white['id']}/redo", json={
        'slot_id': white['id'], 'expected_version_id': old_version, 'reason_category': '生成失败', 'detail_comment': '重试白底图',
    })
    assert response.status_code == 200, response.text
    new_version = response.json()['new_version_id']
    with get_db() as conn:
        rows = conn.execute("""SELECT s.slot_type,sv.prompt_metadata,t.status FROM slots s
            JOIN slot_versions sv ON sv.id=s.current_version_id JOIN tasks t ON t.slot_version_id=sv.id
            WHERE s.design_id=? AND s.slot_type IN ('twin_bed','nightstand_closeup')""", (design_id,)).fetchall()
        assert len(rows)==2 and all(r['status']=='queued' and json.loads(r['prompt_metadata'])['reference_version_id']==new_version for r in rows)
    for _ in range(3):
        assert worker_instance.process_next_task()
    design = client.get(f'/api/designs/{design_id}').json()
    assert all(design['slots'][name]['current_version']['process_status']=='completed' for name in ('white_isolated','twin_bed','nightstand_closeup'))
