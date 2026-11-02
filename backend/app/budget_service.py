import sqlite3
import uuid
from typing import Optional
from .models import BudgetSummary

class BudgetWarningExceededError(Exception):
    """达到预警线且未获得负责人明确批准"""
    pass

class BudgetHardLimitExceededError(Exception):
    """达到批次硬上限，系统硬阻断"""
    pass

def get_batch_budget_summary(conn: sqlite3.Connection, batch_id: str) -> BudgetSummary:
    """获取批次的分类成本和预算状态"""
    cur = conn.cursor()
    cur.execute("""
        SELECT warning_budget, hard_limit_budget, budget_warning_exceeded_approved_by, is_circuit_broken
        FROM batches WHERE id = ?
    """, (batch_id,))
    batch_row = cur.fetchone()
    if not batch_row:
        raise ValueError(f"Batch {batch_id} not found")
    
    warning_budget = batch_row["warning_budget"]
    hard_limit_budget = batch_row["hard_limit_budget"]
    warning_approved = bool(batch_row["budget_warning_exceeded_approved_by"])
    is_circuit_broken = bool(batch_row["is_circuit_broken"])

    cur.execute("""
        SELECT 
            category,
            status,
            ledger_event_type,
            SUM(delta_reserved) as sum_reserved,
            SUM(delta_used) as sum_used,
            SUM(delta_unsettled) as sum_unsettled,
            COUNT(*) as item_count
        FROM budget_ledger
        WHERE batch_id = ?
        GROUP BY category, status, ledger_event_type
    """, (batch_id,))
    
    rows = cur.fetchall()
    
    image_gen_count = 0
    planning_count = 0
    vision_count = 0
    reserved_count = 0
    used_count = 0
    unsettled_count = 0
    unknown_count = 0
    explicit_failure_count = 0
    technical_retry_count = 0

    for r in rows:
        cat = r["category"]
        st = r["status"]
        s_res = r["sum_reserved"] or 0
        s_used = r["sum_used"] or 0
        s_unsettled = r["sum_unsettled"] or 0
        
        if cat == "image_generation":
            if st == "settled":
                image_gen_count += s_used
                used_count += s_used
            elif st == "reserved":
                image_gen_count += s_res
                reserved_count += s_res
            elif st == "unknown_held":
                image_gen_count += (s_res + s_used)
                unknown_count += (s_res if s_res > 0 else s_used)
            elif st == "released":
                if r["ledger_event_type"] == "technical_retry":
                    technical_retry_count += r["item_count"]
                else:
                    explicit_failure_count += r["item_count"]
            if st in ("settled", "reserved", "unknown_held", "released"):
                unsettled_count += s_unsettled
        elif cat == "planning_model":
            planning_count += s_used
        elif cat == "vision_model":
            vision_count += s_used
            
    total_image_in_flight = image_gen_count
    warning_reached = total_image_in_flight >= warning_budget
    hard_limit_reached = total_image_in_flight >= hard_limit_budget

    return BudgetSummary(
        batch_id=batch_id,
        image_generation_count=image_gen_count,
        planning_model_count=planning_count,
        vision_model_count=vision_count,
            failed_retry_count=explicit_failure_count,
            explicit_failure_count=explicit_failure_count,
            technical_retry_count=technical_retry_count,
        unknown_count=unknown_count,
        reserved_count=reserved_count,
        used_count=used_count,
        unsettled_count=unsettled_count,
        warning_budget=warning_budget,
        hard_limit_budget=hard_limit_budget,
        warning_reached=warning_reached,
        warning_approved=warning_approved,
        hard_limit_reached=hard_limit_reached,
        circuit_broken=is_circuit_broken
    )

def reserve_budget(
    conn: sqlite3.Connection,
    batch_id: str,
    category: str = "image_generation",
    amount: int = 1,
    task_id: Optional[str] = None,
    note: Optional[str] = None
) -> str:
    """在当前写事务中原子化检查预算并预占额度"""
    if category != "image_generation":
        raise ValueError("只有 image_generation 参与生图预算限额；规划/视觉成本直接单独结算")
    summary = get_batch_budget_summary(conn, batch_id)
    
    if summary.circuit_broken:
        raise ValueError("批次处于熔断状态，禁止继续派发任务")

    current_active = summary.image_generation_count
    
    if current_active + amount > summary.hard_limit_budget:
        raise BudgetHardLimitExceededError(
            f"已达批次硬上限 ({current_active + amount} > {summary.hard_limit_budget})，后端硬阻断，严禁超支！"
        )

    if current_active + amount > summary.warning_budget:
        if not summary.warning_approved:
            raise BudgetWarningExceededError(
                f"生图请求即将达到或超过预警线 ({current_active + amount} > {summary.warning_budget})，新任务已暂停，需负责人明确批准后方可继续！"
            )

    ledger_id = f"bgt_{uuid.uuid4().hex[:12]}"
    conn.execute("""
        INSERT INTO budget_ledger (
            id, batch_id, category, delta_reserved, delta_used, delta_unsettled, status,
            ledger_event_type, reference_task_id, note
        ) VALUES (?, ?, ?, ?, 0, 0, 'reserved', 'generation_request', ?, ?)
    """, (ledger_id, batch_id, category, amount, task_id, note or "Task dispatch reservation"))
    
    return ledger_id

