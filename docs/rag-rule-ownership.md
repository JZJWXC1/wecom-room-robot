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

## M1B 修改归属声明模板

后续提交说明需标明：

- 房源/素材同步：Snapshot Builder、指针、产物校验。
- 问题重写/意图分析：snapshot_id 锁定、rewrite index 读取。
- Planner：无客户可见话术变化，仅工具证据契约变化。
- 结构化会话记忆：candidate/confirmed room 保存 listing_id 和 snapshot_id。
- 自检回流：密码和 snapshot 一致性校验。
- 发送阶段：房源表 PNG 同快照发送。
- 测试覆盖：新增 snapshot、密码隔离、同轮锁定、失败回退。
