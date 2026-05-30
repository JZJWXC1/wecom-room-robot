# 阿里云轻量应用服务器部署步骤

## 1. 服务器准备

建议系统选择 Ubuntu 22.04 或 24.04。登录服务器后，在项目目录执行：

```bash
sudo bash scripts/bootstrap-aliyun-ubuntu.sh
```

阿里云轻量应用服务器控制台里需要放行：

- 80：HTTP，用于证书签发和跳转
- 443：HTTPS，用于企业微信回调

## 2. 上传项目

推荐目录：

```bash
/opt/wecom-room-robot
```

上传后进入目录：

```bash
cd /opt/wecom-room-robot
cp .env.example .env
```

## 3. 填写 .env

至少填写这些项：

```text
PUBLIC_BASE_URL=https://你的域名
WECOM_CORP_ID=企业微信企业ID
WECOM_AGENT_ID=自建应用AgentId
WECOM_SECRET=自建应用Secret
WECOM_TOKEN=企业微信回调Token
WECOM_AES_KEY=企业微信EncodingAESKey
DASHSCOPE_API_KEY=阿里云百炼API Key
INVENTORY_SOURCE=local_image
ROOM_DATABASE_PATH=room_database
INVENTORY_IMAGE_PATH=room_database/inventory.png
```

当前先不走金山文档，房源表图片和视频统一放在服务器：

```text
/opt/wecom-room-robot/room_database
```

从 Windows 本机上传 `D:\房源数据库`：

```powershell
.\scripts\upload-room-database.ps1 -HostName 服务器公网IP -User root
```

如果使用 SSH 密钥：

```powershell
.\scripts\upload-room-database.ps1 -HostName 服务器公网IP -User root -KeyPath C:\path\to\key.pem
```

检查配置：

```bash
python3 scripts/check_config.py
```

如果服务器没有本地 Python，也可以先跳过这一步，Docker 启动后用 `/health` 检查。

## 4. 启动机器人

```bash
bash scripts/deploy-aliyun.sh
```

查看运行状态：

```bash
docker compose ps
docker compose logs -f robot
```

## 5. 配置域名和 HTTPS

确认域名 A 记录已经指向服务器公网 IP 后执行：

```bash
bash scripts/setup-nginx.sh 你的域名 你的邮箱
```

完成后访问：

```text
https://你的域名/health
```

## 6. 企业微信后台配置

企业微信自建应用的接收消息服务器 URL：

```text
https://你的域名/wecom/callback
```

Token 和 EncodingAESKey 必须和 `.env` 一致。企业微信 URL 验证通过后，再给自建应用发消息测试。
