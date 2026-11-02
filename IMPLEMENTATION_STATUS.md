# 酒店家具智能套图工作台实施进展报告 (IMPLEMENTATION_STATUS)

## 2026-10-30 最终上线验收（主验收代理独立执行）

### 验收方法
- 逐节对照实施计划（一至十八节）完成代码级审计 + 全量自动化回归 + 真实 uvicorn 服务器独立验收脚本。
- 新增 `tests/test_final_acceptance_defects.py`（先复现后修复的回归测试）与
  `tests/acceptance/go_live_acceptance.py`（真实服务器 84 项上线验收，启用真实 Worker 循环）。

### 本轮审计发现并已修复的缺陷（均有复现测试）
1. 前端全景初筛区渲染过滤器与角标不一致：已进入四图阶段的设计会重新出现在初筛区并带"重抽/淘汰"按钮（app.js）。
2. `start_downstream` 硬编码 version_num=1：全景替换后重启下游会产生重复版本号（designs.py）。
3. 淘汰设计时若候补唤醒触发预算预警/硬上限/熔断防护，整个淘汰事务被回滚（423/500），人工决策丢失；
   现改为淘汰提交、候补唤醒推迟并记录审计，负责人批准预警后自动补做唤醒（designs.py / batches.py）。
4. 熔断批次下淘汰/启动下游/重做抛 ValueError → HTTP 500；现改为干净 409（designs.py / slots.py）。
5. 创建批次传入未知/重复系列 → 500；现改为 400（batches.py）。
6. unknown 任务列表在存在 429 技术重试流水时重复显示，且 ledger JOIN 可能选中错误流水；
   现按 reservation/unknown_held 状态精确锚定（system.py）。
7. 全景通过锁定后，操作员仍可通过初筛重做/槽位重做绕过负责人替换管控并使设计卡死；
   现锁定后重做一律 400，唯一换图入口为负责人 `/replace_panorama`（designs.py / slots.py）。
8. `GET /api/batches`、批次详情、预算三个只读接口缺少鉴权依赖，违反"所有派生接口均有权限保护"（batches.py）。
9. 新增 `WORKBENCH_AUTH_MODE=trusted_proxy` 生产身份模式：缺失身份头一律 401，
   落实计划第十五节"后端不信任客户端自传身份"；local_sim 保持本地模拟行为（security.py）。
10. 计划第十二节界面清单补齐"导出记录"：新增 `GET /api/export/records/{batch_id}` 与前端导出记录区。

### 最终回归证据
- `pytest -q`：55 passed（含 10 项本轮新增回归）。
- `node --check static/app.js`：passed。
- Playwright 受控断外网 E2E：passed（16 张截图 + last_server.log 保留于 storage/evidence/）。
- `tests/acceptance/go_live_acceptance.py`：84/84 通过，报告在
  `storage/evidence/acceptance/acceptance_report.json`。覆盖：健康/Mock 声明、RBAC 与路径穿越、
  3 系列×2 款=6 候选+候补与设计多样性、初筛通过/重做上限/负责人特批、淘汰唤醒候补（不超量）、
  预警 423→批准→硬上限 423 不可绕过、下游与四图归档×3、批次完成、ZIP 导出（水印像素级核验、
  重复导出追踪、下载重鉴权）、429 分账、超时 unknown→凭据释放、401 熔断→试运行恢复、
  服务重启自愈/不盲目重跑、30 天清理保护。

### 第一阶段上线结论
本地离线 Mock 闭环按计划第一至十二、十六节要求验收通过，第一阶段达到可上线状态。
第二阶段（90 契约核验）、第三阶段（SSH 隧道）、第四阶段（真实小样）需按计划另行受控推进；
第十四节部署模板已生成于 `deployment/`，生产身份模式已内建但默认保持 local_sim。

## 2026-10-30 失败与技术重试分类修正

1. 复现：原 SQL 按 `category,status` 聚合但选取任意 `note`，同一组 released 只按一条 note 判断；3 明确失败 + 2 个 429 会被错误计成 1/1。新增真实账本测试 `test_summary_counts_multiple_explicit_retries_and_failures_individually` 复现该混合记录场景。
2. 修正：新增 `budget_ledger.ledger_event_type`；新预写 `generation_request`，429 写 `technical_retry`，普通释放迁移为 `explicit_failure`。summary 改为按 `ledger_event_type` 分组并累加 `COUNT(*)`。
3. 既有流水兼容：初始化仅对 Worker 已写入的精确 `'Technical 429 retry backoff'` 回填 `technical_retry`，其他非空 released note 回填 `explicit_failure`；未知旧 note 默认明确失败，不再混淆计数。未改变生图额度语义，额度仍只统计 `image_generation` 的 settled/reserved/unknown_held。
4. UI 分开展示：新增“技术重试 (429)”卡片，绑定 `technical_retry_count`；“失败释放额度”绑定 `explicit_failure_count` / 兼容 `failed_retry_count`。
5. 验证：目标分类测试 passed；完整隔离 `pytest -q` 45 passed；`node --check static/app.js` passed；受控断外网 E2E passed，核心链路、手动启动、可访问名与 ZIP 落盘均通过。

