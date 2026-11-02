import json
import uuid
import sqlite3
from typing import List, Dict, Any, Optional
from pathlib import Path
from PIL import Image
from fastapi import APIRouter, Depends, HTTPException, status
from ..database import get_db
from ..config import DEFAULT_WATERMARK, RAW_IMAGES_DIR, PREVIEW_IMAGES_DIR
from ..models import WatermarkConfigRequest, CleanupRunRequest, UnknownTaskResolveRequest, SeriesCreateRequest, SeriesGenerateRequest, ImageApiConfigRequest, TextApiConfigRequest
from ..security import UserContext, get_current_user, require_roles
from ..cleanup_service import run_protected_cleanup
from ..budget_service import resolve_unknown_budget
from ..series_rules_service import get_configured_series_rules, update_series_rule as apply_series_rule_update, create_series_rule as apply_series_rule_create
from ..image_adapter import load_image_api_config, load_text_api_config, test_connection as test_image_api_connection, chat_completion_real
from ..llm_planning_service import parse_llm_rules, build_series_schema_system_prompt

router = APIRouter(prefix="/api/system", tags=["System"])


@router.get("/text_api")
def get_text_api(user: UserContext = Depends(require_roles(["leader", "admin"]))):
    with get_db() as conn:
        cfg = load_text_api_config(conn)
    return {k: cfg[k] for k in ("base_url", "model", "timeout_seconds", "has_key", "key_source")}


@router.put("/text_api")
def put_text_api(req: TextApiConfigRequest, user: UserContext = Depends(require_roles(["leader", "admin"]))):
    if not req.base_url.strip() or not req.model.strip():
        raise HTTPException(status_code=400, detail="文本模型地址和名称不能为空")
    with get_db(immediate=True) as conn:
        conn.execute("""INSERT INTO text_api_configs (id, base_url, model, timeout_seconds, api_key, updated_by)
            VALUES ('default', ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
            base_url=excluded.base_url, model=excluded.model, timeout_seconds=excluded.timeout_seconds,
            api_key=COALESCE(NULLIF(excluded.api_key, ''), text_api_configs.api_key),
            updated_by=excluded.updated_by, updated_at=datetime('now', 'localtime')""",
            (req.base_url.strip(), req.model.strip(), req.timeout_seconds, req.api_key, user.user_id))
        conn.execute("INSERT INTO audit_logs (id, action, actor_name, actor_role, detail) VALUES (?, 'update_text_api_config', ?, ?, ?)",
                     (f"aud_{uuid.uuid4().hex[:8]}", user.user_id, user.role,
                      json.dumps({"base_url": req.base_url.strip(), "model": req.model.strip(), "api_key_changed": bool(req.api_key)})))
    return {"message": "文本模型配置已保存"}

