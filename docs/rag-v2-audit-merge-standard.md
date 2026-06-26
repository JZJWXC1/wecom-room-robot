# RAG V2 Audit/Merge Standard

本文档定义 H 线 Audit/Merge agent 审查 A/B/C/D/E/F/G 线提交是否可以 cherry-pick 到 `codex/rag-v2-integration` 的统一标准。H 线职责是审计、归档判断和提交审计文档；未收到主 agent 明确指令前，不 cherry-pick 任何业务线提交，不修改生产业务代码。

基线：`codex/rag-v2-integration@726f309d`。

## 一、审计目标

每个待审 commit 必须证明它没有破坏 RAG V2 主线：

```text
用户消息 -> 确定性预处理 -> LLM1 -> 工具执行 -> LLM2 -> 程序化 L0-L3 -> 幂等发送
```

合入判断不以“看起来能回复”为准，而以证据链、测试门禁、敏感信息边界和 legacy 删除完整性为准。任何提交如果引入绕过 Agentic RAG 的客服回复分支、事实猜测、素材错绑、敏感信息泄露或生产 primary 切换，都应阻塞。

## 二、审计执行流程

1. 在目标 worktree 先运行 `git status --short`，确认工作区状态；不得回滚或覆盖用户已有改动。
2. 确认当前 HEAD 与预期基线或待审范围；记录待审 commit、父提交和改动文件。
3. 若仓库有 `.codegraph/`，优先使用 CodeGraph 理解代码；没有索引时使用 `rg`。
4. 阅读 diff 和相关测试，按本文档“硬性阻塞项”和“专项检查项”逐项判定。
5. 记录客户可见行为是否改变、改动归属阶段、测试证据和残余风险。
6. 只输出审计结论；除审计文档外，不修改业务模块，不直接修业务缺陷。

## 三、硬性阻塞项

出现以下任一情况，结论必须是“不可 cherry-pick”，除非后续 commit 已在同一审计范围内完整修复并提供测试证据。

| 编号 | 阻塞项 | 审计标准 |
| --- | --- | --- |
| H1 | 主线偏离 | 终态不再保持“确定性预处理 -> LLM1 -> 工具执行 -> LLM2 -> 程序化 L0-L3 -> 幂等发送”，或把事实判断、素材发送、密码读取迁到非工具证据路径。 |
| H2 | 第三个 Judge LLM 进入正常成功路径 | 正常成功路径不得新增第三个必经 Judge LLM。现有 `assess_kf_final_reply` 只能作为 shadow、过渡兼容或失败保护，不得成为 LLM1/LLM2 之外的新常规裁判角色。 |
| H3 | 新增绕过 Agentic RAG 的固定客服回复 | 不得在 `app/main.py`、`llm.py`、`kf_agentic_rag.py` 或周边新增直接匹配客户文本并返回客服话术的生产分支。旧固定规则只能作为最后安全阀，不能代替 RAG 链路级修复。 |
| H4 | 房源事实离开工具 evidence | 房源、价格、房态、密码、图片、视频事实只能来自最新房源表和素材库工具 evidence；不得来自 prompt 猜测、LLM 自述、历史自然语言记忆或模糊文件名推断。 |
| H5 | 无 `listing_id` / `candidate_set` 仍绑定素材或候选编号 | 没有明确 `listing_id` 或当前有效 `candidate_set` 时，不允许发送图片、视频、原视频、密码，也不允许把“第几个”“这几套”绑定到候选。必须澄清或重新走工具查询。 |
| H6 | 敏感信息进入不该进入的边界 | 密码、token、完整手机号、飞书密钥、服务端凭证不得进入日志、artifact、prompt、长期记忆、通用 evidence、审计输出或测试快照。 |
| H7 | Media Manifest 决策越权 | Media Manifest 只能表达 `listing_id -> media_id` 的确定绑定。模糊文件名只能进入候选证据或人工复核报告，不能生成发送决策，不能跳过工具绑定。 |
| H8 | Snapshot/Router 切生产 | Snapshot/Router 不得启用 production primary，除非 release 线已有明确门禁通过和主 agent 指令。本阶段不得切换生产读路径，不得让客户可见结果来自未批准 primary。 |
| H9 | 测试门禁不足 | 每条线必须有专项测试。触碰 `app/main.py`、`app/services/llm.py`、`app/services/kf_agentic_rag.py` 必须至少跑 L2；合入 integration 前至少 L3；release 前必须 L4。缺少证据即阻塞。 |
| H10 | Legacy 只加不删 | 新实现上线为唯一生产入口后，必须同步删除旧调用方、旧 prompt、旧 fallback，并全仓搜索确认没有双规则抢判断。只新增新规则而保留旧生产判断，必须阻塞。 |
| H11 | 审计报告不完整 | 审计报告必须列出改动文件、风险等级、客户可见行为、测试证据、是否可 cherry-pick 和阻塞项；缺项不得给出通过结论。 |

