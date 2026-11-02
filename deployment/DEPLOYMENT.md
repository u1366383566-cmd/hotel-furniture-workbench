# Linux 部署说明（计划第十四节）

前置：本地第一阶段验收通过（`pytest -q` 全绿 + E2E 通过 + `tests/acceptance/go_live_acceptance.py` 全通过）。
**口径声明**：本地第一阶段是 Mock 闭环验收，不代表生产上线。本目录的 nginx/systemd
均为模板，`nginx-workbench.conf` 尚未经过 `nginx -t` 与真实门户 `auth_request` 链路验证，
上线前必须逐项人工确认并完成真实联调；用户未授权 90 环境接入前不得执行任何部署动作。
本说明只覆盖工作台自身上线；90 上游接入按阶段二/三另行推进（见 `ssh-tunnel.md`）。

## 1. 目标目录与版本化回滚

```
/opt/hotel-furniture-workbench/
├── releases/<timestamp>/     # 每次发布一个独立目录（代码 + venv）
├── current -> releases/<ts>  # systemd WorkingDirectory 指向该软链
└── venv/                     # 共享虚拟环境（或每 release 独立）
```

发布：新目录 → 切 `current` 软链 → `systemctl restart hotel-furniture-workbench`。
回滚：`ln -sfn releases/<上一版本> current && systemctl restart hotel-furniture-workbench`。
回滚不回滚数据库 schema（本项目 schema 仅追加列，向后兼容；见第 6 节）。

## 2. 进程用户与权限

```bash
sudo useradd -r -s /usr/sbin/nologin workbench
sudo mkdir -p /var/lib/hotel-furniture-workbench/logs
sudo chown -R workbench:workbench /var/lib/hotel-furniture-workbench
sudo chmod 750 /var/lib/hotel-furniture-workbench
sudo mkdir -p /etc/hotel-furniture-workbench
sudo cp deployment/env.example /etc/hotel-furniture-workbench/env
sudo chown root:workbench /etc/hotel-furniture-workbench/env && sudo chmod 640 /etc/hotel-furniture-workbench/env
```

生产权限配置（RBAC 三角色沿用系统内逻辑）：
- 操作员 Operator：水印预览、初筛、终审、限制内重做、淘汰、启动下游、导出 ZIP；
- 负责人 Leader：系列规则、批次/预算配置、预警与超限特批、水印、熔断恢复、追加候补；
- 管理员 Admin：部署、密钥、生产配置、无水印原图（访问必审计）、30 天清理、系统权限。
生产身份来源 = 门户/JWT → nginx auth_request → 注入身份头（见 `nginx-workbench.conf`），
后端 `WORKBENCH_AUTH_MODE=trusted_proxy` 拒绝缺失身份。

## 3. 数据目录说明（= 存储目录）

```
/var/lib/hotel-furniture-workbench/
├── db/workbench.db          # SQLite WAL（真实任务账本，含 -wal/-shm）
├── raw_images/              # 无水印原图（Admin-only 访问，接口层审计）
├── preview_images/          # 带水印预览图
├── exports/                 # 带水印 ZIP 导出文件
└── logs/                    # 服务日志
```

权限：全部 `workbench:workbench`，目录 750、数据库 640。
备份至少覆盖 `db/` 与 `raw_images/`（见第 5 节）。

## 4. 服务安装与健康检查

```bash
sudo cp deployment/workbench.service /etc/systemd/system/hotel-furniture-workbench.service
sudo systemctl daemon-reload && sudo systemctl enable --now hotel-furniture-workbench

# 健康检查步骤
curl -s http://127.0.0.1:8021/api/health | tee /dev/stderr | grep -q '"status": "online"'
systemctl is-active hotel-furniture-workbench
curl -sI http://127.0.0.1:8021/ | head -1        # 期望 200
# 经 nginx 的身份链路检查（无身份应 401）
# 前提: workbench.service / env 已设置 WORKBENCH_AUTH_MODE=trusted_proxy,
# 否则后端默认 local_sim 模式会以模拟身份放行, 该 curl 不会返回 401。
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8021/api/batches   # 期望 401
```

端点开放范围：`GET /api/health` 与静态首页是刻意公开的探测端点，不含业务数据；
其余派生业务接口全部读取身份头并做角色校验，未携带身份时在 trusted_proxy 模式下返回 401。

## 5. 备份和恢复说明

```bash
# 每日备份（crontab，示例 02:30）：WAL 安全快照
sqlite3 /var/lib/hotel-furniture-workbench/db/workbench.db ".backup '/backup/workbench-$(date +%F).db'"
tar czf /backup/images-$(date +%F).tgz -C /var/lib/hotel-furniture-workbench raw_images preview_images exports
```
- 恢复：停服务 → 还原 `db/` 与图片目录 → `chown -R workbench:workbench` → 起服务 → 健康检查；
- 重启自愈语义已内建：`running` 任务有完整落盘文件则自愈完成，否则转 unknown 并暂停批次等待负责人核账；
- 长期保留≠异机备份，异机备份策略需另行建设（计划第十六节）。

## 6. 日志策略

- 服务日志：journald + `/var/lib/hotel-furniture-workbench/logs/service(.err).log`，保留 ≥14 天并轮转（logrotate）；
- 业务审计：`audit_logs` 表（创建批次/预算批准/生成/审核/重做/淘汰/版本回选/水印修改/原图访问/ZIP 导出/清理/熔断恢复），
  随数据库一并备份；
- 密钥与身份凭据不得出现在任何日志（红线）。

## 7. 回滚步骤（汇总）

1. `ln -sfn /opt/hotel-furniture-workbench/releases/<prev> /opt/hotel-furniture-workbench/current`
2. `sudo systemctl restart hotel-furniture-workbench`
3. 健康检查（第 4 节）通过后恢复对外；数据无需回滚（schema 向后兼容）。

## 8. 上线红线（不得自动执行）

- 不修改 90 生产配置、不重启现有生产服务、不改号池源码；
- 不开启定时生产、不启用"全景通过后自动生成下游"；
- 隧道与真实密钥按阶段三/四受控推进。
