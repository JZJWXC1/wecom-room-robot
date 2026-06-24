# CodeGraph 结构保护规则

本文件是 M0.6 的后续改造规则。标记为 existing 的规则来自当前代码或 CodeGraph 图；标记为 proposed 的规则是为了保护后续目标结构，尚未完全由当前图证明。

## 修改前必须查询的节点和边

existing:

- 修改企业微信入口前，查询 `app/main.py` 中：
  - `receive_wecom_kf_callback`
  - `_handle_kf_event`
  - `_handle_text_messages_batch`
  - `_restart_kf_turn`
  - `_process_text_turn`
- 修改问题重写前，查询：
  - `app/main.py::_understand_message`
  - `app/services/llm.py::ReplyGenerator.rewrite_kf_message`
  - `app/services/rewrite_inventory_index.py`
  - `app/services/kf_context_memory.py::rewrite_memory_view`
- 修改 Planner/工具后回复前，查询：
  - `app/main.py::_plan_actions`
  - `app/main.py::_execute_tools`
  - `app/main.py::_generate_reply_result`
  - `app/services/llm.py::ReplyGenerator.plan_kf_reply_text`
  - `app/services/kf_orchestrator_flow.py`（当前未跟踪）
- 修改自检前，查询：
  - `app/main.py::_constraint_consistency_selfcheck`
  - `app/main.py::_outbound_package_selfcheck`
  - `app/main.py::_local_human_context_selfcheck`
  - `app/services/llm.py::ReplyGenerator.assess_kf_final_reply`
  - `app/services/kf_agentic_rag.py::KfAgenticRagService.assess_reply`
- 修改发送前，查询：
  - `app/main.py::_send_text`
  - `app/main.py::_send_images`
  - `app/main.py::_send_videos`
  - `app/main.py::_send_final_actions`
  - `app/services/wecom_kf.py`
- 修改同步前，查询：
  - `scripts/sync_feishu_region_inventory.py`
  - `scripts/refresh_rag_inventory_cache.py`
  - `app/services/region_inventory_sync.py`
  - `app/services/region_inventory_sheet_sync.py`
  - `app/services/region_inventory_media_sync.py`
  - `app/services/inventory_image_sync.py`

## 修改后必须重新检查的关系

existing:

- `receive_wecom_kf_callback` 是否仍只进入 `_handle_kf_event`，没有新增绕过 RAG 的客户可见回复分支。
- `_process_text_turn` 是否仍按顺序经过：上下文写入、问题重写、Planner/工具、回复生成/自检、发送、上下文回写。
- `_understand_message` 是否仍读取 `rewrite_memory_view` 和最新房源事实索引。
- `_plan_actions` 是否没有重新解释原始用户意图；如果需要语义澄清，应回问题重写层。
- `_execute_tools` 是否只输出工具证据，不直接发送。
- `_send_final_actions` 是否是唯一发送文本/图片/视频/房源表动作的客服主链路出口。
- `kf_context_memory` 是否仍是上下文读写唯一入口之一，业务模块不直接改结构化黑匣子内部格式。

## 单向依赖规则

existing/proposed:

- `app/main.py` 是当前 composition root，可以依赖各 service。
- `app/services/kf_context_memory.py` 不应依赖 `app/main.py`、发送客户端或 LLM。
- `app/services/rewrite_inventory_index.py` 不应依赖 `app/main.py`、发送客户端或 LLM。
- `app/services/kf_agentic_rag.py` 不应直接发送企业微信消息。
- `app/services/llm.py` 可以调用模型和规则知识，但不应读写企业微信状态或飞书线上数据。
- `app/services/region_inventory_*` 可以依赖 Feishu client 和同步模型，但不应调用客服发送。

proposed:

- `kf_rewrite_flow.py` 只能依赖 context memory 视图、rewrite inventory index、规则知识和 LLM rewrite 入口，不直接发送。
- `kf_planner_flow.py` 只接受结构化任务、约束证明、工具目录、RetryPacket，不读取完整原始对话。
- `kf_tool_flow.py` 只执行工具并返回 evidence，不生成最终客户话术，不发送。
- `kf_selfcheck_flow.py` 只检查 prepared outbound package，不直接发送。
- `kf_send_flow.py` 只发送已通过自检的 outbound package，不重新查房源、不调用 LLM。

## composition root

existing:

- `app/main.py` 是当前 FastAPI 和客服主链路 composition root。
- `scripts/sync_feishu_region_inventory.py` 是服务器区域同步 composition root。
- `scripts/refresh_rag_inventory_cache.py` 是 RAG 缓存刷新 composition root。
- QA runner 脚本是离线测试 composition root。

