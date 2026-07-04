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
