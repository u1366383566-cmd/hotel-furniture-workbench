"""Durable, conservative WebDAV delivery of archived product images."""
import asyncio
import configparser
import hashlib
import logging
import os
import subprocess
import uuid
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx

from .database import get_db
from .config import STORAGE_DIR
from .export_service import _collect_export_data, render_product_images
from .product_numbering import format_product_code

REMOTE_ROOT = '美工/阿里国际站/PIC-BANK/JD/TFMK'
logger = logging.getLogger(__name__)


class NasConflict(Exception):
    pass


class NasPreflightError(Exception):
    pass


def enqueue_upload(conn, design_id, automatic=False):
    ready = os.environ.get('WORKBENCH_NAS_PUBLISH_VERIFIED') == '1'
    settings = conn.execute('SELECT auto_enabled FROM nas_upload_settings WHERE id=1').fetchone()
    held = automatic and (not settings or not settings['auto_enabled'])
    error = '等待测试通过后开启自动上传' if held else None if ready else 'NAS 上传安全检查尚未完成，任务尚未开始'
    status = 'held' if held else 'queued' if ready else 'waiting'
    conn.execute("INSERT OR IGNORE INTO nas_upload_jobs(id,design_id,status,error) VALUES (?,?,?,?)", (uuid.uuid4().hex, design_id, status, error))


def queue_selected(conn, batch_id, ids):
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('请选择不重复的已编号归档组')
    results = []
    for did in ids:
        row = conn.execute('SELECT status,product_number FROM designs WHERE id=? AND batch_id=?', (did, batch_id)).fetchone()
        if not row or row['status'] != 'archived' or row['product_number'] is None:
            results.append({'design_id': did, 'ok': False, 'error': '该组不存在或尚未归档编号'})
            continue
        enqueue_upload(conn, did)
        ready = os.environ.get('WORKBENCH_NAS_PUBLISH_VERIFIED') == '1'
        message = None if ready else 'NAS 上传安全检查尚未完成，任务尚未开始'
        conn.execute("UPDATE nas_upload_jobs SET status=?,error=?,next_attempt_at=NULL,updated_at=datetime('now','localtime') WHERE design_id=? AND status IN ('failed','conflict','waiting','held')", ('queued' if ready else 'waiting',message,did,))
        results.append({'design_id': did, 'ok': True})
    return results


class WebDav:
    def __init__(self):
        path = os.environ.get('WORKBENCH_NAS_RCLONE_CONFIG', '')
        if not path or os.environ.get('WORKBENCH_NAS_PUBLISH_VERIFIED') != '1':
            raise RuntimeError('NAS 连接或上传安全检查尚未配置完成')
        cfg = configparser.ConfigParser(interpolation=None)
        if not cfg.read(path, encoding='utf-8') or 'zhengzhou-nas' not in cfg:
            raise RuntimeError('NAS 连接配置不可读取')
        section = cfg['zhengzhou-nas']
        self.base = section['url'].rstrip('/')
        target = urlsplit(self.base)
        if (target.scheme != 'https' or target.hostname != 'webdav.seezo.top' or
            unquote(target.path).rstrip('/') != '/外贸部' or target.username or target.password or
            target.query or target.fragment or target.port not in (None, 443)):
            raise RuntimeError('NAS 连接地址与指定的郑州外贸部共享不一致')
        try:
            password = subprocess.run(['rclone', 'reveal', section['pass']], check=True, capture_output=True, text=True, timeout=10).stdout.rstrip('\n')
        except (OSError, subprocess.SubprocessError, KeyError):
            raise RuntimeError('NAS 连接凭据不可用') from None
        self.client = httpx.Client(auth=(section['user'], password), timeout=httpx.Timeout(120, connect=45), follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def url(self, path):
        return self.base + '/' + quote(REMOTE_ROOT + '/' + path, safe='/')

    def exists(self, path):
        r = self.client.request('PROPFIND', self.url(path), headers={'Depth': '0'})
        if r.status_code == 404:
            return False
        if r.status_code not in (200, 207):
            raise RuntimeError(f'查询远端状态失败（HTTP {r.status_code}）')
        return True

    def mkdir(self, path):
        r = self.client.request('MKCOL', self.url(path))
        if r.status_code in (405, 409):
            raise NasConflict('产品目录已存在或发生冲突，未覆盖')
        if r.status_code != 201:
            raise RuntimeError(f'创建产品目录结果待确认（HTTP {r.status_code}）')

    def names(self, path):
        r = self.client.request('PROPFIND', self.url(path), headers={'Depth': '1'})
        if r.status_code != 207:
            raise RuntimeError(f'查询远端目录失败（HTTP {r.status_code}）')
        try:
            root = ElementTree.fromstring(r.content)
            hrefs = [unquote(urlsplit(e.text).path).rstrip('/') for e in root.findall('.//{DAV:}response/{DAV:}href') if e.text]
            base = unquote(urlsplit(self.url(path)).path).rstrip('/')
            if base not in hrefs:
                raise ValueError('directory missing')
            return {h[len(base)+1:] for h in hrefs if h.startswith(base+'/')}
        except (ElementTree.ParseError, ValueError):
            raise RuntimeError('远端目录响应异常，无法安全核对') from None

    def put_new(self, path, data):
        r = self.client.put(self.url(path), content=data, headers={'If-None-Match': '*', 'Content-Type': 'image/png'})
        if r.status_code in (409, 412):
            raise NasConflict('远端同名文件已存在，已停止以防覆盖')
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f'图片上传结果待确认（HTTP {r.status_code}）')

