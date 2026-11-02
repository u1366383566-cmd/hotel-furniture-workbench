import pytest
import uuid
import concurrent.futures
from backend.app.database import get_db
from backend.app.budget_service import (
    reserve_budget, settle_budget_success, release_budget_failed,
    hold_budget_unknown, get_batch_budget_summary,
    BudgetWarningExceededError, BudgetHardLimitExceededError
)

def test_budget_reservation_and_limits(client):
    # 1. 创建批次，预警线 20，硬上限 25
    batch_id = f"test_bgt_{uuid.uuid4().hex[:8]}"
    with get_db(immediate=True) as conn:
        conn.execute("""
            INSERT INTO batches (id, name, enabled_series, target_archived_count, warning_budget, hard_limit_budget)
            VALUES (?, 'Budget Test Batch', '["modern_business"]', 3, 20, 25)
        """, (batch_id,))

    # 2. 连续预占至 20
    with get_db(immediate=True) as conn:
        for i in range(20):
            lid = reserve_budget(conn, batch_id=batch_id, amount=1, note=f"Task {i}")
            assert lid.startswith("bgt_")

    # 3. 第 21 次预占应触发 BudgetWarningExceededError（未获负责人审批）
    with pytest.raises(BudgetWarningExceededError):
        with get_db(immediate=True) as conn:
            reserve_budget(conn, batch_id=batch_id, amount=1, note="Task 21 over warning")

    # 4. 负责人明确批准跨越预警线
    with get_db(immediate=True) as conn:
        conn.execute("""
            UPDATE batches SET budget_warning_exceeded_approved_by = 'leader_user' WHERE id = ?
        """, (batch_id,))

    # 5. 批准后，继续预占至 25（硬上限）
    with get_db(immediate=True) as conn:
        for i in range(20, 25):
            lid = reserve_budget(conn, batch_id=batch_id, amount=1, note=f"Task {i}")
            assert lid.startswith("bgt_")

    # 6. 第 26 次预占必须触发 BudgetHardLimitExceededError（后端硬阻断，即便负责人批准也无法逾越）
    with pytest.raises(BudgetHardLimitExceededError):
        with get_db(immediate=True) as conn:
            reserve_budget(conn, batch_id=batch_id, amount=1, note="Task 26 over hard limit")

def test_budget_explicit_failure_and_unknown(client):
    batch_id = f"test_bgt_{uuid.uuid4().hex[:8]}"
    with get_db(immediate=True) as conn:
        conn.execute("""
            INSERT INTO batches (id, name, enabled_series, target_archived_count, warning_budget, hard_limit_budget)
            VALUES (?, 'Failure Test Batch', '["natural_wood"]', 3, 20, 25)
        """, (batch_id,))

    # 预占 2 笔
    with get_db(immediate=True) as conn:
        lid1 = reserve_budget(conn, batch_id=batch_id, amount=1, note="Test 1")
        lid2 = reserve_budget(conn, batch_id=batch_id, amount=1, note="Test 2")

    # 明确失败：释放预算
    with get_db(immediate=True) as conn:
        release_budget_failed(conn, batch_id, lid1, reason="HTTP 401 Unauthorized")
        summary = get_batch_budget_summary(conn, batch_id)
        # lid1 已释放，当前活跃仅 1 (lid2)
        assert summary.reserved_count == 1
        assert summary.failed_retry_count == 1

    # 结果未知：保守计入预算持有
    with get_db(immediate=True) as conn:
        hold_budget_unknown(conn, batch_id, lid2, reason="Timeout")
        summary = get_batch_budget_summary(conn, batch_id)
        assert summary.unknown_count == 1
        # 活跃计数包含 unknown
        assert (summary.used_count + summary.reserved_count + summary.unknown_count) == 1

def test_concurrent_budget_reservations(client):
    """并发预占测试：测试高并发事务下严格不发生超售"""
    batch_id = f"test_conc_{uuid.uuid4().hex[:8]}"
    limit = 10
    with get_db(immediate=True) as conn:
        conn.execute("""
            INSERT INTO batches (id, name, enabled_series, target_archived_count, warning_budget, hard_limit_budget)
            VALUES (?, 'Concurrent Batch', '["soft_contemporary"]', 3, 8, ?)
        """, (batch_id, limit))
        # 负责人特批预警线，测试能否精准停在硬上限
        conn.execute("UPDATE batches SET budget_warning_exceeded_approved_by = 'leader' WHERE id = ?", (batch_id,))

    success_count = 0
    blocked_count = 0

    def try_reserve():
        try:
            with get_db(immediate=True) as conn:
                reserve_budget(conn, batch_id=batch_id, amount=1)
                return True
        except (BudgetWarningExceededError, BudgetHardLimitExceededError):
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(try_reserve) for _ in range(20)]
        for f in concurrent.futures.as_completed(futures):
            if f.result():
                success_count += 1
            else:
                blocked_count += 1

    assert success_count == limit
    assert blocked_count == 10
