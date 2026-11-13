import json
import uuid
import sqlite3
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from ..database import get_db
from ..config import (
    DEFAULT_WATERMARK, DEFAULT_WARNING_BUDGET, DEFAULT_HARD_LIMIT_BUDGET,
    DEFAULT_WARNING_PER_GROUP, DEFAULT_HARD_LIMIT_PER_GROUP,
)
from ..models import BatchCreateRequest, BatchExpandRequest, BatchResponse, BudgetSummary
from ..watermark_service import ensure_watermark_config_schema
from ..security import UserContext, get_current_user, require_roles
from ..budget_service import get_batch_budget_summary, reserve_budget
from ..series_rules_service import get_configured_series_rules
from ..planning_service import build_mock_visual_features, build_slot_prompt, generate_design_spec_from_rule, get_batch_rule_snapshot, series_history
from ..budget_service import BudgetHardLimitExceededError, BudgetWarningExceededError
from .designs import _maybe_wake_standby_design

router = APIRouter(prefix="/api/batches", tags=["Batches"])


@router.put('/{batch_id}/series-counts')
def expand_batch(batch_id: str, req: BatchExpandRequest,
                 user: UserContext = Depends(require_roles(['operator', 'leader', 'admin']))):
    with get_db(immediate=True) as conn:
        batch = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not batch:
            raise HTTPException(404, '任务不存在')
        if not batch['auto_workflow']:
            raise HTTPException(409, '仅自动任务支持修改组数')
        if not batch['approved_series_rules_json']:
            raise HTTPException(409, '任务缺少规则快照')
        old = json.loads(batch['series_counts_json'] or '{}')
        previous = {sid: int(value['primary']) for sid, value in old.items()}
        if req.expected_series_counts != previous:
            raise HTTPException(409, '任务组数已变化，请刷新后重试')
        counts = req.series_counts
        if (not counts or any(isinstance(n, bool) or n < 1 or n > 30 for n in counts.values())
                or sum(counts.values()) > 50 or any(counts.get(sid, 0) < n for sid, n in previous.items())):
            raise HTTPException(400, '已有系列组数不能减少，每系列最多30组，任务最多50组')
        delta = sum(counts.values()) - sum(previous.values())
        if not delta:
            raise HTTPException(400, '请增加组数或添加系列')
        rules = get_configured_series_rules(conn)
        if any(sid not in rules for sid in counts):
            raise HTTPException(400, '选择的风格系列不存在')
        snapshot = json.loads(batch['approved_series_rules_json'])
        for sid, n in counts.items():
            extra = n - previous.get(sid, 0)
            if not extra:
                continue
            rule = rules[sid]
            if sid not in snapshot:
                snapshot[sid] = rule
            index = conn.execute('SELECT COUNT(*) FROM designs WHERE batch_id=? AND series_id=?', (batch_id, sid)).fetchone()[0]
            for offset in range(extra):
                design_id = f'des_{uuid.uuid4().hex}'
                code = f'AUTO-{uuid.uuid4().hex[:12].upper()}'
                spec = generate_design_spec_from_rule(sid, index + offset, rule, history=series_history(conn, sid), batch_id=batch_id)
                conn.execute("INSERT INTO designs (id,batch_id,series_id,design_code,status,structured_spec,template_version,planning_status,series_rule_snapshot_json) VALUES (?,?,?,?,'draft',?,?,'pending',?)", (design_id,batch_id,sid,code,json.dumps(spec,ensure_ascii=False),rule['template_version'],json.dumps(rule,ensure_ascii=False)))
                for stype in ('panorama','white_isolated','twin_bed','nightstand_closeup'):
                    conn.execute("INSERT INTO slots (id,design_id,slot_type,status) VALUES (?,?,?,'empty')", (f'slt_{uuid.uuid4().hex}',design_id,stype))
                conn.execute('INSERT INTO planning_jobs (id,batch_id,design_id) VALUES (?,?,?)', (f'plan_{uuid.uuid4().hex}',batch_id,design_id))
        settings = conn.execute("SELECT warning_per_group,hard_limit_per_group FROM workbench_settings WHERE id='default'").fetchone()
        new_counts = {sid: {'primary': n, 'standby': 0} for sid, n in counts.items()}
        conn.execute("""UPDATE batches SET enabled_series=?,series_counts_json=?,approved_series_rules_json=?,
                     target_archived_count=?,warning_budget=warning_budget+?,hard_limit_budget=hard_limit_budget+?,
                     status=CASE WHEN status='completed' AND is_circuit_broken=0 AND pause_reason IS NULL THEN 'active' ELSE status END,
                     updated_at=datetime('now','localtime') WHERE id=?""",
                     (json.dumps(list(counts),ensure_ascii=False),json.dumps(new_counts,ensure_ascii=False),json.dumps(snapshot,ensure_ascii=False),sum(counts.values()),delta*settings['warning_per_group'],delta*settings['hard_limit_per_group'],batch_id))
        conn.execute("INSERT INTO audit_logs (id,batch_id,action,actor_name,actor_role,detail) VALUES (?,?,'expand_batch',?,?,?)", (f'aud_{uuid.uuid4().hex}',batch_id,user.user_id,user.role,json.dumps({'before':previous,'after':counts},ensure_ascii=False)))
    return {'message': '任务组数已更新', 'added': delta}


