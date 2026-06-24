# M0.x Local Checkpoint Plan

本文件是 M0.8 的本地 checkpoint 方案。它不代表已经执行 `git add`、`git commit`、分支创建或 M1 开发。

## Version Identity

- 当前 HEAD：`a0d9e80dc5b7f7eebb34bee67cdc5f9cd883f564`
- 原始工作区：`<original-worktree>`
- 当前 Codex Worktree：`<codex-worktree>`
- CodeGraph DB：`<original-worktree>/.codegraph/codegraph.db`
- CodeGraph DB SHA256：`b2f5bffd8906ed209895350ae8685fbc2ace97c75ed720093fc0c24910f0a708`
- CodeGraph node count：`2996`
- CodeGraph edge count：`7220`

M0.5C 最后一次记录：

- 全量 pytest：`518 passed, 1 deselected, 2 warnings, 2 subtests passed`
- 固定 QA：`completed=true, passed=true, exit_code=0, high=0, medium=0, fallback=0, network_call_count=0`
- 随机 fallback QA：`completed=true, passed=false, exit_code=3, high=6, medium=2, fallback=60, network_call_count=0`

注意：随机 fallback QA 只能称为 offline fallback regression QA，不能称为真实模型质量或上线质量结果。

## Change Classification

完整分类见 `qa_artifacts/m08_checkpoint/change_classification.csv`。

概要：

- A：M0 前已有业务或规则捕获改动。
- B：M0.x 测试、安全、QA 或文档改动。
- C：同时包含 A/B，无法按整个文件安全分离。
- D：仅 EOL 噪声。本轮没有确认“只包含 EOL”的安全提交文件。
- E：本地生成物或不应提交内容。
- F：来源无法确认。

重点重叠文件：

- `AGENTS.md`
- `.gitignore`
- `tests/test_wecom_kf.py`

这些文件不能只靠整文件提交来证明属于单一来源。

## Strategy A: Single Local Non-deployable Baseline Commit

推荐。

含义：

- 包含当前经过敏感扫描的代码、测试、文档和必要 QA 脚本改动。
- 明确标记为 WIP / NOT FOR DEPLOYMENT。
- 不 push。
- 目的只是冻结当前本地状态，让后续 M1 diff 可追踪。

优点：

- 不需要猜测 hunk 来源。
- 能保留当前实际可运行的本地测试状态。
- 能把未跟踪但已被主链路引用的 `app/services/kf_orchestrator_flow.py` 纳入可复现状态。

风险：

- 包含 M0 前业务改动和 M0.x 基建改动的混合状态。
- 不能作为部署版本。
- 随机 fallback QA 仍有 high/medium 业务问题，不能作为上线质量绿灯。

## Strategy B: Split Commits

不推荐，除非人工逐 hunk 审核并能证明来源。

理论拆分：

1. M0.x 测试、安全、QA、文档。
2. M0 前业务改动和未跟踪 Orchestrator。

当前不推荐的原因：

- `AGENTS.md` 同时包含项目规则和 M0 安全边界。
- `.gitignore` 同时涉及既有忽略和 M0.x artifact 管理。
- `tests/test_wecom_kf.py` 同时包含业务回归和 M0.5B/M0.5C 相关覆盖。
- `app/main.py` 和多个 `app/services/*` 已经是 M0 前业务改动，不能由 M0.x 自动解释。

不得为了实现方案 B 猜测 hunk 来源，也不建议自动 `git add -p`。

## Orchestrator Handling

`app/services/kf_orchestrator_flow.py` 当前：

- 是未跟踪文件。
- 已被当前 `app/main.py` import 和调用。
- 有直接测试 `tests/test_kf_orchestrator_flow.py`。
- 不发送消息、不调用 LLM、不执行工具、不写上下文。

如果不纳入本地 checkpoint，当前行为不可复现。因此方案 A 中建议纳入，但必须标记为“前置未提交业务状态”，不是 M1 成果。M1 不应继续修改它。

## Safety Scan

完整扫描摘要见 `qa_artifacts/m08_checkpoint/sensitive_scan_summary.txt`。

扫描范围：拟纳入 checkpoint 的 allowlist 文件。

明确排除：

- `.env`
- `.codegraph/`
- `.understand-anything/`
- `qa_artifacts/m0*_baseline/`
- `qa_artifacts/m0*_checkpoint/`
- `data/`
- `media/`
- `room_database/`
- `server_snapshots/`
- 虚拟环境、临时文件、PNG、视频和真实房源导出物

当前扫描发现多个疑似模式，包括环境变量名、测试字段名、固定联系电话和运维命令文本。扫描没有输出任何秘密原文；提交前仍应做 staged 敏感扫描并人工确认这些命中是否为预期内容。

## Recommendation

推荐方案 A：创建本地不可部署 checkpoint commit。

该 commit 的唯一用途是冻结当前本地基线，便于 M1 从一个可追踪提交启动。它不能部署、不能 push、不能代表业务质量通过。

M1 必须从用户批准后的 checkpoint commit 启动，而不是从当前未提交工作区直接启动。
