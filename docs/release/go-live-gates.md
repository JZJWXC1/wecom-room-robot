# 上线门禁

本文档定义本项目从本地改动进入服务器或线上系统前必须满足的门禁。M0 不执行部署，只建立边界。

## 绝对部署门禁

以下任何动作都必须先获得用户明确写出的 `APPROVE_DEPLOY`：

- SSH 或连接阿里云服务器。
- 上传代码到服务器。
- 重启服务。
- 修改 systemd service 或 timer。
- 执行服务器上的真实同步脚本。
- 飞书线上表格写入。
- 飞书云盘写入、移动、删除或上传。
- 企业微信配置修改或真实发送测试。
- 任何线上数据修复。

没有 `APPROVE_DEPLOY` 时，只允许本地代码、文档、fixture、离线测试和只读分析。

## 本地测试门禁

部署前至少需要完成：

1. 记录 `git status --short`。
2. 确认没有意外读取或提交真实 `.env`、`.local`、密钥、token、App Secret、服务器凭证。
3. 使用无真实凭证 fixture 或临时测试目录运行测试。
4. 运行 `pytest -q`。
5. 运行仓库现有 QA，并确认输入为 UTF-8。
6. 运行本地 release/current rehearsal，确认 current 指针、release manifest、rollback rehearsal、health payload 解析和服务器操作审批门禁都通过。
7. 将测试日志保存到 `qa_artifacts/` 下的脱敏目录。
8. 明确区分失败类型：
   - 业务断言失败。
   - 环境或依赖失败。
   - 外部服务依赖失败。
   - 测试自身读取线上配置的问题。

## V1 Production Cutover 本地 L4

`scripts/rag-v2-test-gates.ps1 -Level L4` 是 V1 cutover 前的本地 release gate。它只允许离线、本地、脱敏验证，不允许 SSH、部署、连接飞书或企业微信线上服务。

L4 必须同时满足：

- 全量 pytest 重复执行通过。
- 20+ parity QA、真实对话回放 QA、历史失败回放 QA、随机 10 问保底 QA 都生成 QA artifact。
- 每个 release QA artifact 的 `quality_status.passed=true`、`high_count=0`、`medium_count=0`，且完整回放 artifact 必须是 `summary.usable_for_release=true`。
- `tests/fixtures/qa/real_server_dialogues_sanitized.json` 必须存在并满足最小 10 个窗口、100 轮回放；显式传入 `-AllowMissingRealDialogues` 只允许继续跑后续本地检查，但最终仍记录红色 release blocker，不能上线。
- `tests/fixtures/qa/historical_failures_synthetic_sanitized.json` 必须存在并满足历史失败回放最小范围；显式传入 `-AllowMissingHistoricalFailures` 只允许继续跑后续本地检查，但最终仍记录红色 release blocker，不能上线。
- 历史失败回放 runner 输出和落盘 artifact 必须脱敏，不得包含手机号、查看凭据、token、长 hash 或原始签名。
- 显式传入 `-SkipParity` 会记录 release blocker，不能上线。
- 本地 release/current rehearsal 必须生成报告，并验证 current 指针、rollback、health contract、server-ops 审批门禁。
- secret scan 必须对 git 跟踪文件通过；`.env`、`.local`、运行时目录和素材目录不得进入扫描输出或 release manifest。

本地 release rehearsal 不读取真实 `.env` 内容，只报告 `.env` 是否存在、必需键数量和“未读取密钥文件内容”的策略状态；真实无人值守凭证完整性只允许在获得 `APPROVE_DEPLOY` 后通过服务器侧检查确认。

## CI 门禁

GitHub Actions 只能执行：

- checkout。
- Python 环境安装。
- 项目声明依赖和测试依赖安装。
- UTF-8/编译检查。
- 本地单元测试。

CI 禁止包含：

- SSH。
- 服务器地址。
- 飞书写入。
- 企业微信写入。
- 部署。
- 服务重启。
- 输出环境变量内容。

如果当前测试无法在干净环境稳定运行，CI 只能作为草案保留，并在报告里标记阻塞，不能伪造绿色结果。

## 服务器门禁

只有获得 `APPROVE_DEPLOY` 后，才允许执行服务器步骤。服务器步骤至少包括：

1. 上传前再次确认本地测试结果。
2. 上传到服务器目标目录。
3. 在服务器运行全量测试。
4. 重启服务。
5. 检查健康接口。
6. 检查定时器状态。
7. 抽查最新真实对话日志。
8. 明确记录部署版本和回滚路径。

M0 阶段不执行以上服务器步骤。
