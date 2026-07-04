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

## 待办登记（批4/批5）

- 精确集合断言评估（用户裁决追加）：新 fixture 落地后，评估把孤儿工作包发现的零成本断言
  （免押费率梯度、免押自查路径）纳入精确断言范围，结论记录于批4 台账。
- 验收剧本补皋塘组窗口与存在性 gate 窗口（探针=皋塘运都9-402B）；
  验收摘要引用 `test_inventory_cache_provenance.json` 的 `source_snapshot_time`。
