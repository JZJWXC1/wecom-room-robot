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

## 待办登记（批5）

- 验收摘要/manifest 引用 `test_inventory_cache_provenance.json` 的
  `source_snapshot_time` 与 `fixture_version`（裁决 ②a 的摘要侧落地）。