def _save_status(job_id, status, error=None, delay=None):
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE nas_upload_jobs SET status=?,error=?,next_attempt_at=CASE WHEN ? IS NULL THEN NULL ELSE datetime('now','localtime',?) END,updated_at=datetime('now','localtime') WHERE id=?", (status, error, delay, delay, job_id))
        if status in ('awaiting_review','success','waiting','failed','conflict'):
            conn.execute("UPDATE nas_upload_attempts SET status=?,error=?,finished_at=datetime('now','localtime') WHERE job_id=? AND attempt=(SELECT attempts FROM nas_upload_jobs WHERE id=?)", (status,error,job_id,job_id))


def validate_upload_source(conn, design_id):
    rows = conn.execute("""SELECT s.id AS slot_id,s.slot_type,s.status AS slot_status,
        s.current_version_id,sv.id AS version_id,sv.slot_id AS version_slot_id,
        sv.design_id AS version_design_id,sv.process_status,sv.is_locked,sv.is_unknown,
        sv.is_invalidated,sv.depends_on_panorama_version_id
        FROM slots s LEFT JOIN slot_versions sv ON sv.id=s.current_version_id
        WHERE s.design_id=?""", (design_id,)).fetchall()
    by_type = {r['slot_type']: r for r in rows}
    required = {'panorama','white_isolated','twin_bed','nightstand_closeup'}
    if len(rows) != 4 or set(by_type) != required:
        raise ValueError('上传前检查失败：四图槽位不完整')
    for row in rows:
        if (row['slot_status'] != 'approved' or not row['version_id'] or
            row['version_slot_id'] != row['slot_id'] or row['version_design_id'] != design_id or
            row['process_status'] != 'completed' or not row['is_locked'] or
            row['is_unknown'] or row['is_invalidated']):
            raise ValueError('上传前检查失败：图片版本未锁定、损坏或状态不正确')
    panorama = by_type['panorama']['version_id']
    if any(by_type[t]['depends_on_panorama_version_id'] != panorama for t in required - {'panorama'}):
        raise ValueError('上传前检查失败：配图与归档全景版本不一致')
    return {t: by_type[t]['version_id'] for t in required}