## 2026-10-30 主代理验收缺陷修复与逐项证据

### 复现与修复
1. 规划成本挤占生图限额：修复前 6 笔 `planning_model` 使 `used_count=6`。已将 `used_count/reserved_count/unknown_count/image_generation_count/warning/hard` 全部限定为 `image_generation`；`planning_model/vision_model` 单独统计；`reserve_budget` 拒绝非生图分类。并发接口测试用 6 款真实设计、6 规划成本、6 视觉成本后并发启动下游：4 组成功、12 个生图任务、12 笔生图预占，另 2 组被 20 预警 423 阻断。另测负责人批准后 7 组并发：6 组成功、18 个下游任务，总额到 25 硬限，第 7 组 423 阻断。
2. 全景通过校验：`approve` 现在校验设计 `panorama_review`、槽位 `ready`、版本存在且属于槽位、`completed`、非 unknown/invalidated；原图和预览必须物理存在、Pillow 可解码，并对原图实际执行最终水印生成。生成中/失败/空/伪造版本阻断，重复通过 409，前端并发快照与 `BEGIN IMMEDIATE` 防静默覆盖。
3. 规则快照：新增 `batches.approved_series_rules_json` 和 `approved_rule_snapshot_status`。新批次创建时冻结完整规则与版本；追加候补、手动启动、唤醒规划只使用批次快照。旧库迁移为 `legacy_unfrozen`，禁止静默追加/启动；负责人可通过专用接口显式迁移为 `explicit_legacy_migration`，审计记录版本。
4. 手动启动：新增 leader/admin `POST /api/batches/{id}/start`；`auto_start_panorama=false` 创建为 `AWAITING_LEADER_START`。接口校验快照、未知任务、熔断和已有首发任务；事务防重复派发，操作员 403。前端增加负责人启动入口。
5. 结构化规则/提示/Mock特征：系列规则补齐软包范围、金属范围、允许结构变化、禁用工艺、酒店气质等字段；所有生图版本持久化结构化 JSON 提示、`prompt_metadata`（Mock/离线/槽位目标/模板版本/依赖）和 `mock_visual_features`（生成器、材质、配色、Mock 标签）。
6. 可访问性基础：关键批次、规则、水印、预算、unknown、重做控件补 `aria-label`；E2E 断言当前 DOM 图片均有 alt、按钮均有可读名称、控件均有 aria-label 或 label 关联。这不是完整 WCAG 审计。

### 最终验证
- 完整隔离 `pytest -q`：43 passed。
- `node --check static/app.js`：passed。
- 受控断外网 E2E：passed。Playwright 拦截所有非本机请求；无外部请求、无本地资源失败、本地样式加载；覆盖 1440/390 响应式、全景审核、下游、四图快照归档、unknown 凭据、熔断恢复、ZIP 落盘、负责人手动启动、基础可访问名检查。
- E2E 截图与日志保留：`storage/evidence/01` 至 `16` 加 `last_server.log`，既有用户截图未被删除。

### 本地阶段逐项证据与缺口
- 结构化系列规则：已实现上述字段并有 JSON/持久化测试；但规则历史版本 API 未做，只有当前快照、审计和设计版本冻结。
- 提示词与 Mock 特征：已持久化并可通过 API 核验；机器只能验证结构、标签和图像解码，不能验证家具审美、结构真实合理性或商业可交付性。
- 候补唤醒：自动化覆盖淘汰后按缺口唤醒、目标满不唤醒、重复唤醒防护；追加候补有快照测试。尚未覆盖多负责人同时淘汰同一首发的极端并发。
- 并发审核：全景重复/并发通过由写事务和状态机测试；四图终审有版本快照 409 测试。尚未构造多线程同时提交四图归档的数据库级竞态用例。
- 预算：20/25 生图口径已用真实 API 并发验证；规划/视觉分离已验证。技术 429 与明确失败有计数测试，但未模拟同批混合 429、超时、401 同时到达的复杂时序。
- 未知结果：unknown 保守持账、暂停、重启、凭据释放已测；无真实账单系统，未扣费只能负责人人工核账声明。
- 水印/导出：归档与导出验证可解码和最终水印生成、ZIP 原子落盘；无 OCR/感知哈希验证水印视觉效果。
- 清理：时间阈值和保护路径有测试；未做长期多版本、并发清理与生成回写竞争的压力矩阵。
- 可访问性：可访问名和响应式截图通过；未做键盘路径、焦点顺序、屏幕阅读器、对比度完整 WCAG 审计。
- 前端样式：本地 Tailwind 子集断网可用；不是完整 Tailwind 发布包，新增类需同步维护。
- 生产项：远程 90 契约、SSH 隧道、真实密钥、真实小样、门户/JWT、nginx/systemd、备份回滚均未实现。

