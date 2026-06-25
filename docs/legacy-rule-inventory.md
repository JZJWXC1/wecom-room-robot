# Legacy Rule Inventory

本文是 M1A 旧实现审计，不删除生产调用链中的代码。

## 将被 Snapshot 取代的旧同步路径

| 旧路径 | 当前唯一生产入口 | 现状 | removal_milestone | 删除前必须通过的测试 |
| --- | --- | --- | --- | --- |
| `InventoryService._save_cache/_read_cache/_reload_cache_if_file_changed` | `InventoryService.refresh/all_rows/search/snapshot` | 直接读写活动 CSV | M1D | Snapshot Reader 搜索回归、同轮锁定测试、全量 pytest |
| `InventoryService._read_public_document` | `InventoryService.refresh` 的 public document 分支 | 公开文档 fallback | M1D | 飞书源失败保留上一快照、无公开文档依赖测试 |
| `InventoryService._read_image_inventory_text/_parse_image_rows` | `InventoryService.refresh/all_rows/search/snapshot` 的 local_image 分支 | OCR 图片 fallback，会生成 Markdown cache | M1D 或保留为手工灾备 | OCR fallback 不在生产默认路径、密码不入 Prompt 测试 |
| `scripts/refresh_rag_inventory_cache.py` 直接写 CSV/index | systemd `wecom-room-robot-rag-cache-sync.timer` | 只刷新活动 cache 和 rewrite index | M1C | Snapshot 构建脚本测试、timer dry-run、health snapshot 状态 |
| `InventoryImageSyncer._replace_inventory_images` | `InventoryImageSyncer.refresh_if_changed` | 直接替换 `room_database/inventory_*.png` | M1C | PNG 快照目录、失败不切 pointer、发送房源表同快照测试 |
| `write_rewrite_inventory_index` 直接写 `data/rewrite_inventory_index.json` | `app/main.py`、两个脚本 | 非原子写，且当前含 viewing 原文 | M1B | rewrite index 无密码、tmp+replace、schema 校验 |

## 重复生成入口

- `app/main.py::_refresh_inventory`：刷新 `InventoryService` 后调用 `_write_rewrite_inventory_index`。
- `app/main.py::_build_inventory_rewrite_index`：persisted index 缺失时现场调用 `write_rewrite_inventory_index`。
- `scripts/sync_feishu_region_inventory.py::refresh_rewrite_inventory_index`：Region 同步后刷新 cache/index。
- `scripts/refresh_rag_inventory_cache.py::refresh_cache`：定时刷新 cache/index。
- `InventoryImageSyncer.refresh_if_changed`：独立刷新 PNG，与 CSV/index 没有同一原子边界。

Snapshot 后这些入口应统一为“构建候选快照 -> 校验 -> 切 current pointer”。

## 直接读取活动 CSV 或临时文件的调用

- `InventoryService._read_cache` 读 `settings.inventory_cache_path`。
- `InventoryService._reload_cache_if_file_changed` 根据活动 CSV mtime/size 重新加载。
- `InventoryService._read_cache_meta` 读 `settings.inventory_cache_meta_path`。
- `InventoryImageSyncer._current_images` 读 `settings.inventory_image_glob` 和 `settings.inventory_image_path`。
- `app/main.py::_current_inventory_images` 读 `room_database/inventory_*.png` 和 legacy original PNG。
- `load_rewrite_inventory_index` 读 `settings.rewrite_inventory_index_path`。

## main.py 中绕过统一 Snapshot Reader 的入口

- `_inventory_rows_for_resolution` 直接调用 `inventory.all_rows(refresh_if_needed=False)`。
- `_execute_tools` 直接调用 `inventory.search` 和 `inventory.all_rows`。
- `_generate_reply_result` 直接调用 `inventory.format_rows` 和 `inventory.snapshot`。
- `_current_inventory_images` 直接 glob 活动 PNG。
- `/health` 直接返回 `_inventory_cache_meta_for_prompt`。
- `/admin/inventory/refresh`、`/admin/feishu/sync-inventory-image`、`/admin/feishu/sync-region-inventory` 分别刷新不同活动产物。
- startup 直接 `inventory.refresh()`，可改变 cache meta。