@router.get("/series_rules")
def get_series_rules(
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """获取持久化产品系列规则库与模板版本"""
    with get_db(immediate=False) as conn:
        return get_configured_series_rules(conn)

@router.post("/series_rules/generate")
def generate_series_from_style(
    req: SeriesGenerateRequest,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """AI 风格规划：一句话风格方向 → LLM 补全整套系列规则 → 自动创建系列。
    规划成本记 planning_model 账本（与生图额度分离）；生图仍走完整批次人工审图流程。"""
    with get_db(immediate=False) as conn:
        text_cfg = load_text_api_config(conn)
        # 旧安装未配置文本服务时保持原有系列规划入口可用。
        api_cfg = text_cfg if text_cfg["enabled"] else load_image_api_config(conn)
    if not api_cfg["has_key"]:
        raise HTTPException(status_code=400, detail="请先配置文本模型 API 密钥")
    try:
        raw = chat_completion_real(
            api_cfg, build_series_schema_system_prompt(),
            f"风格方向：{req.style_brief.strip()}\n请输出完整的系列规则 JSON。",
            llm_model=api_cfg.get("model") if text_cfg["enabled"] else "auto",
            timeout_seconds=api_cfg.get("timeout_seconds") if text_cfg["enabled"] else 180,
        )
        rules = parse_llm_rules(raw)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    with get_db(immediate=True) as conn:
        try:
            rule = apply_series_rule_create(
                conn, rules["series_id"], rules["name"],
                {k: v for k, v in rules.items() if k not in ("series_id", "name")},
                user.user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        conn.execute("""
            INSERT INTO budget_ledger (id, batch_id, category, delta_reserved, delta_used, delta_unsettled, status, note)
            VALUES (?, NULL, 'planning_model', 0, 1, 0, 'settled', ?)
        """, (f"bgt_llm_{uuid.uuid4().hex[:8]}", f"LLM series planning: {rules['series_id']}"))
        conn.execute("""
            INSERT INTO audit_logs (id, action, actor_name, actor_role, detail)
            VALUES (?, 'ai_generate_series_rule', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", user.user_id, user.role, json.dumps({
            "series_id": rules["series_id"], "name": rules["name"],
            "style_brief": req.style_brief.strip()[:200],
            "llm_model": api_cfg["model"] if text_cfg["enabled"] else "auto",
            "template_version": rule["template_version"],
        })))
    return {"message": f"AI 已生成并创建系列「{rules['name']}」",
            "series_id": rules["series_id"], "rule": rule}

@router.post("/series_rules")
def add_series_rule(
    req: SeriesCreateRequest,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """负责人自定义新增产品系列（结构化规则即提示词来源）"""
    with get_db(immediate=True) as conn:
        try:
            rule = apply_series_rule_create(conn, req.series_id, req.name, req.rule, user.user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        conn.execute("""
            INSERT INTO audit_logs (id, action, actor_name, actor_role, detail)
            VALUES (?, 'create_series_rule', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", user.user_id, user.role, json.dumps({
            "series_id": req.series_id,
            "name": req.name,
            "template_version": rule["template_version"]
        })))
    return {"message": f"系列「{req.name}」创建成功，规则沿用当前模板，可继续编辑后保存修订",
            "series_id": req.series_id, "rule": rule}

@router.put("/series_rules/{series_id}")
def update_series_rule(
    series_id: str,
    rules: Dict[str, Any],
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """负责人配置并更新产品系列规则库；每次修订生成新的规划模板版本"""
    with get_db(immediate=True) as conn:
        try:
            rule = apply_series_rule_update(conn, series_id, rules, user.user_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="系列不存在")

        conn.execute("""
            INSERT INTO audit_logs (id, action, actor_name, actor_role, detail)
            VALUES (?, 'update_series_rule', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", user.user_id, user.role, json.dumps({
            "series_id": series_id,
            "updated": rules,
            "template_version": rule["template_version"]
        })))
    return {"message": "系列规则库更新成功", "series_id": series_id, "rule": rule}

@router.get("/image_api")
def get_image_api_config(
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """生图上游配置（密钥脱敏：只返回是否已配置与来源，绝不返回密钥内容）"""
    with get_db(immediate=False) as conn:
        cfg = load_image_api_config(conn)
    return {
        "enabled": cfg["enabled"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "timeout_seconds": cfg["timeout_seconds"],
        "has_key": cfg["has_key"],
        "key_source": cfg["key_source"],
        "mode": "real_api" if cfg["enabled"] else "mock",
    }

@router.put("/image_api")
def update_image_api_config(
    req: ImageApiConfigRequest,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """负责人配置生图上游；api_key 留空 = 保持现有密钥不变（密钥不进审计明细、不回显）"""
    with get_db(immediate=True) as conn:
        cur = conn.execute("""
            UPDATE image_api_configs
            SET enabled = ?, base_url = ?, model = ?, timeout_seconds = ?,
                api_key = CASE WHEN ? IS NOT NULL AND ? != '' THEN ? ELSE api_key END,
                updated_by = ?, updated_at = datetime('now', 'localtime')
            WHERE id = 'default'
        """, (1 if req.enabled else 0, req.base_url.strip(), req.model.strip(),
              req.timeout_seconds, req.api_key, req.api_key, req.api_key,
              user.user_id))
        if cur.rowcount == 0:
            conn.execute("""
                INSERT INTO image_api_configs (id, enabled, base_url, model, timeout_seconds, api_key, updated_by)
                VALUES ('default', ?, ?, ?, ?, ?, ?)
            """, (1 if req.enabled else 0, req.base_url.strip(), req.model.strip(),
                  req.timeout_seconds, req.api_key or None, user.user_id))

        conn.execute("""
            INSERT INTO audit_logs (id, action, actor_name, actor_role, detail)
            VALUES (?, 'update_image_api_config', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", user.user_id, user.role, json.dumps({
            "enabled": req.enabled,
            "base_url": req.base_url.strip(),
            "model": req.model.strip(),
            "timeout_seconds": req.timeout_seconds,
            "api_key_changed": bool(req.api_key),
            # 密钥内容绝不写入审计
        })))

        cfg = load_image_api_config(conn)
    return {
        "message": "生图上游配置已保存" + ("（已启用真实生图）" if cfg["enabled"] else "（当前仍为本地 Mock）"),
        "enabled": cfg["enabled"],
        "has_key": cfg["has_key"],
        "key_source": cfg["key_source"],
    }

