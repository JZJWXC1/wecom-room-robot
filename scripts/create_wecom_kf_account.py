import argparse
import base64
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

import httpx
from dotenv import load_dotenv


DEFAULT_AVATAR_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAAXNSR0IArs4c6QAAAARnQU1B"
    "AACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAEpSURBVGhD7ZqxCsJAEER3/v9n"
    "WQnBQiCKhYWFxUIR0UwCwXtxZ2bJwDMDd+fNmdlttwDgeR7fgZ2kMUkDkgYkDUgakDQg"
    "aUDSgKQBSQOSBiQNSBqQNKBpQNKApAFJA5IGJA1IGpA0IGlA0oCkAUkDkgYkDUgakDQg"
    "aUDSgKQBSQOSBiQNSBqQNKBpQNKApAFJA5IGJA1IGpA0IGlA0oCkAUkDkgYkDUgakDQg"
    "aUDSgKQBSQOSBiQNSBqQNKBpQNKApAFJA5IGJA1IGpA0IGlA0oCkAUkDkgYkDUgakDQg"
    "aUDSgKQBSQOSBiQNSBqQNKBpQNKApAFJA5IGJA1IGpA0IGlA0oCkAUkDkgYkDUgakDQg"
    "aUDSgKQBSQOSBiQNSBqQNKBpQNKApAFJA5IGJA1IGpA0IGlA0oCkAc89AP78BLtRZ8H/"
    "AAAAAElFTkSuQmCC"
)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("your_"):
        raise RuntimeError(f"缺少环境变量：{name}")
    return value


def get_access_token(corp_id: str, secret: str) -> str:
    response = httpx.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": corp_id, "corpsecret": secret},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    assert_ok(data, "获取微信客服 access_token 失败")
    return data["access_token"]


def upload_avatar(access_token: str, avatar_path: Path) -> str:
    url = (
        "https://qyapi.weixin.qq.com/cgi-bin/media/upload"
        f"?access_token={access_token}&type=image"
    )
    with avatar_path.open("rb") as file_obj:
        response = httpx.post(url, files={"media": file_obj}, timeout=60)
    response.raise_for_status()
    data = response.json()
    assert_ok(data, "上传微信客服头像失败")
    return data["media_id"]


def add_kf_account(access_token: str, name: str, media_id: str) -> dict[str, Any]:
    response = httpx.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/kf/account/add?access_token={access_token}",
        json={"name": name, "media_id": media_id},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    assert_ok(data, "创建微信客服账号失败")
    return data


def assert_ok(data: dict[str, Any], message: str) -> None:
    if data.get("errcode") != 0:
        raise RuntimeError(f"{message}：{data}")


def write_default_avatar() -> Path:
    path = Path(tempfile.gettempdir()) / "wecom-kf-default-avatar.png"
    path.write_bytes(base64.b64decode(DEFAULT_AVATAR_PNG))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="创建微信客服账号")
    parser.add_argument("--name", default="寓你住一起房源客服", help="客服账号名称，不超过16个字符")
    parser.add_argument("--avatar", type=Path, help="客服头像图片路径，留空则使用默认头像")
    args = parser.parse_args()

    load_dotenv()
    corp_id = require_env("WECOM_CORP_ID")
    secret = require_env("WECOM_KF_SECRET")
    avatar_path = args.avatar or write_default_avatar()
    if not avatar_path.exists():
        raise RuntimeError(f"头像文件不存在：{avatar_path}")

    token = get_access_token(corp_id, secret)
    media_id = upload_avatar(token, avatar_path)
    result = add_kf_account(token, args.name, media_id)
    print(f"创建成功：{args.name}")
    print(f"open_kfid={result['open_kfid']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
