import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from backend.app.product_backfill import make_manifest, apply_manifest
from backend.app.product_numbering import allocate_product_number, product_image_name
from backend.app import export_service


def database(path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def test_backfill_order_snapshot_and_idempotence(tmp_path):
    conn = database(tmp_path / 'history.db')
    conn.executescript('''CREATE TABLE batches(id TEXT PRIMARY KEY,name TEXT);
    CREATE TABLE designs(id TEXT PRIMARY KEY,batch_id TEXT,design_code TEXT,status TEXT,archived_at TEXT,product_number INTEGER UNIQUE);
    CREATE TABLE product_number_state(id INTEGER PRIMARY KEY,ready INTEGER,last_number INTEGER);
    INSERT INTO product_number_state VALUES(1,0,0);
    INSERT INTO batches VALUES('production','生产'),('test','验收');
    INSERT INTO designs VALUES('b','production','B','archived','2026-11-02 10:00:00',NULL),
     ('a','production','A','archived','2026-11-01 10:00:00',NULL),
     ('t','test','T','archived','2026-10-01 10:00:00',NULL);''')
    manifest = make_manifest(conn, ['production'])
    assert [r['id'] for r in manifest['assignments']] == ['a', 'b']
    conn.execute("UPDATE designs SET archived_at='2026-10-02 10:00:00' WHERE id='t'")
    with pytest.raises(ValueError, match='变化'):
        apply_manifest(conn, manifest)
    assert not conn.execute('SELECT count(*) FROM designs WHERE product_number IS NOT NULL').fetchone()[0]
    conn.execute("UPDATE designs SET archived_at='2026-10-01 10:00:00' WHERE id='t'")
    conn.execute('BEGIN IMMEDIATE')
    apply_manifest(conn, manifest)
    conn.commit()
    apply_manifest(conn, manifest)
    assert [tuple(r) for r in conn.execute('SELECT id,product_number FROM designs ORDER BY id')] == [('a',1),('b',2),('t',None)]
    conn.close()


def test_concurrent_allocation_and_transaction_rollback(tmp_path):
    path = tmp_path / 'numbers.db'
    conn = database(path)
    conn.executescript('CREATE TABLE product_number_state(id INTEGER PRIMARY KEY,ready INTEGER,last_number INTEGER); INSERT INTO product_number_state VALUES(1,1,0);')
    conn.close()
    def allocate(_):
        conn = database(path)
        conn.execute('BEGIN IMMEDIATE')
        value = allocate_product_number(conn)
        conn.commit()
        conn.close()
        return value
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sorted(pool.map(allocate, range(20))) == list(range(1,21))
    conn = database(path)
    conn.execute('BEGIN IMMEDIATE')
    assert allocate_product_number(conn) == 21
    conn.rollback()
    conn.execute('BEGIN IMMEDIATE')
    assert allocate_product_number(conn) == 21
    conn.commit()
    conn.close()


def test_zip_names_content_and_original_dimensions(tmp_path, monkeypatch):
    monkeypatch.setattr(export_service, 'RAW_IMAGES_DIR', tmp_path)
    monkeypatch.setattr(export_service, 'EXPORTS_DIR', tmp_path)
    monkeypatch.setattr(export_service, 'apply_watermark', lambda img, **kwargs: img.copy())
    Image.new('RGB', (77, 91), 'white').save(tmp_path / 'source.png')
    types = ['panorama', 'white_isolated', 'twin_bed', 'nightstand_closeup']
    slots = [dict(slot_type=t, version_num=1, raw_image_path='source.png', depends_on_panorama_version_id=None) for t in types]
    data = [dict(id='d', design_code='MB-01', product_number=1, series_id='s', structured_spec={}, visual_features={}, archived_at='2026-11-02 10:00:00', slots=slots)]
    _, path = export_service._assemble_watermarked_zip({}, data, 'b')
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert names == [f'TFMK001/TFMK001-X611-{n:02d}-ST.png' for n in range(1,5)] + ['METADATA-TFMK001.json']
        assert Image.open(io.BytesIO(z.read(names[0]))).size == (77,91)
        assert json.loads(z.read(names[-1]))['design_code'] == 'MB-01'
    assert product_image_name(1000, '2027-01-01 00:00:00', 'panorama') == 'TFMK1000-X71-04-ST.png'
