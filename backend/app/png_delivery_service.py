"""Background canonical image preparation for newly archived four-image groups."""
import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import statistics
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image, JpegImagePlugin, UnidentifiedImageError

from .config import (
    ARCHIVE_DELIVERY_DIR,
    DB_PATH,
    MAX_ARCHIVE_JPG_BYTES,
    RAW_IMAGES_DIR,
    STORAGE_DIR,
)
from .database import get_db
from .product_numbering import SLOT_ORDER
from .security import safe_file_path
from .watermark_service import apply_watermark, watermark_config


logger = logging.getLogger(__name__)

REQUIRED_SLOTS = ('panorama', 'white_isolated', 'twin_bed', 'nightstand_closeup')
MIN_LEVEL6_SAVINGS = 0.05
MIN_OPTIMIZE_ADDITIONAL_SAVINGS = 0.05
MAX_OPTIMIZE_EXTRA_SECONDS = 2.0
MAX_PREPARATION_ATTEMPTS = 5
ARCHIVE_JPG_QUALITIES = (95, 94, 93, 92)
INPUT_ERROR_IMAGE_CORRUPT = '原图损坏或格式无效'
INPUT_ERROR_IMAGE_MISSING = '原图文件缺失'


def _safe_preparation_error(exc):
    if isinstance(exc, UnidentifiedImageError):
        return INPUT_ERROR_IMAGE_CORRUPT
    if isinstance(exc, FileNotFoundError):
        return INPUT_ERROR_IMAGE_MISSING
    message = str(exc)
    if message in (
        '归档规范图片清单不完整',
        '归档规范图片格式不一致',
        'PNG 编码改变了像素',
        'PNG 格式、尺寸或色彩模式变化',
        'JPG 格式或尺寸变化',
        'JPG 不是 4:4:4',
        '水印处理改变了图片尺寸',
        '归档版本或后台任务状态已变化',
        '规范图片清单未能原子发布',
        '归档图片格式无效',
    ):
        return message
    return '规范图片后台准备异常，请查看后台日志'


def _png_pixel_hash(image):
    return hashlib.sha256(image.tobytes()).hexdigest()


def _validate_saved_png(path, expected_size, expected_mode, expected_pixel_hash):
    with Image.open(path) as image:
        if image.format != 'PNG' or image.size != expected_size or image.mode != expected_mode:
            raise ValueError('PNG 格式、尺寸或色彩模式变化')
        image.load()
        if _png_pixel_hash(image) != expected_pixel_hash:
            raise ValueError('PNG 编码改变了像素')


def _validate_saved_jpg(path, expected_size):
    with Image.open(path) as image:
        if image.format != 'JPEG' or image.size != expected_size:
            raise ValueError('JPG 格式或尺寸变化')
        if JpegImagePlugin.get_sampling(image) != 0:
            raise ValueError('JPG 不是 4:4:4')
        image.load()


def encode_png_candidates(image):
    """Encode benchmark candidates one at a time; used only by the benchmark."""
    candidates = {}
    for mode, kwargs in (
        ('level1', {'compress_level': 1}),
        ('level6', {'compress_level': 6}),
        ('optimized', {'compress_level': 6, 'optimize': True}),
    ):
        output = io.BytesIO()
        started = time.perf_counter()
        image.save(output, format='PNG', **kwargs)
        candidates[mode] = {
            'png_bytes': output.getvalue(),
            'seconds': time.perf_counter() - started,
        }
    return candidates


