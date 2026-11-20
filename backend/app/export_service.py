import io
import hashlib
import os
import json
import uuid
import zipfile
import sqlite3
from pathlib import Path
from typing import List
from PIL import Image
from .config import ARCHIVE_DELIVERY_DIR, EXPORTS_DIR, RAW_IMAGES_DIR
from .database import get_db
from .watermark_service import (
    apply_watermark,
    ensure_watermark_config_schema,
    watermark_config,
)
from .security import safe_file_path
from .product_numbering import (
    format_product_code,
    product_image_name,
    product_delivery_name,
    SLOT_ORDER,
)

def render_product_images(wm_cfg, design):
    """Return the canonical frozen files, or legacy PNG bytes when none exist."""
    canonical_images = design.get("canonical_images") or []
    if canonical_images:
        if len(canonical_images) != 4:
            raise ValueError("归档规范图片不完整")
        return [(slot, filename, image_path) for slot, filename, image_path in canonical_images]

    result = []
    for slot in sorted(design['slots'], key=lambda item: SLOT_ORDER[item['slot_type']]):
        raw_name = slot['raw_image_path']
        if not raw_name:
            raise ValueError(f"设计 {design['design_code']} 槽位 {slot['slot_type']} 缺少原图路径")
        raw_path = safe_file_path(RAW_IMAGES_DIR, raw_name)
        if not raw_path.exists():
            raise FileNotFoundError(f'原图文件未找到: {raw_name}')
        try:
            with Image.open(raw_path) as img:
                source_size = img.size
                image = apply_watermark(img, watermark_cfg=wm_cfg)
                if image.size != source_size:
                    raise ValueError('水印处理改变了图片尺寸')
                buf = io.BytesIO()
                image.save(buf, format='PNG', compress_level=1)
                with Image.open(io.BytesIO(buf.getvalue())) as checked:
                    if checked.format != 'PNG' or checked.size != source_size:
                        raise ValueError('导出图片格式或尺寸异常')
                    checked.verify()
        except Exception as exc:
            raise RuntimeError(f'水印施加失败，导出已硬阻断: {exc}') from exc
        result.append((slot['slot_type'], product_image_name(design['product_number'], design['archived_at'], slot['slot_type']), buf.getvalue()))
    return result


def build_archive_delivery_manifest(product_number, archived_at, wm_cfg, slots, image_format):
    """Freeze versions, names, source paths, format, and watermark without encoding."""
    if image_format not in ('png', 'jpg'):
        raise ValueError('归档图片格式无效')
    frozen_watermark = watermark_config(wm_cfg)
    result = []
    for slot in sorted(slots, key=lambda item: SLOT_ORDER[item['slot_type']]):
        raw_name = slot['raw_image_path']
        if not raw_name:
            raise ValueError(f"槽位 {slot['slot_type']} 缺少原图路径")
        filename = product_delivery_name(
            product_number, archived_at, slot['slot_type'], image_format
        )
        result.append({
            'slot_type': slot['slot_type'],
            'slot_version_id': slot['vid'],
            'raw_image_path': raw_name,
            'filename': filename,
            'image_format': image_format,
            'archived_at': archived_at,
            'watermark_config': frozen_watermark,
        })
    if len(result) != 4 or len({item['slot_type'] for item in result}) != 4 or len({item['filename'] for item in result}) != 4:
        raise ValueError('归档规范图片版本、槽位或文件名异常')
    return result


def archive_delivery_is_ready(conn, design_id):
    assets = conn.execute(
        'SELECT slot_type,optimization_status FROM archive_delivery_assets WHERE design_id=?',
        (design_id,),
    ).fetchall()
    if not assets:
        return True
    required = {'panorama', 'white_isolated', 'twin_bed', 'nightstand_closeup'}
    return len(assets) == 4 and {r['slot_type'] for r in assets} == required and all(
        r['optimization_status'] == 'ready' for r in assets
    )


def retry_failed_archive_delivery(conn, design_id):
    failed = conn.execute("""
        SELECT COUNT(*) AS count
        FROM archive_delivery_assets
        WHERE design_id=? AND optimization_status='failed'
    """, (design_id,)).fetchone()['count']
    if not failed:
        return False
    conn.execute("""
        UPDATE archive_delivery_assets
        SET optimization_status='pending',optimization_error=NULL,failure_count=0,
            updated_at=datetime('now','localtime')
        WHERE design_id=? AND optimization_status='failed'
    """, (design_id,))
    conn.execute("""
        UPDATE nas_upload_jobs
        SET status='waiting',error='规范图片已重新排队，后台准备完成后继续上传',
            next_attempt_at=NULL,updated_at=datetime('now','localtime')
        WHERE design_id=? AND status='failed'
          AND error LIKE '规范 % 后台准备终态失败%'
    """, (design_id,))
    return True


