import shutil
import tempfile
from pathlib import Path

# Each pytest session gets a private SQLite/image root. This must run before
# backend.app.config is imported anywhere in the test process.
TEST_STORAGE = Path(tempfile.mkdtemp(prefix="hotel-workbench-tests-"))


def cleanup_test_storage():
    shutil.rmtree(TEST_STORAGE, ignore_errors=True)
