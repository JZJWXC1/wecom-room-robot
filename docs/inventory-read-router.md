# Inventory Read Router

本文记录 M1D1 本地读取路由底座，以及 M1D2A 将同一读取 Context 接入客服 RAG turn 的本地门禁。本轮不部署服务器，不启用服务器 primary，不改变客户回复。

## 范围

新增模块：

- `app/services/inventory_read_models.py`：请求级 Context、Decision、Health、Error、Evidence 和一致性校验。
- `app/services/inventory_read_provider.py`：Legacy/Snapshot Provider 适配层。
- `app/services/inventory_read_router.py`：唯一 source selection 入口和本地 primary readiness/fallback 决策。
- `app/services/inventory_read_turn.py`：客服 turn 的最小编排适配层，集中创建 Context、读取 Provider rows/index/meta、整理安全 Evidence。
- `tests/test_inventory_read_router.py`：本地路由、Provider、readiness、fallback、parity 和生产隔离测试。
- `tests/test_inventory_read_turn.py`：turn adapter 的本地等价、安全 evidence 和清空事实测试。

M1D2A 不修改：

- Planner、LLM Prompt、自检、发送阶段。
- 生产 `INVENTORY_SNAPSHOT_MODE`。
- 旧 `InventoryService`、活动 CSV、旧 rewrite index、旧 PNG。

## 读取契约

`InventoryReadRouter` 是唯一负责 source selection 的入口。它在请求或 RAG turn 开始时生成不可变 `InventoryReadContext`，随后本轮所有库存 Provider 调用必须携带这个 Context。

`InventoryReadContext` 包含：

- `request_id`
- `turn_id`
- `source_kind`
- `snapshot_id`
- `source_hash`
- `schema_version`
- `selected_at`
- `fallback_used`
- `fallback_reason`
- `health_at_selection`
- `selection_mode`
- `decision_id`

Context 使用 frozen dataclass，并冻结嵌套日志字段，调用方不能在 turn 中途任意改写。日志序列化只输出安全元数据。

## Provider

`LegacyInventoryReadProvider` 复用现有 `InventoryService.search/all_rows/cache_meta`，不复制 CSV 查询实现，不改变现有排序、预算过滤、户型语义、押一付一/押二付一字段含义或备注水电语义。

`SnapshotInventoryReadProvider` 只通过 `SnapshotReader.get_snapshot(context.snapshot_id)` 读取 Context 锁定的快照，不猜测最新目录，不读取 `private/viewing_secrets.json` 作为普通查询结果。

普通 Provider 接口不返回：

- `viewing_password`
- 完整 viewing 原文
- 手机号
- token
- 私密链接

密码仍留给未来独立 viewing tool 受控读取。

## Evidence

`InventoryListingEvidence` 输出安全字段：`evidence_id`、`listing_id`、`source_kind`、`snapshot_id`、`source_hash`、`schema_version`、`area`、`community`、`room_no`、`layout_desc`、`layout_type`、`rent_pay1`、`rent_pay2`、`utility_summary`、`availability_summary`、`has_image`、`has_video`、`fetched_at`。

`evidence_id` 由 `decision_id + listing_id + source metadata` 生成，单请求内稳定，不使用 Python 随机 hash。

## 模式

`disabled`：

- 只选择 `LegacyInventoryReadProvider`。
- 不读取 Snapshot health。
- 行为保持当前生产读取语义。

`shadow`：

- 客户可见结果仍只来自 Legacy。
- Router 可以读取 Snapshot health 作为选择时元数据。
- Snapshot 异常不影响 Legacy 请求结果。
- 不把 Snapshot 字段混入 Evidence。

`primary`：

- 仅用于本地测试。
- 只有完整 readiness 门禁通过才选择 Snapshot。
- 本轮不接入生产配置，不让服务器进入 primary。

非法模式返回 `invalid_inventory_read_mode`，不静默降级。

## Primary Readiness

本地 primary 必须同时满足：current pointer 存在、pointer 指向完整 Snapshot、manifest 合法、artifact hash 和 size 校验通过、schema_version 受支持、Snapshot health 未 stale、`reconciliation_passed=true`、`blocking_count=0`、`public_artifact_secret_scan_passed=true`，以及 alias coverage 五项全为 0。

任一失败返回结构化原因码，不使用 LLM 判断 readiness。

## Fallback

默认策略是 `strict`：Snapshot 不健康时直接返回结构化错误，不自动切 Legacy。