## 旧 fallback、repair、override、normalize 函数

| 函数 | 唯一调用方/调用范围 | M1 处理 |
| --- | --- | --- |
| `InventoryService._read_public_document` | `InventoryService.refresh` | Snapshot Builder 后移除生产调用 |
| `InventoryService._read_image_inventory_text` | `InventoryService.refresh` local_image 分支 | 降级为手工灾备或删除 |
| `InventoryService._parse_image_rows` | `all_rows/search` local_image 分支 | Snapshot 后不进入生产 RAG |
| `InventoryService._normalize` | `InventoryService.refresh` | 可迁移到 Snapshot Builder |
| `InventoryService._spreadsheet_values_to_frame` | `_read_feishu_inventory_sheet` 和测试 | 可迁移到 Snapshot Builder |
| `dedupe_rows` | `group_rows_by_community` 和测试 | 改为 duplicate conflict 阻断，不再静默覆盖冲突 |
| `group_rows_by_community` | Region sheet 写入和测试 | 保留用于飞书表排版，不作为 snapshot 去重规则 |
| `RegionInventorySheetSyncer.repair_*` | `sync_target_sheet` | 保留飞书表格式修复，不影响 Snapshot Reader |
| `InventoryImageSyncer._replace_inventory_images` | PNG 渲染结尾 | 改为写 snapshot PNG 目录 |
| `_row_viewing_summary` | main.py 多处回复和自检 | 保留客户显式看房场景，默认不持久化密码 |
| `_viewing_evidence` | main.py viewing 工具证据 | 改为唯一 password-on-demand 工具出口 |
| `kf_context_memory.summarize_row` | context normalize/record | 删除 `viewing` 原文持久化 |

## 可删除死代码

M1A 全仓搜索未确认任何库存相关纯死代码可安全删除；本轮不删除代码。

## M1B 旁路实现记录

M1B 已新增 InventorySnapshot 纯本地核心模型、构建器、校验器、Store 和 Reader，但未接入飞书同步、未切换生产读取路径、未修改客户可见回复。旧路径仍保留在当前生产调用链中，后续由 M1C/M1D 分阶段接管或删除。

| 旧路径 | 当前唯一生产入口 | M1B 状态 | removal_milestone |
| --- | --- | --- | --- |
| `InventoryService._save_cache/_read_cache/_reload_cache_if_file_changed` | `InventoryService.refresh/all_rows/search/snapshot` | 保留；新增 SnapshotReader 不被生产调用 | M1D |
| `InventoryService._read_public_document` | `InventoryService.refresh` 的 public document 分支 | 保留；M1B 不新增线上读取 fallback | M1D |
| `InventoryService._read_image_inventory_text/_parse_image_rows` | `InventoryService.refresh/all_rows/search/snapshot` 的 local_image 分支 | 保留；M1B 不接管 OCR 图片 fallback | M1D 或保留为手工灾备 |
| `scripts/refresh_rag_inventory_cache.py` 直接写 CSV/index | systemd `wecom-room-robot-rag-cache-sync.timer` | 保留；M1B 只提供可测试的本地 Store，不新增生产同步入口 | M1C |
| `InventoryImageSyncer._replace_inventory_images` | `InventoryImageSyncer.refresh_if_changed` | 保留；M1B 仅在 manifest 预留 PNG 路径，不生成生产 PNG | M1C |
| `write_rewrite_inventory_index` 直接写 `data/rewrite_inventory_index.json` | `app/main.py`、两个脚本 | 保留；M1B 新增安全 `rewrite_inventory_index` 产物，但未替换旧生产 index | M1B 安全产物已覆盖；生产替换在 M1C |
| `app/main.py::_current_inventory_images` | 客户请求房源表后的发送阶段 | 保留；M1B 未修改发送阶段 | M1C |
| `app/main.py::_inventory_rows_for_resolution/_execute_tools/_generate_reply_result` | Agentic RAG 工具执行和回复生成 | 保留；M1B 未切换 RAG 库存读取入口 | M1C |

