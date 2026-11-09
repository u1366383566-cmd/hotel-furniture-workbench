import os
import sys
import json
import time
import uuid
import shutil
import sqlite3
import tempfile
import subprocess
import urllib.request
import urllib.error
import socket
from urllib.parse import urlparse
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT_DIR = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = ROOT_DIR / "storage" / "evidence"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

# 每次运行使用独立临时存储：结果可重复，且不污染项目 storage/ 下的既有批次与图片。
TEST_STORAGE = Path(tempfile.mkdtemp(prefix=".e2e-run-", dir=ROOT_DIR))
SERVER_DB = TEST_STORAGE / "db" / "workbench.db"
SERVER_EXPORTS = TEST_STORAGE / "exports"

def reserve_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = reserve_port()
SERVER_URL = f"http://127.0.0.1:{PORT}"


def db_write(sql, params=()):
    """服务运行期间直接写入同一 SQLite (WAL)，用于构造 fault 场景。"""
    conn = sqlite3.connect(str(SERVER_DB), timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def db_query(sql, params=()):
    conn = sqlite3.connect(str(SERVER_DB), timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def current_batch_id():
    return db_query("SELECT id, name, status, is_circuit_broken FROM batches ORDER BY created_at LIMIT 1")[0]


def select_batch(page, batch_id):
    """通过自绘下拉组件选择批次（原生 select 已隐藏为数据源）。"""
    page.locator("#batch-select-trigger").click()
    page.locator(f'.cust-option[data-value="{batch_id}"]').first.click()


def seed_unknown_task(batch_id):
    """注入一笔结果未知任务 + unknown_held 账本，验证前端凭据处置链路。"""
    did = f"des_e2e_unk_{uuid.uuid4().hex[:8]}"
    sid = f"slt_e2e_unk_{uuid.uuid4().hex[:8]}"
    vid = f"ver_e2e_unk_{uuid.uuid4().hex[:8]}"
    tid = f"tsk_e2e_unk_{uuid.uuid4().hex[:8]}"
    lid = f"led_e2e_unk_{uuid.uuid4().hex[:8]}"
    for sql, params in [
        ("INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec, status) VALUES (?, ?, 'modern_business', 'E2E-UNKNOWN', '{}', 'panorama_generating')", (did, batch_id)),
        ("INSERT INTO slots (id, design_id, slot_type, status) VALUES (?, ?, 'panorama', 'generating')", (sid, did)),
        ("INSERT INTO slot_versions (id, slot_id, design_id, prompt, process_status, is_unknown, error_message) VALUES (?, ?, ?, 'e2e seeded timeout', 'unknown_pending_confirmation', 1, '上游超时/断连')", (vid, sid, did)),
        ("INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status) VALUES (?, ?, ?, ?, ?, 'generate_panorama', 'unknown')", (tid, batch_id, did, sid, vid)),
        ("INSERT INTO budget_ledger (id, batch_id, category, delta_reserved, delta_used, status, reference_task_id, note) VALUES (?, ?, 'image_generation', 1, 0, 'unknown_held', ?, 'e2e seeded unknown hold')", (lid, batch_id, tid)),
    ]:
        db_write(sql, params)
    return {"task_id": tid, "ledger_id": lid, "version_id": vid, "design_id": did}


def prepare_e2e_batches():
    """在服务启动前创建主流程批次和独立 unknown 测试批次。"""
    os.environ["PYTHONPATH"] = str(ROOT_DIR)
    os.environ["WORKBENCH_STORAGE_DIR"] = str(TEST_STORAGE)
    os.environ.pop("WORKBENCH_DB_PATH", None)
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))

    from fastapi.testclient import TestClient
    from backend.app.main import app
    from backend.app.database import init_db

    init_db()
    with TestClient(app) as setup_client:
        unknown = setup_client.post("/api/batches", json={
            "name": "E2E Unknown Protection Batch",
            "enabled_series": ["modern_business"],
            "target_archived_count": 1,
            "designs_per_series": 1,
            "standby_designs_per_series": 0,
            "auto_start_panorama": False,
        }, headers={"X-User-Role": "leader", "X-User-ID": "e2e_setup"})
        assert unknown.status_code == 200, unknown.text

        main = setup_client.post("/api/batches", json={
            "name": "E2E Main Workflow Batch",
            "enabled_series": ["modern_business", "natural_wood", "soft_contemporary"],
            "target_archived_count": 1,
            "designs_per_series": 1,
            "standby_designs_per_series": 0,
            "auto_start_panorama": True,
        }, headers={"X-User-Role": "leader", "X-User-ID": "e2e_setup"})
        assert main.status_code == 200, main.text

        manual = setup_client.post("/api/batches", json={
            "name": "E2E Leader Held Start Batch",
            "enabled_series": ["modern_business"],
            "target_archived_count": 1,
            "designs_per_series": 1,
            "standby_designs_per_series": 0,
            "warning_budget": 20,
            "hard_limit_budget": 25,
            "auto_start_panorama": False,
        }, headers={"X-User-Role": "leader", "X-User-ID": "e2e_setup"})
        assert manual.status_code == 200, manual.text

    unknown_id = unknown.json()["id"]
    main_id = main.json()["id"]
    seeded = seed_unknown_task(unknown_id)
    db_write(
        "UPDATE batches SET is_circuit_broken = 1, status = 'paused', pause_reason = 'CIRCUIT_BROKEN' WHERE id = ?",
        (unknown_id,),
    )
    manual_id = manual.json()["id"]
    return {"main_id": main_id, "unknown_id": unknown_id, "manual_id": manual_id, "seeded": seeded}


