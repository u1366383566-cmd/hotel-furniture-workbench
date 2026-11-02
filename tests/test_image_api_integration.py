"""生图上游适配器（可配置 Real API）离线测试。

覆盖：
1. 配置 CRUD + 密钥脱敏（GET 永不返回密钥内容；留空保存保持不变；审计不落密钥）。
2. HTTP 错误分类映射（严格对应工作台既有语义：401/403熔断、429技术重试、
   超时/断连/5xx/缺图按未知、内容策略与参数错误明确失败）。
3. b64_json 与 url 两种响应形态的图片解码。
4. worker 真实模式端到端：任务走适配器、落盘、结算、metadata 记录；
   上游明确拒绝时熔断并释放预占。
"""
import base64
import io
import json

import httpx
import pytest
from PIL import Image

import backend.app.image_adapter as image_adapter
from backend.app.image_adapter import (
    ExplicitRateLimitError, generate_image_real, load_image_api_config,
    ImageApiMisconfigError,
)
from backend.app.image_adapter import test_connection as run_connection_test
from backend.app.database import get_db
from backend.app.task_worker import worker_instance


def _png_bytes(color=(120, 90, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buf, format="PNG")
    return buf.getvalue()



def _patch_http(monkeypatch, handler):
    """把 image_adapter 的 httpx.Client 指向 MockTransport。
    提前绑定真实 Client 类，避免 lambda 内再次引用已被替换的名字导致无限递归。"""
    real_client_cls = httpx.Client
    transport = httpx.MockTransport(handler)

    def factory(**kw):
        kw.pop("transport", None)
        return real_client_cls(transport=transport, **kw)

    monkeypatch.setattr(image_adapter.httpx, "Client", factory)

def _cfg(**overrides):
    cfg = {
        "enabled": True, "base_url": "http://upstream.test", "model": "gpt-image-2",
        "timeout_seconds": 30, "api_key": "sk-test", "key_source": "db", "has_key": True,
    }
    cfg.update(overrides)
    return cfg


def _transport(handler):
    return httpx.MockTransport(handler)


# ---------- 错误分类映射 ----------

def test_401_maps_to_permission_error(monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "invalid key"}})
    _patch_http(monkeypatch, handler)
    with pytest.raises(PermissionError):
        generate_image_real(_cfg(), "prompt")


def test_429_maps_to_explicit_rate_limit(monkeypatch):
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})
    _patch_http(monkeypatch, handler)
    with pytest.raises(ExplicitRateLimitError):
        generate_image_real(_cfg(), "prompt")


def test_content_policy_maps_to_value_error(monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "content_policy violation: sensitive"}})
    _patch_http(monkeypatch, handler)
    with pytest.raises(ValueError, match="Policy Violation"):
        generate_image_real(_cfg(), "prompt")


def test_server_error_maps_to_unknown(monkeypatch):
    def handler(request):
        return httpx.Response(500, text="boom")
    _patch_http(monkeypatch, handler)
    with pytest.raises(ConnectionResetError):
        generate_image_real(_cfg(), "prompt")


def test_missing_image_data_maps_to_unknown(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": []})
    _patch_http(monkeypatch, handler)
    with pytest.raises(ConnectionResetError):
        generate_image_real(_cfg(), "prompt")


def test_misconfig_raises_explicit_failure(monkeypatch):
    with pytest.raises(image_adapter.ImageApiMisconfigError):
        generate_image_real(_cfg(api_key=""), "prompt")
    with pytest.raises(image_adapter.ImageApiMisconfigError):
        generate_image_real(_cfg(base_url=" "), "prompt")


def test_b64_response_decodes_to_bytes(monkeypatch):
    payload = base64.b64encode(_png_bytes()).decode("ascii")

    def handler(request):
        assert request.headers["Authorization"] == "Bearer sk-test"
        body = json.loads(request.content)
        assert body["model"] == "gpt-image-2" and body["n"] == 1
        assert body["response_format"] == "b64_json"
        return httpx.Response(200, json={"id": "req_abc", "data": [{"b64_json": payload}]})

    _patch_http(monkeypatch, handler)
    raw, upstream_id = generate_image_real(_cfg(), "prompt")
    assert raw == _png_bytes()
    assert upstream_id == "req_abc"


def test_url_response_downloads_bytes(monkeypatch):
    png = _png_bytes()

    def handler(request):
        if request.url.path == "/v1/images/generations":
            return httpx.Response(200, json={"data": [{"url": "http://upstream.test/images/1.png"}]})
        if request.url.path == "/images/1.png":
            return httpx.Response(200, content=png)
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)
    raw, _ = generate_image_real(_cfg(), "prompt")
    assert raw == png