def _series_inputs(req: BatchCreateRequest, series_ids: List[str]) -> tuple[Dict[str, Dict[str, int]], Dict[str, str]]:
    """Normalize per-series counts/preferences without changing legacy payloads."""
    raw_counts = req.series_counts or {}
    preferences = {str(k): str(v).strip() for k, v in (req.preferences or {}).items() if str(v).strip()}
    preferences.update({str(k): str(v).strip() for k, v in (req.series_preferences or {}).items() if str(v).strip()})
    counts: Dict[str, Dict[str, int]] = {}
    for sid in series_ids:
        raw = raw_counts.get(sid) or {}
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"系列 {sid} 的数量配置格式无效")
        try:
            primary = int(raw.get("primary", req.designs_per_series))
            standby = int(raw.get("standby", req.standby_designs_per_series))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"系列 {sid} 的数量必须为整数") from exc
        if "preference" in raw and str(raw.get("preference") or "").strip():
            preferences[sid] = str(raw["preference"]).strip()[:2000]
        counts[sid] = {"primary": primary, "standby": standby}
    return counts, preferences


def _apply_series_preference(spec: Dict[str, Any], preferences: Dict[str, str], series_id: str) -> Dict[str, Any]:
    preference = (preferences.get(series_id) or "").strip()
    if preference:
        spec["series_preference"] = preference
    return spec


