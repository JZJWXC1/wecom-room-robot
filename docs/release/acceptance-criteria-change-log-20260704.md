# 口径变更清单

日期：2026-07-04

## P0-1 批2 新增约束（fixture 生成器守卫）

| 测试 | 改前断言 | 改后断言 | 理由 |
| --- | --- | --- | --- |
| `tests/test_qa_fixture_guards.py::test_generated_fixture_never_contains_viewing_password_tokens` | 无（旧 fixture 手工维护，已提交版本实际含 `101004#` 密码 2 处） | 生成产物（fixture+溯源 meta）全文不得命中 `\d{4,}#`，且清洗不吞掉同字段正常语义 | 用户裁决 ②b：敏感红线要求看房密码不得进入 QA artifact；旧 fixture 已发生泄漏 |
| `tests/test_qa_fixture_guards.py::test_existence_gate_probes_absent_from_generated_fixture` | 无（华丰欣苑曾以"存在"身份进入合成 fixture，与真实表语义反转） | 存在性 gate 探针 `高塘运都9-402B` 与语义反转纪念探针 `华丰欣苑14-2-901` 必须不在生成产物中（小区级+房号级双断言） | 用户裁决 ②c：防止"不存在房源"场景因 fixture 合成条目语义反转而误判 |
| `tests/test_qa_fixture_guards.py::test_provenance_carries_source_snapshot_time_and_counts` | 无（旧 fixture 无溯源信息，验收摘要无法声明数据出处） | 溯源 meta 必带 `source_snapshot_time`（=源缓存 synced_at_iso）、行数一致性、fixture sha256 | 用户裁决 ②a：验收摘要必须引用快照时间；P0-2 用服务器最新缓存重新生成后跑最终验收 |
| `tests/test_qa_fixture_guards.py::test_generation_is_deterministic_for_same_snapshot` 等其余三项 | 无 | 同一快照重复生成字节级一致；列契约 11 列锁定；源 meta.hash 失配拒绝生成 | fixture 可审计、可复现，消费方列契约显式化 |

## P0-1 批3 口径变更（fixture 换血落地）

| 测试 | 改前断言 | 改后断言 | 理由 |
| --- | --- | --- | --- |
| `tests/test_qa_utf8_inputs.py::test_l4_qa_inventory_fixture_contains_required_entities` | 锚定合成实体：`杨乐府` in communities（真实表只有杨乐府北区/南区）；索引 row_count=14 | 锚定真实快照实体：兴业杨家府/杨家新雅苑/杨乐府北区/杨乐府南区/皋塘运都 in communities，`棠润府15-2-801B`、`皋塘运都16-1-2206` in labels；索引 row_count=40 与 fixture 行数一致 | fixture 换血为快照生成（40 行，source_snapshot_time=2026-07-02 15:12:23，fixture_version=da9cf10fc9f74a5d）；实测换血失败面=全量 1272 中仅此 1 项 |
| `tests/test_qa_fixture_guards.py::test_committed_fixture_*` 三项（新增） | 无（守卫只覆盖生成器逻辑） | 已提交 fixture/溯源 meta/重写索引三产物：密码零命中、存在性探针成立、行数/哈希/出处交叉一致（sha256 口径=LF 规范文本，对 autocrlf 免疫） | 裁决 ②b/②c 对已提交产物的直接强制 |
| 存在性探针口径（新增约定） | 复判报告原文探针为"高塘运都9-402B" | 双探针：房号级 `皋塘运都9-402B`（小区真实存在+房号不存在，双向断言）+ 错别字小区级 `高塘运都`（整小区不存在） | 真实表存在小区**皋塘运都**，与报告写法"高塘运都"仅一字之差——按报告字面用小区级探针会在错别字被"纠正"时发生华丰欣苑式语义反转；双探针两种读法全覆盖 |

