# -*- coding: utf-8 -*-
"""
上线验收脚本 (Go-Live Acceptance Runner)
=========================================
对照《酒店家具智能套图工作台详细实施计划》第十六节验收标准，在真实 uvicorn
服务器（启用真实后台 Worker 循环）上做独立全流程验收：

  1. 健康检查与 Mock 模式声明
  2. RBAC：无效角色 / 越权 / 原图隔离 / 审计 / 路径穿越
  3. 3 系列 x 2 款 = 6 款首轮候选 + 候补；系列规则库完整性；设计结构差异
  4. 全景初筛：通过锁定 / 重做受限 / 淘汰唤醒候补
  5. 重做上限：单槽 2 次 / 超限负责人特批 / 操作员不可越权
  6. 预算：20 预警 423 -> 负责人批准 -> 25 硬上限 423（批准也无法突破）
  7. 下游三图 -> 四图终审归档 x3 -> 批次完成
  8. 导出：ZIP 内容/版本绑定/水印生效/重复导出可追踪/下载重新鉴权
  9. 异常：429 技术重试单独记账；超时 -> unknown 保守持账暂停批次 -> 凭据释放
 10. 熔断：401 熔断 -> 负责人恢复试运行
 11. 服务重启：running 任务恢复（有文件自愈完成 / 无文件转 unknown 暂停批次）

用法:  python tests/acceptance/go_live_acceptance.py
产出:  控制台逐项 PASS/FAIL；storage/evidence/acceptance/acceptance_report.json
退出码: 0 = 全部通过；1 = 存在失败项
"""
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from PIL import Image

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

RESULTS = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    RESULTS.append({"item": name, "status": status, "detail": str(detail)[:300]})
    print(f"  [{status}] {name}" + (f" — {detail}" if (detail and not condition) else ""))
    return bool(condition)


def http(method, url, headers=None, body=None, timeout=15, raw=False):
    if isinstance(body, str):
        body = body.encode("utf-8")
    req = urllib.request.Request(url, method=method, data=body)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return resp.status, (data if raw else json.loads(data) if data else {})
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except Exception:
            return e.code, payload


def role_headers(role, user="acceptance"):
    return {"X-User-Role": role, "X-User-ID": user}


