import io
import json

import pytest
from PIL import Image

from backend.app import image_adapter
from backend.app.database import get_db
import backend.app.task_worker as worker_module
from backend.app.image_adapter import (
    ExplicitRateLimitError,
    ImageRequestTimeout,
)


def png_bytes():
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(image, format="PNG")
    return image.getvalue()


def create_one_design_batch(client):
    response = client.post(
        "/api/batches",
        headers={"X-User-Role": "leader"},
        json={
            "name": "自动重试测试",
            "enabled_series": ["modern_business"],
            "target_archived_count": 1,
            "designs_per_series": 1,
            "standby_designs_per_series": 0,
            "auto_start_panorama": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def enable_auto_retry():
    with get_db(immediate=True) as conn:
        conn.execute(
            """UPDATE workbench_settings
               SET auto_release_unknown=1,auto_retry_request_failures=1
               WHERE id='default'"""
        )


def make_retry_due():
    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE tasks SET retry_after=datetime('now','localtime','-1 second') WHERE status='queued'"
        )


def prepare_real_api(monkeypatch, errors, success_after_errors=True):
    calls = []

    def generate(*args, **kwargs):
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        if not success_after_errors:
            raise errors[-1]
        return png_bytes(), f"upstream_{len(calls)}"

    monkeypatch.setattr(
        worker_module,
        "load_image_api_config",
        lambda conn: {
            "enabled": True,
            "base_url": "https://images.example",
            "model": "image-model",
            "api_key": "secret",
            "timeout_seconds": 30,
        },
    )
    monkeypatch.setattr(worker_module, "generate_image_real", generate)
    return calls


def test_request_timeout_retries_twice_without_consuming_manual_redo(client, monkeypatch):
    batch = create_one_design_batch(client)
    enable_auto_retry()
    calls = prepare_real_api(
        monkeypatch,
        [
            ImageRequestTimeout("first timeout"),
            ImageRequestTimeout("second timeout"),
        ],
    )

    assert worker_module.worker_instance.process_next_task()
    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE batch_id=?", (batch["id"],)
        ).fetchone()
        assert task["status"] == "queued" and task["auto_retry_count"] == 1
    make_retry_due()

    assert worker_module.worker_instance.process_next_task()
    make_retry_due()
    assert worker_module.worker_instance.process_next_task()

    assert len(calls) == 3
    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE batch_id=?", (batch["id"],)
        ).fetchone()
        slot = conn.execute(
            "SELECT * FROM slots WHERE design_id=?", (task["design_id"],)
        ).fetchone()
        version = conn.execute(
            "SELECT * FROM slot_versions WHERE id=?", (task["slot_version_id"],)
        ).fetchone()
        attempts = conn.execute(
            "SELECT outcome FROM image_generation_attempts WHERE task_id=? ORDER BY attempt_no",
            (task["id"],),
        ).fetchall()
        ledgers = conn.execute(
            """SELECT status FROM budget_ledger
               WHERE reference_task_id=? ORDER BY created_at,id""",
            (task["id"],),
        ).fetchall()
        assert task["status"] == "completed" and task["auto_retry_count"] == 2
        assert slot["redo_count"] == 0 and version["process_status"] == "completed"
        assert [row["outcome"] for row in attempts] == [
            "retryable_failure",
            "retryable_failure",
            "success",
        ]
        assert [row["status"] for row in ledgers].count("released") == 2
        assert [row["status"] for row in ledgers].count("settled") == 1


def test_retry_exhaustion_releases_last_attempt_and_keeps_batch_active(client, monkeypatch):
    batch = create_one_design_batch(client)
    enable_auto_retry()
    timeout = ImageRequestTimeout("upstream timeout")
    prepare_real_api(monkeypatch, [timeout, timeout, timeout], success_after_errors=False)

    for _ in range(3):
        assert worker_module.worker_instance.process_next_task()
        make_retry_due()

    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE batch_id=?", (batch["id"],)
        ).fetchone()
        state = conn.execute(
            "SELECT status,pause_reason,is_circuit_broken FROM batches WHERE id=?",
            (batch["id"],),
        ).fetchone()
        ledgers = conn.execute(
            "SELECT status FROM budget_ledger WHERE reference_task_id=?",
            (task["id"],),
        ).fetchall()
        audit = conn.execute(
            """SELECT detail FROM audit_logs
               WHERE batch_id=? AND action='auto_retry_exhausted'""",
            (batch["id"],),
        ).fetchone()
        assert task["status"] == "failed"
        assert task["failure_stage"] == "request_charge_unknown"
        assert task["retry_status"] == "retry_exhausted"
        assert task["auto_retry_count"] == 2
        assert state["status"] == "active" and state["pause_reason"] is None
        assert {row["status"] for row in ledgers} == {"released"}
        assert json.loads(audit["detail"])["upstream_charge"] == "unknown"


