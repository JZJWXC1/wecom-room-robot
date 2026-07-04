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