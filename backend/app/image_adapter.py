"""生图上游适配层（可配置 Real API / 本地 Mock 双模）。

上游契约（已对照 ChatGPT2API 本地反代核验）：
- 鉴权: Authorization: Bearer <auth-key>
- GET  /v1/models                健康与契约核验（不消耗生图额度）
- POST /v1/images/generations    {model, prompt, n:1, response_format:"b64_json"}
       -> {"data": [{"b64_json": "..."} | {"url": "..."}]}
- POST /v1/images/edits          multipart 图生图（下游基于全景重绘，后续阶段接入）

错误分类严格映射工作台既有语义（计划第八节）：
- 401/403            -> PermissionError            明确拒绝：释放预占 + 熔断批次
- 429                -> ExplicitRateLimitError      显式限流：有界技术重试，不占人工重做
- 超时/连接断开/5xx/响应缺图 -> TimeoutError/ConnectionResetError
                                                    结果未知：保守持账 + 暂停批次
- 4xx 且内容策略拒绝  -> ValueError("... Policy Violation ...")   明确失败：释放预占
- 其他 4xx（参数错误）-> ValueError                              明确失败：释放预占

密钥红线：密钥不进入前端响应、不进入日志、不写入审计明细；
环境变量 WORKBENCH_IMAGE_API_KEY 优先于库内配置。
"""
import base64
import json
import os
import uuid

import httpx
import sqlite3

DEFAULT_BASE_URL = "http://127.0.0.1:3000"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_TIMEOUT_SECONDS = 300
POLICY_KEYWORDS = ("policy", "content_policy", "content policy", "sensitive", "敏感", "内容政策")


class ExplicitRateLimitError(Exception):
    """显式 HTTP 429 速率限制（区别于连接断开）；有界技术重试。"""
    pass


class ImageApiMisconfigError(ValueError):
    """上游配置无效：明确失败释放预占，不作为未知结果持账。"""
    pass


def load_image_api_config(conn: sqlite3.Connection) -> dict:
    """读取生图上游配置；密钥环境变量优先，库内密钥次之，均无则未配置。"""
    try:
        row = conn.execute("SELECT * FROM image_api_configs WHERE id = 'default'").fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        return _config_dict(False, DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_TIMEOUT_SECONDS, "", "none")
    env_key = os.environ.get("WORKBENCH_IMAGE_API_KEY", "").strip()
    db_key = (row["api_key"] or "").strip() if row["api_key"] else ""
    if env_key:
        key, source = env_key, "env"
    elif db_key:
        key, source = db_key, "db"
    else:
        key, source = "", "none"
    return _config_dict(
        bool(row["enabled"]),
        row["base_url"] or DEFAULT_BASE_URL,
        row["model"] or DEFAULT_MODEL,
        int(row["timeout_seconds"] or DEFAULT_TIMEOUT_SECONDS),
        key,
        source,
    )


def load_text_api_config(conn: sqlite3.Connection) -> dict:
    """文本模型独立配置；环境变量优先，未配置时不使用生图模型凭据。"""
    row = conn.execute("SELECT * FROM text_api_configs WHERE id = 'default'").fetchone()
    env_key = os.environ.get("WORKBENCH_TEXT_API_KEY", "").strip()
    db_key = (row["api_key"] or "").strip() if row else ""
    key = env_key or db_key
    return _config_dict(bool(row), row["base_url"] if row else "",
                        row["model"] if row else "", row["timeout_seconds"] if row else 180,
                        key, "env" if env_key else "db" if db_key else "none")


def _config_dict(enabled: bool, base_url: str, model: str, timeout_seconds: int, api_key: str, key_source: str) -> dict:
    return {
        "enabled": enabled,
        "base_url": base_url,
        "model": model,
        "timeout_seconds": timeout_seconds,
        "api_key": api_key,
        "key_source": key_source,
        "has_key": bool(api_key),
    }


