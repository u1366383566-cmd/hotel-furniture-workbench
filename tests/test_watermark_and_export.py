import io
import zipfile
import pytest
from PIL import Image
from backend.app.task_worker import worker_instance
from quad_snapshot import quad_version_snapshot
from backend.app.watermark_service import apply_watermark
from backend.app.config import EXPORTS_DIR

def test_watermark_rendering():
    # 测试水印引擎合成无报错且返回 RGB 图像
    base_img = Image.new("RGB", (400, 400), (240, 240, 240))
    wm_img = apply_watermark(base_img, main_text="TEST BRAND", repeat_text="CONFIDENTIAL", opacity=0.3)
    assert wm_img.size == (400, 400)
    assert wm_img.mode == "RGB"

def test_watermarked_zip_export_integrity(client):
    # 创建一个批次并归档设计
    b_res = client.post("/api/batches", json={
        "name": "导出完整性测试批次",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": True
    }, headers={"X-User-Role": "leader"}).json()
    batch_id = b_res["id"]

    # 生成全景
    worker_instance.process_next_task()
    d = client.get(f"/api/designs/batch/{batch_id}").json()[0]
    pano_vid = d["slots"]["panorama"]["current_version"]["id"]

    # 尝试在归档前导出，必须阻断报错 400
    premature_export = client.post(f"/api/export/design/{d['id']}", headers={"X-User-Role": "operator"})
    assert premature_export.status_code == 400
    assert "尚未终审归档" in premature_export.json()["detail"]

    # 推进全景通过并生成下游
    client.post(f"/api/designs/{d['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": pano_vid
    }, headers={"X-User-Role": "operator"})
    client.post(f"/api/designs/{d['id']}/start_downstream", headers={"X-User-Role": "operator"})
    for _ in range(5):
        worker_instance.process_next_task()

    # 终审归档
    d_ready_for_archive = client.get(f"/api/designs/{d['id']}").json()
    client.post(f"/api/designs/{d['id']}/review_quad", json={
        "action": "archive",
        "expected_slot_versions": quad_version_snapshot(d_ready_for_archive),
    }, headers={"X-User-Role": "operator"})

    # 归档后执行导出
    export_res = client.post(f"/api/export/design/{d['id']}", headers={"X-User-Role": "operator"})
    assert export_res.status_code == 200
    zip_info = export_res.json()
    zip_file_name = zip_info["file_name"]

    # 下载 ZIP
    dl_res = client.get(f"/api/export/download/{zip_file_name}", headers={"X-User-Role": "operator"})
    assert dl_res.status_code == 200
    assert dl_res.headers["content-type"] == "application/zip"

    # 解压校验 ZIP 内部文件完整性
    zip_bytes = io.BytesIO(dl_res.content)
    with zipfile.ZipFile(zip_bytes, mode="r") as zf:
        file_list = zf.namelist()
        code = d["design_code"]
        # 必须包含 4 张规范命名的槽位图及元数据
        assert f"{code}/{code}_panorama_v1_watermarked.png" in file_list
        assert f"{code}/{code}_white_isolated_v1_watermarked.png" in file_list
        assert f"{code}/{code}_twin_bed_v1_watermarked.png" in file_list
        assert f"{code}/{code}_nightstand_closeup_v1_watermarked.png" in file_list
        assert f"{code}/METADATA.json" in file_list

        # 验证图片字节可解析且为合法 PNG
        img_bytes = zf.read(f"{code}/{code}_panorama_v1_watermarked.png")
        with Image.open(io.BytesIO(img_bytes)) as parsed_img:
            assert parsed_img.format == "PNG"
            assert parsed_img.size == (1024, 1024)