def test_test_connection_reports_models(monkeypatch):
    def handler(request):
        if request.headers.get("Authorization") != "Bearer sk-good":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        return httpx.Response(200, json={"data": [{"id": "gpt-image-2"}, {"id": "auto"}]})

    _patch_http(monkeypatch, handler)
    ok = run_connection_test("http://upstream.test", "sk-good")
    denied = run_connection_test("http://upstream.test", "sk-bad")
    assert ok["ok"] is True and "gpt-image-2" in ok["models"]
    assert denied["ok"] is False and denied["status_code"] == 401


# ---------- 设置 CRUD 与密钥脱敏 ----------

def test_image_api_settings_crud_and_key_masking(client):
    got = client.get("/api/system/image_api", headers={"X-User-Role": "leader"}).json()
    assert got["enabled"] is False and got["has_key"] is False
    assert got["mode"] == "mock" and got["base_url"]

    forbidden = client.get("/api/system/image_api", headers={"X-User-Role": "operator"})
    assert forbidden.status_code == 403

    saved = client.put("/api/system/image_api", json={
        "enabled": True, "base_url": "http://127.0.0.1:3000", "model": "gpt-image-2",
        "timeout_seconds": 120, "api_key": "sk-super-secret",
    }, headers={"X-User-Role": "leader"})
    assert saved.status_code == 200 and saved.json()["has_key"] is True

    got = client.get("/api/system/image_api", headers={"X-User-Role": "leader"}).json()
    assert got["enabled"] is True and got["has_key"] is True and got["key_source"] == "db"
    assert got["timeout_seconds"] == 120
    # 密钥内容绝不回显
    assert "sk-super-secret" not in json.dumps(got)

    # 留空保存 = 密钥保持不变
    client.put("/api/system/image_api", json={
        "enabled": True, "base_url": "http://127.0.0.1:3000", "model": "gpt-image-2",
        "timeout_seconds": 120,
    }, headers={"X-User-Role": "leader"})
    with get_db(immediate=False) as conn:
        row = conn.execute("SELECT api_key FROM image_api_configs WHERE id = 'default'").fetchone()
    assert row["api_key"] == "sk-super-secret"

    # 审计明细不落密钥
    with get_db(immediate=False) as conn:
        audits = conn.execute(
            "SELECT detail FROM audit_logs WHERE action = 'update_image_api_config'"
        ).fetchall()
    assert all("sk-super-secret" not in (a["detail"] or "") for a in audits)

    # operator 不可修改
    denied = client.put("/api/system/image_api", json={
        "enabled": False, "base_url": "x", "model": "y",
    }, headers={"X-User-Role": "operator"})
    assert denied.status_code == 403


# ---------- worker 真实模式端到端 ----------

