import json
import logging
import uuid
import sqlite3
import io
from PIL import Image
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from ..database import get_db
from ..models import (
    DesignDetail, SlotDetail, SlotVersionDetail, PanoramaReviewRequest, QuadReviewRequest
)
from ..security import UserContext, get_current_user, require_roles

logger = logging.getLogger(__name__)
from ..budget_service import reserve_budget, BudgetWarningExceededError, BudgetHardLimitExceededError
from ..config import DEFAULT_REDO_SLOT_LIMIT, DEFAULT_REDO_DESIGN_LIMIT, RAW_IMAGES_DIR, PREVIEW_IMAGES_DIR, DEFAULT_WATERMARK
from ..watermark_service import apply_watermark
from ..planning_service import build_mock_visual_features, build_slot_prompt

router = APIRouter(prefix="/api/designs", tags=["Designs"])

def _load_design_detail(conn: sqlite3.Connection, design_id: str, is_admin: bool = False) -> DesignDetail:
    cur = conn.cursor()
    cur.execute("SELECT * FROM designs WHERE id = ?", (design_id,))
    d_row = cur.fetchone()
    if not d_row:
        raise HTTPException(status_code=404, detail="设计不存在")

    # 获取 4 个槽位
    cur.execute("SELECT * FROM slots WHERE design_id = ?", (design_id,))
    slot_rows = cur.fetchall()

    slots_dict: Dict[str, SlotDetail] = {}
    for sr in slot_rows:
        sid = sr["id"]
        stype = sr["slot_type"]
        c_vid = sr["current_version_id"]

        # 获取该槽位的所有版本
        cur.execute("SELECT * FROM slot_versions WHERE slot_id = ? ORDER BY version_num DESC", (sid,))
        v_rows = cur.fetchall()
        
        versions_list = []
        curr_v_detail = None
        for vr in v_rows:
            # 权限隔离：非管理员严禁返回底层磁盘原始图片路径
            raw_path_exposed = vr["raw_image_path"] if is_admin else None

            v_det = SlotVersionDetail(
                id=vr["id"],
                slot_id=vr["slot_id"],
                design_id=vr["design_id"],
                version_num=vr["version_num"],
                attempt_num=vr["attempt_num"],
                raw_image_path=raw_path_exposed,
                preview_image_path=vr["preview_image_path"],
                prompt=vr["prompt"],
                prompt_metadata=json.loads(vr["prompt_metadata"]) if vr["prompt_metadata"] else None,
                mock_visual_features=json.loads(vr["mock_visual_features"]) if vr["mock_visual_features"] else None,
                is_mock=bool(vr["is_mock"]),
                upstream_request_id=vr["upstream_request_id"],
                process_status=vr["process_status"],
                is_locked=bool(vr["is_locked"]),
                is_invalidated=bool(vr["is_invalidated"]),
                depends_on_panorama_version_id=vr["depends_on_panorama_version_id"],
                error_message=vr["error_message"],
                is_unknown=bool(vr["is_unknown"]),
                created_at=vr["created_at"]
            )
            versions_list.append(v_det)
            if vr["id"] == c_vid:
                curr_v_detail = v_det

        slots_dict[stype] = SlotDetail(
            id=sid,
            design_id=design_id,
            slot_type=stype,
            current_version_id=c_vid,
            status=sr["status"],
            redo_count=sr["redo_count"],
            technical_retry_count=sr["technical_retry_count"],
            updated_at=sr["updated_at"],
            current_version=curr_v_detail,
            history_versions=versions_list
        )

    spec = json.loads(d_row["structured_spec"]) if d_row["structured_spec"] else {}
    v_features = json.loads(d_row["visual_features"]) if d_row["visual_features"] else None

    return DesignDetail(
        id=d_row["id"],
        batch_id=d_row["batch_id"],
        series_id=d_row["series_id"],
        series_name=spec.get("series_name", d_row["series_id"]),
        design_code=d_row["design_code"],
        is_standby=bool(d_row["is_standby"]),
        status=d_row["status"],
        planning_status=d_row["planning_status"],
        structured_spec=spec,
        visual_features=v_features,
        elimination_reason=d_row["elimination_reason"],
        elimination_comment=d_row["elimination_comment"],
        archived_at=d_row["archived_at"],
        total_redo_count=d_row["total_redo_count"],
        slots=slots_dict,
        created_at=d_row["created_at"],
        updated_at=d_row["updated_at"]
    )

