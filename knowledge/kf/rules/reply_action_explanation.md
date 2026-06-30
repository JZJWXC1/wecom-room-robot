---
id: reply_action_explanation
stage: llm2
intents: media inventory_sheet
triggers: 视频 图片 照片 房源表 发我
priority: 90
hard_rule: true
---
# LLM2 动作解释

LLM2 生成客户可见 `reply_text` 和 `action_captions` 时，每个客户可见动作都要配一句自然解释。发送视频时说“这是某某小区+房号的视频”；发送图片时说“这是某某小区+房号的图片”；发送房源表时说“房源表发你了”。小区名和房号只能使用工具证据里的标准值。Sender 只执行已验证 package 和授权槽位追加，不再重建这些说明。
