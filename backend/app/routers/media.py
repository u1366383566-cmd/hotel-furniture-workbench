import json
import uuid
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from ..database import get_db
from ..config import RAW_IMAGES_DIR, PREVIEW_IMAGES_DIR
from ..security import UserContext, get_current_user, require_roles, safe_file_path

router = APIRouter(prefix="/api/media", tags=["Media"])

@router.get("/preview/{version_id}")
def get_preview_image(
    version_id: str,
    request: Request,
    user: UserContext = Depends(require_roles(["operator", "leader", "admin"]))
):
    with get_db(immediate=False) as conn:
        cur = conn.cursor()
        cur.execute("SELECT preview_image_path FROM slot_versions WHERE id = ?", (version_id,))
        row = cur.fetchone()
        if not row or not row["preview_image_path"]:
            raise HTTPException(status_code=404, detail="预览图尚未生成或不存在")
        
        file_name = row["preview_image_path"]
        file_path = safe_file_path(PREVIEW_IMAGES_DIR, file_name)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="预览图文件不存在")

        cache_control = "private, max-age=604800, immutable"
        response = FileResponse(path=file_path, media_type="image/jpeg", stat_result=file_path.stat(),
                                headers={"Cache-Control": cache_control})
        etag = response.headers['etag']
        candidates = [value.strip().removeprefix('W/') for value in request.headers.get('if-none-match', '').split(',')]
        if etag in candidates or '*' in candidates:
            return Response(status_code=304, headers={'ETag': etag, 'Cache-Control': cache_control})
        return response

@router.get("/raw/{version_id}")
def get_raw_image(
    version_id: str,
    user: UserContext = Depends(get_current_user)
):
    """
    获取无水印/高清原图。
    安全红线：仅限 admin（管理员）有权查看或下载原图！
    操作员 (operator) 和负责人 (leader) 访问必须直接返回 403 Forbidden！
    任何合法的管理员访问必须关联批次记录 audit_logs 审计追踪！
    """
    # Legacy leader is admin-equivalent for admin operations, but unwatermarked
    # originals remain restricted to the explicit system-admin role.
    if not user.is_system_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限受限：仅系统管理员 (Admin) 允许直接访问无水印原图！操作员与负责人请使用带水印预览图或导出带水印成品。"
        )

    with get_db(immediate=True) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT sv.raw_image_path, d.batch_id
            FROM slot_versions sv
            JOIN designs d ON sv.design_id = d.id
            WHERE sv.id = ?
        """, (version_id,))
        row = cur.fetchone()
        if not row or not row["raw_image_path"]:
            raise HTTPException(status_code=404, detail="原图尚未生成或不存在")

        file_name = row["raw_image_path"]
        batch_id = row["batch_id"]
        file_path = safe_file_path(RAW_IMAGES_DIR, file_name)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="原图文件不存在")

        # 关联批次记录管理员访问原图审计日志
        conn.execute("""
            INSERT INTO audit_logs (id, batch_id, action, actor_name, actor_role, detail)
            VALUES (?, ?, 'access_raw_image', ?, ?, ?)
        """, (f"aud_{uuid.uuid4().hex[:8]}", batch_id, user.user_id, user.role, json.dumps({
            "version_id": version_id,
            "file_name": file_name
        })))

        return FileResponse(path=file_path, media_type="image/png")
