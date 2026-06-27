# Media Manifest RAG v2 绑定边界

当前改动归属：房源/素材同步、工具 evidence 收集、发送阶段、测试覆盖。

## 精确绑定规则

- `MediaItem` 必须表达 `listing_id`、`media_id`、`media_type`、`source_kind`、`source_hash`、`source_path_hash`、`source_record_id`、`confidence`、`ambiguity`、`candidate_only` 和 `manifest_version`。
- 只有 `binding_method=listing_id`、`confidence>=0.99`、`ambiguity=false`、`candidate_only=false` 的 item 会被 `MediaManifestProductionAdapter` 作为 `send_ready=true` evidence 返回。
- 企业微信可发送视频和房间图片还必须具备本地文件 `sha256` 校验；发送端会用实际文件重新计算 sha，防止路径串改。
- `source_hash` 必须由读取端重新计算验证，不能只信任 manifest JSON 字段；验证失败时 production evidence 为空。
- 同一个素材路径或文件名里出现多个 `listing_id` 时，归为歧义素材，下载到人工复核区，不进入 manifest 的可发送证据。

## 候选证据规则

- 模糊文件名、中文标签或目录名匹配只能写入 `MediaBindingReport.fuzzy_candidates`。
- fuzzy candidate 必须标记 `ambiguity=true`、`candidate_only=true`、`send_ready=false`，并使用 `binding_method=fuzzy_filename`。
- fuzzy candidate 不能生成 send-ready evidence，不能直接决定图片或视频发送。

## 原视频与微信可发视频

- 微信可发视频使用 `source_kind=wecom_video_file`。
- 原视频本地文件使用 `source_kind=original_video_file`。
- 原视频链接使用 `source_kind=original_video_link`。
- 原视频链接可以作为 production exact evidence 输出，但必须携带 `listing_id/media_id/source_hash/source_record_id` 或 `source_path_hash`；没有原视频 evidence 时不能声称已发送原片或高清源。

## Production read-only adapter

`MediaManifestProductionAdapter` 是 production-ready 的只读 evidence 适配器，只暴露精确 `listing_id` 绑定、非歧义、非候选、`confidence>=0.99` 的 media。`MediaStore.media_manifest_evidence_for_listing()` 默认使用该 adapter，并在 evidence 中标记 `adapter_mode=production_read`。

旧的 `MediaManifestShadowAdapter` 保留为兼容入口；它不会放宽 fuzzy/candidate-only 媒体。

## 生产发送决策

生产模式下，客户可见房间图片/视频发送链路为：

1. 工具阶段按房源 row 的 `listing_id` 读取 Manifest exact evidence。
2. `image_paths/video_paths` 只从该 exact evidence 的 hash-verified `local_path` 生成。
3. 发送阶段重新读取当前 production manifest，再校验同路径、同 `listing_id`、同 `sha256`、同 `source_hash`、同 `media_id` 和 production evidence profile。
4. SendReceipt 成功/失败边界记录 `listing_id/source_hash/sha256`，并在 metadata 标注 Manifest evidence profile。

模糊素材匹配只允许用于候选、报告和缺素材提示；生产聊天路径不会临时触发 Feishu 全量或 on-demand 素材同步。
