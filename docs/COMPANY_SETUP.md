# 公司电脑继续运行调试流程

这份文档用于在另一台电脑上从 GitHub 拉取项目，并继续调试「寓你住一起」微信客服自动回复机器人。

## 1. 克隆项目

```bash
git clone <你的 GitHub 仓库地址>
cd 自动回复逻辑
```

如果公司电脑上目录名不支持中文，也可以克隆到英文目录，例如：

```bash
git clone <你的 GitHub 仓库地址> wecom-room-robot
cd wecom-room-robot
```

## 2. 准备 Python 环境

建议 Python 3.11 或 3.12。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. 准备配置

复制环境变量模板：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

然后把真实配置填入 `.env`。不要把 `.env` 提交到 GitHub。

核心配置包括：

```text
PUBLIC_BASE_URL=
WECOM_KF_SECRET=
WECOM_KF_TOKEN=
WECOM_KF_AES_KEY=
DASHSCOPE_API_KEY=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_TABLE_ID=
FEISHU_DRIVE_ROOT_FOLDER_TOKEN=
FEISHU_INVENTORY_SHEET_TOKEN=
```

## 4. 本地启动

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 5. 跑测试

```bash
pytest -q
```

如果在 Codex 桌面环境里运行，可以使用项目当前的测试方式：

```powershell
$env:PYTHONPATH="$env:TEMP\wecom-room-robot-local-test-deps"
& "C:\Users\吴志坚\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m pytest -q
```

## 6. 房源表和素材

GitHub 仓库不提交 `room_database/`、`media/`、`data/` 和本地截图，这些目录可能包含房源素材、客户上下文或运行数据。

当前推荐流程是：

1. 房源表以飞书电子表格为准。
2. 房间图片和视频放飞书云盘。
3. 服务器运行时从飞书同步到 `room_database/`。
4. 客户要房源表时，机器人在服务器生成 PNG 后发送。

手动触发飞书房源表 PNG 同步：

```bash
curl -X POST "http://127.0.0.1:8000/admin/feishu/sync-inventory-image?force=true"
```

手动触发飞书云盘素材同步：

```bash
curl -X POST "http://127.0.0.1:8000/admin/feishu/sync-media"
```

## 7. 阿里云服务器调试

服务器项目目录约定：

```text
/opt/wecom-room-robot
```

常用命令：

```bash
cd /opt/wecom-room-robot
systemctl status wecom-room-robot --no-pager
journalctl -u wecom-room-robot -n 200 --no-pager
curl -sS http://127.0.0.1:8000/health
```

部署修改后：

```bash
pytest -q
systemctl restart wecom-room-robot
curl -sS http://127.0.0.1:8000/health
```

## 8. 回复逻辑维护重点

- `app/main.py`：客服消息入口、意图识别、素材直发、上下文读取。
- `app/services/llm.py`：大模型提示词。
- `app/services/media_store.py`：素材匹配。
- `app/services/inventory_image_sync.py`：飞书电子表格转 PNG。
- `tests/test_wecom_kf.py`：微信客服回复逻辑测试。
- `tests/test_media_store.py`：素材匹配测试。
- `tests/test_feishu.py`：飞书同步测试。

每次改回复逻辑后，至少运行：

```bash
pytest -q tests/test_wecom_kf.py tests/test_media_store.py
```

改飞书同步后，至少运行：

```bash
pytest -q tests/test_feishu.py
```
