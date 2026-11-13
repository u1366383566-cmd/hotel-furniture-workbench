import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator
from .config import DB_PATH, SERIES_RULES

def get_connection(timeout: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn

@contextmanager
def get_db(immediate: bool = False) -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection()
    try:
        cmd = "BEGIN IMMEDIATE" if immediate else "BEGIN"
        conn.execute(cmd)
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection):
    cur = conn.cursor()
    def add_col_if_missing(table, col_name, col_type):
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r['name'] for r in cur.fetchall()]
        if col_name not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")

    add_col_if_missing('batches', 'pause_reason', 'TEXT')
    add_col_if_missing('workbench_settings', 'auto_release_unknown', 'INTEGER NOT NULL DEFAULT 0')
    add_col_if_missing('batches', 'trial_run_remaining', 'INTEGER DEFAULT 0')
    add_col_if_missing('batches', 'trial_mode', 'INTEGER NOT NULL DEFAULT 0')
    add_col_if_missing('batches', 'approved_series_rules_json', 'TEXT')
    add_col_if_missing('batches', 'approved_rule_snapshot_status', "TEXT NOT NULL DEFAULT 'legacy_unfrozen'")
    add_col_if_missing('batches', 'smart_planning', 'INTEGER NOT NULL DEFAULT 0')
    add_col_if_missing('batches', 'planning_confirmed', 'INTEGER NOT NULL DEFAULT 0')
    add_col_if_missing('batches', 'planning_brief', "TEXT NOT NULL DEFAULT ''")
    add_col_if_missing('batches', 'auto_workflow', 'INTEGER NOT NULL DEFAULT 0')
    add_col_if_missing('batches', 'series_counts_json', "TEXT NOT NULL DEFAULT '{}'")
    add_col_if_missing('batches', 'series_preferences_json', "TEXT NOT NULL DEFAULT '{}'")
    add_col_if_missing('planning_jobs', 'replan_count', 'INTEGER NOT NULL DEFAULT 0')
    add_col_if_missing('watermark_configs', 'renderer', "TEXT NOT NULL DEFAULT 'seezo_logo'")
    add_col_if_missing('watermark_configs', 'logo_asset', "TEXT NOT NULL DEFAULT 'static/seezo-watermark.png'")
    add_col_if_missing('watermark_configs', 'logo_width_ratio', 'REAL NOT NULL DEFAULT 0.20')
    add_col_if_missing('watermark_configs', 'logo_left_ratio', 'REAL NOT NULL DEFAULT 0.0')
    add_col_if_missing('watermark_configs', 'logo_top_ratio', 'REAL NOT NULL DEFAULT 0.02')
    add_col_if_missing('budget_ledger', 'ledger_event_type', 'TEXT')
    add_col_if_missing('designs', 'template_version', "TEXT DEFAULT 'tpl_v1.0'")
    add_col_if_missing('designs', 'planning_status', "TEXT NOT NULL DEFAULT 'legacy'")
    add_col_if_missing('designs', 'series_rule_snapshot_json', 'TEXT')
    add_col_if_missing('designs', 'verified_features', 'TEXT')
    add_col_if_missing('designs', 'verified_features_version_id', 'TEXT')
    add_col_if_missing('designs', 'product_number', 'INTEGER')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_designs_product_number ON designs(product_number) WHERE product_number IS NOT NULL')
    conn.execute('CREATE TABLE IF NOT EXISTS product_number_state (id INTEGER PRIMARY KEY CHECK(id=1), ready INTEGER NOT NULL, last_number INTEGER NOT NULL)')
    conn.execute("INSERT OR IGNORE INTO product_number_state VALUES (1, CASE WHEN EXISTS (SELECT 1 FROM designs WHERE status='archived') THEN 0 ELSE 1 END, 0)")
    conn.execute("""CREATE TABLE IF NOT EXISTS nas_upload_jobs (
        id TEXT PRIMARY KEY, design_id TEXT NOT NULL UNIQUE REFERENCES designs(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'queued', error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))
    """)
    conn.execute("""CREATE TABLE IF NOT EXISTS nas_upload_items (
        job_id TEXT NOT NULL REFERENCES nas_upload_jobs(id) ON DELETE CASCADE,
        slot_type TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
        byte_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        PRIMARY KEY(job_id,slot_type))
    """)
    add_col_if_missing('nas_upload_jobs', 'owns_directory', 'INTEGER NOT NULL DEFAULT 0')
    add_col_if_missing('nas_upload_items', 'verify_offset', 'INTEGER NOT NULL DEFAULT 0')
    add_col_if_missing('nas_upload_items', 'verify_etag', 'TEXT')
    add_col_if_missing('nas_upload_items', 'slot_version_id', 'TEXT')
    add_col_if_missing('nas_upload_jobs', 'confirmed_by', 'TEXT')
    add_col_if_missing('nas_upload_jobs', 'confirmed_at', 'TEXT')
    conn.execute("""CREATE TABLE IF NOT EXISTS nas_upload_attempts (
        job_id TEXT NOT NULL REFERENCES nas_upload_jobs(id) ON DELETE CASCADE,
        attempt INTEGER NOT NULL, started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        finished_at TEXT, status TEXT, error TEXT, PRIMARY KEY(job_id,attempt))
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS nas_upload_settings (id INTEGER PRIMARY KEY CHECK(id=1), auto_enabled INTEGER NOT NULL DEFAULT 0, test_design_id TEXT)")
    conn.execute("INSERT OR IGNORE INTO nas_upload_settings(id,auto_enabled) VALUES (1,0)")
    add_col_if_missing('slot_versions', 'template_version', "TEXT DEFAULT 'tpl_v1.0'")
    add_col_if_missing('slot_versions', 'prompt_metadata', 'TEXT')
    add_col_if_missing('slot_versions', 'mock_visual_features', 'TEXT')

    # Existing released rows have only note text. Backfill the exact notes already
    # written by TaskWorker; truly unknown legacy notes stay explicit failures.
    conn.execute("""
        UPDATE budget_ledger
        SET ledger_event_type = 'technical_retry'
        WHERE status = 'released'
          AND ledger_event_type IS NULL
          AND note = 'Technical 429 retry backoff'
    """)
    conn.execute("""
        UPDATE budget_ledger
        SET ledger_event_type = 'explicit_failure'
        WHERE status = 'released'
          AND ledger_event_type IS NULL
          AND note IS NOT NULL
          AND note != 'Technical 429 retry backoff'
    """)

def _migrate_budget_ledger_allow_null_batch(conn):
    """budget_ledger.batch_id 原 NOT NULL；系列级 LLM 规划不属于任何批次，
    需允许 NULL（批次预算统计按 batch_id 过滤，NULL 行不影响任何批次）。"""
    info = conn.execute("PRAGMA table_info(budget_ledger)").fetchall()
    batch_col = next((c for c in info if c["name"] == "batch_id"), None)
    if batch_col is None or not batch_col["notnull"]:
        return
    conn.execute("""
        CREATE TABLE budget_ledger_new (
            id TEXT PRIMARY KEY,
            batch_id TEXT REFERENCES batches(id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            delta_reserved INTEGER NOT NULL DEFAULT 0,
            delta_used INTEGER NOT NULL DEFAULT 0,
            delta_unsettled INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'reserved',
            ledger_event_type TEXT,
            reference_task_id TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        INSERT INTO budget_ledger_new
            (id, batch_id, category, delta_reserved, delta_used, delta_unsettled,
             status, ledger_event_type, reference_task_id, note, created_at)
        SELECT id, batch_id, category, delta_reserved, delta_used, delta_unsettled,
               status, ledger_event_type, reference_task_id, note, created_at
        FROM budget_ledger
    """)
    conn.execute("DROP TABLE budget_ledger")
    conn.execute("ALTER TABLE budget_ledger_new RENAME TO budget_ledger")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_budget_batch ON budget_ledger(batch_id)")


def init_db():
    with get_db(immediate=True) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            enabled_series TEXT NOT NULL,
            target_archived_count INTEGER NOT NULL DEFAULT 3,
            current_archived_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            is_mock INTEGER NOT NULL DEFAULT 1,
            warning_budget INTEGER NOT NULL DEFAULT 20,
            hard_limit_budget INTEGER NOT NULL DEFAULT 25,
            budget_warning_exceeded_approved_by TEXT,
            budget_warning_exceeded_at TEXT,
            is_circuit_broken INTEGER NOT NULL DEFAULT 0,
            circuit_break_reason TEXT,
            approved_series_rules_json TEXT,
            approved_rule_snapshot_status TEXT NOT NULL DEFAULT 'legacy_unfrozen',
            series_counts_json TEXT NOT NULL DEFAULT '{}',
            series_preferences_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS designs (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            series_id TEXT NOT NULL,
            design_code TEXT NOT NULL,
            is_standby INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            structured_spec TEXT NOT NULL,
            visual_features TEXT,
            elimination_reason TEXT,
            elimination_comment TEXT,
            archived_at TEXT,
            total_redo_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(batch_id, design_code)
        );
        CREATE INDEX IF NOT EXISTS idx_designs_batch ON designs(batch_id);
        CREATE INDEX IF NOT EXISTS idx_designs_status ON designs(status);

        CREATE TABLE IF NOT EXISTS slots (
            id TEXT PRIMARY KEY,
            design_id TEXT NOT NULL REFERENCES designs(id) ON DELETE CASCADE,
            slot_type TEXT NOT NULL,
            current_version_id TEXT,
            status TEXT NOT NULL DEFAULT 'empty',
            redo_count INTEGER NOT NULL DEFAULT 0,
            technical_retry_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(design_id, slot_type)
        );
        CREATE INDEX IF NOT EXISTS idx_slots_design ON slots(design_id);

        CREATE TABLE IF NOT EXISTS slot_versions (
            id TEXT PRIMARY KEY,
            slot_id TEXT NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
            design_id TEXT NOT NULL REFERENCES designs(id) ON DELETE CASCADE,
            version_num INTEGER NOT NULL DEFAULT 1,
            attempt_num INTEGER NOT NULL DEFAULT 1,
            raw_image_path TEXT,
            preview_image_path TEXT,
            prompt TEXT NOT NULL,
            negative_prompt TEXT,
            model_config TEXT,
            is_mock INTEGER NOT NULL DEFAULT 1,
            upstream_request_id TEXT,
            prompt_metadata TEXT,
            mock_visual_features TEXT,
            process_status TEXT NOT NULL DEFAULT 'pending',
            is_locked INTEGER NOT NULL DEFAULT 0,
            is_invalidated INTEGER NOT NULL DEFAULT 0,
            depends_on_panorama_version_id TEXT,
            error_message TEXT,
            is_unknown INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_slot_versions_slot ON slot_versions(slot_id);
        CREATE INDEX IF NOT EXISTS idx_slot_versions_depends ON slot_versions(depends_on_panorama_version_id);

        CREATE TABLE IF NOT EXISTS budget_ledger (
            id TEXT PRIMARY KEY,
            batch_id TEXT REFERENCES batches(id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            delta_reserved INTEGER NOT NULL DEFAULT 0,
            delta_used INTEGER NOT NULL DEFAULT 0,
            delta_unsettled INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'reserved',
            ledger_event_type TEXT,
            reference_task_id TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_budget_batch ON budget_ledger(batch_id);

        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY,
            design_id TEXT NOT NULL REFERENCES designs(id) ON DELETE CASCADE,
            slot_id TEXT REFERENCES slots(id) ON DELETE SET NULL,
            slot_version_id TEXT REFERENCES slot_versions(id) ON DELETE SET NULL,
            review_stage TEXT NOT NULL,
            action TEXT NOT NULL,
            reason_category TEXT,
            detail_comment TEXT,
            reviewer_name TEXT NOT NULL,
            reviewer_role TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_reviews_design ON reviews(design_id);

        CREATE TABLE IF NOT EXISTS audit_logs (
            id TEXT PRIMARY KEY,
            batch_id TEXT REFERENCES batches(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            actor_name TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_logs(batch_id);

        CREATE TABLE IF NOT EXISTS watermark_configs (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            main_text TEXT NOT NULL,
            repeat_text TEXT NOT NULL,
            opacity REAL NOT NULL DEFAULT 0.22,
            font_size INTEGER NOT NULL DEFAULT 36,
            position TEXT NOT NULL DEFAULT 'top_left',
            renderer TEXT NOT NULL DEFAULT 'seezo_logo',
            logo_asset TEXT NOT NULL DEFAULT 'static/seezo-watermark.png',
            logo_width_ratio REAL NOT NULL DEFAULT 0.20,
            logo_left_ratio REAL NOT NULL DEFAULT 0.0,
            logo_top_ratio REAL NOT NULL DEFAULT 0.02,
            updated_by TEXT NOT NULL DEFAULT 'system',
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(batch_id)
        );


        CREATE TABLE IF NOT EXISTS series_rule_configs (
            series_id TEXT PRIMARY KEY,
            rule_json TEXT NOT NULL,
            template_version TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT 'system',
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS export_records (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            design_id TEXT REFERENCES designs(id) ON DELETE SET NULL,
            export_type TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            exported_by TEXT NOT NULL,
            exported_role TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS image_api_configs (
            id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            base_url TEXT NOT NULL DEFAULT 'http://127.0.0.1:3000',
            model TEXT NOT NULL DEFAULT 'gpt-image-2',
            timeout_seconds INTEGER NOT NULL DEFAULT 300,
            response_format TEXT NOT NULL DEFAULT 'b64_json',
            api_key TEXT,
            updated_by TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS text_api_configs (
            id TEXT PRIMARY KEY,
            base_url TEXT NOT NULL,
            model TEXT NOT NULL,
            timeout_seconds INTEGER NOT NULL DEFAULT 180,
            api_key TEXT,
            updated_by TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS workbench_settings (
            id TEXT PRIMARY KEY, warning_per_group INTEGER NOT NULL DEFAULT 8,
            hard_limit_per_group INTEGER NOT NULL DEFAULT 10
        );
        INSERT OR IGNORE INTO workbench_settings (id) VALUES ('default');

        CREATE TABLE IF NOT EXISTS planning_jobs (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            design_id TEXT NOT NULL UNIQUE REFERENCES designs(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'queued',
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            replan_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            design_id TEXT NOT NULL REFERENCES designs(id) ON DELETE CASCADE,
            slot_id TEXT NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
            slot_version_id TEXT NOT NULL REFERENCES slot_versions(id) ON DELETE CASCADE,
            task_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_batch ON tasks(batch_id);
        """)
        _migrate_budget_ledger_allow_null_batch(conn)
        _ensure_columns(conn)
        conn.execute(
            """INSERT OR IGNORE INTO image_api_configs (id, enabled, base_url, model, timeout_seconds, response_format)
               VALUES ('default', 0, 'http://127.0.0.1:3000', 'gpt-image-2', 300, 'b64_json')"""
        )
        for series_id, rule in SERIES_RULES.items():
            conn.execute("""
                INSERT OR IGNORE INTO series_rule_configs (series_id, rule_json, template_version, updated_by)
                VALUES (?, ?, 'tpl_2026_v1.0', 'system')
            """, (series_id, json.dumps(rule, ensure_ascii=False)))
        # Add newly required rule fields without overwriting a leader's saved values.
        # Custom leader-created series are not in SERIES_RULES and keep their stored rules.
        for row in conn.execute("SELECT series_id, rule_json FROM series_rule_configs").fetchall():
            default_rule = SERIES_RULES.get(row["series_id"])
            if not default_rule:
                continue
            stored = json.loads(row["rule_json"])
            changed = False
            for key, value in default_rule.items():
                if key not in stored:
                    stored[key] = value
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE series_rule_configs SET rule_json = ? WHERE series_id = ?",
                    (json.dumps(stored, ensure_ascii=False), row["series_id"]),
                )
