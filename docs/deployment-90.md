# 90 服务器部署记录

- 应用：酒店家具套图工作台；入口 `/next/hotel-furniture/`；本机端口 `127.0.0.1:8028`。
- 代码：`/home/sa/hotel-furniture-workbench`；数据：该目录下的 `storage`；用户服务：`hotel-furniture-workbench.service`。
- 生图和文本共用本机号池 `http://127.0.0.1:8021`，分别使用 `gpt-image-2` 和 `auto`；专用 user key 仅存于 `~/.config/hotel-furniture-workbench.env`（权限 0600）。
- 入口采用 license-service 登录与模块权限校验；管理员映射为 `admin`，获授权设计岗位映射为 `artist`。管理员、业务部设计、外贸部设计可访问；其他岗位门户不显示入口，直链返回 403。生产模式由可信代理注入身份，美工可创建自动规划任务，手动批次仍限管理员。
- 本地前端支持 `WORKBENCH_PUBLIC_BASE`，API、静态资源、预览及 ZIP 下载均使用此前缀；生产身份取自 `/api/session`，角色选择器不可修改真实身份。
- 发布前备份：Nginx `furniture.nginx.conf.bak-hotel-20261104`；门户 `modules.json.bak-hotel-20261104`；license/admin 的 `main.py.bak-hotel-20261104`；许可服务门户过滤前备份 `main.py.bak-hotel-before-nav-20261104`；SQLite 在线备份 `license.db.bak-hotel-20261104`。回滚应先核对修改后的其他业务变更，避免直接覆盖其他人的更新。
- 2026-11-04：模型列表和文本请求 HTTP 200；真实全景 `batch_9cb8506d` / `ver_dbc575cd` 生成完成，版本记录 `is_mock=0`，管理员和美工预览均返回 JPEG 200；未授权岗位 403，美工伪造管理员头后创建批次仍为 403。
- 2026-11-04 追加验收：经用户授权，将验收批次图片硬上限从 1 调整为 4 并记录审计，补齐 3 张真实配图；美工在 UI 完成四图归档，管理员和美工均通过 UI 下载真实 ZIP，包内 4 张 PNG 和 1 份元数据，ZIP 完整性和路径检查通过。隔离 Mock 端到端验收 85/85 通过（含故障与清理检查）。
- `/api/health` 按当前数据库配置返回图像和文本上游是否启用；`network_connected_to_90: null` 表示健康接口没有探测连接，不是故障判断。批次级 `is_mock` 仍是历史静态值，图像真实性应以具体图片版本的 `is_mock` 为准。健康接口修正前服务代码备份为 `backend/app/main.py.bak-health-20261104`。
- 既有 asset-library 服务异常不属于本次范围。真实 ZIP 全链路已在上述验收批次完成。
