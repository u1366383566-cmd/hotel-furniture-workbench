"""Artist-facing task operations; each selected design commits independently."""
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
import httpx
import json
import uuid
from datetime import datetime, timezone

from ..database import get_db
from ..config import RAW_IMAGES_DIR, PREVIEW_IMAGES_DIR
from ..image_adapter import load_image_api_config
from ..models import PanoramaReviewRequest, QuadReviewRequest
from ..security import UserContext, require_roles
from ..budget_service import (
    reserve_budget,
    BudgetWarningExceededError,
    BudgetHardLimitExceededError,
)
from .designs import review_panorama, start_downstream, review_quad

router = APIRouter(prefix='/api/workbench', tags=['Workbench'])

@router.get('/upstream-quota')
def upstream_quota(user: UserContext = Depends(require_roles(['operator', 'leader', 'admin']))):
    with get_db() as conn:
        cfg = load_image_api_config(conn)
    if not cfg['enabled'] or not cfg['has_key'] or not cfg['base_url']:
        return {'status': 'unavailable', 'quota': None, 'checked_at': None}
    try:
        base = cfg['base_url'].rstrip('/')
        if base.endswith('/v1'):
            base = base[:-3]
        response = httpx.get(base + '/api/quota', headers={'Authorization': 'Bearer ' + cfg['api_key']}, timeout=8.0, follow_redirects=False)
        response.raise_for_status()
        quota = response.json().get('quota')
        if isinstance(quota, bool) or not isinstance(quota, int) or quota < 0:
            raise ValueError('invalid quota')
        return {'status': 'ok', 'quota': quota, 'checked_at': datetime.now(timezone.utc).isoformat()}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {'status': 'unavailable', 'quota': None, 'checked_at': None}


class BudgetSettings(BaseModel):
    warning_per_group: int = Field(8, ge=1, le=100)
    hard_limit_per_group: int = Field(10, ge=2, le=200)
    auto_release_unknown: Optional[bool] = None
    auto_retry_request_failures: Optional[bool] = None


class PanoramaSelection(BaseModel):
    versions: Dict[str, str]


class QuadSelection(BaseModel):
    versions: Dict[str, Dict[str, str]]


class TechnicalRetryRequest(BaseModel):
    task_ids: List[str] = Field(min_length=1, max_length=100)
    accept_unknown_charge: bool = False


class WhiteReconcileRequest(BaseModel):
    design_ids: List[str] = Field(min_length=1, max_length=100)