附注：
- 旧 fixture 的"杨家牌楼 文教"**区域标签**是合成的，但小区兴业杨家府/杨家新雅苑真实存在
  （归属其他区域组）；纯合成小区为华丰欣苑（真实近似体=华丰新苑）与东新园8-1201 等。
- 重写索引 fixture 由生成器一并再生（复用生产同源 `write_rewrite_inventory_index`，
  含密码脱敏），签名随快照更新。
- P0-2 用服务器最新缓存重新生成时，实体锚点若因房源上下架变化而失效，按本台账流程更新。

## P0-1 批4 口径变更（验收剧本：存在性 gate 窗口）

| 测试/口径 | 改前 | 改后 | 理由 |
| --- | --- | --- | --- |
| `qa_artifacts/run_rag_10windows_10turns_utf8.py` WINDOWS | 10 窗口，无存在性 gate 场景（复判报告整改项②） | 11 窗口：新增 `existence_gate_gaotang`（10 轮），探针=皋塘运都9-402B（房号级）+高塘运都（错别字小区级） | 复判报告："不存在房源必须反问并给近似候选"整轮未测；探针与 fixture 守卫双向锁定 |
| 机器判分 `_turn_problem` | 无存在性判分规则 | 新增 `_existence_probe_problem`：探针被绑定为真实目标 → high；探针被确认存在/承诺发送且无纠偏词 → high | 存在性 gate 必须机器可判，不依赖人工扫描 |
| 窗口数量契约 | 硬编码 `10` 散布 3 处 | `EXPECTED_FULL_WINDOW_COUNT = len(WINDOWS)` 单一事实源 | 防止后续加窗时漏改计数 |
| `tests/fixtures/qa/test_text_full_utf8.json` | 100 问 | 110 问（前 100 问与旧版逐字一致，纯追加） | 与 WINDOWS 常量一致性由 `test_fixture_questions_match_windows_constant_without_importing_source_script` 强制 |
| `tests/test_qa_fixture_guards.py` 新增 4 项 | 无 | 探针常量 runner↔守卫双向锁定；判分规则三态单测（幻觉绑定/无纠偏确认/正确纠偏）；探针房号全局不存在（任何小区不得有 9-402B） | 探针语义漂移即测试失败 |

注：模块名 `run_rag_10windows_10turns_utf8` 为历史名称，实际窗口数以 `EXPECTED_FULL_WINDOW_COUNT` 为准（改名会破坏既有 artifact 溯源链，不改）。

## 免押断言纳入精确断言范围的评估结论（用户裁决追加项，批4 落卷）

- **已覆盖、无需新增**：免押费率区间（5.5%-8%）与自查路径（支付宝→芝麻信用→信用额度→租房板块）
  在 fix 受控模板中已实现（`app/main.py` `_deposit_policy_evidence`/`_deposit_self_check_text`），
  且已有 pytest 锚定：`test_llm1_controlled_contracts.py:458`、`test_wecom_kf.py:2068/2868-2869`、
  `test_production_chain_boundaries.py:271`、`test_kf_outbound_validation.py:343/363`。
- **非零成本、归孤儿包采纳评审（结构债 2026-07-06）**：三档费率梯度按问句差异化输出
  （3个月5.5%/3-6个月7%/6-12个月8%，fix 只输出区间话术）、server QA runner 的
  免押 gate（`expected_deposit_fee_rate_tiers`/`expected_deposit_selfcheck_path`）与
  重复外发动作检测——需重新实现+测试，严禁 apply 孤儿 patch。

## P0-1 批5 口径变更（验收摘要声明数据出处）

落地上一节待办（裁决 ②a 的摘要侧）：验收摘要与 gate 汇总必须声明本轮 QA 实际消费的房源数据出处，无出处或出处失配一律不得放行。

