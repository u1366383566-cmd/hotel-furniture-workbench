import sqlite3
from typing import List, Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from ..database import get_db
from ..config import EXPORTS_DIR
from ..security import UserContext, get_current_user, require_roles, safe_file_path
from ..export_service import export_designs_to_watermarked_zip

router = APIRouter(prefix="/api/export", tags=["Export"])

class SelectedExportRequest(BaseModel):
    batch_id: str
    design_ids: List[str] = Field(..., min_length=1)

@router.post("/design/{design_id}")
def export_single_design(
    design_id: str,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """导出单款归档设计的带水印 ZIP"""
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT batch_id, status FROM designs WHERE id = ?", (design_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="设计不存在")
        if row["status"] != "archived":
            raise HTTPException(status_code=400, detail="该设计尚未终审归档，禁止导出！")
        
        batch_id = row["batch_id"]
        try:
            zip_filename = export_designs_to_watermarked_zip(
            conn, batch_id=batch_id, design_ids=[design_id],
            actor_name=user.user_id, actor_role=user.role,
            export_type="single_design_zip"
        )
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return {
        "message": "带水印 ZIP 导出成功",
        "file_name": zip_filename,
        "download_url": f"/api/export/download/{zip_filename}"
    }

@router.post("/selected")
def export_selected_designs(
    req: SelectedExportRequest,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """部分导出：即便未达成批次总目标，亦可导出当前已归档的指定设计部分"""
    with get_db(immediate=True) as conn:
        placeholders = ",".join("?" for _ in req.design_ids)
        cur = conn.cursor()
        cur.execute(f"SELECT id, design_code, status FROM designs WHERE batch_id = ? AND id IN ({placeholders})", [req.batch_id] + req.design_ids)
        rows = cur.fetchall()
        if len(rows) != len(req.design_ids):
            raise HTTPException(status_code=404, detail="部分所选设计不存在")
        for r in rows:
            if r["status"] != "archived":
                raise HTTPException(status_code=400, detail=f"设计 {r['design_code']} 尚未完成归档，禁止部分导出！")

        try:
            zip_filename = export_designs_to_watermarked_zip(
            conn, batch_id=req.batch_id, design_ids=req.design_ids,
            actor_name=user.user_id, actor_role=user.role,
            export_type="partial_archived_zip"
        )
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return {
        "message": f"成功部分导出 {len(req.design_ids)} 款已归档设计带水印 ZIP",
        "archived_count": len(req.design_ids),
        "file_name": zip_filename,
        "download_url": f"/api/export/download/{zip_filename}"
    }

@router.post("/batch/{batch_id}")
def export_batch_archived(
    batch_id: str,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """导出批次中全部已归档设计的带水印 ZIP（支持未满额时导出已归档部分）"""
    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM designs WHERE batch_id = ? AND status = 'archived'", (batch_id,))
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(status_code=400, detail="批次中暂无已归档的设计，无法导出")
        
        dids = [r["id"] for r in rows]
        try:
            zip_filename = export_designs_to_watermarked_zip(
            conn, batch_id=batch_id, design_ids=dids,
            actor_name=user.user_id, actor_role=user.role,
            export_type="batch_archived_zip"
        )
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return {
        "message": f"成功导出 {len(dids)} 款归档设计带水印 ZIP",
        "archived_count": len(dids),
        "file_name": zip_filename,
        "download_url": f"/api/export/download/{zip_filename}"
    }

@router.get("/records/{batch_id}")
def list_export_records(
    batch_id: str,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    """批次导出记录（重复导出可追踪）"""
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, design_id, export_type, file_name, file_size, exported_by, exported_role, created_at
            FROM export_records WHERE batch_id = ? ORDER BY created_at DESC LIMIT 100
        """, (batch_id,))
        rows = cur.fetchall()
        return [dict(r) for r in rows]


@router.get("/download/{file_name}")
def download_export_file(
    file_name: str,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    if not file_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="只允许下载带水印 ZIP 导出文件")
    file_path = safe_file_path(EXPORTS_DIR, file_name)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="导出文件未找到或已清理")
    return FileResponse(
        path=file_path,
        filename=file_name,
        media_type="application/zip"
    )