def reset_failed_archive_delivery(conn, design_ids):
    return sum(retry_failed_archive_delivery(conn, design_id) for design_id in design_ids)


DELIVERY_COLUMNS = """
    design_id,slot_type,slot_version_id,raw_image_path,filename,image_format,
    archived_at,canonical_path,sha256,byte_count,optimization_status,optimization_error
"""


def _delivery_signature(rows):
    return tuple(sorted(
        (
            row['slot_type'],
            row['slot_version_id'],
            row['raw_image_path'],
            row['filename'],
            row['image_format'],
            row['archived_at'],
            row['canonical_path'],
            row['sha256'],
            row['byte_count'],
            row['optimization_status'],
        )
        for row in rows
    ))


def _load_delivery_rows(design_ids):
    if not design_ids:
        return {}
    placeholders = ",".join("?" for _ in design_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {DELIVERY_COLUMNS} FROM archive_delivery_assets "
            f"WHERE design_id IN ({placeholders})",
            design_ids,
        ).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row['design_id'], []).append(dict(row))
    return grouped


def _queue_repair_if_unchanged(design_id, expected_rows, error):
    expected_signature = _delivery_signature(expected_rows)
    with get_db(immediate=True) as conn:
        current = conn.execute(
            f"SELECT {DELIVERY_COLUMNS} FROM archive_delivery_assets WHERE design_id=?",
            (design_id,),
        ).fetchall()
        if (
            len(current) != 4
            or any(row['optimization_status'] != 'ready' for row in current)
            or _delivery_signature(current) != expected_signature
        ):
            return False
        updated = conn.execute("""
            UPDATE archive_delivery_assets
            SET canonical_path=NULL,sha256=NULL,byte_count=NULL,
                compress_mode=NULL,jpeg_quality=NULL,
                optimization_status='pending',optimization_error=?,
                failure_count=failure_count+1,
                updated_at=datetime('now','localtime')
            WHERE design_id=? AND optimization_status='ready'
        """, (error[:200], design_id)).rowcount
        return updated == 4


def _delivery_payload(state, message, design_ids, rows_by_design, retry_after_ms=None):
    items = []
    for design_id in design_ids:
        for row in sorted(rows_by_design.get(design_id, []), key=lambda item: item['slot_type']):
            items.append({
                'design_id': design_id,
                'slot_type': row['slot_type'],
                'filename': row['filename'],
                'image_format': row['image_format'] or 'png',
                'status': row['optimization_status'],
                'byte_count': row['byte_count'],
                'error': row['optimization_error'],
            })
    payload = {
        'status': state,
        'delivery_state': state,
        'message': message,
        'items': items,
    }
    if retry_after_ms is not None:
        payload['retry_after_ms'] = retry_after_ms
    return payload