| 测试/口径 | 改前 | 改后 | 理由 |
| --- | --- | --- | --- |
| `run_rag_10windows_10turns_utf8` machine summary `usable_for_release` | passed + full_suite_completed + 计数吻合即可放行，摘要不含数据出处 | 新增第 5 条件 `data_provenance.ok`；payload 与 machine summary 均带 `data_provenance`（schema=`qa_data_provenance.v1`），整轮只解析一次防逐次写盘漂移 | 裁决 ②a：验收摘要必须引用快照时间；无出处的验收产物不可用于放行 |
| 数据出处声明口径（新增约定） | 无 | `qa_fixture` 模式：现场重算 fixture sha256（LF 规范文本口径，与批3 一致）与行数，和溯源 meta 交叉核验，声明 `fixture_version` + `source_snapshot_time`，哈希/行数失配或缺 `source_snapshot_time` 即 ok=false；`server_cache` 模式：必须有缓存 meta 且带同步时间（`synced_at_iso`/`cache_mtime_iso`），声明 `cache_synced_at` | 出处不是自报字段而是现场核验结论——fixture 被篡改或 meta 过期时摘要必须自动失效 |
| `run_kf_qa_gate_graph_utf8` gate release summary | `usable_for_release` = full_suite_completed + 计数吻合 | 新增 `data_provenance_ok`（两个必跑阶段各自 summary.data_provenance.ok 的合取）并入 `usable_for_release`；透出 `fixed_data_provenance`/`random_data_provenance`；跳过的阶段豁免（由 full_suite_completed 拦截，不重复扣分） | 裁决 ②a 在 gate 聚合层的同口径强制：任一必跑阶段缺出处，整卷不可放行 |
| `tests/test_qa_fixture_guards.py` 新增 4 项 | 无 | 已提交 fixture 出处声明成立（`declares_committed_fixture`）；哈希失配拒绝（`rejects_fixture_hash_mismatch`）；server_cache 声明同步时间（`server_cache_declares_sync_time`）；缓存 meta 缺失不放行（`server_cache_missing_meta_is_not_ok`） | 出处核验逻辑的四态守卫，干净检出可跑 |
| `tests/test_qa_utf8_inputs.py` / `tests/test_kf_qa_gate_graph.py` | 三态单测与 gate helper 不感知出处 | 三态单测补出处字段；新增"无出处不得放行"（`machine_summary_requires_data_provenance_for_release`）与"阶段出处失配整卷不放行"（`qa_gate_cli_artifact_not_release_usable_without_stage_data_provenance`） | 摘要层与 gate 层的负向用例，防止条件被静默移除 |

## P0-2 部署前修复批（批6）：候选集清空与幻觉绑定收口

背景：P0-2 部署预检的离线 gate（换血 fixture 首次全量跑动）在 `shiqiao_whole_rent` 第 8 轮抓到 high——候选集被第 6 轮伪锚点误清后，序号 [1,2] 的原视频请求经待发视频单行记录半桶水绑定到单套（清空 bug 家族第 5 次复发 + 幻觉绑定）。310 用例中此为唯一违规；第 11 窗存在性 gate 首次真实跑动 10 轮零问题，random 200 例全绿。