def _maybe_wake_standby_design(conn: sqlite3.Connection, batch_id: str, series_id: Optional[str] = None, source_design_id: Optional[str] = None):
    cur = conn.cursor()
    cur.execute("SELECT target_archived_count, current_archived_count, is_circuit_broken, smart_planning, planning_confirmed, auto_workflow, status FROM batches WHERE id = ?", (batch_id,))
    b_row = cur.fetchone()
    if not b_row:
        return
    if b_row['smart_planning'] and (not b_row['planning_confirmed'] or b_row['status'] != 'active'):
        return
    if b_row['smart_planning'] and not conn.execute("SELECT 1 FROM tasks WHERE batch_id=? AND task_type='generate_panorama' LIMIT 1", (batch_id,)).fetchone():
        return
    target_archived = b_row["target_archived_count"]
    current_archived = b_row["current_archived_count"]
    if b_row["is_circuit_broken"]:
        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'standby_wake_deferred_circuit_broken', 'system', 'system', ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, json.dumps({"reason": "batch circuit broken"})))
        return

    cur.execute("""
        SELECT COUNT(*) as in_flight
        FROM designs
        WHERE batch_id = ? AND is_standby = 0 
          AND status IN ('panorama_generating', 'panorama_review', 'panorama_approved', 'downstream_generating', 'quad_review')
    """, (batch_id,))
    in_flight = cur.fetchone()["in_flight"]
    if b_row['auto_workflow']:
        in_flight += conn.execute("SELECT COUNT(*) AS n FROM planning_jobs p JOIN designs d ON d.id=p.design_id WHERE p.batch_id=? AND d.status='draft' AND p.status IN ('queued','running','failed')", (batch_id,)).fetchone()['n']

    effective_groups = current_archived + in_flight
    if effective_groups >= target_archived:
        return

    query = "SELECT id, series_id, design_code, structured_spec, template_version FROM designs WHERE batch_id = ? AND is_standby = 1 AND status = 'draft' AND planning_status NOT IN ('pending_append', 'ready_append')"
    params = [batch_id]
    if series_id:
        query += " AND series_id = ?"
        params.append(series_id)
    query += " ORDER BY created_at ASC LIMIT 1"

    cur.execute(query, params)
    stby = cur.fetchone()
    if not stby and series_id:
        cur.execute("SELECT id, series_id, design_code, structured_spec, template_version FROM designs WHERE batch_id = ? AND is_standby = 1 AND status = 'draft' AND planning_status NOT IN ('pending_append', 'ready_append') ORDER BY created_at ASC LIMIT 1", (batch_id,))
        stby = cur.fetchone()

    if not stby:
        if b_row['auto_workflow'] and series_id:
            from ..auto_planning_service import queue_replacement
            queue_replacement(conn,batch_id,series_id,source_design_id)
        return

    stby_id = stby["id"]
    stby_code = stby["design_code"]

    cur.execute("SELECT id FROM slots WHERE design_id = ? AND slot_type = 'panorama'", (stby_id,))
    s_row = cur.fetchone()
    if not s_row:
        return
    slot_id = s_row["id"]

    wake_spec = json.loads(stby["structured_spec"])
    wake_prompt, wake_metadata = build_slot_prompt("panorama", wake_spec, stby_code, stby["template_version"])
    v_id = f"ver_{uuid.uuid4().hex[:8]}"
    conn.execute("""
        INSERT INTO slot_versions (
                        id, slot_id, design_id, version_num, attempt_num, prompt, prompt_metadata,
                        mock_visual_features, process_status, template_version
        ) VALUES (?, ?, ?, 1, 1, ?, ?, ?, 'pending', ?)
    """, (
        v_id, slot_id, stby_id, wake_prompt,
        json.dumps(wake_metadata, ensure_ascii=False),
        json.dumps(build_mock_visual_features(wake_spec, "panorama", v_id), ensure_ascii=False),
        stby["template_version"],
    ))

    task_id = f"tsk_{uuid.uuid4().hex[:8]}"
    try:
        reserve_budget(conn, batch_id=batch_id, category="image_generation", amount=1, task_id=task_id, note=f"Wake standby panorama for {stby_code}")
    except (BudgetWarningExceededError, BudgetHardLimitExceededError) as budget_guard:
        # 候补唤醒属于自动补位，绝不能让预算防护回滚人工淘汰决策。
        # 缺口保留，等负责人批准预警或下一次淘汰时再尝试唤醒。
        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'standby_wake_deferred_budget_guard', 'system', 'system', ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, json.dumps({
            "design_id": stby_id, "code": stby_code, "reason": str(budget_guard)
        })))
        return

    conn.execute("""
        INSERT INTO tasks (
            id, batch_id, design_id, slot_id, slot_version_id, task_type, status, idempotency_key
        ) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'queued', ?)
    """, (task_id, batch_id, stby_id, slot_id, v_id, f"wake_stby_{stby_id}"))

    conn.execute("UPDATE slots SET status = 'generating', current_version_id = ? WHERE id = ?", (v_id, slot_id))
    conn.execute("UPDATE designs SET is_standby = 0, status = 'panorama_generating', updated_at = datetime('now', 'localtime') WHERE id = ?", (stby_id,))

    conn.execute("""
        INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
        VALUES (?, ?, 'wake_standby_design', 'system', 'system', ?)
    """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, json.dumps({"design_id": stby_id, "code": stby_code})))