def confirm_manual(conn, design_id, user_id, role):
    job = conn.execute("SELECT j.id,j.status,d.status AS design_status,d.product_number FROM nas_upload_jobs j JOIN designs d ON d.id=j.design_id WHERE j.design_id=?", (design_id,)).fetchone()
    if not job or job['design_status'] != 'archived' or job['product_number'] is None:
        raise ValueError('该产品不存在或未归档编号')
    if job['status'] == 'success':
        return False
    if job['status'] != 'awaiting_review':
        raise ValueError('该组尚未上传完成，不能进行人工确认')
    rows = conn.execute('SELECT slot_type,status FROM nas_upload_items WHERE job_id=?', (job['id'],)).fetchall()
    if len(rows) != 4 or {r['slot_type'] for r in rows} != {'panorama','white_isolated','twin_bed','nightstand_closeup'} or any(r['status'] != 'uploaded_unverified' for r in rows):
        raise ValueError('该组四图上传记录不完整，不能确认')
    conn.execute("UPDATE nas_upload_jobs SET status='success',confirmed_by=?,confirmed_at=datetime('now','localtime'),error=NULL,updated_at=datetime('now','localtime') WHERE id=?", (user_id,job['id']))
    conn.execute("INSERT INTO audit_logs(id,batch_id,action,actor_name,actor_role,detail) SELECT ?,batch_id,'nas_manual_confirm',?,?,? FROM designs WHERE id=?", ('aud_'+uuid.uuid4().hex,user_id,role,'用户确认已在 NAS 检查四张图片及文件名',design_id))
    enabled = conn.execute('UPDATE nas_upload_settings SET auto_enabled=1 WHERE id=1 AND auto_enabled=0 AND test_design_id=?', (design_id,)).rowcount
    if enabled:
        conn.execute("UPDATE nas_upload_jobs SET status='queued',error=NULL,next_attempt_at=NULL,updated_at=datetime('now','localtime') WHERE status='held'")
    return True


def process_one(factory=WebDav):
    with get_db(immediate=True) as conn:
        row = conn.execute("SELECT * FROM nas_upload_jobs WHERE status IN ('queued','waiting') AND (next_attempt_at IS NULL OR next_attempt_at<=datetime('now','localtime')) ORDER BY created_at,id LIMIT 1").fetchone()
        if not row:
            return False
        job = dict(row)
        conn.execute("UPDATE nas_upload_jobs SET status='uploading',error=NULL,attempts=attempts+1,updated_at=datetime('now','localtime') WHERE id=?", (job['id'],))
        conn.execute('INSERT INTO nas_upload_attempts(job_id,attempt) VALUES (?,?)', (job['id'],job['attempts']+1))
    remote = None
    try:
        try:
            with get_db() as conn:
                d = conn.execute('SELECT batch_id FROM designs WHERE id=?', (job['design_id'],)).fetchone()
                if not d:
                    return True
                wm, designs = _collect_export_data(conn, d['batch_id'], [job['design_id']])
                versions = validate_upload_source(conn, job['design_id'])
            design = designs[0]
            images = render_product_images(wm, design)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise NasPreflightError(str(exc)) from None
        if len(images) != 4 or len({slot for slot, _, _ in images}) != 4 or len({name for _, name, _ in images}) != 4:
            raise NasPreflightError('上传前检查失败：四图文件名或图片数量异常')
        with get_db(immediate=True) as conn:
            for slot, filename, data in images:
                digest = hashlib.sha256(data).hexdigest()
                old = conn.execute('SELECT filename,sha256,byte_count,slot_version_id FROM nas_upload_items WHERE job_id=? AND slot_type=?', (job['id'], slot)).fetchone()
                if old and (old['filename'], old['sha256'], old['byte_count']) != (filename, digest, len(data)):
                    raise NasConflict('本地归档图片或水印已变化，需人工核对')
                if old and old['slot_version_id'] and old['slot_version_id'] != versions[slot]:
                    raise NasConflict('归档图片版本已变化，需人工核对')
                conn.execute('INSERT OR IGNORE INTO nas_upload_items(job_id,slot_type,filename,sha256,byte_count,slot_version_id) VALUES (?,?,?,?,?,?)', (job['id'], slot, filename, digest, len(data), versions[slot]))
                conn.execute('UPDATE nas_upload_items SET slot_version_id=? WHERE job_id=? AND slot_type=? AND slot_version_id IS NULL', (versions[slot],job['id'],slot))
        remote = factory()
        code = format_product_code(design['product_number'])
        if remote.exists(code):
            found = remote.names(code)
            expected = {item[1] for item in images}
            if not found.issubset(expected):
                raise NasConflict('正式目录包含其他文件，已停止以防覆盖')
            if not job['owns_directory']:
                raise NasConflict('已有目录归属无法确认，已停止以防覆盖')
        else:
            remote.mkdir(code)
            with get_db(immediate=True) as conn:
                conn.execute('UPDATE nas_upload_jobs SET owns_directory=1 WHERE id=?', (job['id'],))
        # Transfer the entire group before starting slow remote readback, so one
        # verification timeout cannot prevent the other images from arriving.
        for slot, filename, data in images:
            path = code + '/' + filename
            exists = remote.exists(path)
            with get_db() as conn:
                item_status = conn.execute('SELECT status FROM nas_upload_items WHERE job_id=? AND slot_type=?', (job['id'],slot)).fetchone()['status']
            if exists and item_status == 'pending':
                raise NasConflict('远端出现未经本任务上传的同名图片，已停止以防覆盖')
            if not exists:
                with get_db(immediate=True) as conn:
                    conn.execute("UPDATE nas_upload_items SET status='uploading' WHERE job_id=? AND slot_type=?", (job['id'], slot))
                remote.put_new(path, data)
            with get_db(immediate=True) as conn:
                conn.execute("UPDATE nas_upload_items SET status='uploaded_unverified' WHERE job_id=? AND slot_type=?", (job['id'], slot))
        _save_status(job['id'], 'awaiting_review')
    except NasPreflightError as exc:
        _save_status(job['id'], 'failed', str(exc)[:200])
    except NasConflict as exc:
        _save_status(job['id'], 'conflict', str(exc))
    except Exception as exc:
        # Avoid leaking remote URLs, credentials, paths, or raw server responses.
        msg = str(exc) if isinstance(exc, (RuntimeError, ValueError, FileNotFoundError)) else 'NAS 连接中断或超时，稍后检查目标并重试'
        if 'http' in msg.lower() and not msg.startswith(('查询远端','图片上传','远端图片','创建产品')):
            msg = 'NAS 连接中断或超时，稍后检查目标并重试'
        attempts = job['attempts'] + 1
        _save_status(job['id'], 'waiting' if attempts < 10 else 'failed', msg[:200], '+5 minutes' if attempts < 10 else None)
    finally:
        if remote:
            try:
                remote.close()
            except Exception:
                logger.warning('NAS 连接关闭失败')
    return True