| 测试/口径 | 改前 | 改后 | 理由 |
| --- | --- | --- | --- |
| `tests/test_kf_tool_resolver.py::test_plural_price_comparison_uses_confirmed_room_when_only_one_room_is_contextual`（更名为 `..._with_only_confirmed_room_returns_selection_error`） | 复数比较（"这两套哪个价格低"）在候选集缺失时可由单套 confirmed room 绑定作答 | `selection_error=missing_current_candidate_set` 反问重列候选 | 与判分锚"复数序号目标不完整=high"（`tests/test_qa_utf8_inputs.py` 三态）及 `docs/rag-rule-ownership.md`"candidate_binding 只能绑定显式候选集"裁决直接矛盾；判分锚不得放宽，且单套价格回答两套比较属事实性误导 |
| 清空决策锚点口径（行为修复随记） | `_should_clear_room_context_after_empty_inventory_search` 的锚点分支接受纯文本启发式析出的任意提及（伪词"如果还没来"也算显式锚点） | 改用 `_has_vocabulary_backed_inventory_anchor`：房号/区域别名不变；小区提及必须命中已知小区词表（rewrite 索引 communities，含别名与近似纠偏） | 修错误证据而非给 clear 加例外；错别字小区（高塘运都）经近似纠偏仍命中词表，存在性 gate 语义不变；真实新查询空搜照常清空（正向用例锁定） |
| resolver 选择上下文口径（行为修复随记） | 入口守卫对 pending_video/confirmed room 无条件旁路；`wants_original_video`+pending 非空即绑定并覆盖检索行；pending 任意非空即抑制 selection_error | 待发素材仅在数量覆盖全部显式序号时可作选择上下文；单套 confirmed room 仅可满足单一序号；复数选择缺候选集一律 `missing_current_candidate_set` | 向 `docs/rag-rule-ownership.md` 契约收敛（移除 85e864f1 遗留旁路），杜绝半桶水绑定类幻觉 |
| 新增回归用例 | 无 | resolver 3 项（复数序号+pending 单行不绑/无序号续发仍绑/复数指代+单套 confirmed 不绑）；记忆 2 项（看房追问空搜不清空/已知小区空搜仍清空）；词表锚点 1 项（伪词拒绝+真实/错别字小区放行）；分词伪词现状留痕 1 项 | 结构债"先固化回归用例再动刀"的前置固化（清空 bug 家族 + 本次窗口时间线） |

注：`_anchor_terms` 分词伪造（删词拼接伪词）尝试过空格占位修复，实测破坏既有小区提及契约（短语碎片通过 2-8 字过滤），已回滚并以 `test_anchor_terms_fabrication_is_documented_known_behavior` 锁定现状；分词口径的彻底修复归入记忆生命周期单 owner 重构批次（结构债 2026-07-05）。

## P0-2 fixture 对真再生（批7）

按批3 附注流程执行：服务器缓存经 RagCacheSync 实时刷新（飞书拉取，status=success）后
再生 fixture——40 行（source_snapshot_time=2026-07-02 15:12:23，fixture_version=da9cf10fc9f74a5d）
→ 35 行（source_snapshot_time=2026-07-04 15:12:51，fixture_version=f9078b8158fa2995），
房源上下架自然变动。批3 实体锚点（兴业杨家府/杨家新雅苑/杨乐府北区/南区/皋塘运都 communities，
棠润府15-2-801B/皋塘运都16-1-2206 labels）与存在性探针（9-402B 全局不存在、高塘运都/
华丰欣苑14-2-901 缺席）全部存活，无断言需要更新；守卫+锚点 83 passed，全量 1292 passed。
无口径变更。
## P0-2 部署后热修批（批8）：pending 越界拦截 + 视频转码标记补配

背景：第二裁判部署后复审抓到 P1（pending 覆盖检查按条数比较，"第2套原视频"配 1 条待发记录时 1>=1 穿透并错绑唯一一条；"第2套视频"同状态静默漏报）；用户真实对话实测暴露视频发送 40011 后转码重试未触发（话术已发、视频丢失）。

| 测试/口径 | 改前 | 改后 | 理由 |
| --- | --- | --- | --- |
| resolver pending 覆盖口径 | `len(pending) >= len(indices)`（条数比较） | `max(indices) <= len(pending)`（最大序号拦截） | 裁判 P1 实证：越界序号按条数比较会穿透；最大序号口径同时覆盖复数不足与单序号越界两类 |
| `_video_error_allows_transcode_retry` 标记 | 无 "invalid video size"/"invalid media size"/"40011" | 补配三个标记 | 2026-07-04 生产实测：企业微信临时素材超限实际返回 errcode 40011 + "invalid video size"，旧列表只有"video size exceeded"等变体，漏配导致转码链路（AGENTS.md 规定的 ffmpeg 压缩重试）从不触发 |
| 新增回归 4 项 | 无 | 越界单 pending 错绑拦截（原视频/普通视频两态）+ 满覆盖序号仍可续发（正向）+ 40011 允许转码而鉴权/频控快败 | 裁判 P1 双注释与生产日志实证的固化 |
| `.gitignore` | 取证包 `qa_artifacts/archive_codex_worktree_20260704/` 未隔离 | 显式忽略并注释敏感性（117 个 viewing_secrets.json） | 裁判提交卫生告警：防误 `git add -A` 泄密 |

