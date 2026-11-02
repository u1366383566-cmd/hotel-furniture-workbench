"""Final acceptance round: regressions for defects found in the go-live audit.

Each test reproduces one audited defect against the real API state machine before
the fix and locks the corrected behavior afterwards.
"""
import json
import uuid

import pytest

from backend.app.database import get_db
from backend.app.task_worker import worker_instance


def create_batch(client, **overrides):
    payload = {
        "name": "Final acceptance batch",
        "enabled_series": ["modern_business"],
        "target_archived_count": 1,
        "designs_per_series": 1,
        "standby_designs_per_series": 0,
        "warning_budget": 20,
        "hard_limit_budget": 25,
        "auto_start_panorama": True,
    }
    payload.update(overrides)
    response = client.post("/api/batches", json=payload, headers={"X-User-Role": "leader"})
    assert response.status_code == 200, response.text
    return response.json()


def run_queued_tasks(batch_id):
    while True:
        with get_db(immediate=False) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE batch_id = ? AND status = 'queued'",
                (batch_id,),
            ).fetchone()["c"]
        if not remaining:
            return
        worker_instance.process_next_task()


def get_designs(client, batch_id):
    response = client.get(f"/api/designs/batch/{batch_id}")
    assert response.status_code == 200, response.text
    return response.json()


def approve_panorama(client, design):
    pano = design["slots"]["panorama"]
    response = client.post(f"/api/designs/{design['id']}/review_panorama", json={
        "action": "approve",
        "expected_version_id": pano["current_version"]["id"],
    })
    assert response.status_code == 200, response.text


# --- B1: panorama tab must not render designs already past first-pass review ---
def test_panorama_tab_filter_matches_review_stage():
    from pathlib import Path
    app_js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    start = app_js.index("function renderPanoramaTab")
    end = app_js.index("function buildQuadVersionSnapshot")
    body = app_js[start:end]
    assert "['draft', 'panorama_generating', 'panorama_review'].includes(d.status)" in body, (
        "renderPanoramaTab must use the same stage filter as the badge count; "
        "quad-stage designs must not reappear with redo/eliminate buttons"
    )


# --- B2: downstream versions restarted after panorama replacement get new numbers ---
def test_start_downstream_version_numbers_increment_after_replacement(client):
    batch = create_batch(client)
    batch_id = batch["id"]
    run_queued_tasks(batch_id)
    design = get_designs(client, batch_id)[0]
    design_id = design["id"]
    pano_slot = design["slots"]["panorama"]

    # First-pass review: redo once so a second completed panorama version exists.
    first_redo = client.post(f"/api/designs/{design_id}/review_panorama", json={
        "action": "redo",
        "expected_version_id": pano_slot["current_version"]["id"],
        "reason_category": "构图不符",
    })
    assert first_redo.status_code == 200, first_redo.text
    run_queued_tasks(batch_id)

    designs = get_designs(client, batch_id)
    design = designs[0]
    pano_slot = design["slots"]["panorama"]
    assert len(pano_slot["history_versions"]) == 2
    first_version_id = [v["id"] for v in pano_slot["history_versions"] if v["id"] != pano_slot["current_version"]["id"]][0]

    # Approve the current (second) panorama, generate downstream, then the leader
    # replaces the panorama with the first completed version.
    approve_panorama(client, design)
    client.post(f"/api/designs/{design_id}/start_downstream")
    run_queued_tasks(batch_id)

    replaced = client.post(f"/api/slots/designs/{design_id}/replace_panorama", json={
        "new_panorama_version_id": first_version_id,
        "reason": "leader controlled replacement",
    }, headers={"X-User-Role": "leader"})
    assert replaced.status_code == 200, replaced.text

    restarted = client.post(f"/api/designs/{design_id}/start_downstream")
    assert restarted.status_code == 200, restarted.text
    run_queued_tasks(batch_id)

    with get_db(immediate=False) as conn:
        nums = conn.execute(
            """SELECT s.slot_type, sv.version_num FROM slots s
               JOIN slot_versions sv ON sv.slot_id = s.id
               WHERE s.design_id = ? AND s.slot_type != 'panorama'
               ORDER BY s.slot_type, sv.version_num""",
            (design_id,),
        ).fetchall()
    for slot_type in ("white_isolated", "twin_bed", "nightstand_closeup"):
        slot_nums = [r["version_num"] for r in nums if r["slot_type"] == slot_type]
        assert len(slot_nums) == len(set(slot_nums)), (
            f"{slot_type} has duplicate version numbers after restart: {slot_nums}"
        )
        assert max(slot_nums) == 2, f"{slot_type} restarted version should be v2, got {slot_nums}"


