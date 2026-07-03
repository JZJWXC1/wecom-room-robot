# 口径变更清单

日期：2026-07-03

## 本轮 W01T2

| 测试 | 改前断言 | 改后断言 | 理由 |
| --- | --- | --- | --- |
| `tests/test_wecom_kf.py::test_area_alias_filter_excludes_cross_region_rows` | 只验证跨区白田畈被排除，实际候选只断到 `大华海派风景6-703-2` | 精确候选集必须等于 `星桥锦绣嘉苑20-1606A`、`小洋坝家园一区6-201C`、`小洋坝家园二区7-1001E`、`大华海派风景6-703-2` | 防止只靠“不含跨区”碰巧通过，同时漏掉北软组真实 ground truth |
| `tests/test_wecom_kf.py::test_execute_tools_applies_region_whitelist_after_normal_inventory_search` | 只断言白名单后留下 `大华海派风景6-703-2` | 断言 `inventory_rows`、`region_whitelist.labels`、`tool_candidates` 三处都精确等于 W01T2 四套候选 | 验证 normal search 错区/漏召回时，`all_rows` 区域白名单能恢复完整候选集 |

## 受控模板改造期间补登

| 测试 | 改前断言 | 改后断言 | 理由 |
| --- | --- | --- | --- |
| `tests/test_wecom_kf.py::test_llm2_production_literal_greeting_uses_controlled_renderer` | 允许 LLM2 先产出“我先帮您确认一下最新房态，稍后给您准确回复。”，再断言 outbound validation 要求 rewrite | literal greeting 不进入自由 LLM2；断言受控 renderer 直接输出问候，且回复不含“稍后” | 问候属于业务问答/寒暄受控模板，不应让 LLM2 生成泛化等待承诺再靠校验改写 |
| `tests/test_wecom_kf.py::test_controlled_password_reply_stays_out_of_internal_artifacts_and_final_llm` | 回复开头断言为“这套看房方式我发你，密码按下面这条为准。” | 回复开头断言为“看房密码如下。” | 密码回复由受控模板直接承担客户可见话术，避免“我发你”等未来动作承诺和内部通道语感 |
| `tests/test_wecom_kf.py::test_production_controlled_channels_cover_contract_password_deposit_and_viewing` | 合同回复开头为“这单可以直接走定房和电子合同。”；密码回复开头为“这套看房方式我发你。” | 合同回复开头为“合同、定金和订房联系方式如下。”；密码回复开头为“看房密码如下。” | 合同/定金/密码都是受控 slot，客户可见开头应来自模板，不能保留旧 LLM2 原话或拟人承诺 |
| `tests/test_kf_dual_llm_production.py::test_production_validator_accepts_evidence_bound_viewing_contact_for_password_question` | 看房联系回复断言为“这套看房要先联系确认，我把联系方式发你。” | 看房联系回复断言为“看房需要联系确认，联系方式如下。”，并断言 `reply_text_owner=controlled_template` | 看房联系方式是受控 slot，回复层必须明确已经给出联系方式，而不是承诺稍后发送 |

## 本轮新增约束

| 测试 | 改前断言 | 改后断言 | 理由 |
| --- | --- | --- | --- |
| `tests/test_kf_llm2_outbound.py::test_budget_match_by_pay2_must_disclose_payment_tier_for_w01t2_xingqiao` | 无回归约束；只能靠人工判断“星桥押二付一 1800 命中” | 当候选仅靠押二付一命中预算时，客户可见回复必须包含 `押二付一1800`，并保留 `押一付一1900` | 防止 LLM2 把“押二档命中”说成笼统“1800以内”，让独立裁判可按付款档复核 |
