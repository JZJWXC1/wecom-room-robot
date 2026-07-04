# 企微回调重复投递防线与 LLM 重试链延迟预算(2026-07-04)

- 事故实证:2026-07-04 16:01,生产(release 20260704-langgraph-hardening-150051,HEAD 7712db6d)
  同一条"房源表"请求被完整处理两次(16:01:24 与 16:01:30 两份 production audit artifact,
  msgid 相同),房源表图片对客重复外发;journalctl 16:01:37/16:01:39 两条
  `KF RAG timing` total_ms≈40000,说明两轮各自跑满约 40 秒的 LLM1/LLM2 重试链。
- 本文三部分:回调路径审计结论、三层幂等修复设计(含比选)、LLM 重试链延迟预算评估。

## 一、回调路径审计结论

1. **ACK 时序不是缺口**。`POST /wecom/kf/callback`(app/main.py `receive_wecom_kf_callback`)
   在本地 HEAD 与事故版本 7712db6d 中均为"解密验签 → `_schedule_background_task` 投递
   后台任务 → 立即返回 success",满足平台 5 秒应答约束;LLM 客户端全链路 `AsyncOpenAI`
   (异步 httpx),无同步阻塞事件循环的调用。无需改造为"先 ACK 后异步"——已经是。
2. **真正的缺口是幂等窗口**(平台回调为至少一次投递,重复投递是常态而非异常):
   - `WeComKfClient.sync_messages` 的"读游标 → 拉取 → 存游标"临界区不串行,
     并发回调可同时从旧游标拉到同一批消息(拉取网络耗时即竞态窗口);
   - `processed_msgids` 只在整条 RAG 链末尾(kf_send_graph `mark_processed` 节点)写入,
     单条消息处理约 40 秒,窗口内 `is_processed` 对重复投递全程为 False;
   - `_restart_kf_turn` 把纯重复投递(无新增 msgid)当作"客户追问":pending 非空即
     generation+1、取消在跑轮次、用同一内容整轮重放——重复回调不是被忽略,
     而是触发了完整的二次处理与二次外发。
3. **kf_send_outbox 台账为何没拦住**:幂等键(`build_idempotency_key`)是轮次域设计,
   含 turn 上下文、payload 哈希与每轮按 generation/time_ns 派生的证据链 id
   (app/services/inventory_read_turn.py `turn_id = hash(request_id, generation, turn_basis, content)`)。
   重复投递开出新轮次(generation+1)后,重放动作的幂等键全变,按键去重天然拦不住
   跨轮重放。台账按设计工作(同轮重试去重),缺的是跨轮的 msgid 域防线。

## 二、三层幂等修复(本批实现)

| 层 | 位置 | 机制 |
| --- | --- | --- |
| L1 入站认领 | `wecom_kf.sync_messages` + `WeComKfStateStore.claim_many` | 整个"读游标-拉取-存游标-认领"临界区按事件循环锁串行;拉到的 msgid 立即进入持久认领窗口(`inflight_msgids`,TTL=`WECOM_KF_MSGID_CLAIM_TTL_SECONDS` 默认 300s),窗口内重推与同批分页重叠一律丢弃;`mark_processed` 转正,轮次失败不转正、过期后平台重推可补处理,消息不永久丢失 |
| L2 轮次守卫 | `main._restart_kf_turn` | 合并后无新增 msgid 且轮次在跑 → 记日志直接返回,不 bump generation、不取消、不重放;真实客户追问(新 msgid)语义不变 |
| L3 出站防重 | `kf_outbox.reserve` + `kf_send_receipts.msgid_scope_guard_key` | msgid 域键=digest(conversation_id, msgids 集合摘要, action_type, action_id),只由未脱敏顶层字段构成,跨轮次稳定;同域已有 SENT/UNCERTAIN 回执或他键在途 pending → 阻断(`msgid_scope_blocks_duplicate` / `msgid_scope_pending_blocks_duplicate`);域键随记录落盘(白名单内部键),冷启动/跨实例仍生效;新客户消息=新 msgid 集合=新域,不误拦合法重复请求;FAILED 不阻断补发,同键同轮重试语义不变 |

方案比选(依协作规范"重大设计先比选"):
- ①(采纳)三层纵深:入站认领窗口 + 轮次守卫 + 出站域防重。任一层失守其余层兜底,
  且各层语义独立可测。
- ②仅内存去重(在 `_restart_kf_turn`/turn 状态里挡):否决——kf_turn_* 均为进程内存,
  进程重启、未来多 worker 即失效;且本次事故已实证内存层链条存在会漏的交错。