def _benchmark_one_source(conn, design, slot):
    if not slot['raw_image_path']:
        raise ValueError('归档原图路径为空')
    raw_path = safe_file_path(RAW_IMAGES_DIR, slot['raw_image_path'])
    wm_row = conn.execute(
        'SELECT * FROM watermark_configs WHERE batch_id=?', (design['batch_id'],)
    ).fetchone()
    wm_cfg = watermark_config(dict(wm_row) if wm_row else None)
    with Image.open(raw_path) as source:
        source.load()
        watermarked = apply_watermark(source, watermark_cfg=wm_cfg)
        if watermarked.size != source.size:
            raise ValueError('水印处理改变了图片尺寸')
        watermarked.load()
        reference_hash = _png_pixel_hash(watermarked)
        candidates = encode_png_candidates(watermarked)

    result = {}
    valid = True
    for mode, candidate in candidates.items():
        try:
            with Image.open(io.BytesIO(candidate['png_bytes'])) as decoded:
                decoded.load()
                valid = (
                    decoded.format == 'PNG'
                    and decoded.size == watermarked.size
                    and decoded.mode == watermarked.mode
                    and _png_pixel_hash(decoded) == reference_hash
                )
        except Exception:
            valid = False
        result[mode] = len(candidate['png_bytes'])
        result[f'{mode}_seconds'] = candidate['seconds']
    return result, valid


def benchmark_existing_archives(limit=10):
    """Read local archived sources only; this function never contacts an upstream."""
    if limit <= 0:
        raise ValueError('基准组数必须大于零')
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        designs = conn.execute("""
            SELECT d.id,d.batch_id,d.design_code
            FROM designs d
            WHERE d.status='archived'
            ORDER BY d.rowid DESC LIMIT ?
        """, (limit,)).fetchall()
        groups = []
        for design in designs:
            slots = conn.execute("""
                SELECT s.slot_type,sv.raw_image_path
                FROM slots s JOIN slot_versions sv ON sv.id=s.current_version_id
                WHERE s.design_id=? AND s.slot_type IN (?,?,?,?)
            """, (design['id'], *REQUIRED_SLOTS)).fetchall()
            if len(slots) != 4 or {r['slot_type'] for r in slots} != set(REQUIRED_SLOTS):
                continue
            slots = sorted(slots, key=lambda item: SLOT_ORDER[item['slot_type']])
            totals = {mode: 0 for mode in ('level1', 'level6', 'optimized')}
            durations = {mode: 0.0 for mode in totals}
            group_valid = True
            for slot in slots:
                sizes, valid = _benchmark_one_source(conn, design, slot)
                group_valid = group_valid and valid
                for mode in totals:
                    totals[mode] += sizes[mode]
                    durations[mode] += sizes[f'{mode}_seconds']
            groups.append({'design_id': design['id'], 'valid': group_valid, **totals, **{
                f'{mode}_seconds': durations[mode] for mode in durations
            }})
    finally:
        conn.close()

    if not groups:
        return {'policy': 'level1', 'decided': False, 'group_count': 0, 'reason': '没有可访问的本地归档组'}

    total = {mode: sum(group[mode] for group in groups) for mode in ('level1', 'level6', 'optimized')}
    median_seconds = {
        mode: statistics.median(group[f'{mode}_seconds'] for group in groups)
        for mode in total
    }
    level6_savings = (total['level1'] - total['level6']) / total['level1']
    optimize_savings = (total['level6'] - total['optimized']) / total['level6']
    optimize_extra = statistics.median(
        group['optimized_seconds'] - group['level6_seconds'] for group in groups
    )
    all_valid = all(group['valid'] for group in groups)
    policy = 'level1'
    reason = '候选 PNG 较大、无效或像素变化'
    if all_valid and level6_savings >= MIN_LEVEL6_SAVINGS:
        policy = 'level6'
        reason = f'level 6 相对 level 1 节省 {level6_savings:.2%}'
        if optimize_savings >= MIN_OPTIMIZE_ADDITIONAL_SAVINGS and optimize_extra <= MAX_OPTIMIZE_EXTRA_SECONDS:
            policy = 'optimized'
            reason = f'optimize 相对 level 6 额外节省 {optimize_savings:.2%}'
    return {
        'policy': policy,
        'decided': len(groups) == limit and all_valid,
        'group_count': len(groups),
        'reason': reason,
        'total_bytes': total,
        'level6_savings_ratio': level6_savings,
        'optimize_additional_savings_ratio': optimize_savings,
        'median_group_seconds': median_seconds,
        'optimize_extra_seconds_median': optimize_extra,
        'all_pixels_identical': all_valid,
    }