def _generate_design_spec(
    series_id: str,
    index: int,
    configured_rules: Dict[str, Dict[str, Any]],
    is_standby: bool = False,
    history: Optional[list] = None,
    batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    if series_id not in configured_rules:
        raise ValueError(f"系列 {series_id} 不在批准范围")
    return generate_design_spec_from_rule(series_id, index, configured_rules[series_id], is_standby, history, batch_id)


def _queue_initial_panorama(conn, batch_id: str, design_id: str, code: str) -> str:
    cur = conn.cursor()
    cur.execute("""
        SELECT s.id AS slot_id, d.structured_spec, d.template_version
        FROM designs d
        JOIN slots s ON s.design_id = d.id AND s.slot_type = 'panorama'
        WHERE d.id = ?
    """, (design_id,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"设计 {code} 缺少全景槽位")
    spec = json.loads(row["structured_spec"])
    template_version = row["template_version"]
    prompt, prompt_metadata = build_slot_prompt(
        "panorama", spec, code, template_version
    )
    metadata_json = json.dumps(prompt_metadata, ensure_ascii=False)
    features_json = json.dumps(
        build_mock_visual_features(spec, "panorama", "pending"),
        ensure_ascii=False,
    )
    version_id = f"ver_{uuid.uuid4().hex[:8]}"
    cur.execute("""
        INSERT INTO slot_versions (
            id, slot_id, design_id, version_num, attempt_num, prompt, prompt_metadata,
            mock_visual_features, process_status, template_version
        ) VALUES (?, ?, ?, 1, 1, ?, ?, ?, 'pending', ?)
    """, (version_id, row["slot_id"], design_id, prompt, metadata_json, features_json, template_version))

    task_id = f"tsk_{uuid.uuid4().hex[:8]}"
    reserve_budget(
        conn, batch_id=batch_id, category="image_generation", amount=1,
        task_id=task_id, note=f"Initial panorama for {code}"
    )
    cur.execute("""
        INSERT INTO tasks (
            id, batch_id, design_id, slot_id, slot_version_id, task_type, status, idempotency_key
        ) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'queued', ?)
    """, (task_id, batch_id, design_id, row["slot_id"], version_id, f"init_pano_{design_id}"))
    conn.execute(
        "UPDATE slots SET status = 'generating', current_version_id = ? WHERE id = ?",
        (version_id, row["slot_id"]),
    )
    conn.execute(
        "UPDATE designs SET status = 'panorama_generating', updated_at = datetime('now', 'localtime') WHERE id = ?",
        (design_id,),
    )
    return task_id


@router.post("", response_model=BatchResponse)
def create_batch(
    req: BatchCreateRequest,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    batch_id = f"batch_{uuid.uuid4().hex[:8]}"
    if user.is_operator and not req.auto_workflow:
        raise HTTPException(status_code=403, detail='美工只能创建自动规划任务')

    # Automatic tasks own their quota calculation.  Never let a client lower
    # the server defaults by sending explicit budget fields.
    if req.auto_workflow:
        if not req.smart_planning or req.auto_start_panorama or any(c.get('standby', 0) for c in req.series_counts.values()):
            raise HTTPException(status_code=400, detail='自动任务应使用智能规划且不预设候补')
        if sum(c.get('primary', 0) for c in req.series_counts.values()) != req.target_archived_count:
            raise HTTPException(status_code=400, detail='各系列组数之和必须等于任务目标')
        with get_db() as settings_conn:
            settings = settings_conn.execute("SELECT warning_per_group, hard_limit_per_group FROM workbench_settings WHERE id='default'").fetchone()
        req = req.model_copy(update={
            'warning_budget': req.target_archived_count * (settings['warning_per_group'] or DEFAULT_WARNING_PER_GROUP),
            'hard_limit_budget': req.target_archived_count * (settings['hard_limit_per_group'] or DEFAULT_HARD_LIMIT_PER_GROUP),
        })
    else:
        # Explicit legacy/manual callers retain their supplied quota, while a
        # missing value gets the historical small-sample defaults.
        req = req.model_copy(update={
            'warning_budget': req.warning_budget or DEFAULT_WARNING_BUDGET,
            'hard_limit_budget': req.hard_limit_budget or DEFAULT_HARD_LIMIT_BUDGET,
        })
    if req.target_archived_count >= 10 and req.hard_limit_budget <= DEFAULT_HARD_LIMIT_BUDGET:
        raise HTTPException(
            status_code=400,
            detail="日常生产不能直接套用小样 25 次硬上限；请负责人显式评估并填写更大的批次硬上限。"
        )
    if not req.enabled_series:
        raise HTTPException(status_code=400, detail="批次必须至少启用一个产品系列")
    deduped_series = list(dict.fromkeys(req.enabled_series))
    if len(deduped_series) != len(req.enabled_series):
        raise HTTPException(status_code=400, detail="启用系列存在重复项，请去重后提交")
    series_counts, series_preferences = _series_inputs(req, deduped_series)
    if req.auto_workflow and sum(c["primary"] for c in series_counts.values()) != req.target_archived_count:
        raise HTTPException(status_code=400, detail="各系列正式款组数之和必须等于任务目标")
    if req.smart_planning:
        if req.auto_start_panorama:
            raise HTTPException(status_code=400, detail="智能规划必须先审核方案，不能自动派图")
        if set(series_counts) != set(deduped_series):
            raise HTTPException(status_code=400, detail="请为每个已选系列设置数量")
        for counts in series_counts.values():
            if not (1 <= counts["primary"] <= 30 and 0 <= counts["standby"] <= 30):
                raise HTTPException(status_code=400, detail="每系列正式款须为 1–30，候补款须为 0–30")
        if sum(c['primary'] + c['standby'] for c in series_counts.values()) > 60:
            raise HTTPException(status_code=400, detail="单批正式款及候补合计最多 60 款")

    with get_db(immediate=True) as conn:
        configured_rules = get_configured_series_rules(conn)
        unknown_series = [sid for sid in deduped_series if sid not in configured_rules]
        if unknown_series:
            raise HTTPException(status_code=400, detail=f"未知的产品系列: {unknown_series}")
        approved_snapshot = {sid: configured_rules[sid] for sid in deduped_series}
        initial_status = 'active' if req.auto_start_panorama else 'paused'
        initial_pause = None if req.auto_start_panorama else 'AWAITING_LEADER_START'
        conn.execute("""
            INSERT INTO batches (
                id, name, enabled_series, target_archived_count, current_archived_count,
                status, is_mock, warning_budget, hard_limit_budget, trial_run_remaining,
                approved_series_rules_json, approved_rule_snapshot_status, series_counts_json,
                series_preferences_json, pause_reason
            ) VALUES (?, ?, ?, ?, 0, ?, 1, ?, ?, 0, ?, 'approved_frozen', ?, ?, ?)
        """, (
            batch_id, req.name, json.dumps(req.enabled_series), req.target_archived_count,
            initial_status, req.warning_budget, req.hard_limit_budget,
            json.dumps(approved_snapshot, ensure_ascii=False),
            json.dumps(series_counts, ensure_ascii=False),
            json.dumps(series_preferences, ensure_ascii=False), initial_pause
        ))
        if req.smart_planning:
            conn.execute("UPDATE batches SET smart_planning = 1, planning_brief = ?, pause_reason = 'PLANNING_REQUIRED' WHERE id = ?",
                         (req.planning_brief.strip(), batch_id))
        if req.auto_workflow:
            conn.execute("UPDATE batches SET auto_workflow=1, planning_confirmed=1, status='active', pause_reason=NULL WHERE id=?", (batch_id,))

        conn.execute("""
            INSERT INTO watermark_configs (
                id, batch_id, main_text, repeat_text, opacity, font_size, position,
                renderer, logo_asset, logo_width_ratio, logo_left_ratio, logo_top_ratio, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"wm_{uuid.uuid4().hex[:8]}", batch_id,
            DEFAULT_WATERMARK["main_text"], DEFAULT_WATERMARK["repeat_text"],
            DEFAULT_WATERMARK["opacity"], DEFAULT_WATERMARK["font_size"],
            DEFAULT_WATERMARK["position"], DEFAULT_WATERMARK["renderer"],
            DEFAULT_WATERMARK["logo_asset"], DEFAULT_WATERMARK["logo_width_ratio"],
            DEFAULT_WATERMARK["logo_left_ratio"], DEFAULT_WATERMARK["logo_top_ratio"], user.user_id
        ))

        prefix_map = {"modern_business": "MB", "natural_wood": "NW", "soft_contemporary": "SC"}
        slot_types = ["panorama", "white_isolated", "twin_bed", "nightstand_closeup"]
        created_designs = []

        custom_count = 0
        for s_id in deduped_series:
            if s_id in prefix_map:
                prefix = prefix_map[s_id]
            else:
                custom_count += 1
                prefix = 'DS' if custom_count == 1 else f'DS{custom_count}'
            counts = series_counts[s_id]
            primary_count = counts['primary']
            standby_count = counts['standby']
            for i in range(primary_count):
                d_id = f"des_{uuid.uuid4().hex[:8]}"
                code = f"{prefix}-{i+1:02d}"
                spec = _apply_series_preference(
                    _generate_design_spec(s_id, i, configured_rules, is_standby=False, history=series_history(conn, s_id), batch_id=batch_id),
                    series_preferences, s_id,
                )
                
                conn.execute("""
                    INSERT INTO designs (
                        id, batch_id, series_id, design_code, is_standby, status, structured_spec, template_version
                    ) VALUES (?, ?, ?, ?, 0, 'draft', ?, ?)
                """, (d_id, batch_id, s_id, code, json.dumps(spec, ensure_ascii=False), spec["template_version"]))

                # 记录规划模型成本记账流水
                if req.smart_planning:
                    conn.execute("UPDATE designs SET planning_status='pending' WHERE id=?", (d_id,))
                    if req.auto_workflow:
                        conn.execute("INSERT INTO planning_jobs (id,batch_id,design_id) VALUES (?,?,?)", (f'plan_{uuid.uuid4().hex}', batch_id, d_id))
                else:
                    plan_bgt_id = f"bgt_plan_{uuid.uuid4().hex[:8]}"
                    conn.execute("""
                    INSERT INTO budget_ledger (id, batch_id, category, delta_reserved, delta_used, delta_unsettled, status, note)
                    VALUES (?, ?, 'planning_model', 0, 1, 0, 'settled', ?)
                    """, (plan_bgt_id, batch_id, f"Planning model spec for {code}"))

                for stype in slot_types:
                    slot_id = f"slt_{uuid.uuid4().hex[:8]}"
                    conn.execute("""
                        INSERT INTO slots (id, design_id, slot_type, status)
                        VALUES (?, ?, ?, 'empty')
                    """, (slot_id, d_id, stype))

                created_designs.append((d_id, code, s_id, False))

            for j in range(standby_count):
                d_id = f"des_{uuid.uuid4().hex[:8]}"
                idx = primary_count + j
                code = f"{prefix}-STBY-{j+1:02d}"
                spec = _apply_series_preference(
                    _generate_design_spec(s_id, idx, configured_rules, is_standby=True, history=series_history(conn, s_id), batch_id=batch_id),
                    series_preferences, s_id,
                )
                
                conn.execute("""
                    INSERT INTO designs (
                        id, batch_id, series_id, design_code, is_standby, status, structured_spec, template_version
                    ) VALUES (?, ?, ?, ?, 1, 'draft', ?, ?)
                """, (d_id, batch_id, s_id, code, json.dumps(spec, ensure_ascii=False), spec["template_version"]))

                if req.smart_planning:
                    conn.execute("UPDATE designs SET planning_status='pending' WHERE id=?", (d_id,))
                else:
                    plan_bgt_id = f"bgt_plan_{uuid.uuid4().hex[:8]}"
                    conn.execute("""
                    INSERT INTO budget_ledger (id, batch_id, category, delta_reserved, delta_used, delta_unsettled, status, note)
                    VALUES (?, ?, 'planning_model', 0, 1, 0, 'settled', ?)
                    """, (plan_bgt_id, batch_id, f"Planning model appended standby {code}"))

                for stype in slot_types:
                    slot_id = f"slt_{uuid.uuid4().hex[:8]}"
                    conn.execute("""
                        INSERT INTO slots (id, design_id, slot_type, status)
                        VALUES (?, ?, ?, 'empty')
                    """, (slot_id, d_id, stype))

        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'create_batch', ?, ?, ?)
        """, (
            f"aud_{uuid.uuid4().hex[:8]}", batch_id, user.user_id, user.role,
            json.dumps({"name": req.name, "target": req.target_archived_count, "designs": len(created_designs)})
        ))

        if req.auto_start_panorama:
            for d_id, code, s_id, is_stby in created_designs:
                _queue_initial_panorama(conn, batch_id, d_id, code)

    return get_batch_by_id(batch_id)

@router.get("", response_model=List[BatchResponse])
def list_batches(user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))):
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM batches ORDER BY created_at DESC")
        rows = cur.fetchall()
        res = []
        for r in rows:
            res.append(BatchResponse(
                id=r["id"],
                name=r["name"],
                enabled_series=json.loads(r["enabled_series"]),
                target_archived_count=r["target_archived_count"],
                current_archived_count=r["current_archived_count"],
                status=r["status"],
                is_mock=bool(r["is_mock"]),
                warning_budget=r["warning_budget"],
                hard_limit_budget=r["hard_limit_budget"],
                budget_warning_exceeded_approved_by=r["budget_warning_exceeded_approved_by"],
                budget_warning_exceeded_at=r["budget_warning_exceeded_at"],
                is_circuit_broken=bool(r["is_circuit_broken"]),
                circuit_break_reason=r["circuit_break_reason"],
                pause_reason=r["pause_reason"],
                trial_run_remaining=r["trial_run_remaining"],
                approved_rule_snapshot_status=r["approved_rule_snapshot_status"],
                smart_planning=bool(r["smart_planning"]), planning_confirmed=bool(r["planning_confirmed"]),
                auto_workflow=bool(r["auto_workflow"]),
                series_counts=json.loads(r["series_counts_json"] or "{}"),
                series_preferences=json.loads(r["series_preferences_json"] or "{}"),
                created_at=r["created_at"],
                updated_at=r["updated_at"]
            ))
        return res

@router.get("/{batch_id}", response_model=BatchResponse)
def get_batch_by_id(batch_id: str, user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))):
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM batches WHERE id = ?", (batch_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="批次不存在")
        return BatchResponse(
            id=r["id"],
            name=r["name"],
            enabled_series=json.loads(r["enabled_series"]),
            target_archived_count=r["target_archived_count"],
            current_archived_count=r["current_archived_count"],
            status=r["status"],
            is_mock=bool(r["is_mock"]),
            warning_budget=r["warning_budget"],
            hard_limit_budget=r["hard_limit_budget"],
            budget_warning_exceeded_approved_by=r["budget_warning_exceeded_approved_by"],
            budget_warning_exceeded_at=r["budget_warning_exceeded_at"],
            is_circuit_broken=bool(r["is_circuit_broken"]),
            circuit_break_reason=r["circuit_break_reason"],
            pause_reason=r["pause_reason"],
            trial_run_remaining=r["trial_run_remaining"],
            approved_rule_snapshot_status=r["approved_rule_snapshot_status"],
            smart_planning=bool(r["smart_planning"]), planning_confirmed=bool(r["planning_confirmed"]),
            auto_workflow=bool(r["auto_workflow"]),
            series_counts=json.loads(r["series_counts_json"] or "{}"),
            series_preferences=json.loads(r["series_preferences_json"] or "{}"),
            created_at=r["created_at"],
            updated_at=r["updated_at"]
        )

