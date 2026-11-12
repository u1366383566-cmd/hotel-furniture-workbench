from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..database import get_db
from ..nas_upload_service import queue_selected
from ..security import require_roles

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
