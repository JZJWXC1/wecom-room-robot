# M0.6 CodeGraph 架构基线

本文件是 M0.6 只读审计结果，用来固定后续 M1 之前的架构基线。结论只来自当前工作区代码、`git` 输出和真实 CodeGraph SQLite 索引；没有重建索引，没有修改生产代码。

## 索引来源

- 当前 worktree：`<codex-worktree>`
- Git common dir：`<original-worktree>/.git`
- 当前 worktree HEAD：`a0d9e80dc5b7f7eebb34bee67cdc5f9cd883f564`
- 当前 worktree `.codegraph/`：不存在。
- 原始工作区 CodeGraph 索引：`<original-worktree>/.codegraph/codegraph.db`
- 原始工作区 `.understand-anything/`：不存在。
- CodeGraph 工具版本：`1.0.1`
- extraction version：`24`
- DB 文件时间：目录检查显示为 `2026-06-24 17:48:37`
- 索引对应 commit：unknown。DB 未记录 commit；只能确认原始工作区当前 HEAD 与本 worktree 相同，都是 `a0d9e80...`。
- 索引生成命令、排除目录、语言解析器配置：unknown。DB 中未发现这些字段。
- 索引是否包含未提交改动：partial/unknown。`daemon.log` 显示 file watcher 多次 auto-sync，且索引中已有当前 worktree 未跟踪的 `app/services/kf_orchestrator_flow.py` 节点，因此不能把索引等同于 HEAD。

## CodeGraph 图规模

- 文件节点：72
- 总节点：2996
- 总边：7220
- 节点类型：`method 1238`、`function 757`、`import 435`、`class 304`、`variable 179`、`file 72`、`route 11`
- 边类型：`contains 2913`、`calls 2903`、`imports 784`、`instantiates 566`、`references 44`、`extends 10`
- unresolved refs：10917

## 当前主链路

### 企业微信客服入口

existing:

- 文件：`app/main.py`
- FastAPI app：`app = FastAPI(title="寓你住一起客服 Agentic RAG")`
- 验证入口：`verify_wecom_kf_callback`
- 回调入口：`receive_wecom_kf_callback`
- 事件入口：`_handle_kf_event`
- 文本批处理入口：`_handle_text_messages_batch`
- 单条文本兼容入口：`_handle_text_message`
- 单轮主链路：`_process_text_turn`
- 欢迎语入口：`_handle_enter_session`

调用摘要：

`receive_wecom_kf_callback -> wecom_kf.parse_callback_event -> _handle_kf_event -> wecom_kf.sync_messages -> _handle_text_messages_batch -> _restart_kf_turn -> _process_text_turn`

`_process_text_turn` 目前仍是客服一轮对话的 composition root：读取/写入上下文，调用问题重写、Planner、工具执行、回复生成/最终自检、发送阶段，并记录结构化输出。

### 问题重写与意图分析

existing:

- 文件：`app/main.py`
- 入口：`_understand_message`
- LLM 调用：`ReplyGenerator.rewrite_kf_message`
- 事实索引：`load_rewrite_inventory_index`、`slice_rewrite_inventory_index`、`write_rewrite_inventory_index`
- 上下文视图：`kf_context_memory.rewrite_memory_view`
- 输出：`rewritten_query`、`effective_query`、`intent`、`query_state`、`entity_resolution`、`constraint_proof`、`structured_task`

当前 worktree 未提交实现：

- `_understand_message` 额外调用 `_orchestrator_tool_plan_from_understanding`，把 `tool_plan` 写入 `result` 和 `structured_task`。
- `_strip_llm_inferred_community_for_area_alias` 等函数用于区域别名命中时去掉 LLM 推断的小区。

结构风险：

- 问题重写逻辑仍集中在 `app/main.py`，函数量大，且与工具/自检辅助函数混杂。
- `app/services/rewrite_inventory_index.py` 已独立承载索引生成/切片，但主链路仍在 `app/main.py` 组装 prompt 视图和实体约束。

### Planner 与工具规划

existing in HEAD:

- `app/services/llm.py` 中 `ReplyGenerator` 负责 LLM 入口。
- CodeGraph 索引显示 `app/main.py::_plan_actions` 是主流程规划节点之一。

current worktree:

- `app/services/kf_orchestrator_flow.py` 是未跟踪文件，包含：
  - `tool_plan_from_understanding`
  - `planner_reply_selfcheck`
  - `planner_reply_selfcheck_status`
