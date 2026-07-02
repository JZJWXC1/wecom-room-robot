# Production Rule Ownership

本文档记录客服 production 链路的规则归属，防止旧规则和新 Orchestrator 同时抢判断权。

## 归属表

| 规则面 | Production 唯一 owner | 不允许抢判断的旧 owner |
| --- | --- | --- |
| 企业微信入口分流 | `kf_entry_graph.EntryGraph`：classify -> group -> dispatch plan；欢迎语和文本 turn 由计划驱动 | `app/main.py` 在多个分支各自重新判断 enter_session、文本、已处理消息 |
| 流程编排与 trace | LangGraph `StateGraph`：`understand_message(同时产出 route) -> record_understanding -> plan/tools or business_knowledge -> generate_reply` | `app/main.py` 旧大循环在 production 中半路插入理解、澄清、planner 或 selfcheck |
| 工具动作选择 | LLM1 `StructuredTaskPacket.tool_plan` -> `_plan_actions` 门禁 -> `kf_tool_resolver` | 已删除的旧 deterministic action 补齐 |
| 房源/素材目标绑定 | `kf_tool_resolver.resolve_tool_targets` | 回复生成阶段、发送阶段、legacy reply 模板 |
| 客户可见话术 | `compose_production_outbound_package` 或 `compose_controlled_evidence_outbound_package` | `LegacyReplyBuilder`、Planner 直出文本、最终自检兜底话术 |
| 事实与发送动作校验 | `validate_prepared_outbound_package` / `kf_outbound_validation` | `agentic_rag.assess_reply`、旧 outbound package selfcheck、LLM final selfcheck |
| 业务问答知识 | LangGraph `business_knowledge` 节点 + `KfBusinessKnowledgeService` 轻量知识卡 + 确定性规则证据，LLM2 只润色表达 | 房源工具链、旧 `agentic_rag.retrieve_for_reply`、legacy reply 模板 |
| LLM1 失败兜底 | 受控 task packet | 本地 deterministic signal 补动作 |
| LLM2 空输出或不合格 | controlled evidence renderer | legacy reply fallback |
| 发送阶段编排 | `kf_send_graph.SendGraph`：audit -> send/block -> reduce context -> save -> mark processed | `app/main.py` 手写发送尾巴、任何绕过 send graph 的 production 后置发送 |
| 发送回执对账 | `kf_receipt_graph.ReceiptGraph` + `kf_send_receipts` + `kf_outbox`：context receipt -> outbox reserve -> execute -> receipt | 每个发送分支各自 reserve/record，或绕过 outbox 直接调用企业微信 |
| 发送动作不安全 | 没有 PreparedOutboundPackage 就 block/retry，不发送任何文本或素材 | 任何“安全话术”或旧 package 直接放行 |
| 快照切换门禁 | `inventory_cutover_graph` 本地 replay/readiness/rollback 证据；线上切换必须另有 `APPROVE_DEPLOY` | 同步图直接根据 `ok` 或人工感觉切换 primary |
| 发布预检门禁 | `release_preflight_graph`：本地测试、随机守门、配置健康、release rehearsal | 手动跳过随机评估、缺依赖或漏未追踪源码仍发布 |

## 当前迁移原则

- Production 下 LLM1 只做问题理解、上下文继承、实体绑定和工具计划，不生成客户可见回复。
- Production 下 LangGraph 必须开启；`KF_DUAL_LLM_MODE=production` 且 `KF_LANGGRAPH_ENABLED=false` 会直接报错，不退回旧链路。
- 旧 action planner 已物理删除；production、shadow、non-production 都不得再从本地 legacy planner 补动作。
- 旧 `LegacyReplyBuilder` 已物理删除；客户可见文本只能来自 LLM2 outbound package 或 controlled evidence renderer。
- Production 下旧 RAG retrieve / final selfcheck 不能重新判断房源事实或发送动作；业务问答知识由轻量业务知识服务读取，房源事实和动作只由工具证据与 outbound validation 裁决。
- Production 下后置发送由 SendGraph 编排；`_send_final_actions` 仍是确定性发送工具，不拥有是否可发的事实判断。
- Production 下单个发送动作的回执与 outbox 对账由 ReceiptGraph 编排；重复发送判断仍归 `kf_send_receipts` 和 `kf_outbox`。
- LangGraph 只应编排已经明确 owner 的节点，不应把旧规则混在图里继续竞争。

## 对应测试

`tests/test_production_rule_ownership.py` 是这份归属表的静态护栏。它不替代业务回归测试，只负责防止高风险旧 owner 回流到 production 主链路。