## 2026-10-30 第五轮并发稳定性与离线资源修正

1. 幂等不稳定根因：pytest 复用会话级存储，上一用例留下的 429 重试任务会被后续用例的 Worker 领取，后台循环又与手工领取并发结算，导致 `reserved_count` 从 3 变 0。这不是重复派发缺陷。
2. 测试产品级隔离：`WORKBENCH_DISABLE_WORKER_LOOP=1` 时保留重启恢复和手工 `process_next_task` 代码路径，但不启动后台循环；每个 API 用例开始清空批次图，防止上一用例故障任务跨用例抢占。生产与浏览器 E2E 仍启用真实后台循环。
3. 幂等断言改为强校验：第二次启动前后比较具体任务 ID/状态映射和具体账本 ID/状态映射；生图账本只统计 `image_generation` 的 `reserved + settled + unknown_held` 总额，第一次与第二次都必须为 4（1 张已完成全景 + 3 个预占下游），且仍只有 3 个下游任务。规划/视觉不再混入该口径。
4. 本地样式：删除 Tailwind CDN 与 Google Fonts 远程 `@import`，新增 `static/tailwind-lite.css` 本地子集，字体改为本地系统栈；保留白灰蓝紫设计。移除首页静态资源中的所有 `https://` 引用。
5. 浏览器受控断外网验证：Playwright 路由拦截并阻断所有非 `127.0.0.1/localhost` 请求，断言无外部请求、无本地请求失败、本地样式已加载；覆盖 1440px 与 390px 首页/全景响应式，继续完成全景、下游、快照归档、unknown 核账、熔断恢复和 ZIP 落盘。
6. 复跑：目标测试连续 3 次 passed；完整隔离 `pytest -q` 37 passed；`node --check static/app.js` passed；隔离 Playwright E2E exit 0，ZIP 实际生成。14 张截图和服务器日志保留在 `storage/evidence/`。

剩余问题：本地样式是覆盖当前页面的手工子集，不是完整 Tailwind 发布包，后续新增类必须同步补 CSS；Tailwind CDN 历史动态类依赖无法完全保证；unknown 真实账单核验、远程契约、隧道、真实小样、生产认证、部署和备份仍未实现。

## 2026-10-30 第四轮审查缺陷修复记录

1. 前端空库已删除 `createDefaultInitialBatch` 自动播种链路；不再以硬编码 leader 创建批次或自动开启全景。空库仅提示由负责人手动配置并显式批准预算；浏览器演练继续使用脚本预置数据。
2. 四图终审改为 `BEGIN IMMEDIATE` 内的人工所见版本快照校验。前端提交四个槽位 `expected_slot_versions`；服务端校验快照一致、版本归属、全景 approved、设计 `quad_review`、槽位状态、`completed`、非 unknown、非 invalidated、依赖全景一致和无未处置 unknown 任务。
3. 归档前重新核验原图可解码，并实际执行最终水印生成校验；预览图也核验物理存在和可解码。快照变更返回 409，不会静默换图归档。
4. Worker 回写不再使用领取时 `is_invalidated` 快照；在写事务中实时读取版本失效标记和全景依赖。全景替换后又切回原全景时，已标记失效的下游仍保持失效，不覆盖槽位。
5. 新增真实数据回归覆盖终审快照、处理状态、unknown、槽位状态、非 `quad_review`、预览损坏和 Worker 过时快照竞态。
6. 复跑：隔离 `pytest -q` 37 passed；`node --check static/app.js` 通过；隔离 Playwright 流程退出码 0，覆盖全景、下游、快照归档、unknown 核账、熔断恢复和 ZIP 落盘。12 张截图与 `last_server.log` 保留于 `storage/evidence/`。

仍保留的缺口：前端角色头仍为本地模拟信任；Tailwind CDN 离线样式未解决；unknown 未扣费仍是人工核账声明；审美与家具结构仍需人工验收；远程契约、隧道、真实小样、生产认证、部署和备份未实现。