def inspect_archive_delivery(_batch_id, design_ids, retry_failed=False):
    """Run one short delivery check; file verification happens outside DB write transactions."""
    if retry_failed:
        with get_db(immediate=True) as conn:
            reset_failed_archive_delivery(conn, design_ids)

    rows_by_design = _load_delivery_rows(design_ids)
    for design_id in design_ids:
        rows = rows_by_design.get(design_id, [])
        if not rows:
            continue
        formats = {row['image_format'] or 'png' for row in rows}
        if len(rows) != 4 or len(formats) != 1 or formats not in ({'png'}, {'jpg'}):
            return _delivery_payload(
                'failed',
                f'设计 {design_id} 的归档图片清单不完整或格式不一致',
                design_ids,
                rows_by_design,
            )
        statuses = {row['optimization_status'] for row in rows}
        errors = [
            row['optimization_error']
            for row in rows
            if row['optimization_error']
        ]
        if 'failed' in statuses:
            from .png_delivery_service import INPUT_ERROR_IMAGE_CORRUPT, INPUT_ERROR_IMAGE_MISSING
            input_error = next(
                (error for error in errors if error in (
                    INPUT_ERROR_IMAGE_CORRUPT,
                    INPUT_ERROR_IMAGE_MISSING,
                )),
                None,
            )
            if input_error:
                return _delivery_payload(
                    'input_error',
                    f'规范图片后台准备失败：{input_error}。请修复原图后再次点击现有导出入口重试。',
                    design_ids,
                    rows_by_design,
                )
            return _delivery_payload(
                'failed',
                f'规范图片后台准备失败：{errors[0] if errors else "后台准备进入终态失败"}。'
                '请修复后再次点击现有导出入口重试。',
                design_ids,
                rows_by_design,
            )
        if not all(row['optimization_status'] == 'ready' for row in rows):
            from .png_delivery_service import INPUT_ERROR_IMAGE_CORRUPT, INPUT_ERROR_IMAGE_MISSING
            input_error = next(
                (error for error in errors if error in (
                    INPUT_ERROR_IMAGE_CORRUPT,
                    INPUT_ERROR_IMAGE_MISSING,
                )),
                None,
            )
            if input_error:
                return _delivery_payload(
                    'input_error',
                    f'规范图片后台准备失败：{input_error}。请修复原图后再次点击现有导出入口重试。',
                    design_ids,
                    rows_by_design,
                )
            return _delivery_payload(
                'pending',
                '规范图片正在后台准备',
                design_ids,
                rows_by_design,
                retry_after_ms=1000,
            )

    repair_errors = {}
    for design_id in design_ids:
        rows = rows_by_design.get(design_id, [])
        if not rows:
            continue
        try:
            for row in rows:
                _verify_canonical_file(row)
        except (ValueError, FileNotFoundError) as exc:
            repair_errors[design_id] = f'规范图片文件校验失败，已重新排队：{exc}'

    if repair_errors:
        repaired = False
        for design_id, error in repair_errors.items():
            repaired = _queue_repair_if_unchanged(
                design_id,
                rows_by_design[design_id],
                error,
            ) or repaired
        if repaired:
            return _delivery_payload(
                'pending',
                '规范图片文件校验失败，已重新加入后台准备',
                design_ids,
                _load_delivery_rows(design_ids),
                retry_after_ms=1000,
            )
        return _delivery_payload(
            'failed',
            '规范图片校验失败且后台状态已被其他任务改变，未执行覆盖；请重新发起导出',
            design_ids,
            _load_delivery_rows(design_ids),
        )
    return _delivery_payload('ready', '规范图片已准备完成', design_ids, rows_by_design)


def _verify_canonical_file(asset):
    relative_path = Path(asset['canonical_path'])
    if (
        len(relative_path.parts) not in (2, 3)
        or relative_path.parts[0] != asset['design_id']
        or '..' in relative_path.parts
        or relative_path.parts[-1] != asset['filename']
    ):
        raise ValueError(f"归档规范图片路径非法: {asset['filename']}")
    path = (ARCHIVE_DELIVERY_DIR / relative_path).resolve()
    root = ARCHIVE_DELIVERY_DIR.resolve()
    expected_parent = (
        root / asset['design_id']
        if len(relative_path.parts) == 2
        else root / asset['design_id']
    )
    actual_group_dir = (
        path.parent
        if len(relative_path.parts) == 2
        else path.parent.parent
    )
    if actual_group_dir != expected_parent:
        raise ValueError(f"归档规范图片路径非法: {asset['filename']}")
    if not path.is_file():
        raise ValueError(f"归档规范图片文件缺失: {asset['filename']}")
    if path.stat().st_size != asset['byte_count']:
        raise ValueError(f"归档规范图片大小校验失败: {asset['filename']}")
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != asset['sha256']:
        raise ValueError(f"归档规范图片哈希校验失败: {asset['filename']}")
    return path


# ---------------------------------------------------------------------------
# Phase 1 – Read-only DB access: validate designs and collect all data needed
# ---------------------------------------------------------------------------

