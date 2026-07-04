# main 分支孤儿脏改动取证处置记录

- 日期:2026-07-04
- 执行人:Claude(Fable 5),依据用户三卡点裁决 ③
- 基线:main HEAD = `1ed182de`(fix: handle short acknowledgement fallback),fix tip = `eba34f02`
- 安全网:处置前已导出全量备份 `judge/patches/orphan_main_full_backup_20260704.patch`(717 行,含全部 7 个文件脏 diff)

## 取证方法

1. **blob 哈希比对**:对每个脏文件计算 `git hash-object`,与该文件在 `fix/langgraph-hardening-20260703` 全历史所有提交的 blob 哈希逐一比对 → **7 个文件全部无命中**,排除"fix 已提交改动的旧版本"。
2. **新增行存在性检查**:提取脏 diff(vs main HEAD)的新增行,逐行在 fix tip 对应文件中查找 → 各文件均有大量缺失行(main.py 77 行中缺 50,kf_llm2_outbound.py 95 行中缺 88,validation 10 行中缺 6,qa_runner 52 行中缺 47,三个测试文件合计 252 行中缺 88)。
3. **功能符号探测**(决定性证据):以下关键符号在 fix tip 上 `git grep` 全部为零命中:
   `_looks_like_candidate_refinement_query`、`candidate_refinement`、`candidate_context_preserved`、
   `_safe_default_evidence_reply_text`、`_humanized_missing_media_text`、`_humanized_deposit_policy_text`、
   `_INTERNAL_VISIBLE_PATTERNS`、`CUSTOMER_VISIBLE_INTERNAL_PATTERN`、`customer_visible_internal_leak`、
   `collapse_repeated_sequence`、`has_duplicate_external_send_actions`、`expected_deposit_selfcheck_path`、
   `test_empty_candidate_refinement_query_preserves_last_candidate_set`。
4. **media-binding 行核对**:fix tip `app/main.py:7436` 仍为旧版 `media-binding:{sha256(content)}`,脏改动引入的会话级哈希(conversation_id/request_id 参与散列)fix 上不存在。

## 结论

**7 个被改文件 + 1 个未跟踪测试文件构成同一个未收编的孤儿工作包**,内容为三个连贯特性:

- **候选集精筛保护**(app/main.py + tests/test_wecom_kf.py):`_looks_like_candidate_refinement_query` 识别"带燃气优先"类精筛追问,复用 `last_candidate_set` 过滤而非清空房源上下文;空结果时保留候选集并记录 `candidate_context_preserved` 证据;`_execute_tools` 入口 actions 去重;media-binding conversation_id 改为会话级散列。
- **内部措辞对客泄漏治理**(kf_llm2_outbound.py + kf_outbound_validation.py + 两个测试文件):`_INTERNAL_VISIBLE_PATTERNS` 识别"工具未绑定/上一轮只有N套候选/候选N.../XX:图片 暂未找到/packet|planner|traceback"等内部措辞;确定性回退时改走 `_safe_default_evidence_reply_text` 人性化改写(missing_media/target_error/deposit_policy/inventory_candidate 四类);L3 校验新增 `l3.customer_visible_internal_leak`;`_has_target_error_answer` 扩展 `candidate_selection_error`/`target_error` 两个 evidence_type。
- **QA runner 新 gate**(server_online_dialog_qa_runner_v2.py + tests/test_server_online_dialog_qa_runner.py):回复内部措辞泄漏检查(`internal_visible_leak`)、重复外发动作检测(`duplicate_external_send_actions`,含 AB-AB 序列折叠)、免押自查路径与费率梯度断言(`expected_deposit_selfcheck_path`/`expected_deposit_fee_rate_tiers`)。

## 逐文件判定

