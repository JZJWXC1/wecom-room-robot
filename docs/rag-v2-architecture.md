# RAG V2 Architecture

本文记录 M1.5 契约对齐后的双 LLM 终态边界，以及 M4A LLM1 task packet shadow、M5A LLM2 outbound shadow 的非生产接入边界。M4A/M5A 都不接入 `app/main.py`，不修改客户可见回复、Planner 生产语义、自检生产语义、素材发送或库存读取 primary 切换。

## 终态两 LLM 分工

| 阶段 | 结构化输出 | 职责边界 |
| --- | --- | --- |
| LLM1 问题重写/任务拆解 | `StructuredTaskPacket`、`TaskAtom`、`ResponseStrategy` | 拆任务、表达约束继承/替换/排除/清空、声明工具需求；不选择库存数据源 |
| Planner/工具层 | `ToolEvidenceBundle`、`EvidenceItem`、`CandidateSet` | 读取房源、素材和受控事实，所有房源事实必须带 evidence 与 snapshot 追踪 |
| LLM2 待发送包 | `PreparedOutboundPackage`、`Claim`、`ActionCaption`、`SendAction` | 只组合 legacy 文本和工具证据；不自行决定视频、图片、密码或房源表目标 |
| 自检回流 | `self_review`、`selfcheck_profile`、`RetryPacket` | 校验 claims、证据引用、敏感字段和发送动作，失败时生成重试输入 |

## M4A LLM1 Shadow

`app/services/kf_llm1_task_packet.py` 是 LLM1 shadow 的本地契约边界：

- 接收脱敏后的用户消息、结构化记忆、候选集摘要、库存索引摘要和 legacy rewrite/planner 基线。
- 将 LLM1 JSON 归一为 `StructuredTaskPacket`，支持多 `task_atoms`、`inherit/replace/exclude/clear` 约束操作、`candidate_binding` 和 `tool_plan`。
- 没有候选集时会清空候选编号，只记录 `no_candidate_set`，不猜 `candidate_number`。
- 会过滤 `reply_text`、`clarification_text` 等客户可见话术字段；shadow artifact 继续通过契约脱敏。
- `app/services/kf_dual_llm_shadow.py` 只把新 LLM1 packet、tool_plan、candidate_binding 和 legacy diff 写入 shadow record；LLM2 仍只适配 legacy 文本和工具 evidence，不决定发送目标。

`app/services/llm.py::ReplyGenerator.build_kf_task_packet` 只是显式 shadow method，不替换旧 `rewrite_kf_message`，当前不接入生产编排。

## 关键契约字段

`ResponseStrategy` 已从纯字符串 mode 扩展为结构化策略：

- `mode`
- `detail_level`
- `direct_answer_required`
- `acknowledge_context`
- `max_sentences`
- `max_questions`
- `avoid_repeat_fields`
- `action_tense`

`Claim` 是字段级事实声明，至少能表达：

- `claim_id`
- `task_id`
- `listing_id`
- `field`
- `value`
- `evidence_ref`
- `text_span`
- `sensitivity`

`EvidenceItem` 和 `ToolEvidenceBundle` 都携带可追踪证据字段：

- `evidence_id`
- `listing_id`
- `inventory_snapshot_id`
- `source_record_id`
- `field_values`
- `sensitivity`
- `fetched_at`

`PreparedOutboundPackage` 是 LLM2 到发送准备层的唯一结构化输出，包含：

- `reply_text`
- `answered_task_ids`
- `claims`
- `action_captions`
- `send_actions`
- `missing_items`
- `self_review`
- `selfcheck_profile`

## Legacy 兼容

`from_legacy_dict()` 仍接受旧 dict、旧字段 alias、旧 `strategy` 字段和旧字符串/枚举策略。未知字段不会静默丢弃：已知字段进入强类型字段，未知字段进入 `legacy_unknown_fields`，敏感未知字段先脱敏。

`to_legacy_dict()` 继续提供安全 dict 视图，但 `response_strategy` 输出为结构化对象。需要旧 mode 的调用方读取 `response_strategy.mode`。本轮没有把该结构接入生产发送路径。

## M5A LLM2 Outbound Shadow

`app/services/kf_llm2_outbound.py` 新增 `compose_kf_outbound`，输入是 `StructuredTaskPacket + ToolEvidenceBundle + ResponseStrategy`，输出强类型 `PreparedOutboundPackage`。它只校验和包装 LLM2 shadow 的说法，不决定房源候选、素材目标或发送动作：

- `reply_text`、`claims`、`action_captions`、`answered_task_ids`、`self_review` 可以来自 LLM2 shadow 输出。
- `candidate_set`、`listing_id`、`candidate_number`、`send_actions` 只能来自工具证据或程序传入的已定动作。
- 价格、房态、密码、链接和素材目标必须由 evidence 支撑；发现模型新增未证实高风险事实时，输出 retry 包，保留既有动作但清空客户文本、claims 和 captions。
- 密码、链接、完整手机号、token 和 raw tool result 不进入 shadow artifact。

`app/services/llm.py` 仅新增 `compose_kf_outbound_shadow` prompt/method，用于未来 shadow 调用 LLM2 生成候选话术；旧 `rewrite_kf_message`、`plan_kf_reply_text`、`assess_kf_final_reply` 语义不变。

## 敏感信息边界

安全序列化、repr、shadow record 和测试 artifact 不得输出：

- 看房密码
- token、secret、access token
- 飞书密钥
- 完整手机号
- raw tool result

真实密码和受控 viewing 文本仍只能在专用工具边界内使用，不进入通用 `Claim.value`、`EvidenceItem.field_values`、`PreparedOutboundPackage.reply_text` 的安全输出。

## 本轮不变项

- 不改 `app/main.py`。
- 不改旧 production LLM 方法语义；`app/services/llm.py` 只新增显式 LLM1/LLM2 shadow methods。
- 不改变客户可见回复。
- 不改变素材发送决策。
- 不改变库存读取 primary 切换。
- 不连接飞书、企业微信、服务器或外部 LLM。
