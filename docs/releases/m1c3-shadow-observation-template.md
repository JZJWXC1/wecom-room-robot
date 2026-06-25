# M1C3 Shadow 观察模板

M1C3 部署后至少观察 3 个不同 `sync_run_id`。观察期只允许 Shadow 旁路，不允许切换客户生产读取入口。

## 通过标准

每次运行必须满足：

- `reconciliation_passed=true`
- `blocking_count=0`
- `public_artifact_secret_scan_passed=true`
- health 不 stale
- 旧同步成功
- 客户查询继续使用旧数据
- 没有重复调用飞书
- 没有重复触发 Shadow
- 没有新增客户回复异常
- 没有真实密码进入日志或公共 artifact

相同 `source_hash` 的重复同步可以记录运行成功，但 readiness 累计必须遵循 M1C2 语义：只有不同 `source_hash` 的成功才累计 `consecutive_passes`。相同 `source_hash` 不得伪装成三次不同数据验证。

## 观察表

| # | sync_run_id | 时间 | source_version | source_hash | snapshot_id | legacy count | snapshot count | matched count | blocking | warning | duration | old sync status | customer path unchanged | operator conclusion |
| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| 1 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| 3 |  |  |  |  |  |  |  |  |  |  |  |  |  |  |

## 每次观察需附证据

- `scripts/check_inventory_snapshot_shadow.py --json` 的安全输出。
- 对应旧同步状态摘要：region sync 或 RAG cache sync 的 `ok`、row count、duration。
- 客户路径未变证据：代码版本、preflight `production_pointer_reader=legacy_reader_unchanged`、抽样回复不包含 Shadow snapshot_id。
- 日志安全抽查：不得出现真实密码、手机号、token、私密链接或飞书原始响应。

## 操作员结论模板

```text
sync_run_id:
结论: 通过 / 失败 / 需复查
失败或复查原因:
是否影响客户生产路径: 否 / 是
是否需要回滚: 否 / 是
记录人:
```
