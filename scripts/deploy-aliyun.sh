#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 .env，请先填写企业微信、阿里云百炼和域名配置。"
  exit 1
fi

docker compose up -d --build
docker compose ps