@router.get("/{batch_id}/budget", response_model=BudgetSummary)
def get_batch_budget(
    batch_id: str,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    with get_db(immediate=False) as conn:
        return get_batch_budget_summary(conn, batch_id)

@router.post("/{batch_id}/approve_warning")
def approve_budget_warning(
    batch_id: str,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    with get_db(immediate=True) as conn:
        cur = conn.execute("""
            UPDATE batches
            SET budget_warning_exceeded_approved_by = ?, budget_warning_exceeded_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (user.user_id, batch_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="批次不存在")

        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'approve_budget_warning', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, user.user_id, user.role, json.dumps({"batch_id": batch_id})))

        # 预警获批后补做被预算防护推迟的候补唤醒；唤醒失败绝不影响审批本身。
        try:
            _maybe_wake_standby_design(conn, batch_id)
        except Exception as wake_error:  # noqa: BLE001
            conn.execute("""
                INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
                VALUES (?, ?, 'standby_wake_failed_after_warning_approval', 'system', 'system', ?)
            """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, json.dumps({"reason": str(wake_error)})))

    return {"message": "负责人已明确批准跨越预警线继续，硬上限约束依然有效", "approved_by": user.user_id}

@router.post("/{batch_id}/start")
def start_batch_and_initial_panoramas(
    batch_id: str,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """Leader manually starts a held batch. Transaction and existing-task checks prevent duplicate dispatch."""
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT status, pause_reason, is_circuit_broken, enabled_series,
                   approved_series_rules_json, approved_rule_snapshot_status, smart_planning, planning_confirmed
            FROM batches WHERE id = ?
        """, (batch_id,))
        batch = cur.fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="批次不存在")
        if batch["is_circuit_broken"]:
            raise HTTPException(status_code=400, detail="熔断批次必须先通过熔断恢复接口试运行，不能直接启动")
        if batch['smart_planning'] and not batch['planning_confirmed']:
            raise HTTPException(status_code=409, detail="请先确认所有正式款及候补方案")
        if batch["status"] == "completed":
            raise HTTPException(status_code=400, detail="批次已完成，不能重复启动")
        if batch["pause_reason"] == "PAUSE_UNKNOWN_PENDING":
            raise HTTPException(status_code=400, detail="存在结果未知任务，必须先处置")
        if batch["approved_rule_snapshot_status"] == "legacy_unfrozen":
            raise HTTPException(status_code=409, detail="旧批次缺少批准规则快照，请负责人显式迁移后才能启动")

        cur.execute("""
            SELECT COUNT(*) AS task_count
            FROM tasks t JOIN designs d ON t.design_id = d.id
            WHERE t.batch_id = ? AND d.is_standby = 0
        """, (batch_id,))
        if cur.fetchone()["task_count"]:
            raise HTTPException(status_code=409, detail="首发全景任务已存在，禁止重复派发")

        cur.execute("""
            SELECT COUNT(*) AS unknown_count
            FROM slot_versions sv JOIN designs d ON sv.design_id = d.id
            WHERE d.batch_id = ? AND d.is_standby = 0
              AND (sv.process_status = 'unknown_pending_confirmation' OR sv.is_unknown = 1)
        """, (batch_id,))
        if cur.fetchone()["unknown_count"]:
            raise HTTPException(status_code=409, detail="存在结果未知版本，禁止启动")

        enabled_series = json.loads(batch["enabled_series"])
        snapshot = json.loads(batch["approved_series_rules_json"] or "{}")
        cur.execute("""
            SELECT d.id, d.design_code, d.series_id
            FROM designs d
            WHERE d.batch_id = ? AND d.is_standby = 0
            ORDER BY d.created_at ASC
        """, (batch_id,))
        designs = cur.fetchall()
        if not designs:
            raise HTTPException(status_code=400, detail="批次没有首发设计")
        for design in designs:
            if design["series_id"] not in enabled_series or design["series_id"] not in snapshot:
                raise HTTPException(status_code=400, detail="首发设计超出批准系列快照")
            _queue_initial_panorama(conn, batch_id, design["id"], design["design_code"])

        conn.execute("""
            UPDATE batches SET status = 'active', pause_reason = NULL,
                updated_at = datetime('now', 'localtime') WHERE id = ?
        """, (batch_id,))
        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'start_batch_initial_panoramas', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, user.user_id, user.role,
              json.dumps({"queued_designs": len(designs), "rule_snapshot": batch["approved_rule_snapshot_status"]})))

    return {"message": "负责人已启动首发全景生成", "queued_designs": len(designs)}


