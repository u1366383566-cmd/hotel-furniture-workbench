import uuid
from types import SimpleNamespace
from pathlib import Path

from PIL import Image

from backend.app.config import EXPORTS_DIR, RAW_IMAGES_DIR, PREVIEW_IMAGES_DIR
from backend.app.database import get_db
from backend.app.task_worker import worker_instance
from quad_snapshot import quad_version_snapshot, REQUIRED_SLOTS


def _create_batch(client, name, target=1, standby=0, auto=True):
    res = client.post("/api/batches", json={
        "name": name,
        "enabled_series": ["modern_business"],
        "target_archived_count": target,
        "designs_per_series": 1,
        "standby_designs_per_series": standby,
        "auto_start_panorama": auto,
    }, headers={"X-User-Role": "leader"})
    assert res.status_code == 200
    return res.json()


def _make_quad_ready(client, batch_id):
    worker_instance.process_next_task()
    designs = client.get(f"/api/designs/batch/{batch_id}").json()
    design = designs[0]
    pano = design["slots"]["panorama"]
    client.post(f"/api/designs/{design['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": pano["current_version"]["id"]
    })
    client.post(f"/api/designs/{design['id']}/start_downstream")
    for _ in range(3):
        worker_instance.process_next_task()
    return client.get(f"/api/designs/{design['id']}").json()


def _archive_one(client, batch_id):
    ready = _make_quad_ready(client, batch_id)
    archive = client.post(f"/api/designs/{ready['id']}/review_quad", json={
        "action": "archive",
        "expected_slot_versions": quad_version_snapshot(ready),
    })
    assert archive.status_code == 200
    return client.get(f"/api/designs/{ready['id']}").json()


def test_series_rule_revision_affects_future_and_freezes_existing(client):
    old_rule = "Twin rule locked into batch template"
    updated = client.put("/api/system/series_rules/modern_business", json={
        "twin_adapter_rule": old_rule
    }, headers={"X-User-Role": "leader"})
    assert updated.status_code == 200
    locked_version = updated.json()["rule"]["template_version"]

    batch = _create_batch(client, "Rule revision planning batch", auto=False)
    design = client.get(f"/api/designs/batch/{batch['id']}").json()[0]
    assert design["structured_spec"]["twin_adapter_rule"] == old_rule
    assert design["structured_spec"]["template_version"] == locked_version
    assert design["structured_spec"]["template_version"] != "tpl_2026_v1.0"

    client.put("/api/system/series_rules/modern_business", json={
        "twin_adapter_rule": "Later rule must not mutate frozen designs"
    }, headers={"X-User-Role": "leader"})
    frozen = client.get(f"/api/designs/{design['id']}").json()
    assert frozen["structured_spec"]["twin_adapter_rule"] == old_rule
    assert frozen["structured_spec"]["template_version"] == locked_version


def test_circuit_trial_mode_really_limits_dispatch(client):
    batch = _create_batch(client, "Trial dispatch limit batch")
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE batches SET trial_mode = 1, trial_run_remaining = 1 WHERE id = ?", (batch["id"],))

    worker_instance.process_next_task()
    with get_db(immediate=False) as conn:
        state = conn.execute(
            "SELECT trial_mode, trial_run_remaining, is_circuit_broken, status, pause_reason FROM batches WHERE id = ?",
            (batch["id"],),
        ).fetchone()
    assert state["trial_run_remaining"] == 0
    assert state["is_circuit_broken"] == 1
    assert state["pause_reason"] == "CIRCUIT_BROKEN"