class NasWorker:
    def __init__(self):
        self.running = False
        self.task = None
        self.lock = None

    async def start(self):
        if os.environ.get('WORKBENCH_DISABLE_WORKER_LOOP') == '1' or os.environ.get('WORKBENCH_NAS_PUBLISH_VERIFIED') != '1':
            return
        # An OS lock prevents a second app process from resetting or running the
        # same job; process termination releases the lock automatically.
        self.lock = open(STORAGE_DIR / '.nas-worker.lock', 'a+b')
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

    @staticmethod
    def recover_interrupted():
        with get_db(immediate=True) as conn:
            conn.execute("UPDATE nas_upload_attempts SET status='waiting',error='任务中断，稍后检查目标并重试',finished_at=datetime('now','localtime') WHERE finished_at IS NULL AND job_id IN (SELECT id FROM nas_upload_jobs WHERE status IN ('uploading','verifying'))")
            conn.execute("UPDATE nas_upload_jobs SET status='waiting',error='任务中断，稍后检查目标并重试',next_attempt_at=NULL WHERE status IN ('uploading','verifying')")

    async def _loop(self):
        while self.running:
            try:
                await asyncio.to_thread(process_one)
            except Exception:
                # Never put request URLs or credential-bearing exceptions in logs.
                logger.error('NAS 后台任务发生内部错误，将继续轮询')
                try:
                    await asyncio.to_thread(self.recover_interrupted)
                except Exception:
                    logger.error('NAS 中断任务恢复失败，稍后重试')
            await asyncio.sleep(5)

    async def stop(self):
        self.running = False
        if self.task:
            await self.task
            self.task = None
        if self.lock:
            self.lock.close()
            self.lock = None


nas_worker = NasWorker()
