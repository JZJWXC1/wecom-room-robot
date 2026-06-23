---
id: reply_tool_grounded
stage: planner
intents: inventory media viewing inventory_sheet
triggers: 房源 在租 有哪些 还有 视频 图片 素材 稍后 确认 房态
priority: 98
hard_rule: true
---
# Planner 工具证据优先回复

工具层已经返回确定性证据时，Planner 第二阶段必须贴着工具证据生成客户可见 `reply_text`，不能再使用“我先确认房态、稍后回复、需要再确认”这类等待兜底。

- `inventory_rows` 有结果：必须先明确“有的/查到这些还在租”，再列出小区、房号、户型、押一付一、押二付一、水电备注等关键字段。
- 单套结果不能让客户“回序号”；只能说“要视频、图片或看房方式直接说这套”。多套结果如果要求回序号，回复里必须真的有编号。
- `inventory_rows` 为空：必须明确“暂时没查到完全匹配的在租房源”，并说明是按哪些约束没有匹配到。
- `video_paths` 或 `image_paths` 有结果：必须说明已找到并将发送对应房源素材；发送动作仍由发送阶段执行。
- 素材缺失：必须自然说明暂无视频/图片素材，不能沉默，也不能说房源不存在。
- 不允许把已有工具证据的场景回复成“稍后确认最新房态”。
