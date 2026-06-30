---
id: planner_missing_media
stage: tool_resolver
intents: media
triggers: 视频 图片 照片 素材 没有 找不到 只发了一套
priority: 92
hard_rule: true
---
# 素材缺失处理

视频或图片请求必须形成完整工具证据：查目标房源、查素材库、发送已找到素材，并对缺失项返回 `missing_media` / `media_status`。Tool Resolver 不生成客户可见回复；只找到部分素材时，把已找到和缺失的房源逐项写入证据。所有素材都缺失时，证据必须区分“房源存在但素材缺失”和“房源不存在”。缺素材时 LLM2 不允许承诺“稍后发你”“正在补同步”“后面素材补齐再处理”。
