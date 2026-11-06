import io
import os
import json
import uuid
import zipfile
import sqlite3
from pathlib import Path
from typing import List
from PIL import Image
from .config import EXPORTS_DIR, RAW_IMAGES_DIR
from .watermark_service import (
    apply_watermark,
    ensure_watermark_config_schema,
    watermark_config,
)
from .security import safe_file_path


# ---------------------------------------------------------------------------
# Phase 1 – Read-only DB access: validate designs and collect all data needed
# ---------------------------------------------------------------------------

def _collect_export_data(conn: sqlite3.Connection, batch_id: str, design_ids: List[str]) -> tuple:
    """
    Validate every requested design and return all data as plain Python dicts.
    This is the ONLY phase that needs the database; it must complete quickly so
    the caller can release the DB lock before starting heavy image I/O.
    """
    cur = conn.cursor()
    ensure_watermark_config_schema(conn)
    cur.execute("""
        SELECT renderer, logo_asset, logo_width_ratio, logo_left_ratio, logo_top_ratio,
               main_text, repeat_text, opacity, font_size, position
        FROM watermark_configs WHERE batch_id = ?
    """, (batch_id,))
    wm_row = cur.fetchone()
    wm_cfg = watermark_config(dict(wm_row) if wm_row else None)

    designs_data = []
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
        cur.execute("""
            SELECT s.slot_type, sv.version_num, sv.raw_image_path, sv.depends_on_panorama_version_id
            FROM slots s
            JOIN slot_versions sv ON s.current_version_id = sv.id
            WHERE s.design_id = ?
        """, (did,))
        slot_rows = cur.fetchall()

        found_types = {r["slot_type"] for r in slot_rows}
        required_types = {"panorama", "white_isolated", "twin_bed", "nightstand_closeup"}
        if not required_types.issubset(found_types):
            raise ValueError(
                f"设计 {design_code} 槽位不完整，缺少: {required_types - found_types}，拒绝导出"
            )

        designs_data.append({
            "id": d_row["id"],
            "design_code": design_code,
            "series_id": d_row["series_id"],
            "structured_spec": json.loads(d_row["structured_spec"]) if d_row["structured_spec"] else {},
            "visual_features": json.loads(d_row["visual_features"]) if d_row["visual_features"] else {},
            "archived_at": d_row["archived_at"],
            "slots": [
                {
                    "slot_type": r["slot_type"],
                    "version_num": r["version_num"],
                    "raw_image_path": r["raw_image_path"],
                    "depends_on_panorama_version_id": r["depends_on_panorama_version_id"],
                }
                for r in slot_rows
            ],
        })

    return wm_cfg, designs_data


# ---------------------------------------------------------------------------
# Phase 2 – Pure file I/O: watermark images and assemble ZIP (no DB lock)
# ---------------------------------------------------------------------------

def _assemble_watermarked_zip(wm_cfg, designs_data: list, batch_id: str) -> tuple:
    """
    Watermark every image and write the ZIP to a temp file, then rename it into
    place atomically.  No database connection is held here.
    """
    zip_filename = f"export_{batch_id}_{uuid.uuid4().hex[:8]}.zip"
    final_zip_path = EXPORTS_DIR / zip_filename
    temp_zip_path = final_zip_path.with_name(f".{zip_filename}.{uuid.uuid4().hex[:8]}.tmp")

    zf = zipfile.ZipFile(temp_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED)
    try:
        for d in designs_data:
            design_code = d["design_code"]
            for sr in d["slots"]:
                raw_file_name = sr["raw_image_path"]
                if not raw_file_name:
                    raise ValueError(f"设计 {design_code} 槽位 {sr['slot_type']} 缺少原图路径")

                raw_file_path = safe_file_path(RAW_IMAGES_DIR, raw_file_name)
                if not raw_file_path.exists():
                    raise FileNotFoundError(f"原图文件未找到: {raw_file_name}")

                try:
                    with Image.open(raw_file_path) as img:
                        # Always re-watermark the historical raw source.
                        # Never export an existing preview or an unwatermarked fallback.
                        watermarked_img = apply_watermark(img, watermark_cfg=wm_cfg)
                        buf = io.BytesIO()
                        # compress_level=1: trade a few KB for a much shorter export time
                        watermarked_img.save(buf, format="PNG", compress_level=1)
                        img_bytes = buf.getvalue()
                except Exception as e:
                    raise RuntimeError(f"水印施加失败，导出已硬阻断: {str(e)}")

                arcname = (
                    f"{design_code}/{design_code}_{sr['slot_type']}"
                    f"_v{sr['version_num']}_watermarked.png"
                )
                zf.writestr(arcname, img_bytes)

            meta = {
                "design_id": d["id"],
                "design_code": design_code,
                "series_id": d["series_id"],
                "structured_spec": d["structured_spec"],
                "visual_features": d["visual_features"],
                "archived_at": d["archived_at"],
                "watermark_applied": wm_cfg,
                "slots": [
                    {
                        "slot_type": r["slot_type"],
                        "version_num": r["version_num"],
                        "depends_on_panorama": r["depends_on_panorama_version_id"],
                    }
                    for r in d["slots"]
                ],
            }
            zf.writestr(
                f"{design_code}/METADATA.json",
                json.dumps(meta, ensure_ascii=False, indent=2),
            )

        zf.close()
        # Publish only after the complete ZIP has been flushed and closed.
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

    return zip_filename, final_zip_path


# ---------------------------------------------------------------------------
# Phase 3 – Write-back DB access: record every exported design individually
# ---------------------------------------------------------------------------

def _record_exports(
    conn: sqlite3.Connection,
    batch_id: str,
    design_ids: List[str],
    zip_filename: str,
    final_zip_path: Path,
    export_type: str,
    actor_name: str,
    actor_role: str,
) -> None:
    """
    Insert ONE export_records row per design_id so the UI can later mark each
    card as "already exported" by matching design_id.  All rows share the same
    zip file name for re-download purposes.
    """
    file_size = final_zip_path.stat().st_size
    for did in design_ids:
        export_id = f"exp_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO export_records (
                id, batch_id, design_id, export_type,
                file_name, file_path, file_size, exported_by, exported_role
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                export_id, batch_id, did, export_type,
                zip_filename, str(final_zip_path), file_size, actor_name, actor_role,
            ),
        )
