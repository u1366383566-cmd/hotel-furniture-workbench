import io
import time
import uuid
import pytest
from PIL import Image
from backend.app.database import get_db
from backend.app.config import RAW_IMAGES_DIR, PREVIEW_IMAGES_DIR
from backend.app.task_worker import worker_instance
from quad_snapshot import quad_version_snapshot
from backend.app.budget_service import (
    reserve_budget, settle_budget_success, release_budget_failed,
    get_batch_budget_summary
)
from backend.app.cleanup_service import run_protected_cleanup

def test_budget_settle_idempotency(client):
    """缺陷 7 回归：重复调用 settle_budget_success 绝不能把已用归零"""
    batch_id = f"bgt_idem_{uuid.uuid4().hex[:8]}"
    with get_db(immediate=True) as conn:
        conn.execute("""
            INSERT INTO batches (id, name, enabled_series, target_archived_count, warning_budget, hard_limit_budget)
            VALUES (?, 'Idempotency Test Batch', '["modern_business"]', 3, 20, 25)
        """, (batch_id,))
        lid = reserve_budget(conn, batch_id=batch_id, amount=1, note="Test Idempotency")

    # 第一次正常结算
    with get_db(immediate=True) as conn:
        settle_budget_success(conn, batch_id, lid)
        summary1 = get_batch_budget_summary(conn, batch_id)
        assert summary1.used_count == 1
        assert summary1.reserved_count == 0

    # 第二次重复结算（模拟重试或重复消息）
    with get_db(immediate=True) as conn:
        settle_budget_success(conn, batch_id, lid)
        summary2 = get_batch_budget_summary(conn, batch_id)
        # 核心断言：已用依然为 1，绝不能归零！
        assert summary2.used_count == 1
        assert summary2.reserved_count == 0

    # 尝试将已结算的账本错误调用 release，必须被幂等保护阻止
    with get_db(immediate=True) as conn:
        release_budget_failed(conn, batch_id, lid, reason="Late failure callback")
        summary3 = get_batch_budget_summary(conn, batch_id)
        assert summary3.used_count == 1  # 依然保持消耗

def test_cleanup_days_threshold_and_protection(client):
    """缺陷 1 回归：年轻旧版本保留，超过 30 天清理，锁定与依赖绝对保护"""
    batch_id = f"clean_test_{uuid.uuid4().hex[:8]}"
    with get_db(immediate=True) as conn:
        conn.execute("""
            INSERT INTO batches (id, name, enabled_series, target_archived_count)
            VALUES (?, 'Clean Days Test', '["natural_wood"]', 2)
        """, (batch_id,))

        did = f"des_{uuid.uuid4().hex[:8]}"
        conn.execute("""
            INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec, status)
            VALUES (?, ?, 'natural_wood', 'NW-CL-01', '{}', 'panorama_review')
        """, (did, batch_id))

        sid = f"slt_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO slots (id, design_id, slot_type, status) VALUES (?, ?, 'panorama', 'ready')", (sid, did))

        # 1. 创建年轻孤立版本（创建时间在今天，但不是当前有效版本）
        v_young = f"ver_young_{uuid.uuid4().hex[:8]}"
        raw_young = f"young_{v_young}_raw.png"
        (RAW_IMAGES_DIR / raw_young).write_bytes(b"dummy_young")
        conn.execute("""
            INSERT INTO slot_versions (id, slot_id, design_id, version_num, prompt, raw_image_path, process_status, created_at)
            VALUES (?, ?, ?, 1, 'young prompt', ?, 'completed', datetime('now', 'localtime'))
        """, (v_young, sid, did, raw_young))

        # 2. 创建超过 35 天的孤立版本（满足清理天数）
        v_old = f"ver_old_{uuid.uuid4().hex[:8]}"
        raw_old = f"old_{v_old}_raw.png"
        (RAW_IMAGES_DIR / raw_old).write_bytes(b"dummy_old")
        conn.execute("""
            INSERT INTO slot_versions (id, slot_id, design_id, version_num, prompt, raw_image_path, process_status, created_at)
            VALUES (?, ?, ?, 2, 'old prompt', ?, 'completed', datetime('now', 'localtime', '-35 days'))
        """, (v_old, sid, did, raw_old))

        # 3. 创建超过 35 天但被锁定的当前生效版本（必须受绝对保护）
        v_locked_old = f"ver_lock_{uuid.uuid4().hex[:8]}"
        raw_locked = f"locked_{v_locked_old}_raw.png"
        (RAW_IMAGES_DIR / raw_locked).write_bytes(b"dummy_locked")
        conn.execute("""
            INSERT INTO slot_versions (id, slot_id, design_id, version_num, prompt, raw_image_path, process_status, is_locked, created_at)
            VALUES (?, ?, ?, 3, 'locked prompt', ?, 'completed', 1, datetime('now', 'localtime', '-40 days'))
        """, (v_locked_old, sid, did, raw_locked))
        conn.execute("UPDATE slots SET current_version_id = ? WHERE id = ?", (v_locked_old, sid))

    # 执行 30 天清理
    res = client.post("/api/system/cleanup", json={"days_threshold": 30}, headers={"X-User-Role": "admin"})
    assert res.status_code == 200

    # 验证文件状态
    assert (RAW_IMAGES_DIR / raw_young).exists() is True   # 年轻版本强保留！
    assert (RAW_IMAGES_DIR / raw_locked).exists() is True  # 锁定版本强保护！
    assert (RAW_IMAGES_DIR / raw_old).exists() is False    # 超过 30 天且孤立的版本被清理！

