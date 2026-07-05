# 口径变更清单

日期：2026-07-05

## 生产实证修复批（批14）：出站户型/区域声称与证据矛盾拦截（防御纵深）

背景：与批13 同一生产实证（2026-07-04 23:58）——证据行为"翰皋名府8-1403 / 区域组闸弄口 新塘 元宝塘 东站 / 一室一厅"，LLM2 回复却措辞成"新天地这边有一套5000元以上的两室"。批13 修复了检索侧约束丢失的根因；本批在出站校验补上"回复声称 vs 证据行字段"的事实核验缺口（此前 `kf_outbound_validation.py` 全文无任何户型/区域检查，`_known_constraints` 只防"反问已知条件"，价格没错只是因为金额 token 恰有 compose 层白名单核验）。

| 测试/口径 | 改前 | 改后 | 理由 |
| --- | --- | --- | --- |
| 出站校验 L3 事实核验（新增 `l3.layout_claim_mismatch` / `l3.area_claim_mismatch`） | reply_text 层只有风格/泄漏/时态/反问类正则，无任何"回复内容 vs 证据字段"比对；"两室"对"一室一厅"、"新天地"对东站组直接放行 | 回复文本在肯定推荐句段声明的户型/区域词与证据行 layout/area 字段直接矛盾时，产出 L3 issue → `requires_rewrite` → 既有"一次 l3_rewrite 重试 → 仍失败落受控渲染器"路径 | 防御纵深（批13 修根因，本批堵出站）；选 L3 而非 L0-L2：blocking 分支默认禁用受控渲染器（main.py `allow_controlled_renderer`），误伤时该轮可能无回复；L3 是安全降级 |
| 误伤面口径（三防线） | 不适用 | ① 否定/回声句段整体豁免（暂无/没查到/不是/你要的/按您…），允许"你要的两室5000以内没查到"类合法回声；② 声称只在肯定推荐上下文计入（同句含证据行小区/房号标签，或"有一套/这套/查到"类措辞）；③ 户型收敛到 broad 标签比对（"两室"对"两室一厅"泛称包含放行、"大两房"经"两房"归一、"厅卧一体"等无映射表述不报），区域按共享别名组归一（"皋塘"="东站组"），声称词出现在证据全文（含小区名"新塘雅苑"）即 fail-open | `llm.py` 自检提示词明文允许"回复不逐字复述约束"——只拦"声明了且与证据冲突"，不拦"未声明"；证据行无户型/区域字段时该维度不校验（核验只能以证据为基准） |
| 词表来源约束 | 不适用 | 户型口语映射=`inventory_query.ROOM_TYPE_GROUPS`、区域别名=`region_inventory_constants.active_area_alias_groups`、证据行字段键=`kf_dual_llm_shadow.ROW_ALIASES`，出站校验模块不新增同源规则拷贝；支持面只取"像房源行"的证据（有 listing_id 或行字段键），task_packet 约束（inherited_constraints 里的"两室"）明确不算支持证据 | CLAUDE.md 硬规则5（区域别名单一来源）与结构债"同源规则消重"；task_packet 约束正是本次幻觉的来源，不能自证 |
| 新增回归 11 项（`tests/test_kf_outbound_validation.py`） | 无 | 生产时间线固化（双维矛盾 → REWRITE_REQUIRED 且非 blocking，message 含具体冲突值）；泛称包含放行；口语归一正（大两房→两室一厅 pass）反（两房一厅→一室一厅 flag）两用例；否定+回声豁免；无映射口语不报；未声明不拦；同组区域别名放行；小区名含区域词不误报；证据缺字段跳过；混合句只拦肯定句段 | 结构债"先固化回归用例"纪律；正反用例含口语映射为本批验收硬要求 |

既有语义零变更：`facts_passed` / `send_allowed` / `requires_rewrite` 属性定义未动（只增 issue，L3 语义沿用）；`tests/test_kf_dual_llm_production.py` 等既有断言未改。

验证：全量 pytest 1362 passed（1 skipped 为 Windows ACL 平台既有 skip，3 deselected 为 online 标记既有基线）；离线 gate 310/310 usable_for_release=true、failures=[]（artifact kf_qa_gate_graph_utf8_20260705_014907；校验器仅 production 链路消费，离线 gate 走 shadow 分支，复跑为确认无间接回归）。

遗留（挂后续批次）：compose 层 guard（`kf_llm2_outbound` 接入 guard_reasons，走 failure_package 重试，离 LLM2 最近但需松动 main 验收门口径）；判分层户型一致性（5w50 runner 仿区域一致性判分 + 本地 `_constraint_consistency_selfcheck` 平移 `_layout_field_consistency_failures`）；L1 claim 值核验只读 legacy_unknown_fields 而非一等 field/value 的键位缺口为独立 bug，回归面大，单列评估。

不部署、不 push。

## 生产故障修复批（批15）：媒体清单发布策略（送达门与观察门分离）