# --- B3: eliminate must commit even when the standby wake hits the budget guard ---
def test_eliminate_commits_when_standby_wake_blocked_by_warning(client):
    batch = create_batch(
        client,
        warning_budget=1,
        hard_limit_budget=25,
        standby_designs_per_series=1,
    )
    batch_id = batch["id"]
    run_queued_tasks(batch_id)
    design = get_designs(client, batch_id)[0]

    response = client.post(f"/api/designs/{design['id']}/review_panorama", json={
        "action": "eliminate",
        "expected_version_id": design["slots"]["panorama"]["current_version"]["id"],
        "reason_category": "结构错误",
    })
    assert response.status_code == 200, (
        f"eliminate must not be rolled back by the standby wake budget guard: {response.text}"
    )
    with get_db(immediate=False) as conn:
        status = conn.execute(
            "SELECT status FROM designs WHERE id = ?", (design["id"],)
        ).fetchone()["status"]
    assert status == "eliminated"

    # Leader approves crossing the warning line; the deferred standby may now wake.
    approved = client.post(f"/api/batches/{batch_id}/approve_warning", headers={"X-User-Role": "leader"})
    assert approved.status_code == 200, approved.text
    run_queued_tasks(batch_id)
    # A woken standby has is_standby cleared, so locate it by its standby code.
    all_designs = get_designs(client, batch_id)
    woken = [d for d in all_designs if d["design_code"].endswith("-STBY-01")]
    assert woken and woken[0]["status"] in ("panorama_generating", "panorama_review"), all_designs
    assert woken[0]["is_standby"] is False


# --- B4: circuit-broken batch must return clean errors, never HTTP 500 ---
def test_eliminate_commits_when_circuit_broken(client):
    batch = create_batch(client, standby_designs_per_series=1)
    batch_id = batch["id"]
    run_queued_tasks(batch_id)
    design = get_designs(client, batch_id)[0]

    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET is_circuit_broken = 1 WHERE id = ?", (batch_id,))

    response = client.post(f"/api/designs/{design['id']}/review_panorama", json={
        "action": "eliminate",
        "expected_version_id": design["slots"]["panorama"]["current_version"]["id"],
        "reason_category": "构图不符",
    })
    assert response.status_code == 200, response.text
    with get_db(immediate=False) as conn:
        status = conn.execute(
            "SELECT status FROM designs WHERE id = ?", (design["id"],)
        ).fetchone()["status"]
    assert status == "eliminated"


def test_start_downstream_clean_409_when_circuit_broken(client):
    batch = create_batch(client)
    batch_id = batch["id"]
    run_queued_tasks(batch_id)
    design = get_designs(client, batch_id)[0]
    approve_panorama(client, design)

    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET is_circuit_broken = 1 WHERE id = ?", (batch_id,))

    response = client.post(f"/api/designs/{design['id']}/start_downstream")
    assert response.status_code == 409, (
        f"circuit-broken dispatch must be a clean 409, got {response.status_code}: {response.text}"
    )


# --- B5: unknown or duplicated series must be a 400, not an unhandled 500 ---
def test_create_batch_rejects_unknown_series(client):
    response = client.post("/api/batches", json={
        "name": "Bad series batch",
        "enabled_series": ["modern_business", "does_not_exist"],
        "target_archived_count": 1,
    }, headers={"X-User-Role": "leader"})
    assert response.status_code == 400, f"expected 400, got {response.status_code}: {response.text}"

    duplicate = client.post("/api/batches", json={
        "name": "Duplicate series batch",
        "enabled_series": ["modern_business", "modern_business"],
        "target_archived_count": 1,
    }, headers={"X-User-Role": "leader"})
    assert duplicate.status_code == 400, (
        f"duplicated series must be rejected cleanly, got {duplicate.status_code}"
    )


# --- B6: unknown task listing/resolution must pin the reservation ledger row ---
def test_unknown_tasks_no_duplicate_rows_with_retry_ledger(client):
    batch = create_batch(client, auto_start_panorama=False)
    batch_id = batch["id"]
    did = f"des_regr_{uuid.uuid4().hex[:8]}"
    sid = f"slt_regr_{uuid.uuid4().hex[:8]}"
    vid = f"ver_regr_{uuid.uuid4().hex[:8]}"
    tid = f"tsk_regr_{uuid.uuid4().hex[:8]}"
    reservation = f"led_res_{uuid.uuid4().hex[:8]}"
    retry_row = f"led_try_{uuid.uuid4().hex[:8]}"
    with get_db(immediate=True) as conn:
        conn.execute(
            """INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec, status)
               VALUES (?, ?, 'modern_business', 'REGR-UNK', '{}', 'panorama_generating')""",
            (did, batch_id),
        )
        conn.execute(
            "INSERT INTO slots (id, design_id, slot_type, status) VALUES (?, ?, 'panorama', 'generating')",
            (sid, did),
        )
        conn.execute(
            """INSERT INTO slot_versions (id, slot_id, design_id, prompt, process_status, is_unknown)
               VALUES (?, ?, ?, 'seeded', 'unknown_pending_confirmation', 1)""",
            (vid, sid, did),
        )
        conn.execute(
            """INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status)
               VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'unknown')""",
            (tid, batch_id, did, sid, vid),
        )
        conn.execute(
            """INSERT INTO budget_ledger (id, batch_id, category, delta_reserved, status,
                   ledger_event_type, reference_task_id, note)
               VALUES (?, ?, 'image_generation', 1, 'unknown_held', 'generation_request', ?, 'dispatch')""",
            (reservation, batch_id, tid),
        )
        # A 429 technical-retry row for the same task must not duplicate the listing
        # nor be picked as the ledger to resolve.
        conn.execute(
            """INSERT INTO budget_ledger (id, batch_id, category, delta_unsettled, status,
                   ledger_event_type, reference_task_id, note)
               VALUES (?, ?, 'image_generation', 1, 'released', 'technical_retry', ?, 'Technical 429 retry backoff')""",
            (retry_row, batch_id, tid),
        )

    listing = client.get(f"/api/system/batches/{batch_id}/unknown_tasks").json()
    assert len(listing) == 1, f"unknown listing must not duplicate rows: {listing}"

    resolved = client.post("/api/system/resolve_unknown", json={
        "task_id": tid,
        "resolution": "confirm_failure",
        "evidence": "人工核对上游账单无扣费",
        "billing_reference": "BILL-REGR-0001",
        "attests_no_charge": True,
    }, headers={"X-User-Role": "leader"})
    assert resolved.status_code == 200, resolved.text
    with get_db(immediate=False) as conn:
        reservation_status = conn.execute(
            "SELECT status FROM budget_ledger WHERE id = ?", (reservation,)
        ).fetchone()["status"]
    assert reservation_status == "released", f"reservation must be released, got {reservation_status}"