def _load_policy():
    with get_db() as conn:
        row = conn.execute('SELECT policy FROM png_delivery_settings WHERE id=1').fetchone()
    policy = row['policy'] if row else 'level6'
    if policy not in ('level1', 'level6', 'optimized'):
        raise ValueError('PNG 传输压缩策略无效')
    return policy


def _claim_group(design_id):
    with get_db(immediate=True) as conn:
        assets = conn.execute("""
            SELECT a.slot_type,a.slot_version_id,a.raw_image_path,a.image_format,a.filename,
                   a.optimization_status,
                   s.current_version_id AS slot_current_version_id,
                   sv.raw_image_path AS current_raw_image_path
            FROM archive_delivery_assets a
            JOIN slots s ON s.design_id=a.design_id AND s.slot_type=a.slot_type
            JOIN slot_versions sv ON sv.id=s.current_version_id
            WHERE a.design_id=?
        """, (design_id,)).fetchall()
        if len(assets) != 4 or {r['slot_type'] for r in assets} != set(REQUIRED_SLOTS):
            return False
        image_formats = {r['image_format'] or 'png' for r in assets}
        if len(image_formats) != 1 or image_formats not in ({'png'}, {'jpg'}):
            return False
        if any(
            r['optimization_status'] != 'pending'
            or r['slot_version_id'] != r['slot_current_version_id']
            or r['raw_image_path'] != r['current_raw_image_path']
            or not r['filename'].endswith('.' + (r['image_format'] or 'png'))
            for r in assets
        ):
            return False
        conn.execute("""
            UPDATE archive_delivery_assets
            SET optimization_status='optimizing',optimization_error=NULL,updated_at=datetime('now','localtime')
            WHERE design_id=? AND optimization_status='pending'
        """, (design_id,))
    return True


def _remove_orphan_generations(design_id):
    with get_db() as conn:
        referenced = {
            Path(row['canonical_path']).parts[1]
            for row in conn.execute(
                "SELECT canonical_path FROM archive_delivery_assets "
                "WHERE design_id=? AND canonical_path IS NOT NULL",
                (design_id,),
            )
            if row['canonical_path'] and len(Path(row['canonical_path']).parts) == 3
        }
    group_dir = ARCHIVE_DELIVERY_DIR / design_id
    if not group_dir.is_dir():
        return
    for child in group_dir.iterdir():
        if child.name in referenced or not child.name.startswith(('generation-', '.staging-')):
            continue
        shutil.rmtree(child, ignore_errors=True)


def _fsync_file(path):
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def encode_archive_jpeg(
    image,
    max_bytes=MAX_ARCHIVE_JPG_BYTES,
    qualities=ARCHIVE_JPG_QUALITIES,
):
    """Use the highest available 4:4:4 quality; q92 is delivered even above the cap."""
    source_size = image.size
    rgb_image = image.convert('RGB')
    selected = None
    for quality in qualities:
        with tempfile.TemporaryFile() as output:
            rgb_image.save(
                output,
                format='JPEG',
                quality=quality,
                subsampling=0,
            )
            output.seek(0)
            encoded = output.read()
        selected = (encoded, quality)
        if len(encoded) <= max_bytes:
            break
    if selected is None:
        raise ValueError('JPG 质量阶梯未产生结果')
    encoded, quality = selected
    with Image.open(io.BytesIO(encoded)) as checked:
        if checked.format != 'JPEG' or checked.size != source_size:
            raise ValueError('JPG 格式或尺寸变化')
        if JpegImagePlugin.get_sampling(checked) != 0:
            raise ValueError('JPG 不是 4:4:4')
        checked.load()
    return encoded, quality