## 2026-10-30 浏览器验收阻塞修复记录

- 已复现：当前 Windows 沙箱在系统临时目录建数据库时拒绝访问；本机 socketpair 初始化也可能挂起。浏览器验收需允许本机通信。
- 已复现：脚本调用不存在的 `page.expect_dialog()`，在未知任务核账步骤抛出 `AttributeError`。现改为 `page.once("dialog", ...)` 处理原生提示框。
- 测试存储改为项目内独立 `.e2e-run-*` 目录，清除继承的 `WORKBENCH_DB_PATH`，避免父子进程使用不同数据库。结束后删除本次测试数据。
- 服务输出改写文件，避免无人读取的 stdout/stderr 管道积满；末尾日志保存在 `storage/evidence/last_server.log`。
- 前端对于相同批次、相同未知任务列表不重建表单，保留正在填写的核账凭据和勾选状态。
- 实测：`python tests_e2e/test_browser_flow.py` 完整通过，退出码 0；额外断言手动刷新及 3.5 秒轮询后输入仍保留。数据库确认 unknown 任务变为 failed、账本 released、核账审计存在；熔断恢复后 trial_run_remaining=3；最终 ZIP 实际生成（178830 字节）。截图保存在 `storage/evidence/01_...` 至 `12_...`。
- 本次结论仅针对上述浏览器链路，不表示原计划全部要求已验收。页面仍引用 Tailwind CDN，离线样式依赖未在本次修复中解决。

**实施代理角色**: 酒店家具工作台实施代理（主代理监管验收）  
**当前状态**: Codex 已完成第六轮失败/重试分类修正；45/45 项隔离 pytest 通过，受控断外网 E2E 通过并保留 16 张截图。第一阶段仍不判定 100% 完成；逐项缺口见“本地阶段逐项证据与缺口”。  
**接手时间**: 2026-10-30  
**当前接手审查**: 不访问 90 服务器、不读取真实密钥、不回滚他人变更；继续本地离线 Mock。  

## 〇、接手发现与工作项

已确认的实质缺口 / 修正中事项：
1. 导出 ZIP 目前在目标文件上直接写入；水印或文件校验失败可能留下部分 ZIP，缺少临时文件成功后原子发布。
2. unknown `confirm_failure` 仍是字符串长度/关键词式判断；本地无上游账单接口，只能核验负责人显式核账凭据与人工确认标记，不能宣称机器核验了真实未扣费。
3. 前端对失败释放额度没有证据/核账确认输入，会必然被后端拒绝；熔断恢复接口没有前端入口。
4. 系列规则 API 只改内存对象，重启即丢失；新规则是否进入后续规划、既有设计模板是否冻结需要版本化模型与回归测试。
5. Worker 过时回写判断过度依赖槽位 `generating` 状态；版本指针已变更但槽位状态变化时仍可能覆盖。
6. 自动化测试共享生产 SQLite 和图片目录，既有状态会影响结果；本次先增加会话级存储隔离。
7. 2026-10-30 复跑时宿主沙箱对原 `storage/db/workbench.db` 写事务返回 readonly，24 项测试因环境不可写失败；该现象已在测试会话中用独立临时存储隔离，后续以最终复跑为准。

本节后续按完成情况持续更新；历史报告中的测试结果保留为接手前证据，不作为当前验收结论。

### 2026-10-30 第三轮修正与核验结果

已实现并已机器核验：
1. pytest 使用会话级临时 SQLite/图片目录，不再共享既有生产库；接手首跑 readonly 环境已隔离解决。
2. 系列规则持久化并生成新模板版本；新建/候补/重做/下游版本继承设计冻结模板，旧设计不被后续规则改写；候补追加也记录规划模型流水。
3. Worker 过时回写改为版本指针权威判断；全景替换后，在途下游任务会结算消耗但标记依赖失效，不再复活旧下游。
4. 归档前核验四槽位原图与预览图物理存在并可解码；水印 ZIP 改为临时文件成功后原子发布，失败清理临时件且不发布部分 ZIP。
5. unknown 释放需要负责人填写核账依据、凭据编号并显式确认无扣费；审计记录 `human_ledger_attestation`，本地无法机器验证真实账单。
6. 恢复熔断进入真实试运行模式，最多放行 3 个任务；额度用尽后重新阻断，不再只计数不限制。
7. 前端补齐熔断恢复、系列规则模板编辑、unknown 核账凭据、规划/视觉/未结算指标、负责人全景替换、带身份头 ZIP 下载和日常批次显式预算校验；日常目标不默认套用小样 25 上限。

