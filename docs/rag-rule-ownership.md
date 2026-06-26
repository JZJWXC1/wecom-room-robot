# RAG Rule Ownership

本文更新 M1A 审计后的规则归属，约束后续 Snapshot 修改不绕过 Agentic RAG。

## 链路归属

| 阶段 | 当前职责 | Snapshot 后职责 |
| --- | --- | --- |
| 问题重写/意图分析 | `_understand_message` 读取库存行和 rewrite index 辅助实体解析 | 锁定 `inventory_snapshot_id`，读取同快照 rewrite index |
| Planner/工具 | `_plan_actions` 决定 `search_inventory/send_inventory_sheet/explain_unavailable_viewing` 等动作 | Planner 不直接读库存，只声明工具需求 |
| 工具执行 | `_execute_tools` 调用 `inventory.search/all_rows`、素材库和 viewing 规则证据 | 统一通过 Snapshot Reader 和 viewing tool，所有结果带 snapshot_id |
| 结构化会话记忆 | `kf_context_memory` 保存候选、确认房源和 query state | 只保存 listing_id、展示摘要、snapshot_id、非敏感状态 |
| 自检回流 | `agentic_rag.assess_reply` 和本地 selfcheck 拦截错绑、无证据、未问密码 | 校验回复引用的 listing_id 均来自本轮 snapshot；未问密码不得出现密码 |
| 发送阶段 | `_send_final_actions` 发送文本、图片、视频和房源表 PNG | 房源表 PNG 从同快照 `png/` 读取 |
| 房源/素材同步 | 现有飞书同步、cache、rewrite index、PNG 分散生成 | Snapshot Builder 统一生成并原子切换 |

## 密码规则归属

- rewrite index：只允许保存 `has_password`、`needs_contact`、`availability_status`，不允许保存真实密码。
- Inventory Reader：默认返回标准字段和非敏感状态，不返回 `viewing_text`。
- Viewing Tool：唯一可以读取真实 `viewing_text` 的工具；必须有显式 viewing intent 和已绑定 listing。
- LLM Prompt：未显式问看房/密码时，只能看到“看房方式字段：有”这类状态文本。
- 结构化记忆：不得持久化真实 `viewing_text`。

## 不允许的绕行

- 不允许在 `app/main.py` 新增直接读取 CSV、PNG 活动路径或 private secrets 的分支。
- 不允许为了发房源表绕过 RAG 流程；发送动作必须来自 Planner/工具证据或既有确定性 inventory_sheet 规则。
- 不允许让 rewrite 层根据旧 index 声称房源不存在；未验证的 not found 必须路由到工具。
- 不允许把旧固定规则升级为主要事实源；固定规则只能做安全阀。

## M1C1 Shadow 归属

M1C1 属于“房源/素材同步”阶段，只在旧同步完成结构化 rows 和旧 rewrite index 生成后运行 `InventorySnapshotShadowCoordinator`。Shadow 产物写入独立 `data/inventory_snapshots_shadow/` 根目录，聊天链路、RAG、Planner、工具执行、自检回流和发送阶段均不得读取该目录。

本轮没有切换 `InventoryService`、`load_rewrite_inventory_index`、房源表 PNG 发送入口或 viewing tool。`app/main.py` 只在 admin refresh helper 返回 Shadow 状态；不新增客服规则、不新增客户可见回复分支。

`LegacyInventoryToSnapshotAdapter` 的规则归属是“房源/素材同步”的临时字段映射，removal_milestone=M1D。它不是事实 reader，也不是 Planner/RAG 工具。

## M1C2 Shadow Health 归属

M1C2 新增的 Shadow health、`sync_run_id` 去重和 OfflineComparisonRunner 仍只属于“房源/素材同步/测试覆盖”阶段：

- Shadow health 只用于运维和迁移门禁判断，不给 Planner 提供库存事实。
- `ready_for_cutover_evaluation` 只表示后续可以进入人工/发布门禁评估，不表示已切换客服读取路径。
- OfflineComparisonRunner 只读取测试 fixture values，输出安全摘要和测试 artifact，不读取飞书、不连接企业微信、不读写生产活动 CSV/PNG。
- `app/main.py` 中唯一 Shadow 调用仍限定在 `_refresh_inventory` admin helper；客服消息回调、RAG Planner、工具执行、自检回流和发送阶段都不得调用 `run_inventory_snapshot_shadow` 或读取 Shadow 根目录。

