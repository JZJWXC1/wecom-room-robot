# M1C3-FIX1 First Shadow Root Cause

## 背景

M1C3B 首次服务器 Shadow reconciliation 出现：

- `rewrite_index_mismatch.communities`: blocking
- `rewrite_index_sensitive_field_present`: warning
- `rewrite_index_mismatch.area_aliases`: warning
- public secret scan passed

本轮只做本地代码审计和修复；未连接服务器，未读取服务器现场文件。仓库中不存在 `.local/m1c3-diagnostics/`，因此没有使用线上诊断 artifact。

## 逐项确认

1. Shadow 输入 rows 是否与本轮旧 rewrite index 使用完全相同的数据

   - 代码可确认：`scripts/sync_feishu_region_inventory.py::refresh_rewrite_inventory_index` 和 `scripts/refresh_rag_inventory_cache.py::refresh_cache` 都把同一个 `rows` 对象同时传给 `write_rewrite_inventory_index(...)` 和 `run_inventory_snapshot_shadow(...)`。
   - 代码可确认：`app/main.py::_refresh_inventory` 也复用同一个 `rows` 触发旧 index 和 Shadow，但此前只把 index path 传给 Shadow。
   - 服务器现场待确认：首轮服务器执行的入口、部署代码版本、以及当次同步脚本日志。

2. 旧 rewrite index 是否为本轮同步刚生成，而非历史缓存

   - 代码可确认：两个定时/同步脚本先生成旧 rewrite index，再触发 Shadow；脚本入口还会把本轮生成的 in-memory index 传入 Shadow。
   - 代码可确认：修复前 `load_legacy_rewrite_index(path, fallback)` 在同时收到 path 和 fallback 时优先读 path，存在 path 指向历史/未及时覆盖文件时混用历史 index 的风险。
   - 修复后：只要调用方传入本轮 in-memory index，就优先使用该 index；不会再用 path 覆盖当前批次输入。
   - 服务器现场待确认：首轮服务器上的旧 index 文件 mtime、签名、以及是否与当次 sync_run_id 对应。

3. legacy communities 的提取路径

   - 代码可确认：`write_rewrite_inventory_index(...)` -> `build_rewrite_inventory_index(...)` -> `canonicalize_row(...)` -> `_community_stats(...)`。
   - `name` 来自 `canonical_community_display(community)`。
   - `normalized` 来自 `normalize_search_text(community)`。

4. Snapshot communities 的提取路径

   - 代码可确认：`InventorySnapshotShadowCoordinator` -> `LegacyInventoryToSnapshotAdapter.adapt_many(...)` -> `SnapshotBuilder.build(...)` -> `build_safe_rewrite_inventory_index(...)` -> `_community_index(...)`。
   - `name` 来自 `InventoryListing.community`。
   - `normalized` 来自 `InventoryListing.normalized_community`，底层为 `normalize_listing_identity(...)`。

5. 两边标准化规则是否相同

   - 代码可确认：修复前不相同。legacy index 有 display alias 和 search normalize；Snapshot safe index 使用 listing identity normalize；reconciliation 却直接用 `name.strip()` 做 communities 集合 key。
   - 影响：Unicode 空格、全角/半角、display alias、排序差异都可能被误判为 community 集合不一致。
   - 修复后：communities 对比统一使用 `normalize_listing_identity(...)` 生成比较 key；排序差异不影响结果，标准化后相同不算 mismatch。

6. 宣传行、区域标题行、空白行是否一致过滤

   - 代码可确认：Snapshot Builder 会过滤空白行、表头行、区域标题行、宣传行和已关闭房源。
   - 代码可确认：旧 rewrite index 只保留存在小区或房号的 canonical row，实际一致性依赖上游 parser/同步 rows 已经把区域标题和合并单元格处理为结构化房源行。
   - 本轮新增回归：区域标题不会成为 Snapshot community；合并单元格续行不会丢失 community。
   - 服务器现场待确认：当次飞书导出 rows 是否已经完成合并单元格继承。

7. 合并单元格向下继承是否一致

   - 代码可确认：Snapshot Builder 在 `current_community` 上支持向下继承。
   - 代码可确认：旧 rewrite index 自身不做合并单元格继承，必须依赖输入 rows 已经带有继承后的小区值。
   - 修复边界：本轮不改变旧生产 parser，只在 reconciliation 侧保证标准化后的 community 集合比较正确，并新增测试覆盖 Shadow Builder 不丢续行 community。

