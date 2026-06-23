# 无人值守运行说明

这份说明用于把本项目配置成可长期运行的服务，尽量避免 Codex 工作时弹出权限确认、浏览器登录或人工输入。

## 本地 Codex

- Codex 会话使用 `danger-full-access`、`approval_policy=never`、网络开启。
- 本地服务器访问凭证放在 `.local/server-credentials.ps1`，支持：
  - `ROOM_ROBOT_SSH_PASSWORD`
  - `ROOM_ROBOT_SSH_KEY`
  - `ROOM_ROBOT_PLINK`
- 运维入口统一使用 `scripts/server-ops.ps1`，不要临时拼交互式命令。

## 服务器

- 主服务：`wecom-room-robot.service`
- 四区房源同步：`wecom-room-robot-feishu-region-sync.timer`
- RAG 房源索引刷新：`wecom-room-robot-rag-cache-sync.timer`
- 服务端配置文件：`/opt/wecom-room-robot/.env`

服务端 `.env` 必须提供企业微信、飞书、LLM、房源表、素材库相关凭证。检查脚本只报告是否缺失，不输出密钥值。

## 飞书与企业微信

- 飞书使用应用态 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 自动换取 token。
- 企业微信使用服务端回调密钥和客服密钥，不依赖浏览器授权。
- 房源表同步和素材同步由服务器定时任务执行，不放在人工浏览器流程里。

## 常用检查

在本地项目根目录运行：

```powershell
.\scripts\server-ops.ps1 UnattendedCheck
```

检查内容包括：

- 本地是否有免交互 SSH 凭证
- 服务器项目目录和 `.env` 是否存在
- 主服务和两个定时器是否启用/运行
- 必要环境变量是否存在且不是占位符
- 健康接口是否可访问

常用运维命令：

```powershell
.\scripts\server-ops.ps1 Status
.\scripts\server-ops.ps1 Health
.\scripts\server-ops.ps1 Timers
.\scripts\server-ops.ps1 Restart
.\scripts\server-ops.ps1 SyncDryRun
.\scripts\server-ops.ps1 RagCacheSync
```
