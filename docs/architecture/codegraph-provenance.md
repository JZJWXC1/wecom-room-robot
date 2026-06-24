# CodeGraph Provenance

本文件记录 M0.7-RETRY 对 CodeGraph 来源的核验结果。文档中只使用占位符路径；完整本机路径仅保存在 `qa_artifacts/m07_checkpoint/` 下。

## Worktree

- 当前 Codex Worktree：`<codex-worktree>`
- 原始工作区：`<original-worktree>`
- Git common dir：原始工作区的 `.git`
- 两个工作区当前 HEAD 相同：`a0d9e80dc5b7f7eebb34bee67cdc5f9cd883f564`

## CodeGraph DB

- DB 位置：`<original-worktree>/.codegraph/codegraph.db`
- 工具元数据：
  - `indexed_with_version=1.0.1`
  - `indexed_with_extraction_version=24`
- 开始与结束快照一致：
  - SHA256：`b2f5bffd8906ed209895350ae8685fbc2ace97c75ed720093fc0c24910f0a708`
  - size：`9707520`
  - node count：`2996`
  - edge count：`7220`
  - mtime：`2026-06-24 17:48:37`

结论：本轮审计期间 DB 文件稳定，没有观察到 hash、大小、mtime、节点数或边数变化。

## Four-file provenance

CodeGraph DB 的 `files` 表中，以下文件的 content hash 与 `<original-worktree>` 文件一致，与 `<codex-worktree>` 当前文件不一致：

- `app/main.py`
- `app/services/kf_orchestrator_flow.py`
- `app/services/kf_turn_flow.py`
- `app/services/kf_agentic_rag.py`

因此，本轮能证明的结论是：

- CodeGraph DB 对应 `<original-worktree>` 的物理文件状态。
- 不能把该 DB 直接当成 `<codex-worktree>` 当前未提交工作区的固定可复现基线。
- `app/services/kf_orchestrator_flow.py` 的 CodeGraph 节点对应 `<original-worktree>/app/services/kf_orchestrator_flow.py`，因为 DB 中该文件 hash 与原始工作区文件 SHA256 相同。

## Watcher

观察到多个 `codegraph serve --mcp` 进程，其中一个命令行显式包含：

```text
codegraph serve --mcp --path <original-worktree>
```

进程工作目录无法从本轮使用的 `Win32_Process` 字段可靠读取，标记为 `unknown`。未停止 watcher，未重建索引。

## Baseline status

该 DB 可以作为“来源核验证据”，但不能单独称为完全可复现架构基线。原因：

- DB 指向 `<original-worktree>`，不是当前 `<codex-worktree>`。
- 当前工作区存在大量未提交改动。
- `app/services/kf_orchestrator_flow.py` 是未跟踪文件，但已被原始工作区的 CodeGraph DB 索引。

M1 开始前若要使用 CodeGraph 作为严格基线，需要先确认采用哪个工作区、哪个 commit、是否包含未提交文件，以及是否需要在受控条件下重新生成索引。
