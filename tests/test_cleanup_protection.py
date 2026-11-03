import pytest
from backend.app.task_worker import worker_instance
from backend.app.config import RAW_IMAGES_DIR

def test_cleanup_protection_policy(client):
    # 1. 普通操作员触发清理应被 403 阻断
    op_forbidden = client.post("/api/system/cleanup", json={"days_threshold": 30}, headers={"X-User-Role": "operator"})
    assert op_forbidden.status_code == 403

    # 2. 旧负责人身份兼容管理员能力，可进入受保护清理入口；系统仍记录原始角色。
    ldr_allowed = client.post("/api/system/cleanup", json={"days_threshold": 30}, headers={"X-User-Role": "leader"})
    assert ldr_allowed.status_code == 200

    # 3. 创建批次并完成一套归档设计
    b_res = client.post("/api/batches", json={
        "name": "清理保护机制测试批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]

    worker_instance.process_next_task()
    d = client.get(f"/api/designs/batch/{batch_id}", headers={"X-User-Role": "admin"}).json()[0]
    slot_id = d["slots"]["panorama"]["id"]
    cur_v1 = d["slots"]["panorama"]["current_version"]

    # 模拟一次重做，产生旧版本 v1 和新版本 v2
    client.post(f"/api/slots/{slot_id}/redo", json={
        "slot_id": slot_id,
        "expected_version_id": cur_v1["id"],
        "reason_category": "比例异常"
    }, headers={"X-User-Role": "leader"})
    worker_instance.process_next_task()

    # 获取当前 v2 (以 admin 角色读取真实 raw_image_path)
    d_after = client.get(f"/api/designs/{d['id']}", headers={"X-User-Role": "admin"}).json()
    cur_v2 = d_after["slots"]["panorama"]["current_version"]
    assert cur_v2["raw_image_path"] is not None
    
    # 通过 v2 并锁定
    client.post(f"/api/designs/{d['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": cur_v2["id"]
    }, headers={"X-User-Role": "operator"})

    v2_raw_path = RAW_IMAGES_DIR / cur_v2["raw_image_path"]
    assert v2_raw_path.exists()

    # 4. 管理员执行受保护清理
    cleanup_res = client.post("/api/system/cleanup", json={"days_threshold": 30}, headers={"X-User-Role": "admin"})
    assert cleanup_res.status_code == 200
    stats = cleanup_res.json()["stats"]
    
    # 验证核心保护约束：锁定的当前版本 v2 绝不删除
    assert v2_raw_path.exists() is True
    assert stats["protected_version_count"] > 0