背景：wecom-room-robot-feishu-region-sync 自 07-01 19:01 连续失败、media_manifest.json 停更（root cause 与取证见 docs/DECISIONS.md 批15 条目）。核心口径变更：发布阻塞条件从"orphan/fuzzy/missing/ambiguous 全为零"收敛为"下载失败非空或绑定为空"。

| 测试/口径 | 改前 | 改后 | 理由 |
| --- | --- | --- | --- |
| `refresh_media_manifest` 发布门 | 任一 orphan/fuzzy/missing/ambiguous 即整份清单降级进 `_manual_review` 候选区，正式 media_manifest.json 不更新 | 只有 `report.failed` 非空或绑定为空才降级；已绑定素材照常发布，孤儿/模糊/歧义/缺失进持久隔离台账 `_manual_review/media_manifest_quarantine.json` | 素材库天然含历史房源（741/1070 为存量孤儿），孤儿是稳态；生产读取器本就只放行精确绑定条目，全阻塞是重复防线且导致 manifest 永不更新（07-01 起线上实证） |
| 结果契约（state/report） | `ok`=`ready`=完全干净；`status`∈ready/degraded/failed；`blocking_count`=failed+missing+ambiguous+orphan+fuzzy | `ok`=`published`（是否发布）；`ready`=完全干净（纯观察）；`status` 新增 `published_with_quarantine`；`blocking_count`=发布阻塞数（failed 条数+空绑定）；新增 `quarantine_count`/`quarantine_path` | 送达门与观察门分离后 ok 语义跟发布走；graph/systemd 以 ok 判成败，发布成功即绿 |
| `tests/test_inventory_snapshot_m1c2.py::test_region_inventory_degraded_candidate_manifest_keeps_review_files` | 期望素材缺失（missing）→ ok=False、status=degraded、候选区留档 | 改名 `test_region_inventory_missing_kind_publishes_and_quarantines_missing`：缺失不阻塞，已绑定视频照常发布（ok=True、status=published_with_quarantine、blocking_count=0、quarantine_count=1），缺失明细进隔离台账 | 单行缺素材不应扣住其余 34 行的已绑定素材；缺失仍可见（台账+report.missing_sample） |
| 隔离项落盘方式（`FeishuDriveMediaManifestAdapter._isolate_item`） | 每个孤儿/歧义文件下载到 staging 临时目录 `_manual_review/<bucket>/`（随 TemporaryDirectory 销毁，零留痕），`isolated_items[].target_path` 指向已销毁路径 | 不下载，只记 `{source_path, bucket, reason}`；持久明细由同步脚本写 `media_manifest_quarantine.json`；`test_ambiguous_directory_is_isolated_and_not_bound`/`test_orphan_media_with_fuzzy_candidate_only_enters_manual_report` 的 target_path 文件断言改为台账记录+零下载断言 | 旧行为纯耗带宽（每轮约 70% 下载量为孤儿）且无审计价值；台账才是失败可见 |
| 云盘"房源素材"包装层 | 包装层进入 source_path/镜像路径（07-01 服务器长出 video/房源素材/ 双层树的成因） | 视为透明层（清单 walk 与旧版 `_sync_folder` 镜像一致），叠加 source_path 去重（`duplicate_source_path` 进 skipped）；新增正反用例锁定 | 共享常量 MEDIA_WRAPPER_FOLDER_NAMES 单一来源；云盘整体包一层/迁移半途双层并存均不再破坏绑定与镜像 |
| Settings 未知 env 键（批15附） | pydantic-settings 默认 extra="forbid"：.env 先于代码新增键 → 所有进程 extra_forbidden 崩溃（07-04 19:00 实证） | extra="ignore"：未知键忽略，配置先行不再致命；新增 tests/test_config_env_tolerance.py 锁定 | 配置与部署顺序解耦；typo 兜底=字段默认值+部署后 UnattendedCheck |

既有语义零变更：绑定判定（显式 lst_id/精确标签/模糊只出候选不绑定）、生产读取器 send_ready 条件、候选区降级路径（真降级时仍写候选清单+候选文件）、发布原子性与失败保旧清单行为均未动。

验证：全量 pytest + 离线 gate 结果见本文件后续更新与 DECISIONS.md 批15 条目。

不部署、不 push（部署与线上验证等用户 APPROVE_DEPLOY）。

### 批15热修（同日）：result["ready"] 键语义修正

首次部署后线上实证（07-05 19:11 timer 轮）：manifest 发布成功（published_with_quarantine、blocking=0）但 graph 仍 blocked——`inventory_sync_graph._failures_for_stage` 的通用阶段判定为 `ok is False or ready is False or errors`，**ready 键被 graph 当作"阶段是否放行"消费**，不能挂"完全干净"观察语义。修正：顶层 `ready`=published（跟发布走），完全干净观察指标改用新键 `clean`（=report.ready，报告内层 ready 语义不变）。暴露的测试缺口一并补上：新增 `test_sync_script_graph_passes_when_manifest_published_with_quarantine`（graph 端到端固化"发布成功+存在隔离项 → graph 必须 passed"）。此前本批 graph 测试只 stub 了 refresh_media_manifest 旧契约，未覆盖新契约端到端。