@router.post("/{batch_id}/migrate_rule_snapshot")
def migrate_legacy_rule_snapshot(
    batch_id: str,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """Explicit migration for legacy batches; never silently claim an unfrozen batch was approved."""
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT enabled_series, approved_series_rules_json FROM batches WHERE id = ?", (batch_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="批次不存在")
        if row["approved_series_rules_json"]:
            raise HTTPException(status_code=409, detail="该批次已有规则快照，禁止覆盖迁移")

        configured = get_configured_series_rules(conn)
        enabled_series = json.loads(row["enabled_series"])
        if any(series_id not in configured for series_id in enabled_series):
            raise HTTPException(status_code=400, detail="启用系列缺少当前规则配置")
        snapshot = {series_id: configured[series_id] for series_id in enabled_series}
        conn.execute("""
            UPDATE batches SET approved_series_rules_json = ?, approved_rule_snapshot_status = 'explicit_legacy_migration',
                updated_at = datetime('now', 'localtime') WHERE id = ?
        """, (json.dumps(snapshot, ensure_ascii=False), batch_id))
        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'explicit_legacy_rule_snapshot_migration', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, user.user_id, user.role,
              json.dumps({"series": enabled_series, "versions": {sid: rule["template_version"] for sid, rule in snapshot.items()}})))

    return {"message": "已显式迁移为当前规则快照；旧设计模板不会被改写", "snapshot_status": "explicit_legacy_migration"}