因此 M1C2 不改变客户可见回复，不改变 viewing tool 权限，不改变房源表 PNG 发送入口，也不允许用 Shadow report 作为“房源存在/不存在”的客服证据。

## M1C3 发布预检归属

M1C3 新增的 `scripts/check_inventory_snapshot_shadow.py` 和 `scripts/preflight_inventory_snapshot_shadow.py` 只属于运维观察和发布门禁。它们不得被客服消息处理、RAG Planner、工具执行、自检回流或发送阶段调用，也不得把 Shadow report 结果作为客户可见事实来源。

M1C3 仍保持：

- `INVENTORY_SNAPSHOT_MODE` 默认 `disabled`，生产观察期最多设置为 `shadow`。
- 客户查询继续读取旧 `InventoryService`、旧 rewrite index、旧 PNG。
- Shadow root 与正式 production Snapshot root 隔离。
- 不新增客户回复分支、不新增绕过 RAG 的事实判断。

## M1C3-FIX1 Shadow Reconciliation 归属

M1C3-FIX1 只修复 Shadow reconciliation 对旧 rewrite index 与 Snapshot safe rewrite index 的离线一致性判断，不改变 RAG 客服链路：

- community 集合比较统一使用 `normalize_listing_identity` 作为 Snapshot Shadow 对比身份，避免 display name、排序、Unicode 空格和全半角差异被误判为缺失。
- `area_aliases` 仍单独作为 rewrite index warning 比较项，不进入 community 标准集合。
- 旧 rewrite index 的 `viewing` 原文字段仍只产生 warning；报告不得输出真实 viewing、密码、手机号或 token。
- 当 Shadow 调用方已经传入本轮 in-memory legacy index 时，reconciliation 优先使用该 index，避免 path 指向历史文件时混入当前批次。
- 本修复不新增客户消息调用点，不接入 Snapshot Reader，不切换生产读取入口。

## M1D1 Inventory Read Router 归属

M1D1 新增 `InventoryReadRouter`、`LegacyInventoryReadProvider`、`SnapshotInventoryReadProvider`、`InventoryReadContext` 和 `InventoryListingEvidence`，归属为“房源读取契约/测试覆盖”的本地底座，不接入当前客服生产链路。

职责边界：

- Router 唯一负责 source selection。
- Provider 唯一负责读取与 Evidence 转换。
- LLM1 不得选择数据源。
- LLM2 不得选择数据源。
- 工具层不得自行更换数据源。
- selfcheck 不得修改 Context。
- `app/main.py` 不得再次实现 source selection。

M1D1 仍保持：

- 问题重写/意图分析继续使用旧 `InventoryService` 和旧 rewrite index。
- Planner 只声明工具需求，不调用新 Router。
- 工具执行继续使用旧 `inventory.search/all_rows`。
- 结构化会话记忆未切换 snapshot_id。
- 自检回流未读取新 Evidence。
- 发送阶段继续使用旧 PNG。

M1D1 的生产隔离在 M1D2A 后升级为“接入但不切 Snapshot”，由 `tests/test_inventory_read_router.py::test_production_customer_path_uses_read_router_without_snapshot_reader` 覆盖。

## M1D2A Inventory Read Router 接入归属

M1D2A 将 `InventoryReadRouter` 接入客服 RAG turn 起点，但不接 primary、不部署、不改变客户回复。Router 构造、Provider 选择、Snapshot 禁用和 Evidence 整理集中在 `app/services/inventory_read_turn.py`，`app/main.py` 只负责创建一次 Context 并沿 rewrite/tools/selfcheck 编排传递。

归属变更：

- 问题重写/意图分析：`_understand_message` 通过同一 `InventoryReadContext` 读取 resolution rows 与旧 rewrite index，prompt 内容保持旧生产等价。
- Planner：不选择数据源，仅继续消费 rewrite 结果和工具需求。
- 房源事实工具：`_execute_tools` 的库存搜索、原始房号兜底、候选集兜底读取改为 `LegacyInventoryReadProvider`，原始 row 仍按旧 `InventoryService` 结果返回。
- 结构化会话记忆：候选集和确认房源记录 `inventory_cache_meta` 改为本轮 Provider metadata，并在 candidate_state 摘要保留 `decision_id/source_kind/source_hash`。
- 自检回流/发送阶段：工具 evidence 出现 source/context 不一致时清空房源事实、图片/视频待发路径；未切换发送动作。
- 安全兜底：Provider 失败或 consistency failure 不再触发同位置旧直接读取，不让旧结果覆盖 Provider 结果。