def test_state_constraints_and_no_stale_overwrites(client):
    """缺陷 2 回归：已归档/淘汰设计不可改写，普通回选全景被阻断，Worker 回写防复活"""
    # 1. 创建批次并完成全流程归档
    b_res = client.post("/api/batches", json={
        "name": "状态约束回归批次",
        "enabled_series": ["soft_contemporary"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]

    worker_instance.process_next_task()
    d = client.get(f"/api/designs/batch/{batch_id}").json()[0]
    p_slot_id = d["slots"]["panorama"]["id"]
    p_vid = d["slots"]["panorama"]["current_version"]["id"]

    # 通过全景并启动下游
    client.post(f"/api/designs/{d['id']}/review_panorama", json={"action": "approve", "expected_version_id": p_vid}, headers={"X-User-Role": "operator"})
    client.post(f"/api/designs/{d['id']}/start_downstream", headers={"X-User-Role": "operator"})
    for _ in range(3):
        worker_instance.process_next_task()

    # 终审归档
    d_ready = client.get(f"/api/designs/{d['id']}").json()
    client.post(f"/api/designs/{d['id']}/review_quad", json={
        "action": "archive",
        "expected_slot_versions": quad_version_snapshot(d_ready),
    }, headers={"X-User-Role": "operator"})
    d_archived = client.get(f"/api/designs/{d['id']}").json()
    assert d_archived["status"] == "archived"

    # 2. 尝试对已归档设计发起重做 -> 必须被 400 阻断
    redo_archived = client.post(f"/api/slots/{p_slot_id}/redo", json={
        "slot_id": p_slot_id,
        "expected_version_id": p_vid,
        "reason_category": "比例异常"
    }, headers={"X-User-Role": "leader"})
    assert redo_archived.status_code == 400
    assert "终态" in redo_archived.json()["detail"] or "已封存" in redo_archived.json()["detail"]

    # 3. 尝试通过普通 select_version 偷换全景槽位 -> 必须被 400 阻断并提示安全红线
    select_pano = client.post(f"/api/slots/{p_slot_id}/select_version", json={
        "slot_id": p_slot_id,
        "version_id": p_vid
    }, headers={"X-User-Role": "operator"})
    assert select_pano.status_code == 400
    assert "全景版本切换" in select_pano.json()["detail"] or "已处于" in select_pano.json()["detail"]

    # 4. 验证 Worker 回写防淘汰复活：
    # 创建一个设计，派发全景任务，但是在 Worker 执行完前人工淘汰了该设计
    with get_db(immediate=True) as conn:
        did_elim = f"des_elim_{uuid.uuid4().hex[:8]}"
        sid_elim = f"slt_elim_{uuid.uuid4().hex[:8]}"
        vid_elim = f"ver_elim_{uuid.uuid4().hex[:8]}"
        tid_elim = f"tsk_elim_{uuid.uuid4().hex[:8]}"
        conn.execute("""
            INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec, status)
            VALUES (?, ?, 'modern_business', 'MB-ELIM', '{}', 'eliminated')
        """, (did_elim, batch_id))
        conn.execute("INSERT INTO slots (id, design_id, slot_type, status) VALUES (?, ?, 'panorama', 'invalidated')", (sid_elim, did_elim))
        conn.execute("INSERT INTO slot_versions (id, slot_id, design_id, prompt) VALUES (?, ?, ?, 'elim test')", (vid_elim, sid_elim, did_elim))
        conn.execute("INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'queued')", (tid_elim, batch_id, did_elim, sid_elim, vid_elim))

    # Worker 处理该任务
    worker_instance.process_next_task()

    # 核心断言：设计绝不能复活成 panorama_review，槽位不能被置为 ready！
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM designs WHERE id = ?", (did_elim,))
        assert cur.fetchone()["status"] == "eliminated"
        cur.execute("SELECT status FROM slots WHERE id = ?", (sid_elim,))
        assert cur.fetchone()["status"] == "invalidated"

def test_resolve_unknown_validations(client):
    """缺陷 3 回归：非 unknown 拒绝，confirm_success 无图报错，confirm_failure 无凭据拒绝"""
    batch_id = f"b_unk_{uuid.uuid4().hex[:8]}"
    with get_db(immediate=True) as conn:
        conn.execute("INSERT INTO batches (id, name, enabled_series, target_archived_count) VALUES (?, 'Unk Batch', '[\"modern_business\"]', 1)", (batch_id,))
        did = f"d_u_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec) VALUES (?, ?, 'modern_business', 'U-01', '{}')", (did, batch_id))
        sid = f"s_u_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO slots (id, design_id, slot_type) VALUES (?, ?, 'panorama')", (sid, did))
        vid = f"v_u_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO slot_versions (id, slot_id, design_id, prompt, process_status, is_unknown) VALUES (?, ?, ?, 'prompt', 'unknown_pending_confirmation', 1)", (vid, sid, did))
        tid = f"t_u_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'unknown')", (tid, batch_id, did, sid, vid))

    # 1. 尝试无物理文件 confirm_success -> 必须 400
    res_bad_success = client.post("/api/system/resolve_unknown", json={
        "task_id": tid,
        "resolution": "confirm_success"
    }, headers={"X-User-Role": "leader"})
    assert res_bad_success.status_code == 400
    assert "尚未落盘" in res_bad_success.json()["detail"] or "未找到完整图像" in res_bad_success.json()["detail"]

    # 2. 尝试无明确证据 confirm_failure -> 必须 400
    res_bad_failure = client.post("/api/system/resolve_unknown", json={
        "task_id": tid,
        "resolution": "confirm_failure",
        "evidence": "unknown无法确认"
    }, headers={"X-User-Role": "leader"})
    assert res_bad_failure.status_code == 400
    assert "核账凭据编号" in res_bad_failure.json()["detail"]

    # 3. 提供有效证据 confirm_failure -> 200 成功
    res_good_failure = client.post("/api/system/resolve_unknown", json={
        "task_id": tid,
        "resolution": "confirm_failure",
        "evidence": "查询上游号池日志，确认为上游网关连接建立前超时，上游账单核对无此扣费流水",
        "billing_reference": "BILL-20261030-0001",
        "attests_no_charge": True
    }, headers={"X-User-Role": "leader"})
    assert res_good_failure.status_code == 200

    # 4. 重复处置同一个已经 failed 的任务 -> 必须 409 Conflict
    res_duplicate = client.post("/api/system/resolve_unknown", json={
        "task_id": tid,
        "resolution": "confirm_failure",
        "evidence": "重复提交"
    }, headers={"X-User-Role": "leader"})
    assert res_duplicate.status_code == 409