def wait_for(predicate, timeout=90, interval=0.5, desc="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"等待超时: {desc}")


def wait_for_ok(predicate, timeout=90, interval=0.5, desc="condition"):
    """等待真实条件成立; 超时不抛异常而是返回 False, 让 check 记录 FAIL。"""
    try:
        wait_for(predicate, timeout, interval, desc)
        return True
    except TimeoutError:
        return False


def db(storage, sql, params=(), fetch=True):
    conn = sqlite3.connect(str(storage / "db" / "workbench.db"), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    try:
        cur = conn.execute(sql, params)
        if fetch:
            return [dict(r) for r in cur.fetchall()]
        conn.commit()
        return []
    finally:
        conn.close()


class Server:
    def __init__(self, storage, port):
        self.storage = storage
        self.port = port
        self.proc = None

    def start(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT_DIR)
        env["WORKBENCH_STORAGE_DIR"] = str(self.storage)
        env.pop("WORKBENCH_DB_PATH", None)
        env.pop("WORKBENCH_DISABLE_WORKER_LOOP", None)
        self.log = (self.storage / "acceptance_server.log").open("a+b")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.app.main:app",
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            cwd=str(ROOT_DIR), env=env, stdout=self.log, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{self.port}"
        wait_for(lambda: http("GET", f"{base}/api/health")[0] == 200, 30, 0.3, "server health")
        return base

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except Exception:
                self.proc.kill()
        if hasattr(self, "log"):
            self.log.close()


def main():
    storage = Path(tempfile.mkdtemp(prefix=".acceptance-", dir=ROOT_DIR))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = Server(storage, port)

    evidence_dir = ROOT_DIR / "storage" / "evidence" / "acceptance"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 上线验收开始 (isolated storage: {storage.name}) ===")
    try:
        base = server.start()
        run_acceptance(base, storage, server)
    finally:
        server.stop()
        report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "total": len(RESULTS),
                  "passed": sum(1 for r in RESULTS if r["status"] == "PASS"),
                  "failed": [r for r in RESULTS if r["status"] == "FAIL"],
                  "items": RESULTS}
        report_path = evidence_dir / "acceptance_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(storage, ignore_errors=True)
        print(f"\n=== 验收报告已保存: {report_path} ===")

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    print(f"\n===== 验收结果: {passed}/{len(RESULTS)} 通过 =====")
    for r in failed:
        print(f"  [FAIL] {r['item']}: {r['detail']}")
    return 0 if not failed else 1


def run_acceptance(base, storage, server):
    H = lambda r: role_headers(r)

    # ---------- 1. 健康检查与 Mock 模式 ----------
    # /api/health 与静态首页是刻意公开的探测端点; 所有派生业务接口强制读取
    # X-User-Role/X-User-ID 并做角色校验, 生产环境由 nginx auth_request 注入。
    print("\n--- 1. 健康检查与 Mock 模式声明 ---")
    st, health = http("GET", f"{base}/api/health")
    check("健康检查可用", st == 200)
    check("Mock 模式明确声明", health.get("mock_mode") is True and health.get("offline_safe") is True, health)
    st, index = http("GET", base, raw=True)
    check("前端首页可加载", st == 200 and "SEEZO 视觉工作台".encode() in index)

    # ---------- 2. RBAC ----------
    print("\n--- 2. RBAC 权限与安全 ---")
    st, _ = http("POST", f"{base}/api/batches", headers=role_headers("superadmin"), body="{}")
    check("无效角色被 403 拒绝", st == 403, st)
    st, _ = http("POST", f"{base}/api/batches", headers=H("operator"),
                 body=json.dumps({"name": "OP批次", "enabled_series": ["modern_business"],
                                  "target_archived_count": 1}))
    check("操作员不能创建批次", st == 403, st)
    st, _ = http("POST", f"{base}/api/system/cleanup", headers=H("operator"),
                 body=json.dumps({"days_threshold": 30}))
    check("操作员不能触发清理接口", st == 403, st)

    # ---------- 3. 批次创建与规划 ----------
    print("\n--- 3. 批次创建: 3系列x2款=6首轮 + 候补, 系列规则, 设计差异 ---")
    st, batch = http("POST", f"{base}/api/batches", headers=H("leader"), body=json.dumps({
        "name": "上线验收-首轮小样", "enabled_series": ["modern_business", "natural_wood", "soft_contemporary"],
        "target_archived_count": 3, "designs_per_series": 2, "standby_designs_per_series": 1,
        "warning_budget": 20, "hard_limit_budget": 25, "auto_start_panorama": True}))
    check("负责人创建批次成功", st == 200, st)
    batch_id = batch["id"]

    st, rules = http("GET", f"{base}/api/system/series_rules", headers=H("operator"))
    required_rule_keys = ["headboard_types", "bed_frames", "nightstands", "wardrobe_types", "materials",
                          "color_palette", "soft_upholstery_usage", "metal_usage", "allowed_structural_changes",
                          "forbidden_elements", "forbidden_processes", "hotel_mood", "twin_adapter_rule", "white_bg_rule"]
    check("三个系列规则库完整(床屏/床体/柜体/材料/配色/双床/白底规则)", st == 200 and
          all(all(k in rules.get(sid, {}) for k in required_rule_keys) for sid in
              ["modern_business", "natural_wood", "soft_contemporary"]))

    st, budget = http("GET", f"{base}/api/batches/{batch_id}/budget", headers=H("operator"))
    check("规划模型成本单独记账 9 笔(6首发+3候补)", budget.get("planning_model_count") == 9, budget)
    check("规划成本不挤占生图额度", budget.get("image_generation_count") == 6, budget)

    st, designs = http("GET", f"{base}/api/designs/batch/{batch_id}", headers=H("operator"))
    check("6 款首发 + 3 款候补已规划", len(designs) == 9 and sum(1 for d in designs if d["is_standby"]) == 3)
    codes = {(d["series_id"], d["structured_spec"]["headboard"], d["structured_spec"]["bed_frame"],
              d["structured_spec"]["nightstand"]) for d in designs if not d["is_standby"]}
    check("6 款设计结构组合不重复(多样性)", len(codes) == 6, len(codes))
    names = {d["structured_spec"]["series_name"] for d in designs}
    check("三个系列可区分", names == {"现代商务", "自然木质", "柔和当代"}, names)
    check("非管理员不泄露原图磁盘路径",
          all(v.get("raw_image_path") is None for d in designs for s in d["slots"].values() for v in s["history_versions"]))

    # ---------- 4. 等待 6 张全景并初筛 ----------
    print("\n--- 4. 全景初筛: 通过锁定 / 重做受限 / 淘汰唤醒候补 ---")
    def panoramas_done():
        _, ds = http("GET", f"{base}/api/designs/batch/{batch_id}", headers=H("operator"))
        init = [d for d in ds if not d["is_standby"]]
        return all(d["status"] == "panorama_review" for d in init) and len(init) == 6
    check("6 张首发全景全部生成并进入人工初筛", wait_for_ok(panoramas_done, 120, 1, "6张全景生成"))
    _, designs = http("GET", f"{base}/api/designs/batch/{batch_id}", headers=H("operator"))
    initial = [d for d in designs if not d["is_standby"]]
    a, b, c, d4, d5, d6 = initial

    # 4.1 淘汰 4 款首发(c,d4,d5,d6) -> 有效组数 2(A,B) < 目标 3 -> 恰好唤醒 1 个候补
    for ref, reason in ((c, "结构错误"), (d4, "家具变形"), (d5, "相似度过高"), (d6, "主观审美不合格")):
        _, cur = http("GET", f"{base}/api/designs/{ref['id']}", headers=H("operator"))
        st, _ = http("POST", f"{base}/api/designs/{ref['id']}/review_panorama", headers=H("operator"),
                     body=json.dumps({"action": "eliminate",
                                      "expected_version_id": cur["slots"]["panorama"]["current_version"]["id"],
                                      "reason_category": reason, "detail_comment": "验收淘汰"}))
        check(f"全景淘汰成功({cur['design_code']})", st == 200, st)
    wait_for(lambda: any(d["status"] in ("panorama_generating", "panorama_review") and
                         d["design_code"].endswith("-STBY-01") for d in
                         http("GET", f"{base}/api/designs/batch/{batch_id}", headers=H("operator"))[1]),
             90, 1, "候补唤醒")
    _, designs = http("GET", f"{base}/api/designs/batch/{batch_id}", headers=H("operator"))
    woken = [d for d in designs if not d["is_standby"] and d["design_code"].endswith("-STBY-01")]
    still_standby = [d for d in designs if d["is_standby"]]
    check("淘汰缺口自动唤醒候补", len(woken) == 1, len(woken))
    check("候补不超量唤醒(其余候补仍待命)", len(still_standby) == 2, len(still_standby))
    standby_design = woken[0]

    # 4.2 A: 重做上限测试 (单槽 2 次, 第 3 次需负责人特批)
    _, designs = http("GET", f"{base}/api/designs/batch/{batch_id}", headers=H("operator"))
    a = next(d for d in designs if d["id"] == a["id"])
    pano_slot = a["slots"]["panorama"]
    vid = pano_slot["current_version"]["id"]
    for i in range(2):
        st, _ = http("POST", f"{base}/api/designs/{a['id']}/review_panorama", headers=H("operator"),
                     body=json.dumps({"action": "redo", "expected_version_id": vid, "reason_category": "构图不符",
                                      "detail_comment": f"验收重做 {i+1}"}))
        check(f"全景第 {i+1} 次重做(限内)成功", st == 200, st)
        wait_for(lambda: http("GET", f"{base}/api/designs/{a['id']}", headers=H("operator"))[1]["status"] == "panorama_review",
                 60, 1, "重做全景生成")
        _, a = http("GET", f"{base}/api/designs/{a['id']}", headers=H("operator"))
        vid = a["slots"]["panorama"]["current_version"]["id"]
    st, _ = http("POST", f"{base}/api/designs/{a['id']}/review_panorama", headers=H("operator"),
                 body=json.dumps({"action": "redo", "expected_version_id": vid, "reason_category": "主观审美不合格"}))
    check("超过单槽 2 次重做被拦截", st == 400, st)
    st, _ = http("POST", f"{base}/api/designs/{a['id']}/review_panorama", headers=role_headers("operator"),
                 body=json.dumps({"action": "redo", "expected_version_id": vid, "reason_category": "主观审美不合格",
                                  "leader_override": True}))
    check("操作员带 override 标志也不能越权重做", st == 400, st)
    st, _ = http("POST", f"{base}/api/designs/{a['id']}/review_panorama", headers=H("leader"),
                 body=json.dumps({"action": "redo", "expected_version_id": vid, "reason_category": "主观审美不合格",
                                  "leader_override": True, "detail_comment": "负责人特批验收"}))
    check("负责人特批后可继续重做", st == 200, st)
    wait_for(lambda: http("GET", f"{base}/api/designs/{a['id']}", headers=H("operator"))[1]["status"] == "panorama_review",
             60, 1, "特批重做生成")

    # 4.3 通过 A / B
    for design_ref in (a, b):
        _, dd = http("GET", f"{base}/api/designs/{design_ref['id']}", headers=H("operator"))
        st, _ = http("POST", f"{base}/api/designs/{design_ref['id']}/review_panorama", headers=H("operator"),
                     body=json.dumps({"action": "approve", "expected_version_id": dd["slots"]["panorama"]["current_version"]["id"]}))
        check(f"全景初筛通过({dd['design_code']})并锁定", st == 200, st)
        st2, _ = http("POST", f"{base}/api/designs/{design_ref['id']}/review_panorama", headers=H("operator"),
                      body=json.dumps({"action": "redo", "expected_version_id": dd["slots"]["panorama"]["current_version"]["id"],
                                       "reason_category": "构图不符"}))
        check("已锁定全景禁止再走初筛重做(须负责人替换)", st2 == 400, st2)

    # ---------- 5. 预算 20 预警 / 25 硬上限 (独立小批次) ----------
    print("\n--- 5. 预算: 20 预警 423 -> 负责人批准 -> 25 硬上限不可突破 ---")
    st, mini = http("POST", f"{base}/api/batches", headers=H("leader"), body=json.dumps({
        "name": "验收-预算边界批次", "enabled_series": ["modern_business"], "target_archived_count": 1,
        "designs_per_series": 1, "standby_designs_per_series": 0,
        "warning_budget": 2, "hard_limit_budget": 3, "auto_start_panorama": True}))
    check("小预算批次创建成功", st == 200, st)
    mini_id = mini["id"]
    wait_for(lambda: http("GET", f"{base}/api/designs/batch/{mini_id}", headers=H("operator"))[1][0]["status"] == "panorama_review",
             60, 1, "预算批次全景")
    mini_design_id = http("GET", f"{base}/api/designs/batch/{mini_id}", headers=H("operator"))[1][0]["id"]
    _, mini_design = http("GET", f"{base}/api/designs/{mini_design_id}", headers=H("operator"))
    mvid = mini_design["slots"]["panorama"]["current_version"]["id"]
    st, _ = http("POST", f"{base}/api/designs/{mini_design_id}/review_panorama", headers=H("operator"),
                 body=json.dumps({"action": "redo", "expected_version_id": mvid, "reason_category": "构图不符"}))
    check("第 2 次生图(=预警线)允许", st == 200, st)
    wait_for(lambda: http("GET", f"{base}/api/designs/{mini_design_id}", headers=H("operator"))[1]["status"] == "panorama_review",
             60, 1, "预算批次重做")
    _, mini_design = http("GET", f"{base}/api/designs/{mini_design_id}", headers=H("operator"))
    mvid = mini_design["slots"]["panorama"]["current_version"]["id"]
    st, err = http("POST", f"{base}/api/designs/{mini_design_id}/review_panorama", headers=H("operator"),
                   body=json.dumps({"action": "redo", "expected_version_id": mvid, "reason_category": "构图不符"}))
    check("超过预警线未批准 -> 423 暂停新任务", st == 423, (st, err))
    st, _ = http("POST", f"{base}/api/batches/{mini_id}/approve_warning", headers=H("leader"))
    check("负责人批准越过预警线", st == 200, st)
    st, _ = http("POST", f"{base}/api/designs/{mini_design_id}/review_panorama", headers=H("operator"),
                 body=json.dumps({"action": "redo", "expected_version_id": mvid, "reason_category": "构图不符"}))
    check("批准后可继续(未达硬上限)", st == 200, st)
    wait_for(lambda: http("GET", f"{base}/api/designs/{mini_design_id}", headers=H("operator"))[1]["status"] == "panorama_review",
             60, 1, "预算批次特批重做")
    _, mini_design = http("GET", f"{base}/api/designs/{mini_design_id}", headers=H("operator"))
    mvid = mini_design["slots"]["panorama"]["current_version"]["id"]
    st, _ = http("POST", f"{base}/api/designs/{mini_design_id}/review_panorama", headers=H("operator"),
                 body=json.dumps({"action": "redo", "expected_version_id": mvid, "reason_category": "构图不符"}))
    check("第 3 次重做先命中单槽重做上限被拦截(400)", st == 400, st)
    st, _ = http("POST", f"{base}/api/designs/{mini_design_id}/review_panorama", headers=H("leader"),
                 body=json.dumps({"action": "redo", "expected_version_id": mvid, "reason_category": "构图不符",
                                  "leader_override": True}))
    check("负责人特批越过重做上限后仍达硬上限 3 次 -> 后端硬阻断 423", st == 423, st)
    check("硬上限不可通过特批绕过(负责人再次尝试仍 423)", http("POST",
          f"{base}/api/designs/{mini_design_id}/review_panorama", headers=H("leader"),
          body=json.dumps({"action": "redo", "expected_version_id": mvid, "reason_category": "构图不符",
                           "leader_override": True}))[0] == 423)
    check("特批权仅限负责人角色(admin 带 override 被拦 400)", http("POST",
          f"{base}/api/designs/{mini_design_id}/review_panorama", headers=role_headers("admin"),
          body=json.dumps({"action": "redo", "expected_version_id": mvid, "reason_category": "构图不符",
                           "leader_override": True}))[0] == 400)

    # ---------- 6. 下游三图与四图终审归档 ----------
    print("\n--- 6. 下游三图 -> 四图终审 -> 归档 x3 -> 批次完成 ---")
    _, designs = http("GET", f"{base}/api/designs/batch/{batch_id}", headers=H("operator"))
    a = next(d for d in designs if d["id"] == a["id"])
    b = next(d for d in designs if d["id"] == b["id"])
    standby_design = next(d for d in designs if d["id"] == standby_design["id"])
    # 候补全景生成后进入初筛，人工通过
    _, sd = http("GET", f"{base}/api/designs/{standby_design['id']}", headers=H("operator"))
    wait_for(lambda: http("GET", f"{base}/api/designs/{standby_design['id']}", headers=H("operator"))[1]["status"] == "panorama_review",
             90, 1, "候补全景生成")
    _, sd = http("GET", f"{base}/api/designs/{standby_design['id']}", headers=H("operator"))
    st, _ = http("POST", f"{base}/api/designs/{standby_design['id']}/review_panorama", headers=H("operator"),
                 body=json.dumps({"action": "approve", "expected_version_id": sd["slots"]["panorama"]["current_version"]["id"]}))
    check("候补全景初筛通过", st == 200, st)

    for design in (a, b, standby_design):
        st, _ = http("POST", f"{base}/api/designs/{design['id']}/start_downstream", headers=H("operator"))
        check(f"启动下游三图({design['design_code']})", st == 200, st)
    st, _ = http("POST", f"{base}/api/designs/{a['id']}/start_downstream", headers=H("operator"))
    check("下游重复启动幂等(不重复扣预算)", st == 200, st)

    def downstream_ready(design_id):
        dd = http("GET", f"{base}/api/designs/{design_id}", headers=H("operator"))[1]
        return dd["status"] == "quad_review"
    check("三款设计四图就绪进入终审",
          wait_for_ok(lambda: all(downstream_ready(d["id"]) for d in (a, b, standby_design)), 120, 1, "三款设计下游生成"))

    for design in (a, b, standby_design):
        dd = http("GET", f"{base}/api/designs/{design['id']}", headers=H("operator"))[1]
        snapshot = {k: dd["slots"][k]["current_version"]["id"]
                    for k in ("panorama", "white_isolated", "twin_bed", "nightstand_closeup")}
        st, _ = http("POST", f"{base}/api/designs/{design['id']}/review_quad", headers=H("operator"),
                     body=json.dumps({"action": "archive", "detail_comment": "验收归档", "expected_slot_versions": snapshot}))
        check(f"四图终审归档({design['design_code']})", st == 200, st)
        st, _ = http("POST", f"{base}/api/designs/{design['id']}/review_quad", headers=H("operator"),
                     body=json.dumps({"action": "archive", "detail_comment": "重复提交", "expected_slot_versions": snapshot}))
        check("归档重复提交不重复计数(幂等)", st == 200, st)
    _, b3 = http("GET", f"{base}/api/batches/{batch_id}", headers=H("operator"))
    check("批次归档 3/3 且状态 completed", b3["current_archived_count"] == 3 and b3["status"] == "completed", b3)
    _, b4 = http("GET", f"{base}/api/batches/{batch_id}/budget", headers=H("operator"))
    check("生图预算账本与实际一致(19 次: 6全景+1候补+9下游+3重做)", b4["image_generation_count"] == 19, b4)

    # ---------- 7. 导出与水印 ----------
    print("\n--- 7. ZIP 导出 / 水印 / 重复导出追踪 / 下载鉴权 ---")
    st, exp = http("POST", f"{base}/api/export/batch/{batch_id}", headers=H("operator"))
    check("导出批次全部归档 ZIP", st == 200, exp)
    zip_name = exp.get("file_name", "")
    st, zip_bytes = http("GET", f"{base}/api/export/download/{zip_name}", headers=H("operator"), raw=True)
    check("ZIP 下载重新鉴权成功", st == 200 and len(zip_bytes) > 1000, st)
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = zf.namelist()
    check("ZIP 含 3 设计 x (4图+元数据)", len([n for n in names if n.endswith(".png")]) == 12 and
          len([n for n in names if n.endswith("METADATA.json")]) == 3, names[:5])
    check("ZIP 无路径穿越条目", all(not n.startswith("/") and ".." not in n and ":" not in n for n in names))
    # 水印生效: 导出图必须与无水印原图像素不同
    _, designs = http("GET", f"{base}/api/designs/batch/{batch_id}", headers=role_headers("admin"))
    a_admin = next(d for d in designs if d["id"] == a["id"])
    raw_vid = a_admin["slots"]["panorama"]["current_version"]["id"]
    st_raw, raw_bytes = http("GET", f"{base}/api/media/raw/{raw_vid}", headers=role_headers("admin"), raw=True)
    check("管理员可访问无水印原图", st_raw == 200, st_raw)
    raw_img = Image.open(io.BytesIO(raw_bytes))
    exported_name = f"{a_admin['design_code']}/{a_admin['design_code']}_panorama_v{a_admin['slots']['panorama']['current_version']['version_num']}_watermarked.png"
    exported_img = Image.open(io.BytesIO(zf.read(exported_name)))
    check("导出图与原图像素不同(最终水印已压制)", list(raw_img.getdata()) != list(exported_img.getdata()))
    st, _ = http("POST", f"{base}/api/export/batch/{batch_id}", headers=H("operator"))
    check("重复导出成功", st == 200, st)
    check("重复导出可追踪(export_records 2 条)",
          len(db(storage, "SELECT * FROM export_records WHERE batch_id = ?", (batch_id,))) == 2)
    st, exp_records = http("GET", f"{base}/api/export/records/{batch_id}", headers=H("operator"))
    check("导出记录接口可查询(前端导出记录区数据源)", st == 200 and len(exp_records) == 2,
          (st, len(exp_records) if isinstance(exp_records, list) else exp_records))
    st, _ = http("GET", f"{base}/api/media/raw/{raw_vid}", headers=H("operator"), raw=True)
    check("操作员访问原图 403", st == 403, st)
    st, _ = http("GET", f"{base}/api/media/raw/{raw_vid}", headers=H("leader"), raw=True)
    check("负责人访问原图 403", st == 403, st)
    st, _ = http("GET", f"{base}/api/export/download/..%2F..%2Fdb%2Fworkbench.db.zip", headers=H("operator"), raw=True)
    check("下载路径穿越被阻断", st in (400, 404), st)
    check("原图访问已写审计",
          len(db(storage, "SELECT * FROM audit_logs WHERE action = 'access_raw_image'")) >= 1)

    # ---------- 8. 异常: 429 技术重试 / 超时 unknown / 凭据释放 ----------
    print("\n--- 8. 异常分类: 429 技术重试 / 超时 unknown / 负责人凭据释放 ---")
    st, fault = http("POST", f"{base}/api/batches", headers=H("leader"), body=json.dumps({
        "name": "验收-异常分类批次", "enabled_series": ["modern_business"], "target_archived_count": 1,
        "designs_per_series": 2, "standby_designs_per_series": 0,
        "warning_budget": 20, "hard_limit_budget": 25, "auto_start_panorama": True}))
    fault_id = fault["id"]
    wait_for(lambda: len(http("GET", f"{base}/api/designs/batch/{fault_id}", headers=H("operator"))[1]) == 2, 30, 1, "异常批次规划")
    # 直接在账本中标记一个 429 技术重试流水 (对应 worker 真实写入格式)
    db(storage, """INSERT INTO budget_ledger (id, batch_id, category, delta_unsettled, status, ledger_event_type,
                   reference_task_id, note) VALUES ('acc_retry_1', ?, 'image_generation', 1, 'released',
                   'technical_retry', 'acc_task_none', 'Technical 429 retry backoff')""", (fault_id,), fetch=False)
    _, fb = http("GET", f"{base}/api/batches/{fault_id}/budget", headers=H("operator"))
    check("429 技术重试单独计数(不混入明确失败)", fb["technical_retry_count"] == 1 and fb["explicit_failure_count"] == 0, fb)

    # 超时 -> unknown: 用 SQL 注入故障标记到排队任务 (验收用受控故障注入)
    fd = http("GET", f"{base}/api/designs/batch/{fault_id}", headers=H("operator"))[1]
    fd2 = fd[1]
    task = db(storage, """SELECT t.id, sv.id AS vid FROM tasks t JOIN slot_versions sv ON t.slot_version_id = sv.id
                 WHERE t.design_id = ? AND t.status = 'queued'""", (fd2["id"],))
    if task:
        db(storage, "UPDATE slot_versions SET prompt = prompt || ' __INJECT_TIMEOUT__' WHERE id = ?",
           (task[0]["vid"],), fetch=False)
    else:
        check("存在可注入超时的排队任务", False)
    wait_for(lambda: db(storage, "SELECT status FROM tasks WHERE id = ?", (task[0]["id"],))[0]["status"] == "unknown",
             60, 1, "超时任务转 unknown")
    _, fb2 = http("GET", f"{base}/api/batches/{fault_id}", headers=H("operator"))
    check("结果未知后批次暂停(PAUSE_UNKNOWN_PENDING)", fb2["pause_reason"] == "PAUSE_UNKNOWN_PENDING", fb2)
    _, fb3 = http("GET", f"{base}/api/batches/{fault_id}/budget", headers=H("operator"))
    check("unknown 保守计入预算(unknown_held)", fb3["unknown_count"] >= 1, fb3)
    st, _ = http("GET", f"{base}/api/system/batches/{fault_id}/unknown_tasks", headers=H("operator"))
    check("操作员不能查看管理员保护中心", st == 403, st)
    st, ut = http("GET", f"{base}/api/system/batches/{fault_id}/unknown_tasks", headers=H("leader"))
    check("unknown 任务出现在保护中心(无重复行)", st == 200 and len(ut) == 1, ut)
    st, _ = http("POST", f"{base}/api/system/resolve_unknown", headers=H("operator"),
                 body=json.dumps({"task_id": task[0]["id"], "resolution": "confirm_failure", "evidence": "x",
                                  "billing_reference": "BILL-001", "attests_no_charge": True}))
    check("操作员不能处置 unknown", st == 403, st)
    st, _ = http("POST", f"{base}/api/system/resolve_unknown", headers=H("leader"),
                 body=json.dumps({"task_id": task[0]["id"], "resolution": "confirm_failure",
                                  "evidence": "人工核对上游账单流水无此笔扣费",
                                  "billing_reference": "BILL-ACC-20261030-001", "attests_no_charge": True}))
    check("负责人凭据核账后释放", st == 200, st)
    check("unknown 全部处置后批次自动恢复 active",
          wait_for_ok(lambda: http("GET", f"{base}/api/batches/{fault_id}", headers=H("operator"))[1]["status"] == "active",
                      30, 1, "批次自愈恢复"))
    ledger = db(storage, "SELECT status FROM budget_ledger WHERE reference_task_id = ? AND status != 'released'",
                (task[0]["id"],))
    check("unknown 处置后预占账本已释放", all(r["status"] == "released" for r in ledger) or not ledger, ledger)

    # ---------- 9. 熔断与恢复 ----------
    print("\n--- 9. 熔断: 401 熔断 -> 负责人恢复试运行 ---")
    # 确定性注入: 直接插入带 __INJECT_401__ 标记的排队任务(等价于一次重做派发)，
    # 避免 API 创建任务后与 Worker 抢时序。
    fd_all = http("GET", f"{base}/api/designs/batch/{fault_id}", headers=H("operator"))[1]
    victim = fd_all[0]
    _, vcur = http("GET", f"{base}/api/designs/{victim['id']}", headers=H("operator"))
    vslot = vcur["slots"]["panorama"]
    vt_id, vv_id, vs_id = f"tsk_acc401_{int(time.time())}", f"ver_acc401_{int(time.time())}", vslot["id"]
    db(storage, """INSERT INTO slot_versions (id, slot_id, design_id, version_num, attempt_num, prompt,
                   process_status, template_version)
        VALUES (?, ?, ?, 99, 99, 'INJECT_401_TEST __INJECT_401__', 'pending', 'tpl_acc')""",
       (vv_id, vs_id, victim["id"]), fetch=False)
    db(storage, """INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status, idempotency_key)
        VALUES (?, ?, ?, ?, ?, 'redo_slot', 'queued', ?)""",
       (vt_id, fault_id, victim["id"], vs_id, vv_id, f"acc401_{vt_id}"), fetch=False)
    db(storage, "UPDATE slots SET status = 'generating', current_version_id = ? WHERE id = ?", (vv_id, vs_id), fetch=False)
    check("401 鉴权失败触发批次熔断",
          wait_for_ok(lambda: http("GET", f"{base}/api/batches/{fault_id}", headers=H("operator"))[1]["is_circuit_broken"] == 1,
                      60, 1, "401 熔断"))
    st, _ = http("POST", f"{base}/api/batches/{fault_id}/circuit_break_recover", headers=H("operator"))
    check("操作员不能恢复熔断", st == 403, st)
    st, rec = http("POST", f"{base}/api/batches/{fault_id}/circuit_break_recover", headers=H("leader"))
    check("负责人恢复熔断并开启 3 次试运行", st == 200 and rec.get("trial_run_remaining") == 3, rec)

    # ---------- 10. 服务重启恢复 ----------
    print("\n--- 10. 服务重启: running 任务恢复 / 不盲目重跑 ---")
    st, rb = http("POST", f"{base}/api/batches", headers=H("leader"), body=json.dumps({
        "name": "验收-重启恢复批次", "enabled_series": ["modern_business"], "target_archived_count": 1,
        "designs_per_series": 1, "standby_designs_per_series": 0,
        "warning_budget": 20, "hard_limit_budget": 25, "auto_start_panorama": False}))
    restart_id = rb["id"]
    # 种子两个 running 任务: 一个已有完整落盘文件(应自愈), 一个无文件(应转 unknown)
    _, _, _ = seed_restart_cases(storage, restart_id)
    server.stop()
    server.start()
    base = f"http://127.0.0.1:{server.port}"

    check("重启后: 本地已有完整文件的 running 任务自愈为 completed",
          wait_for_ok(lambda: db(storage, "SELECT status FROM tasks WHERE id = 'acc_run_valid'")[0]["status"] == "completed",
                      30, 1, "重启自愈"))
    t2 = db(storage, "SELECT status FROM tasks WHERE id = 'acc_run_lost'")[0]["status"]
    check("重启后: 无文件 running 任务转 unknown(不盲目重发)", t2 == "unknown", t2)
    rbatch = db(storage, "SELECT status, pause_reason FROM batches WHERE id = ?", (restart_id,))[0]
    check("重启后: 含 unknown 的批次被保护暂停", rbatch["pause_reason"] == "PAUSE_UNKNOWN_PENDING", rbatch)
    st, restart_unknown = http("GET", f"{base}/api/system/batches/{restart_id}/unknown_tasks", headers=H("leader"))
    check("重启后 unknown 任务进入保护中心等待负责人核账", st == 200 and len(restart_unknown) == 1,
          (st, len(restart_unknown) if isinstance(restart_unknown, list) else restart_unknown))

    # ---------- 11. 清理保护 ----------
    print("\n--- 11. 30 天清理保护 ---")
    st, _ = http("POST", f"{base}/api/system/cleanup", headers=H("leader"), body=json.dumps({"days_threshold": 30}))
    check("旧负责人身份具有管理员清理权限", st == 200, st)
    st, cl = http("POST", f"{base}/api/system/cleanup", headers=role_headers("admin"), body=json.dumps({"days_threshold": 30}))
    check("管理员执行受保护清理成功", st == 200 and "protected_version_count" in cl.get("stats", {}), cl)
    check("清理不删除归档版本(数量守恒)",
          len(db(storage, """SELECT sv.id FROM slot_versions sv JOIN designs d ON sv.design_id = d.id
              JOIN slots s ON s.id = sv.slot_id WHERE d.status='archived' AND s.current_version_id = sv.id""")) == 12)


def seed_restart_cases(storage, batch_id):
    """构造两个 running 任务: 有文件/无文件。返回 (valid_task, lost_task)。"""
    import uuid as _uuid
    from PIL import Image as _Image
    raw_dir = storage / "raw_images"
    prev_dir = storage / "preview_images"
    did, sid = f"des_acc_{_uuid.uuid4().hex[:6]}", f"slt_acc_{_uuid.uuid4().hex[:6]}"
    vid = f"ver_acc_{_uuid.uuid4().hex[:6]}"
    db(storage, """INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec, status)
        VALUES (?, ?, 'modern_business', 'ACC-RESTART', '{}', 'downstream_generating')""", (did, batch_id), fetch=False)
    db(storage, "INSERT INTO slots (id, design_id, slot_type, current_version_id, status) VALUES (?, ?, 'panorama', ?, 'generating')",
       (sid, did, vid), fetch=False)
    db(storage, """INSERT INTO slot_versions (id, slot_id, design_id, prompt, process_status)
        VALUES (?, ?, ?, 'restart case', 'processing')""", (vid, sid, did), fetch=False)
    db(storage, """INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status)
        VALUES ('acc_run_valid', ?, ?, ?, ?, 'generate_panorama', 'running')""", (batch_id, did, sid, vid), fetch=False)
    db(storage, """INSERT INTO budget_ledger (id, batch_id, category, delta_reserved, status, reference_task_id, note)
        VALUES ('acc_led_valid', ?, 'image_generation', 1, 'reserved', 'acc_run_valid', 'restart valid')""", (batch_id,), fetch=False)
    # 有效落盘文件（写入隔离临时 storage，服务进程读取同一目录）
    raw_name, prev_name = "acc_restart_valid_raw.png", "acc_restart_valid_preview.jpg"
    _Image.new("RGB", (256, 256), (200, 180, 160)).save(raw_dir / raw_name, format="PNG")
    _Image.new("RGB", (256, 256), (200, 180, 160)).save(prev_dir / prev_name, format="JPEG")
    db(storage, "UPDATE slot_versions SET raw_image_path = ?, preview_image_path = ? WHERE id = ?",
       (raw_name, prev_name, vid), fetch=False)
    # 无文件 running 任务
    did2, sid2, vid2 = f"des_acc_{_uuid.uuid4().hex[:6]}", f"slt_acc_{_uuid.uuid4().hex[:6]}", f"ver_acc_{_uuid.uuid4().hex[:6]}"
    db(storage, """INSERT INTO designs (id, batch_id, series_id, design_code, structured_spec, status)
        VALUES (?, ?, 'modern_business', 'ACC-RESTART2', '{}', 'downstream_generating')""", (did2, batch_id), fetch=False)
    db(storage, "INSERT INTO slots (id, design_id, slot_type, current_version_id, status) VALUES (?, ?, 'panorama', ?, 'generating')",
       (sid2, did2, vid2), fetch=False)
    db(storage, """INSERT INTO slot_versions (id, slot_id, design_id, prompt, process_status)
        VALUES (?, ?, ?, 'restart lost case', 'processing')""", (vid2, sid2, did2), fetch=False)
    db(storage, """INSERT INTO tasks (id, batch_id, design_id, slot_id, slot_version_id, task_type, status)
        VALUES ('acc_run_lost', ?, ?, ?, ?, 'generate_panorama', 'running')""", (batch_id, did2, sid2, vid2), fetch=False)
    db(storage, """INSERT INTO budget_ledger (id, batch_id, category, delta_reserved, status, reference_task_id, note)
        VALUES ('acc_led_lost', ?, 'image_generation', 1, 'reserved', 'acc_run_lost', 'restart lost')""", (batch_id,), fetch=False)
    return vid, vid2, None


if __name__ == "__main__":
    sys.exit(main())
