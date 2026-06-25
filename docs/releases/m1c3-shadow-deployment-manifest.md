# M1C3 InventorySnapshot Shadow 部署差异清单

本清单只覆盖 M1C3 Shadow 生产发布预检所需文件。禁止用“部署整个仓库”替代本文件级清单。

## 部署目标

- 目标模式：仅 `INVENTORY_SNAPSHOT_MODE=shadow`。
- 默认安全态：未配置 `INVENTORY_SNAPSHOT_MODE` 时仍为 `disabled`。
- 生产读取：继续使用旧 `InventoryService`、旧 CSV、旧 rewrite index、旧 PNG。
- 禁止内容：不部署测试、fixture、本地 artifact、真实密钥、真实房源导出。

## 必须部署的运行时代码文件

这些文件共同组成 M1C3 Shadow 旁路、健康状态、观察 CLI 和 preflight：

| 文件 | 类型 | 原因 |
| --- | --- | --- |
| `app/config.py` | 修改 | 增加 Shadow stale、readiness、timeout、report retention 集中配置。 |
| `app/main.py` | 修改 | 仅 admin refresh helper 在旧 index 成功后触发 Shadow；客户消息路径不变。 |
| `app/services/inventory.py` | 修改 | 复用集中 legacy 飞书表解析器，保持旧读取入口。 |
| `app/services/inventory_legacy_parser.py` | 新增 | 旧飞书 values 到 legacy rows 的集中解析函数。 |
| `app/services/inventory_snapshot_models.py` | 修改 | 安全脱敏白名单补充 Shadow 扫描布尔字段。 |
| `app/services/inventory_snapshot_builder.py` | 新增/已有 M1 文件 | 构建旁路 Snapshot。 |
| `app/services/inventory_snapshot_legacy_adapter.py` | 新增/已有 M1 文件 | 旧 rows 到 Snapshot 输入的临时适配边界，M1D 删除候选。 |
| `app/services/inventory_snapshot_reconciliation.py` | 修改 | Shadow 与旧 index 的一致性对比报告。 |
| `app/services/inventory_snapshot_shadow.py` | 修改 | Shadow coordinator、health、sync_run_id 去重、report retention。 |
| `app/services/inventory_snapshot_shadow_observer.py` | 新增 | 只读观察数据收集和安全格式化。 |
| `app/services/inventory_snapshot_shadow_preflight.py` | 新增 | 只读发布前检查。 |
| `app/services/inventory_snapshot_store.py` | 新增/已有 M1 文件 | 写 Shadow snapshot artifact；M1C3 不 activate 正式 pointer。 |
| `app/services/inventory_snapshot_validator.py` | 新增/已有 M1 文件 | Snapshot schema 和路径校验。 |
| `scripts/refresh_rag_inventory_cache.py` | 修改 | 旧 RAG cache/index 成功后传入唯一 `sync_run_id` 触发 Shadow。 |
| `scripts/sync_feishu_region_inventory.py` | 修改 | 旧 region 同步成功后传入唯一 `sync_run_id` 触发 Shadow。 |
| `scripts/check_inventory_snapshot_shadow.py` | 新增 | 只读 Shadow 观察 CLI。 |
| `scripts/preflight_inventory_snapshot_shadow.py` | 新增 | 只读 Shadow 发布前检查 CLI。 |

## 建议一并部署但不作为运行时依赖的文档

| 文件 | 原因 |
| --- | --- |
| `.env.example` | 非敏感 Shadow 默认项参考；不得覆盖服务器真实 `.env`。 |
| `docs/releases/m1c3-shadow-deployment-manifest.md` | 本文件，发布差异清单。 |
| `docs/releases/m1c3-server-runtime-audit.md` | 服务器运行方式审计。 |
| `docs/releases/m1c3-shadow-runbook.md` | 发布和回滚 runbook。 |
| `docs/releases/m1c3-shadow-observation-template.md` | 三次 Shadow 观察记录模板。 |
| `docs/legacy-rule-inventory.md` | Legacy Removal Gate 更新。 |
| `docs/rag-rule-ownership.md` | 记录 Shadow health 不属于客服事实源。 |
| `docs/inventory-snapshot-test-plan.md` | M1C3 测试覆盖说明。 |
| `docs/inventory-snapshot-migration.md` | M1C3 状态说明。 |

## 不应部署的文件

