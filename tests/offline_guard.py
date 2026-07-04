from __future__ import annotations

import os
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any


SENSITIVE_ENV_KEYS = (
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "WECOM_CORP_SECRET",
    "WECOM_TOKEN",
    "WECOM_ENCODING_AES_KEY",
    "WECOM_CORP_ID",
    "WECOM_SECRET",
    "WECOM_AES_KEY",
    "WECOM_KF_SECRET",
    "WECOM_KF_TOKEN",
    "WECOM_KF_AES_KEY",
    "SSH_PASSWORD",
    "SERVER_PASSWORD",
    "ROOM_ROBOT_SSH_PASSWORD",
    "REMOTE_HOST",
    "SERVER_HOST",
)
REAL_LLM_API_ENV_KEYS = {
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
}

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_ACTIVATED = False
_BLOCKED_NETWORK_CALLS: list[dict[str, Any]] = []
_PRE_ACTIVATION_IMPORTS: list[str] | None = None
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_OFFLINE_INVENTORY_SHEET_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class OfflineNetworkError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_local_host(host: Any) -> bool:
    text = str(host or "").strip().lower()
    return text in LOCAL_HOSTS or text.startswith("127.")


def _record_and_raise(host: Any, port: Any) -> None:
    stack = "".join(traceback.format_stack(limit=12))
    item = {"host": str(host), "port": str(port), "stack": stack}
    _BLOCKED_NETWORK_CALLS.append(item)
    raise OfflineNetworkError(
        "Blocked external network call during offline test: "
        f"{item['host']}:{item['port']}\n{stack}"
    )


def real_llm_api_tests_enabled() -> bool:
    return (
        os.environ.get("RUN_ONLINE_QA") == "1"
        and os.environ.get("RUN_REAL_LLM_API_TESTS") == "1"
    )


def _ensure_offline_inventory_sheet_fixture(root: Path) -> Path:
    import base64

    base_dir = Path(os.environ.get("TEMP") or root / "data")
    path = base_dir / "wecom_room_robot_offline_inventory_sheet.png"
    if not path.is_file() or path.stat().st_size == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(_OFFLINE_INVENTORY_SHEET_PNG_BASE64))
    return path


def _default_offline_outbox_path(root: Path) -> Path:
    # 离线 QA 必须使用每进程唯一的出站台账:多个 QA 进程共用
    # data/kf_send_outbox.jsonl 会互相争抢文件锁(Windows 下 LK_LOCK
    # 重试约 9 秒后报 OSError),且台账随 QA 轮次无限增长拖垮 send 阶段。
    base_dir = Path(os.environ.get("TEMP") or root / "data") / "wecom_room_robot_offline_outbox"
    return base_dir / f"kf_send_outbox_{os.getpid()}_{time.time_ns()}.jsonl"


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    host, port = address[:2] if isinstance(address, tuple) else (address, "")
    if not _is_local_host(host):
        _record_and_raise(host, port)
    return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)


def _guarded_socket_connect(self: socket.socket, address: Any) -> Any:
    host, port = address[:2] if isinstance(address, tuple) else (address, "")
    if not _is_local_host(host):
        _record_and_raise(host, port)
    return _ORIGINAL_SOCKET_CONNECT(self, address)


def activate_offline_test_mode() -> None:
    global _ACTIVATED, _PRE_ACTIVATION_IMPORTS
    if _PRE_ACTIVATION_IMPORTS is None:
        watched_modules = (
            "app.main",
            "app.config",
            "app.services.llm",
            "openai",
            "wecom_aibot_sdk",
        )
        _PRE_ACTIVATION_IMPORTS = [
            module_name for module_name in watched_modules if module_name in sys.modules
        ]

    allow_real_llm = real_llm_api_tests_enabled()
    for key in SENSITIVE_ENV_KEYS:
        if allow_real_llm and key in REAL_LLM_API_ENV_KEYS:
            continue
        os.environ.pop(key, None)

    root = repo_root()
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("KF_AGENTIC_RAG_KNOWLEDGE_DIR", str(root / "knowledge" / "kf"))
    os.environ.setdefault("INVENTORY_SOURCE", "local_cache")
    os.environ.setdefault("INVENTORY_CACHE_PATH", "tests/fixtures/qa/test_inventory_cache.csv")
    os.environ.setdefault("INVENTORY_CACHE_META_PATH", "data/test_inventory_cache_meta.json")
    os.environ.setdefault("REWRITE_INVENTORY_INDEX_PATH", "tests/fixtures/qa/test_rewrite_inventory_index.json")
    os.environ.setdefault("ROOM_DATABASE_PATH", "room_database")
    os.environ.setdefault("INVENTORY_IMAGE_GLOB", str(_ensure_offline_inventory_sheet_fixture(root)))
    os.environ.setdefault("MEDIA_ROOT", "media/rooms")
    os.environ.setdefault("KF_DIALOGUE_EVENT_LOG_PATH", "data/test_kf_dialogue_events.jsonl")
    os.environ.setdefault("KF_SEND_OUTBOX_PATH", str(_default_offline_outbox_path(root)))

    if _ACTIVATED or allow_real_llm:
        return
    socket.create_connection = _guarded_create_connection
    socket.socket.connect = _guarded_socket_connect
    _ACTIVATED = True


def offline_guard_status() -> dict[str, Any]:
    return {
        "activated": _ACTIVATED,
        "pre_activation_imports": list(_PRE_ACTIVATION_IMPORTS or []),
        "blocked_network_call_count": len(_BLOCKED_NETWORK_CALLS),
        "blocked_network_calls": list(_BLOCKED_NETWORK_CALLS),
    }


def reset_offline_guard_observations() -> None:
    _BLOCKED_NETWORK_CALLS.clear()