def _error_detail_text(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            if err is not None:
                return str(err)
            return json.dumps(body, ensure_ascii=False)[:300]
    except Exception:
        pass
    return (response.text or "")[:300]


def _classify_response(response: httpx.Response, action: str) -> None:
    """把上游 HTTP 状态映射为工作台故障分类（见模块 docstring）。"""
    if response.status_code in (200, 201):
        return
    detail = _error_detail_text(response)
    lower = detail.lower()
    if response.status_code in (401, 403):
        raise PermissionError(f"HTTP {response.status_code}: 生图上游明确拒绝 ({detail[:200]})")
    if response.status_code == 429:
        raise ExplicitRateLimitError(f"HTTP 429: 生图上游限流 ({detail[:200]})")
    if response.status_code >= 500:
        # 服务端内部错误：无法确认请求是否已被受理，保守按未知处理
        raise ConnectionResetError(f"生图上游服务端错误 HTTP {response.status_code} ({detail[:200]})")
    if any(k in lower for k in POLICY_KEYWORDS):
        raise ValueError(f"HTTP {response.status_code} Content Policy Violation: 内容策略拒绝 ({detail[:200]})")
    raise ValueError(f"HTTP {response.status_code}: {action}请求被拒绝 ({detail[:200]})")


def test_connection(base_url: str, api_key: str, timeout_seconds: int = 15) -> dict:
    """契约核验：GET /v1/models。只验证连通与鉴权，不消耗生图额度；
    通过不代表真实生图一定可用（计划第二阶段红线）。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "error": "未填写 Base URL"}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.get(f"{base}/v1/models", headers=headers)
    except httpx.TimeoutException:
        return {"ok": False, "error": f"连接超时（{timeout_seconds}s）：上游无响应"}
    except httpx.TransportError as e:
        return {"ok": False, "error": f"连接失败: {e}"}
    if resp.status_code in (401, 403):
        return {"ok": False, "status_code": resp.status_code, "error": f"HTTP {resp.status_code}: 鉴权被拒绝，请检查 auth-key"}
    if resp.status_code != 200:
        return {"ok": False, "status_code": resp.status_code, "error": f"HTTP {resp.status_code}: {_error_detail_text(resp)[:200]}"}
    models = []
    try:
        body = resp.json()
        models = [m.get("id") for m in body.get("data", []) if isinstance(m, dict) and m.get("id")]
    except Exception:
        return {"ok": False, "status_code": 200, "error": "HTTP 200 但响应不是合法 JSON，请确认地址指向 API 服务"}
    return {"ok": True, "status_code": 200, "models": models}


def edit_image_real(cfg: dict, prompt: str, image_bytes: bytes, filename: str = "reference.png") -> tuple[bytes, str]:
    """以图生图：multipart 上传参考图调 /v1/images/edits，返回 (图片字节, 上游请求编号)。
    错误分类与 generate_image_real 完全一致。"""
    if not cfg.get("enabled"):
        raise ImageApiMisconfigError("生图上游配置无效: 真实生图未启用")
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    model = (cfg.get("model") or "").strip()
    key = (cfg.get("api_key") or "").strip()
    if not base:
        raise ImageApiMisconfigError("生图上游配置无效: 未填写 Base URL")
    if not model:
        raise ImageApiMisconfigError("生图上游配置无效: 未填写模型名")
    if not key:
        raise ImageApiMisconfigError("生图上游配置无效: 未配置 API 密钥（环境变量或设置页）")

    headers = {"Authorization": f"Bearer {key}"}
    files = {"image": (filename, image_bytes, "image/png")}
    form = {"model": model, "prompt": prompt, "n": "1", "response_format": "b64_json"}
    upstream_id = f"real_edit_{uuid.uuid4().hex[:12]}"
    try:
        with httpx.Client(timeout=cfg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS) as client:
            try:
                resp = client.post(f"{base}/v1/images/edits", data=form, files=files, headers=headers)
            except httpx.TimeoutException as e:
                raise TimeoutError(f"生图上游超时: {e}") from e
            except httpx.TransportError as e:
                raise ConnectionResetError(f"生图上游连接失败: {e}") from e
            _classify_response(resp, "edits")
            try:
                body = resp.json()
            except Exception as e:
                raise ConnectionResetError("生图上游返回非 JSON，无法确认结果") from e
            items = body.get("data") if isinstance(body, dict) else None
            if not items or not isinstance(items[0], dict):
                raise ConnectionResetError("生图上游响应缺少 data 图片项，保守按未知处理")
            if body.get("id"):
                upstream_id = str(body["id"])
            b64 = (items[0].get("b64_json") or "").strip()
            if b64:
                try:
                    return base64.b64decode(b64), upstream_id
                except Exception as e:
                    raise ConnectionResetError("生图上游 b64 图片数据解码失败") from e
            url = (items[0].get("url") or "").strip()
            if url:
                try:
                    img_resp = client.get(url)
                except httpx.TimeoutException as e:
                    raise TimeoutError(f"下载生图结果超时: {e}") from e
                except httpx.TransportError as e:
                    raise ConnectionResetError(f"下载生图结果连接失败: {e}") from e
                if img_resp.status_code in (401, 403):
                    img_resp = client.get(url, headers={"Authorization": f"Bearer {key}"})
                if img_resp.status_code != 200 or not img_resp.content:
                    raise ConnectionResetError(f"下载生图结果失败 HTTP {img_resp.status_code}")
                return img_resp.content, upstream_id
            raise ConnectionResetError("生图上游响应既无 b64_json 也无 url，保守按未知处理")
    except httpx.HTTPError as e:
        raise ConnectionResetError(f"生图上游通信异常: {e}") from e


def chat_completion_real(cfg: dict, system: str, user: str, llm_model: str = "auto",
                         timeout_seconds: int = 120) -> str:
    """调用上游文本模型（LLM 规划师），返回文本内容。
    用于风格方向 → 系列规则 JSON 的自动补全；错误统一抛 ValueError（规划明确失败可重试，
    不涉及生图预算）。"""
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    key = (cfg.get("api_key") or "").strip()
    if not base:
        raise ValueError("LLM 规划失败：生图上游 Base URL 未配置")
    if not key:
        raise ValueError("LLM 规划失败：未配置 API 密钥")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": llm_model or "auto",
        "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(f"{base}/v1/chat/completions", json=payload, headers=headers)
    except httpx.TimeoutException as e:
        raise ValueError(f"LLM 规划超时（{timeout_seconds}s），请重试") from e
    except httpx.TransportError as e:
        raise ValueError(f"LLM 规划连接失败: {e}") from e
    if resp.status_code in (401, 403):
        raise ValueError(f"LLM 规划鉴权被拒绝 (HTTP {resp.status_code})，请检查密钥")
    if resp.status_code != 200:
        raise ValueError(f"LLM 规划请求失败 HTTP {resp.status_code}: {_error_detail_text(resp)[:200]}")
    try:
        body = resp.json()
        return str(body["choices"][0]["message"]["content"] or "")
    except Exception as e:
        raise ValueError("LLM 规划响应格式异常，无法提取文本") from e


def generate_image_real(cfg: dict, prompt: str) -> tuple[bytes, str]:
    """调用上游文生图，返回 (图片字节, 上游请求编号)。
    严格按模块 docstring 的映射抛出分类异常。"""
    if not cfg.get("enabled"):
        raise ImageApiMisconfigError("生图上游配置无效: 真实生图未启用")
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    model = (cfg.get("model") or "").strip()
    key = (cfg.get("api_key") or "").strip()
    if not base:
        raise ImageApiMisconfigError("生图上游配置无效: 未填写 Base URL")
    if not model:
        raise ImageApiMisconfigError("生图上游配置无效: 未填写模型名")
    if not key:
        raise ImageApiMisconfigError("生图上游配置无效: 未配置 API 密钥（环境变量或设置页）")

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "prompt": prompt, "n": 1, "response_format": "b64_json"}
    upstream_id = f"real_{uuid.uuid4().hex[:12]}"
    try:
        with httpx.Client(timeout=cfg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS) as client:
            try:
                resp = client.post(f"{base}/v1/images/generations", json=payload, headers=headers)
            except httpx.TimeoutException as e:
                raise TimeoutError(f"生图上游超时: {e}") from e
            except httpx.TransportError as e:
                raise ConnectionResetError(f"生图上游连接失败: {e}") from e
            _classify_response(resp, "generations")
            try:
                body = resp.json()
            except Exception as e:
                raise ConnectionResetError("生图上游返回非 JSON，无法确认结果") from e
            items = body.get("data") if isinstance(body, dict) else None
            if not items or not isinstance(items[0], dict):
                raise ConnectionResetError("生图上游响应缺少 data 图片项，保守按未知处理")
            if body.get("id"):
                upstream_id = str(body["id"])
            b64 = (items[0].get("b64_json") or "").strip()
            if b64:
                try:
                    return base64.b64decode(b64), upstream_id
                except Exception as e:
                    raise ConnectionResetError("生图上游 b64 图片数据解码失败") from e
            url = (items[0].get("url") or "").strip()
            if url:
                try:
                    img_resp = client.get(url)
                except httpx.TimeoutException as e:
                    raise TimeoutError(f"下载生图结果超时: {e}") from e
                except httpx.TransportError as e:
                    raise ConnectionResetError(f"下载生图结果连接失败: {e}") from e
                if img_resp.status_code in (401, 403):
                    img_resp = client.get(url, headers={"Authorization": f"Bearer {key}"})
                if img_resp.status_code != 200 or not img_resp.content:
                    raise ConnectionResetError(f"下载生图结果失败 HTTP {img_resp.status_code}")
                return img_resp.content, upstream_id
            raise ConnectionResetError("生图上游响应既无 b64_json 也无 url，保守按未知处理")
    except httpx.HTTPError as e:
        # 兜底：未预期的 HTTP 库异常一律按未知保守持账，绝不假定无消耗
        raise ConnectionResetError(f"生图上游通信异常: {e}") from e
