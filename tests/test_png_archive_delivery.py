import hashlib
import io
import zipfile
from pathlib import Path
from PIL import Image
from quad_snapshot import quad_version_snapshot

import backend.app.png_delivery_service as png_service
from backend.app.config import RAW_IMAGES_DIR
from backend.app.database import get_db
from backend.app.export_service import _collect_export_data, render_product_images
from backend.app.nas_upload_service import process_one, validate_upload_source
from backend.app.png_delivery_service import optimize_archive_group, process_pending
from backend.app.product_numbering import SLOT_ORDER
from backend.app.routers import designs as designs_router
from backend.app.task_worker import worker_instance
from backend.app.watermark_service import apply_watermark


def _prepare_group(client, name='PNG canonical delivery', count=1):
    batch = client.post('/api/batches', json={
        'name': name,
        'enabled_series': ['modern_business'],
        'target_archived_count': 1,
        'designs_per_series': count,
        'standby_designs_per_series': 0,
        'auto_start_panorama': True,
    }, headers={'X-User-Role': 'leader'}).json()
    worker_instance.process_next_task()
    designs = client.get(f"/api/designs/batch/{batch['id']}").json()
    for design in designs:
        client.post(f"/api/designs/{design['id']}/review_panorama", json={
            'action': 'approve',
            'expected_version_id': design['slots']['panorama']['current_version']['id'],
        }, headers={'X-User-Role': 'operator'})
        client.post(f"/api/designs/{design['id']}/start_downstream", headers={'X-User-Role': 'operator'})
    for _ in range(5 * count):
        worker_instance.process_next_task()
    return batch['id'], [design['id'] for design in designs]


def _archive(client, design_id):
    ready = client.get(f'/api/designs/{design_id}').json()
    return client.post(f'/api/designs/{design_id}/review_quad', json={
        'action': 'archive',
        'expected_slot_versions': quad_version_snapshot(ready),
    }, headers={'X-User-Role': 'operator'})


def _archive_group(client, name='PNG canonical delivery'):
    batch_id, design_ids = _prepare_group(client, name=name)
    response = _archive(client, design_ids[0])
    assert response.status_code == 200, response.text
    return batch_id, design_ids[0]


def test_archive_request_freezes_manifest_without_watermark_or_png_encoding(client, monkeypatch):
    batch_id, design_ids = _prepare_group(client, name='archive freeze only')
    design_id = design_ids[0]

    def forbidden(*args, **kwargs):
        raise AssertionError('archive request must not watermark or encode PNG')

    monkeypatch.setattr(designs_router, 'apply_watermark', forbidden)
    monkeypatch.setattr(Image.Image, 'save', forbidden)
    response = _archive(client, design_id)
    assert response.status_code == 200, response.text

    with get_db() as conn:
        assets = conn.execute(
            'SELECT * FROM archive_delivery_assets WHERE design_id=? ORDER BY slot_type',
            (design_id,),
        ).fetchall()
    assert len(assets) == 4
    assert all(row['optimization_status'] == 'pending' for row in assets)
    assert all(row['canonical_path'] is None for row in assets)
    assert all(row['sha256'] is None for row in assets)
    assert len({row['filename'] for row in assets}) == 4
    assert len({row['watermark_config_json'] for row in assets}) == 1


def test_new_archive_canonical_files_are_shared_by_nas_and_zip(client):
    batch_id, design_id = _archive_group(client)
    assert optimize_archive_group(design_id)
    with get_db() as conn:
        assets = conn.execute(
            'SELECT * FROM archive_delivery_assets WHERE design_id=? ORDER BY slot_type',
            (design_id,),
        ).fetchall()
        versions = validate_upload_source(conn, design_id)
        wm_cfg, designs = _collect_export_data(conn, batch_id, [design_id])
    assert len(assets) == 4
    assert {row['slot_type'] for row in assets} == set(versions)
    assert all(row['slot_version_id'] == versions[row['slot_type']] for row in assets)
    assert all(row['optimization_status'] == 'ready' for row in assets)
    assert all(row['sha256'] for row in assets)

    delivery_images = render_product_images(wm_cfg, designs[0])
    ordered_assets = sorted(assets, key=lambda row: SLOT_ORDER[row['slot_type']])
    assert [row['filename'] for row in ordered_assets] == [filename for _, filename, _ in delivery_images]
    for row, (_, _, path) in zip(ordered_assets, delivery_images):
        assert isinstance(path, Path)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256']

    archived = client.get(f'/api/designs/{design_id}').json()
    export = client.post(f'/api/export/design/{design_id}', headers={'X-User-Role': 'operator'})
    assert export.status_code == 200, export.text
    downloaded = client.get(
        f"/api/export/download/{export.json()['file_name']}",
        headers={'X-User-Role': 'operator'},
    )
    code = archived['product_code']
    number = int(code[4:])
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        for slot, filename, path in delivery_images:
            arcname = f"{code}/{filename}"
            assert arcname == f"{code}/{path.name}"
            assert archive.read(arcname) == path.read_bytes()

    with get_db(immediate=True) as conn:
        job = conn.execute('SELECT id FROM nas_upload_jobs WHERE design_id=?', (design_id,)).fetchone()
        conn.execute("UPDATE nas_upload_jobs SET status='waiting',error=NULL,next_attempt_at=NULL WHERE id=?", (job['id'],))

    class ReadingRemote:
        def __init__(self):
            self.files = {}
            self.puts = 0
        def close(self): pass
        def exists(self, path): return path in self.files
        def names(self, path): return set()
        def mkdir(self, path): pass
        def put_new(self, path, source):
            self.puts += 1
            self.files[path] = source.read()
        def verify(self, *args, **kwargs): pass

    remote = ReadingRemote()
    assert process_one(lambda: remote)
    with get_db() as conn:
        current = conn.execute('SELECT status,error FROM nas_upload_jobs WHERE id=?', (job['id'],)).fetchone()
    assert current['status'] == 'awaiting_review', dict(current)
    assert remote.puts == 4
    assert {remote.files[f'{code}/{path.name}'] for _, _, path in delivery_images} == {
        path.read_bytes() for _, _, path in delivery_images
    }