`legacy_whole_request` 只允许在请求开始、尚未读取任何业务事实时整体退回 Legacy。fallback 会创建新的 Legacy Context，并记录 `fallback_reason`。一旦 `InventoryReadSession` 开始读取业务事实，后续 fallback 会被 `fallback_not_allowed_after_read` 阻断。

## 一致性保护

已实现保护：

- Context.source_kind 与 Provider 不匹配时阻断。
- Context.snapshot_id 与实际 Snapshot 不匹配时阻断。
- 一个结果集合出现多个 source_hash 时阻断。
- 同一 turn 出现 Legacy 和 Snapshot evidence 时阻断。
- Snapshot 更新后旧 Context 仍读取原 snapshot_id。
- 旧 snapshot 被删除时返回结构化错误，不切换到新 Snapshot。
- Context 只能由 Router 生成。

## M1D2A 客户链路接入

`app/main.py` 已在客服 RAG turn 起点创建唯一 `InventoryReadContext`，并将同一个 context 传给问题重写、库存搜索/详情工具和最终回复生成前的工具证据门禁。`app/main.py` 只保留薄编排 wrapper；Router 构造、Provider 选择、Snapshot 禁用、Evidence 转换和清空事实逻辑集中在 `app/services/inventory_read_turn.py`。

当前客户路径只允许：

- `disabled`：选择 `LegacyInventoryReadProvider`。
- `shadow`：客户可见结果仍选择 `LegacyInventoryReadProvider`，且聊天路径不探测 `SnapshotReader`。
- 其他值（包括 `primary`）：明确回退到 `disabled`，不切换 Snapshot。

本轮已迁移的读取点：

- rewrite/entity resolution 读取 inventory rows。
- rewrite inventory index 读取。
- 工具层 `search_inventory`、房源详情候选、按原始房号兜底匹配、最近候选查询兜底。
- 工具证据写入 `inventory_read_context`、`inventory_source_metadata` 和脱敏 `inventory_listing_evidence`。
- Provider 读取失败或 Evidence 与 Context 不一致时，不再回退到同位置旧直接读取；客户可见房源事实和素材待发路径会被清空。

本轮仍不接入：

- 房源表 PNG、图片/视频素材、企业微信发送。
- 看房密码专用读取。
- Snapshot primary 生产切换。
- 无 rows 时 RAG 背景 `inventory.snapshot` fallback。

## Legacy Removal Report

本轮只接入读取路由，不接管生产主源，因此保留 `InventoryService`、活动 CSV、旧 rewrite index、旧 PNG、`app/main.py` 发送入口和当前素材调用方。

| 兼容项 | 唯一调用方 | 保留原因 | removal_milestone | 不参与生产证明 |
| --- | --- | --- | --- | --- |
| `LegacyInventoryReadProvider` | `inventory_read_turn.py` disabled/shadow 客户读路径、专项测试 | primary 切换前需要 Legacy 契约适配和 parity 比较，保证客户结果与旧行为一致 | M1D 后生产切换完成再决策 | 客户 context.source_kind 仍为 legacy |
| `SnapshotInventoryReadProvider` | 本地 Router/专项测试 | primary 本地 readiness 和快照读取验证 | M1D/M1E 接入生产前复审 | 客服 `app/main.py` 不直接引用，disabled/shadow 不读 Snapshot |
| `legacy_whole_request` fallback | 本地 Router/专项测试 | 明确整体 fallback 语义，禁止半途混用 | primary 策略确定后复审 | 客户路径未启用 primary/fallback |

## 测试

`tests/test_inventory_read_router.py` 覆盖 disabled/shadow/primary、readiness、fallback、Context 锁定、Snapshot 更新和删除、Provider/Context mismatch、source_hash 混入、Legacy/Snapshot parity、字母房号、中文和全角符号、敏感字段边界、客户路径不引用 `SnapshotInventoryReadProvider`、不访问网络和不写生产 data 目录。

`tests/test_wecom_kf.py::InventoryReadRouterIntegrationTests` 覆盖客服 RAG turn 同一 context 贯穿 rewrite/tools、shadow 聊天不探测 Snapshot，以及 Evidence/source_hash 不一致时清空客户可见房源事实。

其中 `test_process_text_turn_selects_router_once_and_reuses_decision_id` 证明单 turn 只 select context 一次，rewrite 与工具使用同一 `decision_id/source_hash`；`test_provider_failure_does_not_fallback_to_direct_rewrite_reads` 证明已迁移位置失败时不会绕回旧 metadata/index 直接读取或写入。