proposed:

- 后续如果拆出 `kf_turn_flow.py` 作为真正一轮对话编排器，需要让它成为客服 turn 的 composition root；`app/main.py` 只保留 FastAPI/企业微信 adapter。

## 禁止直接调用发送的模块

existing/proposed:

- 问题重写模块禁止直接调用 `wecom_kf.send_*`。
- Planner 模块禁止直接调用 `wecom_kf.send_*`。
- 工具模块禁止直接调用 `wecom_kf.send_*`。
- 自检模块禁止直接调用 `wecom_kf.send_*`。
- 同步模块禁止直接调用客服发送。
- 客户可见发送应集中在 `app/main.py::_send_final_actions` 或后续 `kf_send_flow.py`。

## 禁止直接读取完整房源表的模块

proposed:

- Planner 不应读取完整房源表，只接收问题重写层给出的结构化任务和必要索引摘要。
- 自检不应读取完整房源表，只检查工具证据、约束证明和最终待发送包。
- 发送模块不应读取完整房源表。

existing:

- 当前 `_understand_message` 和 `_execute_tools` 仍在 `app/main.py` 内读取房源/索引；这是当前集中式结构，不是目标分层。

## 禁止直接调用 LLM 的模块

proposed:

- `kf_tool_flow.py`、`kf_send_flow.py`、同步服务、素材检索服务不直接调用 LLM。
- LLM 调用集中在 `ReplyGenerator`，由问题重写、工具后回复、自检阶段通过明确 stage 调用。

existing:

- `app/services/llm.py::ReplyGenerator` 是当前 LLM 入口。
- `app/main.py` 当前直接调用 `reply_generator`，后续拆分时要逐步把调用移动到对应 flow。

## 状态读写规则

existing:

- 黑匣子/上下文读写集中在 `app/services/kf_context_memory.py`：
  - `append_dialog_message`
  - `start_structured_turn`
  - `update_structured_state`
  - `record_structured_assistant_output`
  - `remember_pending_video_sends`
  - `clear_pending_video_sends`
- 企业微信客服上下文持久化由 `WeComKfContextStore` 承担。

proposed:

- 模块之间传递状态时使用 context memory 提供的 view，不直接共享完整 context dict。
- 新增状态字段必须先在 context memory 归一化函数中定义，再由主链路写入。

## 事实来源规则

existing:

- 房源事实来自 `InventoryService`、`rewrite_inventory_index` 和同步后的房源表。
- 素材事实来自 `MediaStore` 和飞书素材同步结果。
- 飞书事实通过 `FeishuClient` 及其 mixin 获取。

proposed:

- LLM 只能改表达，不能改工具证据中的小区、房号、价格、房态、密码、图片、视频事实。
- 自检发现字段语义冲突必须回流，不允许发送阶段修正事实。

## 如何识别绕过 orchestrator 的新入口

检查方式：

- 搜索 `send_text(`、`send_image(`、`send_video(`，确认新增调用是否只在发送阶段。
- 搜索新的 FastAPI route，确认是否进入 `_handle_kf_event`、同步/admin 路由或明确非客服路径。
- 搜索直接返回客服回复文本的新增分支，确认是否经过最终自检。
- 用 CodeGraph 检查新增边：如果问题重写/Planner/工具/自检模块直接连到 `wecom_kf.send_*`，视为越层。

## 如何判断“搬代码”还是“改变行为”

existing/proposed:

- 只移动函数且输入输出、调用顺序、测试结果不变，属于搬代码。
- 修改函数签名、状态读写、工具证据结构、selfcheck 回流条件、发送时机，属于行为改变。
- 如果拆分后新增或删除 `send_*`、`ReplyGenerator.*`、`InventoryService.search`、`MediaStore`、`FeishuClient` 调用边，必须说明行为影响并补测试。
- 如果拆分后 `app/main.py` 的职责减少但新增 flow 没有被主链路调用，只能标记为“文件存在”，不能标记为“架构已落地”。

## M1 修改前检查清单

- 先查看 `qa_artifacts/m06_codegraph/hotspots.md`。
- 先确认当前 worktree 是否仍有 M0.x 未提交改动。
- 如果要依赖 CodeGraph，先确认索引对应状态；必要时人工审核后再刷新索引。
- 每次改动后输出新增/删除 import 边、核心函数调用边和是否新增客户可见发送入口。
- 所有结论区分 existing 与 proposed。