| 路径 | 原因 |
| --- | --- |
| `tests/` | 只用于本地/服务器测试，不作为运行时代码发布。 |
| `tests/fixtures/` | 包含脱敏测试 fixture，不应进入生产运行目录。 |
| `data/inventory_snapshots_shadow/` | 本地 Shadow artifact，不得上传覆盖服务器现场。 |
| `data/inventory_snapshots/` | 正式 Snapshot root 尚未启用，不得上传本地内容。 |
| `data/*.json`, `data/*.csv`, `data/*.jsonl` | 本地状态/缓存/上下文，不得覆盖生产数据。 |
| `room_database/` 本地素材 | 除非单独素材发布流程批准，否则不得随 M1C3 覆盖。 |
| `.env`, `.local/`, SSH 凭证文件 | 密钥和本地凭证禁止提交/上传。 |
| `__pycache__/`, `.pytest_cache/`, `*.pyc` | 本地构建产物。 |

## 环境变量

M1C3 正式 Shadow 观察期只新增或确认以下 Shadow 配置：

| 环境变量 | 生产建议 | 默认 | 说明 |
| --- | --- | --- | --- |
| `INVENTORY_SNAPSHOT_MODE` | `shadow` | `disabled` | 只允许 `disabled`/`shadow`；非法值报错，不允许 `primary`。 |
| `INVENTORY_SNAPSHOT_SHADOW_ROOT` | `/opt/wecom-room-robot/data/inventory_snapshots_shadow` | `data/inventory_snapshots_shadow` | Shadow 独立根目录。 |
| `INVENTORY_SNAPSHOT_SHADOW_STALE_SECONDS` | `86400` | `86400` | health stale 阈值。 |
| `INVENTORY_SNAPSHOT_SHADOW_REQUIRED_PASSES` | `3` | `3` | readiness 连续不同 `source_hash` 成功次数。 |
| `INVENTORY_SNAPSHOT_SHADOW_TIMEOUT_SECONDS` | `10` | `10.0` | Shadow 单次执行超时。 |
| `INVENTORY_SNAPSHOT_SHADOW_REPORT_RETENTION` | `30` | `30` | 保留最近 reconciliation report 数。 |

不新增真实密码、手机号、token、飞书原始响应到环境变量或命令行参数。

现有生产安全开关确认：

| 开关 | 当前仓库状态 | M1C3 动作 |
| --- | --- | --- |
| `INVENTORY_SNAPSHOT_MODE` | 已存在 | 部署时可设为 `shadow`。 |
| `AUTO_REPLY_ENABLED` | 仓库内未发现集中配置 | 记录为后续发送安全里程碑事项，本轮不新增。 |
| `AUTO_MEDIA_ENABLED` | 仓库内未发现集中配置 | 记录为后续发送安全里程碑事项，本轮不新增。 |
| `AUTO_PASSWORD_ENABLED` | 仓库内未发现集中配置 | 记录为后续发送安全里程碑事项，本轮不新增。 |

## 所需目录

| 目录 | 用途 | 是否预创建 |
| --- | --- | --- |
| `/opt/wecom-room-robot/data/inventory_snapshots_shadow` | Shadow 根目录 | 建议部署前创建，也可由服务首次 Shadow 写入创建。 |
| `/opt/wecom-room-robot/data/inventory_snapshots_shadow/snapshots` | Shadow snapshot artifact | 自动创建。 |
| `/opt/wecom-room-robot/data/inventory_snapshots_shadow/reports` | reconciliation report | 自动创建。 |
| `/opt/wecom-room-robot/data/inventory_snapshots_shadow/runs` | `sync_run_id` 去重 marker | 自动创建。 |
| `/opt/wecom-room-robot/data/inventory_snapshots_shadow/tmp` | SnapshotStore staging | 自动创建。 |

## 目录权限

- systemd 当前仓库声明服务用户为 `root`，因此 root 可写即可运行；服务器现场仍需确认。
- 若后续改为非 root 服务用户，Shadow root 必须由该用户可写。
- private viewing secrets 由 SnapshotStore 写入 snapshot private 子目录；POSIX 下测试覆盖 `0700/0600`，Windows 使用 ACL。
- 不需要给 web/nginx 用户读取 Shadow private 目录。

## 数据库、systemd 和定时器

| 项目 | 是否需要 | 说明 |
| --- | --- | --- |
| 数据库迁移 | 否 | 本项目当前无数据库迁移要求；Shadow artifact 是文件系统产物。 |
| 新生产依赖 | 否 | `requirements.txt` 不变。 |
| systemd service 修改 | 否 | 仍使用现有 `wecom-room-robot.service` 和两个 oneshot 同步 service。 |
| systemd timer 修改 | 否 | 保持现有三次/天频率，不新增 timer。 |
| 客户读取入口切换 | 否 | M1C3 不启用 Snapshot Reader。 |
