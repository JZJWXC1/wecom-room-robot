# M1D2B2 InventorySnapshot Primary Cutover Rehearsal Runbook

本文记录 M1D2B2 的本地 primary 全链路演练、故障回退和未来切换门禁。本轮只允许本地 `tmp_path`/临时目录演练；未获得 `APPROVE_DEPLOY` 时禁止 SSH、部署、上传、服务重启、访问飞书或企业微信线上服务。

## 本地演练范围

- 构建完全虚构的本地 Snapshot。
- 使用 `InventoryReadRouter(mode="primary")` 进行本地 primary replay。
- 与 Legacy Provider 做 query golden parity。
- 验证同一 turn 锁定同一 `snapshot_id/source_hash/decision_id`。
- 验证 `strict` 与 `legacy_whole_request` 整体 fallback，不允许半轮混用。
- 注入 pointer、manifest、private viewing secret、PNG hash/size 故障。
- 复用共享 public artifact secret scan；不得新增第二套 scanner。
- 演练本地 current pointer rollback。
- 输出 Cutover Readiness Evaluation、性能摘要和 Legacy Removal Report。

## Cutover Readiness Gate

只有全部满足时，后续人工切换评估才可继续：

- `current_snapshot.json` 存在且只指向安全相对路径。
- Snapshot directory 通过 `SnapshotValidator.validate_directory`。
- `SnapshotReader.health().status == "ok"`。
- schema version 受当前 Router 支持。
- `reconciliation_passed=true`。
- `blocking_count=0`。
- `public_artifact_secret_scan_passed=true`。
- 区域 alias coverage 五项均为 0。
- primary replay parity case 全部通过。
- public artifact、replay report、cutover report、prepared outbound package、send action metadata 不含密码、手机号、token、canary 或开发机绝对路径。

## M1D3 Stability Gate Addendum

M1D3 cutover stability gate must not rely on pytest-only fixtures or implicit `tests/conftest.py` environment setup. Local primary replay must construct its own in-memory legacy provider and pass when the process default `INVENTORY_SOURCE` is not `local_cache`.

Before any final cutover evaluation:
- run full pytest three consecutive times;
- run local snapshot primary replay three consecutive times;
- run `stability_replay_cases(..., min_cases=20)` and require 100% legacy/snapshot parity;
- compare listing id order and rent signatures for every parity case;
- require `evaluate_cutover_readiness(...).required_parity_cases >= 20`;
- keep `safe_to_cutover=false` if any alias, pointer, reconciliation, secret scan, candidate ordering, price signature, or parity result is unstable.

## Secret Scan Boundary

M1D2B2 继续使用 `app/services/inventory_snapshot_shadow.py::scan_public_artifacts_for_sensitive_text` 的结构化扫描语义。

允许：

- `sha256/source_hash/snapshot_id/decision_id/evidence_id` 等结构化机器 ID 中出现手机号形态数字片段。

必须阻断：

- 普通业务文本中的手机号形态。
- 嵌套 list/dict 普通字段中的手机号形态。
- secret/password/token canary。
- manifest 中业务描述字段泄露的敏感文本。
- Windows 用户目录绝对路径。

不得通过跳过整个 JSON、整个 manifest、任意长字符串或具体测试文件名来规避扫描。

## 本地 Rollback Rehearsal

本地 rollback 只写临时 root 下的 `current_snapshot.json`：

1. 构建并激活 v1 Snapshot。
2. 构建并激活 v2 Snapshot。
3. 校验 v2 为当前 pointer。
4. 调用本地 pointer switch 回到 v1。
5. 重新通过 `SnapshotReader.get_current_pointer()` 和 `SnapshotReader.get_snapshot(v1)` 校验。

生产 rollback 仍需要明确 `APPROVE_DEPLOY`，并且必须保留旧 CSV、旧 rewrite index、旧 PNG 和旧 `InventoryService` 作为回退路径。

## Fault Injection Matrix

| 故障 | 预期 |
| --- | --- |
| 删除 `current_snapshot.json` | readiness false，原因包含 pointer missing |
| 篡改 public manifest hash | readiness false，原因包含 snapshot integrity failure |
| 篡改 private viewing secret | readiness false，private manifest hash mismatch |
| 篡改 PNG | readiness false；发送前 sheet artifact provider 返回 mismatch |
| public artifact 泄露手机号/canary/path | readiness false，secret scan failed |

## Legacy Removal Report

M1D2B2 不删除旧生产路径：

| 保留项 | 原因 | removal_milestone |
| --- | --- | --- |
| `InventoryService` | 生产客户读取仍处于 disabled/shadow，primary 未获批 | primary 切换获批并过回滚窗口后 |
| 旧 CSV/rewrite index/PNG | legacy_whole_request 回退和生产安全 | primary 稳定性复审后 |
| `LegacyInventoryReadProvider` | golden parity 和整体 fallback 契约 | primary 成为唯一批准读取源后 |

## 禁止项

- 不连接服务器。
- 不部署。
- 不启用服务器 primary。
- 不切换生产读取。
- 不修改客户回复、LLM Prompt、Planner、自检或发送。
- 不接入图片和视频素材。
- 不删除旧 CSV/index/PNG。
- 不自动进入 M1D3。
