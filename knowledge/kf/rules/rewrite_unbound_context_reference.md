---
id: rewrite_unbound_context_reference
stage: rewrite
intents: media viewing context_followup
triggers: 这几套 这些 刚才 上面 里面 密码 视频 图片
priority: 97
hard_rule: true
---
# 无绑定上下文的指代处理

客户说“这几套、这些、刚才、上面、里面”时，必须先读取黑匣子里的 last_candidate_set、confirmed_room、last_media_context。

如果黑匣子里没有可绑定的候选房源或已确认房源，不能编造“之前展示的几套”，也不能直接发素材或密码。应追问具体小区+房号，或让用户回复房源序号。

如果本轮消息本身包含完整筛选条件，例如“万达2000以下一室前两套视频”，即使上一轮没有候选，也应先按本轮条件查房源并新建候选，再规划发送视频。
