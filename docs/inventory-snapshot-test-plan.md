# InventorySnapshot 测试计划

本文是 M1A 测试设计，不包含生产实现。

## 已运行基线

最小基线：

```powershell
$env:PYTHONPATH="$env:TEMP\m1a-pytest-deps-36fd"
python -m pytest -q -p no:cacheprovider tests/test_inventory.py tests/test_inventory_query.py tests/test_region_inventory_sync.py tests/test_feishu.py
```

结果：101 passed。

## M1B 单元测试

新增 `tests/test_inventory_snapshot.py`：

- `test_snapshot_id_uses_utc_timestamp_and_source_hash`
- `test_source_hash_changes_when_values_or_schema_change`
- `test_listing_id_stable_for_same_community_room`
- `test_listing_id_map_preserves_id_after_alias_migration`
- `test_downfills_area_and_community_only`
- `test_does_not_downfill_password_price_or_room_no`
- `test_filters_promotional_area_title_and_invalid_rows`
- `test_preserves_room_no_and_viewing_text_as_strings`
- `test_duplicate_identical_rows_are_deduped`
- `test_duplicate_conflicting_rows_block_snapshot`
- `test_inventory_json_and_csv_have_same_listing_ids`
- `test_rewrite_index_does_not_contain_password_text`
- `test_private_viewing_secrets_contains_sensitive_viewing_text`
- `test_current_pointer_switches_only_after_full_validation`
- `test_failed_snapshot_keeps_previous_pointer`
- `test_snapshot_reader_rejects_corrupt_manifest`
- `test_windows_and_posix_relative_paths_round_trip`

## M1B-GATE 加固覆盖

M1B-GATE 后，Snapshot 专项测试扩展到 49 个通过、1 个 Windows 权限语义跳过。新增覆盖不是按数量凑数，而是对应以下行为契约：

- `source_hash` 是确定性内容身份，`snapshot_id` 是构建/发布身份；同内容不同构建时间可有不同 `snapshot_id`。
- 普通字段、密码、行顺序、generator version 变化会改变 `source_hash`；BOM/EOL 不改变 hash。
- `snapshot_id`、pointer path、manifest artifact path 拒绝路径穿越。
- `listing_id` collision 和重复 key 被 Validator 阻断。
- 公共 artifact 递归扫描不含 `TEST_SECRET_001234#`、真实手机号或 viewing 原文；`inventory.csv` 单独覆盖。
- 默认 `repr`、异常、validation report 不含 secret canary。
- `private/viewing_secrets.json` 不进入公共 manifest；private 目录使用 `private/manifest.json` 做完整性校验。
- POSIX private 目录/文件权限检查为 `0700/0600`；Windows 明确跳过并说明 ACL 语义。
- staging 写入失败、manifest 写入失败、private secret 写入失败、pointer replace 失败均保持旧 pointer。
- 已存在 snapshot 不覆盖；发布锁冲突返回明确错误。
- 缺失 manifest 文件、artifact hash mismatch、private hash mismatch 均导致 Reader 拒绝读取或 health corrupt。
- UTF-8 中文和字母房号 roundtrip；公共/私有 artifact 不写入硬编码绝对开发机路径。
- 空价格、待定价格、`3900.0` 和负价格分别按设计处理。
- 合并 community 不跨空白行或区域标题继承；宣传文本带房号时不被误过滤。
- rewrite index 不依赖 LLM，也不含完整 viewing 原文。

Skipped test 审计：当前唯一 skip 为 `tests/test_inventory_snapshot.py::test_private_file_permissions_posix_or_explicit_windows_skip`，只在 Windows 下跳过 POSIX `chmod 0700/0600` 断言，原因是 Windows 使用 ACL，M1B 不声称 private 明文文件已被 chmod 加密。该 skip 不覆盖业务逻辑、不覆盖失败注入、不覆盖 secret 扫描；公共 artifact secret 扫描、private manifest 完整性、异常/repr 脱敏仍在 Windows 上执行。

## M1C2 离线 Shadow 全链路覆盖

新增 `tests/fixtures/inventory_snapshot/offline_shadow_fixture.json`，使用虚构板块、小区、房号和测试 canary，覆盖：

- 表头前宣传行、区域标题行、合并单元格导致的空区域/空小区延续。
- 正常房源、字母房号、前导 0 密码、空出/提前联系/还没空出的看房信息。
- 水电备注、空价格、非法价格、重复房源、缺房号、跨区域后缺小区。
- 图片/视频状态、中文全角标点、全角空格、测试手机号 canary、测试 secret canary。

新增 `tests/test_inventory_snapshot_m1c2.py`：