- `app/main.py::_plan_actions` 当前改为优先读取问题重写阶段产出的 `tool_plan`；没有 `tool_plan` 时按结构化任务做确定性补齐。
- V1 终态已删除 `ReplyGenerator.plan_kf_reply_text`；工具后客户可见文本由 LLM2 outbound 或 Planner 已提供的文本进入统一自检和发送包。

重要区分：

- 文件存在不等于已提交。
- 文件被导入不等于已完成分层。
- `kf_orchestrator_flow.py` 目前很薄，主要是适配工具计划和 planner 阶段 selfcheck 字段，不是完整 orchestrator。

### 工具执行

existing:

- 文件：`app/main.py`
- 入口：`_execute_tools`
- 房源查询：`InventoryService.search`
- 素材查询：`MediaStore`
- 飞书按需/同步相关：`FeishuClient`
- 房源 PNG：`InventoryImageSyncer`
- 输出：`tool_evidence`，包含房源行、图片/视频路径、房源表图片、缺失素材、pending 视频等。

结构风险：

- 工具执行仍在 `app/main.py`，直接依赖 inventory、media、Feishu、context memory 和大量 helper。
- 这是高扇出热点，后续拆分时应迁移到 `kf_tool_flow.py` 或等价模块，但本轮没有实施。

### 最终自检与回复生成

existing/current:

- 本地硬自检：`_constraint_consistency_selfcheck`、`_outbound_package_selfcheck`、`_local_human_context_selfcheck`
- LLM 最终自检：`ReplyGenerator.assess_kf_final_reply`
- 工具后回复生成：LLM2 outbound production/shadow，或 Planner 已提供的 `reply_text` 进入统一自检。
- RAG 质量规则：`KfAgenticRagService.assess_reply`、`assess_action`
- 安全边界：`kf_outbound_validation`、controlled evidence renderer、发送前 package gate。

当前边界：

- 客户可见 reply_text 与动作包最后由 `_generate_reply_result` 组织。
- 自检失败可形成 planner retry reason 并回到 planner/reply 生成流程。
- 但大量字段语义检查、口吻检查、动作包检查仍在 `app/main.py` 内。

### 发送阶段

existing:

- 文件：`app/main.py`
- 文本发送：`_send_text`
- 图片发送：`_send_images`
- 视频发送：`_send_videos`
- 统一发送：`_send_final_actions`
- 企业微信客户端：`WeComKfClient`
- 发送后上下文记录：`kf_context_memory.record_structured_assistant_output`

结构约束：

- 问题重写、Planner、自检模块不应直接调用发送。
- 当前只有 `app/main.py` 作为 composition root 调用发送，这是合理边界。

### 黑匣子上下文

existing:

- 文件：`app/services/kf_context_memory.py`
- 核心函数：
  - `empty_context`
  - `append_dialog_message`
  - `start_structured_turn`
  - `update_structured_state`
  - `record_structured_assistant_output`
  - `rewrite_memory_view`
  - `planner_memory_view`
  - `reply_memory_view`
  - `selfcheck_memory_view`
  - `pending_video_sends`
  - `remember_pending_video_sends`
  - `clear_pending_video_sends`

注意：

- 当前工作区该文件以 BOM 开头，本轮 AST 辅助分析无法解析；这里依据 `rg` 定义列表和 CodeGraph 索引。
- CodeGraph 显示 `empty_context`、`append_dialog_message` 是高扇入节点，说明上下文模块已是主链路基础设施。

### 房源同步、素材同步与 rewrite inventory index

existing:

- 区域同步脚本：`scripts/sync_feishu_region_inventory.py`
- RAG 缓存脚本：`scripts/refresh_rag_inventory_cache.py`
- 总控同步服务：`app/services/region_inventory_sync.py`
- 表格同步：`app/services/region_inventory_sheet_sync.py`
- 素材同步：`app/services/region_inventory_media_sync.py`
- 飞书客户端组合：`app/services/feishu.py` + `feishu_base.py`、`feishu_bitable.py`、`feishu_drive.py`、`feishu_sheet.py`
- 房源表 PNG：`app/services/inventory_image_sync.py`
- 问题重写事实索引：`app/services/rewrite_inventory_index.py`

同步链路：

`scripts/sync_feishu_region_inventory.py -> inventory_sync_graph.run_inventory_sync_graph -> RegionInventorySyncService.sync -> list_bitable_records -> normalize_region_records -> RegionInventorySheetSyncer.sync_target_sheet -> RegionInventoryMediaSyncer.sync_area_media`

旧直连链路只保留为显式 `--legacy-sync` 兼容入口，默认脚本和后台 `/admin/feishu/sync-region-inventory` 都由 Inventory Sync Graph 负责阶段顺序、失败即停和 trace。