@router.post("/image_api/test")
def test_image_api(
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """测试生图上游连接（GET /v1/models 契约核验，不消耗生图额度；
    通过不代表真实生图一定可用）。使用已保存的配置。"""
    with get_db(immediate=False) as conn:
        cfg = load_image_api_config(conn)
    if not cfg["has_key"]:
        result = {"ok": False, "error": "尚未配置 API 密钥"}
    else:
        result = test_image_api_connection(cfg["base_url"], cfg["api_key"])

    with get_db(immediate=True) as conn:
        conn.execute("""
            INSERT INTO audit_logs (id, action, actor_name, actor_role, detail)
            VALUES (?, 'test_image_api', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", user.user_id, user.role, json.dumps({
            "ok": result.get("ok"),
            "status_code": result.get("status_code"),
            "models": (result.get("models") or [])[:10],
            "error": (result.get("error") or "")[:200],
        })))
    return result

@router.get("/batches/{batch_id}/watermark")
def get_watermark_config(
    batch_id: str,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("SELECT main_text, repeat_text, opacity, font_size, position, updated_by, updated_at FROM watermark_configs WHERE batch_id = ?", (batch_id,))
        row = cur.fetchone()
        if not row:
            return DEFAULT_WATERMARK
        return dict(row)

@router.post("/batches/{batch_id}/watermark")
def update_watermark_config(
    batch_id: str,
    req: WatermarkConfigRequest,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    with get_db(immediate=True) as conn:
        conn.execute("""
            INSERT INTO watermark_configs (
                id, batch_id, main_text, repeat_text, opacity, font_size, position, updated_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(batch_id) DO UPDATE SET
                main_text = excluded.main_text,
                repeat_text = excluded.repeat_text,
                opacity = excluded.opacity,
                font_size = excluded.font_size,
                position = excluded.position,
                updated_by = excluded.updated_by,
                updated_at = datetime('now', 'localtime')
        """, (
            f"wm_{uuid.uuid4().hex[:8]}", batch_id, req.main_text, req.repeat_text,
            req.opacity, req.font_size, req.position, user.user_id
        ))

        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'update_watermark', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, user.user_id, user.role, json.dumps(req.model_dump())))

    return {"message": "水印配置已更新", "config": req.model_dump()}

@router.post("/cleanup")
def trigger_protected_cleanup(
    req: CleanupRunRequest,
    user: UserContext = Depends(require_roles(["admin"]))
):
    with get_db(immediate=True) as conn:
        result = run_protected_cleanup(conn, days_threshold=req.days_threshold)
        conn.execute("""
            INSERT INTO audit_logs (id, action, actor_name, actor_role, detail)
            VALUES (?, 'run_cleanup', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", user.user_id, user.role, json.dumps(result)))

    return {
        "message": "受保护清理执行完成",
        "stats": result
    }

@router.get("/batches/{batch_id}/unknown_tasks")
def get_unknown_tasks(
    batch_id: str,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT sv.id as version_id, sv.slot_id, sv.design_id, sv.process_status, sv.error_message, sv.created_at,
                   d.design_code, s.slot_type, t.id as task_id, bl.id as ledger_id
            FROM slot_versions sv
            JOIN slots s ON sv.slot_id = s.id
            JOIN designs d ON sv.design_id = d.id
            LEFT JOIN tasks t ON t.slot_version_id = sv.id
            LEFT JOIN budget_ledger bl ON bl.reference_task_id = t.id
                AND bl.status IN ('reserved', 'unknown_held')
            WHERE d.batch_id = ? AND (sv.process_status = 'unknown_pending_confirmation' OR sv.is_unknown = 1)
        """, (batch_id,))
        rows = cur.fetchall()
        return [dict(r) for r in rows]

@router.post("/resolve_unknown")
def resolve_unknown_task(
    req: UnknownTaskResolveRequest,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """
    结果未知任务人工处置（仅负责人/管理员）：
    - 状态核验：严格校验任务确实为 unknown 状态
    - 防过时指针覆盖：若设计已终态或槽位已生成更新版本，禁止覆盖槽位指针与设计状态
    - confirm_success: 强制验证图片文件存在与真实解码
    - confirm_failure: 强制校验有效未扣费证据
    - 恢复审计：结构化恢复，人工暂停或熔断绝不误恢复
    """
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.id, t.batch_id, t.slot_id, t.slot_version_id, t.status, bl.id as ledger_id,
                   sv.process_status, sv.raw_image_path, sv.preview_image_path, s.slot_type, t.design_id,
                   d.status as design_status, s.current_version_id as slot_current_vid
            FROM tasks t
            JOIN slot_versions sv ON t.slot_version_id = sv.id
            JOIN slots s ON t.slot_id = s.id
            JOIN designs d ON t.design_id = d.id
            LEFT JOIN budget_ledger bl ON bl.reference_task_id = t.id
                AND bl.status IN ('reserved', 'unknown_held')
            WHERE t.id = ?
        """, (req.task_id,))
        task_row = cur.fetchone()
        if not task_row:
            raise HTTPException(status_code=404, detail="未知任务不存在")

        if task_row["status"] != "unknown" or task_row["process_status"] != "unknown_pending_confirmation":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"任务状态当前为 '{task_row['status']}' (版本: '{task_row['process_status']}'), 并非未决 unknown 任务，禁止重复处置！"
            )

        bid = task_row["batch_id"]
        vid = task_row["slot_version_id"]
        sid = task_row["slot_id"]
        did = task_row["design_id"]
        slot_type = task_row["slot_type"]
        ledger_id = task_row["ledger_id"]
        design_curr_status = task_row["design_status"]
        slot_current_vid = task_row["slot_current_vid"]

        if req.resolution == "confirm_success":
            raw_path_str = task_row["raw_image_path"]
            preview_path_str = task_row["preview_image_path"]

            if not raw_path_str or not preview_path_str:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="确认成功失败：该任务尚未落盘图片文件，不可确认成功！"
                )

            raw_file = RAW_IMAGES_DIR / raw_path_str
            prev_file = PREVIEW_IMAGES_DIR / preview_path_str

            if not raw_file.exists() or not prev_file.exists():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="确认成功失败：本地未找到完整图像文件或预览图丢失，严禁确认为成功！"
                )

            try:
                with Image.open(raw_file) as im_r:
                    im_r.verify()
                with Image.open(prev_file) as im_p:
                    im_p.verify()
            except Exception as img_err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"确认成功失败：图片文件损坏无法正常解码 ({str(img_err)})，严禁确认为成功！"
                )

            conn.execute("""
                UPDATE slot_versions 
                SET process_status = 'completed', is_unknown = 0, error_message = NULL
                WHERE id = ?
            """, (vid,))
            conn.execute("UPDATE tasks SET status = 'completed', updated_at = datetime('now', 'localtime') WHERE id = ?", (req.task_id,))

            # 核心防覆盖防复活约束：
            # 只有当设计非终态 (未淘汰且未归档)，且槽位当前指向正是该版本或为空时，才更新槽位指针与设计状态！
            is_stale_or_finalized = (design_curr_status in ("eliminated", "archived")) or (slot_current_vid not in (vid, None))

            if not is_stale_or_finalized:
                conn.execute("UPDATE slots SET status = 'ready', current_version_id = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (vid, sid))
                if slot_type == "panorama":
                    conn.execute("""
                        UPDATE designs 
                        SET status = 'panorama_review', updated_at = datetime('now', 'localtime') 
                        WHERE id = ? AND status = 'panorama_generating'
                    """, (did,))

            if ledger_id:
                resolve_unknown_budget(conn, bid, ledger_id, is_success=True)

        elif req.resolution == "confirm_failure":
            evidence = (req.evidence or req.note or "").strip()
            billing_reference = (req.billing_reference or "").strip()
            if (
                not evidence
                or len(evidence) < 4
                or "无法确认" in evidence
                or "unknown" in evidence.lower()
                or len(billing_reference) < 6
                or not req.attests_no_charge
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=("释放额度失败：需要负责人提供核账凭据编号并显式确认上游无扣费。"
                            "本地阶段不能机器验证真实账单，无法确认消费时必须保持预算占用 (unknown_held)。")
                )

            conn.execute("""
                UPDATE slot_versions 
                SET process_status = 'failed', is_unknown = 0, error_message = ?
                WHERE id = ?
            """, (f"Confirmed failure by {user.role}: {evidence}", vid))
            conn.execute("UPDATE tasks SET status = 'failed', updated_at = datetime('now', 'localtime') WHERE id = ?", (req.task_id,))
            
            # 若当前槽位仍指向该失败版本，清空为 empty
            if slot_current_vid == vid:
                conn.execute("UPDATE slots SET status = 'empty', updated_at = datetime('now', 'localtime') WHERE id = ?", (sid,))
            if ledger_id:
                resolve_unknown_budget(conn, bid, ledger_id, is_success=False)
        else:
            raise HTTPException(status_code=400, detail="无效的处置方式，只能是 confirm_success 或 confirm_failure")

        # 结构化批次自愈恢复：
        # 仅当批次处于 pause_reason == 'PAUSE_UNKNOWN_PENDING'，且未熔断 (is_circuit_broken == 0)，
        # 且该批次内已经没有任何处于 unknown_pending_confirmation 的任务时，才安全恢复为 active！
        cur.execute("""
            SELECT COUNT(*) as c FROM slot_versions sv
            JOIN designs d ON sv.design_id = d.id
            WHERE d.batch_id = ? AND sv.process_status = 'unknown_pending_confirmation'
        """, (bid,))
        remaining_unknown = cur.fetchone()["c"]

        cur.execute("SELECT status, is_circuit_broken, pause_reason FROM batches WHERE id = ?", (bid,))
        batch_state = cur.fetchone()

        if remaining_unknown == 0 and batch_state:
            # 只有在因 unknown 挂起暂停且未熔断时才恢复；人工主动暂停或熔断绝不恢复！
            if batch_state["pause_reason"] == "PAUSE_UNKNOWN_PENDING" and batch_state["is_circuit_broken"] == 0:
                conn.execute("""
                    UPDATE batches 
                    SET status = 'active', pause_reason = NULL, updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                """, (bid,))
                conn.execute("""
                    INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
                    VALUES (?, ?, 'auto_resume_batch_after_unknown_resolved', 'system', 'system', ?)
                """, (f"aud_{uuid.uuid4().hex[:8]}", bid, json.dumps({"resolved_task_id": req.task_id})))

        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'resolve_unknown_task', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", bid, user.user_id, user.role, json.dumps({
            "task_id": req.task_id,
            "resolution": req.resolution,
            "evidence": req.evidence,
            "note": req.note,
            "billing_reference": req.billing_reference,
            "human_attested_no_charge": req.attests_no_charge,
            "verification_level": "human_ledger_attestation"
        })))

    return {"message": "未知状态已人工处置完成", "task_id": req.task_id, "resolution": req.resolution}

@router.get("/batches/{batch_id}/audit_logs")
def get_audit_logs(
    batch_id: str,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM audit_logs WHERE batch_id = ? ORDER BY created_at DESC LIMIT 100", (batch_id,))
        rows = cur.fetchall()
        return [dict(r) for r in rows]
