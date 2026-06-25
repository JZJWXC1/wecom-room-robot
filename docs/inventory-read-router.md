# Inventory Read Router

本文记录 M1D1 本地读取路由底座。本轮只新增本地可测契约，不接入当前客服生产路径，不部署服务器，不启用服务器 primary。

## 范围

新增模块：

- `app/services/inventory_read_models.py`：请求级 Context、Decision、Health、Error、Evidence 和一致性校验。
- `app/services/inventory_read_provider.py`：Legacy/Snapshot Provider 适配层。
- `app/services/inventory_read_router.py`：唯一 source selection 入口和本地 primary readiness/fallback 决策。
- `tests/test_inventory_read_router.py`：本地路由、Provider、readiness、fallback、parity 和生产隔离测试。

本轮不修改：

- `app/main.py` 客服消息链路。
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

## 生产隔离

本轮新增代码不被 `app/main.py` 客户路径引用。专项测试确认 `app/main.py` 未导入或构造 `InventoryReadRouter`，当前 Planner、房源工具和企业微信回调未切换到 Snapshot，默认生产行为仍使用旧 `InventoryService`。

## Legacy Removal Report

本轮尚未接管生产，因此保留 `InventoryService`、活动 CSV、旧 rewrite index、旧 PNG、`app/main.py` 当前读取入口和当前工具调用方。

| 兼容项 | 唯一调用方 | 保留原因 | removal_milestone | 不参与生产证明 |
| --- | --- | --- | --- | --- |
| `LegacyInventoryReadProvider` | 本地 Router/专项测试 | primary 切换前需要 Legacy 契约适配和 parity 比较 | M1D 后生产切换完成再决策 | `app/main.py` 未引用 |
| `SnapshotInventoryReadProvider` | 本地 Router/专项测试 | primary 本地 readiness 和快照读取验证 | M1D/M1E 接入生产前复审 | `app/main.py` 未引用 |
| `legacy_whole_request` fallback | 本地 Router/专项测试 | 明确整体 fallback 语义，禁止半途混用 | primary 策略确定后复审 | 当前生产未调用 Router |

## 测试

`tests/test_inventory_read_router.py` 覆盖 disabled/shadow/primary、readiness、fallback、Context 锁定、Snapshot 更新和删除、Provider/Context mismatch、source_hash 混入、Legacy/Snapshot parity、字母房号、中文和全角符号、敏感字段边界、生产隔离、不访问网络和不写生产 data 目录。
