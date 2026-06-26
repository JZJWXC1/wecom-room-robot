# Media Manifest Adapter

## C 线归属

本轮 Media Manifest Adapter 属于「房源/素材同步」阶段的只读 shadow evidence。它只建立：

- `listing_id -> media_manifest`
- `media_manifest -> media_id`
- `media_id -> local_path/source_url`

它不接入问题重写、Planner、工具执行、自检回流或企业微信发送阶段，不改变客户可见回复，也不改变旧图片/视频发送路径。

## 字段约束

`MediaItem` 和 shadow evidence 固定暴露以下素材绑定字段：

- `media_id`
- `listing_id`
- `variant`
- `sha256`
- `local_path`
- `source_file_token`
- `source_url`
- `modified_at`
- `binding_method`
- `access_verified`

`source_file_token` 当前保存的是源文件 token 的 SHA-256 哈希，不保存明文 token。`access_verified` 只表示本地文件存在且校验通过，或原视频 URL 在 manifest 中已有可用证据；它不是外部服务在线探测结果。

## 模糊匹配边界

模糊文件名匹配只能进入 `MediaBindingReport.fuzzy_candidates` 作为人工复核 evidence，不能生成 `MediaItem`，不能出现在 `MediaManifestShadowAdapter.evidence_for_listing()` 中，也不能直接决定发送。

## 发送路径

`MediaStore.media_manifest_evidence_for_listing()` 只是只读辅助方法。旧的 `list_room_database_videos()`、`list_room_database_images()`、`original_video_sources_for_paths()` 行为保持不变，本轮没有把 `media_manifest.json` 纳入真实发送决策。