## 回调重复投递防线批（架构级修复移交会话）：msgid 幂等三层防线

背景：生产实证 2026-07-04 16:01（release 150051，HEAD 7712db6d），企微回调重复投递把同一
msgid 推成新轮次，同一条"房源表"请求被完整处理两次、房源表图片对客重复外发。根因审计、
三层修复比选与 LLM 重试链延迟预算评估全文见
`docs/audit/kf-callback-dedup-latency-budget-20260704.md`。本轮只新增判定约束，
未修改任何既有测试断言。

| 测试/口径 | 改前 | 改后 | 理由 |
| --- | --- | --- | --- |
| `tests/test_kf_callback_dedup.py`（新增 8 项） | 无约束；并发回调可从旧游标重复拉到同一 msgid 并二次全链处理；`_restart_kf_turn` 把纯重复投递当客户追问整轮重放 | sync"读游标-拉取-存游标-认领"临界区必须串行（并发峰值断言=1）；msgid 认领窗口（默认 300s，`WECOM_KF_MSGID_CLAIM_TTL_SECONDS`）内重推与同批分页重叠一律丢弃；纯重复投递不得 bump generation/取消在跑轮次/重放，真实追问（新 msgid）合并重启语义不变；mark_processed 转正永久去重；轮次失败认领过期后放行补处理 | 平台回调是至少一次投递（平台约束，非业务可选项），重复投递不得二次触发 LLM 处理与外发 |
| `tests/test_kf_outbox_msgid_scope_guard.py`（新增 7 项） | 台账仅按轮次域幂等键去重；重复回调开新轮后证据链 id 全变→键值全变，跨轮重放不拦（本次事故台账失守的直接原因） | 同一 msgid 域（客户消息集合摘要+动作身份，`msgid_scope_key` 随记录落盘）已有 SENT/UNCERTAIN 回执或他键在途 pending 时，新轮次 reserve 必须阻断（`msgid_scope_blocks_duplicate`/`msgid_scope_pending_blocks_duplicate`）；FAILED 放行补发；新客户消息（新 msgid 集合）放行；同键重试 attempt 递增语义不变；冷启动/跨实例决策一致 | 同一批客户消息的同一逻辑外发跨轮次只能物理发生一次，且不误拦客户后续的合法重复请求（幂等键剔除轮次域方案已比选否决，见审计文档） |

关联行为变更（非测试口径，登记备查）：
- 新配置 `WECOM_KF_MSGID_CLAIM_TTL_SECONDS`（默认 300 秒，覆盖实测 40s 与理论重试链尾部）；
- `data/wecom_kf_state.json` 新增 `inflight_msgids` 字段（向后兼容，旧文件缺省为空）；
- 台账记录新增白名单内部键 `msgid_scope_key`（旧记录缺省为空，域防线只对新记录生效，
  存量重复窗口由入站认领层覆盖）；
- 孤儿工作包"重复外发动作检测"为 QA runner 判分 gate，与本批生产台账层防线不同层面，
  未 apply 任何孤儿代码（独立重实现，符合处置台账要求）。

验证记录：本批新增 15 项全绿；关联套件（test_wecom_kf 452 项、outbox 增量缓存、入口图、
轮次流）全绿；全量 `python -m pytest -q` = 1333 passed / 1 skipped（既有 Windows ACL 跳过）/
3 deselected（既有标记），为与并行"发送顺序与话术收敛批（批9）"未提交改动共存的合并态
口径。本批按并行会话共存协议只提交回调去重所属文件与 `app/main.py` 的
`_restart_kf_turn` hunk（批1 commit 9d601d7、批2 commit 955b1c7），未夹带批9 改动。
不部署、不 push。

