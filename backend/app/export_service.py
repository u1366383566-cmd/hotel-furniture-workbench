import io
import os
import json
import uuid
import zipfile
import sqlite3
from pathlib import Path
from typing import List, Optional
from PIL import Image
from .config import EXPORTS_DIR, RAW_IMAGES_DIR, DEFAULT_WATERMARK
from .watermark_service import apply_watermark
from .security import safe_file_path

def export_designs_to_watermarked_zip(
    conn: sqlite3.Connection,
    batch_id: str,
    design_ids: List[str],
    actor_name: str,
    actor_role: str,
    export_type: str = "batch_archived_zip"
) -> str:
    """
    导出带水印的规范 ZIP 包。
    严格核验：
    - 必须为已归档设计
    - 四槽位必须完备、文件可读
    - 重新统一施加最终水印，水印失败则全单失败
    - 绝不允许退化为无水印原图
    - 记录 export_records
    """
    if not design_ids:
        raise ValueError("没有可导出的归档设计")

    # 获取水印配置
    cur = conn.cursor()
    cur.execute("SELECT main_text, repeat_text, opacity, font_size, position FROM watermark_configs WHERE batch_id = ?", (batch_id,))
    wm_row = cur.fetchone()
    if wm_row:
        wm_cfg = dict(wm_row)
    else:
        wm_cfg = DEFAULT_WATERMARK

    zip_filename = f"export_{batch_id}_{uuid.uuid4().hex[:8]}.zip"
    final_zip_path = EXPORTS_DIR / zip_filename
    temp_zip_path = final_zip_path.with_name(f".{zip_filename}.{uuid.uuid4().hex[:8]}.tmp")
    zf = zipfile.ZipFile(temp_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED)
    try:
        for did in design_ids:
            cur.execute("""
                SELECT id, design_code, series_id, status, structured_spec, visual_features, archived_at
                FROM designs WHERE id = ? AND batch_id = ?
            """, (did, batch_id))
            d_row = cur.fetchone()
            if not d_row:
                raise ValueError(f"设计 {did} 不存在")
            if d_row["status"] != "archived":
                raise ValueError(f"设计 {d_row['design_code']} 尚未完成终审归档，禁止导出！")

            design_code = d_row["design_code"]
            # 获取该设计的四个槽位及其当前锁定版本
            cur.execute("""
                SELECT s.slot_type, sv.version_num, sv.raw_image_path, sv.prompt, sv.depends_on_panorama_version_id
                FROM slots s
                JOIN slot_versions sv ON s.current_version_id = sv.id
                WHERE s.design_id = ?
            """, (did,))
            slot_rows = cur.fetchall()

            found_types = {r["slot_type"] for r in slot_rows}
            required_types = {"panorama", "white_isolated", "twin_bed", "nightstand_closeup"}
            if not required_types.issubset(found_types):
                raise ValueError(f"设计 {design_code} 槽位不完整，缺少: {required_types - found_types}，拒绝导出")

            # 写入每张图片（施加最终水印）
            for sr in slot_rows:
                raw_file_name = sr["raw_image_path"]
                if not raw_file_name:
                    raise ValueError(f"设计 {design_code} 槽位 {sr['slot_type']} 缺少原图路径")
                
                raw_file_path = safe_file_path(RAW_IMAGES_DIR, raw_file_name)
                if not raw_file_path.exists():
                    raise FileNotFoundError(f"原图文件未找到: {raw_file_name}")

                try:
                    with Image.open(raw_file_path) as img:
                        watermarked_img = apply_watermark(
                            img,
                            main_text=wm_cfg.get("main_text", DEFAULT_WATERMARK["main_text"]),
                            repeat_text=wm_cfg.get("repeat_text", DEFAULT_WATERMARK["repeat_text"]),
                            opacity=float(wm_cfg.get("opacity", 0.22)),
                            font_size=int(wm_cfg.get("font_size", 36)),
                            position=wm_cfg.get("position", "bottom_right")
                        )
                        # 保存到内存字节流再打入 ZIP；低压缩级别显著缩短批量导出耗时
                        buf = io.BytesIO()
                        watermarked_img.save(buf, format="PNG", compress_level=1)
                        img_bytes = buf.getvalue()
                except Exception as e:
                    # 水印处理失败必须立即中断并抛错，绝不降级为无水印
                    raise RuntimeError(f"水印施加失败，导出已硬阻断: {str(e)}")

                arcname = f"{design_code}/{design_code}_{sr['slot_type']}_v{sr['version_num']}_watermarked.png"
                zf.writestr(arcname, img_bytes)

            # 写入元数据 JSON
            meta = {
                "design_id": d_row["id"],
                "design_code": design_code,
                "series_id": d_row["series_id"],
                "structured_spec": json.loads(d_row["structured_spec"]) if d_row["structured_spec"] else {},
                "visual_features": json.loads(d_row["visual_features"]) if d_row["visual_features"] else {},
                "archived_at": d_row["archived_at"],
                "watermark_applied": wm_cfg,
                "slots": [
                    {
                        "slot_type": r["slot_type"],
                        "version_num": r["version_num"],
                        "depends_on_panorama": r["depends_on_panorama_version_id"]
                    } for r in slot_rows
                ]
            }
            zf.writestr(f"{design_code}/METADATA.json", json.dumps(meta, ensure_ascii=False, indent=2))

        zf.close()
        # Publish only after the complete ZIP has been closed successfully.
        os.replace(temp_zip_path, final_zip_path)
    except Exception:
        try:
            zf.close()
        except Exception:
            pass
        try:
            temp_zip_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    file_size = final_zip_path.stat().st_size
    export_id = f"exp_{uuid.uuid4().hex[:12]}"
    conn.execute("""
        INSERT INTO export_records (
            id, batch_id, design_id, export_type, file_name, file_path, file_size, exported_by, exported_role
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        export_id, batch_id, design_ids[0] if len(design_ids) == 1 else None,
        export_type, zip_filename, str(final_zip_path), file_size, actor_name, actor_role
    ))

    return zip_filename
