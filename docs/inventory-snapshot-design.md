# InventorySnapshot 设计预检

本文是 M1A 设计预检产物，只描述目标架构和迁移约束，不实现生产同步逻辑。

## 当前链路摘要

- 飞书房源表同步入口有三类：`scripts/sync_feishu_region_inventory.py` 定时同步四区源表到目标总表；`scripts/refresh_rag_inventory_cache.py` 刷新 `InventoryService` 缓存和 rewrite index；`app/main.py` 暴露 `/admin/inventory/refresh`、`/admin/feishu/sync-inventory-image`、`/admin/feishu/sync-region-inventory`。
- `InventoryService` 当前读取源包括 `local_image`、`local_cache`、`feishu_bitable`、公开文档；运行时事实主要落在 `data/inventory_cache.csv` 和 `data/inventory_cache_meta.json`。
- `RegionInventorySyncService` 从飞书多维表读取记录，经 `normalize_region_records` 标准化为 `RegionInventoryRow`，再由 `RegionInventorySheetSyncer` 写目标飞书电子表格，素材另由 `RegionInventoryMediaSyncer` 处理。
- `InventoryImageSyncer` 从飞书电子表格读取 values 和导出 xlsx，渲染到 `room_database/inventory_*.png`，状态写入 `data/inventory_image_sync_state.json`。
- `rewrite_inventory_index.py` 当前从最新 rows 生成 `data/rewrite_inventory_index.json`，供 `_understand_message` 的问题重写和实体解析使用。
- `app/main.py` 当前在问题重写、工具执行、回复生成、健康检查、发送房源表阶段直接读取 `inventory.all_rows/search/snapshot/cache_meta` 或 `room_database/inventory_*.png`。

## 目标

InventorySnapshot 要把同一轮同步产物收束成不可变快照：结构化库存、rewrite index、房源表 PNG、报告和指针必须来自同一个 `snapshot_id`。运行时只通过统一 Snapshot Reader 读取当前成功快照，失败同步不得污染线上读路径。

## 目录结构

建议根目录默认使用 `data/inventory_snapshots`，可通过配置覆盖。

```text
data/inventory_snapshots/
  current_snapshot.json
  locks/
    sync.lock
  tmp/
    <snapshot_id>.tmp/
  snapshots/
    <snapshot_id>/
      manifest.json
      sync_report.json
      source/
        source_values.json
        source_meta.json
        source.xlsx
      inventory.json
      inventory.csv
      rewrite_inventory_index.json
      png/
        inventory_01.png
        inventory_02.png
      private/
        viewing_secrets.json
```

`private/viewing_secrets.json` 与 `inventory.json` 同快照保存，但只允许 viewing tool 读取。rewrite index、Prompt 上下文和普通日志不得包含真实密码。

## 快照标识

`snapshot_id` 生成规则：

```text
YYYYMMDDTHHMMSSZ_<source_hash_12>
```

- 时间使用 UTC，保证跨 Windows 和 Linux 一致排序。
- `source_hash_12` 取 `source_hash` 前 12 位，用于人工排查。
- 若同秒同源重复生成，内容 hash 相同则不切换；内容不同则追加 `_<attempt>`，并在 `sync_report.json` 记录原因。

## source_hash 与 schema_version

- `schema_version` 固定从 `inventory_snapshot.v1` 开始；任何字段语义、目录布局或敏感字段策略变化都要升级。
- `source_hash` 基于规范化后的源 payload 计算 SHA-256。payload 包含：源类型、飞书 sheet metadata、revision/range、原始 values、合并单元格信息、导出 xlsx 文件 hash、同步代码版本、字段映射版本。
- 不把 token、App Secret、真实密码以外的凭证写入 source payload；源 token 只写 `source_kind` 和脱敏标识。
- `manifest.json` 同时保存 `source_hash`、`schema_version`、`snapshot_id`、`created_at`、`row_count`、`file_hashes`。

## 字段模型

每条房源拆成三层：