def run_browser_verification():
    try:
        prepared = prepare_e2e_batches()
    except BaseException:
        shutil.rmtree(TEST_STORAGE, ignore_errors=True)
        raise
    main_batch_id = prepared["main_id"]
    unknown_batch_id = prepared["unknown_id"]
    manual_batch_id = prepared["manual_id"]
    seeded = prepared["seeded"]
    print(f"Starting server on {SERVER_URL} (isolated storage: {TEST_STORAGE})...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT_DIR)
    env["WORKBENCH_STORAGE_DIR"] = str(TEST_STORAGE)
    env.pop("WORKBENCH_DB_PATH", None)

    server_log = (TEST_STORAGE / "server.log").open("w+b")
    server_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT_DIR),
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT
    )

    try:
        # 等待当前随机端口对应的服务启动，避免复用残留进程。
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{SERVER_URL}/api/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        else:
            raise RuntimeError("当前 e2e 服务未能在随机端口启动")
        print("Server launched, launching Playwright with msedge channel...")

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            browser_context = browser.new_context(viewport={"width": 1440, "height": 900})
            local_hosts = {"127.0.0.1", "localhost"}
            blocked_external_requests = []

            console_errors = []
            browser_context.route("**/*", lambda route: (
                blocked_external_requests.append(route.request.url)
                if urlparse(route.request.url).hostname not in local_hosts
                else route.continue_()
            ))
            page = browser_context.new_page()
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
            page.on("requestfailed", lambda r: console_errors.append(f"requestfailed: {r.url} {r.failure}") if urlparse(r.url).hostname in local_hosts else None)

            # 1. 打开首页并明确选择主流程批次。
            page.goto(SERVER_URL, timeout=20000, wait_until="domcontentloaded")
            page.wait_for_selector("text=SEEZO 视觉工作台", timeout=10000)
            select_batch(page, main_batch_id)
            page.wait_for_timeout(1200)
            print("1. Homepage loaded successfully.")
            page.screenshot(path=str(EVIDENCE_DIR / "01_homepage_initial.png"))
            stylesheet_ok = page.evaluate("""
                () => [...document.styleSheets].some(sheet => {
                    try { return sheet.href && sheet.href.includes('/static/tailwind-lite.css') }
                    catch { return false }
                })
            """)
            assert stylesheet_ok, "本地 Tailwind 子集样式未成功加载"
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(500)
            page.screenshot(path=str(EVIDENCE_DIR / "13_offline_mobile_homepage.png"))
            page.set_viewport_size({"width": 1440, "height": 900})

            # 2. 等待全景生成并在全景初筛区进行操作
            page.wait_for_timeout(3500)
            print("2. Capturing ready panorama designs.")
            page.screenshot(path=str(EVIDENCE_DIR / "02_panorama_review_ready.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(500)
            page.screenshot(path=str(EVIDENCE_DIR / "14_offline_mobile_panorama.png"))
            page.set_viewport_size({"width": 1440, "height": 900})

            # 点击第一个卡片的“初筛通过”
            first_approve_btn = page.locator(".btn-approve-pano").first
            if first_approve_btn.is_visible() and not first_approve_btn.is_disabled():
                first_approve_btn.click()
                page.wait_for_timeout(1500)
                print("3. Panorama approved.")
                page.screenshot(path=str(EVIDENCE_DIR / "03_panorama_approved.png"))

            # 3. 切换到“四图终审区”
            page.locator('button[data-tab="quad"]').click()
            page.wait_for_timeout(1000)
            print("4. Quad tab opened, waiting for downstream launch.")
            page.screenshot(path=str(EVIDENCE_DIR / "04_quad_tab_waiting_downstream.png"))

            # 新 SEEZO 流程在全景批量通过接口中自动派发下游；兼容旧批次时仍允许手动启动。
            start_downstream_btn = page.locator(".btn-start-downstream").first
            if start_downstream_btn.is_visible():
                start_downstream_btn.click()
                print("5. Downstream generation triggered, waiting for mock completion...")
            else:
                print("5. Downstream already queued by panorama approval.")
            # 等待 Worker 生成三张下游图片
            page.wait_for_timeout(6000)
            page.screenshot(path=str(EVIDENCE_DIR / "05_quad_slots_generated.png"))

            # 点击“四图合格归档”
            archive_btn = page.locator(".btn-archive-quad").first
            if archive_btn.is_visible() and not archive_btn.is_disabled():
                archive_btn.click()
                page.wait_for_timeout(1500)
                print("6. Quad archived.")
                page.screenshot(path=str(EVIDENCE_DIR / "06_quad_archived.png"))

            # 4. 切换到“已归档与导出”标签
            page.locator('button[data-tab="archived"]').click()
            page.wait_for_timeout(1500)
            print("7. Archived tab opened.")
            page.screenshot(path=str(EVIDENCE_DIR / "07_archived_export_tab.png"))

            # 5. 管理中心仅对管理员显示
            page.locator('.role-btn[data-role="admin"]').click()
            page.wait_for_timeout(500)
            page.locator('button[data-tab="styles"]').click()
            page.wait_for_function("document.querySelector('#series-template-version').textContent.includes('当前模板版本:')")
            assert page.locator("#series-rule-json").input_value()
            assert page.locator('.style-library-nav button[data-tab="styles"]').count() == 1
            assert page.locator('.wb-nav-wrap button[data-tab="styles"]').count() == 0
            page.locator('#style-cards button').first.click()
            field = page.locator('#style-materials')
            field.fill('\n'.join(f'测试材质{i}' for i in range(30)))
            assert field.evaluate('(el) => el.scrollHeight > el.clientHeight')
            page.route('**/api/system/series_rules/*/suggestions', lambda route: route.fulfill(status=200, content_type='application/json', body=json.dumps({'options':['候选木材一','候选木材二']})))
            page.locator('#suggestion-generate').click()
            page.locator('#suggestion-list input').first.check()
            page.locator('#suggestion-add').click()
            assert '候选木材一' in field.input_value() and '候选木材二' not in field.input_value()
            assert '候选木材一' in page.locator('#series-rule-json').input_value()
            page.unroute('**/api/system/series_rules/*/suggestions')
            page.locator('button[data-tab="system"]').click()
            page.wait_for_timeout(1500)
            print("8. System protect tab opened.")
            assert "次" in page.locator("#metric-planning").inner_text()
            page.screenshot(path=str(EVIDENCE_DIR / "08_system_and_unknown_protect.png"))

            # 6. 结果未知任务：负责人凭据核账后释放预占额度（前端 → 后端强校验链路）
            print("9. Switching to leader role and reviewing pre-seeded unknown task...")
            page.locator('.role-btn[data-role="admin"]').click()
            page.wait_for_timeout(800)
            select_batch(page, unknown_batch_id)
            page.wait_for_timeout(1000)
            print("   expected unknown batch:", unknown_batch_id)
            print("   seeded count:", db_query(
                "SELECT COUNT(*) AS c FROM slot_versions WHERE process_status = 'unknown_pending_confirmation'"
            ))
            page.locator("#btn-refresh-unknown").click()
            try:
                page.wait_for_selector(".btn-resolve-failure", timeout=8000)
            except Exception:
                print("DEBUG console/page errors:", console_errors[-15:])
                print("DEBUG container html:",
                      page.locator("#unknown-tasks-container").inner_html()[:800])
                print("DEBUG selected batch:", page.locator("#batch-select").input_value())
                raise
            page.locator(".unknown-billing-ref").fill("BILL-E2E-20261030-001")
            page.locator(".unknown-attest").check()
            # An unchanged refresh must not erase a leader's in-progress evidence.
            with page.expect_response(lambda r: "/unknown_tasks" in r.url and r.status == 200):
                page.locator("#btn-refresh-unknown").click()
            page.wait_for_timeout(3500)
            assert page.locator(".unknown-billing-ref").input_value() == "BILL-E2E-20261030-001"
            assert page.locator(".unknown-attest").is_checked()

            # confirm_failure 触发原生 prompt 索要人工核账依据。
            resolve_responses = []
            page.on("response", lambda r: resolve_responses.append((r.status, r.url))
                    if "/api/system/resolve_unknown" in r.url else None)
            resolve_button = page.locator(".btn-resolve-failure").first
            print("   resolve button:", resolve_button.evaluate("el => ({html: el.outerHTML, row: el.closest('.unknown-task-row')?.className})"))
            page.once("dialog", lambda dialog: dialog.accept("人工核对上游账单流水，确认无此笔生图扣费记录"))
            resolve_button.click()
            deadline = time.time() + 8
            while time.time() < deadline:
                task_status = db_query("SELECT status FROM tasks WHERE id = ?", (seeded["task_id"],))[0]["status"]
                if task_status != "unknown":
                    break
                page.wait_for_timeout(250)
            toast_text = page.locator("#toast-text").inner_text()
            print("   resolve responses:", resolve_responses)
            print("   resolve toast:", toast_text)
            print("   post-resolve rows:",
                  db_query("SELECT status FROM tasks WHERE id = ?", (seeded["task_id"],)),
                  db_query("SELECT status FROM budget_ledger WHERE id = ?", (seeded["ledger_id"],)))
            assert task_status == "failed", f"未知任务未完成处置，toast={toast_text!r}, responses={resolve_responses}"
            page.screenshot(path=str(EVIDENCE_DIR / "09_unknown_attestation_released.png"))

            task_after = db_query("SELECT status FROM tasks WHERE id = ?", (seeded["task_id"],))[0]
            ledger_after = db_query("SELECT status FROM budget_ledger WHERE id = ?", (seeded["ledger_id"],))[0]
            assert task_after["status"] == "failed", f"任务应标记 failed，实际 {task_after}"
            assert ledger_after["status"] == "released", f"账本应释放，实际 {ledger_after}"
            audit = db_query(
                "SELECT detail FROM audit_logs WHERE action = 'resolve_unknown_task' AND batch_id = ?",
                (unknown_batch_id,),
            )[0]
            assert json.loads(audit["detail"])["human_attested_no_charge"] is True
            print("   unknown task attested & budget released (DB verified).")

            # 7. 熔断恢复入口：预置熔断批次 → 负责人点击恢复并试运行
            print("10. Recovering pre-seeded circuit breaker via UI...")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("text=SEEZO 视觉工作台", timeout=10000)
            page.wait_for_timeout(2000)
            select_batch(page, unknown_batch_id)
            page.wait_for_timeout(1800)
            page.locator('.role-btn[data-role="admin"]').click()
            page.wait_for_timeout(1200)
            page.locator('button[data-tab="system"]').click()
            page.wait_for_timeout(1000)

            recover_btn = page.locator("#btn-recover-circuit")
            assert recover_btn.is_visible(), "熔断后应显示恢复按钮"
            page.screenshot(path=str(EVIDENCE_DIR / "10_circuit_breaker_visible.png"))
            recover_btn.click()
            page.wait_for_selector("text=熔断已恢复，已开启 3 次少量试运行保护", timeout=8000)

            batch_after = db_query(
                "SELECT is_circuit_broken, status, trial_run_remaining FROM batches WHERE id = ?",
                (unknown_batch_id,),
            )[0]
            assert batch_after["is_circuit_broken"] == 0 and batch_after["trial_run_remaining"] == 3, batch_after
            print("   circuit breaker recovered with 3 trial runs (DB verified).")
            page.screenshot(path=str(EVIDENCE_DIR / "11_circuit_recovered.png"))

            # 8. 归档设计导出单款带水印 ZIP（真实落盘校验）
            print("12. Exporting watermarked ZIP from archived tab...")
            select_batch(page, main_batch_id)
            page.wait_for_timeout(1000)
            page.locator('button[data-tab="archived"]').click()
            page.wait_for_timeout(1200)
            export_btn = page.locator(".btn-export-single").first
            assert export_btn.is_visible(), "已归档设计应提供导出按钮"
            export_btn.click()
            page.wait_for_selector("text=打包完成，已交给浏览器下载", timeout=8000)
            page.wait_for_timeout(800)

            zips = list(SERVER_EXPORTS.glob("*.zip"))
            assert zips, "导出目录应真实产出 ZIP 文件"
            print(f"   ZIP written: {zips[0].name} ({zips[0].stat().st_size} bytes)")
            page.screenshot(path=str(EVIDENCE_DIR / "12_zip_exported.png"))
            # 9. Leader manually starts a held batch through the UI.
            print("13. Leader manually starts held batch via UI...")
            select_batch(page, manual_batch_id)
            page.wait_for_timeout(1200)
            page.locator('.role-btn[data-role="admin"]').click()
            page.wait_for_timeout(800)
            page.locator('button[data-tab="system"]').click()
            page.wait_for_timeout(800)
            start_btn = page.locator("#btn-start-batch")
            assert start_btn.is_visible(), "待启动批次应向负责人显示启动按钮"
            start_btn.click()
            page.wait_for_selector("text=负责人已启动首发全景生成", timeout=8000)
            page.wait_for_timeout(2500)
            manual_batch = db_query(
                "SELECT status, pause_reason FROM batches WHERE id = ?", (manual_batch_id,)
            )[0]
            manual_tasks = db_query(
                "SELECT id, status FROM tasks WHERE batch_id = ?", (manual_batch_id,)
            )
            assert manual_batch["status"] == "active" and manual_batch["pause_reason"] is None, manual_batch
            assert len(manual_tasks) == 1 and manual_tasks[0]["status"] in ("queued", "running", "completed"), manual_tasks
            page.screenshot(path=str(EVIDENCE_DIR / "15_manual_leader_start.png"))
            print("   held batch started once; persisted task verified in DB.")

            # Basic built-in accessibility checks without an external a11y dependency.
            empty_alt_images = page.evaluate(
                "() => [...document.querySelectorAll('img')].filter(img => !img.getAttribute('alt')?.trim()).length"
            )
            unnamed_buttons = page.evaluate(
                "() => [...document.querySelectorAll('button')].filter(b => !(b.innerText.trim() || b.getAttribute('aria-label')?.trim())).length"
            )
            unlabeled_inputs = page.evaluate(r'''
                () => [...document.querySelectorAll('input,select,textarea')].filter(el => {
                    const id = el.id;
                    return !(el.getAttribute('aria-label')?.trim() || el.closest('label') || (id && document.querySelector(`label[for="${id}"]`)));
                }).length
            ''')
            assert empty_alt_images == 0, f"发现无 alt 图片: {empty_alt_images}"
            assert unnamed_buttons == 0, f"发现无名称按钮: {unnamed_buttons}"
            assert unlabeled_inputs == 0, f"发现未关联标签控件: {unlabeled_inputs}"
            print("   basic accessible-name checks passed (not a full WCAG audit).")
            page.screenshot(path=str(EVIDENCE_DIR / "16_accessibility_names.png"))

            assert not blocked_external_requests, f"离线模式出现外部请求: {blocked_external_requests}"
            assert not [message for message in console_errors if "ERR_INTERNET_DISCONNECTED" in message], console_errors

            browser.close()
            print("Playwright browser flow completed, all screenshots captured.")

    finally:
        if server_process.poll() is None:
            server_process.terminate()
        print("Stopping server process...")
        try:
            server_process.wait(timeout=3)
        except Exception:
            server_process.kill()
            server_process.wait(timeout=3)
        server_log.seek(0)
        log_tail = server_log.read().decode("utf-8", errors="replace")[-4000:]
        server_log.close()
        (EVIDENCE_DIR / "last_server.log").write_text(log_tail, encoding="utf-8")
        shutil.rmtree(TEST_STORAGE, ignore_errors=True)


if __name__ == "__main__":
    run_browser_verification()