def test_429_uses_automatic_retry_and_technical_metric(client, monkeypatch):
    batch = create_one_design_batch(client)
    enable_auto_retry()
    prepare_real_api(
        monkeypatch,
        [ExplicitRateLimitError("rate limited", "15")],
    )

    assert worker_module.worker_instance.process_next_task()
    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE batch_id=?", (batch["id"],)
        ).fetchone()
        slot = conn.execute(
            "SELECT * FROM slots WHERE id=?", (task["slot_id"],)
        ).fetchone()
        audit = conn.execute(
            """SELECT detail FROM audit_logs
               WHERE batch_id=? AND action='auto_schedule_image_retry'""",
            (batch["id"],),
        ).fetchone()
        assert task["status"] == "queued" and task["auto_retry_count"] == 1
        assert slot["technical_retry_count"] == 1
        assert json.loads(audit["detail"])["delay_seconds"] == 15

    make_retry_due()
    assert worker_module.worker_instance.process_next_task()
    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE batch_id=?", (batch["id"],)
        ).fetchone()
        assert task["status"] == "completed"


def test_queued_auto_retry_is_cancelled_when_slot_pointer_changes(client, monkeypatch):
    batch = create_one_design_batch(client)
    enable_auto_retry()
    prepare_real_api(monkeypatch, [ImageRequestTimeout("timeout")])
    assert worker_module.worker_instance.process_next_task()

    with get_db(immediate=True) as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE batch_id=?", (batch["id"],)
        ).fetchone()
        conn.execute(
            "UPDATE slots SET current_version_id='new-version' WHERE id=?",
            (task["slot_id"],),
        )
        conn.execute(
            "UPDATE tasks SET retry_after=datetime('now','localtime','-1 second') WHERE id=?",
            (task["id"],),
        )

    assert not worker_module.worker_instance.process_next_task()
    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
        assert task["status"] == "cancelled"
        assert task["retry_status"] == "stale_cancelled"


def test_legacy_request_failure_can_be_batch_retried_after_risk_ack(client, monkeypatch):
    batch = create_one_design_batch(client)
    with get_db(immediate=True) as conn:
        conn.execute(
            """UPDATE workbench_settings
               SET auto_release_unknown=1,auto_retry_request_failures=0
               WHERE id='default'"""
        )
    prepare_real_api(monkeypatch, [ImageRequestTimeout("legacy timeout")])
    assert worker_module.worker_instance.process_next_task()

    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE batch_id=?", (batch["id"],)
        ).fetchone()
    listing = client.get(
        f"/api/workbench/batches/{batch['id']}/technical-failures",
        headers={"X-User-Role": "operator", "X-User-ID": "artist"},
    )
    assert listing.status_code == 200
    assert listing.json()["count"] == 1
    assert listing.json()["items"][0]["task_id"] == task["id"]

    endpoint = f"/api/workbench/batches/{batch['id']}/retry-technical-failures"
    assert client.post(
        endpoint,
        json={"task_ids": [task["id"]], "accept_unknown_charge": True},
        headers={"X-User-Role": "operator"},
    ).status_code == 403
    assert client.post(
        endpoint,
        json={"task_ids": [task["id"]], "accept_unknown_charge": False},
        headers={"X-User-Role": "admin"},
    ).status_code == 400
    response = client.post(
        endpoint,
        json={"task_ids": [task["id"]], "accept_unknown_charge": True},
        headers={"X-User-Role": "admin", "X-User-ID": "owner"},
    )
    assert response.status_code == 200 and response.json()["results"][0]["ok"]
    with get_db() as conn:
        task_after = conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
        assert task_after["status"] == "queued"
        assert task_after["auto_retry_count"] == 0
        assert task_after["slot_version_id"] == task["slot_version_id"]


def test_compact_summary_excludes_history_and_returns_304(client):
    batch = create_one_design_batch(client)
    assert worker_module.worker_instance.process_next_task()

    full = client.get(f"/api/designs/batch/{batch['id']}")
    compact = client.get(f"/api/designs/batch/{batch['id']}/summary")
    assert full.status_code == compact.status_code == 200
    assert len(compact.content) < len(full.content)
    design = compact.json()[0]
    assert "history_versions" not in design["slots"]["panorama"]
    assert "prompt" not in design["slots"]["panorama"]["current_version"]
    cached = client.get(
        f"/api/designs/batch/{batch['id']}/summary",
        headers={"If-None-Match": compact.headers["etag"]},
    )
    assert cached.status_code == 304 and not cached.content