最终复跑证据：
- `pytest -q`：34 passed。
- `python tests_e2e/test_browser_flow.py`：隔离 Edge/Playwright 流程通过；覆盖首页、全景审核、下游、归档、系统规则 UI、unknown 凭据释放、熔断恢复、真实 ZIP 落盘；脚本后清理临时目录。
- 前端 `node --check static/app.js`：通过。

仍如实保留的边界/缺口：
1. unknown 未扣费仍是负责人人工核账声明，本地阶段没有真实账单系统可机器核验。
2. 测试可验证图像文件可解码、水印层和状态/预算不变，不能机器判断家具审美、结构合理性或商业可交付性。
3. 角色头仍为本地模拟信任模型；生产门户/JWT、nginx、隧道、真实契约、真实小样、部署和备份均未实现。
4. 系列规则修订审计可追踪，但没有独立历史版本 API；只保证当前规则和新旧设计版本冻结。
5. 浏览器流程验证可操作性和落盘 ZIP，截图为临时证据并在脚本结束后清理，没有保留长期截图归档。
6. 导出原子性覆盖本地文件系统场景；尚未涉及分布式对象存储或异机备份。

---
**运行模式声明**: 本阶段严格处于**本地离线 Mock 闭环模式**，未连接 90 服务器，未读取或访问真实密钥，所有图像均为本地生成的家具视觉原型。

---

## 一、主代理两轮代码审查修复对照表

| 审查缺陷项 | 缺陷根因描述 | 代码落地与机制强化 | 对应自动化测试函数 |
| :--- | :--- | :--- | :--- |
| **1. 30天清理时间与删除保护** | `cleanup_service` 未使用 `days_threshold`，旧版立即误删，删除失败清空路径 | 引入 `datetime(created_at) <= datetime('now', 'localtime', '-N days')` 时间窗口筛选；仅当磁盘文件真实被成功删除才置空数据库路径，删除异常保留路径。 | `test_cleanup_days_threshold_and_protection` |
| **2. 状态机约束与防回写覆盖** | `select_version` 可直接偷换全景绕过负责人与下游失效；`redo` 未检查设计终态；Worker 回写未防淘汰复活与过期覆盖 | `select_version` 严禁直接回选全景（强制引导走 `/replace_panorama`），下游回选严格校验全景依赖版本；`redo` 阻断已归档/已淘汰设计；Worker 回写在写事务中核验设计终态（已淘汰/已归档作废成果，不覆盖状态）和槽位当前版本（过期任务不覆盖新指针）。 | `test_state_constraints_and_no_stale_overwrites` |
| **3. 结果未知处置鉴真与证据** | `resolve_unknown` 未验证任务确实 unknown；`confirm_success` 无图像解码校验；`confirm_failure` 无凭据随意释放 | 严格前置校验 `tasks.status == 'unknown'` 与 `slot_versions.process_status == 'unknown_pending_confirmation'`；`confirm_success` 强制使用 Pillow 解码核验原图与预览图物理文件；`confirm_failure` 强制校验未扣费证据凭据，无法确认时保持 `unknown_held` 占用。 | `test_resolve_unknown_validations` |
| **4. 批次暂停与重启自愈** | timeout 和 recover 未暂停批次；领取未检查批次暂停/熔断/unknown；重启未 hold 账本，未核验本地结果 | 超时或未知中断自动将批次置为 `paused`，结构化 `pause_reason = 'PAUSE_UNKNOWN_PENDING'`；任务领取严格校验 `b.status == 'active' AND b.is_circuit_broken == 0` 且无 unknown 挂起任务；服务启动扫描中断任务：若本地已有完整合法图像则直接自愈完成并结算，若无则标记 unknown、hold 账本并暂停批次。 | `test_fault_injections_and_circuit_breaker`, `test_connection_reset_is_unknown_not_429` |
| **5. 故障分类与技术重试分离** | 通用 Exception 直接释放预算分类不可靠，`ConnectionResetError` 误判为 429，原图生成后水印失败假定无消耗直接释放 | 严格区分错误类型：401/403 明确拒绝并熔断批次；显式 429 累加 `technical_retry_count`（有界 3 次技术重试，**不消耗人工重做次数**），超限熔断；真实连接断开属于未知中断，转入 unknown 并 hold 预算；原图已生成后水印失败，生图实际已产生消耗，严禁释放，结算消耗并保留原图供排查。 | `test_post_process_watermark_failure_keeps_budget_settled`, `test_connection_reset_is_unknown_not_429`, `test_fault_injections_and_circuit_breaker` |
| **6. 原图路径防泄露与访问审计** | `media.py` 原图访问未审计；向 operator/leader 响应时泄露了底层磁盘路径 | `media.py` 管理员访问原图关联批次写入 `audit_logs (action='access_raw_image')`；`designs.py` 响应对非管理员将 `raw_image_path` 严格脱敏为 `None`，杜绝磁盘路径嗅探。 | `test_raw_access_audit_and_path_masking` |
| **7. 账本结算幂等性** | `budget_service` 结算重复调用会导致 `delta_used` 被抹平成 0，且已结算账本可能被误释放 | `settle_budget_success`、`release_budget_failed`、`hold_budget_unknown`、`resolve_unknown_budget` 全面引入状态机前置判断与乐观锁，重复调用幂等返回，保护已结算预算不归零。 | `test_budget_settle_idempotency` |
| **8. 槽位指针与设计终态保护** | `resolve_unknown` 无条件覆盖槽位当前指针；过时任务可能覆盖新版本 | 处置未知任务与服务重启恢复时，核验设计终态（已淘汰/已归档不改写槽位与设计状态）和槽位当前指向（已有新版本则不覆盖当前指针）。 | `test_resolve_unknown_does_not_overwrite_stale_slot` |
| **9. 结构化恢复与审计防误操作** | 恢复 SQL 字符串匹配不规范，人工暂停或熔断可能被误恢复 | 引入结构化 `pause_reason`；仅当 `pause_reason == 'PAUSE_UNKNOWN_PENDING'` 且未熔断、无剩余 unknown 任务时自动恢复，人工暂停与熔断绝不误恢复；记录恢复审计日志。 | `test_resolve_unknown_validations` |
| **10. 规则配置与记账细分** | 缺少系列规则库配置接口，缺少规划与视觉模型记账，缺少熔断恢复试运行与部分导出 | 增加系列规则查看与更新接口；批次规划时记录 `planning_model` 流水，全景通过提取特征时记录 `vision_model` 流水；实现熔断恢复试运行保护（`trial_run_remaining=3`）；实现多选与未达标时部分导出。 | `test_series_rules_configuration_api`, `test_planning_and_vision_model_cost_accounting`, `test_circuit_break_recover_with_trial_runs`, `test_partial_export_uncompleted_target` |