def _create_single_design_batch(client):
    resp = client.post("/api/batches", json={
        "name": "真实生图集成批次", "enabled_series": ["modern_business"],
        "target_archived_count": 1, "designs_per_series": 1,
        "standby_designs_per_series": 0, "auto_start_panorama": True,
    }, headers={"X-User-Role": "leader"})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_worker_real_mode_completes_and_records_metadata(client, monkeypatch):
    batch_id = _create_single_design_batch(client)
    with get_db(immediate=True) as conn:
        conn.execute("""
            UPDATE image_api_configs SET enabled = 1, base_url = 'http://upstream.test',
                model = 'gpt-image-2', api_key = 'sk-worker'
        """)
        design = conn.execute(
            "SELECT id FROM designs WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        task = conn.execute(
            "SELECT id, slot_version_id FROM tasks WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    tid, vid = task["id"], task["slot_version_id"]

    calls = {}

    def fake_generate(cfg, prompt):
        calls["cfg"] = cfg
        calls["prompt"] = prompt
        return _png_bytes((10, 200, 30)), "real_upstream_001"

    monkeypatch.setattr("backend.app.task_worker.generate_image_real", fake_generate)

    assert worker_instance.process_next_task() is True

    assert "sk-worker" not in calls["prompt"]
    assert "床屏" in calls["prompt"]  # 提示词由结构化规则自动构建
    with get_db(immediate=False) as conn:
        version = conn.execute(
            "SELECT process_status, raw_image_path, preview_image_path, upstream_request_id, prompt_metadata "
            "FROM slot_versions WHERE id = ?", (vid,)
        ).fetchone()
        task_status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]
        ledger = conn.execute(
            "SELECT status FROM budget_ledger WHERE reference_task_id = ?", (tid,)
        ).fetchone()["status"]
    assert version["process_status"] == "completed" and task_status == "completed"
    assert version["upstream_request_id"] == "real_upstream_001"
    assert ledger == "settled"
    assert version["raw_image_path"] and version["preview_image_path"]
    meta = json.loads(version["prompt_metadata"])
    assert meta["generation_mode"] == "real_api" and meta["upstream_model"] == "gpt-image-2"
    assert meta["real_prompt"] == calls["prompt"]

    from backend.app.config import RAW_IMAGES_DIR, PREVIEW_IMAGES_DIR
    assert (RAW_IMAGES_DIR / version["raw_image_path"]).exists()
    assert (PREVIEW_IMAGES_DIR / version["preview_image_path"]).exists()


def test_worker_real_mode_explicit_rejection_circuits_and_releases(client, monkeypatch):
    batch_id = _create_single_design_batch(client)
    with get_db(immediate=True) as conn:
        conn.execute("""
            UPDATE image_api_configs SET enabled = 1, base_url = 'http://upstream.test',
                model = 'gpt-image-2', api_key = 'sk-bad'
        """)
        task = conn.execute(
            "SELECT id, slot_version_id FROM tasks WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    tid, vid = task["id"], task["slot_version_id"]

    def fake_generate(cfg, prompt):
        raise PermissionError("HTTP 401: invalid key")

    monkeypatch.setattr("backend.app.task_worker.generate_image_real", fake_generate)
    worker_instance.process_next_task()

    with get_db(immediate=False) as conn:
        task_status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]
        ledger = conn.execute(
            "SELECT status FROM budget_ledger WHERE reference_task_id = ?", (tid,)
        ).fetchone()["status"]
        batch = conn.execute("SELECT is_circuit_broken FROM batches WHERE id = ?", (batch_id,)).fetchone()["is_circuit_broken"]
    assert task_status == "failed" and ledger == "released" and batch == 1


