# RAG V2 快速测试门禁

本文档定义 RAG V2 本地 QA Fast Gates。目标是提高开发反馈速度，但不降低房源、候选、素材、密码和敏感信息准确性。

适用脚本：

```powershell
.\scripts\rag-v2-test-gates.ps1 -Level L1
.\scripts\rag-v2-test-gates.ps1 -Level L2
.\scripts\rag-v2-test-gates.ps1 -Level L3
.\scripts\rag-v2-test-gates.ps1 -Level L4
```

脚本默认设置 `RUN_ONLINE_QA=0`，只运行本地离线检查；不得用于 SSH、部署、企业微信真实发送、飞书线上读写或任何外部服务连接。

## 分层原则

可弱化的是重复次数和发布前置范围，不是准确性要求本身。快速门禁可以减少“每次开发都跑三遍全量”的成本，但不能移除以下安全边界：

- 房源事实只能来自最新房源表或工具证据。
- 候选序号、上下文候选集和素材目标不能错绑。
- 密码只能在明确看房/密码意图且目标房源已绑定时进入回复。
- 公共 artifact、日志、报告和安全输出不得泄露 token、App Secret、真实密码或运行凭证。

QA replay artifact 的业务审计必须保留这些高风险判定：

- 没有当前候选集或上一轮待发素材上下文时，`第一套`、`前两套`、`1和3` 等序号请求不能绑定到同轮任意搜索结果。
- `不是只问石桥铭苑`、`不是杨家府` 等否定约束不能被重写成正向精确小区。
- `杨家府`、`杨乐府`、`杨家新雅苑`、`棠润府`、`荣润府` 等相似小区不能互相污染；确认式追问可以是 `info`，不能误标成业务失败。
- 视频、图片等房源素材请求必须带稳定 `listing_id` 证据；只有房源 label 不足以证明可以发送。
- 用户没有问密码时，客户可见回复不能出现密码字样或密码格式。
- `准备发送`、`已发送`、`发送失败` 必须和发送阶段动作一致，不能互相混用。

## L1: 最快结构门禁

面向小改动和提交前快速自检。

包含：

- `tests/test_kf_contracts.py`
- `tests/test_media_manifest.py`
- `python -m compileall app`
- `git diff --check`

覆盖重点：

- RAG contract schema、候选编号、证据链、发送 action 和敏感字段脱敏。
- 素材 manifest 的明确绑定、缺失报告、孤儿素材和模糊候选不自动绑定。
- Python 语法可编译。
- diff 中没有尾随空格或冲突标记。

## L2: 核心 RAG 快速回归

面向改动涉及回复链路、候选上下文、素材目标或 LLM prompt 周边时。

包含 L1，并追加：

- `tests/test_wecom_kf.py`
- `tests/test_kf_agentic_rag.py`
- `tests/test_llm.py`
- `tests/test_media_store.py`
- `tests/test_media_manifest.py`
- `tests/test_inventory_query.py`
- `tests/test_inventory_read_router.py`
- `tests/test_inventory_sensitive_access.py`

覆盖重点：

- 客服回调和 Agentic RAG 主链路。
- 问题重写/意图分析结果与工具目标一致。
- 最近候选集、序号选择、上下文继承和新锚点识别。
- 图片、视频、原视频链接与目标房源绑定。
- 看房密码和敏感访问边界。
- QA replay 审计函数的 UTF-8 输入、候选绑定、否定约束、相似小区污染、素材 listing_id、发送时态和 high/medium 分级。

## L3: 单次全量门禁

面向准备合并、跨模块改动或任何不确定影响面的改动。

包含：

- `python -m pytest -q`
- `python -m compileall app`
- `git diff --check`

L3 只把全量 pytest 跑一次，用于开发效率；它不替代发布前的连续稳定性验证。

## L4: Release 门禁

只面向 release/cutover 前评估。L4 不部署、不 SSH、不连接外部服务；生产发布仍必须走 `APPROVE_DEPLOY` 和上线 runbook。

包含：

- 连续 3 次全量 `pytest -q`。
- 20+ parity QA：默认运行两个 10-window UTF-8 RAG parity runner。
- rollback/cutover safety：`tests/test_inventory_snapshot.py` 与 `tests/test_inventory_snapshot_m1d2b2.py`。
- secret scan：扫描已跟踪文件中的私钥、OpenAI/GitHub/AWS 样式密钥和运行时凭证赋值；仅允许 `.env.example` 或测试 fixture 中显然是 `your_`、`missing_`、`dummy_`、`fake_`、`test_`、`example_`、`placeholder_` 这类占位值。
- `python -m compileall app`。
- `git diff --check`。

L4 的重点不是“多跑几遍显得放心”，而是验证全量结果稳定、parity 覆盖足够、回滚演练存在、敏感信息扫描没有退化。

## 何时选择

- 文档、测试脚本、类型 contract 小改动：先跑 L1。
- Planner 周边、Prompt、回复清洗、候选/素材/密码相关测试变更：至少跑 L2。
- 准备交付或改动跨多个模块：跑 L3。
- release/cutover 前：跑 L4，并保留输出日志作为发布证据。

## 失败处理

任何层级失败都视为门禁失败。不要通过减少准确性断言、降低敏感信息检测、跳过候选绑定测试来换取绿色结果。

允许的优化方向：

- 缩短非发布阶段的重复次数。
- 把耗时 parity 留到 L4。
- 拆分更细的本地离线测试集合。

不允许的优化方向：

- 删除候选/素材绑定断言。
- 放宽密码泄露或敏感字段脱敏断言。
- 把真实外部服务调用混入本地门禁。
- 用旧固定规则代替 RAG 链路修复。
