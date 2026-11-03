import json
import os
import time
import uuid
import asyncio
import logging
import sqlite3
from pathlib import Path
from PIL import Image
from typing import Optional, Dict, Any
from .database import get_db
from .config import RAW_IMAGES_DIR, PREVIEW_IMAGES_DIR
from .watermark_service import save_mock_slot_image, save_real_slot_image
from .planning_service import build_real_generation_prompt
from .budget_service import (
    settle_budget_success, release_budget_failed, hold_budget_unknown
)
from .image_adapter import (
    ExplicitRateLimitError, ImageApiMisconfigError,
    generate_image_real, edit_image_real, load_image_api_config,
)

logger = logging.getLogger(__name__)

# Unit tests call process_next_task explicitly. Keeping the production loop out of
# those sessions removes cross-test timing while exercising the same worker code.
BACKGROUND_LOOP_DISABLED = os.environ.get("WORKBENCH_DISABLE_WORKER_LOOP") == "1"

class TaskWorker:
    def __init__(self):
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None

    async def start(self):
        self._running = True
        self.recover_interrupted_tasks()
        if BACKGROUND_LOOP_DISABLED:
            logger.info("TaskWorker background loop disabled for isolated tests.")
            return
        self._loop_task = asyncio.create_task(self._worker_loop())
        logger.info("TaskWorker background loop started.")

    async def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        logger.info("TaskWorker background loop stopped.")

    def recover_interrupted_tasks(self):
        """
        系统重启自愈恢复策略：
        扫描上次处于 'running' 状态的遗留任务。
        1. 检查本地磁盘是否已完整落盘且文件可读：
           - 若已完整落盘：在防过时回写检查前提下，恢复为 completed 并结算预算；
           - 若未落盘或损坏：转入 unknown_pending_confirmation，保守持有预算账本，并暂停批次！
        """
        with get_db(immediate=True) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT t.id, t.batch_id, t.slot_version_id, t.slot_id, t.design_id,
                       sv.raw_image_path, sv.preview_image_path,
                       d.status as design_status, s.current_version_id as slot_current_vid, s.status as slot_status
                FROM tasks t
                JOIN slot_versions sv ON t.slot_version_id = sv.id
                JOIN designs d ON t.design_id = d.id
                JOIN slots s ON t.slot_id = s.id
                WHERE t.status = 'running'
            """)
            interrupted = cur.fetchall()

            for row in interrupted:
                tid = row["id"]
                bid = row["batch_id"]
                vid = row["slot_version_id"]
                sid = row["slot_id"]
                did = row["design_id"]
                raw_path_str = row["raw_image_path"]
                prev_path_str = row["preview_image_path"]
                design_curr_status = row["design_status"]
                slot_current_vid = row["slot_current_vid"]

                has_valid_disk_file = False
                if raw_path_str and prev_path_str:
                    raw_file = RAW_IMAGES_DIR / raw_path_str
                    prev_file = PREVIEW_IMAGES_DIR / prev_path_str
                    if raw_file.exists() and prev_file.exists():
                        try:
                            with Image.open(raw_file) as r_img:
                                r_img.verify()
                            with Image.open(prev_file) as p_img:
                                p_img.verify()
                            has_valid_disk_file = True
                        except Exception:
                            has_valid_disk_file = False

                cur.execute("SELECT id FROM budget_ledger WHERE reference_task_id = ?", (tid,))
                bl_row = cur.fetchone()
                ledger_id = bl_row["id"] if bl_row else None

                if has_valid_disk_file:
                    conn.execute("UPDATE tasks SET status = 'completed', updated_at = datetime('now', 'localtime') WHERE id = ?", (tid,))
                    conn.execute("UPDATE slot_versions SET process_status = 'completed', is_unknown = 0 WHERE id = ?", (vid,))
                    if ledger_id:
                        settle_budget_success(conn, bid, ledger_id, tid)

                    # 仅在设计非终态且槽位未被换指针时恢复指针
                    if design_curr_status not in ("eliminated", "archived") and slot_current_vid in (vid, None):
                        conn.execute("UPDATE slots SET status = 'ready', current_version_id = ? WHERE id = ?", (vid, sid))
                    logger.info(f"Recovered task {tid} as completed from valid disk file.")
                else:
                    conn.execute("UPDATE tasks SET status = 'unknown', last_error = 'Interrupted by restart, unverified upstream state', updated_at = datetime('now', 'localtime') WHERE id = ?", (tid,))
                    conn.execute("UPDATE slot_versions SET process_status = 'unknown_pending_confirmation', is_unknown = 1, error_message = 'Interrupted by restart' WHERE id = ?", (vid,))
                    if ledger_id:
                        hold_budget_unknown(conn, bid, ledger_id, reason="Server restarted during execution")
                    conn.execute("""
                        UPDATE batches 
                        SET status = 'paused', pause_reason = 'PAUSE_UNKNOWN_PENDING', updated_at = datetime('now', 'localtime')
                        WHERE id = ?
                    """, (bid,))
                    logger.warning(f"Recovered interrupted task {tid} as unknown_pending_confirmation and paused batch {bid}.")

    async def _worker_loop(self):
        while self._running:
            try:
                # 真实生图上游可能耗时数十秒：任务执行放入工作线程，避免阻塞事件循环
                # 导致 Web 接口（审核/预算/设置页）全部冻结。
                processed = await asyncio.to_thread(self.process_next_task)
                if not processed:
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Worker loop error: {e}", exc_info=True)
                await asyncio.sleep(1.0)

    def process_next_task(self) -> bool:
        """
        从 SQLite 领取下一个任务。
        约束：
        - 批次 active 且未熔断 (b.is_circuit_broken == 0)
        - 批次无未决 unknown 挂起任务
        - 关联设计未淘汰 (status != 'eliminated')
        """
        with get_db(immediate=True) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT t.id, t.batch_id, t.design_id, t.slot_id, t.slot_version_id, t.task_type, t.retry_count,
                       d.series_id, d.design_code, d.structured_spec, d.status as design_status,
                       d.template_version as design_tpl_ver,
                       s.slot_type, s.current_version_id as slot_current_vid,
                       sv.version_num, sv.prompt, sv.prompt_metadata, sv.is_invalidated,
                       sv.depends_on_panorama_version_id
                FROM tasks t
                JOIN batches b ON t.batch_id = b.id
                JOIN designs d ON t.design_id = d.id
                JOIN slots s ON t.slot_id = s.id
                JOIN slot_versions sv ON t.slot_version_id = sv.id
                WHERE t.status = 'queued'
                  AND b.status = 'active'
                  AND b.is_circuit_broken = 0
                  AND NOT (b.trial_mode = 1 AND b.trial_run_remaining <= 0)
                  AND d.status != 'eliminated'
                  AND NOT EXISTS (
                      SELECT 1 FROM slot_versions sv2
                      JOIN designs d2 ON sv2.design_id = d2.id
                      WHERE d2.batch_id = t.batch_id AND sv2.process_status = 'unknown_pending_confirmation'
                  )
                  -- 白底依赖链顺序保险：双床/床头近景以白底清单图（white_isolated 当前版本）为
                  -- 以图生图参考，其成品未落盘前不领取，防止拿错参考图或空参考退化。
                  AND (
                      s.slot_type IN ('panorama', 'white_isolated')
                      OR EXISTS (
                          SELECT 1
                          FROM slots s2
                          JOIN slot_versions sv2 ON s2.current_version_id = sv2.id
                          WHERE s2.design_id = d.id
                            AND s2.slot_type = 'white_isolated'
                            AND sv2.process_status = 'completed'
                            AND sv2.raw_image_path IS NOT NULL
                      )
                  )
                ORDER BY t.created_at ASC
                LIMIT 1
            """)
            task_row = cur.fetchone()
            if not task_row:
                return False

            task_id = task_row["id"]
            conn.execute("UPDATE tasks SET status = 'running', updated_at = datetime('now', 'localtime') WHERE id = ?", (task_id,))
            conn.execute("UPDATE slot_versions SET process_status = 'processing' WHERE id = ?", (task_row["slot_version_id"],))

        self._execute_single_task(task_row)
        return True

    def _execute_single_task(self, task_row: sqlite3.Row):
        task_id = task_row["id"]
        batch_id = task_row["batch_id"]
        design_id = task_row["design_id"]
        slot_id = task_row["slot_id"]
        slot_version_id = task_row["slot_version_id"]
        series_id = task_row["series_id"]
        design_code = task_row["design_code"]
        slot_type = task_row["slot_type"]
        version_num = task_row["version_num"]
        prompt = task_row["prompt"]
        retry_count = task_row["retry_count"]
        structured_spec = json.loads(task_row["structured_spec"]) if task_row["structured_spec"] else {}

        with get_db(immediate=False) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM budget_ledger WHERE reference_task_id = ?", (task_id,))
            b_row = cur.fetchone()
            ledger_id = b_row["id"] if b_row else None

            cur.execute("SELECT main_text, repeat_text, opacity, font_size, position FROM watermark_configs WHERE batch_id = ?", (batch_id,))
            wm_row = cur.fetchone()
            wm_cfg = dict(wm_row) if wm_row else None

            image_api_cfg = load_image_api_config(conn)

        raw_generated = False
        raw_name = None
        preview_name = None
        upstream_request_id = f"mock_req_{uuid.uuid4().hex[:10]}"
        try:
            if image_api_cfg["enabled"]:
                # === 真实生图上游分支（错误分类严格映射既有语义） ===
                # 参考链：全景 = 文生图；白底清单 = 以全景为参考；
                # 双床/床头近景 = 以白底清单图（white_isolated 当前版本）为参考，兜底全景。
                prompt_metadata = json.loads(task_row["prompt_metadata"]) if task_row["prompt_metadata"] else {}
                reference_bytes = None
                reference_dep_id = None
                if slot_type != "panorama":
                    if slot_type == "white_isolated":
                        reference_dep_id = task_row["depends_on_panorama_version_id"]
                    else:
                        # 双床/床头近景继承白底清单图（第一张白底图）为设计依据
                        with get_db(immediate=False) as ref_conn:
                            wi_row = ref_conn.execute("""
                                SELECT sv.raw_image_path, sv.id
                                FROM slots s JOIN slot_versions sv ON s.current_version_id = sv.id
                                WHERE s.design_id = ? AND s.slot_type = 'white_isolated'
                            """, (design_id,)).fetchone()
                        if wi_row and wi_row["raw_image_path"]:
                            reference_dep_id = wi_row["id"]
                        else:
                            reference_dep_id = task_row["depends_on_panorama_version_id"]
                    if reference_dep_id:
                        with get_db(immediate=False) as ref_conn:
                            ref_row = ref_conn.execute(
                                "SELECT raw_image_path FROM slot_versions WHERE id = ?", (reference_dep_id,)
                            ).fetchone()
                        if ref_row and ref_row["raw_image_path"]:
                            ref_file = RAW_IMAGES_DIR / ref_row["raw_image_path"]
                            if ref_file.exists():
                                reference_bytes = ref_file.read_bytes()

                real_prompt = build_real_generation_prompt(
                    slot_type, structured_spec, design_code, prompt_metadata,
                    has_reference_image=reference_bytes is not None,
                )
                if reference_bytes is not None:
                    ref_tag = "white_isolated" if slot_type not in ("panorama", "white_isolated") else "panorama"
                    raw_bytes, upstream_request_id = edit_image_real(
                        image_api_cfg, real_prompt, reference_bytes,
                        filename=f"{design_code}_{ref_tag}_ref.png",
                    )
                else:
                    raw_bytes, upstream_request_id = generate_image_real(image_api_cfg, real_prompt)
                raw_name, preview_name = save_real_slot_image(
                    raw_bytes=raw_bytes,
                    slot_type=slot_type,
                    series_id=series_id,
                    design_code=design_code,
                    version_num=version_num,
                    watermark_cfg=wm_cfg,
                    version_id=slot_version_id,
                )
                raw_generated = True
            else:
                # === Mock 故障注入分支 ===
                if "__INJECT_401__" in prompt:
                    raise PermissionError("HTTP 401 Unauthorized: Invalid API key or token expired")
                elif "__INJECT_403__" in prompt:
                    raise PermissionError("HTTP 403 Forbidden: Account suspended or access denied")
                elif "__INJECT_CONTENT_POLICY__" in prompt:
                    raise ValueError("HTTP 422 Content Policy Violation: Sensitive layout rejected")
                elif "__INJECT_429__" in prompt:
                    # 显式 429 速率限制（支持技术重试）
                    raise ExplicitRateLimitError("HTTP 429 Too Many Requests: Rate limit reached")
                elif "__INJECT_TIMEOUT__" in prompt or "__SIMULATE_TIMEOUT_UNKNOWN__" in prompt:
                    raise TimeoutError("Network timeout: Gateway connection dropped without response")
                elif "__INJECT_CONNECTION_RESET__" in prompt:
                    # 真正连接重置：属于网络中断未知状态，绝非 429！
                    raise ConnectionResetError("Connection reset by peer: Socket closed abruptly")
                elif "__INJECT_DISK_FULL__" in prompt:
                    raise OSError(28, "No space left on device: Disk full")

                # 本地生成原图与预览图
                raw_name, preview_name = save_mock_slot_image(
                    slot_type=slot_type,
                    series_id=series_id,
                    design_code=design_code,
                    version_num=version_num,
                    structured_spec=structured_spec,
                    watermark_cfg=wm_cfg,
                    version_id=slot_version_id
                )
                raw_generated = True
                # 水印后处理模拟故障（发生在原图已生成之后）
                if "__INJECT_WATERMARK_FAIL__" in prompt:
                    raise RuntimeError("WatermarkEngineError: Failed to composite watermark layer on generated raw image")

            # === 回写前原子校验状态（防淘汰复活与旧任务覆盖新指针） ===
            with get_db(immediate=True) as conn:
                cur = conn.cursor()
                cur.execute("SELECT status FROM designs WHERE id = ?", (design_id,))
                d_curr = cur.fetchone()
                if not d_curr:
                    return

                design_curr_status = d_curr["status"]
                # 1. 检查设计终态：若已被人工淘汰或已归档，任务成果作废，绝不修改槽位与设计状态！
                if design_curr_status in ("eliminated", "archived"):
                    conn.execute("UPDATE tasks SET status = 'completed', last_error = 'Discarded: design already finalized/eliminated', updated_at = datetime('now', 'localtime') WHERE id = ?", (task_id,))
                    conn.execute("UPDATE slot_versions SET process_status = 'completed', raw_image_path = ?, preview_image_path = ? WHERE id = ?", (raw_name, preview_name, slot_version_id))
                    if ledger_id:
                        settle_budget_success(conn, batch_id, ledger_id, task_id)
                    logger.info(f"Task {task_id} completed but discarded because design is {design_curr_status}.")
                    return

                # 2. 检查槽位当前状态：若该槽位已有更新版本生成或被替换，不覆盖当前指针
                cur.execute("SELECT current_version_id, status FROM slots WHERE id = ?", (slot_id,))
                s_curr = cur.fetchone()
                # Pointer comparison alone is authoritative. Slot state can change
                # through reviews or restarts while a task remains in flight.
                is_stale_task = s_curr and s_curr["current_version_id"] != slot_version_id

                # A panorama replacement can invalidate this in-flight downstream
                # result while the task row still carries its dispatch-time snapshot.
                # Always re-read invalidation and dependency here, then compare with
                # the panorama pointer in the same write transaction. A version that
                # was once invalidated remains invalid even if the leader switches back.
                is_invalid_dependency = False
                if not is_stale_task and slot_type != "panorama":
                    cur.execute("""
                        SELECT is_invalidated, depends_on_panorama_version_id
                        FROM slot_versions WHERE id = ?
                    """, (slot_version_id,))
                    dependency_row = cur.fetchone()
                    cur.execute("""
                        SELECT current_version_id FROM slots
                        WHERE design_id = ? AND slot_type = 'panorama'
                    """, (design_id,))
                    pano_row = cur.fetchone()
                    active_pano_vid = pano_row["current_version_id"] if pano_row else None
                    is_invalid_dependency = bool(dependency_row and dependency_row["is_invalidated"]) or (
                        not dependency_row or dependency_row["depends_on_panorama_version_id"] != active_pano_vid
                    )

                if is_invalid_dependency:
                    conn.execute("""
                        UPDATE slot_versions
                        SET raw_image_path = ?, preview_image_path = ?, process_status = 'completed',
                            upstream_request_id = ?, is_invalidated = 1, is_mock = ?,
                            error_message = 'Discarded: depends on replaced panorama'
                        WHERE id = ?
                    """, (raw_name, preview_name, upstream_request_id,
                          0 if image_api_cfg["enabled"] else 1, slot_version_id))
                    conn.execute("""
                        UPDATE tasks SET status = 'completed',
                            last_error = 'Discarded: downstream dependency replaced',
                            updated_at = datetime('now', 'localtime') WHERE id = ?
                    """, (task_id,))
                    if ledger_id:
                        settle_budget_success(conn, batch_id, ledger_id, task_id)
                    logger.info(f"Task {task_id} completed but invalidated by panorama replacement.")
                    return

                conn.execute("""
                    UPDATE slot_versions
                    SET raw_image_path = ?, preview_image_path = ?, process_status = 'completed',
                        upstream_request_id = ?, is_mock = ?
                    WHERE id = ?
                """, (raw_name, preview_name, upstream_request_id,
                      0 if image_api_cfg["enabled"] else 1, slot_version_id))

                if image_api_cfg["enabled"] and not is_stale_task:
                    # 真实模式：把实际发送的提示词与上游信息持久化到版本 metadata（可追溯）
                    meta = json.loads(task_row["prompt_metadata"]) if task_row["prompt_metadata"] else {}
                    meta.update({
                        "generation_mode": "real_api",
                        "generation_pipeline": "image_edit" if reference_bytes is not None else "text2img",
                        "reference_panorama_version_id": reference_dep_id if reference_bytes is not None else None,
                        "upstream_model": image_api_cfg["model"],
                        "upstream_base_url": image_api_cfg["base_url"],
                        "real_prompt": real_prompt,
                    })
                    conn.execute("UPDATE slot_versions SET prompt_metadata = ? WHERE id = ?",
                                 (json.dumps(meta, ensure_ascii=False), slot_version_id))

                conn.execute("UPDATE tasks SET status = 'completed', updated_at = datetime('now', 'localtime') WHERE id = ?", (task_id,))

                if not is_stale_task:
                    conn.execute("""
                        UPDATE slots 
                        SET status = 'ready', current_version_id = ?, updated_at = datetime('now', 'localtime')
                        WHERE id = ?
                    """, (slot_version_id, slot_id))

                if ledger_id:
                    settle_budget_success(conn, batch_id, ledger_id, task_id)

                # 试运行检查：恢复后的试运行模式真正限制派发数量
                cur.execute("SELECT trial_mode, trial_run_remaining FROM batches WHERE id = ?", (batch_id,))
                b_tr = cur.fetchone()
                if b_tr and b_tr["trial_mode"] == 1 and b_tr["trial_run_remaining"] > 0:
                    if b_tr["trial_run_remaining"] == 1:
                        conn.execute("""
                            UPDATE batches
                            SET is_circuit_broken = 1,
                                circuit_break_reason = 'Trial run quota exhausted',
                                status = 'paused', pause_reason = 'CIRCUIT_BROKEN',
                                updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                        """, (batch_id,))
                    conn.execute("UPDATE batches SET trial_run_remaining = trial_run_remaining - 1 WHERE id = ?", (batch_id,))

                # 状态推进
                if not is_stale_task:
                    if slot_type == "panorama":
                        if design_curr_status == "panorama_generating":
                            conn.execute("UPDATE designs SET status = 'panorama_review', updated_at = datetime('now', 'localtime') WHERE id = ?", (design_id,))
                    else:
                        cur.execute("""
                            SELECT s.slot_type, s.status 
                            FROM slots s 
                            WHERE s.design_id = ? AND s.slot_type != 'panorama'
                        """, (design_id,))
                        downstream_slots = cur.fetchall()
                        all_downstream_ready = len(downstream_slots) == 3 and all(r["status"] in ("ready", "approved") for r in downstream_slots)
                        if all_downstream_ready and design_curr_status in ("downstream_generating", "panorama_approved"):
                            conn.execute("UPDATE designs SET status = 'quad_review', updated_at = datetime('now', 'localtime') WHERE id = ?", (design_id,))

        except PermissionError as pe:
            # 明确拒绝 (401/403)：确认无扣费，释放预占预算，熔断批次
            logger.error(f"Task {task_id} Auth Failure: {pe}")
            with get_db(immediate=True) as conn:
                conn.execute("UPDATE tasks SET status = 'failed', last_error = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (str(pe), task_id))
                conn.execute("UPDATE slot_versions SET process_status = 'failed', error_message = ? WHERE id = ?", (str(pe), slot_version_id))
                conn.execute("UPDATE slots SET status = 'empty', updated_at = datetime('now', 'localtime') WHERE id = ?", (slot_id,))
                if ledger_id:
                    release_budget_failed(conn, batch_id, ledger_id, reason=str(pe))
                conn.execute("""
                    UPDATE batches 
                    SET is_circuit_broken = 1, circuit_break_reason = ?, status = 'paused', pause_reason = 'CIRCUIT_BROKEN', updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                """, (f"Circuit broken: {str(pe)}", batch_id))

        except ExplicitRateLimitError as rle:
            # 显式 429 速率限制：技术重试（与人工重做严格分离，不消耗人工限额）
            logger.warning(f"Task {task_id} Explicit Rate Limited (429): retry {retry_count}/3")
            with get_db(immediate=True) as conn:
                conn.execute("UPDATE slots SET technical_retry_count = technical_retry_count + 1 WHERE id = ?", (slot_id,))
                # 记录一次技术重试消耗流水
                retry_log_id = f"bgt_retry_{uuid.uuid4().hex[:8]}"
                conn.execute("""
                    INSERT INTO budget_ledger (id, batch_id, category, delta_reserved, delta_used, delta_unsettled, status, ledger_event_type, reference_task_id, note)
                    VALUES (?, ?, 'image_generation', 0, 0, 1, 'released', 'technical_retry', ?, 'Technical 429 retry backoff')
                """, (retry_log_id, batch_id, task_id))

                if retry_count < 3:
                    conn.execute("UPDATE tasks SET status = 'queued', retry_count = retry_count + 1, last_error = ? WHERE id = ?", (str(rle), task_id))
                    conn.execute("UPDATE slot_versions SET process_status = 'pending', error_message = ? WHERE id = ?", (f"Retrying: {str(rle)}", slot_version_id))
                else:
                    conn.execute("UPDATE tasks SET status = 'failed', last_error = 'Rate limit retry limit exceeded' WHERE id = ?", (task_id,))
                    conn.execute("UPDATE slot_versions SET process_status = 'failed', error_message = 'Max technical retries reached' WHERE id = ?", (slot_version_id,))
                    conn.execute("UPDATE slots SET status = 'empty' WHERE id = ?", (slot_id,))
                    if ledger_id:
                        # 429 表示上游明确拒绝受理，未产生生图消耗 → 释放预占，防止幽灵占用额度
                        release_budget_failed(conn, batch_id, ledger_id, reason="Rate limit retry limit exceeded")
                    conn.execute("""
                        UPDATE batches
                        SET is_circuit_broken = 1, circuit_break_reason = 'Upstream 429 Rate Limit Exhausted', status = 'paused', pause_reason = 'CIRCUIT_BROKEN'
                        WHERE id = ?
                    """, (batch_id,))

        except (TimeoutError, ConnectionResetError) as unk_err:
            # 网络超时或真实连接重置：状态未知，保守计入预算持有，暂停批次防雪崩
            logger.error(f"Task {task_id} Timeout or Connection Reset (Unknown): {unk_err}")
            with get_db(immediate=True) as conn:
                conn.execute("UPDATE tasks SET status = 'unknown', last_error = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (str(unk_err), task_id))
                conn.execute("""
                    UPDATE slot_versions 
                    SET process_status = 'unknown_pending_confirmation', is_unknown = 1, error_message = ?
                    WHERE id = ?
                """, (str(unk_err), slot_version_id))
                if ledger_id:
                    hold_budget_unknown(conn, batch_id, ledger_id, reason=str(unk_err))
                conn.execute("""
                    UPDATE batches 
                    SET status = 'paused', pause_reason = 'PAUSE_UNKNOWN_PENDING', updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                """, (batch_id,))

        except OSError as ose:
            # 磁盘故障：记录错误并熔断批次
            logger.critical(f"Task {task_id} Disk Full/OS Error: {ose}")
            with get_db(immediate=True) as conn:
                conn.execute("UPDATE tasks SET status = 'failed', last_error = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (str(ose), task_id))
                conn.execute("UPDATE slot_versions SET process_status = 'failed', error_message = ? WHERE id = ?", (str(ose), slot_version_id))
                conn.execute("UPDATE slots SET status = 'empty' WHERE id = ?", (slot_id,))
                if ledger_id:
                    hold_budget_unknown(conn, batch_id, ledger_id, reason="Disk full error, holding budget for inspection")
                conn.execute("""
                    UPDATE batches 
                    SET is_circuit_broken = 1, circuit_break_reason = 'Storage disk full or critical OS error', status = 'paused', pause_reason = 'CIRCUIT_BROKEN'
                    WHERE id = ?
                """, (batch_id,))

        except Exception as e:
            # 区分：已生成原图后水印失败，生图实际已产生消耗，绝不能假定无消耗释放！
            if raw_generated:
                logger.error(f"Task {task_id} Watermark failed but raw image generated: {e}")
                with get_db(immediate=True) as conn:
                    conn.execute("UPDATE tasks SET status = 'failed', last_error = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (str(e), task_id))
                    conn.execute("UPDATE slot_versions SET process_status = 'failed', raw_image_path = ?, error_message = ? WHERE id = ?", (raw_name, f"Post-processing failed: {str(e)}", slot_version_id))
                    conn.execute("UPDATE slots SET status = 'empty', updated_at = datetime('now', 'localtime') WHERE id = ?", (slot_id,))
                    if ledger_id:
                        # 结算真实消耗，绝不释放
                        settle_budget_success(conn, batch_id, ledger_id, task_id)
            else:
                # 明确策略拒绝 (ValueError) 释放预算；其余未知异常保守作为 unknown 持有
                err_str = str(e)
                if isinstance(e, ImageApiMisconfigError):
                    # 上游配置无效（未填地址/密钥等）：明确失败释放预占，不作为未知持账
                    logger.error(f"Task {task_id} Image API misconfig: {e}")
                    with get_db(immediate=True) as conn:
                        conn.execute("UPDATE tasks SET status = 'failed', last_error = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (err_str, task_id))
                        conn.execute("UPDATE slot_versions SET process_status = 'failed', error_message = ? WHERE id = ?", (err_str, slot_version_id))
                        conn.execute("UPDATE slots SET status = 'empty', updated_at = datetime('now', 'localtime') WHERE id = ?", (slot_id,))
                        if ledger_id:
                            release_budget_failed(conn, batch_id, ledger_id, reason=f"misconfig: {err_str}")
                elif isinstance(e, ValueError) and "Policy Violation" in err_str:
                    logger.warning(f"Task {task_id} Policy Violation: {e}")
                    with get_db(immediate=True) as conn:
                        conn.execute("UPDATE tasks SET status = 'failed', last_error = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (err_str, task_id))
                        conn.execute("UPDATE slot_versions SET process_status = 'failed', error_message = ? WHERE id = ?", (err_str, slot_version_id))
                        conn.execute("UPDATE slots SET status = 'empty', updated_at = datetime('now', 'localtime') WHERE id = ?", (slot_id,))
                        if ledger_id:
                            release_budget_failed(conn, batch_id, ledger_id, reason="Policy rejected")
                else:
                    # 通用未捕获异常：保守作为 unknown 处理，暂停批次，绝不盲目假定未扣费！
                    logger.error(f"Task {task_id} Unhandled Exception (Conservatively held as unknown): {e}", exc_info=True)
                    with get_db(immediate=True) as conn:
                        conn.execute("UPDATE tasks SET status = 'unknown', last_error = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (err_str, task_id))
                        conn.execute("UPDATE slot_versions SET process_status = 'unknown_pending_confirmation', is_unknown = 1, error_message = ? WHERE id = ?", (err_str, slot_version_id))
                        if ledger_id:
                            hold_budget_unknown(conn, batch_id, ledger_id, reason=f"Unhandled error: {err_str}")
                        conn.execute("""
                            UPDATE batches 
                            SET status = 'paused', pause_reason = 'PAUSE_UNKNOWN_PENDING', updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                        """, (batch_id,))

worker_instance = TaskWorker()
