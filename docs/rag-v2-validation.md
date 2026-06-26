# RAG v2 outbound validation

本文件记录 D 线新增的程序化 outbound 校验器雏形。当前只提供纯模块和离线测试，不接入生产发送链路，不修改 `PreparedOutboundPackage` 契约。

## 位置

- 模块：`app/services/kf_outbound_validation.py`
- 测试：`tests/test_kf_outbound_validation.py`

## 输入

校验器消费现有 `app.services.kf_contracts` 中的 `PreparedOutboundPackage`，可选消费 `StructuredTaskPacket` 或 `OutboundValidationContext`。

它不会复制合同字段为第二套事实源；事实一致性只从 package 内的 candidate、evidence、claim、send action 引用关系中归一化读取。未来 A 线如果给 claim 增加正式 `value/evidence_ref/field` 字段，D 线可把当前 legacy 读取点迁移到正式字段。

## 分层规则

- L0 结构与动作：检查 schema_version、evidence/action/listing/candidate 引用、缺失或重复 action、未知 action/evidence 引用、candidate_number 类型。
- L1 事实一致性：检查 claim value 是否存在于 evidence_ref 指向证据的 `field_values`，检查 listing_id 和 snapshot_id 一致性，并阻断密码或链接值出现在 evidence slot 之外。
- L2 需求完成度：检查 task atom 是否被文本、claim 或 send action 覆盖；视频-only 请求不能混发图片；candidate_number 不能越界；用户没问密码时不能出现密码 claim 或密码 send action。
- L3 口语与上下文：只返回 rewrite reason，不改事实，不生成新回复。当前覆盖内部字段名/工具名泄露、模板话、重复追问已知条件、动作时态错误。

## 结果语义

- L0-L2 任一命中：`status=blocked`，不可发送。
- 只有 L3 命中：`status=rewrite_required`，事实通过，但需要 Planner/LLM2 只重写话术。
- 无命中：`status=pass`，纯校验角度允许发送。

当前模块没有被 `app/main.py`、`app/services/llm.py` 或真实发送逻辑 import。
