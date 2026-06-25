# InventorySnapshot 迁移方案

本文是 M1A 迁移预案，不包含生产代码修改。

## 当前需要收束的入口

- `InventoryService.refresh/all_rows/search/snapshot` 当前直接读写 `data/inventory_cache.csv`、`data/inventory_cache_meta.json` 或 OCR 图片缓存。
- `RegionInventorySyncService.sync` 当前负责飞书源表到目标总表的写入，不产出不可变本地快照。
- `InventoryImageSyncer.refresh_if_changed` 当前直接替换 `room_database/inventory_*.png`。
- `write_rewrite_inventory_index` 当前直接写 `data/rewrite_inventory_index.json`。
- `app/main.py` 在问题重写、工具执行、RAG 动态证据、发送房源表、健康接口和 admin 接口处直接读上述活动文件。

## M1B 建议文件级修改范围

新增文件：

- `app/services/inventory_snapshot.py`：Snapshot Reader、Writer、Manifest、schema 校验、指针读写。
- `app/services/inventory_snapshot_builder.py`：从飞书 values/cache frame 构建标准 listings、CSV、rewrite index 输入和 report。
- `tests/test_inventory_snapshot.py`：核心不可变快照、重复阻断、密码隔离、原子切换测试。

小范围修改：

- `app/config.py`：增加 `inventory_snapshot_root`、`inventory_snapshot_max_age_seconds`、`inventory_snapshot_schema_version`。
- `app/services/inventory.py`：增加 Snapshot Reader 支持，保留旧 reader 作为迁移期 fallback。
- `app/services/rewrite_inventory_index.py`：移除 `room_index[].viewing`，改为状态摘要。
- `app/services/inventory_image_sync.py`：增加“渲染到指定输出目录”的能力，避免直接删写活动 PNG。
- `app/main.py`：在每轮 RAG 开始锁定 `inventory_snapshot_id`，所有库存读入口改走 Snapshot Reader。
- `scripts/refresh_rag_inventory_cache.py`：改为构建 snapshot 并原子切换指针。
- `scripts/sync_feishu_region_inventory.py`：成功同步飞书目标表后触发 snapshot 构建，而不是重复单独写 CSV/index。

暂不修改：

- 客户可见话术。
- 飞书线上结构和企业微信配置。
- 生产依赖。
- 向量数据库。

## 分阶段迁移

### M1B：旁路生成并校验

目标：新 Snapshot Builder 能从当前 `InventoryService.refresh()` 返回的 frame 生成完整快照，但运行时仍可使用旧路径。

验收：

- 生成 `inventory.json`、`inventory.csv`、`rewrite_inventory_index.json`、`sync_report.json`、PNG。
- `rewrite_inventory_index.json` 不含真实密码。
- 失败时不更新 pointer。
- 单元测试覆盖重复房源阻断、字段保真、schema_version、source_hash。

M1B-GATE 当前状态：

- 已实现纯本地 Snapshot Models、Builder、Validator、Store、Reader 和专项测试。
- 未接入飞书生产同步，未切换 `InventoryService`、`RegionInventorySyncService` 或 `app/main.py` 读取路径。
- 公共 manifest 只声明公共 artifact；`private/viewing_secrets.json` 的完整性由 private 目录内 manifest 校验。
- `snapshot_id` 为构建/发布身份，`source_hash` 为确定性内容身份。
- Store 已具备本地原子写、发布锁、路径安全校验和失败回退测试；生产 stale lock 接管、磁盘空间预估和 PNG 渲染接入留待 M1C/M1D 结合定时器实现。

### M1C：运行时只读 Snapshot

目标：`app/main.py` 一轮 RAG 锁定 snapshot，并让 `search/all_rows/snapshot/current_inventory_images` 走同一快照。

验收：

- 同一轮工具、RAG、发送房源表都记录同一 `inventory_snapshot_id`。
- 指针在 turn 中途切换不会影响本轮读到的数据。
- viewing tool 只有显式看房/密码问题才读取 `private/viewing_secrets.json`。

M1C1/M1C2 当前状态：

- 已接入 Shadow 旁路构建，但仅在旧同步成功后运行，不改变运行时 reader。
- `INVENTORY_SNAPSHOT_MODE` 默认仍为 `disabled`；`shadow` 也只写独立 `data/inventory_snapshots_shadow/`。
- M1C2 新增 health/readiness、`sync_run_id` 去重和离线全链路验证，门禁只表示“可进入切换评估”，不自动切换生产 pointer。
- 客服消息回调、Planner、工具执行、自检回流、发送房源表 PNG 仍未读取 Shadow 目录；正式只读 Snapshot 切换仍需后续 M1C 步骤完成。

### M1D：清理旧活动文件直读

目标：确认生产路径不再依赖 `data/inventory_cache.csv`、`data/rewrite_inventory_index.json`、`room_database/inventory_*.png` 活动文件后，删除或降级旧 fallback。

验收：

- 全仓搜索无生产调用方绕过 Snapshot Reader。
- 回归测试、QA 固定集、随机 10 问均通过。
- 服务器全量测试、健康检查、定时器检查通过。

## 数据迁移

- 初始 snapshot 从当前最新库存源生成，不从历史 JSON 猜测。
- 为避免 listing_id 因小区别名变化漂移，M1B 生成 `listing_id_map.json`，首次为空，后续记录 `normalized_key -> listing_id`。
- 迁移期间旧 CSV 和旧 PNG 保留只读，作为回滚用。
- 指针切换后，health 同时报告旧 cache meta 和 snapshot meta，便于对比。

## 回滚

- 若 Snapshot Reader 异常，可通过配置临时回退旧 `InventoryService` 读路径。
- 回滚不得删除快照目录。
- 回滚后继续保留 snapshot 生成任务，但不切换运行时 reader，直到修复。

## 密码迁移

- 旧库存行中的 `看房方式密码` 进入 `private/viewing_secrets.json`。
- `inventory.json` 只保留 `has_viewing_text`、`has_password`、`needs_contact`、`availability_status`。
- structured memory 中候选摘要改为不持久化 `viewing` 原文，只持久化状态摘要。
- 旧上下文文件可能已有历史 `viewing` 字段，M1C 需要读时清洗，不批量改写历史文件。
