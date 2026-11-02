import time
import pytest
from backend.app.task_worker import worker_instance

def test_batch_planning_and_panorama_workflow(client):
    # 1. 负责人创建批次：3 系列 x 2 设计 = 6 款首发，加候补
    create_resp = client.post("/api/batches", json={
        "name": "自动化工作流测试批次",
        "enabled_series": ["modern_business", "natural_wood", "soft_contemporary"],
        "target_archived_count": 3,
        "designs_per_series": 2,
        "standby_designs_per_series": 1,
        "warning_budget": 20,
        "hard_limit_budget": 25,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader", "X-User-ID": "test_leader"})
    assert create_resp.status_code == 200
    batch_data = create_resp.json()
    batch_id = batch_data["id"]

    # 2. 查询该批次设计列表，应有 6 个首发 + 3 个候补 = 9 个设计
    designs_resp = client.get(f"/api/designs/batch/{batch_id}")
    assert designs_resp.status_code == 200
    designs = designs_resp.json()
    assert len(designs) == 9

    first_runs = [d for d in designs if not d["is_standby"]]
    standbys = [d for d in designs if d["is_standby"]]
    assert len(first_runs) == 6
    assert len(standbys) == 3

    # 3. 后台 Worker 消费任务
    # 轮询等待首发设计的全景任务生成就绪
    for _ in range(30):
        worker_instance.process_next_task()
    
    # 刷新设计列表
    d1 = client.get(f"/api/designs/{first_runs[0]['id']}").json()
    pano_slot = d1["slots"]["panorama"]
    assert pano_slot["status"] == "ready"
    cur_v = pano_slot["current_version"]
    assert cur_v is not None
    assert cur_v["process_status"] == "completed"
    assert cur_v["preview_image_path"] is not None

    # 4. 全景初筛 - 通过并锁定
    approve_resp = client.post(f"/api/designs/{d1['id']}/review_panorama", json={
        "action": "approve",
        "expected_version_id": cur_v["id"]
    }, headers={"X-User-Role": "operator"})
    assert approve_resp.status_code == 200
    d1_approved = approve_resp.json()
    assert d1_approved["status"] == "panorama_approved"
    assert d1_approved["slots"]["panorama"]["current_version"]["is_locked"] is True
    assert d1_approved["visual_features"] is not None

def test_panorama_redo_limits_and_standby_wake(client):
    # 创建测试批次
    create_resp = client.post("/api/batches", json={
        "name": "重做与候补测试批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 2,
        "designs_per_series": 1,
        "standby_designs_per_series": 2,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"})
    batch_id = create_resp.json()["id"]

    # 消费任务使全景 ready
    worker_instance.process_next_task()

    designs = client.get(f"/api/designs/batch/{batch_id}").json()
    target_d = [d for d in designs if not d["is_standby"]][0]
    cur_v = target_d["slots"]["panorama"]["current_version"]

    # 第一次重做
    r1 = client.post(f"/api/designs/{target_d['id']}/review_panorama", json={
        "action": "redo",
        "expected_version_id": cur_v["id"],
        "reason_category": "家具变形",
        "detail_comment": "床屏线条变形"
    }, headers={"X-User-Role": "operator"})
    assert r1.status_code == 200
    worker_instance.process_next_task() # 跑完重做任务

    # 第二次重做
    d_after_r1 = client.get(f"/api/designs/{target_d['id']}").json()
    cur_v2 = d_after_r1["slots"]["panorama"]["current_version"]
    r2 = client.post(f"/api/designs/{target_d['id']}/review_panorama", json={
        "action": "redo",
        "expected_version_id": cur_v2["id"],
        "reason_category": "比例异常"
    }, headers={"X-User-Role": "operator"})
    assert r2.status_code == 200
    worker_instance.process_next_task()

    # 第三次重做（单槽位已达 2 次上限，普通操作员无特批应被拒绝 400）
    d_after_r2 = client.get(f"/api/designs/{target_d['id']}").json()
    cur_v3 = d_after_r2["slots"]["panorama"]["current_version"]
    r3_fail = client.post(f"/api/designs/{target_d['id']}/review_panorama", json={
        "action": "redo",
        "expected_version_id": cur_v3["id"],
        "reason_category": "风格不符",
        "leader_override": False
    }, headers={"X-User-Role": "operator"})
    assert r3_fail.status_code == 400
    assert "已超出重做限制" in r3_fail.json()["detail"]

    # 负责人明确特批继续重做
    r3_override = client.post(f"/api/designs/{target_d['id']}/review_panorama", json={
        "action": "redo",
        "expected_version_id": cur_v3["id"],
        "reason_category": "风格不符",
        "leader_override": True
    }, headers={"X-User-Role": "leader"})
    assert r3_override.status_code == 200
    worker_instance.process_next_task()

    # 淘汰该设计，验证候补自动唤醒
    d_after_r3 = client.get(f"/api/designs/{target_d['id']}").json()
    cur_v4 = d_after_r3["slots"]["panorama"]["current_version"]
    elim_resp = client.post(f"/api/designs/{target_d['id']}/review_panorama", json={
        "action": "eliminate",
        "expected_version_id": cur_v4["id"],
        "reason_category": "无法修复"
    }, headers={"X-User-Role": "operator"})
    assert elim_resp.status_code == 200

    # 检查候补设计是否被唤醒并转为首发正式组 (is_standby 变为 False)
    designs_after_elim = client.get(f"/api/designs/batch/{batch_id}").json()
    active_groups = [d for d in designs_after_elim if not d["is_standby"] and d["status"] != "eliminated"]
    assert len(active_groups) == 1 # 候补已被自动唤醒填补缺口
    assert active_groups[0]["design_code"].startswith("MB-STBY-")
