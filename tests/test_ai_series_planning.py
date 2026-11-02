"""AI 风格规划（一句话 → LLM 补全系列规则）回归测试。

LLM 调用全程 mock，不消耗真实文本额度。覆盖：
1. 正常链路：LLM 返回（含反代 <escape> 包裹）→ 规则规整 → 系列创建 →
   planning_model 记账 → 审计 → 可直接用于批次规划。
2. 坏输出：非 JSON / 缺必填字段 → 502 清晰报错，不创建系列。
3. 权限：operator 403。
"""
import json
import uuid

import pytest

from backend.app.database import get_db


VALID_RULES = {
    "series_id": "ai_japandi",
    "name": "日式侘寂",
    "headboard_types": ["原木格栅床屏配和纸灯带", "侘寂灰泥硬包床屏"],
    "bed_frames": ["低矮地台床", "悬浮式原木床架"],
    "nightstands": ["原木双抽床头柜", "石板面床头柜"],
    "wardrobe_types": ["移门原木衣柜", "开放式挂衣区"],
    "materials": ["白橡木", "和纸", "亚麻", "哑光黑钢"],
    "color_palette": ["原木色", "米白", "灰绿"],
    "soft_upholstery_usage": "仅床屏与坐垫使用亚麻织物",
    "metal_usage": "仅哑光黑钢小五金",
    "forbidden_elements": ["亮光烤漆", "欧式雕花", "高饱和跳色"],
    "forbidden_processes": ["高光钢琴漆", "复杂雕刻"],
    "allowed_structural_changes": ["床宽适配", "背板分段"],
    "hotel_mood": "侘寂禅意，晨光透过和纸洒入，静谧朴素",
    "twin_adapter_rule": "两张地台床等距排布，共用人字纹背板",
    "white_bg_rule": "低重心家具平铺陈列，突出原木肌理",
    "companion_items": ["棉麻沙发配原木茶几", "侘寂扶手椅", "原木书桌椅", "矮电视柜", "黑钢行李架", "布艺床尾凳", "和纸落地灯"],
}

ESCAPED_RESPONSE = "<escape>" + json.dumps(VALID_RULES, ensure_ascii=False) + "</escape>"


def _mock_llm(monkeypatch, content):
    import backend.app.image_adapter as image_adapter
    monkeypatch.setattr("backend.app.routers.system.chat_completion_real",
                        lambda cfg, system, user, llm_model="auto", timeout_seconds=180: content)


def test_ai_generate_series_full_chain(client, monkeypatch):
    # 前置：配置上游密钥（AI 规划复用生图上游）
    client.put("/api/system/image_api", json={
        "enabled": False, "base_url": "http://127.0.0.1:8021", "model": "gpt-image-2",
        "timeout_seconds": 300, "api_key": "sk-llm",
    }, headers={"X-User-Role": "leader"})
    _mock_llm(monkeypatch, ESCAPED_RESPONSE)

    forbidden = client.post("/api/system/series_rules/generate", json={
        "style_brief": "日式侘寂，原木与和纸"
    }, headers={"X-User-Role": "operator"})
    assert forbidden.status_code == 403

    created = client.post("/api/system/series_rules/generate", json={
        "style_brief": "日式侘寂，原木与和纸，晨光静谧"
    }, headers={"X-User-Role": "leader"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["series_id"] == "ai_japandi" and body["rule"]["name"] == "日式侘寂"

    # 规则已持久化且字段完整
    rules = client.get("/api/system/series_rules").json()
    assert "ai_japandi" in rules
    got = rules["ai_japandi"]
    assert got["hotel_mood"].startswith("侘寂禅意")
    assert len(got["companion_items"]) >= 6
    assert got["template_version"].startswith("tpl_ai_japandi")

    # planning_model 记账 + 审计（不含密钥）
    with get_db(immediate=False) as conn:
        ledger = conn.execute(
            "SELECT category, status FROM budget_ledger WHERE note LIKE 'LLM series planning: ai_japandi%'"
        ).fetchall()
        audits = conn.execute(
            "SELECT detail FROM audit_logs WHERE action = 'ai_generate_series_rule'"
        ).fetchall()
    assert ledger and all(r["category"] == "planning_model" and r["status"] == "settled" for r in ledger)
    assert all("sk-llm" not in (a["detail"] or "") for a in audits)

    # 生成的系列可直接用于批次规划
    batch = client.post("/api/batches", json={
        "name": "AI 系列批次", "enabled_series": ["ai_japandi"],
        "target_archived_count": 1, "designs_per_series": 1,
        "standby_designs_per_series": 0, "auto_start_panorama": True,
    }, headers={"X-User-Role": "leader"})
    assert batch.status_code == 200, batch.text
    designs = client.get(f"/api/designs/batch/{batch.json()['id']}").json()
    assert designs[0]["structured_spec"]["series_name"] == "日式侘寂"


def test_ai_generate_rejects_bad_llm_output(client, monkeypatch):
    client.put("/api/system/image_api", json={
        "enabled": False, "base_url": "http://127.0.0.1:8021", "model": "gpt-image-2",
        "timeout_seconds": 300, "api_key": "sk-llm",
    }, headers={"X-User-Role": "leader"})
    _mock_llm(monkeypatch, "抱歉，我无法输出 JSON。")  # 非 JSON
    bad = client.post("/api/system/series_rules/generate", json={
        "style_brief": "地中海风格"
    }, headers={"X-User-Role": "leader"})
    assert bad.status_code == 502 and "JSON" in bad.json()["detail"]

    incomplete = dict(VALID_RULES)
    incomplete.pop("materials")
    incomplete.pop("companion_items")
    _mock_llm(monkeypatch, json.dumps(incomplete, ensure_ascii=False))  # 缺必填
    missing = client.post("/api/system/series_rules/generate", json={
        "style_brief": "北欧极简"
    }, headers={"X-User-Role": "leader"})
    assert missing.status_code == 502 and "必填字段" in missing.json()["detail"]

    # 失败不产生系列残留
    rules = client.get("/api/system/series_rules").json()
    assert all("地中海" not in (r.get("name") or "") for r in rules.values())