`scripts/refresh_rag_inventory_cache.py -> InventoryService.refresh -> write_rewrite_inventory_index`

`app/main.py` 的 `/admin/feishu/sync-region-inventory` 路由同步完成后会尝试刷新 rewrite inventory index。

### QA runner

existing/current worktree:

- 固定 QA：`qa_artifacts/run_rag_test_text_window_utf8.py`
- 多窗口 QA：`qa_artifacts/run_rag_10windows_10turns_utf8.py`
- 随机保底 QA 默认入口：`qa_artifacts/run_kf_qa_gate_graph_utf8.py`（内部 random_windows 执行器仍为 `run_rag_random_guard_utf8.py`）
- 其他场景脚本：`run_rag_3questions_10turns_utf8.py`、`run_rag_5questions_5turns_utf8.py`
- Offline guard：`tests/offline_guard.py`

状态：

- 多个 QA 脚本在当前工作区是未跟踪文件，属于 M0.x 测试基础设施改动，不是当前 HEAD 已提交结构。
- QA 脚本导入 `app.main`，用于离线模拟完整回调链路。

### 部署脚本

existing:

- `scripts/server-ops.ps1`
- `scripts/deploy-aliyun.sh`
- `scripts/deploy-systemd-ubuntu.sh`
- `scripts/server_exec.py`
- `scripts/server_upload.py`
- `scripts/server_download.py`
- systemd 文件：`infra/systemd/*`

M0.6 未执行任何部署、SSH、服务器读写或线上访问。

## 四种结构状态

### A. 当前 HEAD 已提交结构

- `app/main.py` 是核心 composition root。
- `app/services/kf_turn_flow.py` 已提交，负责阶段耗时统计。
- `app/services/kf_context_memory.py` 已提交，负责上下文/黑匣子。
- `app/services/kf_agentic_rag.py` 已提交，负责静态知识检索、引用确认、动作/回复质量评估。
- `app/services/llm.py` 已提交，负责 rewrite、工具后 reply_text、自检 LLM 入口。
- `app/services/rewrite_inventory_index.py` 已提交，负责房源事实索引。

### B. 当前工作区未提交结构

- `app/main.py` 已修改：引入 `kf_orchestrator_flow`，增加区域别名/小区剥离逻辑，把工具计划更多前移到问题重写输出。
- `app/services/kf_orchestrator_flow.py` 是未跟踪新增文件：薄适配层，尚未提交。
- `app/services/kf_agentic_rag.py`、`llm.py`、`rewrite_inventory_index.py` 有未提交修改。
- QA/offline guard/docs/workflows 有大量未跟踪或未提交文件。

### C. CodeGraph 索引对应结构

- 索引来自原始工作区 `.codegraph/codegraph.db`。
- 索引已包含 `app/services/kf_orchestrator_flow.py`，但当前 worktree 中它是未跟踪文件。
- 索引有 watcher auto-sync 记录，因此代表“原始工作区某一动态状态”，不严格等同于 HEAD 或当前 worktree。
- 索引中 `app/main.py` 仍是最大热点：332 个节点，文件级 373 出边；`_generate_reply_result`、`_process_text_turn`、`_execute_tools`、`_understand_message` 是主要高扇出函数。

### D. 原计划中尚未实施的目标结构

proposed:

- `kf_rewrite_flow.py`：问题重写、意图分析、实体归一、约束证明。
- `kf_planner_flow.py`：Planner 输入输出、工具规划、RetryPacket。
- `kf_tool_flow.py`：房源、素材、规则工具执行和证据汇总。
- `kf_selfcheck_flow.py`：最终自检、拟人化/上下文连贯、自检回流。
- `kf_send_flow.py`：文本、图片、视频、房源表发送。
- `kf_turn_flow.py`：一轮企业微信消息总编排。

这些文件名/边界是目标，不是 existing。当前除了 `kf_turn_flow.py` 和未跟踪的 `kf_orchestrator_flow.py` 外，主要职责仍集中在 `app/main.py`。

## 当前热点与风险

- `app/main.py` 是最大热点和高扇出节点，仍承担入口、编排、重写、Planner、工具、自检、发送。
- `tests/test_wecom_kf.py` 是最大测试热点，900 个 CodeGraph 节点。
- `kf_context_memory.empty_context`、`append_dialog_message` 是高扇入状态基础设施。
- `InventoryService.search`、`parse_inventory_query`、`normalize_search_text` 是高扇入事实检索基础设施。
- CodeGraph unresolved refs 较高，静态 call 边不能作为完整行为证明，必须结合测试和代码阅读。

