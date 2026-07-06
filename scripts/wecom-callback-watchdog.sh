#!/usr/bin/env bash
# 企业微信「微信客服」回调路由 watchdog。
# 部署路径：/usr/local/sbin/wecom-callback-watchdog.sh（本文件为仓库源真值副本）。
# 由 systemd wecom-room-robot-callback-watchdog.timer 每 10 分钟触发。
#
# 目的：探测 Certbot 重写导致的 IP:80 /wecom 回调 404 复发（历史两次失聪）。
# 判据：404=坏（nginx 未转发/wecom），200 或 422=好（已路由到应用）。
# 输出：journal（logger tag=wecom-callback-watchdog）+ 状态文件 JSON。
set -u
HOST=114.55.168.97
IP_URL=http://127.0.0.1/wecom/kf/callback
HTTPS_URL=https://ynzyqbot.cn/wecom/kf/callback
STATE=/opt/wecom-room-robot/data/callback_watchdog_state.json
TS=$(date -Is)

IP_CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $HOST" "$IP_URL" 2>/dev/null)
HTTPS_CODE=$(curl -sk --resolve ynzyqbot.cn:443:127.0.0.1 -o /dev/null -w '%{http_code}' "$HTTPS_URL" 2>/dev/null)

status=ok
case "$IP_CODE" in
  200|422) status=ok ;;
  404)     status=route_404 ;;
  *)       status=unexpected ;;
esac
# https 路由也纳入：若域名回调也 404，属更严重
if [ "$HTTPS_CODE" = "404" ]; then status=https_route_404; fi

mkdir -p "$(dirname "$STATE")" 2>/dev/null
printf '{"ts":"%s","ip80_code":"%s","https_code":"%s","status":"%s","probe":"%s"}\n' \
  "$TS" "$IP_CODE" "$HTTPS_CODE" "$status" "$IP_URL" > "$STATE"

if [ "$status" != "ok" ]; then
  logger -t wecom-callback-watchdog "ALERT status=$status ip80=$IP_CODE https=$HTTPS_CODE url=$IP_URL — 回调路由异常，疑似 Certbot 重写；检查 /etc/nginx/conf.d/00-wecom-ip-callback.conf 是否在位、nginx -t、reload"
  echo "ALERT status=$status ip80=$IP_CODE https=$HTTPS_CODE" >&2
  exit 1
fi
logger -t wecom-callback-watchdog "ok ip80=$IP_CODE https=$HTTPS_CODE"
exit 0