本轮新增代码均有本地单元测试覆盖；未新增第二套可直接被生产调用的同步入口，未新增直接生成客户回复的规则。

## M1B-GATE Legacy Removal Report

本轮仍未接管生产路径，因此不删除旧 `InventoryService`、旧 rewrite index、旧 PNG 和 `app/main.py` 读取入口。M1B-GATE 只删除或迁移新 Snapshot 核心内部的风险点：

- 公共 `manifest.json` 不再声明 `private/viewing_secrets.json`；private 完整性改由 `private/manifest.json` 负责。
- Snapshot Store 不新增 legacy wrapper，不新增生产 feature flag，不调用旧 `InventoryService` 或 `RegionInventorySyncService`。
- Snapshot Reader 不猜测旧活动 CSV、旧 PNG 或目录 mtime；没有 current pointer 时返回结构化 missing。
- M1B 新代码内部统一使用 v1 `snapshot_id`、`listing_id` 和 artifact 相对路径校验，避免后续 M1C 接入时继承路径穿越风险。
- 合并字段、价格解析、secret 脱敏、atomic write、发布锁和 private 权限均在 Snapshot 专项测试中覆盖；这些属于 M1B 新核心，不改变客户回复。

后续 removal_milestone 保持：

| 待移除/迁移旧入口 | removal_milestone | M1C/M1D 前置门槛 |
| --- | --- | --- |
| 旧 `data/rewrite_inventory_index.json` 生产读取 | M1C | 同轮锁定 snapshot rewrite index，未问密码不进 Prompt/记忆 |
| 旧 `room_database/inventory_*.png` 发送入口 | M1C | 房源表发送从同快照 `png/` 读取并通过发送阶段回归 |
| `InventoryService` 活动 CSV 读写和 cache meta | M1D | Snapshot Reader 搜索/all_rows/snapshot 行为回归，旧 fallback 删除前全量测试通过 |
| OCR/local image 库存 fallback | M1D 或手工灾备 | 明确不在生产默认 RAG 路径，灾备调用需单独权限和测试 |
| Region/Feishu 同步后直接刷新 cache/index | M1C | 同步成功只构建候选快照，校验通过后切 pointer |

## M1C1 Shadow 集成记录

本轮新增 `INVENTORY_SNAPSHOT_MODE` 临时模式开关，取值仅允许 `disabled` 和 `shadow`，默认 `disabled`。该开关不切换生产读取入口；`shadow` 只在旧同步已经生成活动 cache/rewrite index 后，复用同一批结构化 rows 构建 `data/inventory_snapshots_shadow/` 下的 Shadow Snapshot、差异报告和 `shadow_current_snapshot.json`。不得写入 `data/inventory_snapshots/current_snapshot.json`。

`INVENTORY_SNAPSHOT_MODE` 是 M1C1 到 M1D 的迁移开关，removal_milestone=M1D；到 M1D 时必须决定删除、或转为正式 Snapshot 读取配置，不允许继续与生产 reader feature flag 重叠。

`LegacyInventoryToSnapshotAdapter` 是 M1C1 唯一旧字段到 Snapshot 输入字段的适配边界，唯一调用方为 `InventorySnapshotShadowCoordinator`。它只做字段映射，不请求飞书、不生成客户回复、不复制旧 normalizer、不实现第二套业务归一规则。该 adapter removal_milestone=M1D。

M1C1 保留的旧入口与原因：

| 入口 | M1C1 状态 | removal_milestone | 覆盖 |
| --- | --- | --- | --- |
| `scripts/refresh_rag_inventory_cache.py` | 旧 cache/index 成功后非阻断触发 Shadow | M1C/M1D | M1C1 Shadow 专项测试、全量 pytest |
| `scripts/sync_feishu_region_inventory.py::refresh_rewrite_inventory_index` | 旧 region 同步成功后沿原流程刷新 cache/index，再非阻断触发 Shadow | M1C/M1D | M1C1 Shadow 专项测试 |
| `app/main.py::_refresh_inventory` | 仅 admin refresh helper 追加非阻断 Shadow 结果，不改客服回复 | M1C/M1D | 全量 pytest |
| 旧 `data/rewrite_inventory_index.json` | 仍是生产 rewrite 入口；Shadow 只读取本次生成文件做对比 | M1C | viewing 脱敏报告测试 |
| 旧活动 CSV 和旧 PNG | 继续作为唯一生产事实源和客户发送来源 | M1D/M1C | 全量 pytest |

