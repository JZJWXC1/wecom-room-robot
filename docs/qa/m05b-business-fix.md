# M0.5B 房源表动作 / 最终自检业务失败修复报告

## 范围

本轮只处理已锁定的 3 个房源表动作 / 最终自检失败：

- `test_inventory_sheet_hard_rule_keeps_area_constraint_in_reply`
- `test_inventory_sheet_hard_rule_keeps_prepared_image_action`
- `test_planner_reply_uses_tiered_final_selfcheck_for_inventory_sheet`

未修改生产业务代码，未进入 M1，未部署。

## 精确根因

3 个测试都把 `tool_evidence.inventory_images` 设置为 `room_database/inventory_01.png`，但在隔离测试工作目录下这个文件不存在。

`app/main.py::_generate_reply_result` 会把工具证据组装成待发送包，并在最终自检前做动作硬校验。房源表图片动作属于确定性工具动作，但动作证据必须指向真实存在的本地图片。由于测试提供的是不存在路径，`outbound_package_selfcheck` 正确判定：

- `rule_source = outbound_package_selfcheck`
- `rule_reason = 房源表动作包含不存在的本地图片`
- `needs_planner_retry = True`
- `reply = ""`
- `draft_reply = 房源表发你了，你可以让客户先整体看一下。`

因此失败不是 Planner 或 selfcheck 对真实房源表动作过度拦截，而是正向测试没有提供“已准备好”的图片证据。

## 为什么不改生产代码

生产链路里的房源表图片应来自实际工具结果；如果路径不存在，动作就不应该发送。直接放宽 `needs_planner_retry` 会有风险：

- 可能放行缺失图片动作；
- 可能放行区域错误或无证据动作；
- 可能影响视频、图片、密码等其他动作的安全策略。

本轮最小修复是让正向测试提供真实临时 PNG 文件，代表“工具已经准备好房源表图片”；同时新增反向测试，证明缺失图片仍然会触发 retry。

## 修改内容

链路归属：测试覆盖。

- 在 `tests/test_wecom_kf.py` 的 `MainAgenticRagFlowTests` 增加 `_prepared_inventory_image()`，生成测试专用临时 PNG。
- 将 3 个正向房源表动作测试中的 `inventory_images` 改为真实临时 PNG 路径。
- 新增 `test_inventory_sheet_missing_image_action_still_requires_retry`，覆盖缺失房源表图片不能放行。

## 修改前后控制流

修改前：

1. 测试传入不存在的 `room_database/inventory_01.png`。
2. 待发送包包含 `send_inventory_sheet` 和缺失图片路径。
3. `outbound_package_selfcheck` 判定图片不存在。
4. `_generate_reply_result` 返回 `needs_planner_retry=True`。

修改后：

1. 正向测试传入真实存在的临时 PNG。
2. 待发送包硬校验通过。
3. tiered final selfcheck 选择房源表 profile，LLM final selfcheck 被安全跳过。
4. `_generate_reply_result` 保留房源表回复和图片动作，返回 `needs_planner_retry=False`。
5. 反向测试继续验证缺失 PNG 会被拦截。

## 测试结果

已运行：

- 目标 4 项：`4 passed, 2 warnings`
- `python -m compileall -q app scripts tests`：exit 0
- `python -m pytest -q tests`：`512 passed, 1 deselected, 2 warnings, 2 subtests passed`
- 固定 QA 同 seed 两次：两次均 `QUALITY passed=True infrastructure_error=False exit_code=0`

随机 fallback QA：

- 脚本启动并产生 artifact，但在第 4 个窗口第 35 轮附近写 JSON 时触发 Windows 路径转义相关 `OSError: Invalid argument`。
- 已落盘 artifact 显示 `network_call_count=0`，但 `completed=false`，因此本轮不把随机 QA 描述为通过。
- 该问题属于 QA runner / artifact 序列化基础设施问题，不在 M0.5B 允许修改范围内。

## Artifact

- `qa_artifacts/m05b/baseline_status_short.txt`
- `qa_artifacts/m05b/baseline_diff_ignore_cr_app_main_tests.txt`
- `qa_artifacts/m05b/pre_repro2_*.pytest.log`
- `qa_artifacts/m05b/root_cause_probe.jsonl`
- `qa_artifacts/m05b/control_existing_file_*.pytest.log`
- `qa_artifacts/m05b/post_patch_targeted_4.log`
- `qa_artifacts/m05b/compileall.log`
- `qa_artifacts/m05b/pytest_q.log`
- `qa_artifacts/m05b/qa_fixed_run1.log`
- `qa_artifacts/m05b/qa_fixed_run2.log`
- `qa_artifacts/m05b/qa_random_seed20260624.log`

Artifact 已做机械脱敏，手机号和本机绝对路径不保留明文。

## 未做事项

- 未修改 `app/main.py` 的生产行为。
- 未修复随机 QA runner 的 Windows 路径转义问题。
- 未处理随机 QA 中暴露的候选/素材绑定业务问题。
- 未进入 M1。

## 安全声明

- 未读取真实 `.env`。
- 未访问公网。
- 未连接飞书或企业微信线上服务。
- 未 SSH。
- 未部署服务器。
- 未进入 M1。