| 文件 | 脏改动规模(vs main HEAD) | fix 历史 blob 命中 | 判定 | 处置 |
|---|---|---|---|---|
| app/main.py | +77 行(4 个 hunk) | 无 | ③c 独有 | patch 隔离 + stash |
| app/services/kf_llm2_outbound.py | +95 行 | 无 | ③c 独有 | patch 隔离 + stash |
| app/services/kf_outbound_validation.py | +10 行 | 无 | ③c 独有 | patch 隔离 + stash |
| qa_artifacts/server_online_dialog_qa_runner_v2.py | +52 行(main 与 fix 该文件本无分歧,纯新增) | 无 | ③c 独有 | patch 隔离 + stash |
| tests/test_kf_llm2_outbound.py | +185/-1 行 | 无 | ③c 独有 | patch 隔离 + stash |
| tests/test_kf_outbound_validation.py | +14 行 | 无 | ③c 独有 | patch 隔离 + stash |
| tests/test_wecom_kf.py | +53 行 | 无 | ③c 独有 | patch 隔离 + stash |
| tests/test_server_online_dialog_qa_runner.py(未跟踪,64 行) | 全新文件 | **git 全历史不存在**(交接书称"fix 已提交版本"与实况不符) | 孤儿工作包配套测试 | patch 隔离,删除本地副本(内容存于 patch) |

注:`tests/test_server_online_dialog_qa_runner.py` 断言的行为(`internal_visible_leak`、`expected_deposit_selfcheck_path`)仅存在于脏版 qa_runner 中,留在树上必然挂测试,故随包隔离。

## 隔离产物清单(judge/patches/)

| 文件 | 内容 |
|---|---|
| orphan_main_full_backup_20260704.patch | 全量备份(717 行,处置前快照) |
| orphan_main_app_main.patch | app/main.py 脏 diff |
| orphan_main_kf_llm2_outbound.patch | kf_llm2_outbound.py 脏 diff |
| orphan_main_kf_outbound_validation.patch | kf_outbound_validation.py 脏 diff |
| orphan_main_server_online_dialog_qa_runner_v2.patch | qa_runner_v2 脏 diff |
| orphan_main_test_kf_llm2_outbound.patch | test_kf_llm2_outbound 脏 diff |
| orphan_main_test_kf_outbound_validation.patch | test_kf_outbound_validation 脏 diff |
| orphan_main_test_wecom_kf.patch | test_wecom_kf 脏 diff |
| orphan_main_test_server_online_dialog_qa_runner.patch | 未跟踪测试全文(new-file patch) |

另有 git stash 一份:`stash@{0}: On main: orphan-main-dirty-20260704`(7 个跟踪文件,可 `git stash apply` 原样恢复)。

## 待审事项(不合入、不删除)

1. 该工作包针对的问题(内部措辞泄漏、候选集误清空)与 fix 分支 P0/P1 修复方向一致但实现互不相同;patch 以 main HEAD 为基线,fix 上这些文件已有 2842 行演进,**直接 apply 到 fix 必然冲突**,若审后决定收编需人工移植。
2. 是否收编、何时收编,待用户裁决;在此之前 patch 与 stash 均保留。

## 用户终审裁决(2026-07-04,追加)

1. ③c 判定认可,8 个文件按"独有孤儿工作包"处置成立。
2. **不合入、不重放**:fix 分支同题实现已通过完整验收+第二裁判复核,孤儿版未经评审
   且基线漂移 2842 行,patch 直接 apply 不可行也不应行。
3. **不弃置**:登记结构债「孤儿工作包采纳评审」(到期 2026-07-06,见 CLAUDE.md 结构债节),
   按功能逐项与 fix 现实现对比,有价值项以「重新实现+测试」方式吸收;优先淘金 QA runner
   新 gate(重复外发动作检测、免押费率梯度断言)。
4. `judge/patches/` 全部 patch 随批 1 commit 进 fix 入库(纯文本证据,比 stash 耐久),
   附 README 注明隔离待审状态。

## 附记:协作机制验证

本轮取证是一次双向纠错的实例:交接书推断"未跟踪测试文件与 fix 已提交版本同源"、
第二裁判早前推断"脏改动可弃置",均被开发者以 blob 哈希/符号探测证据推翻;
裁判据证据改判并公开留痕。开发者用证据推翻裁判推断、裁判认错改判,两个方向的
纠错都已发生——三角分工(开发者/机器判分/独立裁判)机制有效性由此再次确认。

## 关联处置(同日)

- codex worktree `.codex/worktrees/4513` 已归档(`qa_artifacts/archive_codex_worktree_20260704/`,含 qa_artifacts 1056 文件 + data 4 文件 + tmp 12 文件,共 38M)后移除;git 注册已清除,残留一个被进程占用的空目录壳,重启后可删。
- 主树已于处置完成后 checkout `fix/langgraph-hardening-20260703`,后续批次全部提交在 fix。