- `raw_fields`：源表原始字段名和值，均为字符串，保留审计用途。
- `fields`：标准字段，包含 `area`、`community`、`room_no`、`layout_description`、`layout_class`、`rent_one`、`rent_two`、`remark`、`status`。
- `sensitive`：只放 `viewing_text`、`has_password`、`needs_contact`、`availability_status` 等 viewing tool 需要字段；其中真实 `viewing_text` 不进入 rewrite index。

标准字段映射：

| 标准字段 | 当前来源别名 |
| --- | --- |
| `area` | 区域、商圈、板块、位置 |
| `community` | 小区、社区、楼盘、小区名 |
| `room_no` | 房号、房间号、编号、门牌 |
| `layout_description` | 户型描述、户型、描述、户型详情 |
| `layout_class` | 户型分类、户型标签、房型 |
| `rent_one` | 押一付一、押一付、押一 |
| `rent_two` | 押二付一、押二付、押二 |
| `remark` | 备注、水电、水电费、说明 |
| `viewing_text` | 看房方式密码、看房方式、看房密码、密码、门锁密码 |

## 合并单元格和过滤规则

向下填充只适用于表格结构字段：

- `area`：区域标题行或区域列值向下填充到后续房源行，直到下一个区域标题。
- `community`：合并小区单元格或上一非空小区向下填充到后续房号行。
- 禁止向下填充 `room_no`、价格、备注、看房方式密码。

过滤规则：

- 表头行、重复表头行、纯区域标题行、纯宣传行、全空行不进入 `inventory.json`。
- 无 `community` 或无 `room_no` 的行不进入库存。
- 明确已租/下架/不租状态的行过滤，过滤原因写入 `sync_report.json.filtered_rows`。
- 宣传行识别以“欢迎、推荐、全佣、免押、联系方式、电话”等提示为辅助，不得误删有房号的房源行。

## 字符串保真

- `room_no`、`viewing_text`、价格字段全部按字符串保存，不做数值类型转换。
- 保留房号中的横杠、字母、前导零和大小写；仅额外生成 `normalized_room_no` 用于检索。
- 保留密码中的 `#`、空格和中文说明；不得把密码写入 rewrite index、日志摘要或 Prompt。
- CSV 输出使用 UTF-8 with BOM，并对所有字段按 CSV writer 规则转义，避免 Excel 打开后误转数字。

## listing_id

M1 v1 稳定 ID：

```text
listing_id = "lst_" + sha256(normalize_community + "\0" + normalize_room_no)[:16]
```

同一小区同一房号就是同一套房源。`inventory.json` 同时保存：

- `listing_id`
- `listing_key`
- `normalized_community`
- `normalized_room_no`
- `source_record_ids`

未来迁移方案：

- M1B 先引入 `listing_id_map.json`，记录旧 key 到 listing_id 的绑定。
- M2 如出现小区改名或房号格式修正，允许通过人工确认的 alias/migration 表保留 listing_id。
- 若同一 key 出现两个不同源记录且价格/户型/密码冲突，同步阻断，不自动生成两个 listing_id。

## 重复房源阻断

重复检测键为 `normalized_community + normalized_room_no`。

- 完全重复：字段完全一致，保留一条，报告 `deduplicated_rows`。
- 非完全重复：任一标准字段或敏感字段冲突，快照校验失败，不切换 `current_snapshot.json`。
- 同步报告要列出冲突字段名、脱敏后的房源 key、源记录行号或 record_id。

## 产物

`inventory.json`：

- 机器主读产物，包含 `schema_version`、`snapshot_id`、`source_hash`、`generated_at`、`listings`。
- 每条 listing 包含标准字段、检索字段、非敏感状态和指向 sensitive 的同快照引用。

`inventory.csv`：

- 人工审计和回归对比产物，字段顺序稳定。
- 包含 `listing_id` 和标准字段，不包含真实密码；真实看房方式仅在 `private/viewing_secrets.json`。

`rewrite_inventory_index.json`：

- 保留区域、小区、房号、价格范围、户型、媒体状态、availability summary。
- 删除当前 `room_index[].viewing` 原文字段。
- 只保留 `viewing_summary`、`availability`、`has_password` 等布尔/枚举，不含真实密码。

