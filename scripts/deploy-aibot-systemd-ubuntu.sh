#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-/opt/wecom-room-robot}"

cd "$PROJECT_DIR"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

cat >/etc/systemd/system/wecom-room-aibot.service <<SERVICE
[Unit]
Description=WeCom Smart Robot Long Connection
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${PROJECT_DIR}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${PROJECT_DIR}/.venv/bin/python -m app.aibot_runner
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now wecom-room-aibot
systemctl restart wecom-room-aibot
systemctl status wecom-room-aibot --no-pager