## 核心模块矩阵

### `app/main.py`

- 公开入口：FastAPI route、`startup`、`health`、`_process_text_turn`。
- 主要调用方：FastAPI/企业微信回调、QA runner。
- 主要被调用方：`ReplyGenerator`、`KfAgenticRagService`、`InventoryService`、`MediaStore`、`FeishuClient`、`InventoryImageSyncer`、`kf_context_memory`、`WeComKfClient`。
- 输入：企业微信回调 payload、客服消息、上下文、房源/素材工具结果。
- 输出：客户可见文本/图片/视频/房源表动作、上下文更新、admin route 结果。
- 状态读写：读写 `WeComKfContextStore`、结构化记忆、pending 视频、欢迎语审计。
- 外部依赖：企业微信、飞书、LLM、文件系统素材、房源缓存。
- composition root：是。
- 跨层调用：存在且较多，这是当前集中式结构。
- 循环依赖：未在本轮发现 Python import 循环；CodeGraph unresolved refs 较多，不能断言无运行时循环。
- 热点：最高扇出。
- 后续允许拆分：允许，但必须按 RAG 阶段拆，不改变客户可见行为。

### `app/services/kf_agentic_rag.py`

- 公开入口：`KfAgenticRagService.retrieve_for_reply`、`rewrite_user_need`、`reference_confirmation_for_message`、`assess_action`、`assess_reply`。
- 主要调用方：`app/main.py`、相关测试。
- 主要被调用方：`fuzzy_match`、本地知识库 markdown。
- 输入：用户需求、候选房源、上下文文本、工具证据摘要。
- 输出：RAG evidence、引用确认、动作/回复质量评估。
- 状态读写：读取知识库文件；不应写客服上下文。
- 外部依赖：本地 `knowledge/kf`。
- composition root：否。
- 跨层调用：不应发送、不应写线上。
- 热点：中高扇出，高扇入类。
- 后续允许拆分：允许拆出知识检索、引用确认、回复质量规则。

### `app/services/llm.py`

- 公开入口：`ReplyGenerator.rewrite_kf_message`、`build_kf_task_packet`、`compose_kf_outbound_shadow`、`compose_kf_outbound_production`、`assess_kf_final_reply`。
- 主要调用方：`app/main.py`。
- 主要被调用方：OpenAI-compatible client、`RuleKnowledgeService`。
- 输入：prompt payload、规则卡片、结构化任务、工具证据、自检上下文。
- 输出：重写结果、LLM1 task packet、LLM2 outbound package、自检结果。
- 状态读写：不应写客服上下文。
- 外部依赖：LLM API；M0.6 未联网调用。
- composition root：否。
- 跨层调用：可调用模型，不应调用发送/飞书。
- 后续允许拆分：允许按 stage 拆 prompt，但所有 LLM 调用仍应集中可审计。

### `app/services/kf_context_memory.py`

- 公开入口：`empty_context`、`append_dialog_message`、`start_structured_turn`、`update_structured_state`、`record_structured_assistant_output`、`rewrite_memory_view`、`planner_memory_view`、`reply_memory_view`、`selfcheck_memory_view`。
- 主要调用方：`app/main.py`、测试。
- 主要被调用方：无业务 service。
- 输入：上下文 dict、用户/客服消息、结构化摘要、pending 视频状态。
- 输出：归一化后的上下文、各阶段 memory view。
- 状态读写：是上下文结构的唯一归一化中心之一。
- 外部依赖：文件路径类型、时间函数；不应访问网络。
- composition root：否。
- 跨层调用：无发送/LLM/飞书直接调用。
- 热点：`empty_context`、`append_dialog_message` 高扇入。
- 后续允许拆分：谨慎，只能拆纯归一化/视图，不改变结构含义。

### `app/services/rewrite_inventory_index.py`

- 公开入口：`build_rewrite_inventory_index`、`write_rewrite_inventory_index`、`load_rewrite_inventory_index`、`slice_rewrite_inventory_index`。
- 主要调用方：`app/main.py`、`scripts/sync_feishu_region_inventory.py`、`scripts/refresh_rag_inventory_cache.py`、QA runner。
- 输入：最新房源 rows、查询文本。
- 输出：区域别名、小区、房号、价格、户型、素材摘要、相近小区等索引切片。
- 状态读写：读写 rewrite inventory index 文件。
- 外部依赖：房源缓存文件系统；不应访问企业微信。
- composition root：否。
- 后续允许拆分：允许拆成生成器/切片器/字段语义，但接口要稳定。

### `app/services/kf_orchestrator_flow.py`

