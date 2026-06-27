# V1 合入顺序和冲突处理

本文给出 A-F 工作树进入 `codex/rag-v2-integration` 的建议顺序。G 审计或主审计未完成前，不得合入 integration。

## 1. 推荐顺序

1. B `codex/v1-inventory-primary`
   - 先稳定事实源和 snapshot 边界。
   - 合入后跑 inventory 相关测试。

2. C `codex/v1-media-send-production`
   - 让素材发送绑定 listing_id、media evidence 和 hash。
   - 合入后重点跑素材发送与 `app/main.py` 相关测试。

3. D `codex/v1-outbox-receipts`
   - 在素材发送路径稳定后接入持久化发送事务。
   - 合入时人工审查 C/D 在 `_send_images_with_receipts`、`_send_videos_with_receipts`、`_send_final_actions` 的冲突。

4. A `codex/v1-production-orchestrator`
   - 在事实源、素材和发送事务稳定后，把 LLM2 package validation 作为生产主门禁。
   - 合入时人工审查 A/C/D 在 `app/main.py` 的 production 分支、outbound package 和 suppress actions 的交叉。

5. F `codex/v1-legacy-rule-prune`
   - 最后删除旧规则，避免先删后无替代入口。
   - F 必须 rebase 到 A-D 后重新跑删除相关测试。

6. E `codex/v1-qa-release-gates`
   - 最后把最终形态纳入 release gate。
   - 合入后跑 gate L1、release rehearsal 和 QA artifact 相关测试。

## 2. 重点冲突文件

- `app/main.py`
  - C 修改素材 evidence 收集和发送前校验。
  - D 修改发送事务和 receipt/outbox 接线。
  - A 修改 LLM2 production package validation。
  - F 删除旧直出规则和旧 fallback。

- `docs/rag-v2-architecture.md`
  - D 更新 SendReceipt/Outbox。
  - F 更新旧 production LLM/legacy 描述。

- `tests/test_wecom_kf.py`
  - A、C、F 都可能修改。
  - 合并后必须跑相关筛选用例，再跑全量。

## 3. 冲突处理原则

- 事实、素材、发送动作以确定性工具和 evidence 为准，LLM 输出不得覆盖。
- 如果 A 与 C/D 冲突，保留 C/D 的发送前素材/Outbox 安全检查，再让 A 的 validation 包住最终 package。
- 如果 F 删除的旧函数仍被 A-D 调用，优先修改调用方走 V1 新入口，不恢复旧客户可见直出。
- 如果测试冲突，保留更严格的断言；不得为了通过测试删除敏感泄露、错发素材、重复发送检测。

## 4. 合入后必跑

```powershell
$env:PYTHONPATH="$env:TEMP\wecom-room-robot-local-test-deps"
python -m pytest -q tests/test_kf_dual_llm_production.py tests/test_kf_outbound_validation.py tests/test_kf_llm2_outbound.py
python -m pytest -q tests/test_inventory_read_router.py tests/test_inventory_read_turn.py tests/test_inventory_sensitive_access.py tests/test_inventory_snapshot.py
python -m pytest -q tests/test_media_manifest.py tests/test_media_store.py
python -m pytest -q tests/test_kf_send_receipts.py tests/test_kf_context_memory.py tests/test_kf_send_receipt_faults.py
python -m pytest -q tests/test_release_pipeline.py tests/test_qa_utf8_inputs.py tests/test_real_dialogue_fixtures.py
python -m pytest -q tests/test_wecom_kf.py
python -m compileall app
git diff --check
```

最终 release 前再跑：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/rag-v2-test-gates.ps1 -Level L4
```

如果真实对话 fixture 缺失，L4 不得作为上线通过证据。
