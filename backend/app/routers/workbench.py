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
