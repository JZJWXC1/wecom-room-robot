# LangGraph 本地接入说明

本项目已接入 `langgraph==1.2.7` 和 `langgraph-checkpoint-sqlite==3.1.0`，用于把客服 Agentic RAG 链路拆成可持久化、可回放、可测试的状态图。

当前 production 要求 LangGraph 作为唯一流程编排入口。开启双 LLM production 模式后，`KF_LANGGRAPH_ENABLED` 必须为 `true`；如果关闭，会直接报错，不再静默退回旧大循环。旧 planner、旧 reply、旧 RAG selfcheck 只允许留在 shadow/non-production 调试路径。

## 配置项

```env
KF_LANGGRAPH_ENABLED=true
KF_LANGGRAPH_CHECKPOINT_PATH=data/kf_langgraph_checkpoints.sqlite
KF_LANGGRAPH_SMOKE_THREAD_ID=kf-langgraph-smoke
```

- `KF_LANGGRAPH_ENABLED`：LangGraph production 编排开关。production 下必须开启；shadow/non-production 可关闭以运行旧路径测试。
- `KF_LANGGRAPH_CHECKPOINT_PATH`：SQLite checkpoint 文件路径，用于后续保存客服图状态。
- `KF_LANGGRAPH_SMOKE_THREAD_ID`：本地 smoke graph 默认线程 ID。

## 本地验证

```powershell
python -m pytest tests/test_kf_langgraph_flow.py tests/test_kf_langgraph_runtime.py -q
```

全量回归仍使用项目标准命令：

```powershell
$env:PYTHONPATH="$env:TEMP\wecom-room-robot-local-test-deps"
python -m pytest -q
```

## 迁移边界

当前 production LangGraph 图按项目 RAG 阶段拆分节点：

1. `understand_message`：LLM1 负责语义理解、上下文继承和 `tool_plan`，同时在本节点产出 `route`，把中介问题分成房源工具问题或业务问答；不生成客户可见回复。
2. `record_understanding`：把 LLM1 理解结果写入当前轮结构化状态，供后续工具绑定消费。
3. `plan_actions`：房源相关问题只接受 LLM1 `tool_plan.actions`，production 下不使用旧 planner 补动作。
4. `execute_tools`：由 `kf_tool_resolver` 和现有工具绑定房源表、素材、房态、价格、水电、看房证据。
5. `business_knowledge`：业务问答只取 `KfBusinessKnowledgeService` 的 `knowledge/kf/*.md` 轻量知识卡和确定性规则证据，再交给 LLM2 润色；不复用旧 `agentic_rag.retrieve_for_reply`。
6. `generate_reply`：由 LLM2 outbound package 或 controlled renderer 生成客户可见文本；事实和动作校验归 outbound validation。
7. 发送阶段：仍保留在 `app/main.py` 的 `_send_final_actions`，LangGraph 不直接调用企业微信发送 API；production 没有 PreparedOutboundPackage 时，任何文本/素材都不会发送。

房源、价格、房态、密码、图片和视频事实仍只能来自最新房源表和素材库工具结果。

## EntryGraph

`app/services/kf_entry_graph.py` 已接入企业微信客服消息入口。它不生成回复，不读取房源，不发送欢迎语，只负责编排入口分流：

1. `classify_messages`：把消息分成 `enter_session`、可自动回复文本、忽略消息；单条异常只标记 ignored，不拖垮同批次。
2. `group_text_messages`：按 `open_kfid + external_userid` 合并文本消息，保留原有 pending turn 合并语义。
3. `build_dispatch_plan`：输出可追踪的 dispatch plan，供 `app/main.py` 调用 `_handle_enter_session` 或 `_restart_kf_turn`。

欢迎语发送、文本 turn 重启、已处理消息判断仍归原确定性工具和状态存储；EntryGraph 只负责入口流程和 trace。

## SendGraph

`app/services/kf_send_graph.py` 已接入 production 文本链路的后置发送边界。它不替代 `_send_final_actions` 里的确定性发送工具，只负责编排发送阶段状态：

1. `audit_artifact`：先写生产审计 artifact，保留主图 trace、route、tool evidence 和最终话术。
2. `send_actions`：未被 block 时调用 `_send_final_actions`，实际发送文本、房源表 PNG、视频等。
3. `reduce_sent_context`：把发送结果、final package 和工具证据写回结构化上下文。
4. `reduce_blocked_context`：当 `reply_result.send_blocked` 或安全门要求 suppress 时，不调用发送工具，只记录 block 结果；如果上游误带 `final_reply`，也会失败关闭。
5. `persist_context`：保存会话上下文。
6. `mark_processed`：标记本轮企业微信消息已处理。

发送动作、素材路径、视频转码和企业微信 API 调用仍归确定性工具；SendGraph 只负责可追踪的流程、阻断和状态收口。

## ReceiptGraph

