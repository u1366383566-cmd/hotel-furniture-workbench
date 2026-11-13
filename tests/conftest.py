import sys
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from storage_isolation import TEST_STORAGE, cleanup_test_storage

# Ensure the environment is fixed before backend.app.config executes.
import os
os.environ["WORKBENCH_STORAGE_DIR"] = str(TEST_STORAGE)
os.environ.pop("WORKBENCH_DB_PATH", None)
os.environ["WORKBENCH_DISABLE_WORKER_LOOP"] = "1"

# 确保 backend 模块在路径中
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.main import app
from backend.app.database import init_db
from backend.app.database import get_db

@pytest.fixture(scope="session", autouse=True)
def setup_database():
    init_db()

    yield

    cleanup_test_storage()

@pytest.fixture
def client():
    with TestClient(app) as c:
        # Session storage keeps SQLite setup cheap, but each API test starts from an
        # empty production graph. This prevents old queued fault tasks from being
        # claimed by the next test and turning timing into false failures.
        with get_db(immediate=True) as conn:
            conn.execute("DELETE FROM batches")
            conn.execute("UPDATE nas_upload_settings SET auto_enabled=0,test_design_id=NULL WHERE id=1")
            conn.execute("""UPDATE workbench_settings
                SET warning_per_group=5,hard_limit_per_group=6,
                    auto_release_unknown=0,auto_retry_request_failures=0
                WHERE id='default'""")
            conn.execute("DELETE FROM text_api_configs")
            # 生图上游配置重置为默认禁用，防止上一个用例的真实模式泄漏到后续用例
            conn.execute("""
                UPDATE image_api_configs
                SET enabled = 0, api_key = NULL,
                    base_url = 'http://127.0.0.1:3000', model = 'gpt-image-2',
                    timeout_seconds = 300
                WHERE id = 'default'
            """)
        yield c