def test_fault_injections_and_circuit_breaker(client):
    """缺陷 4 与 5 回归：超时暂停批次、401 批次熔断、429 技术重试分离"""
    b_res = client.post("/api/batches", json={
        "name": "故障模拟与技术重试批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 2,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": False
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active', pause_reason=NULL WHERE id=?", (batch_id,))

    # 1. 注入超时故障：验证批次自动暂停
    with get_db(immediate=True) as conn:
        did1 = f"d_t_{uuid.uuid4().hex[:8]}"
        sid1 = f"s_t_{uuid.uuid4().hex[:8]}"
        vid1 = f"v_t_{uuid.uuid4().hex[:8]}"
        tid1 = f"t_t_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec) VALUES (?, ?, 'modern_business', 'TO-01', '{}')", (did1, batch_id))
        conn.execute("INSERT INTO slots (id, design_id, slot_type) VALUES (?, ?, 'panorama')", (sid1, did1))
        conn.execute("INSERT INTO slot_versions (id, slot_id, design_id, prompt) VALUES (?, ?, ?, '__INJECT_TIMEOUT__')", (vid1, sid1, did1))
        conn.execute("UPDATE slots SET current_version_id=? WHERE id=?", (vid1, sid1))
        conn.execute("INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'queued')", (tid1, batch_id, did1, sid1, vid1))

    worker_instance.process_next_task()

    # 验证批次因未知任务而被自动置为 paused
    b_info = client.get(f"/api/batches/{batch_id}").json()
    assert b_info["status"] == "paused"

    # 处于 paused 状态的批次，Worker 不应继续领取新任务
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE tasks SET status = 'queued' WHERE id = 'dummy'")
    assert worker_instance.process_next_task() is False

    # 2. 注入 401 故障：验证批次直接进入熔断 (is_circuit_broken = 1)
    b_res2 = client.post("/api/batches", json={
        "name": "401 熔断批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": False
    }, headers={"X-User-Role": "leader"}).json()
    batch_id2 = b_res2["id"]
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active', pause_reason=NULL WHERE id=?", (batch_id2,))

    with get_db(immediate=True) as conn:
        did2 = f"d_401_{uuid.uuid4().hex[:8]}"
        sid2 = f"s_401_{uuid.uuid4().hex[:8]}"
        vid2 = f"v_401_{uuid.uuid4().hex[:8]}"
        tid2 = f"t_401_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec) VALUES (?, ?, 'modern_business', 'AUTH-01', '{}')", (did2, batch_id2))
        conn.execute("INSERT INTO slots (id, design_id, slot_type) VALUES (?, ?, 'panorama')", (sid2, did2))
        conn.execute("INSERT INTO slot_versions (id, slot_id, design_id, prompt) VALUES (?, ?, ?, '__INJECT_401__')", (vid2, sid2, did2))
        conn.execute("UPDATE slots SET current_version_id=? WHERE id=?", (vid2, sid2))
        conn.execute("INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'queued')", (tid2, batch_id2, did2, sid2, vid2))

    worker_instance.process_next_task()
    b_info2 = client.get(f"/api/batches/{batch_id2}").json()
    assert b_info2["is_circuit_broken"] is True

    # 3. 注入 429 故障：验证技术重试单独记账，不消耗人工重做次数
    b_res3 = client.post("/api/batches", json={
        "name": "429 技术重试批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": False
    }, headers={"X-User-Role": "leader"}).json()
    batch_id3 = b_res3["id"]
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active', pause_reason=NULL WHERE id=?", (batch_id3,))

    with get_db(immediate=True) as conn:
        did3 = f"d_429_{uuid.uuid4().hex[:8]}"
        sid3 = f"s_429_{uuid.uuid4().hex[:8]}"
        vid3 = f"v_429_{uuid.uuid4().hex[:8]}"
        tid3 = f"t_429_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec) VALUES (?, ?, 'modern_business', 'RT-01', '{}')", (did3, batch_id3))
        conn.execute("INSERT INTO slots (id, design_id, slot_type) VALUES (?, ?, 'panorama')", (sid3, did3))
        conn.execute("INSERT INTO slot_versions (id, slot_id, design_id, prompt) VALUES (?, ?, ?, '__INJECT_429__')", (vid3, sid3, did3))
        conn.execute("UPDATE slots SET current_version_id=? WHERE id=?", (vid3, sid3))
        conn.execute("INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'queued')", (tid3, batch_id3, did3, sid3, vid3))

    worker_instance.process_next_task() # 第 1 次 429 技术重试
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("SELECT redo_count, technical_retry_count FROM slots WHERE id = ?", (sid3,))
        slot_counts = cur.fetchone()
        assert slot_counts["technical_retry_count"] == 1  # 技术重试递增
        assert slot_counts["redo_count"] == 0             # 绝不消耗人工审美重做次数！
        cur.execute("SELECT status, retry_count FROM tasks WHERE id = ?", (tid3,))
        task_info = cur.fetchone()
        assert task_info["status"] == "queued"           # 自动放回队列技术重试
        assert task_info["retry_count"] == 1