---

## 二、逐条可核验需求矩阵

对照原始详细实施计划（一至十八节）对功能进行全面分类与审计：

| 原计划条款 | 需求要点 | 落地状态 | 验证方式 / 对应自动化测试 |
| :--- | :--- | :---: | :--- |
| **一、项目目标** | 酒店定制视觉方案工作台，非生产证明，四图固定组合 | **已实现** | 自动化测试与前端页面显著标注非实物生产证明 |
| **二、每组四图标准** | 全景图、整套白底重组图、双床配置白底图、床头近景白底图 | **已实现且已测** | `test_downstream_generation_and_quad_archive` (四槽位完备性与依赖校验) |
| **三、系列规则库** | 现代商务、自然木质、柔和当代三大系列规则库（床屏/床体/柜体/材料/配色/双床/白底规则） | **已实现且已测** | `test_series_rules_configuration_api`, `config.SERIES_RULES` |
| **四、规划原则** | AI 只负责出方案，状态/预算由后端控制；多样性保证；全景未出现家具有限补充 | **已实现且已测** | `test_batch_planning_and_panorama_workflow` (后端状态机强控预算与状态) |
| **五、批次生产流程** | 负责人配置批次 -> 首发全景生成 -> 全景初筛 -> 启动下游三图 -> 四图终审归档 | **已实现且已测** | `test_workflow_state_machine.py`, `test_downstream_and_invalidation.py` |
| **六、候补与补位策略** | 淘汰时根据有效组数 (已归档+在途) 自动按序唤醒候补；候补耗尽支持追加候补 | **已实现且已测** | `test_panorama_redo_limits_and_standby_wake` (淘汰自动唤醒，不超量唤醒) |
| **七、审核与重做上限** | 单槽位最多 2 次人工重做，整组最多 4 次，超限需负责人特批；技术重试单独统计不占人工次数 | **已实现且已测** | `test_panorama_redo_limits_and_standby_wake`, `test_fault_injections_and_circuit_breaker` (429 仅增技术重试) |
| **七、全景替换依赖失效** | 替换全景后，依赖旧全景的下游槽位与版本级联失效 (`invalidated`)，禁止混合归档 | **已实现且已测** | `test_panorama_replacement_invalidates_downstream` |
| **八、预算事务与限额** | 事务中锁定；小样 20 次预警线暂停，25 次硬上限后端硬阻断；并发抢占无超售 | **已实现且已测** | `test_budget_reservation_and_limits`, `test_concurrent_budget_reservations` |
| **八、异常策略分类** | 明确失败释放预占；网络超时/未知保守计费并暂停批次；显式 429 有界技术重试；磁盘满熔断 | **已实现且已测** | `test_fault_injections_and_circuit_breaker`, `test_connection_reset_is_unknown_not_429` |
| **九、RBAC 角色权限** | 操作员/负责人/管理员；**原图严格隔离**：非 admin 访问原图 403 Forbidden，原图访问审计，接口路径脱敏 | **已实现且已测** | `test_rbac_raw_image_isolation`, `test_raw_access_audit_and_path_masking` |
| **十、水印与 ZIP 导出** | Pillow 合成防盗半透明水印；ZIP 重新统一压制最终水印；水印失败全单阻断不降级；支持部分导出 | **已实现且已测** | `test_watermark_rendering`, `test_watermarked_zip_export_integrity`, `test_partial_export_uncompleted_target` |
| **十一、数据模型与版本** | Batch, Design, Slot, SlotVersion, BudgetLedger, Review, AuditLog, WatermarkConfig, ExportRecord, Task | **已实现且已测** | `database.py` WAL 模式完整落盘并由测试覆盖验证 |
| **十二、系统架构与前端** | FastAPI + SQLite WAL + 原生 JS 简约白卡片前端 (Manus 风格，无 React/Vue) | **已实现且已测** | Playwright 8 步全流程浏览器真实操作验收通过 |
| **十六、保留策略与清理保护** | 30 天清理保护时间窗口，绝对保护归档版本、当前使用版本、被依赖全景版本，删除异常不丢路径 | **已实现且已测** | `test_cleanup_days_threshold_and_protection` |
| **十七、第一版明确不包含项** | 自动发品、文案生成、NAS 同步、GPU 混合引擎、号池补号、定时生产、自动启动下游、模型置信度当作事实保证 | **明确未实现** | 严格遵循需求边界，第一阶段坚决不予包含 |
| **十三、第二至四阶段远程服务** | 远程 90 服务器接口探测与契约核验、SSH 隧道配置与生产密钥注入、真实小样试运行 | **未实现(本阶段缺口)** | 严格属于后续受控推进阶段，本阶段不访问 90、不调用真实密钥 |