`app/services/kf_receipt_graph.py` 已接入 `_execute_send_action_once`，成为文字、图片、视频等实际发送动作的统一回执生命周期编排层：

1. `check_context_receipt`：先查当前上下文是否已有 sent/uncertain 回执，避免重复发送。
2. `reserve_outbox`：调用 `LocalKfOutboxLedger.reserve` 做持久化预占，防止进程重启或并发导致重复发。
3. `execute_send`：只在上下文和 outbox 都放行时调用真实发送 callback；成功写 sent 回执，异常写 failed/uncertain 回执后保持原异常语义。

重复发送判断、idempotency key、失败脱敏、outbox 文件锁和企业微信 API 调用仍由 `kf_send_receipts`、`kf_outbox` 与 `_send_final_actions` 负责；ReceiptGraph 只负责把这些确定性步骤串成可测试流程。

## QA Gate Graph

`app/services/kf_qa_gate_graph.py` 提供默认 QA 守门图，外部发布门禁和随机保底入口必须先走这个图。旧 `qa_artifacts/run_rag_random_guard_utf8.py` 保留为 `random_windows` 节点的执行器，不再作为发布默认入口。它把评估集流程拆成：

1. `fixed_windows`
2. `random_windows`
3. `historical_failures`
4. `write_artifact`

任一阶段出现 high、medium 或基础设施错误时，`fail_fast=True` 会直接跳到 artifact，不继续烧后续窗口。`fail_fast=False` 可用于完整审计，但最终 status 仍会因为已累积失败而变成 `blocked`。

## Inventory Sync Graph

`app/services/inventory_sync_graph.py` 提供独立房源/素材同步编排图，后台接口和 `scripts/sync_feishu_region_inventory.py` 默认都走这个图；旧脚本直连同步只允许显式 `--legacy-sync`。它把同步流程拆成：

1. `sync_region_inventory`
2. `refresh_inventory_cache`
3. `render_inventory_sheet_image`
4. `build_media_manifest`
5. `publish_snapshot`
6. `write_report`

真实飞书读写、PNG 渲染、素材上传、视频转码和 snapshot 发布仍由原同步工具负责；SyncGraph 只负责阶段顺序、失败即停、dry-run/full-audit 模式和报告状态。若某阶段返回 `ready=false`，也会按失败处理，避免 cutover readiness 被误判通过。

## Inventory Cutover Graph

`app/services/inventory_cutover_graph.py` 提供本地快照切换门禁图，不直接修改线上配置，不切服务器 pointer：

1. `primary_replay`：运行 primary replay，要求 legacy 与 snapshot 结果一致。
2. `evaluate_readiness`：调用 cutover readiness evaluator，检查 public artifact secret scan、router decision、parity case 数量。
3. `rollback_rehearsal`：本地演练 pointer rollback。
4. `write_report`：写本地报告，`fail_fast=True` 时任一阶段失败即停。

`build_local_inventory_cutover_deps()` 可直接绑定现有 `inventory_snapshot_cutover.py` 工具运行本地演练。没有 `APPROVE_DEPLOY` 时，这个图只能产生本地证据，不能修改服务器或生产读取入口。

## Release Preflight Graph

`app/services/release_preflight_graph.py` 提供本地发布预检图：

1. `local_tests`
2. `random_guard`（默认调用 `qa_artifacts/run_kf_qa_gate_graph_utf8.py`）
3. `config_check`
4. `release_rehearsal`
5. `write_report`

`build_local_release_preflight_deps()` 可绑定本地测试命令、QA Gate Graph、`get_config_status()` 和 `scripts/rehearse_release_pipeline.py`。发布演练清单现在会把未追踪但位于 release source 路径内的源码纳入候选清单，避免本地新模块能 import、部署清单却漏文件。

`/health/config` 也会检查 production 配置：`KF_DUAL_LLM_MODE=production` 时如果 `KF_LANGGRAPH_ENABLED=false`，状态会直接包含 `KF_LANGGRAPH_ENABLED_REQUIRED_FOR_PRODUCTION`；如果 LangGraph 包未安装，会包含 `LANGGRAPH_PACKAGE_REQUIRED_FOR_PRODUCTION`。两者都不能作为健康配置上线。

## 评估集

随机保底入口为：

```powershell
python qa_artifacts/run_kf_qa_gate_graph_utf8.py --seed 0
```

该入口先跑固定窗口，再生成 20 个随机窗口，每个随机窗口 10 轮，合计 200 轮随机保底；任一阶段出现 high/medium 业务问题或执行异常，会立即写出 QA Gate artifact 并停止后续问题。完整通过后还会检查实际工具调用覆盖：房源查询、房源表图片、房间视频、房间图片、原视频/缺素材、价格水电、看房密码、未空出约看、合同定房、免押政策。固定 10 窗口、holdout、历史失败回放仍保留各自执行器，但发布默认入口统一由 QA Gate Graph 编排。
