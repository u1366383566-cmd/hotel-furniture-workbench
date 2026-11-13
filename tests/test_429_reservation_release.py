"""429 技术重试耗尽后的预算释放回归测试。

背景缺陷：任务因 429 重试 3 次耗尽而失败时，最初派发的预占账本从未释放，
额度被幽灵占用（用户实测发现：失败任务导致 image_generation_count 虚高）。
修复：429 表示上游明确拒绝受理、无消耗，失败时必须 release_budget_failed。
"""
from backend.app.database import get_db
from backend.app.task_worker import worker_instance


def _create_batch(client, **overrides):
    payload = {
        "name": "429 释放测试批次", "enabled_series": ["modern_business"],
        "target_archived_count": 1, "designs_per_series": 1,
        "standby_designs_per_series": 0, "auto_start_panorama": True,
    }
    payload.update(overrides)
    resp = client.post("/api/batches", json=payload, headers={"X-User-Role": "leader"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_429_exhausted_failure_releases_reservation(client):
    batch = _create_batch(client)
    batch_id = batch["id"]

    # 注入 429：worker 应重试 3 次后失败，并释放最初的预占
    with get_db(immediate=True) as conn:
        task = conn.execute(
            "SELECT id, slot_version_id FROM tasks WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        vid = task["slot_version_id"]
        conn.execute(
            "UPDATE slot_versions SET prompt = prompt || ' __INJECT_429__' WHERE id = ?", (vid,)
        )

    for _ in range(4):  # 初次 + 3 次技术重试
        worker_instance.process_next_task()
        with get_db(immediate=True) as conn:
            conn.execute("UPDATE tasks SET retry_after=NULL WHERE id=?", (task["id"],))

    with get_db(immediate=False) as conn:
        task_status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task["id"],)
        ).fetchone()["status"]
        reservation = conn.execute(
            """SELECT status FROM budget_ledger
               WHERE batch_id = ? AND category = 'image_generation'
                 AND ledger_event_type = 'generation_request'""",
            (batch_id,),
        ).fetchall()

    assert task_status == "failed"
    # 唯一的派发预占必须已释放，不允许幽灵占用
    assert len(reservation) == 1 and reservation[0]["status"] == "released", reservation

    # 额度水位归零：失败的生图不占用预算
    summary = client.get(f"/api/batches/{batch_id}/budget").json()
    assert summary["image_generation_count"] == 0, summary
    assert summary["technical_retry_count"] == 4, summary
