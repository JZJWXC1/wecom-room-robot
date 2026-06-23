---
id: reply_action_explanation
stage: planner
intents: media inventory_sheet
triggers: 视频 图片 照片 房源表 发我
priority: 90
hard_rule: true
---
# Planner 动作解释

Planner 第二阶段生成客户可见 `reply_text` 时，每个客户可见动作都要配一句自然解释。发送视频时说“这是某某小区+房号的视频”；发送图片时说“这是某某小区+房号的图片”；发送房源表时说“房源表发你了”。小区名和房号只能使用工具证据里的标准值。