---

## 三、全量自动化测试结果（27/27 Passed）

项目全量 pytest 测试套件目前包含 27 项自动化测试，涵盖所有状态机转换、并发竞争、异常故障注入、权限隔离与防过时覆盖断言，**全部通过**（通过率 100%）：

```text
tests/test_budget_transactions.py::test_budget_reservation_and_limits          PASSED [  3%]
tests/test_budget_transactions.py::test_budget_explicit_failure_and_unknown    PASSED [  7%]
tests/test_budget_transactions.py::test_concurrent_budget_reservations         PASSED [ 11%]
tests/test_cleanup_protection.py::test_cleanup_protection_policy               PASSED [ 14%]
tests/test_defect_regressions.py::test_budget_settle_idempotency               PASSED [ 18%]
tests/test_defect_regressions.py::test_cleanup_days_threshold_and_protection   PASSED [ 22%]
tests/test_defect_regressions.py::test_state_constraints_and_no_stale_overwrites PASSED [ 25%]
tests/test_defect_regressions.py::test_resolve_unknown_validations             PASSED [ 29%]
tests/test_defect_regressions.py::test_fault_injections_and_circuit_breaker   PASSED [ 33%]
tests/test_defect_regressions.py::test_raw_access_audit_and_path_masking       PASSED [ 37%]
tests/test_defect_regressions.py::test_post_process_watermark_failure_keeps_budget_settled PASSED [ 40%]
tests/test_defect_regressions.py::test_connection_reset_is_unknown_not_429     PASSED [ 44%]
tests/test_defect_regressions.py::test_resolve_unknown_does_not_overwrite_stale_slot PASSED [ 48%]
tests/test_defect_regressions.py::test_circuit_break_recover_with_trial_runs   PASSED [ 51%]
tests/test_defect_regressions.py::test_idempotent_downstream_and_archive_repeats PASSED [ 55%]
tests/test_defect_regressions.py::test_partial_export_uncompleted_target       PASSED [ 59%]
tests/test_defect_regressions.py::test_planning_and_vision_model_cost_accounting PASSED [ 62%]
tests/test_defect_regressions.py::test_series_rules_configuration_api         PASSED [ 66%]
tests/test_downstream_and_invalidation.py::test_downstream_generation_and_quad_archive PASSED [ 70%]
tests/test_downstream_and_invalidation.py::test_panorama_replacement_invalidates_downstream PASSED [ 74%]
tests/test_rbac_and_security.py::test_rbac_raw_image_isolation                 PASSED [ 77%]
tests/test_rbac_and_security.py::test_path_traversal_protection                PASSED [ 81%]
tests/test_rbac_and_security.py::test_invalid_role_rejection                   PASSED [ 85%]
tests/test_watermark_and_export.py::test_watermark_rendering                   PASSED [ 88%]
tests/test_watermark_and_export.py::test_watermarked_zip_export_integrity     PASSED [ 92%]
tests/test_workflow_state_machine.py::test_batch_planning_and_panorama_workflow PASSED [ 96%]
tests/test_workflow_state_machine.py::test_panorama_redo_limits_and_standby_wake PASSED [100%]
======================= 27 passed in 19.46s =======================
```