def test_generation_versions_inherit_frozen_design_template(client):
    updated = client.put("/api/system/series_rules/modern_business", json={
        "white_bg_rule": "Inherited by all generated versions"
    }, headers={"X-User-Role": "leader"})
    assert updated.status_code == 200
    template_version = updated.json()["rule"]["template_version"]

    batch = _create_batch(client, "Template inheritance batch")
    worker_instance.process_next_task()
    design = client.get(f"/api/designs/batch/{batch['id']}").json()[0]
    assert design["structured_spec"]["template_version"] == template_version
    assert design["slots"]["panorama"]["current_version"]["prompt"]
    with get_db(immediate=False) as conn:
        stored = conn.execute(
            "SELECT template_version FROM slot_versions WHERE design_id = ?", (design["id"],)
        ).fetchall()
    assert all(row["template_version"] == template_version for row in stored)

    client.post(f"/api/designs/{design['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": design["slots"]["panorama"]["current_version"]["id"]
    })
    client.post(f"/api/designs/{design['id']}/start_downstream")
    downstream = client.get(f"/api/designs/{design['id']}").json()
    for slot_name in ("white_isolated", "twin_bed", "nightstand_closeup"):
        slot = downstream["slots"][slot_name]
        assert slot["current_version"]["id"]
        with get_db(immediate=False) as conn:
            row = conn.execute(
                "SELECT template_version, depends_on_panorama_version_id FROM slot_versions WHERE id = ?",
                (slot["current_version"]["id"],),
            ).fetchone()
        assert row["template_version"] == template_version


def test_replaced_panorama_discards_inflight_downstream_result(client):
    batch = _create_batch(client, "Inflight replacement batch")
    worker_instance.process_next_task()
    design = client.get(f"/api/designs/batch/{batch['id']}").json()[0]
    pano_slot = design["slots"]["panorama"]
    old_pano_id = pano_slot["current_version"]["id"]

    # First-pass redo creates a second completed panorama before the first is locked.
    redo = client.post(f"/api/designs/{design['id']}/review_panorama", json={
        "action": "redo",
        "expected_version_id": old_pano_id,
        "reason_category": "风格不符",
    })
    assert redo.status_code == 200
    worker_instance.process_next_task()
    refreshed = client.get(f"/api/designs/{design['id']}").json()
    new_pano_id = refreshed["slots"]["panorama"]["current_version"]["id"]
    assert new_pano_id != old_pano_id

    client.post(f"/api/designs/{design['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": new_pano_id
    })
    client.post(f"/api/designs/{design['id']}/start_downstream")

    # Simulate the normal cross-entry pause window: downstream tasks are already
    # in flight while a leader replaces the approved panorama.
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE tasks SET status = 'running' WHERE design_id = ? AND task_type = 'generate_downstream'", (design["id"],))

    replace = client.post(f"/api/slots/designs/{design['id']}/replace_panorama", json={
        "new_panorama_version_id": old_pano_id,
        "reason": "Leader approved replacement during in-flight downstream work",
    }, headers={"X-User-Role": "leader"})
    assert replace.status_code == 200, replace.json()

    with get_db(immediate=True) as conn:
        rows = conn.execute("""
            SELECT t.id, t.batch_id, t.design_id, t.slot_id, t.slot_version_id, t.task_type, t.retry_count,
                   d.series_id, d.design_code, d.structured_spec, d.status as design_status,
                   d.template_version as design_tpl_ver,
                   s.slot_type, s.current_version_id as slot_current_vid,
                   sv.version_num, sv.prompt, sv.is_invalidated,
                   sv.depends_on_panorama_version_id
            FROM tasks t
            JOIN designs d ON t.design_id = d.id
            JOIN slots s ON t.slot_id = s.id
            JOIN slot_versions sv ON t.slot_version_id = sv.id
            WHERE t.design_id = ? AND t.task_type = 'generate_downstream' AND t.status = 'running'
        """, (design["id"],)).fetchall()
        assert rows
    # Release the test transaction before invoking the worker's own write path.
    for row in rows:
        worker_instance._execute_single_task(row)

    with get_db(immediate=False) as conn:
        versions = conn.execute("""
            SELECT sv.is_invalidated, sv.process_status, s.status as slot_status
            FROM slot_versions sv JOIN slots s ON sv.slot_id = s.id
            WHERE sv.design_id = ? AND s.slot_type != 'panorama'
        """, (design["id"],)).fetchall()
    assert all(row["is_invalidated"] == 1 for row in versions)
    assert all(row["slot_status"] == "invalidated" for row in versions)