def _collect_export_data(
    conn: sqlite3.Connection,
    batch_id: str,
    design_ids: List[str],
) -> tuple:
    """
    Validate every requested design and return all data as plain Python dicts.
    This is the ONLY phase that needs the database; it must complete quickly so
    the caller can release the DB lock before starting heavy image I/O.
    """
    cur = conn.cursor()
    ensure_watermark_config_schema(conn)
    cur.execute("""
        SELECT renderer, logo_asset, logo_width_ratio, logo_left_ratio, logo_top_ratio,
               main_text, repeat_text, opacity, font_size, position
        FROM watermark_configs WHERE batch_id = ?
    """, (batch_id,))
    wm_row = cur.fetchone()
    wm_cfg = watermark_config(dict(wm_row) if wm_row else None)

    designs_data = []
    for did in design_ids:
        cur.execute("""
            SELECT id, design_code, series_id, status, structured_spec, visual_features, archived_at, product_number
            FROM designs WHERE id = ? AND batch_id = ?
        """, (did, batch_id))
        d_row = cur.fetchone()
        if not d_row:
            raise ValueError(f"设计 {did} 不存在")
        if d_row["status"] != "archived":
            raise ValueError(f"设计 {d_row['design_code']} 尚未完成终审归档，禁止导出！")
        if d_row['product_number'] is None:
            raise ValueError(f"设计 {d_row['design_code']} 已归档但尚未分配正式产品编号，无法按规范导出")

        design_code = d_row["design_code"]
        cur.execute("""
            SELECT s.slot_type, sv.id AS version_id, sv.version_num, sv.raw_image_path,
                   sv.depends_on_panorama_version_id
            FROM slots s
            JOIN slot_versions sv ON s.current_version_id = sv.id
            WHERE s.design_id = ?
        """, (did,))
        slot_rows = cur.fetchall()

        found_types = {r["slot_type"] for r in slot_rows}
        required_types = {"panorama", "white_isolated", "twin_bed", "nightstand_closeup"}
        if found_types != required_types:
            raise ValueError(
                f"设计 {design_code} 的四图槽位异常，缺少: {required_types - found_types}，额外: {found_types - required_types}，拒绝导出"
            )

        panorama_version = next(r["version_id"] for r in slot_rows if r["slot_type"] == "panorama")
        for r in slot_rows:
            if r["slot_type"] == "panorama":
                continue
            if r["depends_on_panorama_version_id"] != panorama_version:
                raise ValueError(f"设计 {design_code} 槽位 {r['slot_type']} 依赖的归档全景版本不一致，拒绝导出")

        asset_rows = cur.execute("""
            SELECT design_id,slot_type,slot_version_id,raw_image_path,filename,canonical_path,
                   source_width,source_height,source_mode,watermark_config_json,
                   sha256,byte_count,compress_mode,jpeg_quality,image_format,archived_at,
                   optimization_status
            FROM archive_delivery_assets WHERE design_id=?
        """, (did,)).fetchall()
        verified_canonical_images = []
        frozen_watermark = None
        delivery_format = None
        if asset_rows:
            if len(asset_rows) != 4 or {r['slot_type'] for r in asset_rows} != required_types:
                raise ValueError(f"设计 {design_code} 归档规范图片不完整")
            formats = {r['image_format'] or 'png' for r in asset_rows}
            if len(formats) != 1 or formats not in ({'png'}, {'jpg'}):
                raise ValueError(f"设计 {design_code} 归档图片格式不一致")
            delivery_format = next(iter(formats))
            slots_by_type = {r['slot_type']: r for r in slot_rows}
            for asset in sorted(asset_rows, key=lambda item: SLOT_ORDER[item['slot_type']]):
                source_slot = slots_by_type[asset['slot_type']]
                if asset['slot_version_id'] != source_slot['version_id']:
                    raise ValueError(f"设计 {design_code} 槽位 {asset['slot_type']} 的归档版本已变化，拒绝导出")
                if asset['raw_image_path'] != source_slot['raw_image_path']:
                    raise ValueError(f"设计 {design_code} 槽位 {asset['slot_type']} 的归档原图已变化，拒绝导出")
                if asset['archived_at'] and asset['archived_at'] != d_row['archived_at']:
                    raise ValueError(f"设计 {design_code} 槽位 {asset['slot_type']} 的归档时间已变化，拒绝导出")
                if asset['optimization_status'] != 'ready':
                    label = 'JPG' if delivery_format == 'jpg' else 'PNG'
                    raise ValueError(f"设计 {design_code} 的规范 {label} 正在后台处理，暂不能导出")
                verified_canonical_images.append(
                    (asset['slot_type'], asset['filename'], _verify_canonical_file(asset))
                )
                frozen_watermark = json.loads(asset['watermark_config_json'])
            if len({item[1] for item in verified_canonical_images}) != 4:
                raise ValueError(f"设计 {design_code} 归档文件名异常")

        designs_data.append({
            "id": d_row["id"],
            "design_code": design_code,
            "product_number": d_row['product_number'],
            "series_id": d_row["series_id"],
            "structured_spec": json.loads(d_row["structured_spec"]) if d_row["structured_spec"] else {},
            "visual_features": json.loads(d_row["visual_features"]) if d_row["visual_features"] else {},
            "archived_at": d_row["archived_at"],
            "slots": [
                {
                    "slot_type": r["slot_type"],
                    "version_id": r["version_id"],
                    "version_num": r["version_num"],
                    "raw_image_path": r["raw_image_path"],
                    "depends_on_panorama_version_id": r["depends_on_panorama_version_id"],
                }
                for r in slot_rows
            ],
        })
        if verified_canonical_images:
            if len(verified_canonical_images) != 4:
                verified_canonical_images = []
            designs_data[-1]["canonical_images"] = verified_canonical_images
        if frozen_watermark is not None:
            designs_data[-1]["frozen_watermark"] = frozen_watermark
        if delivery_format is not None:
            designs_data[-1]["delivery_format"] = delivery_format
        if asset_rows and any(row['jpeg_quality'] for row in asset_rows):
            designs_data[-1]["jpeg_quality"] = max(
                row['jpeg_quality'] for row in asset_rows if row['jpeg_quality']
            )

    return wm_cfg, designs_data