## 四、RAG 主线检查

审计时按阶段归属标记每个改动：

| 阶段 | 允许做什么 | 不允许做什么 |
| --- | --- | --- |
| 确定性预处理 | 清洗输入、脱敏、构造请求上下文、读取安全会话状态。 | 直接生成客服回复；根据固定关键词声明房源事实。 |
| LLM1 | 问题重写、意图分析、任务拆解、工具需求声明。 | 选择库存数据源；猜测房源是否存在；输出可发送素材目标。 |
| 工具执行 | 按 `InventoryReadContext`、`listing_id`、`candidate_set` 读取库存、素材、房源表和受控密码 evidence。 | 从 prompt、长期记忆或模糊文件名直接取事实；半途混用 Legacy/Snapshot 来源。 |
| LLM2 | 基于工具 evidence 生成候选回复、claims 和发送意图。 | 增加新事实；决定未绑定素材；绕过程序化 L0-L3。 |
| 程序化 L0-L3 | 校验证据引用、候选绑定、敏感信息、发送动作和安全 fallback。 | 调用新的常规 Judge LLM；用旧固定规则覆盖 RAG 输出。 |
| 幂等发送 | 根据已验证 send actions 发送文本、图片、视频、房源表。 | 重新解释客户意图；重新绑定候选或素材；发送未经 evidence 证明的资产。 |

如果某个提交让同一职责出现在多个阶段并产生竞争判断，必须要求作者收敛为唯一入口。

## 五、事实与证据边界

房源事实必须满足以下条件才可进入客户可见回复或发送动作：

- 每个事实可追溯到工具 evidence，并带有当前 turn 的上下文元数据。
- 房源类 evidence 必须能关联 `listing_id`；多候选场景必须关联当前有效 `candidate_set`。
- 价格、房态、房号、面积、户型、看房方式、图片、视频和房源表不得由 LLM prompt 直接推断。
- 长期记忆只保存非敏感结构化状态，例如候选摘要、`listing_id`、`candidate_set_id`、非敏感 query state；不得保存真实密码或凭证。
- selfcheck 或 fallback 只能收窄、清空或澄清，不能创造新房源事实。

## 六、候选与素材绑定

候选编号和素材发送必须遵守：

- “第一个”“这套”“这几套”“刚才那两个”等引用必须先解析到当前有效 `candidate_set`。
- 当前 turn 如果产生新的候选集合，旧候选编号不能继续隐式复用，除非逻辑明确继承并有测试覆盖。
- 没有 `listing_id` 时不得调用素材发送路径。
- 多房源素材请求必须逐套绑定，不得把一个房间的视频或图片复用于多个房源。
- 素材上传失败、转码失败或文件过大时，只能在原已绑定素材的前提下走压缩重试或链接兜底，不能重新模糊搜索别的素材。

## 七、敏感信息边界

以下内容不得出现在日志、artifact、prompt、长期记忆、通用 evidence、审计报告或测试 golden 中：

- 真实看房密码或完整 viewing 原文。
- token、App Secret、飞书密钥、企业微信凭证、服务器登录信息。
- 完整手机号或可直接还原联系人的敏感号码。
- 未脱敏私有链接、源文件凭证或 raw tool result。

允许保存的只有脱敏状态、哈希、布尔标记、结构化 ID、非敏感 evidence 摘要。若提交新增日志、repr、异常消息、QA artifact 或调试输出，必须审查敏感字段扫描和测试断言。

## 八、Media Manifest 标准

Media Manifest 的审计结论必须符合：

- Manifest 的确定关系只允许是 `listing_id -> media_id`，以及 `media_id -> 本地安全素材元数据`。
- 模糊文件名、目录名、人工命名相似度只能产出 candidate evidence 或 report，不得生成 `MediaItem` 发送绑定。
- 客户可见发送路径不得因为 manifest shadow evidence 而跳过现有工具证据门禁。
- Manifest 中不得保存明文外部 token；需要追踪来源时只能保存哈希或脱敏元数据。
- `MediaStore`、发送阶段和测试必须能证明无 `listing_id` 不发送。

## 九、Snapshot/Router 标准

本阶段 Snapshot/Router 只允许作为 disabled/shadow 或本地 rehearsal 能力：

