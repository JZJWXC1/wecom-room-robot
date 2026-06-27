# L4 离线 Smoke 约束

L4 release gate 的 dual LLM production package smoke 必须是纯离线 contract smoke。

- `scripts/rag-v2-test-gates.ps1 -Level L4` 显式设置 `APP_ENV=test` 和 `KF_DUAL_LLM_MODE=production`。
- L4 只调用 `scripts/smoke_dual_llm_production.py` 的默认路径，不传 `--allow-live-llm`。
- 默认 smoke 使用 `FakeReplyGenerator` 生成确定性 LLM2 输出。
- 默认 smoke 不导入 `app.config`，不读取本地 `.env`，不实例化真实 `ReplyGenerator`，不连接真实 LLM，也不调用发送通道。
- `--allow-live-llm` 只保留给人工诊断，不能作为 L4 gate 的一部分。

release rehearsal 的总 `ok` 也必须依赖完整审批门禁：

- `requires_approve_deploy=true`
- `approval_guard_before_credential_load=true`
- `approval_guard_before_credential_load` 必须用行级检查判断：`function Require-DeployApproval` 定义不算调用，非函数定义的实际 `Require-DeployApproval` 调用必须早于 `Get-Content -Path $CredentialFile`。

只有两个条件同时满足，`server_ops_approval_guard.ok` 和 rehearsal 总 `ok` 才能为 true。

L4 还必须包含独立历史失败回放 gate：

- 默认要求 `tests/fixtures/qa/historical_failures_synthetic_sanitized.json` 存在。
- fixture 可以是 synthetic sanitized fixture，只用于验证 gate 机制和已知失败形态，不得声称来自真实服务器。
- 回放 artifact 的 `quality_status.high_count` 和 `quality_status.medium_count` 必须都为 0。
- runner 控制台输出和保留的 artifact 必须脱敏，不得包含手机号、查看凭据、token、长 hash 或原始签名。
- `-AllowMissingHistoricalFailures` 只能用于本地继续跑后续检查；它会记录 release blocker，不能作为可上线结果。
