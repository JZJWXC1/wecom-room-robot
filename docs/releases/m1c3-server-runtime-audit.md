# M1C3 服务器运行方式审计

本审计只基于仓库内文件做只读分析；本轮未连接服务器。每条结论标注为“代码可确认”或“服务器现场待确认”。

## systemd 主服务

| 项目 | 结论 | 状态 |
| --- | --- | --- |
| 主服务名称 | `wecom-room-robot.service` | 代码可确认：`scripts/deploy-systemd-ubuntu.sh` 生成该 service。 |
| 工作目录 | `/opt/wecom-room-robot` | 代码可确认。 |
| 环境加载 | `EnvironmentFile=/opt/wecom-room-robot/.env` | 代码可确认；`.env` 实际内容服务器现场待确认。 |
| 启动命令 | `/opt/wecom-room-robot/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` | 代码可确认。 |
| Restart | `always`, `RestartSec=3` | 代码可确认。 |
| User/Group | deploy 脚本主服务未显式设置，通常由 systemd 默认 root 执行 | 代码可确认脚本未写 User；服务器现场实际 unit 待确认。 |

## systemd 同步服务和定时器

| unit | 入口 | 频率 | User/Group | 状态 |
| --- | --- | --- | --- | --- |
| `wecom-room-robot-feishu-region-sync.service` | `/opt/wecom-room-robot/.venv/bin/python /opt/wecom-room-robot/scripts/sync_feishu_region_inventory.py` | 由 timer 触发 | `root/root` | 代码可确认。 |
| `wecom-room-robot-feishu-region-sync.timer` | 同步四区房源/素材后刷新旧 cache/index 并触发 Shadow | 08:00、13:00、19:00 | 不适用 | 代码可确认。 |
| `wecom-room-robot-rag-cache-sync.service` | `/opt/wecom-room-robot/.venv/bin/python /opt/wecom-room-robot/scripts/refresh_rag_inventory_cache.py` | 由 timer 触发 | `root/root` | 代码可确认。 |
| `wecom-room-robot-rag-cache-sync.timer` | 刷新旧 RAG cache/index 并触发 Shadow | 08:05、13:05、19:05 | 不适用 | 代码可确认。 |

timer 是否已启用、最近运行结果、现场 unit 是否被手工修改：服务器现场待确认。

## server-ops.ps1

| Action | 行为 | 状态 |
| --- | --- | --- |
| `Status` | 远程执行 `systemctl status` 三个 unit | 代码可确认；本轮禁止执行。 |
| `Health` | 远程 `curl http://127.0.0.1:8000/health` | 代码可确认；本轮禁止执行。 |
| `Test` | 远程 `.venv/bin/python -m pytest -q` | 代码可确认；本轮禁止执行。 |
| `Restart` | 远程重启服务并健康检查 | 代码可确认；M1C3 预检阶段禁止执行。 |
| `SyncDryRun`/`SyncRun` | 远程运行 region sync | 代码可确认；M1C3 预检阶段禁止执行。 |
| `RagCacheSync` | 远程运行 RAG cache sync | 代码可确认；M1C3 预检阶段禁止执行。 |
| `UnattendedCheck` | 检查服务、timer、health 和凭证状态 | 代码可确认；本轮禁止执行。 |

`server-ops.ps1` 可从 `.local/server-credentials.ps1` 或环境变量读取 SSH 凭证。凭证内容不得提交；服务器现场凭证状态待确认。

## 部署脚本

| 脚本 | 行为 | 状态 |
| --- | --- | --- |
| `scripts/deploy-systemd-ubuntu.sh` | 安装 Python/nginx、创建 `.venv`、安装 `requirements.txt`、写主 service、复制 timer units 并 enable | 代码可确认；M1C3 不要求修改 systemd。 |
| `scripts/deploy-aliyun.sh` | 使用 docker compose 启动 | 代码可确认；当前 systemd 路径与该脚本并存，服务器实际使用方式待确认。 |
| `scripts/bootstrap-aliyun-ubuntu.sh` | 安装 docker/nginx 等基础组件 | 代码可确认；本轮不执行。 |