def test_level6_compression_failure_falls_back_to_valid_level1(client, monkeypatch):
    batch_id, design_id = _archive_group(client)

    def failed_candidate(*args, **kwargs):
        raise RuntimeError('candidate encoder failed')

    monkeypatch.setattr(png_service, '_encode_candidate', failed_candidate)
    assert optimize_archive_group(design_id)
    with get_db() as conn:
        assets = conn.execute(
            'SELECT slot_type,canonical_path,source_width,source_height,source_mode,sha256,byte_count,compress_mode '
            'FROM archive_delivery_assets WHERE design_id=? ORDER BY slot_type',
            (design_id,),
        ).fetchall()
        _, designs_data = _collect_export_data(conn, batch_id, [design_id])
    assert len(assets) == 4
    assert all(row['compress_mode'] == 'level1' for row in assets)
    delivery_images = render_product_images({}, designs_data[0])
    slots_by_type = {row['slot_type']: row for row in assets}
    for slot, _, path in delivery_images:
        row = slots_by_type[slot]
        assert row['compress_mode'] == 'level1'
        with Image.open(path) as delivered:
            delivered.load()
        assert delivered.format == 'PNG'
        assert (delivered.width, delivered.height) == (row['source_width'], row['source_height'])


def test_one_failed_group_does_not_starve_the_next_group(client, monkeypatch):
    _, first_design = _archive_group(client, name='failing PNG group')
    _, design_ids = _prepare_group(client, name='following PNG group')
    second_design = design_ids[0]
    second_archive = _archive(client, second_design)
    assert second_archive.status_code == 200, second_archive.text

    original_write = png_service._write_png_baseline
    def fail_first(asset, staging_dir):
        if asset['design_id'] == first_design:
            raise RuntimeError('first group failed')
        return original_write(asset, staging_dir)

    monkeypatch.setattr(png_service, '_write_png_baseline', fail_first)
    assert process_pending() == 1
    with get_db() as conn:
        first = conn.execute(
            'SELECT optimization_status,optimization_error,failure_count FROM archive_delivery_assets WHERE design_id=? LIMIT 1',
            (first_design,),
        ).fetchone()
        second = conn.execute(
            'SELECT optimization_status FROM archive_delivery_assets WHERE design_id=? LIMIT 1',
            (second_design,),
        ).fetchone()
    assert first['optimization_status'] == 'pending'
    assert first['failure_count'] == 1
    assert first['optimization_error']
    assert second['optimization_status'] == 'ready'


def test_nas_waits_until_canonical_png_is_published(client):
    _, design_id = _archive_group(client)
    with get_db(immediate=True) as conn:
        job = conn.execute('SELECT id FROM nas_upload_jobs WHERE design_id=?', (design_id,)).fetchone()
        conn.execute("UPDATE nas_upload_jobs SET status='waiting',error=NULL,next_attempt_at=NULL WHERE id=?", (job['id'],))

    class MustNotConnect:
        def __getattr__(self, name):
            raise AssertionError('NAS must not connect before canonical PNG is complete')

    assert process_one(lambda: MustNotConnect()) is False
    with get_db() as conn:
        current = conn.execute(
            'SELECT status,error,next_attempt_at,attempts FROM nas_upload_jobs WHERE id=?', (job['id'],)
        ).fetchone()
    assert current['status'] == 'waiting'
    assert current['error'] is None
    assert current['next_attempt_at'] is None
    assert current['attempts'] == 0


