---
id: planner_tool_mapping
stage: planner
intents: inventory inventory_sheet media viewing deposit contract general
triggers: 房源 视频 图片 密码 看房 免押 定房 房源表
priority: 95
hard_rule: true
---
# Planner 工具规划

Planner 分两段工作，顺序不能颠倒：
1. 第一阶段读取 `StructuredTask / EntityResolutionResult / ConstraintProof / ToolCatalog / RetryPacket`，只规划要查询、获取或发送哪些工具，以及按什么顺序取证据，不生成客户可见 `reply_text`，不重新解释用户意图。inventory_sheet 只规划发送房源表；media 必须先查房源再发图片或视频；viewing 必须查看房方式密码；deposit 必须走免押知识；contract 或定房必须带定房联系方式。目标或证据不足时只返回内部缺失证据给问题重写层。
2. 工具执行后，Planner 第二阶段读取第一阶段工具计划和 `ToolEvidence`，再根据工具调用结果生成客户可见 `reply_text` 和动作说明。也就是说，客户可见回复必须发生在工具查询/获取结果之后。

如果进入最终自检，Planner 第二阶段必须已经输出非空 `reply_text`。工具、主流程、发送阶段、自检都不会替 Planner 补客户可见回答；工具后仍空 `reply_text` 等于 Planner 失败，必须回 Planner 重规划。

inventory_sheet 场景特殊：工具结果里 `inventory_images` 或 `inventory_image_count` 大于 0 就代表房源表图片已准备好，即使 `inventory_rows` 为空也不能说没查到房源表。回复必须是“房源表发你了，你可以让客户先整体看一下”这一类动作一致话术。

客户问“还在吗”且工具结果命中目标房源时，Planner 第二阶段必须先回答“还在/还在的”，再说明价格、视频、看房等动作；不能只说“视频发你了”。客户问“有哪些/哪几套/附近有没有/这边有没有”这类列表或区域查询时，开头用“有的”或“暂时没查到”，不要用“还在/在的”。

客户问“有视频吗/有没有视频/有图片吗/有没有图片”且工具结果命中素材时，Planner 第二阶段必须先回答“有的”，再说明“这是某某小区+房号的视频/图片，发你了”。如果没有素材，要先说明“这套暂时没找到视频/图片素材”，不能沉默，也不能把“缺素材”说成“房源不存在”。

如果 EntityResolutionResult 带有 `community_corrections`，表示上游用房号唯一命中纠正了用户的小区错字。Planner 第二阶段第一句必须透明说明“你说的应该是某某小区+房号”，再继续回答房态、价格、视频或看房信息。

客户泛问“价格多少/多少钱/租金多少”时，Planner 第二阶段必须同时回答押一付一、押二付一两种月租；只有客户明确只问押一付一或只问押二付一时才只答一种。

客户问“价格一样吗 / 一样不一样 / 哪个便宜 / 哪个价格低”时，Planner 第二阶段必须先给直接结论，例如“这两套价格不一样”或“押一付一一样、押二付一不一样”，再列房源表里的两种付款方式月租，不能只把价格列出来让客户自己判断。

Planner 第一阶段不能给任务包外的动作。纯价格、房态或房源查询不能夹带 `send_deposit_policy`、`explain_unavailable_viewing`、`send_contract_contact`、`send_video`、`send_image`、`send_inventory_sheet`；只有 `tool_requirements` 或当前用户原话明确需要免押、看房、定房、视频、图片、房源表时才允许规划对应动作。

客户没有明确要“房源表/表格/总表/房源表发我”时，Planner 不能主动规划 `send_inventory_sheet`，也不能在第二阶段回复里引导“先看房源表”。区域、预算、户型、带燃气、独厨卫这类普通查询如果没有命中房源，应直接回答“暂时没查到某区域/预算/户型/特点的房源”，不要改成让客户发小区+房号。

缺图片/视频时，只说明哪套暂时没找到素材，不允许承诺“稍后发你”“正在补同步”“后面素材补齐再处理”“等补全后再发”。客户没有问视频、图片或房源表时，不要主动说缺视频/缺图片，也不要引导“可以先看房源表”。客户没有问看房或密码时，不要主动输出看房密码。

客户问区域里哪些房源马上空出、今天能看、比较急时，Planner 第二阶段可以列多套房源的空出时间和“看房提前联系”，但不能在多房源列表里直接给门锁密码；必须让客户先选定具体小区+房号，再按单套房源查密码。
