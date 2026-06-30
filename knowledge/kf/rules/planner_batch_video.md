---
id: planner_batch_video
stage: tool_resolver
intents: media
triggers: 前两套 这三套 1和5 都发视频 继续发 剩下的视频
priority: 94
hard_rule: true
---
# 批量视频

“前两套、这三套、1和5、都发视频”必须绑定候选列表编号或明确房源。单轮发送数量受 KF_VIDEO_SEND_LIMIT 限制；超限或失败的剩余视频写入 pending_video_sends。下一轮只有用户明确说继续、补发、发剩下的，才继续 pending；用户问新问题时 pending 不抢跑。
