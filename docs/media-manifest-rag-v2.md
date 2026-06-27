# Media Manifest RAG v2 绑定边界

本轮改动归属：房源/素材同步、只读证据输出、测试覆盖。

## 精确绑定规则

- `MediaItem` 必须表达 `listing_id`、`media_id`、`media_type`、`source_kind`、`source_path_hash`、`source_record_id`、`confidence`、`ambiguity`、`candidate_only` 和 `manifest_version`。
- 只有 `binding_method=listing_id`、`confidence>=0.99`、`ambiguity=false`、`candidate_only=false` 的 item 会被 `MediaManifestShadowAdapter` 作为 `send_ready=true` evidence 返回。
- 同一个素材路径或文件名里出现多个 `listing_id` 时，归为歧义素材，下载到人工复核区，不进入 manifest 的可发送证据。

## 候选证据规则

- 模糊文件名、中文标签或目录名匹配只能写入 `MediaBindingReport.fuzzy_candidates`。
- fuzzy candidate 必须标记 `ambiguity=true`、`candidate_only=true`、`send_ready=false`，并使用 `binding_method=fuzzy_filename`。
- fuzzy candidate 不能生成 send-ready evidence，不能直接决定图片或视频发送。

## 原视频与微信可发视频

- 微信可发视频使用 `source_kind=wecom_video_file`。
- 原视频本地文件使用 `source_kind=original_video_file`。
- 原视频链接使用 `source_kind=original_video_link`。
- 链接、文件名、素材路径只允许作为内部证据字段，不允许直接拼进客户可见回复。

## Production read-only adapter

`MediaManifestProductionAdapter` 是 production-ready 的只读 evidence 适配器，只暴露精确 `listing_id` 绑定、非歧义、非候选、`confidence>=0.99` 的 media。`MediaStore.media_manifest_evidence_for_listing()` 默认使用该 adapter，并在 evidence 中标记 `adapter_mode=production_read`。

旧的 `MediaManifestShadowAdapter` 保留为兼容入口，过滤规则与 production adapter 相同；它不会放宽 fuzzy/candidate-only 媒体。

## 未接入发送决策

`MediaStore.media_manifest_evidence_for_listing()` 仍是只读辅助方法。旧的 `list_room_database_videos()`、`list_room_database_images()` 和 `original_video_sources_for_paths()` 行为保持不变；本轮没有修改 `app/main.py`、真实发送逻辑、企业微信上传逻辑或部署脚本。
