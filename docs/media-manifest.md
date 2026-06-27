# Media Manifest Adapter

## C 线归属

当前 Media Manifest Adapter 属于「房源/素材同步」和「发送阶段」的生产 evidence。它建立：

- `listing_id -> media_manifest`
- `media_manifest -> media_id`
- `media_id -> local_path/source_url`
- `listing_id + media_id + source_hash + sha256 -> SendReceipt`

客户可见房间图片/视频发送必须由 `MediaManifestProductionAdapter` 返回的 exact `listing_id` evidence 驱动。问题重写、Planner、自检主路径不在本文范围内；发送阶段只消费工具层已经收集好的 Manifest evidence。

## 字段约束

`MediaItem` 和 shadow evidence 固定暴露以下素材绑定字段：

- `evidence_id`
- `media_id`
- `listing_id`
- `variant`
- `source_hash`
- `sha256`
- `local_path`
- `source_file_token`
- `source_url`
- `modified_at`
- `binding_method`
- `access_verified`

`source_file_token` 当前保存的是源文件 token 的 SHA-256 哈希，不保存明文 token。`access_verified` 只表示本地文件存在且校验通过，或原视频 URL 在 manifest 中已有可用证据；它不是外部服务在线探测结果。

`source_hash` 是 manifest 有效内容身份，不信任 JSON 字段本身。读取 `media_manifest.json` 时会重新计算内容 hash；如果文件内容被改动但保留旧 hash，或把 hash 字段伪造成另一个 64 位值，production adapter 不会产出可发送 evidence。

## 模糊匹配边界

模糊文件名匹配只能进入 `MediaBindingReport.fuzzy_candidates` 作为人工复核 evidence，不能生成 send-ready `MediaItem`，不能出现在 production send evidence 中，也不能直接决定发送。

## 发送路径

生产模式下：

- `_collect_room_media()` 只按 row 的 `listing_id` 调用 `MediaStore.media_manifest_evidence_for_listing()`，不走模糊路径查找。
- 聊天生产路径不会触发 Feishu on-demand 素材同步；未命中 Manifest evidence 时只返回缺素材。
- `_send_images_with_receipts()` 和 `_send_videos_with_receipts()` 在发送前再次读取当前 production manifest，校验同路径、同 `listing_id`、同 `sha256`、同 `source_hash`、同 `media_id` 和 production evidence profile。
- 视频上传失败仍先尝试企业微信可发送版转码重试；成功或失败都写入 SendReceipt。缺少 Manifest evidence 时直接拦截，不发送说明文字，也不降级为链接。

非生产/兼容模式下，旧的 `list_room_database_videos()`、`list_room_database_images()`、`original_video_sources_for_paths()` 仍保留，用于历史测试和人工排查。