当前保持：

- 客户路径 `disabled`/`shadow` 的 `context.source_kind` 均为 `legacy`。
- `shadow` 聊天路径不调用 `SnapshotReader`，本轮不做双读。
- `primary` 配置不会在客服聊天中启用，会明确回退到 `disabled`。
- 房源表 PNG、图片/视频素材、看房密码专用读取、企业微信发送、部署均未纳入本轮。

测试证明：

- `tests/test_wecom_kf.py::InventoryReadRouterIntegrationTests`
- `tests/test_inventory_read_turn.py`
- `tests/test_inventory_read_router.py::test_production_customer_path_uses_read_router_without_snapshot_reader`
- `tests/test_inventory_read_router.py::test_shadow_chat_mode_can_skip_snapshot_health_probe`

## M1D2B1 Sensitive Tool Ownership

M1D2B1 仍属于 RAG 工具执行层和发送准备层的读取契约收口，不属于 Planner、LLM Prompt、自检规则或发送语义变更。

归属变更：

- Viewing Tool：`app/services/inventory_sensitive_access.py` 成为真实 viewing 文本的唯一受控读取边界。必须有同一 turn 的 `InventoryReadContext`、绑定 `listing_id`、显式 viewing/password 目的和一致的 `decision_id`。
- Sheet Artifact Tool：房源表 PNG 读取从 `_execute_tools` 中的直接刷新/列举，收口为 `sheet_artifacts_for_context`。Legacy provider 继续调用旧刷新和旧 PNG 列表，Snapshot provider 只读 Context snapshot manifest。
- Tool Evidence：看房证据和房源表 artifact evidence 都带 `decision_id/source_kind/source_hash/schema_version`。普通摘要、日志和 RAG knowledge context 只能保存脱敏版本。

保持不变：

- Planner action 判断不变。
- LLM Prompt 文本不变。
- 客户回复、欢迎语、澄清话术、安全兜底不变。
- 图片、视频、原视频、PNG 实际发送路径不变。
- 水电、价格字段解释和候选排序不变。

新增测试证明：

- `tests/test_inventory_sensitive_access.py`
- `tests/test_wecom_kf.py::MainAgenticRagFlowTests::test_tool_evidence_summary_redacts_viewing_secret`
- `tests/test_wecom_kf.py::MainAgenticRagFlowTests::test_video_action_does_not_create_viewing_instruction_evidence`
- `tests/test_wecom_kf.py::MainAgenticRagFlowTests::test_send_inventory_sheet_uses_artifact_evidence`

## G 线：Inventory/Snapshot Router 收束

G 线继续保持 `InventoryReadRouter` 是唯一 source selection 入口。Provider 只按传入的 `InventoryReadContext` 读取，不自行做 primary/legacy fallback 判断；`disabled` 和客服 `shadow` 路径固定选择 legacy，且 `inventory_read_turn` 使用禁用的 Snapshot Provider，聊天路径不得探测或读取 Snapshot primary。

普通房源 evidence 必须可追踪到 `decision_id/source_kind/source_hash/listing_id/snapshot_id`，同一 turn 的 evidence 不允许混用 source 或 decision。Snapshot 缺失、source 不可用和 whole-request fallback 必须以 `missing_snapshot`、`source_unavailable`、`fallback_used` 等结构化 reason 呈现，禁止字段级静默混用。看房方式/密码继续只归属 `inventory_sensitive_access.py`，普通 evidence、prompt、记忆和公开 artifact 只保留脱敏摘要。

## M1D2B2 Local Primary Rehearsal Ownership

M1D2B2 属于“房源读取契约/切换门禁/测试覆盖”和“运维 runbook”层，不属于客户回复、Planner、LLM Prompt、自检规则或发送语义变更。

归属：

