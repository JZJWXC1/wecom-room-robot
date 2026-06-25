# Inventory Sensitive Access

本文记录 M1D2B1 对看房信息和房源表产物的读取收口。目标是让敏感事实也遵守同一个 `InventoryReadContext`，同时不改变客户可见回复、Planner、自检和发送语义。

## 范围

- 新增 `app/services/inventory_sensitive_access.py`。
- `app/main.py` 只在工具编排处调用统一 provider，不新增 source selection、fallback 业务规则、候选编号解析或固定回复。
- Legacy 模式继续复用原 `看房方式密码` 字段和旧 PNG 列表，保持客户回复等价。
- Snapshot 模式只在本地测试中读取 Context 锁定的 snapshot，不读取 current pointer 的最新状态，不访问线上服务。

## Viewing Contract

`ViewingAccessRequest` 必须携带 `request_id`、`turn_id`、`task_id`、`decision_id`、`InventoryReadContext`、`listing_id`、用途和请求字段。Provider 会校验请求中的 `decision_id` 与 Context 一致，工具不能在执行中途重新选择 Context。

`ViewingInstructionEvidence` 只允许把真实看房文本放在 `SecretValue` 内部。`repr`、`str`、日志 dict、普通 tool summary 和 QA artifact 都不能输出真实密码。只有客户显式询问看房或密码，并且已经绑定具体房源时，legacy rule dict 才能为旧回复函数揭示同一条字段值，以保持当前话术逐字等价。

批量“把所有密码都发我”会被标记为 `viewing_batch_password_blocked`，不返回多房源密码。视频、图片、房源表动作不会触发 viewing provider。

## Sheet Artifact Contract

`InventorySheetArtifactProvider` 将房源表 PNG 作为带 Context 的 artifact evidence 输出：

- `decision_id`
- `source_kind`
- `source_hash`
- `schema_version`
- `snapshot_id`
- `safe_filename`
- `relative_path`
- `sha256`
- `byte_size`
- `mime_type`

Legacy provider 只包装既有 `_refresh_inventory_images(force=False)` 和 `_current_inventory_images()`，不改变旧刷新和发送路径。Snapshot provider 只读取 Context 的 `snapshot_id` 对应 manifest，并校验 PNG hash/size，禁止 fallback 到旧 PNG。

## Safety

- 普通 `InventoryListingEvidence` 不含密码。
- `ToolEvidence` summary 中的 viewing 统一走 `safe_rule_evidence_for_summary`。
- `knowledge_context` 写入确定性规则证据前先脱敏。
- consistency failure 时返回结构化错误，清空相关客户可见事实或素材动作。
- error payload 只包含 reason code、source metadata 和安全摘要，不输出真实密码、手机号、token 或开发机绝对路径。

## Tests

M1D2B1 覆盖：

- `tests/test_inventory_sensitive_access.py`
- `tests/test_wecom_kf.py::MainAgenticRagFlowTests::test_tool_evidence_summary_redacts_viewing_secret`
- `tests/test_wecom_kf.py::MainAgenticRagFlowTests::test_video_action_does_not_create_viewing_instruction_evidence`
- `tests/test_wecom_kf.py::MainAgenticRagFlowTests::test_send_inventory_sheet_uses_artifact_evidence`

这些测试证明 legacy/shadow 不读 Snapshot provider、snapshot Context 不随 current pointer 漂移、房源表 artifact 绑定同一 `decision_id/source_hash`、普通摘要不泄露看房密码。
