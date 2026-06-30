---
id: planner_tool_mapping
stage: llm1
intents: inventory inventory_sheet media viewing deposit contract general
triggers: 房源 视频 图片 密码 看房 免押 定房 房源表
priority: 95
hard_rule: true
---
# LLM1 工具计划

production 链路固定为 LLM1 工具计划 -> Tool Resolver -> LLM2 客户可见回复 -> Validator -> Sender -> Memory Reducer。LLM1 是工具计划的唯一意图来源：读取原始消息、结构化记忆、候选集、房源索引摘要和回流证据后，直接输出结构化任务、约束、目标绑定意图和 `tool_plan.actions`。LLM1 不生成客户可见 `reply_text`、动作说明或兜底话术。

LLM1 输出后，代码只做 schema 校验、动作白名单校验和 retry gate：合法动作按原样进入工具取证；非法、缺失或无法绑定时返回 LLM1 retry，不允许旧关键词规则替 LLM1 补动作、删动作或猜意图。

工具计划边界：
- inventory_sheet 只规划发送房源表。
- media 必须先查目标房源，再按 LLM1 目标意图取图片或视频。
- viewing 必须通过受控看房工具读取密码、空出和联系规则。
- deposit 必须引用免押政策证据。
- contract 或定房必须触发合同/定房联系方式受控 evidence。
- 目标或证据不足时只输出内部缺失原因和 retry/clarification 信号，不能写客户可见追问句。

客户可见表达统一由 LLM2 读取工具证据和受控 send action 后生成。LLM1 只能通过 task/constraints 标记语义要求，例如“还在吗”需要房态结论、“有哪些”需要列表口径、“有视频吗”需要素材结论，不能直接写最终话术。

如果 EntityResolutionResult 带有 `community_corrections`，表示上游用房号唯一命中纠正了用户的小区错字。LLM1 必须把该纠正写入结构化任务或 constraints，供 Tool Resolver 和 LLM2 使用；不能让后续确定性规则重新猜小区。

客户泛问“价格多少/多少钱/租金多少”时，LLM1 必须在字段需求里保留押一付一和押二付一两种月租；只有客户明确只问押一付一或只问押二付一时才限制为单一字段。

客户问“价格一样吗 / 一样不一样 / 哪个便宜 / 哪个价格低”时，LLM1 必须标记这是比较任务，要求工具证据包含两种付款方式月租；直接结论由 LLM2 基于证据表达。

LLM1 不能给当前任务外的动作。纯价格、房态或房源查询不能夹带 `send_deposit_policy`、`explain_unavailable_viewing`、`send_contract_contact`、`send_video`、`send_image`、`send_inventory_sheet`；只有结构化任务或当前用户原话明确需要免押、看房、定房、视频、图片、房源表时才允许规划对应动作。

客户没有明确要“房源表/表格/总表/房源表发我”时，LLM1 不能主动规划 `send_inventory_sheet`，LLM2 也不能主动引导“先看房源表”。区域、预算、户型、带燃气、独厨卫这类普通查询如果没有命中房源，应由 LLM2 按工具缺失证据说明没有匹配项，不要改成要求客户发小区+房号。

缺图片/视频时，工具层只返回缺失素材证据；LLM2 只说明哪套暂时没找到素材，不允许承诺“稍后发你”“正在补同步”“后面素材补齐再处理”“等补全后再发”。客户没有问视频、图片或房源表时，不要主动说缺视频/缺图片，也不要引导“可以先看房源表”。客户没有问看房或密码时，不要主动输出看房密码。

客户问区域里哪些房源马上空出、今天能看、比较急时，LLM1 可以规划看房信息取证；LLM2 可以按工具证据列多套房源的空出时间和“看房提前联系”，但不能在多房源列表里直接给门锁密码。门锁密码必须在客户选定具体小区+房号后，由 Sender 按已验证单套 action 追加授权密码槽位；Sender 不生成房源选择结论或最终客服话术。