Shadow 失败、超时、报告写失败、reconciliation blocking 和并发冲突都不得改变旧同步返回值，不得删除旧产物，不得更新生产 pointer。Shadow 报告只允许记录 `has_password` 和 `password_match` 布尔，不写真实密码、完整 viewing 原文、token、手机号或开发机绝对路径。

## M1C2 Shadow 健康和离线验证记录

M1C2 仍不切换生产读取路径。新增 `InventorySnapshotShadowHealth` 只读取 Shadow 独立目录中的 `shadow_status.json`、公开 snapshot artifact 和安全扫描结果，用于回答“是否具备进入切换评估”的健康状态；它不写 `data/inventory_snapshots/current_snapshot.json`，也不修改 `INVENTORY_SNAPSHOT_MODE`。

新增 `sync_run_id` 去重门禁：同一个旧同步 run 只能执行一次 Shadow，重复 run 只返回 `duplicate_skipped`，不再构建第二份 snapshot，不累计连续通过次数。连续通过门禁按不同 `source_hash` 计数，同一份房源内容即使用不同 `sync_run_id` 重跑也不能把 `consecutive_passes` 刷高。出现 blocking reconciliation 或 Shadow error 时，`consecutive_passes` 归零并记录 failure。

新增 `InventorySnapshotOfflineComparisonRunner` 作为离线测试工具，显式以 `mode="shadow"` 运行，输入为脱敏飞书 values fixture。该 Runner 只生成安全摘要、Shadow report 和 health artifact，不连接飞书、不读生产 CSV、不写生产 pointer。它用于 M1C2 全链路验证，不是线上同步入口。

M1C2 保留的旧入口与原因：

| 入口 | M1C2 状态 | removal_milestone | 覆盖 |
| --- | --- | --- | --- |
| `scripts/refresh_rag_inventory_cache.py` | 旧 cache/index 成功后传入唯一 `sync_run_id` 非阻断触发 Shadow | M1C/M1D | M1C1/M1C2 Shadow 测试 |
| `scripts/sync_feishu_region_inventory.py::refresh_rewrite_inventory_index` | Region 旧流程成功后传入唯一 `sync_run_id` 非阻断触发 Shadow | M1C/M1D | M1C2 脚本测试 |
| `app/main.py::_refresh_inventory` | admin helper 传入唯一 `sync_run_id`，客服消息路径不调用 Shadow | M1C/M1D | M1C2 AST 调用范围测试 |
| `InventorySnapshotOfflineComparisonRunner` | 离线测试入口，强制 Shadow 模式但只写测试 artifact root | M1C2 后可保留为回归工具 | M1C2 fixture 全链路测试 |

M1C2 后，若要进入正式切换评估，至少需要 Shadow 模式、最近一次 reconciliation 无 blocking、公开产物扫描通过、状态未过期、连续不同 `source_hash` 通过次数达到门槛、且最近一次无 error。该状态仅表示“可进入人工/后续门禁评估”，不是自动切生产。

## M1C3 Shadow 发布预检 Legacy Removal Gate

M1C3 只准备 Shadow 生产观察的部署清单、配置、观察工具、preflight 和回滚方案；仍禁止删除旧生产路径，因为旧路径继续承担客户事实读取。

