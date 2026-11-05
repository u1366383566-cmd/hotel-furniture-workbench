---
title: 酒店家具套图工作台
tags: [服务, 家具设计, 生图]
created: 2026-11-04
updated: 2026-11-04
status: active
---

# 酒店家具套图工作台

部署于 90 服务器的酒店家具智能套图工作台，入口 `/next/hotel-furniture/`，module_id `hotel-furniture`，后端监听 `127.0.0.1:8028`。代码在 `/home/sa/hotel-furniture-workbench`，数据在其 `storage` 目录，用户服务为 `hotel-furniture-workbench.service`。

登录及模块授权由 license-service 和 Nginx 实施：管理员映射为 `admin`，业务部设计和外贸部设计映射为 `artist`；其他岗位入口不可见且直链返回 403。生产后端只接受代理注入身份，美工可创建自动规划任务，手动批次仍限管理员。`WORKBENCH_PUBLIC_BASE=/next/hotel-furniture` 统一处理 API、静态文件、图片预览及 ZIP 下载路径。

图像和文本模型通过 `http://127.0.0.1:8021` 连接 seezo 号池，模型分别为 `gpt-image-2` 与 `auto`。专用 user key 仅存于 `~/.config/hotel-furniture-workbench.env`，权限 0600。实际生图模式以 `image_api_configs.enabled` 为准；各图真实或 Mock 的依据为图片版本记录的 `is_mock`。健康接口反映配置开关，`network_connected_to_90: null` 表示未执行连接探测。

2026-11-04 验收：在用户授权下生成 4 张真实图，四图通过界面归档；管理员和美工分别通过界面下载 ZIP，文件完整，含 4 张 PNG 和一份元数据。门户三种目标岗位可见，其他岗位不可见且访问 403。隔离环境端到端验收 85/85 通过。验收批次 `batch_9cb8506d`，设计 `des_21616b22`。

回滚时先核对其他业务修改：Nginx、门户模块清单、license/admin 服务源码以及 license SQLite 有 `bak-hotel-20261104` 快照；门户过滤前许可服务另有 `main.py.bak-hotel-before-nav-20261104`。工作台可通过用户级 systemd 停用；号池专用 key 可在号池管理端撤销。既有 asset-library 异常不属于本次范围。

关联：[[seezo-top-platform]]、[[new-tool-onboarding]]。