# --- B7: an approved panorama is locked; redo must not bypass leader replacement ---
def test_approved_panorama_blocks_operator_redo_paths(client):
    batch = create_batch(client)
    batch_id = batch["id"]
    run_queued_tasks(batch_id)
    design = get_designs(client, batch_id)[0]
    approve_panorama(client, design)
    design_id = design["id"]

    review_redo = client.post(f"/api/designs/{design_id}/review_panorama", json={
        "action": "redo",
        "expected_version_id": design["slots"]["panorama"]["current_version"]["id"],
        "reason_category": "构图不符",
    })
    assert review_redo.status_code == 400, (
        f"approved panorama must not be redoable through first-pass review: {review_redo.text}"
    )

    pano_slot = design["slots"]["panorama"]["id"]
    slot_redo = client.post(f"/api/slots/{pano_slot}/redo", json={
        "slot_id": pano_slot,
        "expected_version_id": design["slots"]["panorama"]["current_version"]["id"],
        "reason_category": "构图不符",
    })
    assert slot_redo.status_code == 400, (
        f"approved panorama must not be redoable through slot redo: {slot_redo.text}"
    )

    # The leader-controlled replacement path stays available and is audited.
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE slots SET status = 'ready' WHERE id = ?", (pano_slot,))
    with get_db(immediate=False) as conn:
        current_vid = conn.execute(
            "SELECT current_version_id FROM slots WHERE id = ?", (pano_slot,)
        ).fetchone()["current_version_id"]
    replace_denied = client.post(f"/api/slots/designs/{design_id}/replace_panorama", json={
        "new_panorama_version_id": current_vid,
        "reason": "self replace attempt",
    })
    assert replace_denied.status_code == 403, replace_denied.text


# --- B8: production trusted_proxy mode must reject missing identity headers ---
def test_trusted_proxy_mode_rejects_missing_identity(client, monkeypatch):
    from backend.app import security

    monkeypatch.setattr(security, "AUTH_MODE", "trusted_proxy")
    try:
        anonymous = client.get("/api/batches")
        assert anonymous.status_code == 401, anonymous.text
        forged = client.get("/api/batches", headers={"X-User-Role": "not_a_role", "X-User-ID": "u1"})
        assert forged.status_code == 403, forged.text
        identified = client.get("/api/batches", headers={"X-User-Role": "leader", "X-User-ID": "u1"})
        assert identified.status_code == 200, identified.text
    finally:
        monkeypatch.setattr(security, "AUTH_MODE", "local_sim")

    fallback = client.get("/api/batches")
    assert fallback.status_code == 200, "local_sim 模式缺失身份头回退 operator"


# --- B9: export records must be listable per batch (导出记录可追踪) ---
def test_export_records_listing(client):
    batch = create_batch(client, auto_start_panorama=False)
    batch_id = batch["id"]
    with get_db(immediate=True) as conn:
        conn.execute("""
            INSERT INTO export_records (id, batch_id, export_type, file_name, file_path, file_size,
                                        exported_by, exported_role)
            VALUES ('exp_regr_1', ?, 'batch_archived_zip', 'export_demo.zip', '/tmp/export_demo.zip', 1234,
                    'acceptance', 'operator')
        """, (batch_id,))
    listing = client.get(f"/api/export/records/{batch_id}")
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert len(rows) == 1 and rows[0]["file_name"] == "export_demo.zip"
    assert rows[0]["exported_role"] == "operator" and rows[0]["file_size"] == 1234
