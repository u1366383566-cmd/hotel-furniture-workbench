import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

from backend.app.config import PREVIEW_IMAGES_DIR, RAW_IMAGES_DIR
from backend.app.database import get_db
from backend.app.main import app
from fastapi.testclient import TestClient as TestClientInstance
from backend.app.task_worker import worker_instance


def create_batch(client, **overrides):
    payload = {
        "name": "Acceptance gap batch",
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


def run_all_panorama_tasks(client, batch_id):
    while True:
        with get_db(immediate=False) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE batch_id = ? AND task_type = 'generate_panorama' AND status = 'queued'",
                (batch_id,),
            ).fetchone()["c"]
        if not remaining:
            return
        worker_instance.process_next_task()


def approve_design(client, design):
    pano = design["slots"]["panorama"]
    response = client.post(f"/api/designs/{design['id']}/review_panorama", json={
        "action": "approve",
        "expected_version_id": pano["current_version"]["id"],
    })
    assert response.status_code == 200, response.text


def test_planning_and_vision_costs_do_not_enter_image_budget_warning(client):
    batch = create_batch(client, designs_per_series=6, auto_start_panorama=False)
    batch_id = batch["id"]

    forbidden = client.post(f"/api/batches/{batch_id}/start", headers={"X-User-Role": "operator"})
    assert forbidden.status_code == 403
    started = client.post(f"/api/batches/{batch_id}/start", headers={"X-User-Role": "leader"})
    assert started.status_code == 200, started.text
    repeated = client.post(f"/api/batches/{batch_id}/start", headers={"X-User-Role": "leader"})
    assert repeated.status_code == 409

    with get_db(immediate=False) as conn:
        initial_tasks = dict(conn.execute(
            "SELECT id, status FROM tasks WHERE batch_id = ? AND task_type = 'generate_panorama'", (batch_id,)
        ).fetchall())
    assert len(initial_tasks) == 6
    assert list(initial_tasks.values()).count("queued") == 6
    initial_budget = client.get(f"/api/batches/{batch_id}/budget").json()
    assert initial_budget["planning_model_count"] == 6
    assert initial_budget["reserved_count"] == 6
    assert initial_budget["used_count"] == 0
    assert initial_budget["image_generation_count"] == 6

    run_all_panorama_tasks(client, batch_id)
    with get_db(immediate=False) as conn:
        path_rows = conn.execute("SELECT sv.id, raw_image_path, preview_image_path, process_status FROM slot_versions sv JOIN designs d ON sv.design_id=d.id WHERE d.batch_id=?", (batch_id,)).fetchall()
    assert all(row["raw_image_path"] and row["preview_image_path"] and row["process_status"] == "completed" for row in path_rows), path_rows
    designs = client.get(f"/api/designs/batch/{batch_id}").json()
    for design in designs:
        approve_design(client, design)

    before_concurrency = client.get(f"/api/batches/{batch_id}/budget").json()
    assert before_concurrency["planning_model_count"] == 6
    assert before_concurrency["vision_model_count"] == 6
    assert before_concurrency["used_count"] == 6
    assert before_concurrency["reserved_count"] == 0
    assert before_concurrency["image_generation_count"] == 6

    def start_downstream(design):
        return client.post(f"/api/designs/{design['id']}/start_downstream")

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(start_downstream, design) for design in designs]
        responses = [future.result() for future in as_completed(futures)]

    succeeded = [response for response in responses if response.status_code == 200]
    warning_blocked = [response for response in responses if response.status_code == 423]
    assert len(succeeded) == 4
    assert len(warning_blocked) == 2

    final_budget = client.get(f"/api/batches/{batch_id}/budget").json()
    assert final_budget["planning_model_count"] == 6
    assert final_budget["vision_model_count"] == 6
    assert final_budget["used_count"] == 6
    assert final_budget["reserved_count"] == 12
    assert final_budget["image_generation_count"] == 18
    assert final_budget["warning_reached"] is False
    assert len(warning_blocked) == 2
    assert final_budget["hard_limit_reached"] is False

    with get_db(immediate=False) as conn:
        downstream_ids = [row["id"] for row in conn.execute(
            "SELECT id FROM tasks WHERE batch_id = ? AND task_type = 'generate_downstream'", (batch_id,)
        ).fetchall()]
        reserved_ledger_ids = [row["id"] for row in conn.execute("""
            SELECT id FROM budget_ledger
            WHERE batch_id = ? AND category = 'image_generation' AND status = 'reserved'
        """, (batch_id,)).fetchall()]
        planning_rows = conn.execute(
            "SELECT id, delta_used FROM budget_ledger WHERE batch_id = ? AND category = 'planning_model'", (batch_id,)
        ).fetchall()
    assert len(downstream_ids) == 12
    assert len(set(downstream_ids)) == 12
    assert len(reserved_ledger_ids) == 12
    assert len(planning_rows) == 6
    assert all(row["delta_used"] == 1 for row in planning_rows)


