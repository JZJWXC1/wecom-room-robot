---
id: rewrite_inventory_sheet
stage: rewrite
intents: inventory_sheet
triggers: 房源表 表格 总表 最新房源 房源发一下 表发我
priority: 100
hard_rule: true
---
# 房源表请求

客户要“房源表、表格、总表、最新房源表、房源表发我”时，直接输出 intent=inventory_sheet，并生成发送房源表任务。不要追问小区、价位或户型。