def test_worker_real_mode_downstream_uses_image_edit(client, monkeypatch):
    """下游三图必须走以图生图（/v1/images/edits 上传全景原图），不允许纯文生图。"""
    batch_id = _create_single_design_batch(client)
    with get_db(immediate=True) as conn:
        conn.execute("""
            UPDATE image_api_configs SET enabled = 1, base_url = 'http://upstream.test',
                model = 'gpt-image-2', api_key = 'sk-worker'
        """)

    pano_bytes = _png_bytes((10, 10, 10))
    edit_calls = []

    def fake_edit(cfg, prompt, image_bytes, filename="reference.png"):
        edit_calls.append({"prompt": prompt, "image": image_bytes, "filename": filename})
        return _png_bytes((200, 30, 30)), f"real_edit_{len(edit_calls):03d}"

    # 先跑完全景（文生图 mock），人工通过，再启动下游
    monkeypatch.setattr("backend.app.task_worker.generate_image_real",
                        lambda cfg, p: (pano_bytes, "real_pano_001"))
    monkeypatch.setattr("backend.app.task_worker.edit_image_real", fake_edit)
    worker_instance.process_next_task()  # 全景
    with get_db(immediate=False) as conn:
        design = conn.execute("SELECT id FROM designs WHERE batch_id = ?", (batch_id,)).fetchone()["id"]
        pano_vid = conn.execute(
            "SELECT current_version_id FROM slots WHERE design_id = ? AND slot_type = 'panorama'", (design,)
        ).fetchone()["current_version_id"]
    approve = client.post(f"/api/designs/{design}/review_panorama", json={
        "action": "approve", "expected_version_id": pano_vid,
    })
    assert approve.status_code == 200, approve.text
    start = client.post(f"/api/designs/{design}/start_downstream")
    assert start.status_code == 200, start.text

    # 下游第一棒：white_isolated —— 以全景原图为参考（固定三行版式）
    assert worker_instance.process_next_task() is True
    assert len(edit_calls) == 1, "白底清单未走以图生图管线"
    assert edit_calls[0]["image"] == pano_bytes, "白底清单的参考图必须是全景原图"
    assert "第一行" in edit_calls[0]["prompt"] and "三行版式" in edit_calls[0]["prompt"]
    assert "全套大床组" in edit_calls[0]["prompt"]
    with get_db(immediate=False) as conn:
        wi_row = conn.execute("""
            SELECT sv.raw_image_path FROM slots s JOIN slot_versions sv ON s.current_version_id = sv.id
            WHERE s.design_id = ? AND s.slot_type = 'white_isolated'
        """, (design,)).fetchone()
    assert wi_row["raw_image_path"], "白底清单应已完成"

    # 下游第二棒：twin_bed —— 以白底清单原图（而非全景）为参考
    assert worker_instance.process_next_task() is True
    assert len(edit_calls) == 2, "双床任务未走以图生图管线"
    from backend.app.config import RAW_IMAGES_DIR
    assert edit_calls[1]["image"] == (RAW_IMAGES_DIR / wi_row["raw_image_path"]).read_bytes(),         "双床的参考图必须是白底清单原图"
    assert "两张单人床" in edit_calls[1]["prompt"] and "不添加衣柜" in edit_calls[1]["prompt"], edit_calls[1]["prompt"][:200]

    with get_db(immediate=False) as conn:
        meta = json.loads(conn.execute(
            "SELECT prompt_metadata FROM slot_versions WHERE upstream_request_id = 'real_edit_001'"
        ).fetchone()["prompt_metadata"])
    assert meta["generation_pipeline"] == "image_edit"
    assert meta["reference_panorama_version_id"] == pano_vid


def test_image_edit_multipart_request_format(monkeypatch):
    """edits 请求必须是 multipart：model/prompt/n/response_format 表单 + image 文件。"""
    captured = {}

    def handler(request):
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png_bytes()).decode()}]})

    _patch_http(monkeypatch, handler)
    raw, _ = image_adapter.edit_image_real(_cfg(), "白底提示词", _png_bytes(), filename="ref.png")
    assert len(raw) > 0
    assert "multipart/form-data" in captured["content_type"]
    assert b'name="model"' in captured["body"] and b"gpt-image-2" in captured["body"]
    assert b'name="prompt"' in captured["body"]
    assert b'name="image"' in captured["body"] and b"ref.png" in captured["body"]


def test_worker_real_mode_misconfig_releases_budget(client, monkeypatch):
    batch_id = _create_single_design_batch(client)
    with get_db(immediate=True) as conn:
        # 启用但无密钥 -> 配置无效，明确失败释放，绝不按 unknown 持账
        conn.execute("""
            UPDATE image_api_configs SET enabled = 1, base_url = 'http://upstream.test',
                model = 'gpt-image-2', api_key = NULL
        """)
        task = conn.execute(
            "SELECT id FROM tasks WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    tid = task["id"]

    monkeypatch.setattr("backend.app.task_worker.generate_image_real",
        lambda cfg, p: (_ for _ in ()).throw(
            ImageApiMisconfigError("生图上游配置无效: 未配置 API 密钥")))
    worker_instance.process_next_task()

    with get_db(immediate=False) as conn:
        task_status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]
        ledger = conn.execute(
            "SELECT status FROM budget_ledger WHERE reference_task_id = ?", (tid,)
        ).fetchone()["status"]
        version = conn.execute(
            "SELECT process_status FROM slot_versions WHERE id = (SELECT slot_version_id FROM tasks WHERE id = ?)",
            (tid,),
        ).fetchone()["process_status"]
    assert task_status == "failed" and ledger == "released" and version == "failed"