## 发送顺序与话术收敛批（批9，架构级修复移交会话）：视频先传后发 + 失败纠正话术 + 合并话术去重

背景：生产实证 2026-07-04 16:06（与批8 同一轮真实对话）。客户要视频时链路先发
"这是XXX的视频。"再上传视频，40011 上传超限时话术已发出，客户看到"这是视频"却收不到
视频（孤儿话术，转码标记漏配已在批8 修复，但发送顺序缺陷独立存在）；同时 LLM2 合并版
话术（"这是A的视频，这是B的视频。"）与逐条 caption 重复，一次视频请求对客外发 3 条文本。
与"回调重复投递防线批"（回调去重批1/2）为并行移交会话，工作区同期双批开发；
回调批已先行提交，本批经临时索引仅提交本会话变更，不含对方在途暂存内容。

| 测试/口径 | 改前 | 改后 | 理由 |
| --- | --- | --- | --- |
| 视频发送顺序（`_send_videos_with_receipts`） | 先发 caption 文本，再 `wecom_kf.send_video`（上传+发消息合并调用），上传失败=孤儿话术 | 先 `upload_media` 取 media_id（含既有转码重试链，转码后重传），上传成功后才发 caption，再 `send_video_media` 发视频消息；配套动作元数据 `transaction=upload_then_caption_then_video`、新增 `video_msg_ms` 计时 | 上传失败时客户不再收到任何"这是视频"式承诺，从顺序结构上消灭孤儿话术；企微 media 上传与 send_msg 本就是两个平台接口（平台能力，非本项目虚构），拆开不引入新依赖 |
| 视频失败客户可见口径 | 失败仅记回执，客户被静默晾着或被孤儿话术误导 | 确定失败（上传/转码/caption 阶段任何失败，或视频消息确定失败）补发受控模板纠正话术："{房源}的视频这边暂时没发出去，你稍后再让我发一次。"；`send_uncertain`（视频消息可能已送达）禁止补发 | 纠正话术属受控模板层（同"房源表发你了"先例），不经 LLM；未决场景补发会对可能已收到视频的客户构成误导 |
| 视频失败回执与重放语义 | 所有异常统一按 `send_error_is_uncertain` 分类，上传阶段超时也记 `send_uncertain` 并阻断重放 | 视频消息发出前（first_upload/transcode/retry_upload/caption 阶段）的失败一律 `build_failed_receipt`（确定失败，允许重放补发）；仅 `video_message` 阶段保留未决语义 | 上传超时≠送达未决：send_msg 从未调用，消息必然没发出；旧口径把可补发的失败错误地永久阻断 |
| `failure_stage` 口径 | 由时长字段事后推断（`_video_send_failure_stage`，caption 先发假设） | 发送过程中显式跟踪：first_upload/transcode/retry_upload/caption/video_message | 新顺序下时长推断不再成立；显式跟踪消除歧义，`caption` 阶段语义从"话术未发"变为"上传成功后发话术时失败" |
| 合并话术 vs 逐条 caption（`_send_final_actions`） | 合并版最终话术与逐条 caption 同时外发（视频请求 3 条文本） | 合并话术逐子句（去空白与中英文标点后精确匹配）均被"计划外发"的 caption 覆盖时不再单发，收敛为 caption 一处（`final_reply_deduped_into_captions` 落 tool_evidence 备查）；任一子句含 caption 之外信息则照常发送；prepared 与 legacy 两条链路同规则 | 媒体叙述唯一客户可见来源=caption。评估结论：`kf_llm2_outbound` claim/caption 契约与 `production_missing_action_caption`、`require_captions`、`llm2_output_missing_visible_reply` 守卫口径全部不变（caption 仍强制、LLM2 仍须产出非空 reply_text），收敛点放在发送层而非 compose 置空 reply_text——后者会触发 main 验收门（非空 reply 硬条件）连锁重试，影响面见 DECISIONS 比选 |
| 去重安全边界 | — | 只统计文件存在的动作 caption；`suppress_actions` 或空 caption 集不触发去重；生产守卫拦截媒体动作时 caption 与合并文本都不外发（静默留回执） | 防止把客户可见内容删没；守卫拦截场景"静默+回执"优于"这是视频"式假承诺（正是本次事故形态） |
| 死代码清理 | `main._send_videos`（caption 先发旧实现）无任何调用方仍留存 | 删除 | 防"改一处漏一处"（main.py 拆分结构债 2026-07-08 的同源风险） |
| 新增/改造回归 | 视频链路测试桩为合并式 `send_video(path)` | 桩统一拆为 `upload_media`+`send_video_media`；新增 9 项：上传失败无孤儿话术+纠正话术+重放（改造）、视频消息确定失败补纠正话术+重放、上传超时按确定失败可重放、未决不补话术（改造）、prepared 去重/带增量信息保留、legacy 去重/带增量信息保留、子句覆盖规则单测 | 生产实证三形态（孤儿话术/静默/重复文本）全部固化为回归 |

