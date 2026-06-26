# RAG Contracts

本文记录客服 RAG 后续双 LLM 正常路径的强类型数据契约基础。本轮只新增契约模型和 legacy dict 适配器，不接入 `app/main.py`，不修改客户可见回复、Planner、Prompt、自检或发送语义。

## 模块职责

`app/services/kf_contracts.py` 是客服 RAG 结构化数据的边界模块：

- `StructuredTaskPacket`：承载 rewrite/任务拆解结果。
- `TaskAtom`：承载单个任务、约束操作、所需工具和策略。
- `CandidateSet` / `CandidateItem`：承载房源候选集合、候选编号、`listing_id`、`candidate_set_id`。
- `ToolEvidenceBundle` / `EvidenceItem`：承载工具证据、`evidence_id`、`inventory_snapshot_id`。
- `Claim`：承载回复里的事实声明和证据引用。
- `PreparedOutboundPackage`：承载待发送文本、claims、send actions 和证据引用。
- `RetryPacket`：承载自检失败后的重写原因和重试指令。
- `SendAction` / `SendReceipt`：承载发送动作与发送回执。

## 字段所有权

所有契约模型共享基础追踪字段：

- `schema_version`：契约版本，默认 `kf_rag_contracts.v1`。
- `prompt_version`：生成该结构的 prompt 版本。
- `conversation_id` / `turn_id` / `case_id`：会话、轮次和测试/回放 case 标识。
- `audience`：当前默认 `broker`。
- `inventory_snapshot_id`：库存事实源 snapshot 标识。
- `candidate_set_id`：候选集合标识。
- `listing_id`：房源标识。
- `evidence_id`：证据标识。

`TaskAtom.constraint_operation` 只能使用：

- `inherit`
- `replace`
- `exclude`
- `clear`

`response_strategy` 只能使用枚举值，例如 `tool_first`、`answer`、`ask_clarification`、`send_media`、`retry`。

## Unknown Field 策略

强类型构造默认拒绝未知字段，避免未来 LLM 输出或中间层数据悄悄漂移。

legacy dict 入口使用 `from_legacy_dict()`：

- 已知字段按 canonical 字段名或显式 alias 解析。
- 未知字段进入 `legacy_unknown_fields`。
- 敏感未知字段会先脱敏再记录。

输出使用 `to_legacy_dict()`：

- 保持 dict 兼容路径。
- 默认输出安全视图。
- 不输出 raw tool result。
- 不输出真实看房密码、手机号、token、secret。

## 兼容路径

本轮只建立数据契约，不替换现有 RAG 主链路。后续迁移应按顺序进行：

1. rewrite 阶段将 legacy task dict 适配为 `StructuredTaskPacket`。
2. Planner/工具层只消费 `TaskAtom` 和 `CandidateSet` 的强类型字段。
3. 工具证据统一进入 `ToolEvidenceBundle`。
4. 回复生成输出 `PreparedOutboundPackage`。
5. 自检失败输出 `RetryPacket`。
6. 发送阶段消费 `SendAction` 并生成 `SendReceipt`。

任何迁移必须保持客户可见回复、候选排序、发送动作和自检语义不变，先做 golden parity，再替换旧 dict。

## 敏感信息边界

通用日志、repr、测试 artifact 不得包含：

- 真实看房密码
- 手机号
- token
- secret
- private/raw viewing 字段

需要受控读取的 viewing/password 信息必须留在专用工具边界，不进入通用 `EvidenceItem.metadata`、`Claim.text` 或 `PreparedOutboundPackage.reply_text` 的安全输出。

## 本轮非目标

- 不合并 LLM 调用。
- 不修改 Prompt。
- 不修改 Planner 判断。
- 不修改客户回复策略。
- 不修改发送逻辑。
- 不新增固定问法正则回复分支。
- 不连接服务器、飞书、企业微信或外部服务。
- 不引入向量数据库。
