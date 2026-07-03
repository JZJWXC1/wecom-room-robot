# 运行时工件消费方审计

审计日期：2026-07-03

本轮结论：代码对齐 commit 7d4b8893。只读 checksum/dry-run diff 中 266 个 tracked 文件 changed_count=0；服务器额外路径均按运行时工件处理，不作为代码漂移。

## 只读审计范围

- 本地代码：`app/`、`scripts/`、`tests/`、`docs/`
- 服务器核对结果：仅使用上一轮只读 checksum/dry-run diff 的路径摘要
- 本轮未 SSH、未删除、未移动、未重启、未部署

## 工件消费方

| 路径/模式 | 生产是否读取 | 当前消费方 | 保留价值 |
| --- | --- | --- | --- |
| `RELEASE_MANIFEST` | 否 | 发布审计/人工核对；当前代码未发现运行时读取 | 保留到对应 release 完成复核 |
| `docs/release/*` | 否 | 人工审计文档；当前代码未发现运行时读取 | 保留为 release 说明和回溯依据 |
| `.media_manifest_staging_*` | 否 | `scripts/sync_feishu_region_inventory.py::refresh_media_manifest` 在一次同步函数内创建临时目录，正常由 `TemporaryDirectory` 自动清理；残留通常表示同步被中断或进程退出异常 | 只作故障法证，不作为生产素材来源 |
| `room_database/media_manifest.json` | 是 | `app/services/media_store.py` 通过 `MediaManifestProductionAdapter` 读取，用于素材证据和发送前校验 | 生产素材证据源，不能删 |
| `room_database/_manual_review/media_manifest_candidate.json` | 否 | 媒体同步 degraded 时写入给人工复核；生产发送仍不读取候选 manifest | 保留到人工处理完成或下一次成功 manifest 发布 |
| `qa_artifacts/inventory_cutover_graph_*` | 否 | `scripts/rehearse_inventory_cutover_graph.py` 和 `inventory_cutover_graph` 运行时写入本次演练报告；后续只在人工/第二裁判复核时按路径读取 | release/cutover 证据，保留到最终验收完成 |
| `qa_artifacts/inventory_cutover_region_sync_*` | 否 | `scripts/sync_feishu_region_inventory.py::_cutover_rehearsal_root` 在同步图中生成 cutover rehearsal root | 同步图证据，保留到对应同步/验收完成 |
| `data/inventory_snapshots/current_snapshot.json` | 仅 primary 模式 | `inventory_read_turn._configured_snapshot_reader()` 指向 `settings.inventory_snapshot_root`，默认是 `data/inventory_snapshots`；不读取 release 目录下的 cutover graph 工件 | primary cutover 后才是生产房源事实指针 |
| `.local_last_deploy_release_path.txt` | 否 | 本地操作者记录上次 release 路径；服务器不应存在 | 应加入本地独有排除项 |

## 当前判断

- `qa_artifacts/inventory_cutover_graph_*` 内的 `current_snapshot.json` 只属于演练根，不是生产 `INVENTORY_SNAPSHOT_ROOT/current_snapshot.json`。
- 生产房源读取当前不会自动扫描 release 目录中的 cutover graph 历史快照。
- `.media_manifest_staging_*` 不是生产素材路径；生产素材证据只应来自 `room_database/media_manifest.json` 及其声明文件。
- 这些工件在最终验收、第二裁判复核、事故法证完成前不删除。

## 结构债登记

| 结构债 | 到期日 | 验收口径 |
| --- | --- | --- |
| 建立预期服务器独有路径清单，包含 `RELEASE_MANIFEST`、`docs/release/*`、`qa_artifacts/inventory_cutover_graph_*`、`qa_artifacts/inventory_cutover_region_sync_*`、`.media_manifest_staging_*` 等 | 2026-07-04 | 只读 diff 可区分代码漂移、服务器独有工件、本地独有文件 |
| 将 `.local_last_deploy_release_path.txt` 加入本地独有排除项 | 2026-07-04 | 下次 checksum/dry-run diff 不再把该文件计为服务器缺失 |
| 运行时工件迁出 release 目录至 `data/` 或专用 runtime root，并配置保留策略 | 2026-07-10 | release 目录恢复不可变；运行时写入路径不落在 release 版本目录 |
| 为 cutover graph、media manifest staging、QA 结果分别建立保留策略 | 2026-07-10 | 可按类型保留最近 N 份或按天数清理，且清理前不影响生产读取 |
