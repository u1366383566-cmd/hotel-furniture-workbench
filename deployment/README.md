# 上线部署模板与说明（计划第十四节交付物）

本目录为《酒店家具智能套图工作台详细实施计划》第十四节要求的部署交付物。
**前置条件**：本地第一阶段验收已通过（见 `storage/evidence/acceptance/acceptance_report.json`）。
**模板状态**：本地验收是 Mock 闭环，不等于生产上线。`nginx-workbench.conf` 为模板，
未经 `nginx -t` 与真实门户 `auth_request` 链路验证，不宣称生产可用。
开放端点仅 `GET /api/health` 与静态首页；派生业务接口全部要求身份并做角色校验。

## 文件清单

| 文件 | 对应计划要求 |
| --- | --- |
| `DEPLOYMENT.md` | Linux 部署说明、数据目录说明、日志策略、健康检查、备份恢复、回滚步骤、生产权限配置 |
| `workbench.service` | systemd 服务模板 |
| `nginx-workbench.conf` | nginx 配置模板（含身份头信任边界与伪造头清理） |
| `env.example` | 环境变量说明 |
| `ssh-tunnel.md` | SSH 隧道配置说明（第三阶段，绑定本机） |

## 上线前必须逐项确认（计划第十四节清单）

- [ ] 目标目录：`/opt/hotel-furniture-workbench`
- [ ] 服务端口：`127.0.0.1:8021`（只绑本机回环，由 nginx 对外）
- [ ] 进程用户：专用低权限用户 `workbench`
- [ ] 数据目录：`/var/lib/hotel-furniture-workbench`（SQLite + 图片 + 导出）
- [ ] 存储目录：同上，见 DEPLOYMENT.md 目录表
- [ ] nginx 路由：`location /` → 反代 127.0.0.1:8021
- [ ] 门户接入方式：nginx `auth_request` → 门户/license-service 校验端点
- [ ] JWT 来源：门户签发，由 auth_request 子请求校验，后端不解析 JWT
- [ ] 身份头信任边界：后端 `WORKBENCH_AUTH_MODE=trusted_proxy`；nginx 清除外部伪造头后注入已验证身份
- [ ] 生产密钥来源：systemd `EnvironmentFile=/etc/hotel-furniture-workbench/env`（root:workbench 0640）
- [ ] 回滚方式：代码目录版本化（`releases/<timestamp>` + `current` 软链），回滚 = 切软链 + 重启服务

## 明确禁止（计划第十四节红线）

- 不得自动修改 90 服务器生产配置；不得重启现有生产服务
- 不得开启定时生产；不得启用"全景通过后自动生成下游"
- 不得修改现有号池源码或其他生产服务
- 90 生图端口不得暴露到局域网/公网；隧道只绑定本机（见 ssh-tunnel.md）