def test_raw_access_audit_and_path_masking(client):
    """缺陷 6 回归：原图访问必须审计，非 admin 响应不得泄露 raw_image_path"""
    b_res = client.post("/api/batches", json={
        "name": "原图审计与脱敏批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]
    worker_instance.process_next_task()

    # 1. 操作员与负责人获取设计列表，验证 raw_image_path 字段为 None (已脱敏)
    op_designs = client.get(f"/api/designs/batch/{batch_id}", headers={"X-User-Role": "operator"}).json()
    v_op = op_designs[0]["slots"]["panorama"]["current_version"]
    assert v_op["raw_image_path"] is None
    assert v_op["preview_image_path"] is not None

    ldr_designs = client.get(f"/api/designs/batch/{batch_id}", headers={"X-User-Role": "leader"}).json()
    v_ldr = ldr_designs[0]["slots"]["panorama"]["current_version"]
    assert v_ldr["raw_image_path"] is None

    # 管理员可以查看真实原图路径
    adm_designs = client.get(f"/api/designs/batch/{batch_id}", headers={"X-User-Role": "admin"}).json()
    v_adm = adm_designs[0]["slots"]["panorama"]["current_version"]
    assert v_adm["raw_image_path"] is not None

    # 2. 管理员访问原图，验证审计日志产生
    raw_res = client.get(f"/api/media/raw/{v_adm['id']}", headers={"X-User-Role": "admin", "X-User-ID": "admin_auditor"})
    assert raw_res.status_code == 200

    logs = client.get(f"/api/system/batches/{batch_id}/audit_logs", headers={"X-User-Role": "admin"}).json()
    raw_access_logs = [l for l in logs if l["action"] == "access_raw_image"]
    assert len(raw_access_logs) >= 1
    assert raw_access_logs[0]["actor_name"] == "admin_auditor"


def test_post_process_watermark_failure_keeps_budget_settled(client):
    """第二轮审查项 1：原图已生成但水印失败，生图实际已产生消耗，绝不能释放预算，必须 settle 结算并保留原图"""
    b_res = client.post("/api/batches", json={
        "name": "水印失败消耗测试批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": False
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active', pause_reason=NULL WHERE id=?", (batch_id,))

    with get_db(immediate=True) as conn:
        did = f"d_wm_{uuid.uuid4().hex[:8]}"
        sid = f"s_wm_{uuid.uuid4().hex[:8]}"
        vid = f"v_wm_{uuid.uuid4().hex[:8]}"
        tid = f"t_wm_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec) VALUES (?, ?, 'modern_business', 'WM-01', '{}')", (did, batch_id))
        conn.execute("INSERT INTO slots (id, design_id, slot_type) VALUES (?, ?, 'panorama')", (sid, did))
        conn.execute("INSERT INTO slot_versions (id, slot_id, design_id, prompt) VALUES (?, ?, ?, '__INJECT_WATERMARK_FAIL__')", (vid, sid, did))
        conn.execute("UPDATE slots SET current_version_id=? WHERE id=?", (vid, sid))
        lid = reserve_budget(conn, batch_id=batch_id, category="image_generation", amount=1, task_id=tid)
        conn.execute("INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'queued')", (tid, batch_id, did, sid, vid))

    # Worker 处理
    worker_instance.process_next_task()

    # 断言：已用预算必须确认为 1，绝不能被错误释放为 0！
    with get_db(immediate=False) as conn:
        summary = get_batch_budget_summary(conn, batch_id)
        assert summary.image_generation_count == 1
        assert summary.reserved_count == 0

        # 原图物理文件必须保留供排查
        cur = conn.cursor()
        cur.execute("SELECT raw_image_path, process_status FROM slot_versions WHERE id = ?", (vid,))
        row = cur.fetchone()
        assert row["process_status"] == "failed"
        assert row["raw_image_path"] is not None
        assert (RAW_IMAGES_DIR / row["raw_image_path"]).exists() is True

def test_connection_reset_is_unknown_not_429(client):
    """第二轮审查项 1：真正连接断开 (ConnectionResetError) 是未知中断，必须 hold 账本并暂停批次，绝非 429！"""
    b_res = client.post("/api/batches", json={
        "name": "真实连接断开批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": False
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET status='active', pause_reason=NULL WHERE id=?", (batch_id,))

    with get_db(immediate=True) as conn:
        did = f"d_cr_{uuid.uuid4().hex[:8]}"
        sid = f"s_cr_{uuid.uuid4().hex[:8]}"
        vid = f"v_cr_{uuid.uuid4().hex[:8]}"
        tid = f"t_cr_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec) VALUES (?, ?, 'modern_business', 'CR-01', '{}')", (did, batch_id))
        conn.execute("INSERT INTO slots (id, design_id, slot_type) VALUES (?, ?, 'panorama')", (sid, did))
        conn.execute("INSERT INTO slot_versions (id, slot_id, design_id, prompt) VALUES (?, ?, ?, '__INJECT_CONNECTION_RESET__')", (vid, sid, did))
        conn.execute("UPDATE slots SET current_version_id=? WHERE id=?", (vid, sid))
        lid = reserve_budget(conn, batch_id=batch_id, category="image_generation", amount=1, task_id=tid)
        conn.execute("INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'queued')", (tid, batch_id, did, sid, vid))

    worker_instance.process_next_task()

    # 断言：批次进入 paused，任务进入 unknown，预算为 unknown_held
    with get_db(immediate=False) as conn:
        summary = get_batch_budget_summary(conn, batch_id)
        assert summary.unknown_count == 1
        assert summary.reserved_count == 0

        cur = conn.cursor()
        cur.execute("SELECT status, pause_reason FROM batches WHERE id = ?", (batch_id,))
        b_info = cur.fetchone()
        assert b_info["status"] == "paused"
        assert b_info["pause_reason"] == "PAUSE_UNKNOWN_PENDING"

        cur.execute("SELECT status FROM tasks WHERE id = ?", (tid,))
        assert cur.fetchone()["status"] == "unknown"

def test_resolve_unknown_does_not_overwrite_stale_slot(client):
    """第二轮审查项 2：resolve_unknown 在确认成功时，如果槽位指针已经被替换或设计已被淘汰，绝不覆盖槽位当前指针"""
    batch_id = f"b_stale_{uuid.uuid4().hex[:8]}"
    with get_db(immediate=True) as conn:
        conn.execute("""INSERT INTO batches (id, name, enabled_series, target_archived_count) VALUES (?, 'Stale Slot Batch', '["modern_business"]', 1)""", (batch_id,))
        did = f"d_stale_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec, status) VALUES (?, ?, 'modern_business', 'ST-01', '{}', 'panorama_generating')", (did, batch_id))
        sid = f"s_stale_{uuid.uuid4().hex[:8]}"
        
        # 槽位当前指向更新的版本 v_new
        vid_new = f"v_new_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO slots (id, design_id, slot_type, current_version_id, status) VALUES (?, ?, 'panorama', ?, 'ready')", (sid, did, vid_new))

        # 旧版本 v_old 挂起在 unknown
        vid_old = f"v_old_{uuid.uuid4().hex[:8]}"
        raw_name = f"stale_{vid_old}_raw.png"
        prev_name = f"stale_{vid_old}_preview.jpg"
        # 写入真实可读的测试图片
        im = Image.new("RGB", (64, 64), (100, 100, 100))
        im.save(RAW_IMAGES_DIR / raw_name, "PNG")
        im.save(PREVIEW_IMAGES_DIR / prev_name, "JPEG")

        conn.execute("INSERT INTO slot_versions (id, slot_id, design_id, prompt, process_status, is_unknown, raw_image_path, preview_image_path) VALUES (?, ?, ?, 'old', 'unknown_pending_confirmation', 1, ?, ?)", (vid_old, sid, did, raw_name, prev_name))
        tid_old = f"t_stale_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'unknown')", (tid_old, batch_id, did, sid, vid_old))

    # 处置旧 unknown 任务为 confirm_success
    res = client.post("/api/system/resolve_unknown", json={
        "task_id": tid_old,
        "resolution": "confirm_success"
    }, headers={"X-User-Role": "leader"})
    assert res.status_code == 200

    # 核心断言：槽位当前的 current_version_id 必须依然保持为 v_new，绝不能被旧任务覆盖！
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_version_id FROM slots WHERE id = ?", (sid,))
        assert cur.fetchone()["current_version_id"] == vid_new

def test_circuit_break_recover_with_trial_runs(client):
    """第二轮审查项 3：负责人恢复熔断，开启 3 次少量试运行保护"""
    b_res = client.post("/api/batches", json={
        "name": "熔断试运行恢复批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": False
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]

    # 置为熔断
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET is_circuit_broken = 1, circuit_break_reason = 'Manual trip test', status = 'paused' WHERE id = ?", (batch_id,))

    # 恢复熔断
    recover_res = client.post(f"/api/batches/{batch_id}/circuit_break_recover", headers={"X-User-Role": "leader"})
    assert recover_res.status_code == 200
    assert recover_res.json()["trial_run_remaining"] == 3

    b_info = client.get(f"/api/batches/{batch_id}").json()
    assert b_info["is_circuit_broken"] is False
    assert b_info["status"] == "active"

def test_idempotent_downstream_and_archive_repeats(client):
    """第二轮审查项 4：重复启动下游与重复归档幂等保护"""
    b_res = client.post("/api/batches", json={
        "name": "幂等防护批次",
        "enabled_series": ["natural_wood"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]

    worker_instance.process_next_task()
    d = client.get(f"/api/designs/batch/{batch_id}").json()[0]
    p_vid = d["slots"]["panorama"]["current_version"]["id"]

    client.post(f"/api/designs/{d['id']}/review_panorama", json={"action": "approve", "expected_version_id": p_vid}, headers={"X-User-Role": "operator"})

    # 第一次启动下游
    start1 = client.post(f"/api/designs/{d['id']}/start_downstream", headers={"X-User-Role": "operator"})
    assert start1.status_code == 200

    budget_after_start1 = client.get(f"/api/batches/{batch_id}/budget").json()
    with get_db(immediate=False) as conn:
        tasks_after_start1 = {
            row["id"]: row["status"]
            for row in conn.execute("""
                SELECT id, status FROM tasks
                WHERE batch_id = ? AND task_type = 'generate_downstream'
            """, (batch_id,)).fetchall()
        }
        ledgers_after_start1 = {
            row["id"]: row["status"]
            for row in conn.execute("""
                SELECT id, status FROM budget_ledger
                WHERE batch_id = ? AND category = 'image_generation'
                  AND status IN ('reserved', 'settled', 'unknown_held')
            """, (batch_id,)).fetchall()
        }
        image_generation_total_after_start1 = sum(
            row["delta_reserved"] + row["delta_used"]
            for row in conn.execute("""
                SELECT delta_reserved, delta_used FROM budget_ledger
                WHERE batch_id = ? AND category = 'image_generation'
                  AND status IN ('reserved', 'settled', 'unknown_held')
            """, (batch_id,))
        )
    assert len(tasks_after_start1) == 3
    assert list(tasks_after_start1.values()).count("queued") == 3
    assert len(ledgers_after_start1) == 4
    assert list(ledgers_after_start1.values()).count("reserved") == 3
    assert budget_after_start1["reserved_count"] == 3
    assert budget_after_start1["image_generation_count"] == 4
    assert budget_after_start1["unknown_count"] == 0
    assert image_generation_total_after_start1 == 4

    # 第二次重复启动下游 -> 必须幂等返回，不能重复多扣 3 次预算！
    start2 = client.post(f"/api/designs/{d['id']}/start_downstream", headers={"X-User-Role": "operator"})
    assert start2.status_code == 200
    budget_after_start2 = client.get(f"/api/batches/{batch_id}/budget").json()
    assert budget_after_start2["reserved_count"] == budget_after_start1["reserved_count"]
    with get_db(immediate=False) as conn:
        tasks_after_start2 = {
            row["id"]: row["status"]
            for row in conn.execute("""
                SELECT id, status FROM tasks
                WHERE batch_id = ? AND task_type = 'generate_downstream'
            """, (batch_id,)).fetchall()
        }
        ledgers_after_start2 = {
            row["id"]: row["status"]
            for row in conn.execute("""
                SELECT id, status FROM budget_ledger
                WHERE batch_id = ? AND category = 'image_generation'
                  AND status IN ('reserved', 'settled', 'unknown_held')
            """, (batch_id,)).fetchall()
        }
    assert tasks_after_start2 == tasks_after_start1
    assert ledgers_after_start2 == ledgers_after_start1
    assert budget_after_start2["image_generation_count"] == 4
    with get_db(immediate=False) as conn:
        assert image_generation_total_after_start1 == 4

    # 跑完下游
    for _ in range(3):
        worker_instance.process_next_task()
    with get_db(immediate=False) as conn:
        final_tasks = {
            row["id"]: row["status"]
            for row in conn.execute("""
                SELECT id, status FROM tasks
                WHERE batch_id = ? AND task_type = 'generate_downstream'
            """, (batch_id,)).fetchall()
        }
    assert set(final_tasks) == set(tasks_after_start1)
    assert list(final_tasks.values()).count("completed") == 3

    # 第一次归档
    d_ready = client.get(f"/api/designs/{d['id']}").json()
    arch1 = client.post(f"/api/designs/{d['id']}/review_quad", json={
        "action": "archive",
        "expected_slot_versions": quad_version_snapshot(d_ready),
    }, headers={"X-User-Role": "operator"})
    assert arch1.status_code == 200
    b_stat1 = client.get(f"/api/batches/{batch_id}").json()
    assert b_stat1["current_archived_count"] == 1

    # 第二次重复点击归档 -> 幂等直接返回，归档计数绝不重复递增为 2！
    d_before_repeat = client.get(f"/api/designs/{d['id']}").json()
    arch2 = client.post(f"/api/designs/{d['id']}/review_quad", json={
        "action": "archive",
        "expected_slot_versions": quad_version_snapshot(d_before_repeat),
    }, headers={"X-User-Role": "operator"})
    assert arch2.status_code == 200
    b_stat2 = client.get(f"/api/batches/{batch_id}").json()
    assert b_stat2["current_archived_count"] == 1

def test_partial_export_uncompleted_target(client):
    """第二轮审查项 5：未达总目标时部分导出功能可用"""
    b_res = client.post("/api/batches", json={
        "name": "部分导出测试批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 5, # 目标 5 款
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]

    worker_instance.process_next_task()
    d = client.get(f"/api/designs/batch/{batch_id}").json()[0]
    p_vid = d["slots"]["panorama"]["current_version"]["id"]
    client.post(f"/api/designs/{d['id']}/review_panorama", json={"action": "approve", "expected_version_id": p_vid}, headers={"X-User-Role": "operator"})
    client.post(f"/api/designs/{d['id']}/start_downstream", headers={"X-User-Role": "operator"})
    for _ in range(3):
        worker_instance.process_next_task()
    d_ready = client.get(f"/api/designs/{d['id']}").json()
    client.post(f"/api/designs/{d['id']}/review_quad", json={
        "action": "archive",
        "expected_slot_versions": quad_version_snapshot(d_ready),
    }, headers={"X-User-Role": "operator"})

    # 当前归档 1 款，未达 5 款目标，执行指定设计部分导出
    export_part = client.post("/api/export/selected", json={
        "batch_id": batch_id,
        "design_ids": [d["id"]]
    }, headers={"X-User-Role": "operator"})
    assert export_part.status_code == 200
    assert "部分导出 1 款" in export_part.json()["message"]

def test_planning_and_vision_model_cost_accounting(client):
    """第二轮审查项 6：规划模型与视觉模型真实流水记账与预算分离展示"""
    b_res = client.post("/api/batches", json={
        "name": "多模型记账批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 2,
        "standby_designs_per_series": 1,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]

    # 批次创建规划了 2 款首发，应产生 2 笔 planning_model 流水
    budget1 = client.get(f"/api/batches/{batch_id}/budget").json()
    assert budget1["planning_model_count"] == 3

    # 全景生成并初筛通过
    worker_instance.process_next_task()
    d = client.get(f"/api/designs/batch/{batch_id}").json()[0]
    p_vid = d["slots"]["panorama"]["current_version"]["id"]
    client.post(f"/api/designs/{d['id']}/review_panorama", json={"action": "approve", "expected_version_id": p_vid}, headers={"X-User-Role": "operator"})

    # 全景通过提取特征后，应产生 1 笔 vision_model 流水
    budget2 = client.get(f"/api/batches/{batch_id}/budget").json()
    assert budget2["vision_model_count"] == 1

def test_series_rules_configuration_api(client):
    """第二轮审查项 7：系列规则库查看与更新 API"""
    rules = client.get("/api/system/series_rules", headers={"X-User-Role": "operator"}).json()
    assert "modern_business" in rules

    # 负责人更新规则
    up_res = client.put("/api/system/series_rules/modern_business", json={
        "twin_adapter_rule": "Updated twin adapter rule for test"
    }, headers={"X-User-Role": "leader"})
    assert up_res.status_code == 200
    assert up_res.json()["rule"]["twin_adapter_rule"] == "Updated twin adapter rule for test"

