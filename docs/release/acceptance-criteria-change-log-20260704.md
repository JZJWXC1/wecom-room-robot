# 口径变更清单

日期：2026-07-04

## P0-1 批2 新增约束（fixture 生成器守卫）

| 测试 | 改前断言 | 改后断言 | 理由 |
| --- | --- | --- | --- |
| `tests/test_qa_fixture_guards.py::test_generated_fixture_never_contains_viewing_password_tokens` | 无（旧 fixture 手工维护，已提交版本实际含 `101004#` 密码 2 处） | 生成产物（fixture+溯源 meta）全文不得命中 `\d{4,}#`，且清洗不吞掉同字段正常语义 | 用户裁决 ②b：敏感红线要求看房密码不得进入 QA artifact；旧 fixture 已发生泄漏 |
| `tests/test_qa_fixture_guards.py::test_existence_gate_probes_absent_from_generated_fixture` | 无（华丰欣苑曾以"存在"身份进入合成 fixture，与真实表语义反转） | 存在性 gate 探针 `高塘运都9-402B` 与语义反转纪念探针 `华丰欣苑14-2-901` 必须不在生成产物中（小区级+房号级双断言） | 用户裁决 ②c：防止"不存在房源"场景因 fixture 合成条目语义反转而误判 |
| `tests/test_qa_fixture_guards.py::test_provenance_carries_source_snapshot_time_and_counts` | 无（旧 fixture 无溯源信息，验收摘要无法声明数据出处） | 溯源 meta 必带 `source_snapshot_time`（=源缓存 synced_at_iso）、行数一致性、fixture sha256 | 用户裁决 ②a：验收摘要必须引用快照时间；P0-2 用服务器最新缓存重新生成后跑最终验收 |
| `tests/test_qa_fixture_guards.py::test_generation_is_deterministic_for_same_snapshot` 等其余三项 | 无 | 同一快照重复生成字节级一致；列契约 11 列锁定；源 meta.hash 失配拒绝生成 | fixture 可审计、可复现，消费方列契约显式化 |

## 待办登记（批3 迁移时执行）

- `tests/test_qa_utf8_inputs.py::test_l4_qa_inventory_fixture_contains_required_entities`：
  旧断言锚定合成实体（兴业杨家府/杨家新雅苑/杨乐府），换血后按真实快照实体更新口径，
  并同步再生 `tests/fixtures/qa/test_rewrite_inventory_index.json`（复用
  `app/services/rewrite_inventory_index.write_rewrite_inventory_index`）。
  实测换血失败面 = 全量 1272 中仅此 1 项。
- 已提交 fixture/溯源产物的直接守卫（密码零命中、探针缺席）随换血一并落地。
- 精确集合断言评估（用户裁决追加）：评估把孤儿工作包发现的零成本断言
  （免押费率梯度、免押自查路径）纳入精确断言范围，结论记录于批3/批4 台账。
