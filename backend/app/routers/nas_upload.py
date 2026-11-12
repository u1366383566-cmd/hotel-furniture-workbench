from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..database import get_db
from ..nas_upload_service import queue_selected, confirm_manual
from ..security import require_roles, UserContext

router = APIRouter(prefix='/api/nas-upload', tags=['NAS Upload'])


class Selection(BaseModel):
    batch_id: str
    design_ids: list[str] = Field(..., min_length=1, max_length=100)


@router.post('/enqueue', dependencies=[Depends(require_roles(['operator','leader','admin']))])
def enqueue(req: Selection):
    try:
        with get_db(immediate=True) as conn:
            return {'results': queue_selected(conn, req.batch_id, req.design_ids)}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


class ManualConfirmation(BaseModel):
    attested: bool


@router.post('/design/{design_id}/confirm')
def confirm(design_id: str, req: ManualConfirmation, user: UserContext = Depends(require_roles(['operator','leader','admin']))):
    if not req.attested:
        raise HTTPException(400, detail='请先确认已在 NAS 检查四张图片和文件名')
    try:
        with get_db(immediate=True) as conn:
            changed = confirm_manual(conn, design_id, user.user_id, user.role)
        return {'confirmed': True, 'changed': changed}
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