@router.post('/batches/{batch_id}/reconcile-white-versions')
def reconcile_white_versions(batch_id: str, req: WhiteReconcileRequest, user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    """Use the completed white version shared by existing dependent images."""
    results = []
    for design_id in dict.fromkeys(req.design_ids):
        try:
            with get_db(immediate=True) as conn:
                d = conn.execute('SELECT status FROM designs WHERE id=? AND batch_id=?', (design_id,batch_id)).fetchone()
                if not d or d['status'] != 'quad_review':
                    raise ValueError('设计尚未进入四图待终审状态')
                rows = {r['slot_type']:r for r in conn.execute("""SELECT s.id slot_id,s.slot_type,s.current_version_id,s.status slot_status,sv.id version_id,sv.process_status,sv.raw_image_path,sv.is_invalidated,sv.depends_on_panorama_version_id,sv.prompt_metadata FROM slots s JOIN slot_versions sv ON sv.id=s.current_version_id WHERE s.design_id=? AND s.slot_type IN ('panorama','white_isolated','twin_bed','nightstand_closeup')""", (design_id,)).fetchall()}
                if set(rows) != {'panorama','white_isolated','twin_bed','nightstand_closeup'}:
                    raise ValueError('四图槽位不完整')
                refs = [json.loads(rows[k]['prompt_metadata'] or '{}').get('reference_version_id') for k in ('twin_bed','nightstand_closeup')]
                if not refs[0] or refs[0] != refs[1]:
                    raise ValueError('双床和局部图没有共同的白底版本，请逐张核对')
                old = conn.execute('SELECT sv.*,s.id slot_id FROM slot_versions sv JOIN slots s ON s.id=sv.slot_id WHERE sv.id=? AND sv.design_id=? AND s.slot_type=?', (refs[0],design_id,'white_isolated')).fetchone()
                if not old or old['process_status'] != 'completed' or old['is_invalidated'] or not old['raw_image_path'] or not (RAW_IMAGES_DIR/old['raw_image_path']).exists() or old['depends_on_panorama_version_id'] != rows['panorama']['version_id']:
                    raise ValueError('共同对应的旧白底已失效或不可用')
                if rows['white_isolated']['current_version_id'] == refs[0]:
                    results.append({'design_id':design_id,'ok':True,'changed':False,'white_version_id':refs[0]})
                    continue
                conn.execute("UPDATE slots SET current_version_id=?,status='ready',updated_at=datetime('now','localtime') WHERE id=?", (refs[0],rows['white_isolated']['slot_id']))
                conn.execute("INSERT INTO audit_logs(id,batch_id,action,actor_name,actor_role,detail) VALUES (?,?,'reconcile_white_version',?,?,?)", (f'aud_{uuid.uuid4().hex}',batch_id,user.user_id,user.role,json.dumps({'design_id':design_id,'old_white_version_id':refs[0],'new_white_version_id':rows['white_isolated']['current_version_id'],'generated_images_reused':True},ensure_ascii=False)))
                results.append({'design_id':design_id,'ok':True,'changed':True,'white_version_id':refs[0]})
        except ValueError as exc:
            results.append({'design_id':design_id,'ok':False,'error':str(exc)})
        except Exception as exc:
            results.append({'design_id':design_id,'ok':False,'error':_error(exc)})
    return {'results':results}


@router.post('/batches/{batch_id}/retry-isolated-restarts')
def retry_isolated_restarts(batch_id: str, req: TechnicalRetryRequest, user: UserContext = Depends(require_roles(['admin']))):
    """Create replacement attempts while retaining the uncertain original charges."""
    if not req.accept_unknown_charge:
        raise HTTPException(400, '必须确认上游可能重复计费')
    task_ids = list(dict.fromkeys(req.task_ids))
    with get_db(immediate=True) as conn:
        batch = conn.execute('SELECT status,is_circuit_broken FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not batch or batch['status'] != 'active' or batch['is_circuit_broken']:
            raise HTTPException(409, '批次不在可生成状态')
        results = []
        for task_id in task_ids:
            row = conn.execute("""SELECT t.*,s.current_version_id,s.slot_type,d.status design_status,
                sv.prompt,sv.prompt_metadata,sv.mock_visual_features,sv.depends_on_panorama_version_id,
                sv.template_version,sv.raw_image_path,sv.preview_image_path,sv.is_invalidated,sv.process_status
                FROM tasks t JOIN slots s ON s.id=t.slot_id JOIN designs d ON d.id=t.design_id
                JOIN slot_versions sv ON sv.id=t.slot_version_id WHERE t.id=? AND t.batch_id=?""", (task_id,batch_id)).fetchone()
            if not row or row['status']!='unknown' or row['process_status']!='unknown_pending_confirmation' or row['failure_stage']!='restart_isolated' or row['current_version_id']!=row['slot_version_id'] or row['design_status'] in ('archived','eliminated') or row['raw_image_path'] or row['preview_image_path'] or row['is_invalidated'] or row['slot_type'] not in ('twin_bed','nightstand_closeup'):
                raise HTTPException(409, f'任务 {task_id} 已变化或不符合重发条件')
            ledger = conn.execute("SELECT id FROM budget_ledger WHERE reference_task_id=? AND category='image_generation' AND status='unknown_held'", (task_id,)).fetchall()
            if len(ledger)!=1:
                raise HTTPException(409, f'任务 {task_id} 的待核账记录异常')
            meta=json.loads(row['prompt_metadata'] or '{}')
            ref=meta.get('reference_version_id')
            white=conn.execute("SELECT s.current_version_id,sv.process_status,sv.raw_image_path,sv.is_invalidated FROM slots s JOIN slot_versions sv ON sv.id=s.current_version_id WHERE s.design_id=? AND s.slot_type='white_isolated'", (row['design_id'],)).fetchone()
            if not white or ref!=white['current_version_id'] or white['process_status']!='completed' or not white['raw_image_path'] or white['is_invalidated']:
                raise HTTPException(409, f'任务 {task_id} 的白底参考已变化或不可用')
            number=conn.execute('SELECT COALESCE(MAX(version_num),0)+1 FROM slot_versions WHERE slot_id=?', (row['slot_id'],)).fetchone()[0]
            version_id=f'ver_{uuid.uuid4().hex[:8]}'
            replacement=f'tsk_{uuid.uuid4().hex[:8]}'
            reserve_budget(conn,batch_id=batch_id,task_id=replacement,note=f'Replacement for uncertain request {task_id}')
            conn.execute("""INSERT INTO slot_versions(id,slot_id,design_id,version_num,attempt_num,prompt,prompt_metadata,mock_visual_features,process_status,depends_on_panorama_version_id,template_version)
                VALUES (?,?,?,?,?,?,?,?, 'pending',?,?)""",(version_id,row['slot_id'],row['design_id'],number,number,row['prompt'],row['prompt_metadata'],row['mock_visual_features'],row['depends_on_panorama_version_id'],row['template_version']))
            conn.execute("INSERT INTO tasks(id,batch_id,design_id,slot_id,slot_version_id,task_type,status,idempotency_key) VALUES (?,?,?,?,?,'redo_slot','queued',?)",(replacement,batch_id,row['design_id'],row['slot_id'],version_id,f'restart_replacement_{task_id}'))
            conn.execute("UPDATE tasks SET status='superseded_unknown' WHERE id=?",(task_id,))
            conn.execute("UPDATE slot_versions SET process_status='superseded_unknown' WHERE id=?",(row['slot_version_id'],))
            conn.execute("UPDATE slots SET current_version_id=?,status='generating',updated_at=datetime('now','localtime') WHERE id=?",(version_id,row['slot_id']))
            results.append({'original_task_id':task_id,'replacement_task_id':replacement,'version_id':version_id,'original_ledger_id':ledger[0]['id']})
        conn.execute("INSERT INTO audit_logs(id,batch_id,action,actor_name,actor_role,detail) VALUES (?,?,'retry_isolated_restarts',?,?,?)",(f'aud_{uuid.uuid4().hex}',batch_id,user.user_id,user.role,json.dumps({'risk_accepted':True,'original_upstream_charge':'unknown','results':results},ensure_ascii=False)))
    return {'results':results}


@router.post('/batches/{batch_id}/isolate-restart-unknowns')
def isolate_restart_unknowns(batch_id: str, user: UserContext = Depends(require_roles(['admin']))):
    """Resume other designs without releasing or resending interrupted requests."""
    with get_db(immediate=True) as conn:
        batch = conn.execute('SELECT status,pause_reason,is_circuit_broken FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not batch or batch['status'] != 'paused' or batch['pause_reason'] != 'PAUSE_UNKNOWN_PENDING' or batch['is_circuit_broken']:
            raise HTTPException(409, '任务不是因未知结果暂停，无法自动隔离恢复')
        unknown = conn.execute("""SELECT t.id,t.design_id,t.last_error,sv.process_status,sv.is_unknown
            FROM tasks t JOIN slot_versions sv ON sv.id=t.slot_version_id
            WHERE t.batch_id=? AND t.status != 'superseded_unknown' AND (t.status='unknown' OR sv.is_unknown=1 OR sv.process_status='unknown_pending_confirmation')""", (batch_id,)).fetchall()
        if not unknown or any(r['last_error'] != 'Interrupted by restart, unverified upstream state' or r['process_status'] != 'unknown_pending_confirmation' for r in unknown):
            raise HTTPException(409, '存在其他未知结果，不能通过重启隔离恢复')
        for row in unknown:
            ledgers = conn.execute("SELECT status FROM budget_ledger WHERE reference_task_id=? AND category='image_generation' AND status='unknown_held'", (row['id'],)).fetchall()
            if len(ledgers) != 1:
                raise HTTPException(409, '未知任务账本异常，需先核对')
        conn.executemany("UPDATE tasks SET failure_stage='restart_isolated' WHERE id=? AND status='unknown'", [(r['id'],) for r in unknown])
        conn.execute("UPDATE batches SET status='active',pause_reason=NULL,updated_at=datetime('now','localtime') WHERE id=? AND status='paused' AND pause_reason='PAUSE_UNKNOWN_PENDING'", (batch_id,))
        conn.execute("INSERT INTO audit_logs(id,batch_id,action,actor_name,actor_role,detail) VALUES (?,?,'isolate_restart_unknowns',?,?,?)", (f'aud_{uuid.uuid4().hex}', batch_id, user.user_id, user.role, json.dumps({'task_ids':[r['id'] for r in unknown], 'ledger':'unknown_held', 'resent':False},ensure_ascii=False)))
    return {'isolated':len(unknown),'resumed':True}


@router.get('/statistics')
def statistics(user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    """Return workbench stage statistics with explicit, non-overlapping semantics."""
    with get_db() as conn:
        tasks = conn.execute(
            "SELECT COUNT(*) AS n FROM batches WHERE date(created_at)=date('now','localtime')"
        ).fetchone()['n']
        completed = conn.execute(
            "SELECT COUNT(*) AS n FROM designs WHERE status='archived' AND date(archived_at)=date('now','localtime')"
        ).fetchone()['n']
        batch = conn.execute("""
            SELECT target_archived_count, current_archived_count
            FROM batches WHERE status != 'completed'
            ORDER BY updated_at DESC LIMIT 1
        """).fetchone()
        pending = conn.execute("""
            SELECT COUNT(*) AS n FROM designs
            WHERE status = 'panorama_review'
        """).fetchone()['n']
    target = int(batch['target_archived_count']) if batch else 0
    archived = int(batch['current_archived_count']) if batch else 0
    return {
        'today_tasks': tasks,
        'today_archived': completed,
        'current_target': target,
        'current_archived': archived,
        'current_remaining': max(0, target - archived),
        'pending_panorama': pending,
    }


@router.get('/budget-settings')
def budget_settings(user: UserContext = Depends(require_roles(['admin']))):
    with get_db() as conn:
        r=conn.execute(
            """SELECT warning_per_group,hard_limit_per_group,auto_release_unknown,
                      auto_retry_request_failures
               FROM workbench_settings WHERE id='default'"""
        ).fetchone()
        return {
            **dict(r),
            'auto_release_unknown': bool(r['auto_release_unknown']),
            'auto_retry_request_failures': bool(r['auto_retry_request_failures']),
        }


@router.put('/budget-settings')
def set_budget_settings(req: BudgetSettings, user: UserContext = Depends(require_roles(['admin']))):
    if req.hard_limit_per_group <= req.warning_per_group:
        raise HTTPException(400,'硬上限必须高于预警线')
    with get_db(immediate=True) as conn:
        setting_columns = """warning_per_group,hard_limit_per_group,auto_release_unknown,
                             auto_retry_request_failures"""
        before=dict(conn.execute(
            f"SELECT {setting_columns} FROM workbench_settings WHERE id='default'"
        ).fetchone())
        effective_auto_release = (
            before['auto_release_unknown']
            if req.auto_release_unknown is None else int(req.auto_release_unknown)
        )
        if req.auto_retry_request_failures and not effective_auto_release:
            raise HTTPException(400, '开启自动重试前必须同时接受失败请求的未知扣费风险')
        conn.execute(
            """UPDATE workbench_settings
               SET warning_per_group=?,hard_limit_per_group=?,
                   auto_release_unknown=COALESCE(?,auto_release_unknown),
                   auto_retry_request_failures=COALESCE(?,auto_retry_request_failures)
               WHERE id='default'""",
            (
                req.warning_per_group,
                req.hard_limit_per_group,
                None if req.auto_release_unknown is None else int(req.auto_release_unknown),
                None if req.auto_retry_request_failures is None
                else int(req.auto_retry_request_failures),
            ),
        )
        after=dict(conn.execute(
            f"SELECT {setting_columns} FROM workbench_settings WHERE id='default'"
        ).fetchone())
        conn.execute("INSERT INTO audit_logs(id,action,actor_name,actor_role,detail) VALUES (?,'update_budget_settings',?,?,?)",(f'aud_{uuid.uuid4().hex}',user.user_id,user.role,json.dumps({'before':before,'after':after},ensure_ascii=False)))
    return {'message':'新任务默认额度已保存'}


@router.get('/risk-releases')
def risk_releases(user: UserContext = Depends(require_roles(['admin']))):
    with get_db() as conn:
        rows=conn.execute("SELECT batch_id,action,actor_name,created_at,detail FROM audit_logs WHERE action IN ('auto_release_unknown','risk_release_unknown') ORDER BY created_at DESC,id DESC").fetchall()
    return {'count':len(rows),'items':[dict(r) for r in rows]}


def _error(exc):
    return str(getattr(exc,'detail',None) or str(exc) or type(exc).__name__)


def _batch_design_ids(conn, batch_id: str, requested_ids):
    if not requested_ids:
        raise ValueError('未选择任何设计')
    placeholders = ','.join('?' for _ in requested_ids)
    rows = conn.execute(
        f'SELECT id FROM designs WHERE batch_id=? AND id IN ({placeholders})',
        [batch_id, *requested_ids],
    ).fetchall()
    found = {row['id'] for row in rows}
    unknown = [did for did in requested_ids if did not in found]
    if unknown:
        raise ValueError(f'设计不属于当前任务: {unknown}')
    return found


@router.post('/batches/{batch_id}/approve-panoramas')
def approve_panoramas(batch_id: str, req: PanoramaSelection, user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    results=[]
    for design_id, version in req.versions.items():
        try:
            with get_db() as conn:
                _batch_design_ids(conn, batch_id, [design_id])
                row=conn.execute("SELECT d.status,s.current_version_id FROM designs d JOIN slots s ON s.design_id=d.id AND s.slot_type='panorama' WHERE d.id=?",(design_id,)).fetchone()
            if row['current_version_id'] != version:
                raise ValueError('全景版本已变化，请重新审核')
            # Approval itself is idempotent at the state-machine layer; a
            # repeated request only fills the missing downstream dispatch.
            if row['status'] == 'panorama_review':
                review_panorama(design_id,PanoramaReviewRequest(action='approve',expected_version_id=version),user)
            start_downstream(design_id, expected_panorama_version_id=version, user=user)
            results.append({'design_id':design_id,'ok':True,'stage':'downstream_started'})
        except Exception as exc:
            results.append({'design_id':design_id,'ok':False,'stage':'unchanged','error':_error(exc),'retryable':not isinstance(exc, ValueError) or '版本已变化' not in _error(exc)})
    return {'results':results}


@router.post('/batches/{batch_id}/archive')
def archive_selected(batch_id: str, req: QuadSelection, user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    results=[]
    for design_id, snapshot in req.versions.items():
        try:
            with get_db() as conn:
                _batch_design_ids(conn, batch_id, [design_id])
            review_quad(design_id,QuadReviewRequest(action='archive',expected_slot_versions=snapshot),user)
            results.append({'design_id':design_id,'ok':True,'stage':'archived'})
        except Exception as exc:
            message = _error(exc)
            results.append({'design_id':design_id,'ok':False,'stage':'unchanged','error':message,'retryable':'版本已变更' not in message and '不存在' not in message})
    return {'results':results}


@router.get('/batches/{batch_id}/planning')
def planning_status(batch_id: str, user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    with get_db() as conn:
        rows=conn.execute("""SELECT design_id,status,error,attempts,technical_retry_count,
            retry_after,started_at FROM planning_jobs WHERE batch_id=? ORDER BY created_at""",(batch_id,)).fetchall()
        return {'jobs':[dict(r) for r in rows]}


@router.post('/batches/{batch_id}/retry-planning')
def retry_planning(batch_id: str, user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    with get_db(immediate=True) as conn:
        batch=conn.execute('SELECT status,is_circuit_broken FROM batches WHERE id=?',(batch_id,)).fetchone()
        if not batch: raise HTTPException(404,'任务不存在')
        if batch['status']!='active' or batch['is_circuit_broken']:
            raise HTTPException(409,'任务已暂停，请联系管理员')
        # Only safe, explicit planning failures are retried.  Unknown/running
        # jobs are left untouched and no new design is created by this endpoint.
        cur=conn.execute("""
            UPDATE planning_jobs
            SET status='queued', error=NULL, technical_retry_count=0, retry_after=NULL,
                updated_at=datetime('now','localtime')
            WHERE batch_id=? AND status='failed'
              AND design_id IN (SELECT id FROM designs WHERE status='draft')
        """,(batch_id,))
    return {'queued':cur.rowcount}


def _technical_failure_eligibility(row) -> tuple[bool, Optional[str]]:
    if row['task_status'] != 'failed' or row['failure_stage'] != 'request_charge_unknown':
        return False, '该任务不是可重试的请求失败'
    if row['design_status'] in ('archived', 'eliminated'):
        return False, '设计已归档或淘汰'
    if row['slot_current_version_id'] != row['slot_version_id']:
        return False, '槽位已切换版本，旧重试已失效'
    if row['raw_image_path'] or row['preview_image_path']:
        return False, '本地已有图片记录，不能按未知结果重发'
    stem = f"{row['design_code']}_{row['slot_type']}_v{row['version_num']}_{row['slot_version_id'][:8]}"
    if (
        (RAW_IMAGES_DIR / f"{stem}_raw.png").exists()
        or (PREVIEW_IMAGES_DIR / f"{stem}_preview.jpg").exists()
    ):
        return False, '本地已有图片文件，不能按未知结果重发'
    return True, None


@router.get('/batches/{batch_id}/technical-failures')
def technical_failures(
    batch_id: str,
    user: UserContext = Depends(require_roles(['operator', 'leader', 'admin'])),
):
    with get_db() as conn:
        batch = conn.execute('SELECT id FROM batches WHERE id=?', (batch_id,)).fetchone()
        if not batch:
            raise HTTPException(404, '任务不存在')
        rows = conn.execute(
            """SELECT t.id AS task_id,t.batch_id,t.design_id,t.slot_id,t.slot_version_id,
                      t.status AS task_status,t.failure_stage,t.last_error,t.auto_retry_count,
                      t.retry_status,t.updated_at AS task_updated_at,
                      s.slot_type,s.current_version_id AS slot_current_version_id,s.status AS slot_status,
                      d.design_code,d.status AS design_status,
                      sv.version_num,sv.raw_image_path,sv.preview_image_path,
                      (SELECT COUNT(*) FROM image_generation_attempts a WHERE a.task_id=t.id)
                          AS attempt_count
               FROM tasks t
               JOIN slots s ON s.id=t.slot_id
               JOIN designs d ON d.id=t.design_id
               JOIN slot_versions sv ON sv.id=t.slot_version_id
               WHERE t.batch_id=? AND t.status='failed'
                 AND t.failure_stage='request_charge_unknown'
               ORDER BY t.updated_at ASC,t.id ASC""",
            (batch_id,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        retryable, reason = _technical_failure_eligibility(row)
        item['retryable'] = retryable
        item['reason'] = reason
        items.append(item)
    return {'batch_id': batch_id, 'count': len(items), 'items': items}


@router.post('/batches/{batch_id}/retry-technical-failures')
def retry_technical_failures(
    batch_id: str,
    req: TechnicalRetryRequest,
    user: UserContext = Depends(require_roles(['leader', 'admin'])),
):
    if not req.accept_unknown_charge:
        raise HTTPException(400, '必须明确接受上游扣费未知的风险')
    results = []
    with get_db() as conn:
        batch = conn.execute(
            'SELECT status,is_circuit_broken FROM batches WHERE id=?', (batch_id,)
        ).fetchone()
    if not batch:
        raise HTTPException(404, '任务不存在')
    if batch['status'] != 'active' or batch['is_circuit_broken']:
        raise HTTPException(409, '任务当前已暂停或熔断，请恢复后再重试')

    for task_id in dict.fromkeys(req.task_ids):
        try:
            with get_db(immediate=True) as conn:
                row = conn.execute(
                    """SELECT t.id AS task_id,t.batch_id,t.design_id,t.slot_id,
                              t.slot_version_id,t.status AS task_status,t.failure_stage,
                              t.last_error,t.auto_retry_count,t.retry_status,
                              s.slot_type,s.current_version_id AS slot_current_version_id,
                              s.status AS slot_status,d.design_code,d.status AS design_status,
                              sv.version_num,sv.raw_image_path,sv.preview_image_path
                       FROM tasks t JOIN slots s ON s.id=t.slot_id
                       JOIN designs d ON d.id=t.design_id
                       JOIN slot_versions sv ON sv.id=t.slot_version_id
                       WHERE t.id=? AND t.batch_id=?""",
                    (task_id, batch_id),
                ).fetchone()
                if not row:
                    raise ValueError('任务不存在或不属于当前批次')
                retryable, reason = _technical_failure_eligibility(row)
                if not retryable:
                    raise ValueError(reason or '任务当前不可重试')
                ledger_id = reserve_budget(
                    conn,
                    batch_id=batch_id,
                    category='image_generation',
                    amount=1,
                    task_id=task_id,
                    note=f"Manual confirmed technical retry for {row['design_code']} {row['slot_type']}",
                )
                conn.execute(
                    """UPDATE tasks
                       SET status='queued',failure_stage='request_retryable',
                           retry_status='manual_retry_queued',auto_retry_count=0,
                           retry_count=0,retry_after=datetime('now','localtime'),
                           updated_at=datetime('now','localtime')
                       WHERE id=?""",
                    (task_id,),
                )
                conn.execute(
                    """UPDATE slot_versions
                       SET process_status='pending',is_unknown=0,
                           error_message='人工确认上游扣费风险后重新尝试'
                       WHERE id=?""",
                    (row['slot_version_id'],),
                )
                conn.execute(
                    """UPDATE slots SET status='generating',
                       updated_at=datetime('now','localtime')
                       WHERE id=? AND current_version_id=?""",
                    (row['slot_id'], row['slot_version_id']),
                )
                conn.execute(
                    """INSERT INTO audit_logs(id,batch_id,action,actor_name,actor_role,detail)
                       VALUES (?,?,'manual_retry_technical_failure',?,?,?)""",
                    (
                        f"aud_{uuid.uuid4().hex}",
                        batch_id,
                        user.user_id,
                        user.role,
                        json.dumps(
                            {
                                'task_id': task_id,
                                'design_code': row['design_code'],
                                'slot_type': row['slot_type'],
                                'previous_error': row['last_error'],
                                'ledger_id': ledger_id,
                                'risk_accepted': True,
                                'upstream_charge': 'unknown',
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
            results.append({'task_id': task_id, 'ok': True, 'stage': 'queued'})
        except (ValueError, BudgetWarningExceededError, BudgetHardLimitExceededError) as exc:
            results.append({'task_id': task_id, 'ok': False, 'error': _error(exc)})
        except Exception as exc:
            results.append({'task_id': task_id, 'ok': False, 'error': _error(exc)})
    return {'results': results}