def _write_png_baseline(asset, staging_dir):
    raw_path = safe_file_path(RAW_IMAGES_DIR, asset['raw_image_path'])
    baseline_path = staging_dir / f"baseline-{asset['slot_type']}.png"
    with Image.open(raw_path) as source:
        source.load()
        watermarked = apply_watermark(source, watermark_cfg=json.loads(asset['watermark_config_json']))
        if watermarked.size != source.size:
            raise ValueError('水印处理改变了图片尺寸')
        watermarked.load()
        with baseline_path.open('wb') as output:
            watermarked.save(output, format='PNG', compress_level=1)
            output.flush()
            os.fsync(output.fileno())

    with Image.open(baseline_path) as encoded:
        encoded.load()
        info = {
            'width': encoded.size[0],
            'height': encoded.size[1],
            'mode': encoded.mode,
            'pixel_sha256': _png_pixel_hash(encoded),
            'byte_count': baseline_path.stat().st_size,
        }
        _validate_saved_png(
            baseline_path,
            (info['width'], info['height']),
            info['mode'],
            info['pixel_sha256'],
        )
    return baseline_path, info


def _write_jpg_baseline(asset, staging_dir):
    raw_path = safe_file_path(RAW_IMAGES_DIR, asset['raw_image_path'])
    baseline_path = staging_dir / f"baseline-{asset['slot_type']}.jpg"
    with Image.open(raw_path) as source:
        source.load()
        watermarked = apply_watermark(
            source, watermark_cfg=json.loads(asset['watermark_config_json'])
        )
        if watermarked.size != source.size:
            raise ValueError('水印处理改变了图片尺寸')
        encoded, quality = encode_archive_jpeg(watermarked)
        with baseline_path.open('wb') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    _validate_saved_jpg(baseline_path, watermarked.size)
    return baseline_path, {
        'width': source.size[0],
        'height': source.size[1],
        'mode': 'RGB',
        'jpeg_quality': quality,
        'byte_count': baseline_path.stat().st_size,
    }


def _encode_candidate(baseline_path, info, candidate_path, policy):
    with Image.open(baseline_path) as source:
        source.load()
        with candidate_path.open('wb') as output:
            kwargs = {'compress_level': 6}
            if policy == 'optimized':
                kwargs['optimize'] = True
            source.save(output, format='PNG', **kwargs)
            output.flush()
            os.fsync(output.fileno())
    _validate_saved_png(
        candidate_path,
        (info['width'], info['height']),
        info['mode'],
        info['pixel_sha256'],
    )
    return candidate_path.stat().st_size


def _stage_one_asset(asset, staging_dir, policy):
    image_format = asset['image_format']
    if image_format == 'jpg':
        baseline_path, info = _write_jpg_baseline(asset, staging_dir)
        selected_path = baseline_path
        selected_mode = 'jpg-444'
    elif image_format == 'png':
        baseline_path, info = _write_png_baseline(asset, staging_dir)
        selected_path = baseline_path
        selected_mode = 'level1'
    else:
        raise ValueError('归档图片格式无效')

    candidate_path = staging_dir / f"candidate-{asset['slot_type']}.png"
    if image_format == 'png' and policy != 'level1':
        try:
            candidate_size = _encode_candidate(baseline_path, info, candidate_path, policy)
            if candidate_size <= info['byte_count']:
                selected_path = candidate_path
                selected_mode = 'optimized' if policy == 'optimized' else 'level6'
        except Exception as exc:
            logger.warning('PNG level 6 编码失败，已回退 level 1: %s', exc)

    selected_path = staging_dir / asset['filename']
    source_for_selected = candidate_path if image_format == 'png' and selected_mode != 'level1' else baseline_path
    os.replace(source_for_selected, selected_path)
    candidate_path.unlink(missing_ok=True)
    baseline_path.unlink(missing_ok=True)
    _fsync_file(selected_path)
    if image_format == 'jpg':
        _validate_saved_jpg(selected_path, (info['width'], info['height']))
    digest = hashlib.sha256()
    with selected_path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return {
        'slot_type': asset['slot_type'],
        'slot_version_id': asset['slot_version_id'],
        'filename': asset['filename'],
        'staged_path': selected_path,
        'relative_path': f"{asset['design_id']}/{asset['filename']}",
        'width': info['width'],
        'height': info['height'],
        'mode': info['mode'],
        'sha256': digest.hexdigest(),
        'byte_count': selected_path.stat().st_size,
        'compress_mode': selected_mode,
        'jpeg_quality': info.get('jpeg_quality'),
        'image_format': image_format,
    }


