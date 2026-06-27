# V1 终态上线审计计划

本文定义 V1 终态合入 `codex/rag-v2-integration` 前的强制审计门槛。所有结论必须来自代码 diff、实际测试输出和本地 artifact；未运行的测试不得写成已通过。

## 0. 硬边界

- 未获得新的部署授权前，不 SSH、不上传、不重启服务、不修改线上数据。
- 不读取、不输出 `.env`、token、App Secret、服务器凭证或真实客户敏感信息。
- `main` 工作树当前脏改不参与本轮 V1 合入评估。
- A-F 任一工作树未通过本审计，不得合入 integration。
- 合入后仍需重新运行 integration 级测试，不能复用单工作树测试结果冒充最终结果。

## 1. P0 阻塞项

出现任一 P0，禁止合入：

- 客户可见回复绕过双 LLM production 或确定性工具 evidence。
- LLM1 生成客户可见回复，或 LLM2 决定房源、密码、素材绑定、候选编号。
- L0/L1/L2 validation 未在发送准备前执行，或失败后仍发送。
- 价格、房态、listing_id、候选编号、素材、密码来自非 evidence 来源。
- MediaManifest production 允许模糊匹配直接发送图片或视频。
- Snapshot primary 同一轮内可漂移到不同 `snapshot_id/source_hash`。
- rewrite index、日志、artifact、receipt、outbox 写入真实密码、token、App Secret、完整凭证或原始签名。
- SendReceipt/Outbox 重启后无法阻断同一幂等键重复发送。
- 真实对话回放缺失却被 release gate 标记为可上线。
- 删除旧规则后缺少 V1 替代入口，导致免押、合同、看房、密码、素材发送等核心场景失效。

## 2. P1 阻塞项

出现 P1 时不得进入 release/cutover，可在同一工作树修复后复审：

- `app/main.py` 合并冲突人工解决后缺少针对冲突区域的测试。
- L3 质量问题被当作 pass，且未触发话术重写或 retry/fallback。
- Outbox JSONL 无锁或锁失败不可见。
- `send_uncertain` 后续仍可能自动重发素材。
- Snapshot readiness、fallback strategy 或 primary env 配置缺少可审计状态。
- QA artifact 缺少 `high_count=0`、`medium_count=0`、`usable_for_release=true` 校验。
- release rehearsal 不验证 rollback、current pointer、health contract 或审批门禁。
- 旧规则删除报告没有记录删除对象、新入口和覆盖测试。

## 3. P2 观察项

P2 不阻塞代码合入，但必须进入上线前检查清单：

- 生产 `data/kf_send_outbox.jsonl` 轮转、备份和权限策略。
- 生产 `media_manifest.json` 完整性和真实文件 hash 覆盖率。
- 生产 `INVENTORY_SNAPSHOT_ROOT`、`INVENTORY_SNAPSHOT_PRIMARY_READINESS_PATH`、`INVENTORY_READ_FALLBACK_STRATEGY` 的 systemd 配置。
- 完整 L4 耗时与 nightly/人工 release 流程分工。

## 4. Worker 审计门槛

### A production orchestrator

- 必查文件：`app/main.py`、`app/services/kf_dual_llm_production.py`、`tests/test_kf_dual_llm_production.py`、`tests/test_wecom_kf.py`。
- 必须证明：LLM2 package 先过 `validate_prepared_outbound_package`；L0-L2 阻断；L3 不降低事实门槛；validation 失败不写成功 package。
- 必跑测试：`tests/test_kf_dual_llm_production.py`、`tests/test_kf_outbound_validation.py`、相关 `tests/test_wecom_kf.py` production 用例。

### B InventorySnapshot primary

- 必查文件：`inventory_read_*`、`inventory_sensitive_access.py`、`inventory_snapshot_store.py`、`inventory_snapshot_validator.py`。
- 必须证明：同轮 snapshot 锁定；current pointer 原子切换；失败不切 pointer；公开 artifact 无敏感；fallback 为整轮策略。
- 必跑测试：`test_inventory_read_router.py`、`test_inventory_read_turn.py`、`test_inventory_sensitive_access.py`、`test_inventory_snapshot.py`。

### C MediaManifest production

- 必查文件：`media_manifest.py`、`media_store.py`、`app/main.py` 素材收集和发送段。
- 必须证明：生产发送只接受 listing_id 精确 evidence；发送前校验路径、listing_id、sha/source_hash；不临时全量同步飞书素材。
- 必跑测试：`test_media_manifest.py`、`test_media_store.py`、素材相关 `test_wecom_kf.py`。

### D Outbox/SendReceipt

- 必查文件：`kf_outbox.py`、`kf_send_receipts.py`、`app/main.py` 发送提交段、`kf_context_memory.py` 相关测试。
- 必须证明：持久化判重、重启后抑制重复、失败可回放、不确定结果阻断自动重发、写入脱敏。
- 必跑测试：`test_kf_send_receipts.py`、`test_kf_context_memory.py`、`test_kf_send_receipt_faults.py`。

### E QA/release gates

- 必查文件：`scripts/rag-v2-test-gates.ps1`、`scripts/rehearse_release_pipeline.py`、release/qa docs。
- 必须证明：真实对话缺失、SkipParity、QA artifact medium/high 非 0、release rehearsal 失败均阻断 release。
- 必跑测试：`test_release_pipeline.py`、`test_qa_utf8_inputs.py`、`test_real_dialogue_fixtures.py`、gate L1。

### F legacy-rule-prune

- 必查文件：`app/main.py`、`app/services/llm.py`、`app/services/kf_agentic_rag.py`、删除报告。
- 必须证明：删掉的旧客户可见直出均有 V1 替代入口；保留项只作为安全阀，不抢 Orchestrator 主判断。
- 必跑测试：`test_rule_knowledge.py`、`test_llm.py`、`test_kf_agentic_rag.py`、相关 `test_wecom_kf.py`。

## 5. 最终 integration 门槛

- 所有 worker diff 经过审计并解决 P0/P1。
- 合入后的 `git diff --check` 通过。
- 合入后的定向测试覆盖 A-F 冲突区域。
- 合入后的全量 pytest 至少一次通过。
- release 前 L4 必须带真实对话 fixture；若使用 `-AllowMissingRealDialogues`，只能作为开发门禁，不能作为上线门禁。