---

## 四、浏览器端到端验收证据

Playwright（系统 Microsoft Edge 驱动）真实加载 Web 页面，走通全流程并保存 8 张屏幕截图至 `storage/evidence/`：
1. `01_homepage_initial.png`: 首页加载、批次仪表盘、各模型分类预算监控（生图/规划/视觉/重试/未知/预占/已用）、Mock 状态标签及首发设计规划呈现；
2. `02_panorama_review_ready.png`: 首发 6 款设计的全景图生成就绪，带水印预览图与款式参数（床屏/床体/柜体/材质）呈现；
3. `03_panorama_approved.png`: 操作员点击“初筛通过”，全景锁定并提取成图视觉特征基准，记录视觉模型成本；
4. `04_quad_tab_waiting_downstream.png`: 切换至“四图终审区”，全景已就绪，等待人工确认启动下游三图；
5. `05_quad_slots_generated.png`: 人工启动下游后，Worker 独立生成 3 张下游槽位图（白底重组、双床适配、床头近景），4 宫格并列就绪；
6. `06_quad_archived.png`: 终审人员核验一致性，点击“四图合格归档”，设计标记 archived，批次归档进度达成；
7. `07_archived_export_tab.png`: 切换至“已归档与导出”，呈现已归档套图与“导出单款带水印 ZIP”功能；
8. `08_system_and_unknown_protect.png`: 呈现“结果未知任务保护中心”、“水印参数配置”与“30 天清理保护机制”。

---

## 五、服务启动与复现验证命令

在项目根目录 `D:\公司业务\连接服务器\05_独立项目与业务资料\hotel-furniture-workbench`：

1. **运行全量自动化测试（27 项）**:
   ```powershell
   pytest -v
   ```

2. **启动本地 Web 工作台服务**:
   ```powershell
   python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8021
   ```
   浏览器打开: `http://127.0.0.1:8021`。

3. **重新运行 Playwright 浏览器端到端流程与截图**:
   ```powershell
   python tests_e2e\test_browser_flow.py
   ```

---

## 六、当前事实核对与剩余未满足要求说明

### 本地第一阶段已满足事实
- 本地 Mock 全状态机闭环、SQLite WAL + BEGIN IMMEDIATE 并发事务预算账本（生图、规划模型、视觉分析模型分类记账，幂等结算防归零）；
- 全景初筛通过/锁定、重做单槽位 2 次/整组 4 次硬拦截、全景替换下游依赖失效、候补按缺口自动补位；
- 结果未知保护机制、批次结构化暂停与人工凭证处置；
- 故障分类与有界技术重试（显式 429 技术重试不占人工重做次数，连接断开保守作为 unknown，原图生成后水印失败结算已用并保留原图）；
- RBAC 严格隔离、原图访问审计、非管理员原图底层路径脱敏；
- 30 天时间窗口清理保护（年轻版本保留、归档/当前/依赖版本强保护，删除异常保留路径）；
- 带水印 ZIP 导出原子性、防退化保护以及未满额时的部分导出功能。

### 剩余未满足要求（后续阶段缺口）
按照整体实施计划，以下内容严格属于第二阶段及之后的工作，本阶段依授权保持隔离，暂未实现：
1. **第二阶段远程服务契约**：尚未对 90 服务器进行远程 HTTP 接口探测与响应契约核验（包括 `/v1/images/generations`、`/v1/images/edits`、超时与限流实测）；
2. **第三阶段网络隧道**：尚未建立 SSH 隧道将本地请求转发至 90 服务器内部 `127.0.0.1:8021`，生产密钥尚未注入；
3. **第四阶段真实小样**：尚未在真实生图模型上验证 6 款首发设计的真实成图质感、一致性及真实 token/额度消耗比；
4. **第十四/十五节生产部署与认证**：尚未接入生产环境的门户/JWT 认证层，尚未编写生产 systemd/nginx 服务配置。