- OfflineComparisonRunner 以脱敏 fixture 完整跑 legacy parser、旧 rewrite index、Snapshot Shadow、reconciliation、health 和 artifact 安全扫描。
- Shadow 只写 `shadow_current_snapshot.json`，不写生产 `current_snapshot.json`。
- 公开 artifact、安全摘要、health 输出不得包含 canary、手机号或开发机绝对路径。
- parser 保留字母房号、前导 0 密码、图片/视频状态，并阻止小区跨区域继承。
- reconciliation 对价格/房号/密码布尔、utility、media、rewrite index 安全字段做 blocking/warning 区分，报告只含安全 code 和 `source_row_ref`。
- 重复 `sync_run_id` 返回 `duplicate_skipped`，不生成第二份 report。
- 同一 `source_hash` 使用不同 `sync_run_id` 重跑不会增加 `consecutive_passes`。
- 不同 `source_hash` 连续通过才满足门禁；blocking 结果会重置 passes，后续成功可重新计数。
- health 覆盖 disabled、never_run、stale、corrupt status 和安全序列化。
- AST 测试确认 `app/main.py` 中 Shadow 调用仅存在于 `_refresh_inventory` admin helper，不在客服消息/RAG 链路。
- Region 同步脚本测试确认旧流程只 refresh 一次，并传入唯一 `sync_run_id` 给 Shadow。

## M1C3 发布预检覆盖

新增 `tests/test_inventory_snapshot_m1c3.py`：

- Shadow 观察 CLI 覆盖 healthy、never_run、disabled、stale、blocking、corrupt status。
- `--json` 输出不含 secret canary、手机号、token 或开发机绝对路径。
- CLI 只读：读取前后 Shadow 状态、pointer、report 文件字节不变。
- UTF-8 中文输出可正常打印。
- Preflight 覆盖：不访问网络、不写 production pointer、非法 mode、Shadow/production path 重叠。
- Preflight 覆盖旧生产文件存在、旧生产读取路径未切 Snapshot Reader。
- 路径输出使用安全 label，避免公开绝对敏感路径；覆盖 Windows 绝对路径和相对部署路径。
- 客服消息路径仍无 Snapshot Reader 调用。

## 现有测试扩展

`tests/test_inventory.py`：

- Snapshot Reader 下的 `search/all_rows/snapshot` 行为保持当前搜索语义。
- 活动 CSV 文件变化不再影响已锁定 snapshot。
- cache meta 改为 snapshot meta 后仍能进入工具证据。

`tests/test_feishu.py`：

- `InventoryImageSyncer` 支持渲染到指定 snapshot PNG 目录。
- PNG 渲染失败不更新 `current_snapshot.json`。
- 多页 PNG 与 manifest file_hash 对齐。

`tests/test_region_inventory_sync.py`：

- Region 同步成功后触发 snapshot 构建的 report 结构。
- 源区域失败时只报告失败，不切换快照。
- duplicate conflict 阻断后不写目标 pointer。

`tests/test_wecom_kf.py`：

- `_process_text_turn` 在 rewrite 阶段锁定 `inventory_snapshot_id`。
- 同一轮 search、all_rows、rewrite index、房源表 PNG 均使用同一 snapshot。
- 客户未问密码时，Prompt、tool_evidence_summary、structured memory 不含真实密码。
- 客户明确问密码且房源已绑定时，viewing tool 可以读取同快照 private 数据。
- 房源表请求发送 snapshot 下 PNG，不读活动 `room_database/inventory_*.png`。

`tests/test_kf_context_memory.py`：

- `summarize_row` 不再持久化 `viewing` 原文。
- 历史上下文含旧 `viewing` 字段时，normalize 后只保留状态摘要。

## 集成测试

- 构造两份快照 A/B，模拟 turn 中途 pointer 从 A 切到 B，断言本轮回复仍基于 A。
- 模拟磁盘写满：writer 在写 PNG 或 CSV 时抛错，断言 pointer 仍指向旧快照。
- 模拟崩溃残留 tmp 目录，下一轮同步清理过期 tmp，不影响 current reader。
- 模拟 rewrite index JSON 损坏，Reader 退回上一快照或返回健康错误，不生成客户可见错误。

## 安全测试

- 对 `rewrite_inventory_index.json`、`inventory.json`、`sync_report.json`、日志摘要、structured memory 做正则扫描：不得出现 `\d{3,8}#?` 形态的看房密码原文。
- 允许 `private/viewing_secrets.json` 含敏感字段，但测试中只用假数据。
- `viewing tool` 必须同时满足 `snapshot_id`、`listing_id`、显式 viewing intent。

## QA 回归

固定集：

- 房源表 PNG 请求。
- 区域/预算/户型筛选。
- 单套房源价格、视频、看房密码。
- 多套候选后续“这几套视频/密码”。
- 未空出房源预约看房。
- 错别字小区和相似小区澄清。

随机保底：

- 固定集通过后随机生成不同的 10 个问题。
- 如暴露上下文丢失、候选错绑、错误追问或答非所问，M1B 不算完成。

## 最终命令

本地：

```powershell
$env:PYTHONPATH="$env:TEMP\m1a-pytest-deps-36fd"
python -m pytest -q -p no:cacheprovider
python -m compileall app
```

部署后才允许服务器命令；M1A/M1B 本地设计阶段不执行 SSH、不重启服务。
