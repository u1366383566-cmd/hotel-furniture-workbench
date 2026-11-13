"""Large-list acceptance for compact polling, image caching and card patching."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright


ROOT_DIR = Path(__file__).resolve().parent.parent
TEST_STORAGE = Path(tempfile.mkdtemp(prefix=".e2e-large-list-", dir=ROOT_DIR))
SERVER_DB = TEST_STORAGE / "db" / "workbench.db"


def reserve_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = reserve_port()
SERVER_URL = f"http://127.0.0.1:{PORT}"


def seed_large_batch():
    os.environ["PYTHONPATH"] = str(ROOT_DIR)
    os.environ["WORKBENCH_STORAGE_DIR"] = str(TEST_STORAGE)
    os.environ["WORKBENCH_DISABLE_WORKER_LOOP"] = "1"
    os.environ.pop("WORKBENCH_DB_PATH", None)
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))

    from fastapi.testclient import TestClient
    from backend.app.database import get_db, init_db
    from backend.app.main import app

    init_db()
    with TestClient(app) as client:
        response = client.post(
            "/api/batches",
            headers={"X-User-Role": "leader", "X-User-ID": "perf_setup"},
            json={
                "name": "E2E Large List",
                "enabled_series": ["modern_business"],
                "target_archived_count": 1,
                "designs_per_series": 1,
                "standby_designs_per_series": 0,
                "auto_start_panorama": False,
            },
        )
        assert response.status_code == 200, response.text
        batch_id = response.json()["id"]

    preview_dir = TEST_STORAGE / "preview_images"
    raw_dir = TEST_STORAGE / "raw_images"
    preview_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (256, 256), "white")
    image.save(preview_dir / "perf_preview.jpg", format="JPEG", quality=70)
    image.save(raw_dir / "perf_raw.png", format="PNG")

    with get_db(immediate=True) as conn:
        conn.execute("DELETE FROM designs WHERE batch_id=?", (batch_id,))
        conn.execute(
            """UPDATE batches SET target_archived_count=100,current_archived_count=95,
               status='active',warning_budget=1000,hard_limit_budget=1200 WHERE id=?""",
            (batch_id,),
        )
        for index in range(100):
            design_id = f"des_perf_{index:03d}"
            technical = index >= 95
            status = "downstream_generating" if technical else "archived"
            code = f"PERF-{index:03d}"
            spec = {
                "series_name": "现代商务",
                "model_title": f"压力测试款 {index:03d}",
                "headboard": "米白软包床屏",
                "bed_frame": "深色木床体",
                "nightstand": "深色木床头柜",
                "wardrobe": "通顶衣柜",
                "main_materials": ["木饰面"],
                "color_scheme": ["米白", "深胡桃木"],
                "panorama_prompt": "酒店客房全景",
            }
            conn.execute(
                """INSERT INTO designs(
                       id,batch_id,series_id,design_code,is_standby,status,structured_spec,
                       product_number,created_at,updated_at,archived_at,total_redo_count
                   ) VALUES (?,?,'modern_business',?,0,?,?,?,?,?,?,0)""",
                (
                    design_id,
                    batch_id,
                    code,
                    status,
                    json.dumps(spec, ensure_ascii=False),
                    None if technical else index + 1,
                    f"2026-01-01 00:{index // 60:02d}:{index % 60:02d}",
                    f"2026-01-01 00:{index // 60:02d}:{index % 60:02d}",
                    None if technical else "2026-01-01 01:00:00",
                ),
            )
            for slot_index, slot_type in enumerate(
                ("panorama", "white_isolated", "twin_bed", "nightstand_closeup")
            ):
                slot_id = f"slt_perf_{index:03d}_{slot_index}"
                version_id = f"ver_perf_{index:03d}_{slot_index}"
                task_id = f"tsk_perf_{index:03d}_{slot_index}"
                failed = technical and slot_index == 0
                conn.execute(
                    """INSERT INTO slots(
                           id,design_id,slot_type,current_version_id,status,redo_count,technical_retry_count
                       ) VALUES (?,?,?,?,?,0,0)""",
                    (
                        slot_id,
                        design_id,
                        slot_type,
                        version_id,
                        "empty" if failed else "approved",
                    ),
                )
                conn.execute(
                    """INSERT INTO slot_versions(
                           id,slot_id,design_id,version_num,attempt_num,raw_image_path,
                           preview_image_path,prompt,prompt_metadata,is_mock,process_status,
                           error_message,is_unknown
                       ) VALUES (?,?,?,1,1,?,?,?,?,0,?,?,0)""",
                    (
                        version_id,
                        slot_id,
                        design_id,
                        None if failed else "perf_raw.png",
                        None if failed else "perf_preview.jpg",
                        "compact summary test prompt",
                        json.dumps(
                            {
                                "prompt_status": "completed",
                                "panorama_view": {"label": "正面"},
                                "real_prompt": "x" * 20000,
                            }
                        ),
                        "failed" if failed else "completed",
                        "upstream timeout" if failed else None,
                    ),
                )
                conn.execute(
                    """INSERT INTO tasks(
                           id,batch_id,design_id,slot_id,slot_version_id,task_type,status,
                           failure_stage,retry_status,auto_retry_count,last_error,idempotency_key
                       ) VALUES (?,?,?,?,?,'generate_downstream',?,?,?,0,?,?)""",
                    (
                        task_id,
                        batch_id,
                        design_id,
                        slot_id,
                        version_id,
                        "failed" if failed else "completed",
                        "request_charge_unknown" if failed else None,
                        "retry_exhausted" if failed else None,
                        "upstream timeout" if failed else None,
                        f"perf-task-{index}-{slot_index}",
                    ),
                )
    return batch_id


def run():
    batch_id = seed_large_batch()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT_DIR)
    env["WORKBENCH_STORAGE_DIR"] = str(TEST_STORAGE)
    env.pop("WORKBENCH_DB_PATH", None)
    env.pop("WORKBENCH_DISABLE_WORKER_LOOP", None)
    server_log = (TEST_STORAGE / "server.log").open("w+b")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        cwd=str(ROOT_DIR),
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{SERVER_URL}/api/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        else:
            raise RuntimeError("large-list server did not start")

        headers = {"X-User-Role": "admin", "X-User-ID": "perf"}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            image_requests = []
            summary_requests = []
            full_requests = []
            page.on(
                "request",
                lambda request: image_requests.append(request.url)
                if "/api/media/preview/" in request.url else None,
            )
            page.on(
                "request",
                lambda request: summary_requests.append(request.url)
                if request.url.endswith(f"/api/designs/batch/{batch_id}/summary")
                else None,
            )
            page.on(
                "request",
                lambda request: full_requests.append(request.url)
                if request.url.endswith(f"/api/designs/batch/{batch_id}")
                else None,
            )

            page.goto(SERVER_URL, wait_until="domcontentloaded")
            page.wait_for_selector("#batch-select-trigger")
            page.wait_for_timeout(1500)
            page.locator('.role-btn[data-role="admin"]').click()
            page.wait_for_timeout(800)
            assert page.locator(".technical-failure-item").count() == 5
            assert page.locator("#technical-failures-retry").is_visible()

            page.locator('button[data-tab="archived"]').click()
            page.wait_for_selector("#archived-designs-grid .long-list-card")
            page.wait_for_timeout(1200)
            card_count = page.locator("#archived-designs-grid .long-list-card").count()
            assert card_count == 95, f"expected 95 archived cards, got {card_count}"
            assert page.locator("#archived-designs-grid img[loading='lazy']").count() == 380
            initial_image_request_count = len(image_requests)
            assert initial_image_request_count < 80, (
                f"too many eager image requests: {initial_image_request_count}"
            )
            assert summary_requests, "summary polling endpoint was not used"
            assert not full_requests, f"full design endpoint was requested by the page: {full_requests}"

            page.evaluate(
                """() => {
                    const first = document.querySelector('#archived-designs-grid .long-list-card');
                    first.dataset.preservedByTest = '1';
                }"""
            )
            import sqlite3

            conn = sqlite3.connect(str(SERVER_DB), timeout=20)
            conn.execute(
                "UPDATE designs SET updated_at='2030-01-01 00:00:00' WHERE id='des_perf_050'"
            )
            conn.commit()
            conn.close()
            page.wait_for_timeout(9000)
            polled_card_count = page.locator('#archived-designs-grid .long-list-card').count()
            assert polled_card_count == 95, (
                f"card count changed after polling: {polled_card_count}"
            )
            assert (
                page.locator('#archived-designs-grid .long-list-card[data-preserved-by-test="1"]').count()
                == 1
            ), "unchanged card DOM was replaced during polling"

            first_image_url = page.locator("#archived-designs-grid img").first.get_attribute("src")
            if first_image_url.startswith("/"):
                first_image_url = SERVER_URL + first_image_url
            image_response = context.request.get(first_image_url, headers=headers)
            assert image_response.ok
            assert (
                image_response.headers["cache-control"]
                == "private, max-age=604800, immutable"
            )

            compact = context.request.get(
                f"{SERVER_URL}/api/designs/batch/{batch_id}/summary", headers=headers
            )
            full = context.request.get(
                f"{SERVER_URL}/api/designs/batch/{batch_id}", headers=headers
            )
            assert compact.ok and full.ok
            assert len(compact.body()) < 1_000_000
            assert len(compact.body()) < len(full.body()) / 2
            cached = context.request.get(
                f"{SERVER_URL}/api/designs/batch/{batch_id}/summary",
                headers={**headers, "If-None-Match": compact.headers["etag"]},
            )
            assert cached.status == 304 and not cached.body()

            image_requests.clear()
            page.reload(wait_until="domcontentloaded")
            page.locator('button[data-tab="archived"]').click()
            page.wait_for_selector("#archived-designs-grid .long-list-card")
            page.wait_for_timeout(1200)
            cached_transfers = page.evaluate(
                """() => performance.getEntriesByType('resource')
                     .filter(item => item.name.includes('/api/media/preview/'))
                     .map(item => item.transferSize)"""
            )
            assert cached_transfers and any(size == 0 for size in cached_transfers), (
                "reload did not reuse cached preview images",
                cached_transfers[:20],
            )
            compact_bytes = len(compact.body())
            full_bytes = len(full.body())
            browser.close()
            print(
                "large-list acceptance passed:",
                {
                    "cards": 95,
                    "technical_failures": 5,
                    "initial_image_requests": initial_image_request_count,
                    "compact_bytes": compact_bytes,
                    "full_bytes": full_bytes,
                },
            )
    finally:
        if server.poll() is None:
            server.terminate()
        try:
            server.wait(timeout=5)
        except Exception:
            server.kill()
            server.wait(timeout=3)
        server_log.close()
        shutil.rmtree(TEST_STORAGE, ignore_errors=True)


if __name__ == "__main__":
    run()
