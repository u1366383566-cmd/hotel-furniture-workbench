import pytest
from backend.app.task_worker import worker_instance
from quad_snapshot import quad_version_snapshot

def test_downstream_generation_and_quad_archive(client):
    # 创建测试批次
    b_res = client.post("/api/batches", json={
        "name": "下游四图闭环测试",
        "enabled_series": ["natural_wood"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]

    # 跑全景任务
    worker_instance.process_next_task()
    designs = client.get(f"/api/designs/batch/{batch_id}").json()
    d = designs[0]
    pano_vid = d["slots"]["panorama"]["current_version"]["id"]

    # 未通过全景前启动下游应报错
    fail_start = client.post(f"/api/designs/{d['id']}/start_downstream", headers={"X-User-Role": "operator"})
    assert fail_start.status_code == 400

    # 通过全景
    client.post(f"/api/designs/{d['id']}/review_panorama", json={
        "action": "approve",
        "expected_version_id": pano_vid
    }, headers={"X-User-Role": "operator"})

    # 启动下游三图
    start_res = client.post(f"/api/designs/{d['id']}/start_downstream", headers={"X-User-Role": "operator"})
    assert start_res.status_code == 200

    # 跑完 3 个下游任务
    for _ in range(5):
        worker_instance.process_next_task()

    # 查询设计组，此时 4 槽位全部 ready，设计状态应自动转为 quad_review
    d_ready = client.get(f"/api/designs/{d['id']}").json()
    assert d_ready["status"] == "quad_review"
    for stype in ["panorama", "white_isolated", "twin_bed", "nightstand_closeup"]:
        slot = d_ready["slots"][stype]
        assert slot["current_version"] is not None
        assert slot["current_version"]["process_status"] == "completed"
        if stype != "panorama":
            assert slot["current_version"]["depends_on_panorama_version_id"] == pano_vid

    # 四图终审合格归档
    archive_res = client.post(f"/api/designs/{d['id']}/review_quad", json={
        "action": "archive",
        "detail_comment": "四图一致性极高，符合自然木质系列规范",
        "expected_slot_versions": quad_version_snapshot(d_ready),
    }, headers={"X-User-Role": "operator"})
    assert archive_res.status_code == 200
    d_arch = archive_res.json()
    assert d_arch["status"] == "archived"
    assert d_arch["archived_at"] is not None

def test_panorama_replacement_invalidates_downstream(client):
    # 创建测试批次
    b_res = client.post("/api/batches", json={
        "name": "全景替换依赖失效测试",
        "enabled_series": ["soft_contemporary"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]

    worker_instance.process_next_task() # 完成初始全景
    d = client.get(f"/api/designs/batch/{batch_id}").json()[0]
    pano_v1 = d["slots"]["panorama"]["current_version"]["id"]

    # 初审阶段重做出第二个已完成全景 v2（锁定前重做是合法路径）
    r_redo = client.post(f"/api/designs/{d['id']}/review_panorama", json={
        "action": "redo", "expected_version_id": pano_v1,
        "reason_category": "主观审美不合格"
    }, headers={"X-User-Role": "operator"})
    assert r_redo.status_code == 200, r_redo.text
    worker_instance.process_next_task()
    d = client.get(f"/api/designs/batch/{batch_id}").json()[0]
    pano_v2 = d["slots"]["panorama"]["current_version"]["id"]
    assert pano_v2 != pano_v1

    # 通过全景并启动下游
    client.post(f"/api/designs/{d['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": pano_v2
    }, headers={"X-User-Role": "operator"})
    client.post(f"/api/designs/{d['id']}/start_downstream", headers={"X-User-Role": "operator"})
    for _ in range(3):
        worker_instance.process_next_task()

    # 负责人替换回 v1（受控替换接口是锁定后唯一的换图入口）
    replace_res = client.post(f"/api/slots/designs/{d['id']}/replace_panorama", json={
        "new_panorama_version_id": pano_v1,
        "reason": "升级为更优全景版本"
    }, headers={"X-User-Role": "leader"})
    assert replace_res.status_code == 200

    # 检查下游槽位状态是否已被标记为 invalidated，且无法归档
    d_after_replace = client.get(f"/api/designs/{d['id']}").json()
    assert d_after_replace["slots"]["white_isolated"]["status"] == "invalidated"
    assert d_after_replace["slots"]["white_isolated"]["current_version"]["is_invalidated"] is True

    # 尝试将旧下游与新全景混合归档，必须被系统硬拦截 (400)
    bad_archive = client.post(f"/api/designs/{d['id']}/review_quad", json={
        "action": "archive",
        "expected_slot_versions": quad_version_snapshot(d_after_replace),
    }, headers={"X-User-Role": "operator"})
    assert bad_archive.status_code == 400
    assert "依赖失效" in bad_archive.json()["detail"] or "不匹配" in bad_archive.json()["detail"]
