"""用户实测反馈修复的回归测试（2026-10-31 第二轮验收后）。

覆盖：
1. 重抽/单张重做弹窗函数 openRedoModal 必须真实存在（此前被调用但未定义，按钮点击无反应）。
2. 负责人可自定义新增产品系列：创建后可被批次规划使用；重复/非法标识被拦截；规则修订保留名称。
3. 导出水印贴片缓存不改变水印渲染结果（性能优化无行为回归）。
"""
import json
import uuid

import pytest

from backend.app.database import get_db
from backend.app.watermark_service import apply_watermark
from PIL import Image


def test_redo_modal_function_is_defined():
    from pathlib import Path
    app_js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "function openRedoModal(ctx)" in app_js, (
        "重抽/单张重做按钮依赖 openRedoModal，函数必须真实定义（此前按钮点击无反应）"
    )
    # 弹窗提交逻辑依赖该上下文字段
    for field in ("redoCount", "totalRedo", "versionId"):
        assert field in app_js, f"openRedoModal 上下文缺少 {field}"


def test_leader_can_create_custom_series_and_plan_batch(client):
    series_id = f"luxury_{uuid.uuid4().hex[:6]}"
    created = client.post("/api/system/series_rules", json={
        "series_id": series_id, "name": "轻奢木质测试",
    }, headers={"X-User-Role": "leader"})
    assert created.status_code == 200, created.text
    assert created.json()["series_id"] == series_id

    rules = client.get("/api/system/series_rules").json()
    assert series_id in rules and rules[series_id]["name"] == "轻奢木质测试"
    # 必填规划字段自动沿用内置模板，保证规划提示词结构完整
    for key in ("headboard_types", "bed_frames", "nightstands", "wardrobe_types", "materials", "color_palette"):
        assert rules[series_id][key], f"新系列缺少规划字段 {key}"

    batch = client.post("/api/batches", json={
        "name": "自定义系列批次", "enabled_series": [series_id],
        "target_archived_count": 1, "designs_per_series": 1,
        "standby_designs_per_series": 0, "auto_start_panorama": False,
    }, headers={"X-User-Role": "leader"})
    assert batch.status_code == 200, batch.text
    designs = client.get(f"/api/designs/batch/{batch.json()['id']}").json()
    assert designs and designs[0]["structured_spec"]["series_name"] == "轻奢木质测试"
    assert designs[0]["design_code"].startswith("DS-"), designs[0]["design_code"]

    # 自定义系列规则修订：名称必须保留（此前内置取值会 KeyError）
    updated = client.put(f"/api/system/series_rules/{series_id}", json={
        "hotel_mood": "轻奢度假、温暖精致",
    }, headers={"X-User-Role": "leader"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["rule"]["name"] == "轻奢木质测试"
    assert updated.json()["rule"]["hotel_mood"] == "轻奢度假、温暖精致"


def test_create_series_rejects_duplicates_invalid_ids_and_operators(client):
    forbidden = client.post("/api/system/series_rules", json={
        "series_id": "op_series", "name": "操作员系列",
    }, headers={"X-User-Role": "operator"})
    assert forbidden.status_code == 403

    created = client.post("/api/system/series_rules", json={
        "series_id": "dup_series", "name": "重复系列",
    }, headers={"X-User-Role": "leader"})
    assert created.status_code == 200

    duplicate = client.post("/api/system/series_rules", json={
        "series_id": "dup_series", "name": "再次创建",
    }, headers={"X-User-Role": "leader"})
    assert duplicate.status_code == 400

    builtin_clash = client.post("/api/system/series_rules", json={
        "series_id": "modern_business", "name": "与内置冲突",
    }, headers={"X-User-Role": "leader"})
    assert builtin_clash.status_code == 400

    bad_id = client.post("/api/system/series_rules", json={
        "series_id": "Bad-Id!", "name": "非法标识",
    }, headers={"X-User-Role": "leader"})
    assert bad_id.status_code in (400, 422), bad_id.text

    # 内置系列名称不可被修订改写
    renamed = client.put("/api/system/series_rules/modern_business", json={
        "name": "试图改名",
    }, headers={"X-User-Role": "leader"})
    assert renamed.status_code == 200
    assert renamed.json()["rule"]["name"] == "现代商务"


def test_watermark_tile_cache_does_not_change_output():
    base = Image.new("RGB", (512, 512), (220, 210, 200))
    first = apply_watermark(base.copy())
    second = apply_watermark(base.copy())
    assert list(first.getdata()) == list(second.getdata()), "贴片缓存前后水印渲染必须一致"
    assert list(first.getdata()) != list(base.convert("RGB").getdata()), "水印必须实际生效"