- 客户路径 `disabled`/`shadow` 的可见结果仍来自 Legacy provider。
- `primary` 配置不得在生产聊天路径生效；非法或未批准模式必须安全退回或结构化失败。
- Router 是唯一 source selection 入口；LLM1、LLM2、工具层、selfcheck、`app/main.py` 不得各自实现 source selection。
- 同一 turn 不得混用 Legacy 和 Snapshot evidence；`source_hash`、`decision_id`、`snapshot_id` 不一致时必须清空相关事实和待发素材。
- Snapshot readiness 只能由程序化门禁判断，不由 LLM 判断。

## 十、测试门禁

审计报告必须写明实际运行的测试命令、结果和适用门禁。

| 门禁 | 使用场景 | 最低要求 |
| --- | --- | --- |
| 专项测试 | 每条业务线提交 | 覆盖该线新增/修改的核心行为，至少包含失败边界或安全边界。 |
| L2 | 触碰 `app/main.py`、`app/services/llm.py`、`app/services/kf_agentic_rag.py`，或影响 Planner、Prompt、候选、素材、密码、上下文 | 运行 RAG 快速回归集合，并保留通过证据。 |
| L3 | 合入 integration 前 | 至少一次全量本地测试通过。 |
| L4 | release/cutover 前 | 连续稳定性、parity、rollback/cutover safety、secret scan 全部通过。 |

若提交只改文档，仍需至少运行 `git diff --check` 或说明为何无需业务测试。若测试无法运行，必须给出原因和风险，不得默认通过。

## 十一、Legacy 删除标准

任何“替换旧规则/旧入口”的提交必须按以下顺序完成：

1. 新实现落地，并证明属于正确 RAG 阶段。
2. 专项测试覆盖新实现的成功路径、失败路径和安全边界。
3. 新实现成为唯一生产入口，且调用方统一。
4. 删除旧调用方、旧 prompt、旧 fallback、旧固定规则或旧素材绑定分支。
5. 使用 `rg` 全仓搜索确认旧入口不再参与生产判断，并在审计报告中记录搜索关键词和结果。

允许暂存 legacy 的唯一理由是：尚未批准 primary/cutover，或 legacy 是明确的 disabled/shadow 兼容层。此时必须写明保留位置、保留原因、removal milestone 和测试覆盖。

## 十二、风险等级

| 等级 | 定义 | 合入建议 |
| --- | --- | --- |
| Blocker | 命中硬性阻塞项，或缺少必要测试证据。 | 不可 cherry-pick。 |
| High | 改客户可见行为、核心 RAG 阶段、素材/密码/候选绑定、Snapshot/Router 或发送阶段。 | 只有专项测试 + L2/L3 证据充分时才可考虑。 |
| Medium | 不直接改客户可见结果，但影响契约、测试工具、shadow adapter、日志或 artifact。 | 需要专项测试或清晰的静态审查证据。 |
| Low | 文档、注释、非生产说明，且不改变脚本或运行行为。 | 可通过文档审查和 diff 检查。 |

客户可见行为包括：回复文本、是否追问、是否发送图片/视频/房源表、发送顺序、是否暴露联系方式或密码、是否改变房源事实来源。

## 十三、审计报告模板

每次审计待 cherry-pick commit，报告必须包含以下字段：

```text
审计对象：
- 分支/commit：
- 基线：
- 改动文件：

阶段归属：
- 问题重写/意图分析：
- Planner：
- 工具执行：
- 结构化会话记忆：
- 自检回流：
- 发送阶段：
- 房源/素材同步：
- 运维部署：
- 测试覆盖：

客户可见行为：
- 是否改变：
- 变化说明：

风险等级：
- 等级：
- 理由：

硬性阻塞项检查：
- H1 主线：
- H2 Judge LLM：
- H3 固定回复分支：
- H4 工具 evidence：
- H5 listing/candidate 绑定：
- H6 敏感信息：
- H7 Media Manifest：
- H8 Snapshot/Router：
- H9 测试门禁：
- H10 Legacy 删除：
- H11 报告完整性：

测试证据：
- 专项测试：
- L2：
- L3：
- L4：
- 其他静态检查：

Legacy 删除/保留：
- 删除项：
- 保留项及原因：
- 全仓搜索证据：

结论：
- 是否可 cherry-pick：
- 阻塞项：
- 后续要求：
```

## 十四、Cherry-pick 结论口径

- `可 cherry-pick`：未命中阻塞项，测试证据满足风险等级要求，报告完整。
- `暂缓 cherry-pick`：方向可接受，但缺测试、缺 legacy 搜索、缺报告字段或存在需主 agent 确认的非阻塞风险。
- `不可 cherry-pick`：命中任一硬性阻塞项，或存在客户事实错误、素材错绑、敏感信息泄露、生产 primary 切换、绕过 RAG 分支。

H 线在收到明确 cherry-pick 指令前，只给出以上结论和证据，不执行合入动作。