def test_approved_warning_concurrent_requests_stop_at_hard_limit(client):
    batch = create_batch(client, designs_per_series=7, auto_start_panorama=False)
    batch_id = batch["id"]
    started = client.post(f"/api/batches/{batch_id}/start", headers={"X-User-Role": "leader"})
    assert started.status_code == 200
    run_all_panorama_tasks(client, batch_id)
    designs = client.get(f"/api/designs/batch/{batch_id}").json()
    for design in designs:
        approve_design(client, design)

    approved = client.post(f"/api/batches/{batch_id}/approve_warning", headers={"X-User-Role": "leader"})
    assert approved.status_code == 200

    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = [executor.submit(lambda d: client.post(f"/api/designs/{d['id']}/start_downstream"), design) for design in designs]
        responses = [future.result() for future in as_completed(futures)]
    succeeded = [response for response in responses if response.status_code == 200]
    hard_blocked = [response for response in responses if response.status_code == 423]
    assert len(succeeded) == 6
    assert len(hard_blocked) == 1

    final = client.get(f"/api/batches/{batch_id}/budget").json()
    assert final["used_count"] == 7
    assert final["reserved_count"] == 18
    assert final["image_generation_count"] == 25
    assert final["hard_limit_reached"] is True
    with get_db(immediate=False) as conn:
        downstream_count = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE batch_id = ? AND task_type = 'generate_downstream'", (batch_id,)
        ).fetchone()["c"]
    assert downstream_count == 18


def test_panorama_approve_validates_design_slot_version_and_images(client):
    batch = create_batch(client)
    run_all_panorama_tasks(client, batch["id"])
    ready = client.get(f"/api/designs/batch/{batch['id']}").json()[0]
    pano = ready["slots"]["panorama"]
    version_id = pano["current_version"]["id"]
    payload = {"action": "approve", "expected_version_id": version_id}

    wrong_design_state = client.post(f"/api/designs/{ready['id']}/review_panorama", json=payload)
    assert wrong_design_state.status_code == 200
    # Restore the equivalent review state to exercise the explicit state guard.
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE designs SET status='panorama_generating' WHERE id=?", (ready["id"],))
        conn.execute("UPDATE slot_versions SET is_locked=0 WHERE id=?", (version_id,))
    generating = client.post(f"/api/designs/{ready['id']}/review_panorama", json=payload)
    assert generating.status_code == 409
    assert "panorama_review" in generating.json()["detail"]
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE designs SET status='panorama_review' WHERE id=?", (ready["id"],))


    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE slot_versions SET process_status = 'failed' WHERE id = ?",
            (ready["slots"]["panorama"]["current_version"]["id"],),
        )
    failed = client.post(f"/api/designs/{ready['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": ready["slots"]["panorama"]["current_version"]["id"],
    })
    assert failed.status_code == 400
    assert "当前状态为 'approved'" in failed.json()["detail"]
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE slots SET status='ready' WHERE design_id=? AND slot_type='panorama'", (ready["id"],))

    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE slot_versions SET process_status = 'completed', is_unknown = 1 WHERE id = ?",
            (ready["slots"]["panorama"]["current_version"]["id"],),
        )
    unknown = client.post(f"/api/designs/{ready['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": ready["slots"]["panorama"]["current_version"]["id"],
    })
    assert unknown.status_code == 400
    assert "结果未知" in unknown.json()["detail"]

    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE slot_versions SET is_unknown = 0, is_invalidated = 1 WHERE id = ?",
            (ready["slots"]["panorama"]["current_version"]["id"],),
        )
    invalidated = client.post(f"/api/designs/{ready['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": ready["slots"]["panorama"]["current_version"]["id"],
    })
    assert invalidated.status_code == 400
    assert "失效" in invalidated.json()["detail"]

    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE slot_versions SET is_invalidated = 0 WHERE id = ?",
            (ready["slots"]["panorama"]["current_version"]["id"],),
        )
    current = client.get(f"/api/designs/{ready['id']}").json()["slots"]["panorama"]["current_version"]
    raw_path = RAW_IMAGES_DIR / current["preview_image_path"].replace("_preview.jpg", "_raw.png")
    valid_bytes = raw_path.read_bytes()
    raw_path.write_bytes(b"corrupt")
    corrupt = client.post(f"/api/designs/{ready['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": current["id"],
    })
    assert corrupt.status_code == 400
    assert "损坏" in corrupt.json()["detail"]

    raw_path.write_bytes(valid_bytes)
    approved = client.post(f"/api/designs/{ready['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": current["id"],
    })
    assert approved.status_code == 200
    repeated = client.post(f"/api/designs/{ready['id']}/review_panorama", json={
        "action": "approve", "expected_version_id": current["id"],
    })
    assert repeated.status_code == 409


