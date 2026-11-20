import hashlib
import io
import json
import random
import shutil
import threading
import zipfile

from PIL import Image, JpegImagePlugin

import backend.app.png_delivery_service as delivery
from backend.app.config import ARCHIVE_DELIVERY_DIR, RAW_IMAGES_DIR
from backend.app.database import get_db
from backend.app.nas_upload_service import process_one
from backend.app.png_delivery_service import (
    archive_optimizer_worker,
    encode_archive_jpeg,
    optimize_archive_group,
    process_pending,
)
from backend.app.export_service import inspect_archive_delivery
from test_png_archive_delivery import _archive_group


def _set_format(client, image_format):
    response = client.post(
        "/api/system/archive_delivery",
        json={"image_format": image_format},
        headers={"X-User-Role": "leader"},
    )
    assert response.status_code == 200, response.text


def _download(client, response):
    assert response.status_code == 200, response.text
    payload = response.json()
    downloaded = client.get(payload["download_url"], headers={"X-User-Role": "operator"})
    assert downloaded.status_code == 200
    return payload, downloaded.content


def _unzip_metadata(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        metadata_name = next(name for name in archive.namelist() if "/" not in name and name.startswith("METADATA-"))
        metadata = json.loads(archive.read(metadata_name))
        images = {
            name.rsplit("/", 1)[-1]: archive.read(name)
            for name in archive.namelist()
            if "/" in name
        }
    return metadata, images


class ReadingRemote:
    def __init__(self):
        self.files = {}
        self.puts = 0

    def close(self):
        pass

    def exists(self, path):
        return path in self.files

    def names(self, path):
        return {name[len(path) + 1:] for name in self.files if name.startswith(path + "/")}

    def mkdir(self, path):
        pass

    def put_new(self, path, source):
        self.puts += 1
        self.files[path] = source.read() if hasattr(source, "read") else source


def test_archive_format_defaults_png_and_switch_only_affects_future_groups(client):
    default = client.get(
        "/api/system/archive_delivery",
        headers={"X-User-Role": "operator"},
    )
    assert default.status_code == 200
    assert default.json()["image_format"] == "png"

    _, png_design = _archive_group(client, name="default PNG archive")
    _set_format(client, "jpg")
    _, jpg_design = _archive_group(client, name="JPG archive")
    _set_format(client, "png")
    _, later_png_design = _archive_group(client, name="later PNG archive")

    assert optimize_archive_group(png_design)
    assert optimize_archive_group(jpg_design)
    assert optimize_archive_group(later_png_design)

    with get_db() as conn:
        designs = {}
        for design_id in (png_design, jpg_design, later_png_design):
            rows = conn.execute(
                "SELECT * FROM archive_delivery_assets WHERE design_id=? ORDER BY slot_type",
                (design_id,),
            ).fetchall()
            designs[design_id] = rows
            archived = conn.execute(
                "SELECT archived_at FROM designs WHERE id=?", (design_id,)
            ).fetchone()
            assert archived["archived_at"]
            assert len(rows) == 4
            assert len({row["slot_version_id"] for row in rows}) == 4
            assert len({row["watermark_config_json"] for row in rows}) == 1
            assert {row["archived_at"] for row in rows} == {archived["archived_at"]}
            assert all(row["optimization_status"] == "ready" for row in rows)
            assert all(row["sha256"] for row in rows)

    assert {row["image_format"] for row in designs[png_design]} == {"png"}
    assert {row["filename"].rsplit(".", 1)[-1] for row in designs[png_design]} == {"png"}
    assert {row["image_format"] for row in designs[jpg_design]} == {"jpg"}
    assert {row["filename"].rsplit(".", 1)[-1] for row in designs[jpg_design]} == {"jpg"}
    assert {row["image_format"] for row in designs[later_png_design]} == {"png"}


def test_jpeg_quality_ladder_and_q92_over_limit_are_deliverable():
    randomizer = random.Random(7)
    image = Image.frombytes(
        "RGB",
        (1000, 1000),
        bytes(randomizer.randrange(256) for _ in range(1000 * 1000 * 3)),
    )
    q95, _ = encode_archive_jpeg(image, max_bytes=10**9, qualities=(95,))
    selected, quality = encode_archive_jpeg(image, max_bytes=len(q95) - 1)
    assert quality < 95
    assert len(selected) <= len(q95) - 1

    delivered, final_quality = encode_archive_jpeg(image)
    assert final_quality == 92
    assert len(delivered) > 700000
    with Image.open(io.BytesIO(delivered)) as decoded:
        assert decoded.size == image.size
        assert decoded.format == "JPEG"
        assert JpegImagePlugin.get_sampling(decoded) == 0


def test_export_polling_is_short_then_nas_reuses_same_jpg_bytes(client):
    _set_format(client, "jpg")
    batch_id, design_id = _archive_group(client, name="automatic JPG preparation")
    with get_db(immediate=True) as conn:
        job = conn.execute(
            "SELECT id FROM nas_upload_jobs WHERE design_id=?", (design_id,)
        ).fetchone()
        conn.execute(
            "UPDATE nas_upload_jobs SET status='waiting',error=NULL,next_attempt_at=NULL "
            "WHERE id=?",
            (job["id"],),
        )

    class MustNotConnect:
        def __getattr__(self, name):
            raise AssertionError("NAS must not connect while images are preparing")

    assert process_one(lambda: MustNotConnect()) is False
    with get_db() as conn:
        waiting_job = conn.execute(
            "SELECT status,attempts FROM nas_upload_jobs WHERE id=?", (job["id"],)
        ).fetchone()
    assert waiting_job["status"] == "waiting"
    assert waiting_job["attempts"] == 0

    pending = client.post(
        f"/api/export/design/{design_id}?retry_failed=1",
        headers={"X-User-Role": "operator"},
    )
    assert pending.status_code == 202
    assert pending.json()["status"] == "pending"
    assert pending.json()["retry_after_ms"] == 1000
    selected_pending = client.post(
        "/api/export/selected?retry_failed=1",
        json={"batch_id": batch_id, "design_ids": [design_id]},
        headers={"X-User-Role": "operator"},
    )
    batch_pending = client.post(
        f"/api/export/batch/{batch_id}?retry_failed=1",
        headers={"X-User-Role": "operator"},
    )
    assert selected_pending.status_code == 202
    assert batch_pending.status_code == 202

    assert optimize_archive_group(design_id)
    response = client.post(
        f"/api/export/design/{design_id}?retry_failed=0",
        headers={"X-User-Role": "operator"},
    )
    _, content = _download(client, response)
    metadata, exported_images = _unzip_metadata(content)
    assert metadata["image_format"] == "jpg"
    assert {name.rsplit(".", 1)[-1] for name in exported_images} == {"jpg"}

    with get_db() as conn:
        assets = conn.execute(
            "SELECT * FROM archive_delivery_assets WHERE design_id=? ORDER BY slot_type",
            (design_id,),
        ).fetchall()
        archived = conn.execute(
            "SELECT product_number FROM designs WHERE id=?", (design_id,)
        ).fetchone()
        raw_sizes = {}
        for asset in assets:
            with Image.open(RAW_IMAGES_DIR / asset["raw_image_path"]) as source:
                raw_sizes[asset["slot_type"]] = source.size
        conn.execute(
            "UPDATE nas_upload_jobs SET status='waiting',error=NULL,next_attempt_at=NULL "
            "WHERE id=?",
            (job["id"],),
        )
    assert all(asset["jpeg_quality"] in (95, 94, 93, 92) for asset in assets)
    assert all(asset["byte_count"] == len(exported_images[asset["filename"]]) for asset in assets)
    for asset in assets:
        with Image.open(ARCHIVE_DELIVERY_DIR / asset["canonical_path"]) as decoded:
            assert decoded.size == raw_sizes[asset["slot_type"]]
            assert JpegImagePlugin.get_sampling(decoded) == 0

    summary = client.get(
        f"/api/designs/batch/{batch_id}/summary",
        headers={"X-User-Role": "operator"},
    )
    summary_design = next(item for item in summary.json() if item["id"] == design_id)
    assert summary_design["archive_delivery"]["status"] == "ready"
    assert summary_design["archive_delivery"]["image_format"] == "jpg"
    assert all(item["byte_count"] for item in summary_design["archive_delivery"]["items"])

    remote = ReadingRemote()
    assert process_one(lambda: remote)
    code = f"TFMK{archived['product_number']:03d}"
    assert remote.puts == 4
    for asset in assets:
        assert remote.files[f"{code}/{asset['filename']}"] == exported_images[asset["filename"]]


def test_legacy_group_stays_png_and_partial_upload_reuses_bytes(client, monkeypatch):
    _set_format(client, "jpg")
    _, design_id = _archive_group(client, name="legacy PNG compatibility")
    with get_db(immediate=True) as conn:
        conn.execute("DELETE FROM archive_delivery_assets WHERE design_id=?", (design_id,))
        job = conn.execute(
            "SELECT id FROM nas_upload_jobs WHERE design_id=?", (design_id,)
        ).fetchone()
        conn.execute(
            "UPDATE nas_upload_jobs SET status='waiting',error=NULL,next_attempt_at=NULL "
            "WHERE id=?",
            (job["id"],),
        )

    class LoseFirst(ReadingRemote):
        def put_new(self, path, source):
            self.puts += 1
            self.files[path] = source.read() if hasattr(source, "read") else source
            if self.puts == 1:
                raise TimeoutError("first response lost")

    remote = LoseFirst()
    assert process_one(lambda: remote)
    first_path, first_bytes = next(iter(remote.files.items()))
    with get_db() as conn:
        item = conn.execute(
            "SELECT byte_count,sha256 FROM nas_upload_items "
            "WHERE job_id=? AND status='uploading'",
            (job["id"],),
        ).fetchone()
    assert item["byte_count"] == len(first_bytes)
    assert item["sha256"] == hashlib.sha256(first_bytes).hexdigest()

    client.post(
        "/api/nas-upload/enqueue",
        json={"batch_id": client.get(f"/api/designs/{design_id}").json()["batch_id"], "design_ids": [design_id]},
    )
    assert process_one(lambda: remote)
    assert remote.puts == 4
    assert remote.files[first_path] == first_bytes
    assert all(path.endswith(".png") for path in remote.files)

    _, content = _download(
        client,
        client.post(
            f"/api/export/design/{design_id}",
            headers={"X-User-Role": "operator"},
        ),
    )
    metadata, exported_images = _unzip_metadata(content)
    assert metadata["image_format"] == "png"
    assert all(name.endswith(".png") for name in exported_images)


def test_terminal_preparation_failure_is_explicit_and_existing_export_retries(client, monkeypatch):
    _, design_id = _archive_group(client, name="retry failed preparation")
    original_stage = delivery._stage_one_asset
    monkeypatch.setattr(delivery, "MAX_PREPARATION_ATTEMPTS", 1)

    def always_fail(*args, **kwargs):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(delivery, "_stage_one_asset", always_fail)
    try:
        optimize_archive_group(design_id)
    except RuntimeError:
        pass
    else:
        raise AssertionError("preparation failure must be raised")

    with get_db() as conn:
        failed_assets = conn.execute(
            "SELECT optimization_status FROM archive_delivery_assets WHERE design_id=?",
            (design_id,),
        ).fetchall()
        failed_job = conn.execute(
            "SELECT status,attempts FROM nas_upload_jobs WHERE design_id=?", (design_id,)
        ).fetchone()
    assert {row["optimization_status"] for row in failed_assets} == {"failed"}
    assert failed_job["status"] == "failed"
    original_attempts = failed_job["attempts"]

    reset_export = client.post(
        f"/api/export/design/{design_id}?retry_failed=1",
        headers={"X-User-Role": "operator"},
    )
    assert reset_export.status_code == 202
    process_pending()
    failed_export = client.post(
        f"/api/export/design/{design_id}?retry_failed=0",
        headers={"X-User-Role": "operator"},
    )
    assert failed_export.status_code == 409
    assert "再次点击现有导出入口重试" in failed_export.json()["detail"]["message"]
    with get_db() as conn:
        assert {
            row["optimization_status"]
            for row in conn.execute(
                "SELECT optimization_status FROM archive_delivery_assets WHERE design_id=?",
                (design_id,),
            )
        } == {"failed"}

    monkeypatch.setattr(delivery, "_stage_one_asset", original_stage)
    retried = client.post(
        f"/api/export/design/{design_id}?retry_failed=1",
        headers={"X-User-Role": "operator"},
    )
    assert retried.status_code == 202
    assert optimize_archive_group(design_id)
    retried = client.post(
        f"/api/export/design/{design_id}?retry_failed=0",
        headers={"X-User-Role": "operator"},
    )
    assert retried.status_code == 200, retried.text
    with get_db() as conn:
        current_job = conn.execute(
            "SELECT status,attempts FROM nas_upload_jobs WHERE design_id=?", (design_id,)
        ).fetchone()
        statuses = conn.execute(
            "SELECT optimization_status FROM archive_delivery_assets WHERE design_id=?",
            (design_id,),
        ).fetchall()
    assert current_job["attempts"] == original_attempts
    assert {row["optimization_status"] for row in statuses} == {"ready"}


def test_restart_recovery_concurrency_and_orphan_cleanup(client):
    _, design_id = _archive_group(client, name="restart and concurrency")
    orphan = ARCHIVE_DELIVERY_DIR / design_id / ".staging-interrupted"
    orphan.mkdir(parents=True)
    (orphan / "partial.png").write_bytes(b"partial")
    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE archive_delivery_assets SET optimization_status='optimizing' "
            "WHERE design_id=?",
            (design_id,),
        )
    archive_optimizer_worker.recover_interrupted()
    with get_db() as conn:
        assert {
            row["optimization_status"]
            for row in conn.execute(
                "SELECT optimization_status FROM archive_delivery_assets WHERE design_id=?",
                (design_id,),
            )
        } == {"pending"}

    results = []

    def prepare():
        results.append(optimize_archive_group(design_id))

    threads = [threading.Thread(target=prepare) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
    assert not orphan.exists()
    with get_db() as conn:
        assert {
            row["optimization_status"]
            for row in conn.execute(
                "SELECT optimization_status FROM archive_delivery_assets WHERE design_id=?",
                (design_id,),
            )
        } == {"ready"}

        assets = conn.execute(
            "SELECT slot_type,filename,canonical_path FROM archive_delivery_assets "
            "WHERE design_id=?",
            (design_id,),
        ).fetchall()
    legacy_paths = {}
    for asset in assets:
        source = ARCHIVE_DELIVERY_DIR / asset["canonical_path"]
        destination = ARCHIVE_DELIVERY_DIR / design_id / asset["filename"]
        shutil.move(source, destination)
        legacy_paths[asset["slot_type"]] = f"{design_id}/{asset['filename']}"
    with get_db(immediate=True) as conn:
        for slot_type, canonical_path in legacy_paths.items():
            conn.execute(
                "UPDATE archive_delivery_assets SET canonical_path=? "
                "WHERE design_id=? AND slot_type=?",
                (canonical_path, design_id, slot_type),
            )
    legacy_export = client.post(
        f"/api/export/design/{design_id}",
        headers={"X-User-Role": "operator"},
    )
    assert legacy_export.status_code == 200, legacy_export.text

    corrupted = ARCHIVE_DELIVERY_DIR / legacy_paths[assets[0]["slot_type"]]
    corrupted.write_bytes(b"hash mismatch")
    repaired_export = client.post(
        f"/api/export/design/{design_id}?retry_failed=0",
        headers={"X-User-Role": "operator"},
    )
    assert repaired_export.status_code == 202
    assert repaired_export.json()["status"] == "pending"
    assert optimize_archive_group(design_id)
    repaired_export = client.post(
        f"/api/export/design/{design_id}?retry_failed=0",
        headers={"X-User-Role": "operator"},
    )
    assert repaired_export.status_code == 200, repaired_export.text
    with get_db() as conn:
        repaired = conn.execute(
            "SELECT canonical_path,sha256 FROM archive_delivery_assets "
            "WHERE design_id=? AND slot_type=?",
            (design_id, assets[0]["slot_type"]),
        ).fetchone()
    repaired_path = ARCHIVE_DELIVERY_DIR / repaired["canonical_path"]
    assert hashlib.sha256(repaired_path.read_bytes()).hexdigest() == repaired["sha256"]


def test_delivery_verification_does_not_hold_write_transaction(client, monkeypatch):
    _, design_id = _archive_group(client, name="verification without write lock")
    assert optimize_archive_group(design_id)
    batch_id = client.get(f"/api/designs/{design_id}").json()["batch_id"]
    entered = threading.Event()
    release = threading.Event()
    import backend.app.export_service as export_service
    original_verify = export_service._verify_canonical_file

    def blocked_verify(row):
        entered.set()
        assert release.wait(2)
        return original_verify(row)

    monkeypatch.setattr(export_service, "_verify_canonical_file", blocked_verify)
    result = {}

    def inspect():
        result["delivery"] = inspect_archive_delivery(batch_id, [design_id])

    thread = threading.Thread(target=inspect)
    thread.start()
    assert entered.wait(2)
    with get_db(immediate=True) as conn:
        conn.execute(
            "UPDATE nas_upload_jobs SET updated_at=datetime('now','localtime') "
            "WHERE design_id=?",
            (design_id,),
        )
    release.set()
    thread.join()
    assert result["delivery"]["status"] == "ready"
