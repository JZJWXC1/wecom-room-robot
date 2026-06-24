# Legacy Rule Inventory

本文是 M1A 旧实现审计，不删除生产调用链中的代码。

## 将被 Snapshot 取代的旧同步路径

| 旧路径 | 当前唯一生产入口 | 现状 | removal_milestone | 删除前必须通过的测试 |
| --- | --- | --- | --- | --- |
| `InventoryService._save_cache/_read_cache/_reload_cache_if_file_changed` | `InventoryService.refresh/all_rows/search/snapshot` | 直接读写活动 CSV | M1D | Snapshot Reader 搜索回归、同轮锁定测试、全量 pytest |
| `InventoryService._read_public_document` | `InventoryService.refresh` 的 public document 分支 | 公开文档 fallback | M1D | 飞书源失败保留上一快照、无公开文档依赖测试 |
| `InventoryService._read_image_inventory_text/_parse_image_rows` | `InventoryService.refresh/all_rows/search/snapshot` 的 local_image 分支 | OCR 图片 fallback，会生成 Markdown cache | M1D 或保留为手工灾备 | OCR fallback 不在生产默认路径、密码不入 Prompt 测试 |
| `scripts/refresh_rag_inventory_cache.py` 直接写 CSV/index | systemd `wecom-room-robot-rag-cache-sync.timer` | 只刷新活动 cache 和 rewrite index | M1C | Snapshot 构建脚本测试、timer dry-run、health snapshot 状态 |
| `InventoryImageSyncer._replace_inventory_images` | `InventoryImageSyncer.refresh_if_changed` | 直接替换 `room_database/inventory_*.png` | M1C | PNG 快照目录、失败不切 pointer、发送房源表同快照测试 |
| `write_rewrite_inventory_index` 直接写 `data/rewrite_inventory_index.json` | `app/main.py`、两个脚本 | 非原子写，且当前含 viewing 原文 | M1B | rewrite index 无密码、tmp+replace、schema 校验 |

## 重复生成入口

- `app/main.py::_refresh_inventory`：刷新 `InventoryService` 后调用 `_write_rewrite_inventory_index`。
- `app/main.py::_build_inventory_rewrite_index`：persisted index 缺失时现场调用 `write_rewrite_inventory_index`。
- `scripts/sync_feishu_region_inventory.py::refresh_rewrite_inventory_index`：Region 同步后刷新 cache/index。
- `scripts/refresh_rag_inventory_cache.py::refresh_cache`：定时刷新 cache/index。
- `InventoryImageSyncer.refresh_if_changed`：独立刷新 PNG，与 CSV/index 没有同一原子边界。

Snapshot 后这些入口应统一为“构建候选快照 -> 校验 -> 切 current pointer”。

## 直接读取活动 CSV 或临时文件的调用

- `InventoryService._read_cache` 读 `settings.inventory_cache_path`。
- `InventoryService._reload_cache_if_file_changed` 根据活动 CSV mtime/size 重新加载。
- `InventoryService._read_cache_meta` 读 `settings.inventory_cache_meta_path`。
- `InventoryImageSyncer._current_images` 读 `settings.inventory_image_glob` 和 `settings.inventory_image_path`。
- `app/main.py::_current_inventory_images` 读 `room_database/inventory_*.png` 和 legacy original PNG。
- `load_rewrite_inventory_index` 读 `settings.rewrite_inventory_index_path`。

## main.py 中绕过统一 Snapshot Reader 的入口

- `_inventory_rows_for_resolution` 直接调用 `inventory.all_rows(refresh_if_needed=False)`。
- `_execute_tools` 直接调用 `inventory.search` 和 `inventory.all_rows`。
- `_generate_reply_result` 直接调用 `inventory.format_rows` 和 `inventory.snapshot`。
- `_current_inventory_images` 直接 glob 活动 PNG。
- `/health` 直接返回 `_inventory_cache_meta_for_prompt`。
- `/admin/inventory/refresh`、`/admin/feishu/sync-inventory-image`、`/admin/feishu/sync-region-inventory` 分别刷新不同活动产物。
- startup 直接 `inventory.refresh()`，可改变 cache meta。

## 旧 fallback、repair、override、normalize 函数

| 函数 | 唯一调用方/调用范围 | M1 处理 |
| --- | --- | --- |
| `InventoryService._read_public_document` | `InventoryService.refresh` | Snapshot Builder 后移除生产调用 |
| `InventoryService._read_image_inventory_text` | `InventoryService.refresh` local_image 分支 | 降级为手工灾备或删除 |
| `InventoryService._parse_image_rows` | `all_rows/search` local_image 分支 | Snapshot 后不进入生产 RAG |
| `InventoryService._normalize` | `InventoryService.refresh` | 可迁移到 Snapshot Builder |
| `InventoryService._spreadsheet_values_to_frame` | `_read_feishu_inventory_sheet` 和测试 | 可迁移到 Snapshot Builder |
| `dedupe_rows` | `group_rows_by_community` 和测试 | 改为 duplicate conflict 阻断，不再静默覆盖冲突 |
| `group_rows_by_community` | Region sheet 写入和测试 | 保留用于飞书表排版，不作为 snapshot 去重规则 |
| `RegionInventorySheetSyncer.repair_*` | `sync_target_sheet` | 保留飞书表格式修复，不影响 Snapshot Reader |
| `InventoryImageSyncer._replace_inventory_images` | PNG 渲染结尾 | 改为写 snapshot PNG 目录 |
| `_row_viewing_summary` | main.py 多处回复和自检 | 保留客户显式看房场景，默认不持久化密码 |
| `_viewing_evidence` | main.py viewing 工具证据 | 改为唯一 password-on-demand 工具出口 |
| `kf_context_memory.summarize_row` | context normalize/record | 删除 `viewing` 原文持久化 |

## 可删除死代码

M1A 全仓搜索未确认任何库存相关纯死代码可安全删除；本轮不删除代码。

## 删除前总门槛

- `pytest -q` 通过。
- `python -m compileall app` 通过。
- Agentic RAG 固定连续对话覆盖集通过。
- 随机 10 问保底通过。
- 服务器部署后健康接口、服务状态、两个定时器状态、无人值守凭证完整性检查通过。
