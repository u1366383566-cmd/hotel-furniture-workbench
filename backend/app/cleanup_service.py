import os
import sqlite3
from pathlib import Path
from typing import Dict, Any, Set
from .config import RAW_IMAGES_DIR, PREVIEW_IMAGES_DIR

def run_protected_cleanup(
    conn: sqlite3.Connection,
    days_threshold: int = 30
) -> Dict[str, Any]:
    """
    执行受保护的旧版本文件清理。
    绝对保护原则：
    1. 保护所有归档设计的当前锁定版本及关联版本
    2. 保护所有槽位当前生效版本 (slots.current_version_id)
    3. 保护所有被下游有效引用的全景版本 (depends_on_panorama_version_id)
    4. 保护显式标记锁定的版本 (is_locked = 1)
    5. 时间约束：只清理创建时间早于 days_threshold 天的废弃版本，保留年轻版本！
    6. 删除安全：物理删除成功后才清空数据库路径引用，删除失败保留路径。
    """
    cur = conn.cursor()
    
    # 1. 查询所有必须绝对保护的 version_id 集合
    protected_version_ids: Set[str] = set()

    # a. 槽位当前指向的有效版本
    cur.execute("SELECT current_version_id FROM slots WHERE current_version_id IS NOT NULL")
    for r in cur.fetchall():
        protected_version_ids.add(r["current_version_id"])

    # b. 显式锁定的版本
    cur.execute("SELECT id FROM slot_versions WHERE is_locked = 1")
    for r in cur.fetchall():
        protected_version_ids.add(r["id"])

    # c. 被下游版本依赖的全景版本
    cur.execute("SELECT DISTINCT depends_on_panorama_version_id FROM slot_versions WHERE depends_on_panorama_version_id IS NOT NULL")
    for r in cur.fetchall():
        protected_version_ids.add(r["depends_on_panorama_version_id"])

    # d. 已归档设计下的所有关联槽位版本
    cur.execute("""
        SELECT s.current_version_id 
        FROM designs d 
        JOIN slots s ON d.id = s.design_id 
        WHERE d.status = 'archived' AND s.current_version_id IS NOT NULL
    """)
    for r in cur.fetchall():
        protected_version_ids.add(r["current_version_id"])

    # 2. 查询所有候选可清理版本（非受保护、满足天数阈值、历史废弃或失败版本）
    placeholders = ",".join("?" for _ in protected_version_ids) if protected_version_ids else "''"
    time_modifier = f"-{days_threshold} days"
    sql = f"""
        SELECT id, raw_image_path, preview_image_path, process_status, is_locked, created_at
        FROM slot_versions
        WHERE id NOT IN ({placeholders})
          AND (process_status IN ('failed', 'completed') OR is_invalidated = 1)
          AND datetime(created_at) <= datetime('now', 'localtime', ?)
    """
    params = list(protected_version_ids) + [time_modifier]
    cur.execute(sql, params)
    candidates = cur.fetchall()

    deleted_files = 0
    freed_bytes = 0

    for row in candidates:
        v_id = row["id"]
        raw_name = row["raw_image_path"]
        preview_name = row["preview_image_path"]

        raw_deleted = False
        preview_deleted = False

        if raw_name:
            raw_file = RAW_IMAGES_DIR / raw_name
            if raw_file.exists() and raw_file.is_file():
                try:
                    size = raw_file.stat().st_size
                    raw_file.unlink()
                    deleted_files += 1
                    freed_bytes += size
                    raw_deleted = True
                except Exception:
                    raw_deleted = False
            else:
                raw_deleted = True

        if preview_name:
            prev_file = PREVIEW_IMAGES_DIR / preview_name
            if prev_file.exists() and prev_file.is_file():
                try:
                    size = prev_file.stat().st_size
                    prev_file.unlink()
                    deleted_files += 1
                    freed_bytes += size
                    preview_deleted = True
                except Exception:
                    preview_deleted = False
            else:
                preview_deleted = True

        # 仅在物理删除成功或原本无文件时，才置空对应字段，绝不因删除失败而误清空路径
        update_fields = []
        update_params = []
        if raw_deleted:
            update_fields.append("raw_image_path = NULL")
        if preview_deleted:
            update_fields.append("preview_image_path = NULL")
        
        if update_fields:
            sql_update = f"UPDATE slot_versions SET {', '.join(update_fields)} WHERE id = ?"
            update_params.append(v_id)
            conn.execute(sql_update, update_params)

    return {
        "protected_version_count": len(protected_version_ids),
        "cleaned_candidates_count": len(candidates),
        "deleted_files_count": deleted_files,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
        "days_threshold": days_threshold
    }
