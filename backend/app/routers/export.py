from typing import List
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from ..database import get_db
from ..config import EXPORTS_DIR
from ..security import UserContext, require_roles, safe_file_path
from ..export_service import _collect_export_data, _assemble_watermarked_zip, _record_exports

router = APIRouter(prefix="/api/export", tags=["Export"])


def _save_records(batch_id, design_ids, zip_filename, final_zip_path, export_type, user):
    try:
        with get_db(immediate=True) as conn:
            _record_exports(conn, batch_id, design_ids, zip_filename, final_zip_path,
                            export_type, user.user_id, user.role)
    except Exception:
        final_zip_path.unlink(missing_ok=True)
        raise


class SelectedExportRequest(BaseModel):
    batch_id: str
    design_ids: List[str] = Field(..., min_length=1)


@router.post("/design/{design_id}")
def export_single_design(
    design_id: str,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"])),
):
    """导出单款归档设计的带水印 ZIP"""
    # Phase 1 – brief DB read to validate + collect data
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT batch_id, status FROM designs WHERE id = ?", (design_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="设计不存在")
        if row["status"] != "archived":
            raise HTTPException(status_code=400, detail="该设计尚未终审归档，禁止导出！")
        batch_id = row["batch_id"]
        try:
            wm_cfg, designs_data = _collect_export_data(conn, batch_id, [design_id])
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # Phase 2 – watermark + ZIP assembly, NO DB lock held
    try:
        zip_filename, final_zip_path = _assemble_watermarked_zip(wm_cfg, designs_data, batch_id)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Phase 3 – brief DB write to record the export
    _save_records(batch_id, [design_id], zip_filename, final_zip_path, "single_design_zip", user)

    return {
        "message": "带水印 ZIP 导出成功",
        "file_name": zip_filename,
        "download_url": f"/api/export/download/{zip_filename}",
    }


@router.post("/selected")
def export_selected_designs(
    req: SelectedExportRequest,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"])),
):
    """部分导出：导出指定的若干已归档设计"""
    # Phase 1 – validate all requested designs + collect data
    with get_db() as conn:
        placeholders = ",".join("?" for _ in req.design_ids)
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, design_code, status FROM designs WHERE batch_id = ? AND id IN ({placeholders})",
            [req.batch_id] + req.design_ids,
        )
        rows = cur.fetchall()
        if len(rows) != len(req.design_ids):
            raise HTTPException(status_code=404, detail="部分所选设计不存在")
        for r in rows:
            if r["status"] != "archived":
                raise HTTPException(
                    status_code=400,
                    detail=f"设计 {r['design_code']} 尚未完成归档，禁止部分导出！",
                )
        try:
            wm_cfg, designs_data = _collect_export_data(conn, req.batch_id, req.design_ids)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # Phase 2 – watermark + ZIP, NO DB lock held
    try:
        zip_filename, final_zip_path = _assemble_watermarked_zip(wm_cfg, designs_data, req.batch_id)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Phase 3 – one record per design
    _save_records(req.batch_id, req.design_ids, zip_filename, final_zip_path, "partial_archived_zip", user)

    return {
        "message": f"成功部分导出 {len(req.design_ids)} 款已归档设计带水印 ZIP",
        "archived_count": len(req.design_ids),
        "file_name": zip_filename,
        "download_url": f"/api/export/download/{zip_filename}",
    }


@router.post("/batch/{batch_id}")
def export_batch_archived(
    batch_id: str,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"])),
):
    """导出批次中全部已归档设计的带水印 ZIP"""
    # Phase 1 – brief DB read
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM designs WHERE batch_id = ? AND status = 'archived'", (batch_id,)
        )
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(status_code=400, detail="批次中暂无已归档的设计，无法导出")
        dids = [r["id"] for r in rows]
        try:
            wm_cfg, designs_data = _collect_export_data(conn, batch_id, dids)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # Phase 2 – watermark + ZIP, NO DB lock held
    try:
        zip_filename, final_zip_path = _assemble_watermarked_zip(wm_cfg, designs_data, batch_id)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Phase 3 – one record per design for full traceability
    _save_records(batch_id, dids, zip_filename, final_zip_path, "batch_archived_zip", user)

    return {
        "message": f"成功导出 {len(dids)} 款归档设计带水印 ZIP",
        "archived_count": len(dids),
        "file_name": zip_filename,
        "download_url": f"/api/export/download/{zip_filename}",
    }


@router.get("/records/{batch_id}", dependencies=[Depends(require_roles(["operator", "leader", "admin"]))])
def list_export_records(
    batch_id: str,
):
    """批次导出记录（重复导出可追踪；每款设计独立一条记录）"""
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, design_id, export_type, file_name, file_size,
                   exported_by, exported_role, created_at
            FROM export_records WHERE batch_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (batch_id,),
        )
        return [dict(r) for r in cur.fetchall()]


@router.get("/download/{file_name}", dependencies=[Depends(require_roles(["operator", "leader", "admin"]))])
@router.head("/download/{file_name}", dependencies=[Depends(require_roles(["operator", "leader", "admin"]))])
def download_export_file(
    file_name: str,
):
    if not file_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="只允许下载带水印 ZIP 导出文件")
    file_path = safe_file_path(EXPORTS_DIR, file_name)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="导出文件未找到或已清理")
    return FileResponse(path=file_path, filename=file_name, media_type="application/zip")