- 状态：当前 worktree 未跟踪；CodeGraph 索引已有节点。
- 公开入口：`tool_plan_from_understanding`、`planner_reply_selfcheck`、`planner_reply_selfcheck_status`。
- 主要调用方：当前 `app/main.py` 未提交改动。
- 输入：问题重写输出、工具后 planner reply result。
- 输出：工具计划、planner 阶段 selfcheck 状态。
- composition root：否。
- 风险：文件很薄，不能视为完整 orchestrator 已落地。
- 后续允许拆分：可作为 Planner 边界雏形，但需先确定是否提交并补测试。

### `app/services/inventory.py` 与 `app/services/inventory_query.py`

- 公开入口：`InventoryService.refresh`、`InventoryService.search`、`parse_inventory_query`、`row_matches_hard_constraints`。
- 主要调用方：`app/main.py`、同步/缓存脚本、QA。
- 输入：用户查询、房源缓存、约束证明。
- 输出：命中房源 rows、缓存刷新结果。
- 状态读写：房源缓存文件。
- 外部依赖：飞书/表格配置、pandas；M0.6 未触发线上读取。
- composition root：否。
- 后续允许拆分：允许优化查询，但不能让 LLM 替代硬约束过滤。

### `app/services/media_store.py`

- 公开入口：`MediaStore`。
- 主要调用方：`app/main.py`、测试。
- 输入：小区、房号、素材目录。
- 输出：图片/视频路径。
- 状态读写：读取服务器本地素材目录。
- 外部依赖：文件系统。
- composition root：否。
- 后续允许拆分：允许，但不能直接联网同步或发送。

### `app/services/region_inventory_sync.py`、`region_inventory_sheet_sync.py`、`region_inventory_media_sync.py`

- 公开入口：`RegionInventorySyncService.sync`、`RegionInventorySheetSyncer.sync_target_sheet`、`RegionInventoryMediaSyncer.sync_area_media`。
- 主要调用方：`scripts/sync_feishu_region_inventory.py`、admin route。
- 输入：四区源表配置、飞书 records、目标表格、目标云盘根目录。
- 输出：目标房源表更新、素材同步结果、失败清单。
- 状态读写：同步状态文件、飞书线上表格/云盘（M0.6 未调用）。
- 外部依赖：飞书 API、文件系统、视频转码工具。
- composition root：否；脚本是 composition root。
- 后续允许拆分：允许，但线上写入必须有部署/线上审批。

### `app/services/inventory_image_sync.py`

- 公开入口：`InventoryImageSyncer.refresh_if_changed`、`current_images`、渲染函数。
- 主要调用方：`app/main.py`、测试。
- 输入：飞书电子表格导出或 values。
- 输出：房源表 PNG。
- 状态读写：房源表图片缓存和渲染状态。
- 外部依赖：飞书 API、LibreOffice/本地渲染工具、openpyxl、文件系统。
- composition root：否。
- 后续允许拆分：允许把渲染和飞书下载分开。

### `app/services/wecom.py` 与 `app/services/wecom_kf.py`

- 公开入口：企业微信加解密/客服 API/状态存储。
- 主要调用方：`app/main.py`、同步/飞书误解析边中也出现少量低置信引用。
- 输入：企业微信回调参数、发送动作。
- 输出：解析后的消息、发送结果、状态存储。
- 状态读写：客服 state store。
- 外部依赖：企业微信 API；M0.6 未调用。
- composition root：否。
- 后续允许拆分：发送封装可以保持，但不要让非发送层直接调用。

### QA runner

- 公开默认入口：`qa_artifacts/run_kf_qa_gate_graph_utf8.py`、`qa_artifacts/run_rag_test_text_window_utf8.py`、`run_rag_10windows_10turns_utf8.py`；`run_rag_random_guard_utf8.py` 保留为 QA Graph 的 random_windows 执行器。
- 主要调用方：人工/CI。
- 输入：fixture 问题、离线 stub、offline guard。
- 输出：QA artifact、质量状态。
- 状态读写：`qa_artifacts`。
- 外部依赖：默认不应联网。
- composition root：是，限测试。
- 后续允许拆分：允许，但不得降低门槛或绕过 offline guard。

## M1 前缺口

- 需要人工确认是否以当前未提交 `kf_orchestrator_flow.py` 为目标基础，还是先清理/提交 M0.x 测试基础设施。
- 若要让 CodeGraph 作为 M1 精确基线，应在人工审核后对确定的工作区状态重新生成/刷新索引；M0.6 本轮没有重建。
- 需要先解决当前工作区大量未提交改动的归属，否则后续架构 diff 很难判断责任边界。
