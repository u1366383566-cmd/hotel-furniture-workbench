import json
import uuid
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, status
from ..database import get_db
from ..models import SlotRedoRequest, SlotVersionSelectRequest, PanoramaReplaceRequest
from ..security import UserContext, get_current_user, require_roles
from ..budget_service import reserve_budget
from ..planning_service import build_mock_visual_features, build_slot_prompt
from ..config import DEFAULT_REDO_SLOT_LIMIT, DEFAULT_REDO_DESIGN_LIMIT

router = APIRouter(prefix="/api/slots", tags=["Slots"])

@router.post("/{slot_id}/redo")
def redo_slot(
    slot_id: str,
    req: SlotRedoRequest,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """
    单张槽位重做（不级联重做其他槽位）：
    - 状态约束：已归档 (archived) 或已淘汰 (eliminated) 的设计严禁重做
    - 依赖约束：下游槽位重做前，全景必须处于有效且已通过 (approved) 状态
    - 检查单槽位重做上限 (<=2) 与整组重做上限 (<=4)，超限需负责人特批
    - 预占预算，派发生成任务
    - 保留旧版本，以备之后回选
    """
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id, s.design_id, s.slot_type, s.current_version_id, s.redo_count,
                   d.batch_id, d.design_code, d.total_redo_count, d.status as design_status,
                   d.structured_spec, d.template_version, d.series_id
            FROM slots s
            JOIN designs d ON s.design_id = d.id
            WHERE s.id = ?
        """, (slot_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="槽位不存在")

        # 状态检查：已归档或已淘汰不可修改
        design_status = row["design_status"]
        if design_status in ("archived", "eliminated"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"设计当前处于 '{design_status}' 终态，已封存快照或已淘汰，严禁发起重做！"
            )

        slot_type = row["slot_type"]
        design_id = row["design_id"]
        if req.custom_prompt and slot_type == 'panorama':
            raise HTTPException(400, '仅白底图支持自定义提示词重做')

        # 全景锁定红线：全景一旦通过初筛锁定，重做必须走负责人替换接口，
        # 避免下游依赖基准被静默替换且不触发级联失效。
        if (
            slot_type == "panorama"
            and design_status in ("panorama_approved", "downstream_generating", "quad_review")
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="全景已通过人工初筛锁定，禁止槽位重做；更换全景必须由负责人通过全景替换接口操作并使旧下游失效。"
            )

        circuit_row = cur.execute("SELECT is_circuit_broken FROM batches WHERE id = ?", (row["batch_id"],)).fetchone()
        if circuit_row and circuit_row["is_circuit_broken"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="批次处于熔断状态，禁止派发新生成任务；请先由负责人恢复熔断。"
            )

        # 下游重做依赖约束：全景必须处于 approved 状态
        if slot_type != "panorama":
            cur.execute("SELECT status, current_version_id FROM slots WHERE design_id = ? AND slot_type = 'panorama'", (design_id,))
            p_row = cur.fetchone()
            if not p_row or p_row["status"] != "approved" or not p_row["current_version_id"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="下游槽位重做依赖有效全景基准，当前全景未初筛通过或处于失效状态，请先完成全景审核！"
                )
            depends_on = p_row["current_version_id"]
        else:
            depends_on = None

        curr_vid = row["current_version_id"]
        if curr_vid != req.expected_version_id:
            raise HTTPException(status_code=409, detail="槽位版本已更新，请刷新重试")

        slot_redo = row["redo_count"]
        total_redo = row["total_redo_count"]
        batch_id = row["batch_id"]
        design_code = row["design_code"]

        # 上限检查
        if slot_redo >= DEFAULT_REDO_SLOT_LIMIT or total_redo >= DEFAULT_REDO_DESIGN_LIMIT:
            if not (req.leader_override and user.is_leader):
                raise HTTPException(
                    status_code=400,
                    detail=f"已超出重做限制 (槽位:{slot_redo}/{DEFAULT_REDO_SLOT_LIMIT}, 整组:{total_redo}/{DEFAULT_REDO_DESIGN_LIMIT})，需负责人明确特批"
                )

        # 递增重做计数
        conn.execute("UPDATE slots SET redo_count = redo_count + 1 WHERE id = ?", (slot_id,))
        conn.execute("UPDATE designs SET total_redo_count = total_redo_count + 1 WHERE id = ?", (design_id,))

        cur.execute("SELECT MAX(version_num) as mv FROM slot_versions WHERE slot_id = ?", (slot_id,))
        max_v = cur.fetchone()["mv"] or 1
        new_vid = f"ver_{uuid.uuid4().hex[:8]}"
        redo_spec = json.loads(row["structured_spec"])
        if slot_type == 'panorama':
            from ..planning_service import redo_panorama_view
            view, changed = redo_panorama_view(conn, redo_spec, design_id, batch_id, row['series_id'], curr_vid, req.detail_comment)
            if view:
                redo_spec['panorama_view'] = view
        redo_prompt, redo_metadata = build_slot_prompt(slot_type, redo_spec, design_code, row["template_version"], depends_on)
        if slot_type == 'panorama' and changed:
            redo_metadata['panorama_view_changed'] = True
        redo_metadata["redo_reason_category"] = req.reason_category
        redo_metadata["detail_comment"] = req.detail_comment
        if req.custom_prompt and req.custom_prompt.strip():
            redo_metadata['custom_prompt'] = req.custom_prompt.strip()
            redo_metadata['prompt_author'] = user.user_id
            redo_metadata['prompt_author_role'] = user.role
        if slot_type == 'white_isolated':
            redo_metadata['reference_version_id'] = depends_on
        elif slot_type in ('twin_bed', 'nightstand_closeup'):
            reference = conn.execute("SELECT s.current_version_id,sv.process_status FROM slots s LEFT JOIN slot_versions sv ON sv.id=s.current_version_id WHERE s.design_id=? AND s.slot_type='white_isolated'", (design_id,)).fetchone()
            if not reference or not reference['current_version_id'] or reference['process_status'] != 'completed':
                raise HTTPException(409, '请先完成白底组合图，再重做此图')
            redo_metadata['reference_version_id'] = reference['current_version_id']

        conn.execute("""
            INSERT INTO slot_versions (
                    id, slot_id, design_id, version_num, attempt_num, prompt, prompt_metadata,
                    mock_visual_features, process_status, depends_on_panorama_version_id, template_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (
                new_vid, slot_id, design_id, max_v + 1, max_v + 1, redo_prompt,
                json.dumps(redo_metadata, ensure_ascii=False),
                json.dumps(build_mock_visual_features(redo_spec, slot_type, new_vid), ensure_ascii=False),
                depends_on, row["template_version"],
            ))

        # 预占预算
        task_id = f"tsk_{uuid.uuid4().hex[:8]}"
        reserve_budget(conn, batch_id=batch_id, category="image_generation", amount=1, task_id=task_id, note=f"Redo {slot_type} for {design_code}")

        conn.execute("""
            INSERT INTO tasks (
                id, batch_id, design_id, slot_id, slot_version_id, task_type, status, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, 'redo_slot', 'queued', ?)
        """, (task_id, batch_id, design_id, slot_id, new_vid, f"redo_slot_{new_vid}"))

        conn.execute("UPDATE slots SET status = 'generating', current_version_id = ? WHERE id = ?", (new_vid, slot_id))

        if slot_type == 'white_isolated':
            old_version = conn.execute('SELECT process_status FROM slot_versions WHERE id=? AND slot_id=?', (curr_vid, slot_id)).fetchone()
            if old_version and old_version['process_status'] == 'failed':
                conn.execute("""UPDATE slot_versions SET prompt_metadata=json_set(prompt_metadata,'$.reference_version_id',?)
                    WHERE design_id=? AND process_status='pending' AND json_valid(prompt_metadata)
                    AND json_extract(prompt_metadata,'$.reference_version_id')=?
                    AND slot_id IN (SELECT id FROM slots WHERE design_id=? AND slot_type IN ('twin_bed','nightstand_closeup'))
                    AND EXISTS (SELECT 1 FROM tasks t WHERE t.slot_version_id=slot_versions.id AND t.status='queued')""",
                    (new_vid,design_id,curr_vid,design_id))

        # 记录审核
        conn.execute("""
            INSERT INTO reviews (id, design_id, slot_id, slot_version_id, review_stage, action, reason_category, detail_comment, reviewer_name, reviewer_role)
            VALUES (?, ?, ?, ?, 'slot_redo', 'redo', ?, ?, ?, ?)
        """, (f"rev_{uuid.uuid4().hex[:8]}", design_id, slot_id, curr_vid, req.reason_category, req.detail_comment, user.user_id, user.role))

    return {"message": "已派发单张重做任务", "new_version_id": new_vid, "task_id": task_id}

@router.post("/{slot_id}/select_version")
def select_version(
    slot_id: str,
    req: SlotVersionSelectRequest,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """
    回选历史有效旧版本作为当前使用版本：
    - 状态约束：已归档或已淘汰设计严禁修改版本
    - 架构红线：全景槽位严禁通过普通 select_version 偷换，必须由负责人通过 /replace_panorama 审批并使下游失效
    - 依赖约束：下游槽位回选的版本，其依赖的全景版本必须与当前全景锁定版本严格一致且未失效
    """
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id, s.slot_type, s.design_id, d.status as design_status
            FROM slots s
            JOIN designs d ON s.design_id = d.id
            WHERE s.id = ?
        """, (slot_id,))
        s_row = cur.fetchone()
        if not s_row:
            raise HTTPException(status_code=404, detail="槽位不存在")

        if s_row["design_status"] in ("archived", "eliminated"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"设计已处于 '{s_row['design_status']}' 状态，严禁修改版本！"
            )

        if s_row["slot_type"] == "panorama":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="全景版本切换直接影响下游三图的视觉一致性基准，严禁直接回选！必须由负责人通过全景替换接口 (/api/slots/designs/{id}/replace_panorama) 明确操作并使旧下游失效。"
            )

        # 验证要回选的下游版本
        cur.execute("SELECT id, is_invalidated, process_status, depends_on_panorama_version_id FROM slot_versions WHERE id = ? AND slot_id = ?", (req.version_id, slot_id))
        v_row = cur.fetchone()
        if not v_row:
            raise HTTPException(status_code=404, detail="指定的版本不存在于该槽位")
        if v_row["process_status"] != "completed":
            raise HTTPException(status_code=400, detail="只能回选已成功生成的版本")
        if v_row["is_invalidated"]:
            raise HTTPException(status_code=400, detail="该版本已被标记为依赖失效，不可选为当前版本")

        # 检查是否依赖当前最新的全景版本
        cur.execute("SELECT current_version_id FROM slots WHERE design_id = ? AND slot_type = 'panorama'", (s_row["design_id"],))
        p_row = cur.fetchone()
        active_pano_vid = p_row["current_version_id"] if p_row else None
        if v_row["depends_on_panorama_version_id"] != active_pano_vid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"该版本依赖的历史全景 ({v_row['depends_on_panorama_version_id']}) 与当前全景基准 ({active_pano_vid}) 不一致，禁止回选！"
            )

        conn.execute("UPDATE slots SET current_version_id = ?, status = 'ready', updated_at = datetime('now', 'localtime') WHERE id = ?", (req.version_id, slot_id))
        
        # 审计日志
        conn.execute("""
            INSERT INTO audit_logs (id, action, actor_name, actor_role, detail)
            VALUES (?, 'select_version', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", user.user_id, user.role, json.dumps({"slot_id": slot_id, "selected_version_id": req.version_id})))

    return {"message": "版本切换成功", "slot_id": slot_id, "current_version_id": req.version_id}

@router.post("/designs/{design_id}/replace_panorama")
def replace_panorama(
    design_id: str,
    req: PanoramaReplaceRequest,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """
    全景替换与下游依赖失效（仅负责人/管理员操作）：
    - 状态约束：已归档或已淘汰设计严禁替换全景
    - 依赖级联失效：任何未依赖新全景版本的下游版本一律标记为 is_invalidated = 1
    - 下游槽位标记为 invalidated，禁止与新全景混合归档，必须重新生成受影响的下游
    """
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM designs WHERE id = ?", (design_id,))
        d_row = cur.fetchone()
        if not d_row:
            raise HTTPException(status_code=404, detail="设计不存在")
        if d_row["status"] in ("archived", "eliminated"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"设计已处于 '{d_row['status']}' 状态，已封存归档，严禁替换全景！"
            )

        cur.execute("SELECT id, current_version_id FROM slots WHERE design_id = ? AND slot_type = 'panorama'", (design_id,))
        p_row = cur.fetchone()
        if not p_row:
            raise HTTPException(status_code=404, detail="全景槽位不存在")
        old_pano_vid = p_row["current_version_id"]
        slot_id = p_row["id"]

        # 检查新版本是否有效
        cur.execute("SELECT id, process_status FROM slot_versions WHERE id = ? AND slot_id = ?", (req.new_panorama_version_id, slot_id))
        new_v_row = cur.fetchone()
        if not new_v_row or new_v_row["process_status"] != "completed":
            raise HTTPException(status_code=400, detail="所选新全景版本未完成或不存在")

        # 将新全景设为当前版本并锁定
        conn.execute("UPDATE slots SET current_version_id = ?, status = 'approved', updated_at = datetime('now', 'localtime') WHERE id = ?", (req.new_panorama_version_id, slot_id))
        conn.execute("UPDATE slot_versions SET is_locked = 1 WHERE id = ?", (req.new_panorama_version_id,))

        # 级联失效非当前新全景版本的所有下游版本
        conn.execute("""
            UPDATE slot_versions 
            SET is_invalidated = 1 
            WHERE design_id = ? AND slot_id != ? AND (depends_on_panorama_version_id != ? OR depends_on_panorama_version_id IS NULL)
        """, (design_id, slot_id, req.new_panorama_version_id))

        # 将下游槽位状态更新为 invalidated
        conn.execute("""
            UPDATE slots 
            SET status = 'invalidated', updated_at = datetime('now', 'localtime')
            WHERE design_id = ? AND slot_type != 'panorama'
        """, (design_id,))

        # 设计状态重置为全景已通过，等待重新启动下游
        conn.execute("""
            UPDATE designs 
            SET status = 'panorama_approved', updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (design_id,))

        # 记录审计日志
        conn.execute("""
            INSERT INTO audit_logs (id, action, actor_name, actor_role, detail)
            VALUES (?, 'replace_panorama_invalidated_downstream', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", user.user_id, user.role, json.dumps({
            "design_id": design_id,
            "old_panorama": old_pano_vid,
            "new_panorama": req.new_panorama_version_id,
            "reason": req.reason
        })))

    return {
        "message": "全景替换完成，已自动失效旧下游版本，下游槽位已标记为 invalidated，需重新生成下游",
        "active_panorama_id": req.new_panorama_version_id
    }