`png/`：

- 与同一 source_hash 同步生成。
- 先写入临时目录，完成图片存在性和尺寸校验后再进入快照目录。

`sync_report.json`：

- 包含本轮输入源、过滤行、重复行、校验结果、旧快照引用、是否切换指针、失败原因、耗时。
- 不包含真实密码和凭证。

`current_snapshot.json`：

```json
{
  "schema_version": "inventory_snapshot_pointer.v1",
  "snapshot_id": "<snapshot_id>",
  "source_hash": "<source_hash>",
  "snapshot_path": "snapshots/<snapshot_id>",
  "created_at": "<iso8601>",
  "activated_at": "<iso8601>",
  "row_count": 0,
  "health": {
    "status": "ok",
    "age_seconds": 0
  }
}
```

## 原子生成和切换

1. 读取飞书源数据到内存。
2. 在 `tmp/<snapshot_id>.tmp` 生成所有产物。
3. 运行完整校验：JSON schema、行数、唯一 listing_id、PNG 存在、rewrite index 无密码、hash 对齐。
4. 将 tmp 目录原子移动到 `snapshots/<snapshot_id>`。
5. 写 `current_snapshot.json.tmp`，校验可读后 `replace` 到 `current_snapshot.json`。
6. Reader 每次先读取 pointer，再读取 pointer 指向目录；如果任何文件缺失，退回上一成功 pointer 并记录健康警告。

Windows 与 Linux 均使用 `pathlib` 和同一文件系统内 `Path.replace`。不依赖 symlink，因为 Windows 权限和 Linux systemd 环境对 symlink 行为可能不同。

## 失败回退与健康状态

- 同步失败、校验失败、进程崩溃、磁盘写满时，不改 `current_snapshot.json`。
- 服务继续使用上一成功快照。
- 健康状态按 snapshot 年龄分级：
  - `ok`：年龄小于配置阈值。
  - `stale`：超过阈值但仍可读。
  - `missing`：无成功快照。
  - `corrupt`：指针存在但产物校验失败，必须退回上一快照或人工修复。

## RAG 同轮锁定

每个客服 turn 在 `rewrite_intent` 开始时读取一次 `current_snapshot.json`，得到 `inventory_snapshot_id`，并写入：

- structured turn state
- tool evidence
- candidate_set
- confirmed_room
- final selfcheck package

同一轮内所有 `search_inventory`、`all_rows`、rewrite index、PNG、viewing tool 都必须使用同一个 `inventory_snapshot_id`。如果中途 current pointer 切换，本轮不跟随新快照。

## 密码读取边界

- rewrite index 不保存真实密码。
- Agentic RAG 的动态证据默认只知道“有看房字段/需联系/未空出”等状态。
- 只有客户明确问看房、密码、自助看房、打不开门、预约看房，并且目标房源已由工具绑定时，viewing tool 才按 `listing_id + snapshot_id` 读取 `private/viewing_secrets.json`。
- viewing tool 返回的密码只能进入本轮工具证据和客户可见回复，不进入持久 rewrite index、普通候选摘要和日志。

## 并发、崩溃和磁盘

- 同步进程使用跨进程锁 `locks/sync.lock`，包含 pid、created_at、hostname；超过 stale 阈值可接管。
- 每个快照构建在独立 tmp 目录，崩溃后由下一轮清理超时 tmp。
- 写大文件前估算剩余磁盘空间，低于阈值时失败并保留上一快照。
- `current_snapshot.json` 指针写入后立即重读校验；Linux 可增加目录 fsync，Windows 至少保证同卷 replace。

## 路径兼容

- 所有配置均使用相对项目根路径或绝对路径，内部统一转 `Path`。
- artifact 内保存 POSIX 风格相对路径，运行时解析为本机 Path。
- 不把 Windows 盘符路径写入可迁移 artifact。
- systemd 工作目录保持 `/opt/wecom-room-robot`，本地测试保持当前 worktree 根目录。