def test_terminal_png_preparation_failure_is_actionable_without_retry(client, monkeypatch):
    _, design_id = _archive_group(client)
    monkeypatch.setattr(png_service, 'MAX_PREPARATION_ATTEMPTS', 2)
    original_write = png_service._write_png_baseline

    def failed_write(asset, staging_dir):
        if asset['design_id'] == design_id:
            raise RuntimeError('disk failure simulation')
        return original_write(asset, staging_dir)

    monkeypatch.setattr(png_service, '_write_png_baseline', failed_write)
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE nas_upload_jobs SET status='waiting',next_attempt_at=NULL,attempts=4 WHERE design_id=?", (design_id,))
    process_pending()
    process_pending()
    with get_db() as conn:
        manifest = conn.execute(
            'SELECT optimization_status,optimization_error FROM archive_delivery_assets WHERE design_id=? LIMIT 1',
            (design_id,),
        ).fetchone()
        job = conn.execute(
            'SELECT status,error,attempts,next_attempt_at FROM nas_upload_jobs WHERE design_id=?',
            (design_id,),
        ).fetchone()
    assert manifest['optimization_status'] == 'failed'
    assert manifest['optimization_error'].startswith('规范图片连续后台准备失败')
    assert job['status'] == 'failed'
    assert job['error'].startswith('规范 PNG 后台准备终态失败')
    assert job['attempts'] == 4
    assert job['next_attempt_at'] is None


def test_technical_failure_list_filters_invalid_versions_but_retry_still_validates(client):
    batch = client.post('/api/batches', json={
        'name': 'technical failure filter',
        'enabled_series': ['modern_business'],
        'target_archived_count': 1,
        'designs_per_series': 6,
        'standby_designs_per_series': 0,
        'auto_start_panorama': True,
    }, headers={'X-User-Role': 'leader'}).json()
    for _ in range(5):
        worker_instance.process_next_task()
    with get_db(immediate=True) as conn:
        rows = conn.execute("""
            SELECT t.id AS task_id,t.slot_id,t.slot_version_id,t.design_id,t.status AS task_status
            FROM tasks t JOIN designs d ON d.id=t.design_id
            WHERE t.batch_id=? AND t.task_type='generate_panorama'
            ORDER BY d.created_at,t.id
        """, (batch['id'],)).fetchall()
        assert len(rows) >= 6
        stale, archived, eliminated, invalidated, _unused, unprocessed = rows[:6]
        eligible_task = unprocessed['task_id']
        eligible_version = f"ver_eligible_{eligible_task[:8]}"
        conn.execute(
            "INSERT INTO slot_versions(id,slot_id,design_id,prompt,process_status) VALUES (?,?,?,?, 'completed')",
            (eligible_version, unprocessed['slot_id'], unprocessed['design_id'], 'eligible'),
        )
        conn.execute('UPDATE slots SET current_version_id=? WHERE id=?', (eligible_version, unprocessed['slot_id']))
        conn.execute(
            "UPDATE tasks SET slot_version_id=?,status='failed',failure_stage='request_charge_unknown' WHERE id=?",
            (eligible_version, eligible_task),
        )
        replacement = f"ver_stale_{stale['task_id'][:8]}"
        conn.execute(
            "INSERT INTO slot_versions(id,slot_id,design_id,prompt,process_status) VALUES (?,?,?,?, 'completed')",
            (replacement, stale['slot_id'], stale['design_id'], 'newer'),
        )
        conn.execute('UPDATE slots SET current_version_id=? WHERE id=?', (replacement, stale['slot_id']))
        conn.execute("UPDATE tasks SET status='failed',failure_stage='request_charge_unknown' WHERE id IN (?,?,?,?,?)", (
            eligible_task, stale['task_id'], archived['task_id'], eliminated['task_id'], invalidated['task_id'],
        ))
        conn.execute("UPDATE designs SET status='archived',product_number=9001 WHERE id=?", (archived['design_id'],))
        conn.execute("UPDATE designs SET status='eliminated' WHERE id=?", (eliminated['design_id'],))
        conn.execute('UPDATE slot_versions SET is_invalidated=1 WHERE id=?', (invalidated['slot_version_id'],))

    response = client.get(
        f"/api/workbench/batches/{batch['id']}/technical-failures",
        headers={'X-User-Role': 'operator'},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['count'] == 1
    assert [item['task_id'] for item in payload['items']] == [eligible_task]
    assert all(item['retryable'] for item in payload['items'])

    retried = client.post(f"/api/workbench/batches/{batch['id']}/retry-technical-failures", json={
        'task_ids': [stale['task_id'], eligible_task],
        'accept_unknown_charge': True,
    }, headers={'X-User-Role': 'leader'})
    assert retried.status_code == 200
    results = {row['task_id']: row for row in retried.json()['results']}
    assert results[stale['task_id']]['ok'] is False
    assert results[eligible_task]['ok'] is True