- ③sync 时立即 mark_processed:否决——轮次失败(LLM 全链超时、进程崩溃)后消息
  永久不重试,破坏"失败靠平台重推补处理"的现有恢复语义;认领窗口 TTL 保留了该语义。
- ④改造幂等键(剔除轮次域成分):否决——幂等键必须保留轮次域,否则客户隔天再要
  一次房源表(同素材同动作)会被历史回执永久阻断;msgid 域键以"客户消息集合"为
  生命周期,天然区分"同请求重放"与"新请求重复素材"。
- 孤儿工作包说明:judge/patches/ 中"重复外发动作检测"(`has_duplicate_external_send_actions`,
  AB-AB 序列折叠)是 QA runner 判分 gate,与本批生产台账层防线不同层面;
  按处置台账要求未 apply 任何孤儿代码,本批为独立重实现。

脱敏红线核对:域键只含 conversation/turn-scope/action 摘要哈希,不含客户文本与敏感
字段;`SendAction.metadata` 在合同层已脱敏(实现时据此改用未脱敏顶层 `turn_id` 前缀,
并将域键作为白名单内部键随台账记录落盘,已被 tests/test_kf_outbox_msgid_scope_guard.py
的跨 msgid 域碰撞用例守护)。

## 三、LLM 重试链延迟预算评估(任务3,只评估不改行为)

事故消息实测 total≈40s;各阶段当前上限(生产 LangGraph 双模型路径):

| 阶段 | 外层上限 | 应用层重试 | SDK 层(openai 默认) | 最坏累计 |
| --- | --- | --- | --- | --- |
| rewrite/问题重写(qwen-flash) | **无 wait_for 上限** | planner 反馈可触发二次 rewrite | timeout=600s,max_retries=2 | 理论无界(实测 5-15s) |
| LLM1 packet(qwen-flash) | `wait_for` 25s(`KF_LLM1_PRODUCTION_TIMEOUT_SECONDS`) | contract retry 二次 build,再退受控合同(无 LLM) | 同上 | ≈50s |
| 工具执行 | 房源表 artifact/媒体绑定,通常 <3s;视频转码走 `asyncio.to_thread` | - | - | ≈3s |
| LLM2 出站(qwen-plus) | `wait_for` 45s(`KF_LLM2_PRODUCTION_TIMEOUT_SECONDS`) | `needs_planner_retry` 触发整链(planner+tools+LLM2)第二轮 | 同上 | ≈45s×2 |
| LLM 终检 | `wait_for` 3s,超时放行 | - | 同上 | ≈3s |
| 发送 | WeCom API httpx timeout 30-40s/次 | 视频 40011 转码重传一次 | - | 常规 <5s |

- 理论最坏 ≈ rewrite(无界)+2×(50+3+45+3)+发送 ≈ 200s+;实测 40s 属"LLM1 与 LLM2
  各自吃满一轮重试"的中间形态,与 journalctl 证据一致。
- 平台约束对照:回调 ACK 5s(已满足,与处理时长解耦);sync_msg 游标消费无时限。
  40s 的代价是客户等待体验与重复投递风险窗口的长度,不是协议违规。
- 认领窗口 TTL 取 300s:覆盖实测 P100(40s)与绝大部分理论尾部;若后续压缩重试链,
  TTL 可同步下调。
- 后续收敛建议(留待独立批次,不在本批改动):
  1. rewrite 阶段补 `wait_for`(建议 15s)——当前唯一无界阶段;
  2. `AsyncOpenAI` 显式设 `timeout`(对齐各阶段外层上限)与 `max_retries=1`,
     避免 SDK 内层重试与外层 wait_for 叠乘(外层超时后 SDK 内层重试白烧配额);
  3. 目标预算:P95 单消息 ≤30s(rewrite 8 + LLM1 12 + tools 3 + LLM2 18 + 终检 3 + 发送 5,
     二轮重试仅允许单阶段触发);
  4. 改动会影响 QA gate 的成功率口径,须按台账流程先比选再实施。

## 四、验证

- 新增 `tests/test_kf_callback_dedup.py` 8 项(认领窗口授予/过期/转正/空值、状态文件
  读改写保留认领、sync 重推去重、同批分页重叠去重、并发串行化、纯重复投递不重启
  +真实追问仍重启)。
- 新增 `tests/test_kf_outbox_msgid_scope_guard.py` 7 项(域键跨轮稳定且跨 msgid 区分、
  SENT 阻断跨轮重放、在途 pending 阻断并发重放、FAILED 放行补发、新客户消息放行、
  同键重试 attempt 语义不变、冷启动/跨实例一致)。
- 全量 pytest 结果见台账 `docs/release/acceptance-criteria-change-log-20260704.md`。
- 本批不部署、不 push;部署需用户 APPROVE_DEPLOY。