def test_append_standby_and_legacy_migration_use_batch_rule_snapshot(client):
    original_rule = "Frozen twin rule for this batch"
    client.put("/api/system/series_rules/modern_business", json={
        "twin_adapter_rule": original_rule,
    }, headers={"X-User-Role": "leader"})
    batch = create_batch(client, designs_per_series=1, standby_designs_per_series=1, auto_start_panorama=False)
    initial_designs = client.get(f"/api/designs/batch/{batch['id']}").json()
    frozen = {d["design_code"]: d["structured_spec"] for d in initial_designs}

    changed = client.put("/api/system/series_rules/modern_business", json={
        "twin_adapter_rule": "Later global rule must not enter an old batch",
    }, headers={"X-User-Role": "leader"})
    assert changed.status_code == 200

    appended = client.post(f"/api/batches/{batch['id']}/append_standby", headers={"X-User-Role": "leader"})
    assert appended.status_code == 200, appended.text
    assert appended.json()["rule_snapshot"] == "approved_frozen"
    after_append = client.get(f"/api/designs/batch/{batch['id']}").json()
    appended_code = appended.json()["appended"][0]["code"]
    assert after_append[-1]["design_code"] == appended_code
    assert after_append[-1]["structured_spec"]["twin_adapter_rule"] == original_rule
    assert after_append[-1]["structured_spec"]["template_version"] == frozen["MB-01"]["template_version"]

    legacy_id = f"legacy_{uuid.uuid4().hex[:8]}"
    with get_db(immediate=True) as conn:
        conn.execute("""
            INSERT INTO batches (id, name, enabled_series, target_archived_count, warning_budget, hard_limit_budget)
            VALUES (?, 'legacy batch', '["modern_business"]', 1, 20, 25)
        """, (legacy_id,))
    blocked_append = client.post(f"/api/batches/{legacy_id}/append_standby", headers={"X-User-Role": "leader"})
    assert blocked_append.status_code == 409
    assert "缺少批准规则快照" in blocked_append.json()["detail"]

    migrated = client.post(f"/api/batches/{legacy_id}/migrate_rule_snapshot", headers={"X-User-Role": "leader"})
    assert migrated.status_code == 200
    legacy_append = client.post(f"/api/batches/{legacy_id}/append_standby", headers={"X-User-Role": "leader"})
    assert legacy_append.status_code == 200
    assert legacy_append.json()["rule_snapshot"] == "explicit_legacy_migration"


def test_manual_start_is_persistent_across_testclient_instances(client):
    batch = create_batch(client, auto_start_panorama=False)
    assert batch["status"] == "paused"
    assert batch["pause_reason"] == "AWAITING_LEADER_START"
    assert batch["approved_rule_snapshot_status"] == "approved_frozen"

    with get_db(immediate=False) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE batch_id = ?", (batch["id"],)
        ).fetchone()["c"] == 0

    with TestClientInstance(app) as restarted_client:
        started = restarted_client.post(f"/api/batches/{batch['id']}/start", headers={"X-User-Role": "leader"})
        assert started.status_code == 200
        repeated = restarted_client.post(f"/api/batches/{batch['id']}/start", headers={"X-User-Role": "leader"})
        assert repeated.status_code == 409

    with get_db(immediate=False) as conn:
        tasks = dict(conn.execute(
            "SELECT id, status FROM tasks WHERE batch_id = ?", (batch["id"],)
        ).fetchall())
    assert len(tasks) == 1
    assert list(tasks.values()).count("queued") == 1


def test_structured_rules_prompt_metadata_and_mock_features_are_persisted(client):
    batch = create_batch(client)
    design = client.get(f"/api/designs/batch/{batch['id']}").json()[0]
    spec = design["structured_spec"]
    for field in (
        "soft_upholstery_usage", "metal_usage", "allowed_structural_changes",
        "forbidden_elements", "forbidden_processes", "hotel_mood",
        "twin_adapter_rule", "white_bg_rule",
    ):
        assert spec[field]
    run_all_panorama_tasks(client, batch["id"])
    generated = client.get(f"/api/designs/{design['id']}").json()
    version = generated["slots"]["panorama"]["current_version"]
    assert "LOCAL MOCK VISUAL PROTOTYPE" in version["prompt"]
    assert json.dumps(spec["template_version"]) in json.dumps(version["prompt_metadata"])
    assert version["prompt_metadata"]["mock_only"] is True
    assert version["prompt_metadata"]["no_network"] is True
    assert version["mock_visual_features"]["visible_mock_labels"] == [
        "SLOT", "LOCAL MOCK", "OFFLINE GENERATED"
    ]
