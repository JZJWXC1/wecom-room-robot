# 企微客服回调 Certbot 复发防线（B+C 已落地，A 待用户操作）

> 2026-07-06。归属：运维 / 入站回调可达性 + 无人值守监控。不触碰 Agentic RAG 对话链路。
> 相关：`docs/DECISIONS.md` 同日条目；第一次 404 事件见 DECISIONS 2026-07-04 P0-2 收官记录。

## 1. 根因

企微「微信客服」回调 URL 配的是 `http://114.55.168.97/wecom/kf/callback`（IP 主机 + 端口 80）。
`/etc/nginx/conf.d/ynzy-miniapp.conf` 的 `listen 80` 块由 Certbot 托管：块内两条
`if ($host = 域名) return 301 https` 只对**域名**主机跳转，对 **IP 主机不跳转**，
Host=IP 的请求落到块内 location。手工加的 `location ^~ /wecom/` 转发在每次手工
`certbot --nginx`（扩子域名证书）重跑时，随整个 `listen 80` 块被重写为
"域名跳 https + 裸 `return 404`" 而被抹掉 → IP:80 的 `/wecom/` 落到 `return 404`
→ 回调 404 → 机器人对客失聪。2026-07-04、2026-07-06 两次实证（后者约 19 小时，23:06→18:41）。

**注**：每日 `certbot.timer` 只续期不重写 nginx；真正抹除来自**手工扩域名的 certbot --nginx 重跑**。

## 2. 勘察结论（2026-07-06 服务器实况）

| 探测 | 结果 | 含义 |
|---|---|---|
| `IP:80 /wecom/kf/callback` | 422 | 已路由到应用（18:41 修复在位）。**判据是"非 404"，实测 422 不是 500**——裸 GET 缺签名参数时 FastAPI 返回 422 |
| `https://ynzyqbot.cn/wecom/kf/callback` | 422 | 443 块的 /wecom location 从不被 Certbot 抹除 → https 路径天然稳定 |
| 证书 | Let's Encrypt，CN=ynzyqbot.cn，至 2026-09-10 | WeCom 信任 LE，https 回调证书链 OK |
| `certbot.timer` | enabled，每日约 2 次 | 续期不重写 |
| 服务器 `.env` | `PUBLIC_BASE_URL=http://114.55.168.97` | 媒体签名链接与 config_check 均基于此 |
| IP:80 `/media` `/room-database` | 404 | **IP:80 只承载 `/wecom/`**；媒体公网链接（`http://IP/media/...`）在 IP:80 已 404 |

副发现：因 `PUBLIC_BASE_URL=http://IP` 且 IP:80 /media=404，下发给客户的**视频公网链接目前在 IP:80 会 404**；
切到 https 域名（方案 A）会**顺带修复媒体链接**。

## 3. 已落地（B + C，2026-07-06，APPROVE_DEPLOY）

### B — 独立 conf 抢占 IP:80（Certbot 免疫）
- 新增 `/etc/nginx/conf.d/00-wecom-ip-callback.conf`（仓库源真值：`infra/nginx/00-wecom-ip-callback.conf`）。
- 独立 server 块 `listen 80; server_name 114.55.168.97;`，`/wecom/`→`127.0.0.1:8000`、其余 `return 404`，带 `X-Callback-Guard: ip80-standalone` 取证头。
- 原理：独立文件 Certbot 不托管；`00-` 前缀在 conf.d 中最先加载，赢得 `114.55.168.97` 的 server_name（ynzy-miniapp 同名声明因冲突被忽略，仅一条 cosmetic warn，`nginx -t` 通过）。→ Host=IP 的 :80 请求恒由本块处理，Certbot 原地重写 ynzy-miniapp 永远夺不回 IP。
- 验证：reload 后 `curl -H "Host: 114.55.168.97" http://127.0.0.1/wecom/kf/callback` → **422 + X-Callback-Guard 头**（证明是新块在服务）；域名 :80 仍 301→https 且无 guard 头；https /wecom 仍 422；app health 200。
- 纯追加、不碰 Certbot 托管文件；改动前 `nginx -T` 全量备份至 `/root/nginx-backups/`。回滚 = 删该文件 + reload。

### C — 回调健康监控（journal + 状态文件）
- 服务器新增 `wecom-room-robot-callback-watchdog.{service,timer}`（仓库：`infra/systemd/`）+ 脚本 `/usr/local/sbin/wecom-callback-watchdog.sh`（仓库：`scripts/wecom-callback-watchdog.sh`）。
- 每 10 分钟探测 IP:80 + https 回调，`404` 写 journal（`journalctl -t wecom-callback-watchdog`）+ 状态文件 `/opt/wecom-room-robot/data/callback_watchdog_state.json`；判据 404=坏、200/422=好。
- 已 `enable --now` 并首跑：state=`{"ip80_code":"422","https_code":"422","status":"ok"}`。
- 仓库侧：`scripts/check_unattended_runtime.py` 增加回调路由探测（`--callback-*` 参数，404 记 `problem=callback_route:route_404`）+ `tests/test_check_unattended_callback.py` 8 项。
  **未部署到服务器**：服务器 `scripts/check_unattended_runtime.py` 是较旧分叉版（154 行 vs 仓库 223 行），上传会顺带引入本任务外的行为变化，故仅入仓库前向状态，漂移另行治理（见 §5）。