## .env/config 加载方式

| 项目 | 结论 | 状态 |
| --- | --- | --- |
| Python 配置 | `app/config.py` 使用 pydantic-settings，`env_file=".env"`，UTF-8 | 代码可确认。 |
| systemd 环境 | 主服务和两个同步 service 都声明 `EnvironmentFile=/opt/wecom-room-robot/.env` | 代码可确认。 |
| Shadow 默认 | `INVENTORY_SNAPSHOT_MODE` 默认 `disabled` | 代码可确认。 |
| Shadow 正式观察 | 只允许设置 `INVENTORY_SNAPSHOT_MODE=shadow` | 代码可确认解析只允许 `disabled`/`shadow`。 |
| 真实 `.env` | 是否已有 Shadow 配置、路径是否可写 | 服务器现场待确认。 |

## 当前同步入口和频率

- `scripts/sync_feishu_region_inventory.py`：region 源同步成功且非 dry-run 后，调用 `InventoryService().refresh()` 刷新旧 cache，再写旧 rewrite index，并在 M1C3 中触发 Shadow。
- `scripts/refresh_rag_inventory_cache.py`：直接刷新旧 cache/index，并在 M1C3 中触发 Shadow。
- 两个 timer 每天三次，region 在 08:00/13:00/19:00，RAG cache 在 08:05/13:05/19:05。

以上为代码可确认；服务器 timer 是否启用、是否存在额外 cron 或手工同步入口：服务器现场待确认。

## 服务用户、数据目录、日志目录

| 项目 | 结论 | 状态 |
| --- | --- | --- |
| 同步 service 用户 | `root/root` | 代码可确认。 |
| 主 service 用户 | deploy 脚本未显式设置 User/Group | 代码可确认；现场 unit 待确认。 |
| 项目目录 | `/opt/wecom-room-robot` | 代码可确认。 |
| 旧 cache | `data/inventory_cache.csv` | 代码可确认默认值；现场文件待确认。 |
| 旧 cache meta | `data/inventory_cache_meta.json` | 代码可确认默认值；现场文件待确认。 |
| 旧 rewrite index | `data/rewrite_inventory_index.json` | 代码可确认默认值；现场文件待确认。 |
| 旧 PNG | `room_database/inventory_*.png` | 代码可确认默认 glob；现场文件待确认。 |
| Shadow root | `data/inventory_snapshots_shadow` 或 `.env` 覆盖 | 代码可确认；现场目录待确认。 |
| 应用日志 | systemd journal；业务事件写 `data/kf_dialogue_events.jsonl` 等 | 代码可确认路径默认；现场日志保留策略待确认。 |

## 健康检查入口

| 入口 | 返回 | 状态 |
| --- | --- | --- |
| `GET /health` | `ok=true`、service 名、旧 `inventory_cache_meta` | 代码可确认。 |
| `scripts/check_inventory_snapshot_shadow.py` | Shadow health/readiness 和最近 reconciliation 摘要 | M1C3 新增，代码可确认。 |
| `scripts/preflight_inventory_snapshot_shadow.py` | 只读发布前检查 | M1C3 新增，代码可确认。 |

`/health` 当前不读取 Shadow health，不作为 Shadow readiness 依据；这是代码可确认。

## 生产读取路径

M1C3 代码可确认：

- `app/main.py` 仍创建 `inventory = InventoryService()`。
- 客服处理、RAG 工具和发送阶段仍使用旧 `InventoryService`、旧 rewrite index、旧 PNG。
- `run_inventory_snapshot_shadow` 只在 `_refresh_inventory` admin helper 以及两个同步脚本中触发。
- 未启用 `SnapshotReader` 作为客户查询事实源。

服务器现场待确认：

- 线上代码是否与 M1C3 commit 一致。
- 是否有未纳入仓库的补丁或额外进程读取 Snapshot root。
