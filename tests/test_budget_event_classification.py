import uuid

from backend.app.budget_service import (
    get_batch_budget_summary,
    release_budget_failed,
    reserve_budget,
)
from backend.app.database import get_db


def _insert_batch(batch_id):
    with get_db(immediate=True) as conn:
        conn.execute("""
            INSERT INTO batches (id, name, enabled_series, target_archived_count)
            VALUES (?, 'event classification', '["modern_business"]', 1)
        """, (batch_id,))


def test_summary_counts_multiple_explicit_retries_and_failures_individually(client):
    batch_id = f"bgt_event_{uuid.uuid4().hex[:10]}"
    _insert_batch(batch_id)

    with get_db(immediate=True) as conn:
        reserved_ids = [
            reserve_budget(conn, batch_id=batch_id, task_id=f"task_{index}")
            for index in range(5)
        ]
        # Existing compatibility path: plain released rows with a failure reason.
        release_budget_failed(conn, batch_id, reserved_ids[0], reason="HTTP 401 Unauthorized")
        release_budget_failed(conn, batch_id, reserved_ids[1], reason="HTTP 403 Forbidden")
        release_budget_failed(conn, batch_id, reserved_ids[2], reason="Content policy rejected")

        conn.execute("""
            INSERT INTO budget_ledger (
                id, batch_id, category, delta_reserved, delta_used, delta_unsettled,
                status, ledger_event_type, reference_task_id, note
            ) VALUES (?, ?, 'image_generation', 0, 0, 1, 'released', 'technical_retry', ?, 'Technical 429 retry backoff')
        """, (f"retry_{uuid.uuid4().hex[:8]}", batch_id, "task_retry_1"))
        conn.execute("""
            INSERT INTO budget_ledger (
                id, batch_id, category, delta_reserved, delta_used, delta_unsettled,
                status, ledger_event_type, reference_task_id, note
            ) VALUES (?, ?, 'image_generation', 0, 0, 1, 'released', 'technical_retry', ?, 'Technical 429 retry backoff')
        """, (f"retry_{uuid.uuid4().hex[:8]}", batch_id, "task_retry_2"))

        summary = get_batch_budget_summary(conn, batch_id)

    assert summary.explicit_failure_count == 3
    assert summary.failed_retry_count == 3
    assert summary.technical_retry_count == 2
    assert summary.image_generation_count == 2
    assert summary.reserved_count == 2
    assert summary.used_count == 0
    assert summary.unknown_count == 0


def test_existing_legacy_released_notes_are_backfilled_consistently(client):
    batch_id = f"bgt_legacy_{uuid.uuid4().hex[:10]}"
    _insert_batch(batch_id)
    with get_db(immediate=True) as conn:
        for index in range(4):
            conn.execute("""
                INSERT INTO budget_ledger (
                    id, batch_id, category, delta_reserved, delta_used, delta_unsettled,
                    status, reference_task_id, note
                ) VALUES (?, ?, 'image_generation', 0, 0, 1, 'released', ?, ?)
            """, (f"legacy_{index}_{uuid.uuid4().hex[:6]}", batch_id, f"task_{index}", "Legacy failure"))
        conn.execute("""
            INSERT INTO budget_ledger (
                id, batch_id, category, delta_reserved, delta_used, delta_unsettled,
                status, reference_task_id, note
            ) VALUES (?, ?, 'image_generation', 0, 0, 1, 'released', ?, 'Technical 429 retry backoff')
        """, (f"legacy_retry_{uuid.uuid4().hex[:6]}", batch_id, "task_retry"))

    # Init/backfill is part of app startup; invoke the same migration path explicitly.
    from backend.app.database import _ensure_columns
    with get_db(immediate=True) as conn:
        _ensure_columns(conn)
        summary = get_batch_budget_summary(conn, batch_id)

    assert summary.explicit_failure_count == 4
    assert summary.technical_retry_count == 1
