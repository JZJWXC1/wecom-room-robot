---
id: planner_context_binding
stage: tool_resolver
intents: inventory media viewing
triggers: 这套 那套 上一个 上个 就这个 就这套 刚才 上面 附近 预算 视频 看房 星桥那个 棠闰府那套 1和5 前两套 这三套 原视频 高清视频
priority: 97
hard_rule: true
---
# Tool Resolver 上下文目标绑定

Tool Resolver 只接收 LLM1 产出的结构化任务包、当前候选记忆和最新房源/素材证据，不重新解释用户意图。
凡是“这套、那套、上一个、上个、就这个、就这套、刚才那个、星桥那个、棠闰府那套”这类口语指代，Tool Resolver 必须使用任务包里的候选编号、标准小区名、标准房号和记忆里的候选集绑定目标；如果目标不唯一，只能返回结构化 `selection_error` 或 `target_error`，不能自己猜房源，也不能直接乱发素材。

用户说“1和5视频、前两套视频、这三套视频、都发视频”时，Tool Resolver 必须按上一轮候选编号绑定目标；不能把编号当房号，也不能重新按整张房源表泛搜。

如果当前任务已经带有新的区域、预算、户型、小区范围，且用户问的是“附近有没有、这边有哪些、预算多少以内、某区域几室”等新范围查询，即使 LLM1 标了 `context_reference=true`，Tool Resolver 也不能把旧 `confirmed_room` 混入本轮 `target_rows`。本轮查询必须以最新房源搜索结果为准。

工具结果已经证明视频、图片或房源表可发送时，Tool Resolver 必须保留目标和动作证据；客户可见动作说明由 LLM2 的 `PreparedOutboundPackage.action_captions` 生成，Sender 只执行已验证 package 和授权槽位追加，不重新造话术。

如果 Validator 只因为 L3 口吻、模板感或上下文接话不顺失败，回流只能让 LLM2 重写表达，不能丢掉已经由工具证据证明可发送的图片、视频或房源表动作。只有 L0-L2 事实不一致、目标不明确、文件不存在、动作和证据矛盾时，才允许拦截动作并回 LLM1/工具重新取证。

原视频/高清视频/不要压缩/客户要保存转发，属于素材需求升级。Tool Resolver 必须优先查原始素材、下载链接或素材页证据；如果只能发普通企业微信视频，则把“平台可能压缩/没有原素材下载链接”的证据交给 LLM2 表达。
