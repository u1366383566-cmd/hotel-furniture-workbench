import pytest
from backend.app.task_worker import worker_instance

def test_rbac_raw_image_isolation(client):
    # 创建一个批次并生成图片
    b_res = client.post("/api/batches", json={
        "name": "RBAC 权限隔离测试",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]
    worker_instance.process_next_task()

    designs = client.get(f"/api/designs/batch/{batch_id}").json()
    vid = designs[0]["slots"]["panorama"]["current_version"]["id"]

    # 1. 操作员查看带水印预览图 -> 200 OK
    prev_res_op = client.get(f"/api/media/preview/{vid}", headers={"X-User-Role": "operator"})
    assert prev_res_op.status_code == 200
    assert prev_res_op.headers["content-type"] == "image/jpeg"

    # 2. 负责人查看带水印预览图 -> 200 OK
    prev_res_ldr = client.get(f"/api/media/preview/{vid}", headers={"X-User-Role": "leader"})
    assert prev_res_ldr.status_code == 200

    # 3. 操作员直接请求无水印原图 -> 严格 403 Forbidden！
    raw_res_op = client.get(f"/api/media/raw/{vid}", headers={"X-User-Role": "operator"})
    assert raw_res_op.status_code == 403
    assert "仅系统管理员" in raw_res_op.json()["detail"]

    # 4. 负责人请求无水印原图 -> 同样 403 Forbidden！
    raw_res_ldr = client.get(f"/api/media/raw/{vid}", headers={"X-User-Role": "leader"})
    assert raw_res_ldr.status_code == 403

    # 5. 管理员请求无水印原图 -> 200 OK
    raw_res_adm = client.get(f"/api/media/raw/{vid}", headers={"X-User-Role": "admin"})
    assert raw_res_adm.status_code == 200
    assert raw_res_adm.headers["content-type"] == "image/png"

def test_path_traversal_protection(client):
    # 尝试跨目录遍历攻击下载系统敏感文件
    attack_res = client.get("/api/export/download/../../../../Windows/win.ini", headers={"X-User-Role": "admin"})
    # safe_file_path 会剥离目录名或阻断返回 400/404
    assert attack_res.status_code in (400, 404)

def test_invalid_role_rejection(client):
    # 伪造未知角色
    res = client.get("/api/batches", headers={"X-User-Role": "super_hacker"})
    # 某些公开接口可能不要求角色，但受保护接口必须 403
    post_res = client.post("/api/batches", json={"name": "Fake"}, headers={"X-User-Role": "super_hacker"})
    assert post_res.status_code == 403
