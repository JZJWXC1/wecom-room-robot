# CLAUDE.md — 项目交接与工作规范（Claude Code 接手开发）

> Claude Code 每次会话自动读取本文件。本文件是「接手开发」的最小完整上下文；
> 详细规则以根目录 `AGENTS.md`（含《协作规范（追加段）》）为准，两者冲突时以 AGENTS.md 为准。

## 一、项目是什么

企业微信「微信客服」房产租赁机器人（面向合作中介）：LangGraph 五阶段 Agentic RAG
（问题重写 → Planner/LLM1 → 工具 → LLM2 出站 → 自检/L3 校验），双模型走 DashScope
（qwen-flash planner + qwen-plus reply）。房源数据来自飞书多维表格（INVENTORY_SOURCE=feishu_bitable，
定时同步），素材已同步至服务器本地 `room_database/`。生产部署在
`/opt/wecom-room-robot`（systemd，release 目录 + current symlink 切换）。

## 二、必须遵守的硬规则（摘要，全文见 AGENTS.md）

1. **中文交流与注释**；emoji/"嘻嘻"仅限对话回复，禁止进入代码、注释、commit message、
   测试、QA 报告与一切客户可见话术。
2. **变更纪律**：按 owner 分批 commit（每批附测试，message 注明 RAG 阶段归属）；
   连续工作超 2 小时或跨 3 个模块未提交即违规。禁止在 detached HEAD 上长期工作。
3. **部署边界**：没有用户明确的 `APPROVE_DEPLOY`，禁止 SSH/上传/重启/部署。
4. **修复质量门**：话术问题加禁词只算止血，必须同步推进源头收口（结构化 evidence、
   受控模板）；记忆类问题禁止用"给 clear 加例外"当最终修复，同类第二次复发必须开结构债工单。
5. **证据链**：修复证据必须指到 文件+函数+测试名；QA 每轮落盘候选明细
   （区域组/小区/房号/两档价/来源阶段）与 shadow 出站副作用计数；区域别名只准消费
   `app/services/region_inventory_constants.py` 共享定义。
6. **检索回归一律用精确集合断言**（期望候选集 == 实际候选集），不用"不含 X"式弱断言。
7. **验收**：机器判分 + 人工扫描 + 独立第二裁判三方全绿；最终验收轮用全新剧本、
   关闭失败即停、全量收集违规。改测试口径必须登记
   `docs/release/acceptance-criteria-change-log-*.md` 台账。
8. **敏感红线**：门锁/看房密码等敏感字段不得进入 LLM 可见散文、日志摘要与 QA artifact；
   客户可见文本只能来自 LLM2/受控模板层。

## 三、当前状态（2026-07-04 交接时点）

- 分支：`fix/langgraph-hardening-20260703`，本地 HEAD `eba34f02`，工作树代码区干净
  （QA 交付物有意未跟踪，供第二裁判取证）。
- 服务器 current release：`releases/20260703-langgraph-allowed-rooms-012230`，
  代码对齐 commit `7d4b8893`；**本地领先的若干 commit（2fe18e85 起）尚未部署**。
- 最近一轮本地最终验收：机器 gate 300/300 通过（`qa_artifacts/kf_qa_gate_graph_utf8_20260703_225825_*.json`），
  shadow 副作用 = 0；第二裁判判定**条件性通过**，两项未决见下。
- 前任开发者：GPT-5.5（CODEX）。交接后单写者变更为 Claude Code；
  **动手改代码前先确认 CODEX 已停止写入**（单写者原则）。
- 建议裁判换位：GPT-5.5 转任独立第二裁判（保留跨模型互审，这套机制双向抓出过真 bug）。

## 四、接手后的任务队列（按序执行）

### P0-1 fixture 单一事实源整改（第二裁判实锤问题）
现状：`tests/fixtures/qa/test_inventory_cache.csv` 是 2026-06-27 手工提交的 14 行合成数据，
与真实房源表价格冲突（例：星桥锦绣嘉苑20-1606A fixture=押一1800/押二1600，
真实表=押一1900/押二1800），且含真实表不存在的条目（"东新园8-1201"、"杨家牌楼 文教"组）。
要求：
1. QA fixture 改为**从真实房源表快照生成**（脚本化，带版本号与生成时间戳）；
2. 验收摘要必须声明数据出处（fixture 版本 / 服务器缓存时间）；
3. 验收剧本补**皋塘（闸弄口/新塘/元宝塘/东站）组窗口**——当前 506 条候选明细覆盖
   四个区域组，唯独缺这组，而它是 W04T2 区域漂移回归的招牌场景；
4. 迁移期间旧 fixture 相关断言按台账流程更新口径。

### P0-2 服务器真实对话验收（需用户 APPROVE_DEPLOY）
部署本地最新 HEAD → 服务器全量 pytest → 重启健康检查 → 真实对话验收轮
（全新剧本、no-fail-fast、全量收集违规，产出含候选明细/shadow 计数的 md+json）→
记录交用户送独立第二裁判。三方全绿才算收官。

### 结构债（前任登记的到期日，接手即继承）
- 2026-07-05：记忆生命周期单 owner 重构（历史上同类清空 bug 已复发 4 次，
  全部固化为回归用例后再动刀）；
- 2026-07-06：main/resolver 双份同源规则消重；内外语言隔离收口
  （关闭条件：连续两组 50 轮零新增禁词）；
- 2026-07-06：孤儿工作包采纳评审（main 脏改动隔离包，2026-07-04 取证，patch 见
  `judge/patches/`，台账见 `docs/audit/orphan-changes-disposition-20260704.md`；
  评审方式=按功能逐项与 fix 现实现对比，有价值项以「重新实现+测试」方式吸收，
  **严禁直接 apply**——孤儿版未经评审且基线漂移 2842 行；优先淘金 QA runner
  新 gate：重复外发动作检测、免押费率梯度断言，此两项 fix 现有判分体系没有）；
- 2026-07-07：运行时工件迁出 release 目录 + 服务器独有路径清单
  （详见 `runtime-artifact-consumer-audit-20260703.md`，cutover graph 文件迁移前禁止删除）;
- 2026-07-08：main.py 按 owner 拆分（当前约 1.1 万行，历史多起"改一处漏一处"源于此）。

## 五、常用入口

- 本地全量：`python -m pytest -q`（当前基线 1265 passed）
- QA gate：`qa_artifacts/run_kf_qa_gate_graph_utf8.py`（需把 repo 根加入 PYTHONPATH）
- 台账：`docs/release/acceptance-criteria-change-log-20260703.md`
- 工件审计：`runtime-artifact-consumer-audit-20260703.md`
- 第二裁判交接物约定：验收产物打包含 manifest.json（SHA256/HEAD/fixture 版本）
