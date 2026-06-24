# InventorySnapshot 风险登记

本文是 M1A 风险清单，不包含生产实现。

| ID | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | 当前 worktree HEAD 落后批准基线 | 设计审计可能缺少基线后两次文档/规则提交 | 最终报告明确 HEAD 关系；M1B 开始前切到批准基线 worktree 或确认继续 |
| R2 | rewrite index 当前保存 `viewing` 原文 | 密码可能进入 artifact 和 Prompt | M1B 移除 `room_index[].viewing`，增加无密码扫描测试 |
| R3 | structured memory 当前摘要保存 `viewing` | 历史上下文可能持久化密码 | 新 normalize 仅读状态摘要；历史文件读时清洗，不批量改写 |
| R4 | 活动 CSV、rewrite index、PNG 分别生成 | 同一轮 RAG 可能读到不一致版本 | Snapshot 指针绑定全套产物，同轮锁定 snapshot_id |
| R5 | PNG 替换先删旧图再 replace | 渲染失败或崩溃可能短暂无图 | PNG 写入快照目录，指针切换前完成存在性校验 |
| R6 | 直接写 JSON 非原子 | 进程中断可能留下半文件 | 所有 pointer/report/index 使用 tmp + replace |
| R7 | 重复房源当前 dedupe 最新覆盖 | 冲突房源可能静默覆盖 | duplicate conflict 阻断快照并写 report |
| R8 | 房号/密码被 pandas 自动转类型 | 前导零、`#` 或日期样式丢失 | 所有源值转字符串；CSV 全字段按字符串写 |
| R9 | 飞书合并单元格读取只靠空白填充 | 小区/区域错绑会污染搜索 | 显式 downfill 规则和测试，只填结构字段 |
| R10 | 同步失败 fallback 到旧 cache | 旧数据可能被当作新数据 | health 暴露 snapshot age/status；失败不切换 pointer |
| R11 | Windows/Linux 路径差异 | 本地测试通过但服务器路径失效 | artifact 保存相对 POSIX 路径，运行时用 pathlib 解析 |
| R12 | 磁盘不足 | tmp 或 PNG 写到一半 | 写前空间检查，失败保留上一快照 |
| R13 | 并发定时器重叠 | 两个进程竞争切 pointer | 跨进程锁 + stale 接管 + 指针重读校验 |
| R14 | 旧 admin 接口绕过 Snapshot Reader | 人工触发导致活动文件和 snapshot 不一致 | admin refresh 统一走 Snapshot Builder |
| R15 | 客户可见回复行为被误改 | 影响客服稳定性 | M1B 只替换数据读取层，客户话术回归全量测试 |

## 未决策项

- Snapshot root 是否沿用 `data/inventory_snapshots`，还是放在 `room_database/inventory_snapshots` 以便素材一起打包。
- `private/viewing_secrets.json` 是否单独 chmod 或依赖项目目录权限。M1B-GATE 当前实现：POSIX 尽量设置 private 目录 `0700`、文件 `0600`；Windows 明确依赖 NTFS ACL，不声称 chmod 等价或加密。
- 旧 `local_image` OCR 是否在 M1D 删除，还是保留为人工灾备工具。
- listing_id alias 迁移是否需要人工维护文件，或由飞书 record_id 辅助迁移。
- health 对 stale 的默认阈值应与当前 300 秒 cache max age 一致，还是按定时器 3 次/日放宽。

## M1B-GATE 已缓解风险

- R2：M1B Snapshot rewrite index 不保存 `viewing` 原文，并有 canary 扫描测试；旧生产 index 的替换仍在 M1C。
- R6：Snapshot JSON、CSV、private manifest 和 pointer 使用同目录临时文件 + replace；旧生产活动文件仍待 M1C/M1D 迁移。
- R7：M1B Builder/Validator 对 duplicate conflict 阻断，listing_id collision 有专项测试。
- R9：合并字段不跨空白行或区域标题继承。
- R13：M1B Store 使用 `locks/sync.lock` 独占创建做发布冲突检测；stale lock 接管策略留待生产同步接入时细化。
