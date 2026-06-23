#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-/opt/wecom-room-robot}"

cd "$PROJECT_DIR"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip nginx

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

cat >/etc/systemd/system/wecom-room-robot.service <<SERVICE
[Unit]
Description=WeCom Room Reply Robot
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${PROJECT_DIR}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${PROJECT_DIR}/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now wecom-room-robot
if [ -f "${PROJECT_DIR}/infra/systemd/wecom-room-robot-feishu-region-sync.service" ]; then
  cp "${PROJECT_DIR}/infra/systemd/wecom-room-robot-feishu-region-sync.service" /etc/systemd/system/
  cp "${PROJECT_DIR}/infra/systemd/wecom-room-robot-feishu-region-sync.timer" /etc/systemd/system/
  cp "${PROJECT_DIR}/infra/systemd/wecom-room-robot-rag-cache-sync.service" /etc/systemd/system/
  cp "${PROJECT_DIR}/infra/systemd/wecom-room-robot-rag-cache-sync.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now wecom-room-robot-feishu-region-sync.timer
  systemctl enable --now wecom-room-robot-rag-cache-sync.timer
fi
systemctl status wecom-room-robot --no-pager