残留与后续：图片链路（`_send_images_with_receipts` + `wecom_kf.send_image`）仍是
caption 先发、上传+发送合并调用，存在同构孤儿话术风险（图片体积小、上传失败率低，
生产未实证），需要 `send_image_media` 拆分后同规则收口，留独立批次。

验证记录：全量 `python -m pytest -q` 1333 passed / 1 skipped（既有 Windows ACL 跳过）/
3 deselected（既有标记），含并行回调批工作区变更的混合状态；本批提交内容另在隔离
worktree 复验（仅本批 hunk 叠加 HEAD），结果见 commit message。

## 用户需求批（批10）：转码等待提示 + 原视频签名直链

需求原文（用户 2026-07-04）：视频大小超限得转码发送，提示客户"视频过大压缩中，请稍等"；客户说太糊/要原视频时发原视频链接。链接来源经用户裁决=服务器签名直链（备选飞书分享链因外部客户打不开、小程序素材页因路由契约在另一仓库，均落选）。

| 测试/口径 | 改前 | 改后 | 理由 |
| --- | --- | --- | --- |
| 转码链路文本口径（`test_video_upload_failure_transcodes_with_ffmpeg_and_retries` 等 2 项断言更新） | 上传超限转码期间客户零感知（静默 30 秒~数分钟） | 确认转码且无缓存时先发受控提示「视频有点大，正在压缩，请稍等。」；缓存命中秒回不发（新增 `test_transcode_cache_hit_skips_wait_notice`）；重复回调重放不重发 | 用户需求①；文案已逐条核验避开出站校验 HARD_FORBIDDEN/FUTURE_SEND/IMMEDIATE 三组正则与 QA BAD_TEXT 表；发送层确定性过程文本沿欢迎语/批9 失败纠正话术先例 |
| 原视频链接证据口径 | `original_video_urls` 仅来自素材库 manifest 的 kind=original_video 条目（生产恒空，回复固定"没有原视频/高清下载链接"） | manifest 无链接证据时，对已绑定视频源文件生成 HMAC 签名直链兜底（`/wecom/media/original`，默认 48h 时效，上限 3 条）；manifest 有链接仍优先；密钥未配置整体关闭回到原口径（三态测试锁定） | 用户需求②；下游展示（"原视频链接："模板）/LLM2 字段白名单/判分器"有 URL 证据即放行"全部现成，本批只补数据生产端 |
| 新增回源端点 `/wecom/media/original` | 无 | GET+签名校验回源 room_database 内文件；签名/时效/路径边界任一失败一律 404（fail-closed，4 项路由测试） | 复用既有 nginx /wecom/ 转发零配置；密钥仅存服务器 .env（已于部署授权下就地生成） |