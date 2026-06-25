# M1C3 InventorySnapshot Shadow 发布和回滚 Runbook

本 runbook 供获得明确 `APPROVE_DEPLOY` 后使用。本轮预检禁止 SSH、禁止上传、禁止重启服务、禁止修改服务器。

## 发布前原则

- 只允许设置 `INVENTORY_SNAPSHOT_MODE=shadow`。
- 禁止 `primary`、自动切换、客户读取入口切换。
- 禁止删除旧 CSV、旧 rewrite index、旧 PNG、旧 `InventoryService`。
- 禁止把真实密码、手机号、token、飞书原始响应写入命令行、日志或文档。
- M1C3 不新增生产依赖，`requirements.txt` 预期不变。

## 发布流程

1. 记录当前服务器 commit/version。
   - 在服务器项目目录记录 `git rev-parse HEAD` 或当前发布包版本。
   - 记录当前 `.env` 中 `INVENTORY_SNAPSHOT_MODE` 是否存在；不得打印密钥。

2. 备份即将覆盖的代码文件。
   - 备份清单必须来自 `docs/releases/m1c3-shadow-deployment-manifest.md` 的“必须部署的运行时代码文件”。
   - 备份目标建议放在服务器本地带时间戳目录，例如 `/opt/wecom-room-robot-backups/m1c3-YYYYmmdd-HHMMSS/`。
   - 不备份到公开目录。

3. 确认旧 CSV/index/PNG 可用。
   - 检查 `data/inventory_cache.csv` 存在且可读。
   - 检查 `data/rewrite_inventory_index.json` 存在且可读。
   - 检查 `room_database/inventory_*.png` 至少有一份。
   - 这些文件不得被 M1C3 覆盖或删除。

4. 上传 M1C3 文件。
   - 只上传 manifest 中列出的运行时代码文件和需要的文档。
   - 禁止上传 `tests/fixtures/`、本地 `data/`、本地 `room_database/`、`.env`、`.local/`。

5. 安装依赖检查。
   - 本阶段预期无新依赖。
   - 若 `requirements.txt` 与服务器当前版本一致，不应重新安装依赖。
   - 若执行依赖检查，不得访问无关外部服务。

6. 运行服务器允许的完整测试。
   - 首选：`.venv/bin/python -m pytest -q`
   - 若服务器资源不允许全量，至少运行 Snapshot/Shadow/preflight 相关测试，并记录原因。

7. 运行编译检查。
   - `.venv/bin/python -m compileall app`

8. 运行 preflight。
   - `.venv/bin/python scripts/preflight_inventory_snapshot_shadow.py --mode shadow`
   - 必须确认：无 `primary`、旧生产文件存在、Shadow/production path 隔离、当前生产 pointer 未被读取、未访问飞书/企业微信。

9. 设置 `INVENTORY_SNAPSHOT_MODE=shadow`。
   - 只修改服务器 `.env` 中该项和必要的 Shadow 配置项。
   - 建议同时确认：
     - `INVENTORY_SNAPSHOT_SHADOW_ROOT=/opt/wecom-room-robot/data/inventory_snapshots_shadow`
     - `INVENTORY_SNAPSHOT_SHADOW_STALE_SECONDS=86400`
     - `INVENTORY_SNAPSHOT_SHADOW_REQUIRED_PASSES=3`
     - `INVENTORY_SNAPSHOT_SHADOW_TIMEOUT_SECONDS=10`
     - `INVENTORY_SNAPSHOT_SHADOW_REPORT_RETENTION=30`
   - 不修改 `AUTO_REPLY_ENABLED`、`AUTO_MEDIA_ENABLED`、`AUTO_PASSWORD_ENABLED`；当前仓库未提供这三个集中开关。

10. 重启服务。
    - 重启前必须已经完成备份和 preflight。
    - 重启后立即确认主服务状态。

11. 健康检查。
    - `curl -sS http://127.0.0.1:8000/health`
    - 注意：`/health` 仍是旧库存健康，不代表 Shadow readiness。

12. 手动触发或等待下一次原有同步。
    - 优先等待现有 timer；如需手动触发，必须明确记录。
    - 手动触发也只能走既有 `scripts/sync_feishu_region_inventory.py` 或 `scripts/refresh_rag_inventory_cache.py`，不得新增同步入口。

13. 运行 Shadow 观察 CLI。
    - `.venv/bin/python scripts/check_inventory_snapshot_shadow.py`
    - 或 `.venv/bin/python scripts/check_inventory_snapshot_shadow.py --json`
    - 观察 `reconciliation_passed`、`blocking_count`、`public_artifact_secret_scan_passed`、`consecutive_passes`、`stale`。

14. 验证客户回复仍读取旧生产路径。
    - 确认客服消息路径没有导入/调用 Snapshot Reader。
    - 观察旧 `InventoryService` cache meta、旧 rewrite index 和旧 PNG 仍可用。
    - 抽样客服回复不得出现 Shadow-only snapshot_id、真实密码泄漏或房源事实变化。

## 回滚流程

1. 将 mode 改回 disabled。
   - `.env` 中设置 `INVENTORY_SNAPSHOT_MODE=disabled`，或删除该配置让默认值生效。

2. 恢复旧代码文件。
   - 使用发布前备份恢复 manifest 中覆盖过的代码文件。
   - 不恢复本地测试 fixture 或 artifact。

3. 重启服务。
   - 重启后检查 systemd 状态。

4. 验证旧同步、旧房源查询和企业微信健康。
   - 检查 `data/inventory_cache.csv`、`data/rewrite_inventory_index.json`、`room_database/inventory_*.png` 仍存在。
   - 执行 `/health`。
   - 检查企业微信客服回调状态和近期日志。

5. Shadow 目录可以保留用于排查。
   - 不需要删除 `data/inventory_snapshots_shadow`。
   - Shadow 目录不得影响生产读取。

6. 不删除旧生产房源文件。
   - 不删除旧 CSV。
   - 不删除旧 rewrite index。
   - 不删除旧 PNG。
   - 不删除旧 `InventoryService` 相关代码。

## 发布中止条件

任一条件满足时立即停止发布或回滚：

- preflight 发现 `primary` 或非法 mode。
- Shadow root 与正式 Snapshot root 重叠。
- 旧 CSV/index/PNG 缺失。
- `pytest` 或 `compileall` 失败。
- Shadow report 出现 blocking mismatch。
- 公共 artifact secret scan 失败。
- 客户回复路径出现 Snapshot Reader 读取。
- 日志或公共 artifact 出现真实密码、手机号、token 或私密链接。
