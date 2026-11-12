import io
import zipfile
import pytest
from PIL import Image, ImageChops
from backend.app.task_worker import worker_instance
from quad_snapshot import quad_version_snapshot
from backend.app.watermark_service import apply_watermark
from backend.app.config import (
    RAW_IMAGES_DIR,
    SEEZO_WATERMARK_PATH,
    SEEZO_WATERMARK_WIDTH_RATIO,
    SEEZO_WATERMARK_TOP_RATIO,
)


def test_seezo_logo_watermark_geometry_and_legacy_text_is_ignored():
    base_img = Image.new("RGB", (400, 300), (24, 42, 61))
    rendered = apply_watermark(
        base_img,
        main_text="LEGACY BRAND TEXT",
        repeat_text="LEGACY TILE TEXT",
        opacity=0.3,
        font_size=72,
        position="bottom_right",
    )

    with Image.open(SEEZO_WATERMARK_PATH) as source:
        logo = source.convert("RGBA")
        expected_width = round(base_img.width * SEEZO_WATERMARK_WIDTH_RATIO)
        expected_height = round(expected_width * logo.height / logo.width)
        assert expected_width == 163
        assert expected_height == 50
        resized_logo = logo.resize((expected_width, expected_height), Image.Resampling.LANCZOS)

    expected = base_img.convert("RGBA")
    expected.alpha_composite(resized_logo, (0, round(base_img.height * SEEZO_WATERMARK_TOP_RATIO)))
    assert rendered.size == base_img.size
    assert rendered.mode == "RGB"
    # Exact composition proves the left edge/top offset, aspect-preserving resize,
    # alpha handling, and that legacy text/tile arguments do not affect output.
    assert rendered.tobytes() == expected.convert("RGB").tobytes()


def test_watermark_config_exposes_logo_semantics_but_accepts_legacy_fields(client):
    batch = client.post("/api/batches", json={
        "name": "SEEZO 水印配置测试",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "auto_start_panorama": False,
    }, headers={"X-User-Role": "leader"}).json()

    payload = {
        "main_text": "legacy brand",
        "repeat_text": "legacy tile",
        "opacity": 0.3,
        "font_size": 48,
        "position": "center",
    }
    response = client.post(
        f"/api/system/batches/{batch['id']}/watermark",
        json=payload,
        headers={"X-User-Role": "leader"},
    )
    assert response.status_code == 200, response.text
    cfg = response.json()["config"]
    assert cfg["renderer"] == "seezo_logo"
    assert cfg["logo_asset"] == "static/seezo-watermark.png"
    assert cfg["logo_width_ratio"] == pytest.approx(418 / 1024)
    assert cfg["logo_left_ratio"] == pytest.approx(0.0)
    assert cfg["logo_top_ratio"] == pytest.approx(83 / 1024)
    assert cfg["legacy_text_ignored"] is True
    assert cfg["main_text"] == payload["main_text"]

    fetched = client.get(
        f"/api/system/batches/{batch['id']}/watermark",
        headers={"X-User-Role": "operator"},
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["renderer"] == "seezo_logo"
    assert fetched.json()["legacy_text_ignored"] is True

def test_preview_and_export_share_logo_renderer(client):
    """A historical raw file is re-watermarked for export, matching preview geometry."""
    base = Image.new("RGB", (400, 300), (29, 48, 68))
    preview = apply_watermark(base)
    expected_diff = ImageChops.difference(preview, base).getbbox()
    assert expected_diff == (0, 24, 163, 74)

    # The export path calls the same renderer against raw bytes; compare its
    # decoded PNG result directly to the shared renderer, not to a preview JPEG.
    from backend.app.export_service import apply_watermark as export_renderer
    exported = export_renderer(base)
    assert exported.tobytes() == preview.tobytes()


def test_watermarked_zip_export_integrity(client, monkeypatch):
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

    # 打包期间其他连接仍可取得写锁。
    import sqlite3
    from backend.app.config import DB_PATH
    from backend.app.routers import export as export_router
    assemble = export_router._assemble_watermarked_zip
    def assert_unlocked(*args):
        conn = sqlite3.connect(str(DB_PATH), timeout=1, isolation_level=None)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('ROLLBACK')
        finally:
            conn.close()
        return assemble(*args)
    monkeypatch.setattr(export_router, '_assemble_watermarked_zip', assert_unlocked)
    # 归档后执行导出
    export_res = client.post(f"/api/export/design/{d['id']}", headers={"X-User-Role": "operator"})
    assert export_res.status_code == 200
    zip_info = export_res.json()
    zip_file_name = zip_info["file_name"]

    head = client.head(f"/api/export/download/{zip_file_name}", headers={"X-User-Role": "operator"})
    assert head.status_code == 200 and not head.content
    assert client.head('/api/export/download/missing.zip', headers={"X-User-Role": "operator"}).status_code == 404

    # 下载 ZIP
    dl_res = client.get(f"/api/export/download/{zip_file_name}", headers={"X-User-Role": "operator"})
    assert dl_res.status_code == 200
    assert dl_res.headers["content-type"] == "application/zip"

    # 解压校验 ZIP 内部文件完整性
    zip_bytes = io.BytesIO(dl_res.content)
    with zipfile.ZipFile(zip_bytes, mode="r") as zf:
        file_list = zf.namelist()
        archived = client.get(f"/api/designs/{d['id']}").json()
        code = archived['product_code']
        from backend.app.product_numbering import product_image_name
        number = int(code[4:])
        # 必须包含 4 张规范命名的槽位图及元数据
        for slot in ('panorama', 'white_isolated', 'twin_bed', 'nightstand_closeup'):
            assert f"{code}/{product_image_name(number, archived['archived_at'], slot)}" in file_list
        assert f"METADATA-{code}.json" in file_list

        # 验证图片字节可解析且为合法 PNG
        img_bytes = zf.read(f"{code}/{product_image_name(number, archived['archived_at'], 'panorama')}")
        with Image.open(io.BytesIO(img_bytes)) as parsed_img:
            assert parsed_img.format == "PNG"
            assert parsed_img.size == (1024, 1024)
            admin_d = client.get(f"/api/designs/{d['id']}", headers={"X-User-Role": "admin"}).json()
            raw_name = admin_d["slots"]["panorama"]["current_version"]["raw_image_path"]
            with Image.open(RAW_IMAGES_DIR / raw_name) as raw_img:
                expected = apply_watermark(raw_img)
            assert parsed_img.tobytes() == expected.tobytes()
