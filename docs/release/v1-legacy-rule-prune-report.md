# V1 旧规则清理报告

本轮归属：问题重写/意图分析、Planner 工具后回流、自检回流、发送阶段、测试覆盖、文档清理。未修改 InventorySnapshot、MediaManifest、Outbox、release gate 专项实现；未 SSH、未部署。

## P1 修复结论

- 本轮采用“受控通道”，未恢复合同/定金或看房密码旧客户可见直出主路径。
- 合同/定金：`rule_evidence.contract_contact` 进入受控 `contract_contact` evidence/action，真实联系电话只在程序拥有的 action `sensitive_payload` 中保存，由发送边界渲染；LLM2 只能写 caption/slot，不能自由生成手机号。
- 显式看房密码：`rule_evidence.viewing.rooms` 在用户明确问密码时进入受控 `viewing_password` evidence/action；validation 仅在 `user_asked_password=True` 且 action/claim 引用 `viewing_password` evidence 时放行。用户只问看房、不问密码时，只能走 `viewing_contact`，不会泄露密码真值。
- 免押+水电：仍走 `deposit_policy` 证据和 LLM2/validation 自检；未新增固定直出。
- 合并提醒：F 与 A 合并时，必须以 A 的 outbound validation gate hunk 为准，不能丢掉 A 对 `PreparedOutboundPackage` 的发送前 gate。本轮新增的 `contract_contact`、`viewing_password`、`viewing_contact` 受控 action 已按 evidence-bound 形态设计，用来接入 A gate。

## P1 验收覆盖

- `tests/test_wecom_kf.py::test_production_controlled_channels_cover_contract_password_deposit_and_viewing` 覆盖 production 合同/定金、显式密码、免押+水电、看房预约，以及“问看房但未问密码”不泄露密码。
- `tests/test_wecom_kf.py::test_outbound_validation_allows_password_only_when_user_asked_and_evidence_bound` 覆盖 validation 对显式密码的 evidence-bound allowlist：用户未问或 evidence 缺失均不能放行。

## 删除清单

| 删除对象 | 原职责 | V1 替代入口 | 覆盖测试 |
| --- | --- | --- | --- |
| `docs/legacy-rule-inventory.md` | M1 阶段旧库存/旧规则审计文档，内容已过期且会继续暗示 legacy 路径可保留 | 本报告作为 V1 终态清理记录；库存切换事实仍以 `docs/inventory-read-router.md`、`docs/rag-v2-*` 和专项文档为准 | 文档删除由本报告记录 |
| `ReplyGenerator.plan_kf_reply_text` | 旧工具后单 LLM 直接生成客户可见 `reply_text`，与双 LLM LLM2 outbound 重叠 | `ReplyGenerator.compose_kf_outbound_production` / `compose_kf_outbound_shadow`，由 LLM2 只负责话术，不决定素材目标和事实 | `tests/test_llm.py::test_legacy_plan_kf_reply_text_method_removed`、`tests/test_llm.py::test_reply_generator_routes_each_rag_stage_to_configured_model` |
| `_generate_reply_result` 中旧 `plan_kf_reply_text` 调用分支 | Planner 缺 `reply_text` 时尝试旧单 LLM 补文本 | `planner_output_gate` 回 Planner；重试后仅允许已有工具证据进入受控 fallback | `tests/test_wecom_kf.py::test_planner_missing_reply_before_retry_does_not_enter_selfcheck`、`tests/test_wecom_kf.py::test_removed_planner_reply_text_after_retry_uses_tool_grounded_inventory_reply` |
| `app/main.py::_reply_for_deposit_and_utilities` | 免押/水电/价格固定话术直出 | 免押事实进入 `rule_evidence.deposit_policy`；水电/价格必须由房源工具证据支撑，最终由 LLM2/Planner 文本和 validation 校验 | `tests/test_wecom_kf.py::test_legacy_deposit_utilities_direct_reply_is_removed`、`tests/test_wecom_kf.py::test_deposit_and_utilities_evidence_survives_without_direct_reply_helper` |
| `app/main.py::_reply_for_utilities_and_viewing` | 水电 + 看房/密码固定话术直出 | `viewing_evidence` 仍作为 Planner 工具证据；客户可见文本由 LLM2/Planner 生成，`_constraint_consistency_selfcheck` 校验水电、看房字段和联系方式 | `tests/test_wecom_kf.py::test_utilities_and_viewing_validation_keeps_both_fields` |
| `app/main.py::_reply_for_contract_contact` | 合同/定金固定联系话术直出 | `rule_evidence.contract_contact` 提供确定性证据；最终文本由 LLM2/Planner，`_safe_fallback_for_intent` 仅作为 validation 失败后的安全阀 | `tests/test_wecom_kf.py::test_safe_contract_fallback_contains_contact_numbers` |
| `app/main.py::_reply_for_viewing`、`_customer_visible_viewing_text` | 看房/密码固定话术直出和旧局部脱敏 | 密码边界由 `_row_viewing_summary`、`_constraint_consistency_selfcheck`、`_local_human_context_selfcheck`、`_outbound_package_selfcheck` 共同校验 | `tests/test_wecom_kf.py::test_legacy_viewing_direct_reply_is_removed`、`tests/test_wecom_kf.py::test_viewing_validation_allows_password_only_when_explicitly_requested` |
| `KfAgenticRagService._retry_or_fallback` 客户可见 `fallback_text` 输出 | 旧 selfcheck 直接携带可发送兜底文本 | selfcheck 只返回 `retry_instruction` 给 Orchestrator 回流；客户可见兜底由 main 的 validation/发送阶段统一组包 | `tests/test_kf_agentic_rag.py::test_agentic_rag_assessment_retries_bad_deposit_reply`、`tests/test_kf_agentic_rag.py::test_agentic_rag_assessment_blocks_misleading_video_sent_reply_when_pending` |