# ---------------------------------------------------------------------------
# Phase 2 – Pure file I/O: watermark images and assemble ZIP (no DB lock)
# ---------------------------------------------------------------------------

def _assemble_watermarked_zip(wm_cfg, designs_data: list, batch_id: str) -> tuple:
    """
    Watermark every image and write the ZIP to a temp file, then rename it into
    place atomically.  No database connection is held here.
    """
    zip_filename = f"export_{batch_id}_{uuid.uuid4().hex[:8]}.zip"
    final_zip_path = EXPORTS_DIR / zip_filename
    temp_zip_path = final_zip_path.with_name(f".{zip_filename}.{uuid.uuid4().hex[:8]}.tmp")

    zf = zipfile.ZipFile(temp_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED)
    try:
        for d in designs_data:
            design_code = d["design_code"]
            for _, filename, img_bytes in render_product_images(wm_cfg, d):
                code = format_product_code(d['product_number'])
                arcname = f"{code}/{filename}"
                if isinstance(img_bytes, Path):
                    zf.write(img_bytes, arcname)
                else:
                    zf.writestr(arcname, img_bytes)

            meta = {
                "design_id": d["id"],
                "design_code": design_code,
                "product_code": format_product_code(d['product_number']),
                "image_format": d.get("delivery_format", "png"),
                "series_id": d["series_id"],
                "structured_spec": d["structured_spec"],
                "visual_features": d["visual_features"],
                "archived_at": d["archived_at"],
                "watermark_applied": d.get("frozen_watermark") or wm_cfg,
                "slots": [
                    {
                        "slot_type": r["slot_type"],
                        "version_id": r.get("version_id"),
                        "version_num": r["version_num"],
                        "depends_on_panorama": r["depends_on_panorama_version_id"],
                    }
                    for r in d["slots"]
                ],
            }
            zf.writestr(
                f"METADATA-{format_product_code(d['product_number'])}.json",
                json.dumps(meta, ensure_ascii=False, indent=2),
            )

        zf.close()
        # Publish only after the complete ZIP has been flushed and closed.
        os.replace(temp_zip_path, final_zip_path)
    except Exception:
        try:
            zf.close()
        except Exception:
            pass
        try:
            temp_zip_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    return zip_filename, final_zip_path


# ---------------------------------------------------------------------------
# Phase 3 – Write-back DB access: record every exported design individually
# ---------------------------------------------------------------------------

def _record_exports(
    conn: sqlite3.Connection,
    batch_id: str,
    design_ids: List[str],
    zip_filename: str,
    final_zip_path: Path,
    export_type: str,
    actor_name: str,
    actor_role: str,
) -> None:
    """
    Insert ONE export_records row per design_id so the UI can later mark each
    card as "already exported" by matching design_id.  All rows share the same
    zip file name for re-download purposes.
    """
    file_size = final_zip_path.stat().st_size
    for did in design_ids:
        export_id = f"exp_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO export_records (
                id, batch_id, design_id, export_type,
                file_name, file_path, file_size, exported_by, exported_role
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                export_id, batch_id, did, export_type,
                zip_filename, str(final_zip_path), file_size, actor_name, actor_role,
            ),
        )
