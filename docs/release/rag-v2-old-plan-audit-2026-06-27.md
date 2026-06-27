# RAG V2 old plan audit checkpoint - 2026-06-27

本文把旧 M0-M7 上线计划与 `codex/rag-v2-integration@de2bc0e9` 的 RAG V2 架构重新对齐，并记录 2026-06-27 本轮优先 worktree 的实际进展。本文档只做本地审计和计划归档，不代表已部署或已切 production primary。

## 基线与硬边界

- 唯一新架构基线：`codex/rag-v2-integration@de2bc0e9`。
- 终态主线：确定性预处理 -> LLM1 -> 确定性工具执行 -> LLM2 -> 程序化 L0/L1/L2/L3 -> 幂等发送 + SendReceipt。
- LLM1 只做理解、任务拆解、上下文继承、实体绑定和工具计划，不生成客户可见回复。
- LLM2 只基于工具 evidence 生成口语化话术、claims、action captions，不新增事实、不决定素材/密码/房源绑定。
- 未收到 `APPROVE_DEPLOY` 前，不 SSH、不上传服务器、不重启服务、不修改线上数据。
- 本轮文档不读取、不记录、不输出任何线上密钥、token、App Secret、服务器凭证或 `.env` 内容。

## 旧计划 M0-M7 对齐结论

| 旧计划 | 当前状态 | 可采用性 | 新 RAG V2 归属 |
| --- | --- | --- | --- |
| M0/M0.7 checkpoint | 已完成为历史边界 | 可直接采用为基线说明 | 只保留“不改生产链路”的边界 |
| M1 InventorySnapshot | 大部分已实现，本轮补 stale tmp 清理 | 需要继续接入生产前门禁 | 工具执行 / InventoryReadRouter / SnapshotStore |
| M2 MediaManifest | foundation/shadow 已有，production cutover 未完成 | 需要改造后采用 | 素材工具 evidence，禁止模糊匹配直接发送 |
| M3 强类型契约 | Contracts、LLM1/LLM2 shadow、validation 已合入 integration | 已完成 shadow 层，可采用 | LLM1/LLM2/PreparedOutboundPackage |
| M4 对话和候选状态 | candidate_set 与 evidence boundary 已有，production 双 LLM 未切 | 需要改造后采用 | LLM1 task packet + deterministic tools |
| M5 自检与幂等发送 | L0-L3 validation 已有；SendReceipt/idempotent send 未完成 | 需要改造后采用 | 程序化 validation + 发送阶段 |
| M6 QA/故障注入/Shadow | QA gates、L4 最近已通过；历史失败回放和故障注入仍需扩展 | 需要继续补齐 | release gate / QA artifact |
| M7 发布和回滚 | server ops 文档脚本存在但 release/current、审批、回滚演练仍需补 | 需要改造后采用 | 运维发布，不得自动部署 |

## 旧计划可采用项清单

已完成：

- Contracts、Media Manifest shadow、Inventory Router evidence boundary、LLM1 shadow、LLM2 outbound shadow、Validation、QA gates 已在 integration 基线内。
- InventorySnapshot 已具备 snapshot_id、source_hash、manifest 校验、private viewing secrets、current pointer 和本地 primary replay。
- Router 已支持 disabled/shadow/primary、strict 与 whole-request fallback、turn-level snapshot_id/context 锁定。
- 程序化 outbound validation 已覆盖 L0/L1/L2/L3 的基础结构、事实、需求完成和口语化问题。

可直接采用：

- Snapshot/Router 作为本地 rehearsal 和 future primary 的唯一 source selection 入口。
- `PreparedOutboundPackage`、`Claim`、`ActionCaption`、`SendAction` 契约作为 LLM2 到发送准备层的边界。
- `docs/rag-v2-audit-merge-standard.md` 的 H1-H11 合入阻塞项。

需要改造后采用：

- InventorySnapshot 生产切换：还需要 release gate 证明、线上无人值守同步、失败沿用 current 的运行证据。
- MediaManifest production：必须做到 `listing_id -> media_id` 精确绑定，模糊匹配只能进入候选/报告。
- SendReceipt + 幂等发送：必须覆盖重复回调、发送超时、企微返回不确定、视频上传失败后重试。
- 历史失败回放：必须把 L4 发现过的问题、人工事故样本和随机 QA 失败样本纳入固定 replay。
- release pipeline：必须加入 `APPROVE_DEPLOY`、release/current、health、rollback rehearsal、凭证完整性检查。

暂缓/不建议采用：

- 任何绕过 RAG 主链路的旧固定回复扩展。
- 任何让 LLM2 决定图片、视频、密码或房源绑定的方案。
- 任何从模糊素材文件名直接生成发送动作的生产方案。
- 任何未经 release gate 的 Snapshot primary 生产切换。

## 本轮优先 worktree 进展