8. area_aliases 是否错误参与 communities 对比

   - 代码可确认：没有。`area_aliases` 通过 `_alias_pairs(...)` 单独比较，severity 为 warning；communities 只读取 `index["communities"]`。
   - 服务器首轮的 `rewrite_index_mismatch.area_aliases` 与 `rewrite_index_mismatch.communities` 是两个独立结果。

9. 是否存在两个独立的小区 normalize 实现

   - 代码可确认：存在多个领域不同的 normalize 函数：
     - `normalize_search_text(...)` 服务旧 RAG/fuzzy search。
     - `canonical_community_display(...)` 服务旧 rewrite index 展示别名。
     - `normalize_listing_identity(...)` 服务 Snapshot listing_id 和稳定身份。
   - 根因不是单纯“函数数量多”，而是 reconciliation 没有选择唯一的比较身份函数，直接比较 display name。
   - 修复后：rewrite index reconciliation 的 community 身份比较唯一归属 `normalize_listing_identity(...)`；旧 RAG/fuzzy search normalize 不在本轮删除，因为仍属生产旧读取路径。

10. 对比是否只比较数量，而没有比较规范化集合

   - 代码可确认：修复前不是只比较数量，但比较的是 raw display name 集合，不是标准化集合。
   - 修复后：先建立标准化 community bucket，再比较规范化 key 集合；重复项单独 warning，不伪装成缺失/新增。

## 根因

首轮 blocking 的直接根因是 reconciliation 使用 `communities[].name.strip()` 作为集合 key，而 legacy rewrite index 与 Snapshot safe rewrite index 的 `name` 来源不同：

- legacy `name` 是旧 rewrite index 的展示名，可能经过 `canonical_community_display(...)`。
- Snapshot `name` 是快照房源里的原始小区名。
- 两边 `normalized` 字段也分别来自旧 search normalize 和 Snapshot identity normalize。

因此同一批数据下，实际标准小区集合相同，但 display name 或空格/全半角/别名差异会被误判为 `rewrite_index_mismatch.communities` blocking。

次要风险是 current index 选择顺序：修复前同时传入 path 和 in-memory current index 时优先读 path，理论上可能把历史 path 文件用于当前批次 Shadow 对比。本轮改为优先使用 in-memory current index。

## 修复语义

- communities 比较使用同一批输入下的标准小区集合。
- 排序差异不算 mismatch。
- 标准化后相同不算 mismatch。
- 重复 community entry 单独输出 `rewrite_index_duplicate_community` warning。
- 实际缺失或新增 community 仍保持 blocking。
- area alias 不进入 community 集合。
- 旧 rewrite index 中的 viewing 字段继续只产生 warning，不输出原值。
- Snapshot safe rewrite index 不重新加入 viewing 原文。
- 当前批次 in-memory legacy index 优先于 path，避免历史文件混入。

## Legacy Removal Report

本轮删除/替代的新代码内部重复点：

- 替换原先 raw display-name `_community_map(...)`，改为标准化 community bucket 与重复项报告。
- 替换 `_layouts_by_community(...)` 的 raw community key，改为标准化 key，避免 fallback layouts 因显示名差异失配。
- 修改 `load_legacy_rewrite_index(...)` 的 path-first 行为，当前批次 in-memory index 不再被 path 覆盖。

本轮未删除的旧生产路径：

- 旧 CSV 读取仍保留。
- 旧 rewrite index 生产读取仍保留。
- 旧 PNG 生成/发送仍保留。
- 旧 `InventoryService` 仍保留。
- 旧 RAG/fuzzy search normalize 仍保留，因为客户查询路径尚未切换到 Snapshot Reader。

## 验证

新增专项测试覆盖：

- community 集合相同但顺序不同。
- 标准化后重复 community 只 warning，不误报缺失。
- 全角空格和普通空格规范化后通过。
- area alias 不进入 community 集合。
- 真缺 community 仍 blocking。
- 真多 community 仍 blocking。
- 当前批次 index 不被历史 path 覆盖。
- 合并单元格续行不丢 community。
- 区域标题不成为 community。
- sensitive viewing warning 不泄露值。
- area_aliases 差异保持 warning。
- public artifact scan 继续通过。
- `.local/m1c3-diagnostics/` 不被 Git 跟踪。

