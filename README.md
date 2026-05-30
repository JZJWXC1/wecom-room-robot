# 企业微信房源自动回复机器人

这是一个可以部署到阿里云轻量应用服务器的企业微信自动回复服务。当前版本先使用服务器本地房源库，房源表图片和视频放在 `room_database` 目录；金山文档链路暂时停用。

## 当前已包含

- 企业微信回调地址校验
- 企业微信加密消息解析
- 微信客服回调、消息拉取和普通微信客户回复入口
- 文本、数字、链接等消息入口
- 图片、语音、视频消息的媒体 ID 接收入口
- 本地房源表图片和视频目录挂载
- 本地房源缓存兜底
- 阿里云百炼 DashScope OpenAI 兼容接口调用
- 房间图片/视频目录索引
- Docker Compose 部署文件

## 目录约定

```text
data/inventory_cache.csv      房源库存缓存
room_database/房源信息表.png   房源表图片
room_database/video/          房源视频
media/rooms/101/              101 房间图片和视频
media/rooms/202/              202 房间图片和视频
```

图片支持 `.jpg`、`.jpeg`、`.png`、`.webp`，视频支持 `.mp4`、`.mov`、`.m4v`。

## 本地启动

```bash
cp .env.example .env
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://localhost:8000/health
```

调试一条客户消息：

```bash
curl -X POST http://localhost:8000/debug/message \
  -H "Content-Type: application/json" \
  -d "{\"content\":\"还有一室一厅吗？预算3500\"}"
```

## 阿里云轻量应用服务器部署

1. 安装 Docker 和 Docker Compose。
2. 把本项目上传到服务器，例如 `/opt/wecom-room-robot`。
3. 复制配置：

```bash
cp .env.example .env
```

4. 填写 `.env`：

```text
PUBLIC_BASE_URL=https://你的域名
WECOM_CORP_ID=企业ID
WECOM_AGENT_ID=应用AgentId
WECOM_SECRET=应用Secret
WECOM_TOKEN=回调Token
WECOM_AES_KEY=EncodingAESKey
DASHSCOPE_API_KEY=阿里云百炼API Key
```

5. 启动服务：

```bash
bash scripts/deploy-aliyun.sh
```

6. 在阿里云轻量应用服务器防火墙放行 80、443。如果暂时不用 HTTPS 反代，也可临时放行 8000 做测试。

## 企业微信后台配置

企业微信自建应用的接收消息地址填写：

```text
https://你的域名/wecom/callback
```

Token 和 EncodingAESKey 要与 `.env` 中一致。企业微信校验通过后，用户给该应用发消息，服务器会收到回调并自动回复。

## 微信客服入口

普通微信客户不能直接进入企业微信智能机器人长连接。要回复普通微信客户，使用企业微信后台的「微信客服」入口，并把回调地址配置为：

```text
https://你的域名/wecom/kf/callback
```

`.env` 需要补充：

```text
WECOM_KF_SECRET=微信客服Secret
WECOM_KF_TOKEN=微信客服回调Token
WECOM_KF_AES_KEY=微信客服EncodingAESKey
```

微信客服回调只通知有新消息，服务会再调用 `/cgi-bin/kf/sync_msg` 拉取客户文本消息，复用现有房源匹配和大模型回复逻辑，然后通过 `/cgi-bin/kf/send_msg` 回复普通微信客户。已处理的 `msgid` 和游标会记录在 `data/wecom_kf_state.json`，避免重复回复。

客户明确要房源表、图片或视频时，服务会优先发送 `room_database` 中的素材，避免简单素材请求还等待大模型。房源表图片和房源视频会先上传为企业微信临时素材并原生发送，上传失败时才退回可打开链接。当前已关闭微信客服满意度追问，避免打断客户连续咨询。

创建客服账号可以使用：

```bash
python scripts/create_wecom_kf_account.py --name 寓你住一起房源客服
```

这个脚本对应微信客服「添加客服账号」接口，会用 `WECOM_KF_SECRET` 获取 `access_token`，上传头像临时素材，再创建客服账号并输出 `open_kfid`。

## 金山文档公开链接说明

当前配置：

```text
KDOCS_PUBLIC_URL=https://www.kdocs.cn/l/ctkRJKMS8Xkx?f=csv
```

公开 CSV 链接如果返回的是动态网页或登录页，程序无法稳定读到表格，会使用 `data/inventory_cache.csv`。临时上线时可以先定时把金山表格导出为 CSV 覆盖这个文件。后续拿到 WPS 开放平台权限后，再把读取模块切到官方 API。

## 下一步建议

先完成服务器部署和企业微信 URL 校验。校验成功后，把真实房源数据填入 `data/inventory_cache.csv`，再逐步接入语音转文字、图片理解、原生图片/视频素材发送。
