---
id: selfcheck_tiered_final
stage: validator
intents: all
triggers: 终检 自检 超时 Validator LLM2
priority: 85
hard_rule: true
---
# 分层 Validator

所有 LLM2 回复、受控渲染回复和待发送动作都必须进入 Validator。

为了保证企业微信日常可用速度，最终自检分两层：

- L0-L2 硬校验：必须检查 schema、证据引用、listing/candidate 绑定、字段语义、动作一致、敏感信息和素材证据。
- L3 表达校验：只处理口吻、模板感、重复追问、caption 时态和内部词泄露；失败时只回 LLM2 重写。
- LLM 终检：只用于高风险情况，例如长文本、投诉纠纷、密码打不开、无明确工具证据的泛答、或本地规则无法覆盖的自然度判断。

如果 L0-L2 已经覆盖了房源表、素材、房源表图片、免押、定房联系方式等确定性工具证据，并且 L3 没有发现风险词，允许跳过阻塞式 LLM 终检；这仍然算通过 Validator，不是绕过 RAG。

机器人不能承诺后续主动通知客户，例如“有合适的马上通知你”“帮你留意着”“稍后会为您推送最新信息”。只能说“可以继续按条件查”或“你再发预算/小区/房号我继续筛”。
