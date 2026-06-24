# 当前架构状态基线

本文档记录 M0 阶段只读梳理到的当前实现状态。它不是未来方案，也不表示所有存在的文件都已经稳定接入主链路。

## 状态边界

- 当前默认服务对象：合作中介，`audience=broker` / `broker profile`。
- 本轮未部署服务器，未连接飞书或企业微信线上写入，未 SSH。
- 房源、价格、房态、密码、图片、视频事实来源仍应限定为最新房源表和素材库工具结果。
- 当前工作区已有未提交业务改动，本文档将“当前 HEAD 已提交实现”和“当前工作区未提交实现”分开描述。

## 当前 HEAD 已提交实现

以当前仓库结构和已跟踪文件为准，主链路集中在 `app/main.py`：

1. 企业微信客服消息批处理入口：`_handle_text_messages_batch`。
2. 单轮消息编排：`_process_text_turn`。
3. 问题重写和意图分析：`_understand_message`，调用 `ReplyGenerator.rewrite_kf_message`。
4. Planner 阶段：`_plan_actions`。
5. 工具执行：`_execute_tools`，汇总房源、素材、规则等证据。
6. 回复生成和最终自检：`_generate_reply_result`，调用 `ReplyGenerator.assess_kf_final_reply` 等检查。
7. 发送阶段：`_send_final_actions`，再调用企业微信发送文本、图片、视频或房源表图片。

主要服务文件：

- `app/services/llm.py`：问题重写、Planner 后文本生成、最终自检相关 LLM 入口。
- `app/services/kf_context_memory.py`：结构化会话记忆、最近原始对话、候选集、已确认房源、pending 视频摘要等视图。
- `app/services/inventory.py`：房源读取和缓存。
- `app/services/media_store.py`：本地房源图片、视频素材检索。
- `app/services/feishu.py`、`app/services/feishu_drive.py`：飞书 API 客户端和云盘素材能力。
- `app/services/inventory_image_sync.py`：房源表图片刷新。
- `app/services/rewrite_inventory_index.py`：问题重写使用的房源事实索引构建能力。

## 当前工作区未提交实现

M0 开始前工作区已经存在未提交改动。它们不能算作本轮成果，也不能视为已上线：

- `app/main.py` 已有大量未提交修改，包含 RAG 主链路、工具执行和自检相关变化。
- `app/services/llm.py` 已有未提交修改，包含重写、Planner、自检 prompt 和规则知识接入变化。
- `app/services/rewrite_inventory_index.py` 已有未提交修改，涉及房源事实索引字段语义。
- `app/services/kf_agentic_rag.py` 已有未提交修改。
- `app/services/kf_orchestrator_flow.py` 是未跟踪文件，但当前 `app/main.py` 已引用它；这说明当前工作区可能能运行，当前 HEAD 未必能单独复现。
- 多个 `tests/test_*.py` 已有未提交修改，属于前置测试改动。
- `qa_artifacts/` 已有未跟踪 QA 脚本和输出。

结论：当前工作区不是干净基线。后续任何测试结果都必须标注“基于当前未提交工作区”，不能等同于当前 HEAD。

## 房源同步链路

脚本入口：

- `scripts/sync_feishu_region_inventory.py`

只读梳理到的调用关系：

1. 脚本读取 `FEISHU_REGION_SYNC_SOURCES` 等配置。
2. 使用 `RegionInventorySyncService().sync(dry_run=..., sync_media=...)` 同步区域房源和素材。
3. 同步成功且非 dry-run 后，调用 `InventoryService().refresh()`。
4. 调用 `write_rewrite_inventory_index(...)` 生成问题重写事实索引。
5. 写入同步状态文件。

systemd 定时器：

- `infra/systemd/wecom-room-robot-feishu-region-sync.timer`
- 当前配置：每天 `08:00 / 13:00 / 19:00`。

## RAG 缓存刷新链路

脚本入口：

- `scripts/refresh_rag_inventory_cache.py`

只读梳理到的调用关系：

1. `InventoryService().refresh()` 刷新房源缓存。
2. 将缓存行写入 `write_rewrite_inventory_index(...)`。
3. 写入 RAG 缓存刷新状态。

systemd 定时器：

- `infra/systemd/wecom-room-robot-rag-cache-sync.timer`
- 当前配置：每天 `08:05 / 13:05 / 19:05`。

## 问题重写与意图分析

当前主入口：

- `app/main.py::_understand_message`
- `app/services/llm.py::ReplyGenerator.rewrite_kf_message`

输入来源包括：

- 当前客户原话。
- `kf_context_memory.rewrite_memory_view(context)` 提供的最小会话记忆。
- 当前房源事实索引切片。
- Planner 回流证据。

当前工作区的 prompt 已要求读取最新房源事实索引、识别区域别名、继承短句上下文、处理素材请求和 pending 视频状态。由于这些改动处于未提交工作区，M0 不判断其上线质量。

## Planner、工具、自检和发送

Planner：

- `app/main.py::_plan_actions`
- 当前工作区还引用未跟踪的 `app/services/kf_orchestrator_flow.py`。

工具执行：

- `app/main.py::_execute_tools`
- 涉及房源表、素材、规则知识、房源表图片、pending 视频等证据汇总。

回复生成与自检：

- `app/main.py::_generate_reply_result`
- `app/services/llm.py::ReplyGenerator.assess_kf_final_reply`
- 本地硬规则自检函数仍在 `app/main.py` 中。

发送：

- `app/main.py::_send_final_actions`
- 企业微信实际发送在 `app/services/wecom_kf.py`。

## 上下文与黑匣子

当前上下文能力主要在 `app/services/kf_context_memory.py`：

- `raw_dialog_context`：最近原始对话。
- `turn_records`：每轮 user raw、rewritten query、intent、query_state、assistant sent summary。
- `rewrite_memory_view`：给问题重写读取的视图。
- `reply_memory_view`、`selfcheck_memory_view`：给回复和自检读取的较小视图。

注意：M0 只记录当前状态，不调整黑匣子结构。

## 设计中但尚未在 M0 实施的能力

以下内容属于后续 M1+，本轮未实施：

- 房源快照改造。
- 素材 manifest 改造。
- RAG 模块进一步拆分。
- 回复质量优化。
- Planner 和自检链路重构。
- 线上部署、服务器测试、systemd 修改。