| Legacy 路径 | M1C3 状态 | M1D 候选处理 |
| --- | --- | --- |
| 旧 `data/inventory_cache.csv` | 保留，仍由 `InventoryService` 和旧同步使用 | M1D 候选删除；前提是 Snapshot Reader 已成为唯一生产库存读取源且回归通过。 |
| 旧 `data/rewrite_inventory_index.json` | 保留，RAG rewrite/实体解析仍读取旧 index | M1D 候选删除；前提是同轮锁定 snapshot rewrite index 已上线并覆盖测试。 |
| 旧 `room_database/inventory_*.png` 生成/发送 | 保留，客户要房源表仍依赖旧 PNG 发送入口 | 是否删除取决于客户发房源表需求；若 Snapshot PNG 接管发送阶段，旧 PNG 才能降级或删除。 |
| 旧 `InventoryService` 读取 | 保留，客户查询、工具执行和 health 仍依赖 | M1D 候选删除或降级为灾备；前提是 `search/all_rows/snapshot` 全部切 Snapshot Reader。 |
| Shadow mode 临时兼容代码 | 保留，用于 M1C3 观察和门禁 | M1D 决策：删除、改为正式 Snapshot 发布路径、或保留为只读审计工具。 |
| `LegacyInventoryToSnapshotAdapter` | 保留，Shadow 唯一旧字段适配边界 | M1D 删除；前提是同步源直接输出 Snapshot 标准字段。 |

M1C3 内部收束：

- Shadow mode 解析继续集中在 `parse_inventory_snapshot_mode`，禁止在 CLI 或脚本中自行解析 `primary`。
- Shadow health 读取集中由 `get_inventory_snapshot_shadow_health` 和 `inventory_snapshot_shadow_observer` 复用。
- CLI 人类输出和 JSON 输出复用同一套安全观察 payload，避免重复格式化逻辑。
- Preflight 只做只读检查，不构建真实 Snapshot，不写 production pointer，不启动同步，不连接飞书或企业微信。

## M1C3-FIX1 First Shadow Blocking Legacy Removal Gate

M1C3-FIX1 只修复首次服务器 Shadow reconciliation 中 `rewrite_index_mismatch.communities` 的误判风险，不删除当前生产读取路径。

本轮删除/替代的新 Shadow 内部重复点：

- 删除 raw display-name community map 的比较语义，替换为标准化 community bucket；重复小区项单独输出 warning。
- 删除 layouts fallback 的 raw community key 选择，替换为与 community bucket 相同的标准化 key。
- 删除 `load_legacy_rewrite_index` 的 path-first current index 选择；当调用方已经传入本轮 in-memory index 时，不再读取 path 覆盖它。

本轮明确保留到 M1D 或后续决策：

| Legacy 路径 | M1C3-FIX1 状态 | M1D 候选处理 |
| --- | --- | --- |
| 旧 `data/inventory_cache.csv` | 保留，仍承担 `InventoryService` 生产读取 | M1D 候选删除 |
| 旧 `data/rewrite_inventory_index.json` | 保留，RAG rewrite/实体解析仍读取旧 index | M1D 候选删除 |
| 旧 `room_database/inventory_*.png` | 保留，客户要房源表仍走旧发送入口 | 是否删除取决于 Snapshot PNG 是否接管客户发房源表需求 |
| 旧 `InventoryService` 读取 | 保留，客户查询仍依赖 | M1D 候选删除或降级为灾备 |
| Shadow mode 临时兼容代码 | 保留，用于观察和门禁 | M1D 决策 |
| `LegacyInventoryToSnapshotAdapter` | 保留，仍是 Shadow 唯一旧字段适配边界 | M1D 删除 |

未删除 `normalize_search_text`、`canonical_community_display`、`InventoryService._normalize` 等旧生产 normalize 函数，因为它们仍属于当前客户查询/RAG 或旧同步链路。M1C3-FIX1 的唯一新增约束是：Shadow rewrite index reconciliation 的 community 身份比较归属 `normalize_listing_identity`，不得再直接比较 display name 集合。

## 删除前总门槛

- `pytest -q` 通过。
- `python -m compileall app` 通过。
- Agentic RAG 固定连续对话覆盖集通过。
- 随机 10 问保底通过。
- 服务器部署后健康接口、服务状态、两个定时器状态、无人值守凭证完整性检查通过。
