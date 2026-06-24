# InventorySnapshot M1B Contract Deviation Report

本报告记录 M1B-GATE 对 Snapshot Models、Builder、Validator、Store、Reader 和测试的契约一致性审计结论。M1B 仍是旁路实现：未接入飞书生产同步，未切换生产读取入口，未修改客户回复。

| 契约点 | 文档要求 | 审计时代码行为 | 是否一致 | 不一致风险 | 处理 | 理由 |
| --- | --- | --- | --- | --- | --- | --- |
| `snapshot_id` 语义 | 设计文档使用 `YYYYMMDDTHHMMSSZ_<source_hash_12>` | 代码按构建时间 + hash 前缀生成；测试名曾暗示同输入同 ID | 部分不一致 | 把构建身份误当内容身份会影响复用、回滚和测试判断 | 修改文档和测试，并保留代码语义 | M1A 目录和指针设计已经依赖按时间排序的构建身份，确定性内容身份由 `source_hash` 承担 |
| `source_hash` 语义 | 基于规范化源 payload 的 SHA-256 | 包含 schema、source metadata、generator version 和 rows；BOM/EOL 归一；行顺序保留 | 一致，已补测试 | 密码、算法或行顺序变化若未入 hash 会导致事实版本错判 | 补测试并写明契约 | 密码变化必须产生新事实版本；电子表格行顺序属于源 payload |
| `listing_id` 稳定性 | 规范小区 + 房号生成，不含价格、手机号、密码 | 生成规则一致；Validator 能发现重复 ID 和 key 冲突 | 一致，已补 collision 测试 | collision 静默覆盖会错绑房源 | 补测试 | M1B 不做别名迁移，只在文档保留 M2 人工迁移策略 |
| 公共 manifest 与 private 边界 | 公共产物不得暴露真实 viewing 文本 | 审计时公共 manifest 声明了 `private/viewing_secrets.json` 并带 hash/size | 不一致 | 低熵密码 hash/大小进入公共 manifest，扩大泄露面 | 修改代码和文档 | 公共 manifest 只声明公共 artifact；private 目录内自带 `private/manifest.json` 做完整性校验 |
| private 文件安全状态 | `private/viewing_secrets.json` 只能由 viewing tool 读取 | 审计时明文写入，无权限文档；无 private manifest | 部分不一致 | 误称加密或缺少校验会掩盖安全状态 | 修改代码、测试和文档 | POSIX 设置目录 0700、文件 0600；Windows 明确为 ACL 场景，不声称加密 |
| Store 原子发布 | 校验通过前不切 pointer；pointer tmp + replace | pointer 原子；snapshot artifact 直接写 staging；无发布锁 | 部分不一致 | 半写、并发发布、路径注入可能污染 current | 修改代码并补故障注入测试 | artifact 写入也改为 tmp + replace；发布使用 `locks/sync.lock` 独占创建 |
| Reader 路径安全 | 不通过 mtime 猜最新；路径不能穿越 | Reader 不猜目录；但 `get_snapshot(snapshot_id)` 直接拼路径 | 部分不一致 | 调用方传入 `../` 可读取 snapshot root 外路径 | 修改代码并补测试 | `snapshot_id`、pointer path、manifest artifact path 均做安全相对路径校验 |
| Builder 合并单元格 | area/community 只能结构化向下填充，不跨区域或空白分段 | 审计时 community 可能跨空白行/区域标题继承 | 不一致 | 房源被错绑到上一区域/小区 | 修改代码并补测试 | 空白行重置 area/community；区域标题重置 community |
| 价格解析 | 价格字段按字符串输入，非法价格不静默转错 | 审计时 `-100` 会被正则提取为 `100` | 不一致 | 负价被当有效低价进入库存 | 修改代码并补测试 | `3900.0` 可归一为 3900，空/待定为 null，负数阻断 |
| 默认 `repr`/异常 | 不得输出真实密码 | dataclass 默认 `repr` 可能包含 `private_viewing_secrets` | 不一致 | 测试失败或日志打印对象时泄露密码 | 修改代码并补测试 | Snapshot、Listing、Report、Issue、ReadResult repr 均走脱敏输出 |
| M1B legacy gate | 不接入生产同步、不修改 `app/main.py` | 代码未接入生产路径 | 一致 | 提前接管会绕过 M1C 验收 | 仅更新文档 | 本轮只加固 M1B 核心实现 |