- `inventory_snapshot_cutover.py`：本地 primary replay、cutover readiness evaluator、rollback rehearsal、性能摘要和 Legacy Removal Report。
- `inventory_snapshot_shadow.scan_public_artifacts_for_sensitive_text`：offline、shadow、primary replay 共享的 public artifact 安全扫描语义。
- `tests/test_inventory_snapshot_m1d2b2.py`：primary replay、fault injection、secret scanner、PreparedOutboundPackage/send action metadata、并发隔离和 rollback 专项。

保持不变：

- 客服生产路径不启用 primary。
- `app/main.py` 不新增 source selection、fallback 规则、候选编号解析、alias 归一或固定回复。
- Planner 只声明工具需求，不参与数据源选择。
- LLM Prompt 和 selfcheck profile 不变。
- 发送阶段仍使用既有文本/PNG/图片/视频动作语义。

M1D2B2 的 Cutover Readiness 只表示“本地证据足以进入后续人工切换评估”；没有 `APPROVE_DEPLOY` 时不得把该结果用于服务器配置、生产 pointer 切换或客服读取入口切换。

## RAG V2 D 线 Dual LLM Shadow 归属

D 线新增的 `app/services/kf_dual_llm_shadow.py` 只属于“问题重写/意图分析、Planner、发送准备的结构化契约 shadow 适配层”，不属于生产客服回复链路。它把 legacy rewrite/planner/tool evidence 适配为未来 LLM1/LLM2 的强类型 shadow 数据：

- LLM1 shadow 记录 `task_atoms`、约束、候选绑定摘要、`response_strategy` 和 `tool_plan`。
- LLM2 shadow 记录候选绑定摘要、`response_strategy`、`claims`、`send_actions` 和 `self_review`。
- shadow LLM2 只表达 legacy 文本和程序 evidence，不能决定视频、图片、密码或房源表目标；这些动作只能来自工具 evidence/program。
- 本轮不接入 `app/main.py`，不调用真实 LLM，不访问网络，不连接飞书、企业微信或服务器。
- 输出 artifact、repr 和测试 JSON 必须通过契约脱敏，不能包含真实密码、手机号、token、raw tool result 或客户原文敏感字段。

本归属不改变客户可见回复、不改变 Planner/Prompt/selfcheck/send 语义，不删除旧 LLM 阶段，也不修改客户回复 golden。

## M1.5 契约对齐归属

M1.5 只补齐终态两 LLM 所需的强类型契约，归属为“问题重写/意图分析、Planner/工具证据、发送准备、自检回流的结构化数据边界”，不属于生产客服回复链路切换。

- `ResponseStrategy` 从 legacy mode 字符串扩展为结构化策略，保留旧字符串、枚举常量和 `strategy` alias 输入兼容。
- `Claim` 补齐字段级事实声明，绑定 `task_id/listing_id/field/value/evidence_ref/text_span/sensitivity`。
- `EvidenceItem` 和 `ToolEvidenceBundle` 补齐 `source_record_id/field_values/sensitivity/fetched_at`，继续禁止 raw tool result 出现在安全输出中。
- `PreparedOutboundPackage` 补齐 `answered_task_ids/action_captions/missing_items/self_review/selfcheck_profile`。
- `app/services/kf_dual_llm_shadow.py` 只做 legacy 数据到新契约字段的 shadow 适配，不决定素材目标，不改变发送动作。

本轮仍不修改 `app/main.py`、`app/services/llm.py`、图片/视频/PNG 发送逻辑、库存读取 primary 切换、飞书/企业微信/服务器配置。密码、token、飞书密钥和完整手机号必须在 `to_legacy_dict()`、repr、shadow record 和测试 JSON 中脱敏。

## M1B 修改归属声明模板

后续提交说明需标明：

- 房源/素材同步：Snapshot Builder、指针、产物校验。
- 问题重写/意图分析：snapshot_id 锁定、rewrite index 读取。
- Planner：无客户可见话术变化，仅工具证据契约变化。
- 结构化会话记忆：candidate/confirmed room 保存 listing_id 和 snapshot_id。
- 自检回流：密码和 snapshot 一致性校验。
- 发送阶段：房源表 PNG 同快照发送。
- 测试覆盖：新增 snapshot、密码隔离、同轮锁定、失败回退。
