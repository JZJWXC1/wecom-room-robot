# 孤儿工作包隔离 patch(状态:隔离待审,禁止直接 apply)

- 来源:main 分支工作树脏改动(基线 main HEAD `1ed182de`),2026-07-04 取证隔离。
- 取证台账:`docs/audit/orphan-changes-disposition-20260704.md`(三层证据:blob 哈希零命中、
  13 个功能符号 fix tip 零命中、media-binding 行核对)。
- 用户裁决(2026-07-04):
  1. **不合入、不重放**——fix 分支同题实现已通过完整验收+第二裁判复核,孤儿版未经评审
     且相对 fix 基线漂移 2842 行,直接 apply 不可行也不应行;
  2. **不弃置**——已登记结构债「孤儿工作包采纳评审」(到期 2026-07-06,见 CLAUDE.md 结构债节)。
     评审方式=按功能逐项与 fix 现实现对比,有价值项以**重新实现+测试**方式吸收;
  3. 优先淘金 QA runner 新 gate:**重复外发动作检测**(`has_duplicate_external_send_actions`,
     含 AB-AB 序列折叠)与**免押费率梯度断言**(`expected_deposit_fee_rate_tiers`/
     `expected_deposit_selfcheck_path`)——此两项 fix 现有判分体系没有。

## 文件清单

| 文件 | 内容 | 规模 |
|---|---|---|
| orphan_main_full_backup_20260704.patch | 处置前全量备份(7 文件合一) | 717 行 |
| orphan_main_app_main.patch | 候选集精筛保护 + actions 去重 + media-binding 会话级哈希 | +77 行 |
| orphan_main_kf_llm2_outbound.patch | 内部措辞人性化改写(四类 evidence) | +95 行 |
| orphan_main_kf_outbound_validation.patch | L3 `customer_visible_internal_leak` 校验 | +10 行 |
| orphan_main_server_online_dialog_qa_runner_v2.patch | QA runner 新 gate(泄漏/重复外发/免押费率) | +52 行 |
| orphan_main_test_kf_llm2_outbound.patch | 人性化改写配套测试 | +185/-1 行 |
| orphan_main_test_kf_outbound_validation.patch | L3 新校验配套测试 | +14 行 |
| orphan_main_test_wecom_kf.patch | 候选集精筛保护配套测试 | +53 行 |
| orphan_main_test_server_online_dialog_qa_runner.patch | QA runner gate 配套测试(全新文件,git 历史无) | 64 行 |

另有 `git stash`(`orphan-main-dirty-20260704`,存于 main 分支 stash)可原样恢复 7 个跟踪文件。