def optimize_archive_group(design_id):
    """Stage four files, atomically switch their generation directory, then publish the manifest."""
    if not _claim_group(design_id):
        return False
    policy = None
    image_format = None
    staging_dir = ARCHIVE_DELIVERY_DIR / design_id / f".staging-{uuid.uuid4().hex}"
    staged = []
    published_dir = None
    try:
        _remove_orphan_generations(design_id)
        with get_db() as conn:
            assets = conn.execute(
                'SELECT * FROM archive_delivery_assets WHERE design_id=? ORDER BY slot_type',
                (design_id,),
            ).fetchall()
        if len(assets) != 4 or {r['slot_type'] for r in assets} != set(REQUIRED_SLOTS):
            raise ValueError('归档规范图片清单不完整')
        image_formats = {r['image_format'] or 'png' for r in assets}
        if len(image_formats) != 1 or image_formats not in ({'png'}, {'jpg'}):
            raise ValueError('归档规范图片格式不一致')
        image_format = next(iter(image_formats))
        if image_format == 'png':
            policy = _load_policy()

        staging_dir.mkdir(parents=True, exist_ok=False)
        for asset in sorted(assets, key=lambda item: SLOT_ORDER[item['slot_type']]):
            staged.append(_stage_one_asset(asset, staging_dir, policy))

        group_dir = ARCHIVE_DELIVERY_DIR / design_id
        generation_name = f"generation-{uuid.uuid4().hex}"
        published_dir = group_dir / generation_name
        os.replace(staging_dir, published_dir)
        _fsync_directory(group_dir)
        for item in staged:
            item['relative_path'] = f"{design_id}/{generation_name}/{item['filename']}"

        with get_db(immediate=True) as conn:
            current = conn.execute("""
                SELECT a.slot_type,a.slot_version_id,a.optimization_status,
                       s.current_version_id AS slot_current_version_id
                FROM archive_delivery_assets a
                JOIN slots s ON s.design_id=a.design_id AND s.slot_type=a.slot_type
                WHERE a.design_id=?
            """, (design_id,)).fetchall()
            if len(current) != 4 or any(
                r['optimization_status'] != 'optimizing'
                or r['slot_version_id'] != r['slot_current_version_id']
                for r in current
            ):
                raise ValueError('归档版本或后台任务状态已变化')
            if {(item['slot_type'], item['slot_version_id']) for item in staged} != {
                (r['slot_type'], r['slot_version_id']) for r in current
            }:
                raise ValueError('归档版本或后台任务状态已变化')
            for item in staged:
                conn.execute("""
                    UPDATE archive_delivery_assets
                    SET canonical_path=?,source_width=?,source_height=?,source_mode=?,
                        sha256=?,byte_count=?,compress_mode=?,jpeg_quality=?,
                        optimization_status='ready',optimization_error=NULL,failure_count=0,
                        updated_at=datetime('now','localtime')
                    WHERE design_id=? AND slot_type=? AND slot_version_id=? AND optimization_status='optimizing'
                """, (
                    item['relative_path'], item['width'], item['height'], item['mode'],
                    item['sha256'], item['byte_count'], item['compress_mode'], item['jpeg_quality'],
                    design_id, item['slot_type'], item['slot_version_id'],
                ))
            ready_count = conn.execute("""
                SELECT COUNT(*) AS count FROM archive_delivery_assets
                WHERE design_id=? AND optimization_status='ready'
            """, (design_id,)).fetchone()['count']
            if ready_count != 4:
                raise ValueError('规范图片清单未能原子发布')
        published_dir = None
        return True
    except Exception as exc:
        if published_dir is not None:
            shutil.rmtree(published_dir, ignore_errors=True)
        error = _safe_preparation_error(exc)[:200]
        with get_db(immediate=True) as conn:
            conn.execute("""
                UPDATE archive_delivery_assets
                SET optimization_status='pending',optimization_error=?,failure_count=failure_count+1,
                    updated_at=datetime('now','localtime')
                WHERE design_id=? AND optimization_status='optimizing'
            """, (error, design_id))
            failure_count = conn.execute(
                'SELECT MAX(failure_count) AS failure_count FROM archive_delivery_assets WHERE design_id=?',
                (design_id,),
            ).fetchone()['failure_count']
            if failure_count >= MAX_PREPARATION_ATTEMPTS:
                terminal_error = (
                    '规范图片连续后台准备失败，已停止 NAS；'
                    '请检查原图、预览图、磁盘空间与日志后重新处理。'
                )
                conn.execute("""
                    UPDATE archive_delivery_assets
                    SET optimization_status='failed',optimization_error=?,
                        updated_at=datetime('now','localtime')
                    WHERE design_id=? AND optimization_status='pending'
                """, (terminal_error, design_id))
                label = 'JPG' if image_format == 'jpg' else 'PNG'
                conn.execute("""
                    UPDATE nas_upload_jobs
                    SET status='failed',error=?,next_attempt_at=NULL,
                        updated_at=datetime('now','localtime')
                    WHERE design_id=? AND status IN ('queued','waiting','held')
                """, (
                    f'规范 {label} 后台准备终态失败，请检查原图、磁盘与后台日志后处理；NAS 未创建远端目录。',
                    design_id,
                ))
        raise
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def process_pending():
    with get_db() as conn:
        design_ids = [r['design_id'] for r in conn.execute("""
            SELECT design_id,MIN(rowid) AS group_row
            FROM archive_delivery_assets
            WHERE optimization_status='pending'
            GROUP BY design_id
            ORDER BY group_row
        """).fetchall()]
    completed = 0
    for design_id in design_ids:
        try:
            if optimize_archive_group(design_id):
                completed += 1
        except Exception:
            logger.exception('规范图片后台处理失败，已保留待重试并继续下一组')
    return completed