def test_stale_task_never_replaces_changed_slot_pointer(client):
    batch = _create_batch(client, "Stale pointer guard batch", auto=False)
    with get_db(immediate=True) as conn:
        did = f"des_gap_{uuid.uuid4().hex[:8]}"
        sid = f"slt_gap_{uuid.uuid4().hex[:8]}"
        current_vid = f"ver_current_{uuid.uuid4().hex[:8]}"
        stale_vid = f"ver_stale_{uuid.uuid4().hex[:8]}"
        tid = f"tsk_gap_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec, status) VALUES (?, ?, 'modern_business', 'GAP-STALE', '{}', 'panorama_generating')",
            (did, batch["id"]),
        )
        conn.execute(
            "INSERT INTO slots (id, design_id, slot_type, current_version_id, status) VALUES (?, ?, 'panorama', ?, 'ready')",
            (sid, did, current_vid),
        )
        conn.execute(
            "INSERT INTO slot_versions (id, slot_id, design_id, prompt, process_status) VALUES (?, ?, ?, 'valid current', 'completed')",
            (current_vid, sid, did),
        )
        conn.execute(
            "INSERT INTO slot_versions (id, slot_id, design_id, prompt, process_status) VALUES (?, ?, ?, 'stale queued', 'pending')",
            (stale_vid, sid, did),
        )
        conn.execute(
            "INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'queued')",
            (tid, batch["id"], did, sid, stale_vid),
        )

    worker_instance.process_next_task()
    with get_db(immediate=False) as conn:
        row = conn.execute("SELECT current_version_id, status FROM slots WHERE id = ?", (sid,)).fetchone()
        assert row["current_version_id"] == current_vid
        assert row["status"] == "ready"


def test_unknown_failure_requires_explicit_human_ledger_attestation(client):
    batch_id = f"b_gap_unknown_{uuid.uuid4().hex[:8]}"
    with get_db(immediate=True) as conn:
        conn.execute("INSERT INTO batches (id, name, enabled_series, target_archived_count) VALUES (?, ?, '[\"modern_business\"]', 1)", (batch_id, "Gap Unknown"))
        did = f"d_gap_unknown_{uuid.uuid4().hex[:8]}"
        sid = f"s_gap_unknown_{uuid.uuid4().hex[:8]}"
        vid = f"v_gap_unknown_{uuid.uuid4().hex[:8]}"
        tid = f"t_gap_unknown_{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec) VALUES (?, ?, 'modern_business', 'GAP-UNKNOWN', '{}')", (did, batch_id))
        conn.execute("INSERT INTO slots (id, design_id, slot_type) VALUES (?, ?, 'panorama')", (sid, did))
        conn.execute("INSERT INTO slot_versions (id, slot_id, design_id, prompt, process_status, is_unknown) VALUES (?, ?, ?, 'timeout', 'unknown_pending_confirmation', 1)", (vid, sid, did))
        conn.execute("INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'unknown')", (tid, batch_id, did, sid, vid))

    weak = client.post("/api/system/resolve_unknown", json={
        "task_id": tid,
        "resolution": "confirm_failure",
        "evidence": "上游账单核对无此扣费流水",
    }, headers={"X-User-Role": "leader"})
    assert weak.status_code == 400

    resolved = client.post("/api/system/resolve_unknown", json={
        "task_id": tid,
        "resolution": "confirm_failure",
        "evidence": "人工查询账单流水后确认无扣费",
        "billing_reference": "BILL-GAP-000001",
        "attests_no_charge": True,
    }, headers={"X-User-Role": "leader"})
    assert resolved.status_code == 200

    logs_response = client.get(f"/api/system/batches/{batch_id}/audit_logs", headers={"X-User-Role": "leader"})
    assert logs_response.status_code == 200
    logs = logs_response.json()
    entry = next(item for item in logs if item["action"] == "resolve_unknown_task")
    detail = __import__("json").loads(entry["detail"])
    assert detail["human_attested_no_charge"] is True
    assert detail["verification_level"] == "human_ledger_attestation"