@router.get("/batch/{batch_id}", response_model=List[DesignDetail])
def list_batch_designs(
    batch_id: str,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """获取批次下所有设计组详情（非管理员自动隐藏 raw_image_path）"""
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM designs WHERE batch_id = ? ORDER BY is_standby ASC, created_at ASC", (batch_id,))
        rows = cur.fetchall()
        return [_load_design_detail(conn, r["id"], is_admin=user.is_system_admin) for r in rows]

@router.get("/{design_id}", response_model=DesignDetail)
def get_design_by_id(
    design_id: str,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    with get_db(immediate=False) as conn:
        return _load_design_detail(conn, design_id, is_admin=user.is_system_admin)

@router.post("/{design_id}/review_panorama")
def review_panorama(
    design_id: str,
    req: PanoramaReviewRequest,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """
    人工全景初筛：
    - 状态约束：已归档或已淘汰设计严禁审核
    - approve: 锁定版本，提取特征，状态更新为 panorama_approved
    - redo: 选标准化原因，重做次数校验（单槽位<=2，整组<=4，超限需负责人特批），派发新全景
    - eliminate: 淘汰设计，计算有效组数，若有缺口自动唤醒候补
    """
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT batch_id, status, total_redo_count, series_id, design_code, structured_spec, template_version FROM designs WHERE id = ?", (design_id,))
        d_row = cur.fetchone()
        if not d_row:
            raise HTTPException(status_code=404, detail="设计不存在")
        
        curr_status = d_row["status"]
        if curr_status in ("archived", "eliminated"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"设计已处于 '{curr_status}' 终态，严禁重复操作！"
            )
        if req.action == "approve" and curr_status != "panorama_review":
            raise HTTPException(status_code=409, detail=f"全景当前设计状态为 '{curr_status}'，只能审核 panorama_review 状态")

        batch_id = d_row["batch_id"]
        series_id = d_row["series_id"]
        design_code = d_row["design_code"]
        total_redo = d_row["total_redo_count"]

        cur.execute("SELECT id, current_version_id, redo_count, status FROM slots WHERE design_id = ? AND slot_type = 'panorama'", (design_id,))
        slot_row = cur.fetchone()
        if not slot_row:
            raise HTTPException(status_code=404, detail="全景槽位不存在")
        slot_id = slot_row["id"]
        curr_vid = slot_row["current_version_id"]
        slot_redo = slot_row["redo_count"]

        if req.action == "approve" and slot_row["status"] != "ready":
            raise HTTPException(status_code=400, detail=f"全景槽位当前状态为 '{slot_row['status']}'，禁止通过审核")

        if curr_vid != req.expected_version_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"版本已发生变更 (当前: {curr_vid}, 传入: {req.expected_version_id})，请刷新页面重新审核"
            )

        if req.action == "approve":
            if not curr_vid:
                raise HTTPException(status_code=400, detail="全景当前版本为空，禁止通过审核")
            cur.execute("""
                SELECT id, slot_id, process_status, is_unknown, is_invalidated,
                       raw_image_path, preview_image_path
                FROM slot_versions WHERE id = ?
            """, (curr_vid,))
            version_row = cur.fetchone()
            if not version_row or version_row["id"] != curr_vid or version_row["slot_id"] != slot_id:
                raise HTTPException(status_code=400, detail="全景当前版本不存在或不属于该槽位，禁止通过审核")
            if version_row["process_status"] != "completed":
                raise HTTPException(status_code=400, detail=f"全景版本处理状态为 '{version_row['process_status']}'，禁止通过审核")
            if version_row["is_unknown"]:
                raise HTTPException(status_code=400, detail="全景版本为结果未知状态，禁止通过审核")
            if version_row["is_invalidated"]:
                raise HTTPException(status_code=400, detail="全景版本已失效，禁止通过审核")

            if not version_row["raw_image_path"] or not version_row["preview_image_path"]:
                raise HTTPException(status_code=400, detail="全景原图或水印预览缺失，禁止通过审核")
            raw_file = RAW_IMAGES_DIR / version_row["raw_image_path"]
            preview_file = PREVIEW_IMAGES_DIR / version_row["preview_image_path"]
            if not raw_file.exists() or not preview_file.exists():
                raise HTTPException(status_code=400, detail="全景原图或水印预览文件丢失，禁止通过审核")
            try:
                with Image.open(raw_file) as raw_image:
                    raw_image.verify()
                with Image.open(raw_file) as watermark_probe:
                    watermark_probe.load()
                    apply_watermark(watermark_probe)
                with Image.open(preview_file) as preview_image:
                    preview_image.verify()
                with Image.open(preview_file) as preview_probe:
                    preview_probe.load()
            except Exception as image_error:
                logger.exception("Panorama approval image validation failed: %s", image_error)
                raise HTTPException(status_code=400, detail=f"全景原图或水印预览损坏，禁止通过审核: {image_error}")

            conn.execute("UPDATE slot_versions SET is_locked = 1 WHERE id = ?", (curr_vid,))
            conn.execute("UPDATE slots SET status = 'approved', updated_at = datetime('now', 'localtime') WHERE id = ?", (slot_id,))
            
            extracted_features = {
                "source_panorama_version_id": curr_vid,
                "series_id": series_id,
                "approved_timestamp": uuid.uuid4().hex[:6],
                "visual_harmony_score": 0.95,
                "extracted_bed_style": "Confirmed standard luxury hotel design"
            }
            conn.execute("""
                UPDATE designs 
                SET status = 'panorama_approved', visual_features = ?, updated_at = datetime('now', 'localtime')
                WHERE id = ?
            """, (json.dumps(extracted_features, ensure_ascii=False), design_id))

            # 记录视觉分析模型成本流水
            vis_bgt_id = f"bgt_vis_{uuid.uuid4().hex[:8]}"
            conn.execute("""
                INSERT INTO budget_ledger (id, batch_id, category, delta_reserved, delta_used, delta_unsettled, status, note)
                VALUES (?, ?, 'vision_model', 0, 1, 0, 'settled', ?)
            """, (vis_bgt_id, batch_id, f"Vision feature extraction for {design_code}"))

            conn.execute("""
                INSERT INTO reviews (id, design_id, slot_id, slot_version_id, review_stage, action, reviewer_name, reviewer_role)
                VALUES (?, ?, ?, ?, 'panorama_first_pass', 'approve', ?, ?)
            """, (f"rev_{uuid.uuid4().hex[:8]}", design_id, slot_id, curr_vid, user.user_id, user.role))

        elif req.action == "redo":
            if not req.reason_category:
                raise HTTPException(status_code=400, detail="重做必须选择标准化原因分类")
            if curr_status in ("panorama_approved", "downstream_generating", "quad_review"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="全景已通过人工初筛锁定，禁止再走初筛重做；更换全景必须由负责人通过全景替换接口操作并使旧下游失效。"
                )
            circuit_row = cur.execute("SELECT is_circuit_broken FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if circuit_row and circuit_row["is_circuit_broken"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="批次处于熔断状态，禁止派发新生成任务；请先由负责人恢复熔断。"
                )

            if (slot_redo >= DEFAULT_REDO_SLOT_LIMIT or total_redo >= DEFAULT_REDO_DESIGN_LIMIT):
                if not (req.leader_override and user.is_leader):
                    raise HTTPException(
                        status_code=400,
                        detail=f"已超出重做限制 (单槽位:{slot_redo}/{DEFAULT_REDO_SLOT_LIMIT}, 整组:{total_redo}/{DEFAULT_REDO_DESIGN_LIMIT})，需负责人明确特批后方可继续！"
                    )

            conn.execute("UPDATE slots SET redo_count = redo_count + 1 WHERE id = ?", (slot_id,))
            conn.execute("UPDATE designs SET total_redo_count = total_redo_count + 1 WHERE id = ?", (design_id,))

            cur.execute("SELECT MAX(version_num) as mv FROM slot_versions WHERE slot_id = ?", (slot_id,))
            max_v = cur.fetchone()["mv"] or 1
            new_vid = f"ver_{uuid.uuid4().hex[:8]}"
            redo_spec = json.loads(d_row["structured_spec"])
            redo_prompt, redo_metadata = build_slot_prompt("panorama", redo_spec, design_code, d_row["template_version"])
            redo_metadata["redo_reason_category"] = req.reason_category
            redo_metadata["detail_comment"] = req.detail_comment

            conn.execute("""
                INSERT INTO slot_versions (
                    id, slot_id, design_id, version_num, attempt_num, prompt, prompt_metadata,
                    mock_visual_features, process_status, template_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (
                new_vid, slot_id, design_id, max_v + 1, max_v + 1, redo_prompt,
                json.dumps(redo_metadata, ensure_ascii=False),
                json.dumps(build_mock_visual_features(redo_spec, "panorama", new_vid), ensure_ascii=False),
                d_row["template_version"],
            ))

            task_id = f"tsk_{uuid.uuid4().hex[:8]}"
            reserve_budget(conn, batch_id=batch_id, category="image_generation", amount=1, task_id=task_id, note=f"Redo panorama for {design_code}")

            conn.execute("""
                INSERT INTO tasks (
                    id, batch_id, design_id, slot_id, slot_version_id, task_type, status, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, 'redo_slot', 'queued', ?)
            """, (task_id, batch_id, design_id, slot_id, new_vid, f"redo_pano_{new_vid}"))

            conn.execute("UPDATE slots SET status = 'generating', current_version_id = ? WHERE id = ?", (new_vid, slot_id))
            conn.execute("UPDATE designs SET status = 'panorama_generating' WHERE id = ?", (design_id,))

            conn.execute("""
                INSERT INTO reviews (id, design_id, slot_id, slot_version_id, review_stage, action, reason_category, detail_comment, reviewer_name, reviewer_role)
                VALUES (?, ?, ?, ?, 'panorama_first_pass', 'redo', ?, ?, ?, ?)
            """, (f"rev_{uuid.uuid4().hex[:8]}", design_id, slot_id, curr_vid, req.reason_category, req.detail_comment, user.user_id, user.role))

        elif req.action == "eliminate":
            conn.execute("""
                UPDATE designs 
                SET status = 'eliminated', elimination_reason = ?, elimination_comment = ?, updated_at = datetime('now', 'localtime')
                WHERE id = ?
            """, (req.reason_category or "全景初筛淘汰", req.detail_comment, design_id))
            conn.execute("UPDATE slots SET status = 'invalidated' WHERE design_id = ?", (design_id,))

            conn.execute("""
                INSERT INTO reviews (id, design_id, slot_id, slot_version_id, review_stage, action, reason_category, detail_comment, reviewer_name, reviewer_role)
                VALUES (?, ?, ?, ?, 'panorama_first_pass', 'eliminate', ?, ?, ?, ?)
            """, (f"rev_{uuid.uuid4().hex[:8]}", design_id, slot_id, curr_vid, req.reason_category, req.detail_comment, user.user_id, user.role))

            _maybe_wake_standby_design(conn, batch_id, series_id, design_id)

        else:
            raise HTTPException(status_code=400, detail="未知的审核动作")

        return _load_design_detail(conn, design_id, is_admin=user.is_system_admin)

@router.post("/{design_id}/start_downstream")
def start_downstream(
    design_id: str,
    expected_panorama_version_id: Optional[str] = None,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """
    人工启动下游三图：
    - 状态约束：必须全景已通过 (status == 'panorama_approved')
    - 依序分别启动：white_isolated, twin_bed, nightstand_closeup
    """
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT batch_id, status, design_code, structured_spec, template_version FROM designs WHERE id = ?", (design_id,))
        d_row = cur.fetchone()
        if not d_row:
            raise HTTPException(status_code=404, detail="设计不存在")
        # Unknown results are a hard stop even on an otherwise idempotent retry;
        # never let a bulk retry hide an unresolved upstream charge/result.
        unknown = cur.execute("""
            SELECT 1 FROM tasks t
            JOIN slot_versions sv ON sv.id=t.slot_version_id
            WHERE t.design_id=? AND (t.status='unknown' OR sv.process_status='unknown_pending_confirmation' OR sv.is_unknown=1)
            LIMIT 1
        """, (design_id,)).fetchone()
        if unknown:
            raise HTTPException(status_code=409, detail="设计存在结果未知任务，必须先人工处置")

        # 重复启动下游幂等保护：已在生成或已四图就绪，幂等直接返回，严禁重复扣除预算
        if d_row["status"] in ("downstream_generating", "quad_review"):
            return _load_design_detail(conn, design_id, is_admin=user.is_system_admin)

        if d_row["status"] != "panorama_approved":
            raise HTTPException(status_code=400, detail=f"必须在全景初筛通过后才允许启动下游三图 (当前状态: {d_row['status']})")
        
        batch_id = d_row["batch_id"]
        code = d_row["design_code"]

        circuit_row = cur.execute("SELECT is_circuit_broken FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if circuit_row and circuit_row["is_circuit_broken"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="批次处于熔断状态，禁止派发下游生成任务；请先由负责人恢复熔断。"
            )

        cur.execute("SELECT current_version_id, status FROM slots WHERE design_id = ? AND slot_type = 'panorama'", (design_id,))
        p_row = cur.fetchone()
        if not p_row or p_row["status"] != "approved" or not p_row["current_version_id"]:
            raise HTTPException(status_code=400, detail="全景当前版本异常或未通过，无法作为下游依据")
        pano_vid = p_row["current_version_id"]
        if expected_panorama_version_id and expected_panorama_version_id != pano_vid:
            raise HTTPException(status_code=409, detail='全景版本已变化，请重新审核')

        downstream_slots = ["white_isolated", "twin_bed", "nightstand_closeup"]
        for stype in downstream_slots:
            cur.execute("SELECT id FROM slots WHERE design_id = ? AND slot_type = ?", (design_id, stype))
            s_row = cur.fetchone()
            if not s_row:
                continue
            slot_id = s_row["id"]

            # 重启下游（全景替换后）不得复用旧版本号，否则历史版本编号重复。
            cur.execute("SELECT COALESCE(MAX(version_num), 0) AS mv FROM slot_versions WHERE slot_id = ?", (slot_id,))
            next_num = (cur.fetchone()["mv"] or 0) + 1

            v_id = f"ver_{uuid.uuid4().hex[:8]}"
            downstream_spec = json.loads(d_row["structured_spec"])
            downstream_prompt, downstream_metadata = build_slot_prompt(stype, downstream_spec, code, d_row["template_version"], pano_vid)
            conn.execute("""
                INSERT INTO slot_versions (
                        id, slot_id, design_id, version_num, attempt_num, prompt, prompt_metadata,
                        mock_visual_features, process_status, depends_on_panorama_version_id, template_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (
                v_id, slot_id, design_id, next_num, next_num, downstream_prompt,
                json.dumps(downstream_metadata, ensure_ascii=False),
                json.dumps(build_mock_visual_features(downstream_spec, stype, v_id), ensure_ascii=False),
                pano_vid, d_row["template_version"],
            ))

            task_id = f"tsk_{uuid.uuid4().hex[:8]}"
            reserve_budget(conn, batch_id=batch_id, category="image_generation", amount=1, task_id=task_id, note=f"Downstream {stype} for {code}")

            conn.execute("""
                INSERT INTO tasks (
                    id, batch_id, design_id, slot_id, slot_version_id, task_type, status, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, 'generate_downstream', 'queued', ?)
            """, (task_id, batch_id, design_id, slot_id, v_id, f"downstream_{stype}_{v_id}"))

            conn.execute("UPDATE slots SET status = 'generating', current_version_id = ? WHERE id = ?", (v_id, slot_id))

        conn.execute("UPDATE designs SET status = 'downstream_generating', updated_at = datetime('now', 'localtime') WHERE id = ?", (design_id,))

        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'start_downstream', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, user.user_id, user.role, json.dumps({"design_id": design_id, "based_on_panorama": pano_vid})))

        return _load_design_detail(conn, design_id, is_admin=user.is_system_admin)

@router.post("/{design_id}/review_quad")
def review_quad(
    design_id: str,
    req: QuadReviewRequest,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """
    四图终审必须在 BEGIN IMMEDIATE 事务中校验人工所见版本快照。
    任何当前指针、处理状态、依赖、未知任务或图像变化都会阻断终审，不允许静默换图归档。
    """
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT batch_id, status, series_id, design_code FROM designs WHERE id = ?", (design_id,))
        d_row = cur.fetchone()
        if not d_row:
            raise HTTPException(status_code=404, detail="设计不存在")

        batch_id = d_row["batch_id"]
        series_id = d_row["series_id"]
        code = d_row["design_code"]
        design_status = d_row["status"]

        if design_status == "archived" and req.action == "archive":
            return _load_design_detail(conn, design_id, is_admin=user.is_system_admin)
        if design_status in ("archived", "eliminated"):
            raise HTTPException(status_code=400, detail=f"设计已处于 '{design_status}' 状态，不可重复终审！")
        if req.action not in ("archive", "eliminate"):
            raise HTTPException(status_code=400, detail="未知的终审动作")

        required_types = {"panorama", "white_isolated", "twin_bed", "nightstand_closeup"}
        expected_map = req.expected_slot_versions
        if not isinstance(expected_map, dict) or set(expected_map) != required_types:
            raise HTTPException(status_code=400, detail="终审必须提交四个槽位的人工审核版本快照")

        cur.execute("""
            SELECT s.id as slot_id, s.slot_type, s.status as slot_status,
                   sv.id as vid, sv.slot_id as version_slot_id, sv.process_status,
                   sv.is_unknown, sv.is_invalidated, sv.raw_image_path, sv.preview_image_path,
                   sv.depends_on_panorama_version_id
            FROM slots s
            LEFT JOIN slot_versions sv ON s.current_version_id = sv.id
            WHERE s.design_id = ?
        """, (design_id,))
        rows_by_type = {row["slot_type"]: row for row in cur.fetchall()}
        if set(rows_by_type) != required_types:
            raise HTTPException(status_code=400, detail=f"四图槽位不完整，缺少: {required_types - set(rows_by_type)}")

        for stype in required_types:
            row = rows_by_type[stype]
            seen_vid = expected_map.get(stype)
            if not row["vid"] or seen_vid != row["vid"]:
                raise HTTPException(status_code=409, detail=f"槽位 {stype} 版本已变更，请刷新后重新终审")
            if row["version_slot_id"] != row["slot_id"]:
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 当前版本归属异常，禁止终审")
            if row["is_invalidated"]:
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 的当前版本已被标记为依赖失效，禁止归档！需重新生成。")
            if row["slot_status"] not in ("ready", "approved"):
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 状态为 {row['slot_status']}，禁止终审")
            if row["process_status"] != "completed":
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 当前版本处理状态为 {row['process_status']}，禁止终审")
            if row["is_unknown"]:
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 当前版本为结果未知状态，禁止终审")

        active_pano = rows_by_type["panorama"]
        active_pano_vid = active_pano["vid"]
        if active_pano["slot_status"] != "approved":
            raise HTTPException(status_code=400, detail="全景当前版本未通过人工初筛，禁止四图终审")
        for stype in ("white_isolated", "twin_bed", "nightstand_closeup"):
            row = rows_by_type[stype]
            if row["depends_on_panorama_version_id"] != active_pano_vid:
                raise HTTPException(
                    status_code=400,
                    detail=f"槽位 {stype} 依赖的全景版本 ({row['depends_on_panorama_version_id']}) 与当前全景 ({active_pano_vid}) 不匹配，禁止终审！"
                )

        if design_status != "quad_review":
            raise HTTPException(status_code=400, detail=f"必须在四图就绪 (quad_review) 后才允许终审，当前状态: {design_status}")

        cur.execute("""
            SELECT COUNT(*) AS unknown_count
            FROM tasks t
            JOIN slot_versions sv ON t.slot_version_id = sv.id
            WHERE t.design_id = ? AND (t.status = 'unknown' OR sv.process_status = 'unknown_pending_confirmation')
        """, (design_id,))
        if cur.fetchone()["unknown_count"]:
            raise HTTPException(status_code=400, detail="设计仍存在结果未知任务，必须先人工处置并保持批次保护")

        if req.action == "eliminate":
            conn.execute("""
                UPDATE designs
                SET status = 'eliminated', elimination_reason = '四图终审淘汰', elimination_comment = ?,
                    updated_at = datetime('now', 'localtime')
                WHERE id = ?
            """, (req.detail_comment, design_id))
            conn.execute("UPDATE slots SET status = 'invalidated' WHERE design_id = ?", (design_id,))
            conn.execute("""
                INSERT INTO reviews (id, design_id, review_stage, action, detail_comment, reviewer_name, reviewer_role)
                VALUES (?, ?, 'quad_final_pass', 'eliminate', ?, ?, ?)
            """, (f"rev_{uuid.uuid4().hex[:8]}", design_id, req.detail_comment, user.user_id, user.role))
            _maybe_wake_standby_design(conn, batch_id, series_id, design_id)
            return _load_design_detail(conn, design_id, is_admin=user.is_system_admin)

        b_row = cur.execute("SELECT is_circuit_broken, status FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if b_row and (b_row["is_circuit_broken"] or b_row["status"] == "paused"):
            raise HTTPException(status_code=400, detail="批次处于暂停或熔断状态，禁止执行归档操作！")

        wm_cfg = DEFAULT_WATERMARK
        wm_row = cur.execute("""
            SELECT main_text, repeat_text, opacity, font_size, position
            FROM watermark_configs WHERE batch_id = ?
        """, (batch_id,)).fetchone()
        if wm_row:
            wm_cfg = dict(wm_row)

        for stype in required_types:
            row = rows_by_type[stype]
            if not row["raw_image_path"]:
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 原图路径为空，禁止归档")
            raw_file = RAW_IMAGES_DIR / row["raw_image_path"]
            if not raw_file.exists():
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 对应原图文件丢失，禁止归档")
            try:
                with Image.open(raw_file) as archive_image:
                    archive_image.verify()
                with Image.open(raw_file) as watermark_source:
                    watermarked = apply_watermark(
                        watermark_source,
                        main_text=wm_cfg.get("main_text", DEFAULT_WATERMARK["main_text"]),
                        repeat_text=wm_cfg.get("repeat_text", DEFAULT_WATERMARK["repeat_text"]),
                        opacity=float(wm_cfg.get("opacity", DEFAULT_WATERMARK["opacity"])),
                        font_size=int(wm_cfg.get("font_size", DEFAULT_WATERMARK["font_size"])),
                        position=wm_cfg.get("position", DEFAULT_WATERMARK["position"]),
                    )
                    output = io.BytesIO()
                    watermarked.save(output, format="PNG")
                    if not output.getvalue():
                        raise RuntimeError("watermark output empty")
            except Exception:
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 原图损坏或最终水印预览无法生成，禁止归档")

            if not row["preview_image_path"]:
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 预览图缺失，禁止归档")
            preview_file = PREVIEW_IMAGES_DIR / row["preview_image_path"]
            if not preview_file.exists():
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 预览图文件丢失，禁止归档")
            try:
                with Image.open(preview_file) as preview_image:
                    preview_image.verify()
            except Exception:
                raise HTTPException(status_code=400, detail=f"槽位 {stype} 预览图损坏或格式无效，禁止归档")

        for stype in required_types:
            row = rows_by_type[stype]
            conn.execute("UPDATE slot_versions SET is_locked = 1 WHERE id = ?", (row["vid"],))
            conn.execute("UPDATE slots SET status = 'approved' WHERE id = ?", (row["slot_id"],))

        conn.execute("""
            UPDATE designs
            SET status = 'archived', archived_at = datetime('now', 'localtime'), updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (design_id,))
        conn.execute("UPDATE batches SET current_archived_count = current_archived_count + 1 WHERE id = ?", (batch_id,))

        b_curr = cur.execute(
            "SELECT target_archived_count, current_archived_count FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if b_curr["current_archived_count"] >= b_curr["target_archived_count"]:
            conn.execute("UPDATE batches SET status = 'completed' WHERE id = ?", (batch_id,))

        conn.execute("""
            INSERT INTO reviews (id, design_id, review_stage, action, detail_comment, reviewer_name, reviewer_role)
            VALUES (?, ?, 'quad_final_pass', 'archive', ?, ?, ?)
        """, (f"rev_{uuid.uuid4().hex[:8]}", design_id, req.detail_comment or "四图终审合格归档", user.user_id, user.role))
        return _load_design_detail(conn, design_id, is_admin=user.is_system_admin)