| Worktree | 分支 | commit | 归属 | 状态 |
| --- | --- | --- | --- | --- |
| `rag-v2-sensitive-index-boundary` | `codex/rag-v2-sensitive-index-boundary` | `f8f3012` | rewrite index 敏感边界 | 已提交，待审后 cherry-pick |
| `rag-v2-llm2-oralization-shadow` | `codex/rag-v2-llm2-oralization-shadow` | `88c8dbd` | LLM2 shadow 口语化 fallback | 已提交，待审后 cherry-pick |
| `rag-v2-inventory-snapshot` | `codex/rag-v2-inventory-snapshot` | `75fb0e4` | SnapshotStore stale tmp 清理 | 已提交，待审后 cherry-pick |
| `rag-v2-old-plan-audit` | `codex/rag-v2-old-plan-audit` | 本文档 | 旧计划对齐/上线门槛 | 本文档提交后待审 |

## 后续 worktree 计划

### 1. `codex/rag-v2-media-manifest-production`

- 路径：新 worktree 从 `codex/rag-v2-integration` 创建。
- 目标：把 MediaManifest 从 shadow 推到可验证 production-ready 工具 evidence，但不接客户发送 production。
- 允许改：`app/services/media_manifest.py`、`app/services/media_store.py`、素材 manifest 测试和 docs。
- 禁止改：`app/main.py` 的客户可见发送逻辑、服务器配置、线上素材。
- 依赖：sensitive-index-boundary 可并行；SendReceipt 前最好完成。
- 测试：`tests/test_media_store.py`、manifest 专项、无 listing_id 不发送、模糊匹配不产出 send action。
- 合入门槛：错发视频/图片=0，模糊素材自动发送=0。

### 2. `codex/rag-v2-send-receipt-idempotency`

- 目标：定义 SendReceipt、幂等键和重复回调保护，把发送动作从“执行”升级为“可审计提交”。
- 允许改：发送阶段服务、SendAction/SendReceipt 契约、相关 tests。
- 禁止改：LLM1/LLM2 事实判断、素材模糊绑定、线上服务。
- 依赖：Contracts 已有；MediaManifest production 最好先完成。
- 测试：重复 msgid/callback、企微超时、返回不确定、视频上传失败、转码重试、不会重复发送。
- 合入门槛：重复回调重复发送=0；失败可 replay；receipt 不含敏感明文。

### 3. `codex/rag-v2-history-fault-qa`

- 目标：把历史失败和故障注入固化为 QA gate。
- 允许改：`scripts/rag-v2-test-gates.ps1`、`tests/fixtures/qa/`、QA 报告生成脚本。
- 禁止改：生产回复逻辑，除非另开修复分支。
- 依赖：SendReceipt 和 MediaManifest 可并行补 fixture。
- 测试：历史失败回放、重复回调、LLM 超时、视频上传失败、飞书同步失败、候选编号过期、不同客户 case 切换。
- 合入门槛：QA artifact high=0 medium=0，secret scan 通过。

### 4. `codex/rag-v2-release-pipeline`

- 目标：形成服务器上线前的 release/current、预检、健康检查、回滚演练和审批门禁。
- 允许改：`scripts/server-ops.ps1`、release docs、health 检查脚本。
- 禁止改：线上数据、服务器、凭证；未 `APPROVE_DEPLOY` 不得连接服务器。
- 依赖：至少完成 SendReceipt、QA gates 和 release rehearsal。
- 测试：本地 dry-run、缺凭证检查只报告不输出值、rollback rehearsal、health 解析。
- 合入门槛：无人值守凭证完整性检查存在且脱敏；无审批不执行部署。

## 当前上线就绪结论

状态：`NO-GO`。

原因：

- 当前 production 链路仍未切到双 LLM production；LLM1/LLM2 仍是 shadow/adapter。
- MediaManifest production、SendReceipt 幂等发送、历史失败回放、release/current pipeline 仍未完成。
- Snapshot primary 只具备本地 rehearsal 和未来切换基础，未获得 production release gate。
- 未运行服务器测试、未执行部署、未做线上健康检查。

## 本轮测试证据索引

- sensitive-index-boundary：专项 9 passed；库存/snapshot/QA 178 passed, 1 skipped；全量 790 passed, 1 skipped, 1 deselected, 6 subtests passed；compileall 通过；`git diff --check` 通过。
- llm2-oralization-shadow：专项 25 passed；全量 788 passed, 1 skipped, 1 deselected, 6 subtests passed；compileall 通过；`git diff --check` 通过。
- inventory-snapshot：专项 86 passed, 1 skipped；全量 787 passed, 1 skipped, 1 deselected, 6 subtests passed；compileall 通过；`git diff --check` 通过。
- old-plan-audit：文档分支只需 `git diff --check` 和状态检查；不改生产代码。

## 合入建议

1. 先审计并 cherry-pick `f8f3012`，因为它降低 rewrite index 敏感泄露风险。
2. 再审计并 cherry-pick `75fb0e4`，因为它补齐 SnapshotStore 崩溃残留 tmp 的运行卫生。
3. 再审计并 cherry-pick `88c8dbd`，因为它只影响 LLM2 shadow/fallback 口语化，不接 production。
4. 最后 cherry-pick 本文档，作为后续 M2/M5/M6/M7 的执行索引。

合入 integration 后必须重新运行至少一次全量本地测试；进入 release 前仍需 L4、QA artifact high=0 medium=0、secret scan、rollback rehearsal 和明确 `APPROVE_DEPLOY` 授权。
