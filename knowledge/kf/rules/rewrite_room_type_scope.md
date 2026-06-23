---
id: rewrite_room_type_scope
stage: rewrite
intents: inventory media viewing
triggers: 一室 1室 一房 一室一厅 带厅 有厅
priority: 98
hard_rule: true
---
# 户型范围理解

客户只说“一室、1室、一房”时，默认是宽匹配，包含一室户和一室一厅，不要追问“一室户还是一室一厅”。

只有客户明确说“一室一厅、1室1厅、一房一厅、带厅、有厅、要厅”时，才把户型收窄到一室一厅。

客户说“单间、开间”时，不等同于“一室一厅”。应优先匹配单间、开间、无厅的一室；不要把带厅的一室一厅混进“单间”结果里。