def test_archive_rejects_corrupt_snapshot_and_export_has_no_partial_zip(client):
    batch = _create_batch(client, "Corrupt archive snapshot batch")
    ready = _make_quad_ready(client, batch["id"])

    downstream = ready["slots"]["white_isolated"]
    raw_name = downstream["current_version"]["preview_image_path"].replace("_preview.jpg", "_raw.png")
    raw_path = RAW_IMAGES_DIR / raw_name
    assert raw_path.exists()
    valid_bytes = raw_path.read_bytes()
    raw_path.write_bytes(b"corrupt-not-a-png")

    rejected = client.post(f"/api/designs/{ready['id']}/review_quad", json={
        "action": "archive",
        "expected_slot_versions": quad_version_snapshot(ready),
    })
    assert rejected.status_code == 400
    assert "损坏" in rejected.json()["detail"]

    raw_path.write_bytes(valid_bytes)
    recovered_archive = client.post(f"/api/designs/{ready['id']}/review_quad", json={
        "action": "archive",
        "expected_slot_versions": quad_version_snapshot(ready),
    })
    assert recovered_archive.status_code == 200
    raw_path.write_bytes(b"corrupt-not-a-png")

    zips_before = {p.name for p in EXPORTS_DIR.glob("*.zip")}
    export = client.post(f"/api/export/design/{ready['id']}")
    assert export.status_code == 400
    assert {p.name for p in EXPORTS_DIR.glob("*.zip")} == zips_before


def test_quad_archive_validates_real_snapshot_state_and_unknown_tasks(client):
    batch = _create_batch(client, "Quad snapshot validation batch")
    ready = _make_quad_ready(client, batch["id"])
    design_id = ready["id"]
    payload = {"action": "archive", "expected_slot_versions": quad_version_snapshot(ready)}

    missing_snapshot = client.post(f"/api/designs/{design_id}/review_quad", json={"action": "archive"})
    assert missing_snapshot.status_code == 422

    stale = dict(payload)
    stale["expected_slot_versions"] = {
        **payload["expected_slot_versions"],
        "white_isolated": "ver_seen_before_concurrent_change",
    }
    stale_response = client.post(f"/api/designs/{design_id}/review_quad", json=stale)
    assert stale_response.status_code == 409
    assert "版本已变更" in stale_response.json()["detail"]

    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE slot_versions SET process_status = 'processing' WHERE id = ?",
            (ready["slots"]["white_isolated"]["current_version"]["id"],),
        )
    processing = client.post(f"/api/designs/{design_id}/review_quad", json=payload)
    assert processing.status_code == 400
    assert "处理状态" in processing.json()["detail"]

    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE slot_versions SET process_status = 'completed', is_unknown = 1 WHERE id = ?",
            (ready["slots"]["white_isolated"]["current_version"]["id"],),
        )
    unknown_current = client.post(f"/api/designs/{design_id}/review_quad", json=payload)
    assert unknown_current.status_code == 400
    assert "结果未知" in unknown_current.json()["detail"]

    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE slot_versions SET is_unknown = 0 WHERE id = ?",
            (ready["slots"]["white_isolated"]["current_version"]["id"],),
        )
        stale_unknown_id = f"t_unknown_{uuid.uuid4().hex[:8]}"
        stale_unknown_version = f"v_unknown_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO slot_versions (id, slot_id, design_id, prompt, process_status, is_unknown) VALUES (?, ?, ?, 'old unknown', 'unknown_pending_confirmation', 1)",
            (stale_unknown_version, ready["slots"]["white_isolated"]["id"], design_id),
        )
        conn.execute(
            "INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_downstream', 'unknown')",
            (stale_unknown_id, batch["id"], design_id, ready["slots"]["white_isolated"]["id"], stale_unknown_version),
        )
    unresolved_unknown = client.post(f"/api/designs/{design_id}/review_quad", json=payload)
    assert unresolved_unknown.status_code == 400
    assert "结果未知任务" in unresolved_unknown.json()["detail"]

    with get_db(immediate=True) as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (stale_unknown_id,))
        conn.execute("UPDATE slots SET status = 'invalidated' WHERE design_id = ? AND slot_type = 'twin_bed'", (design_id,))
    bad_slot_state = client.post(f"/api/designs/{design_id}/review_quad", json=payload)
    assert bad_slot_state.status_code == 400
    assert "twin_bed" in bad_slot_state.json()["detail"]

    with get_db(immediate=True) as conn:
        conn.execute("UPDATE slots SET status = 'ready' WHERE design_id = ? AND slot_type = 'twin_bed'", (design_id,))
        conn.execute("UPDATE slots SET status = 'generating' WHERE design_id = ? AND slot_type = 'panorama'", (design_id,))
    unapproved_panorama = client.post(f"/api/designs/{design_id}/review_quad", json=payload)
    assert unapproved_panorama.status_code == 400
    assert "状态为 generating" in unapproved_panorama.json()["detail"]

    with get_db(immediate=True) as conn:
        conn.execute("UPDATE slots SET status = 'approved' WHERE design_id = ? AND slot_type = 'panorama'", (design_id,))
        conn.execute("UPDATE designs SET status = 'panorama_approved' WHERE id = ?", (design_id,))
    wrong_design_state = client.post(f"/api/designs/{design_id}/review_quad", json=payload)
    assert wrong_design_state.status_code == 400
    assert "quad_review" in wrong_design_state.json()["detail"]

    with get_db(immediate=True) as conn:
        conn.execute("UPDATE designs SET status = 'quad_review' WHERE id = ?", (design_id,))
    preview_path = PREVIEW_IMAGES_DIR / ready["slots"]["twin_bed"]["current_version"]["preview_image_path"]
    preview_path.write_bytes(b"corrupt-preview")
    bad_preview = client.post(f"/api/designs/{design_id}/review_quad", json=payload)
    assert bad_preview.status_code == 400
    assert "预览图损坏" in bad_preview.json()["detail"]

    preview_path.unlink(missing_ok=True)