## 4. 待用户操作 —— 方案 A（把回调迁到 https，彻底摆脱 IP 依赖）

B 在位后回调已永久安全，A 是战略清理（更安全 + 顺带修复媒体链接）。A 需两步，**第一步只有你能在企微后台做**：

### A-1（用户，企微管理后台）
1. 登录 [企业微信管理后台](https://work.weixin.qq.com) → 「应用管理」→「微信客服」→ 开发者设置 / API 接收消息。
2. 把「回调 URL」从 `http://114.55.168.97/wecom/kf/callback` 改为
   **`https://ynzyqbot.cn/wecom/kf/callback`**（Token / EncodingAESKey 不变）。
3. 保存时企微会向新 URL 发 GET echostr 验证握手——该 https 路径已实测可达（返回 422/正确响应），
   Token/AESKey 未变，握手会通过。若保存报错，说明验证未过，**不要强改**，回退原 IP URL 即可（B 仍保底）。

### A-2（我，服务器 .env 迁移，需你新的 APPROVE_DEPLOY）
> 此步改动**客户可见媒体 URL**（http://IP → https://域名），属行为变化，需你明确同意再做。可与 A-1 解耦、任意先后。
1. 备份 `.env` 与 `current/.env`；`PUBLIC_BASE_URL=http://114.55.168.97` → `PUBLIC_BASE_URL=https://ynzyqbot.cn`。
2. 重启 `wecom-room-robot`；健康检查 + 合成媒体链接验证（https /media 走 443 块，实测路由正常）。
3. 效果：新签名媒体链接变 `https://ynzyqbot.cn/media/...`（顺带修复 IP:80 /media 的 404）；
   `config_check.py` 的 `kf_callback_url`/`callback_url` 自动派生为 https（无硬编码 IP，见 `app/services/config_check.py:70-71`）。
4. 旧的 `http://IP/media/...` 链接：已下发的历史链接仍由 ynzy-miniapp 的 IP 处理（B 未改这些路径的现状）。

**A 完成后**：`00-wecom-ip-callback.conf` 与 watchdog 变为纯冗余保底，可保留（零成本、且是排障取证点）。

## 4.5 A 执行中发现并修复：智能机器人 URL 验证空 receiveid → 500（2026-07-06 已修）

用户在企微后台把智能机器人「住一起中介小帮手」URL 回调改 https 保存时报「HTTP返回码500」。逐层定位：
1. 排除 nginx/证书（https 侧 nginx 返回 422 正常、证书有效）。
2. 应用日志：先是 `wx_crypto.verify_signature` 抛「签名校验失败」——后台表单 Token/AESKey 被「随机获取」改过、与服务器 `.env` 不一致（保存失败未落库故线上未受影响，机器人未中断）。用户把表单 Token/AESKey 回填为服务器现值（`y0InLS` / `8Zms7SC...`）后仍 500。
3. 新堆栈变为 `wx_crypto.decrypt` 抛「CorpID 不匹配」。用服务器 AESKey 裸解密该验证 echostr（**解密成功**，证明密钥已对）：echostr 明文=`8914615545705267199`、**receiveid=空字符串**，而 `decrypt` 强制要求 `receiveid == WECOM_CORP_ID`（`ww89008239fc5b4654`）→ 空≠corp → 500。

**根因**：企业微信「智能机器人」URL 回调地址验证的 echostr 携带**空 receiveid**（与自建应用/微信客服携带 CorpID 不同），KF 导向的 `decrypt` 不兼容。

**修复**（`app/services/wx_crypto.py`，已 APPROVE_DEPLOY 部署验证）：`if receive_id != self.corp_id:` → `if receive_id and receive_id != self.corp_id:`——空 receiveid（验证握手）放行、非空（真实消息）仍校验 CorpID，签名校验始终生效，不弱化消息路径安全。新增 `tests/test_wx_crypto.py` 6 项、与 test_wecom_kf 合跑 457 passed 零回归。用企微真实签名请求回放：app/IP:80/https 三路径均 200 + 正确明文。服务器以原地热修补丁部署（备份 `wx_crypto.py.bak-emptyrecv-20260706-213949` + restart）。

**边界**：该端点原按微信客服(KF)消息格式实现；智能机器人若消息 schema 不同，需真实客户消息端到端确认处理分支正常（已提示用户保存后实测一条）。

## 5. 遗留 / 结构债
- **服务器 `scripts/check_unattended_runtime.py` 与仓库漂移**（服务器 154 行 / 旧，仓库 223 行 / 新，含 media_manifest、dual_llm 检查与本次回调探测）。需一次运维窗口把仓库版对齐部署到服务器（会引入较新的 readiness 校验，需评估 env/manifest 前置是否满足），本任务未做以免越界。
- 方案 A 的 PUBLIC_BASE_URL 迁移（A-2）待用户 APPROVE_DEPLOY。

## 6. 台账说明
本批只**新增**监控检查与测试，未改动任何 QA/裁判验收口径或既有测试断言，故不涉及
`docs/release/acceptance-criteria-change-log-*.md` 台账登记（该台账专用于验收口径变更）。