def settle_budget_success(
    conn: sqlite3.Connection,
    batch_id: str,
    ledger_id: str,
    task_id: Optional[str] = None
):
    """
    将预占流水确认为已消耗结算（严格幂等）：
    - 若已经处于 settled，直接幂等返回，防止重复调用导致 delta_used 被置 0
    - 仅允许从 reserved 或 unknown_held 转移至 settled
    """
    cur = conn.cursor()
    cur.execute("SELECT status, delta_reserved, delta_used FROM budget_ledger WHERE id = ? AND batch_id = ?", (ledger_id, batch_id))
    row = cur.fetchone()
    if not row:
        return

    curr_status = row["status"]
    if curr_status == "settled":
        return  # 幂等返回，保持原有 delta_used

    if curr_status not in ("reserved", "unknown_held"):
        return

    amount = row["delta_reserved"] if row["delta_reserved"] > 0 else (row["delta_used"] if row["delta_used"] > 0 else 1)
    conn.execute("""
        UPDATE budget_ledger 
        SET delta_reserved = 0, delta_used = ?, status = 'settled', note = 'Completed successfully'
        WHERE id = ? AND batch_id = ? AND status IN ('reserved', 'unknown_held')
    """, (amount, ledger_id, batch_id))

def release_budget_failed(
    conn: sqlite3.Connection,
    batch_id: str,
    ledger_id: str,
    reason: str = "Explicit failure"
):
    """
    明确失败释放预占预算（严格幂等）：
    - 仅允许释放 reserved 或 unknown_held 状态的记录
    - 若已经 settled（已真实消耗结算），绝不允许回退成 released
    """
    cur = conn.cursor()
    cur.execute("SELECT status FROM budget_ledger WHERE id = ? AND batch_id = ?", (ledger_id, batch_id))
    row = cur.fetchone()
    if not row:
        return

    if row["status"] == "released":
        return  # 幂等

    if row["status"] == "settled":
        return  # 保护已消费账本

    conn.execute("""
        UPDATE budget_ledger 
        SET delta_reserved = 0, delta_used = 0, status = 'released', note = ?
        WHERE id = ? AND batch_id = ? AND status IN ('reserved', 'unknown_held')
    """, (f"Released: {reason}", ledger_id, batch_id))

def hold_budget_unknown(
    conn: sqlite3.Connection,
    batch_id: str,
    ledger_id: str,
    reason: str = "Timeout or connection dropped"
):
    """结果未知：保守计入预算持有（严格幂等）"""
    cur = conn.cursor()
    cur.execute("SELECT status FROM budget_ledger WHERE id = ? AND batch_id = ?", (ledger_id, batch_id))
    row = cur.fetchone()
    if not row or row["status"] == "unknown_held":
        return
    if row["status"] != "reserved":
        return

    conn.execute("""
        UPDATE budget_ledger 
        SET status = 'unknown_held', note = ?
        WHERE id = ? AND batch_id = ? AND status = 'reserved'
    """, (f"Held unknown: {reason}", ledger_id, batch_id))

def resolve_unknown_budget(
    conn: sqlite3.Connection,
    batch_id: str,
    ledger_id: str,
    is_success: bool
):
    """人工处置结果未知任务后，更新账本状态（严格幂等）"""
    cur = conn.cursor()
    cur.execute("SELECT status, delta_reserved, delta_used FROM budget_ledger WHERE id = ? AND batch_id = ?", (ledger_id, batch_id))
    row = cur.fetchone()
    if not row or row["status"] != "unknown_held":
        return

    if is_success:
        amount = row["delta_reserved"] if row["delta_reserved"] > 0 else (row["delta_used"] if row["delta_used"] > 0 else 1)
        conn.execute("""
            UPDATE budget_ledger 
            SET delta_reserved = 0, delta_used = ?, status = 'settled', note = 'Confirmed success by operator/leader'
            WHERE id = ? AND batch_id = ? AND status = 'unknown_held'
        """, (amount, ledger_id, batch_id))
    else:
        conn.execute("""
            UPDATE budget_ledger 
            SET delta_reserved = 0, delta_used = 0, status = 'released', note = 'Confirmed failure by operator/leader, released'
            WHERE id = ? AND batch_id = ? AND status = 'unknown_held'
        """, (ledger_id, batch_id))
