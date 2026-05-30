# 飞书资料库接入

房源表可以放飞书多维表格，房间图片和视频可以放飞书云空间文件夹。机器人读取多维表格后会写入 `data/inventory_cache.csv`，读取飞书云空间后会把视频同步到 `room_database/video`，把图片同步到 `room_database/images`。微信客服回复时仍然会把本地文件上传为企业微信临时素材，再原生发送图片或视频给客户。

`.env` 配置示例：

```text
INVENTORY_SOURCE=feishu_bitable
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=your_feishu_app_secret
FEISHU_BITABLE_APP_TOKEN=your_bitable_app_token
FEISHU_BITABLE_TABLE_ID=your_bitable_table_id
FEISHU_BITABLE_VIEW_ID=
FEISHU_DRIVE_ROOT_FOLDER_TOKEN=your_drive_folder_token
FEISHU_SYNC_MEDIA_ON_STARTUP=false
FEISHU_INVENTORY_SHEET_TOKEN=your_sheet_token
FEISHU_INVENTORY_SHEET_SYNC_ON_STARTUP=false
FEISHU_INVENTORY_SHEET_CHECK_SECONDS=300
INVENTORY_IMAGE_SYNC_STATE_PATH=data/inventory_image_sync_state.json
```

手动同步房源表：

```bash
curl -X POST http://127.0.0.1:8000/admin/inventory/refresh
```

手动同步飞书云空间图片和视频：

```bash
curl -X POST http://127.0.0.1:8000/admin/feishu/sync-media
```

手动检查飞书电子表格是否变动，并在变动时重新生成房源表 PNG：

```bash
curl -X POST 'http://127.0.0.1:8000/admin/feishu/sync-inventory-image?force=true'
```

配置 `FEISHU_INVENTORY_SHEET_TOKEN` 后，客户在微信客服里索要“房源表/表格/截图”时，服务会先读取飞书电子表格 revision 和单元格内容生成指纹。指纹有变化才会导出 xlsx，在服务器使用 LibreOffice 渲染有内容区域并覆盖 `room_database/inventory_*.png`，然后再发送最新图片。

飞书应用需要开通 `drive:export:readonly` 或 `docs:document:export` 任一导出权限，否则无法把电子表格导出为 xlsx，也就无法自动重绘房源表图片。

建议飞书云空间目录：

```text
房源资料/
  小洋坝三区12-1003-2/
    客厅.jpg
    卧室.jpg
    看房视频.mp4
  星桥6-901-4/
    客厅.jpg
    看房视频.mp4
```

同步后本地目录会变成：

```text
room_database/images/小洋坝三区12-1003-2/客厅.jpg
room_database/video/小洋坝三区12-1003-2/看房视频.mp4
```

飞书应用需要开通云文档/云空间和多维表格相关权限，并把多维表格、云空间文件夹授权给该应用。