@router.post("/{batch_id}/circuit_break_recover")
def circuit_break_recover(
    batch_id: str,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    """
    负责人恢复熔断（试运行机制）：
    解除熔断，设置 3 次少量试运行保护，试运行期间任务继续受监控，不跨日自动补足。
    """
    with get_db(immediate=True) as conn:
        batch = conn.execute("SELECT is_circuit_broken FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="批次不存在")
        if not batch["is_circuit_broken"]:
            raise HTTPException(status_code=409, detail="批次当前未熔断，不能重置试运行次数")
        conn.execute("""
            UPDATE batches 
            SET is_circuit_broken = 0, status = 'active', pause_reason = NULL,
                trial_mode = 1, trial_run_remaining = 3, trial_epoch = trial_epoch + 1,
                updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (batch_id,))

        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'circuit_break_recover', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, user.user_id, user.role, json.dumps({"trial_runs": 3})))

    return {"message": "熔断已恢复，已开启 3 次少量试运行保护", "trial_run_remaining": 3}

@router.post("/{batch_id}/append_standby")
def append_standby_designs(
    batch_id: str,
    count_per_series: int = 1,
    user: UserContext = Depends(require_roles(["leader", "admin"]))
):
    if count_per_series < 1 or count_per_series > 10:
        raise HTTPException(status_code=400, detail="每系列候补数量必须在 1-10")
    with get_db(immediate=True) as conn:
        smart = conn.execute("SELECT smart_planning, planning_confirmed FROM batches WHERE id=?", (batch_id,)).fetchone()
        cur = conn.cursor()
        cur.execute("""
            SELECT enabled_series, approved_series_rules_json, approved_rule_snapshot_status
            FROM batches WHERE id = ?
        """, (batch_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="批次不存在")
        if row["approved_rule_snapshot_status"] == "legacy_unfrozen" or not row["approved_series_rules_json"]:
            raise HTTPException(
                status_code=409,
                detail="旧批次缺少批准规则快照，不能静默使用当前规则追加；请负责人显式迁移后继续"
            )
        enabled_series = json.loads(row["enabled_series"])
        snapshot = json.loads(row["approved_series_rules_json"])

        prefix_map = {"modern_business": "MB", "natural_wood": "NW", "soft_contemporary": "SC"}
        slot_types = ["panorama", "white_isolated", "twin_bed", "nightstand_closeup"]
        appended = []

        custom_count = 0
        for s_id in enabled_series:
            if s_id not in snapshot:
                raise HTTPException(status_code=400, detail=f"批准规则快照缺少系列 {s_id}")
            if s_id in prefix_map:
                prefix = prefix_map[s_id]
            else:
                custom_count += 1
                prefix = 'DS' if custom_count == 1 else f'DS{custom_count}'
            cur.execute("SELECT COUNT(*) as c FROM designs WHERE batch_id = ? AND series_id = ?", (batch_id, s_id))
            existing_count = cur.fetchone()["c"]

            for k in range(count_per_series):
                d_id = f"des_{uuid.uuid4().hex[:8]}"
                code = f"{prefix}-STBY-{existing_count + k + 1:02d}"
                spec = _generate_design_spec(s_id, existing_count + k, snapshot, is_standby=True, history=series_history(conn, s_id), batch_id=batch_id)

                conn.execute("""
                    INSERT INTO designs (id, batch_id, series_id, design_code, is_standby, status, structured_spec, template_version)
                    VALUES (?, ?, ?, ?, 1, 'draft', ?, ?)
                """, (d_id, batch_id, s_id, code, json.dumps(spec, ensure_ascii=False), spec["template_version"]))

                if smart and smart['smart_planning']:
                    conn.execute("UPDATE designs SET planning_status=? WHERE id=?", ('pending_append' if smart['planning_confirmed'] else 'pending', d_id))
                else:
                    plan_bgt_id = f"bgt_plan_{uuid.uuid4().hex[:8]}"
                    conn.execute("""
                    INSERT INTO budget_ledger (id, batch_id, category, delta_reserved, delta_used, delta_unsettled, status, note)
                    VALUES (?, ?, 'planning_model', 0, 1, 0, 'settled', ?)
                    """, (plan_bgt_id, batch_id, f"Planning model appended standby {code}"))

                for stype in slot_types:
                    slot_id = f"slt_{uuid.uuid4().hex[:8]}"
                    conn.execute("INSERT INTO slots (id, design_id, slot_type, status) VALUES (?, ?, ?, 'empty')", (slot_id, d_id, stype))

                appended.append({"code": code, "template_version": spec["template_version"]})

        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'append_standby', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, user.user_id, user.role,
              json.dumps({"appended": appended, "rule_snapshot": row["approved_rule_snapshot_status"]})))

    return {"message": "成功追加候补设计" + ("，请先规划并确认" if smart and smart['smart_planning'] else ""), "appended": appended, "rule_snapshot": row["approved_rule_snapshot_status"]}