## 保留安全阀及归属

| 保留对象 | 阶段 | 保留原因 | 覆盖 |
| --- | --- | --- | --- |
| `_content_wants_*`、`_deterministic_signals` | 预处理 | 只生成内部信号和工具需求，不生成客户可见主回复 | `tests/test_wecom_kf.py` 多轮工具计划覆盖 |
| `_force_inventory_sheet_task`、`_force_deposit_task`、`_force_contract_task` | 问题重写/意图分析 | 只修正明确意图，避免 LLM 漏掉房源表/免押/合同工具；不直接发送文本 | `tests/test_wecom_kf.py` 意图强制相关覆盖 |
| `_safe_fallback_for_intent` | validation / 自检回流后的最终安全阀 | 只在 Planner/LLM2 已失败或返回不安全文本后使用，确保免押不说免费、合同给正确号码、房源表不谎称已发 | `tests/test_wecom_kf.py::test_safe_deposit_fallback_contains_fee_tiers`、`test_safe_contract_fallback_contains_contact_numbers` |
| `_outbound_package_selfcheck` | validation / 发送前 | 保护素材错发、文件不存在、动作与文本矛盾、原视频误称、listing 绑定等发送包边界 | `tests/test_wecom_kf.py` outbound selfcheck 相关覆盖 |
| `_reply_for_sendable_action_fallback` | 发送阶段 | 仅在已经有验证过的图片/视频/房源表动作且自检因口吻等原因失败时，保留已验证动作说明；不重新选素材 | `tests/test_wecom_kf.py::test_selfcheck_retry_fallback_preserves_valid_video_actions` |
| `_reply_for_inventory_search`、`_final_inventory_evidence_fallback` | 自检回流 / 工具证据兜底 | 当前仍用于 Planner 重试后已有房源工具证据时，避免退回“请重新发小区房号”；事实只来自工具结果 | 建议下一轮在 LLM2 production 稳定后删除，见下表 |

## 下一轮建议删除对象

| 对象 | 当前不能删的原因 | 下一轮删除门槛 |
| --- | --- | --- |
| `ReplyGenerator.rewrite_kf_message` | 双 LLM production 之外的本地/旧 shadow 测试仍用它生成工具前计划 | `kf_dual_llm_mode=production` 成为唯一客服入口，LLM1 task packet 覆盖旧 rewrite golden |
| `_reply_for_inventory_search` | Planner 重试后仍承担纯工具证据列表兜底，防止客户已给条件却被要求重复补小区房号 | LLM2 production 对 inventory/search 文本 golden 稳定，且 final fallback 不再需要工具证据列表生成 |
| `_reply_for_prepared_media`、`_reply_for_missing_media` | 发送阶段仍需要在素材动作存在/缺失时保护动作说明和缺失说明 | MediaManifest + LLM2 action captions 全量接管并通过素材错发回归 |
| `_safe_fallback_for_intent` | 仍是 validation 连续失败后的最后安全阀 | LLM2/final selfcheck 能稳定输出结构化 safe_fallback package，且免押/合同/房源表 fallback golden 全覆盖 |
