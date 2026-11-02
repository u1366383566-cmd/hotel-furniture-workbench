"""Artist-facing task operations; each selected design commits independently."""
from typing import Dict, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..database import get_db
from ..models import PanoramaReviewRequest, QuadReviewRequest
from ..security import UserContext, require_roles
from .designs import review_panorama, start_downstream, review_quad

router = APIRouter(prefix='/api/workbench', tags=['Workbench'])


class BudgetSettings(BaseModel):
    warning_per_group: int = Field(5, ge=1, le=100)
    hard_limit_per_group: int = Field(6, ge=2, le=200)


class PanoramaSelection(BaseModel):
    versions: Dict[str, str]


class QuadSelection(BaseModel):
    versions: Dict[str, Dict[str, str]]


@router.get('/statistics')
def statistics(user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    with get_db() as conn:
        tasks = conn.execute("SELECT COUNT(*) AS n FROM batches WHERE date(created_at)=date('now','localtime')").fetchone()['n']
        completed = conn.execute("SELECT COUNT(*) AS n FROM designs WHERE status='archived' AND date(archived_at)=date('now','localtime')").fetchone()['n']
    return {'today_tasks': tasks, 'today_archived': completed}


@router.get('/budget-settings')
def budget_settings(user: UserContext = Depends(require_roles(['admin']))):
    with get_db() as conn:
        r=conn.execute("SELECT warning_per_group,hard_limit_per_group FROM workbench_settings WHERE id='default'").fetchone()
        return dict(r)


@router.put('/budget-settings')
def set_budget_settings(req: BudgetSettings, user: UserContext = Depends(require_roles(['admin']))):
    if req.hard_limit_per_group <= req.warning_per_group:
        raise HTTPException(400,'硬上限必须高于预警线')
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE workbench_settings SET warning_per_group=?,hard_limit_per_group=? WHERE id='default'", (req.warning_per_group,req.hard_limit_per_group))
    return {'message':'新任务默认额度已保存'}


def _error(exc):
    return str(getattr(exc,'detail',None) or str(exc) or type(exc).__name__)


@router.post('/batches/{batch_id}/approve-panoramas')
def approve_panoramas(batch_id: str, req: PanoramaSelection, user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    results=[]
    for design_id, version in req.versions.items():
        try:
            with get_db() as conn:
                row=conn.execute("SELECT d.batch_id,d.status,s.current_version_id FROM designs d JOIN slots s ON s.design_id=d.id AND s.slot_type='panorama' WHERE d.id=?",(design_id,)).fetchone()
            if not row or row['batch_id'] != batch_id:
                raise ValueError('设计不属于当前任务')
            if row['current_version_id'] != version:
                raise ValueError('全景版本已变化，请重新审核')
            if row['status'] == 'panorama_review':
                review_panorama(design_id,PanoramaReviewRequest(action='approve',expected_version_id=version),user)
            start_downstream(design_id, expected_panorama_version_id=version, user=user)
            results.append({'design_id':design_id,'ok':True})
        except Exception as exc:
            results.append({'design_id':design_id,'ok':False,'error':_error(exc)})
    return {'results':results}


@router.post('/batches/{batch_id}/archive')
def archive_selected(batch_id: str, req: QuadSelection, user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    results=[]
    for design_id, snapshot in req.versions.items():
        try:
            with get_db() as conn:
                row=conn.execute('SELECT batch_id FROM designs WHERE id=?',(design_id,)).fetchone()
            if not row or row['batch_id']!=batch_id:
                raise ValueError('设计不属于当前任务')
            review_quad(design_id,QuadReviewRequest(action='archive',expected_slot_versions=snapshot),user)
            results.append({'design_id':design_id,'ok':True})
        except Exception as exc:
            results.append({'design_id':design_id,'ok':False,'error':_error(exc)})
    return {'results':results}


@router.get('/batches/{batch_id}/planning')
def planning_status(batch_id: str, user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    with get_db() as conn:
        rows=conn.execute('SELECT design_id,status,error,attempts FROM planning_jobs WHERE batch_id=? ORDER BY created_at',(batch_id,)).fetchall()
        return {'jobs':[dict(r) for r in rows]}


@router.post('/batches/{batch_id}/retry-planning')
def retry_planning(batch_id: str, user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    with get_db(immediate=True) as conn:
        batch=conn.execute('SELECT status,is_circuit_broken FROM batches WHERE id=?',(batch_id,)).fetchone()
        if not batch: raise HTTPException(404,'任务不存在')
        if batch['status']!='active' or batch['is_circuit_broken']:
            raise HTTPException(409,'任务已暂停，请联系管理员')
        cur=conn.execute("UPDATE planning_jobs SET status='queued',error=NULL WHERE batch_id=? AND status='failed'",(batch_id,))
    return {'queued':cur.rowcount}
