# SSH 隧道配置说明（计划第十三节第三阶段）

目标：本地后端通过 SSH 隧道访问 90 服务器的生图服务 `127.0.0.1:8021`，
90 的生图端口不暴露到局域网或公网，隧道只绑定本机。

## 隧道建立（在部署 workbench 的主机上执行）

```bash
# -N 不执行远端命令；-T 不分配远端终端
# 绑定 127.0.0.1（缺省即本机回环，绝不使用 0.0.0.0 / -g）
ssh -N -T \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -L 127.0.0.1:18021:127.0.0.1:8021 \
    tunnel-user@server90
```

- 本地工作台配置 `UPSTREAM_IMAGE_API_BASE=http://127.0.0.1:18021`（本地转发端口）。
- 浏览器不直接访问 90；前端只与本地工作台后端通信。
- 隧道进程建议由 systemd 用户服务或 autossh 托管：

```ini
# /etc/systemd/system/workbench-tunnel.service
[Unit]
Description=SSH tunnel to server90 image API (localhost only)
After=network-online.target

[Service]
User=workbench
ExecStart=/usr/bin/ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 -L 127.0.0.1:18021:127.0.0.1:8021 tunnel-user@server90
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 密钥红线

- SSH 私钥与上游 API 密钥只存放在部署主机的受保护配置（`/etc/hotel-furniture-workbench/env`，0640）或环境变量；
- 密钥不进入前端、不进入 Git、不进入日志；
- 不读取 90 运行容器中的现有密钥；不自动修改 90 生产配置。

## 前置条件（计划第二阶段）

启用隧道前必须先完成 90 接口契约核验：`/v1/images/generations`、`/v1/images/edits`、
模型名、输出格式、超时、429/401/403 行为、健康检查、额度查询、请求状态查询能力。
不能因为健康接口正常就宣称真实生图可用。