class ArchiveOptimizerWorker:
    def __init__(self):
        self.running = False
        self.task = None
        self.lock = None

    def recover_interrupted(self):
        with get_db(immediate=True) as conn:
            conn.execute("""
                UPDATE archive_delivery_assets
                SET optimization_status='pending',optimization_error='任务中断，稍后重试',
                    updated_at=datetime('now','localtime')
                WHERE optimization_status='optimizing'
            """)

    async def start(self):
        if os.environ.get('WORKBENCH_DISABLE_WORKER_LOOP') == '1':
            return
        self.lock = open(STORAGE_DIR / '.archive-optimizer.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.lock.seek(0); self.lock.write(b'0'); self.lock.flush(); self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close(); self.lock = None
            return
        self.recover_interrupted()
        self.running = True
        self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        while self.running:
            try:
                await asyncio.to_thread(process_pending)
            except Exception:
                logger.exception('PNG 后台优化轮询异常，将继续轮询')
                try:
                    await asyncio.to_thread(self.recover_interrupted)
                except Exception:
                    logger.exception('PNG 后台优化中断恢复失败')
            await asyncio.sleep(5)

    async def stop(self):
        self.running = False
        if self.task:
            await self.task
            self.task = None
        if self.lock:
            self.lock.close(); self.lock = None


archive_optimizer_worker = ArchiveOptimizerWorker()