def test_worker_rereads_invalidation_even_when_task_row_snapshot_is_stale(client):
    batch = _create_batch(client, "Worker realtime invalidation batch")
    worker_instance.process_next_task()
    design = client.get(f"/api/designs/batch/{batch['id']}").json()[0]
    pano_id = design["slots"]["panorama"]["current_version"]["id"]
    client.post(f"/api/designs/{design['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": pano_id,
    })
    client.post(f"/api/designs/{design['id']}/start_downstream")

    with get_db(immediate=True) as conn:
        stale_rows = conn.execute("""
            SELECT t.id, t.batch_id, t.design_id, t.slot_id, t.slot_version_id, t.task_type, t.retry_count,
                   d.series_id, d.design_code, d.structured_spec, d.status as design_status,
                   d.template_version as design_tpl_ver,
                   s.slot_type, s.current_version_id as slot_current_vid,
                   sv.version_num, sv.prompt, sv.is_invalidated,
                   sv.depends_on_panorama_version_id
            FROM tasks t
            JOIN designs d ON t.design_id = d.id
            JOIN slots s ON t.slot_id = s.id
            JOIN slot_versions sv ON t.slot_version_id = sv.id
            WHERE t.design_id = ? AND t.task_type = 'generate_downstream' AND t.status = 'queued'
        """, (design["id"],)).fetchall()
        assert len(stale_rows) == 3
        # Keep dispatch-time snapshot false in the passed row, but force the durable
        # version to invalidated while active panorama still equals its dependency.
        for row in stale_rows:
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (row["id"],))
            conn.execute("UPDATE slot_versions SET is_invalidated = 1 WHERE id = ?", (row["slot_version_id"],))

    for stale_row in stale_rows:
        worker_instance._execute_single_task(stale_row)

    with get_db(immediate=False) as conn:
        rows = conn.execute("""
            SELECT sv.is_invalidated, sv.process_status, t.status as task_status
            FROM slot_versions sv
            JOIN tasks t ON t.slot_version_id = sv.id
            WHERE sv.design_id = ? AND t.task_type = 'generate_downstream'
        """, (design["id"],)).fetchall()
    assert len(rows) == 3
    assert all(row["is_invalidated"] == 1 for row in rows)
    assert all(row["process_status"] == "completed" for row in rows)
    assert all(row["task_status"] == "completed" for row in rows)


def test_frontend_has_no_leader_seeded_auto_production(client):
    # Source-level regression: an empty DB must not silently impersonate a leader
    # or expand production; the owner must explicitly configure and approve it.
    source = Path("static/app.js").read_text(encoding="utf-8")
    assert "createDefaultInitialBatch" not in source
    assert "首轮批次播种" not in source
    assert "由管理员手动配置并批准批次" in source
