#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"

if [ -z "$DOMAIN" ]; then
  echo "用法：bash scripts/setup-nginx.sh 你的域名 你的邮箱"
  exit 1
fi

cd "$(dirname "$0")/.."

sudo sed "s/__DOMAIN__/${DOMAIN}/g" infra/nginx/wecom-room-robot.conf.template | sudo tee /etc/nginx/sites-available/wecom-room-robot >/dev/null
sudo ln -sf /etc/nginx/sites-available/wecom-room-robot /etc/nginx/sites-enabled/wecom-room-robot
sudo nginx -t
sudo systemctl reload nginx

if [ -n "$EMAIL" ]; then
  sudo certbot --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --no-eff-email
else
  sudo certbot --nginx -d "$DOMAIN" --register-unsafely-without-email --agree-tos
fi

sudo nginx -t
sudo systemctl reload nginx

echo "Nginx 和 HTTPS 已配置完成：https://${DOMAIN}/health"
