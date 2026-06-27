from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


REQUIRED_ENV_KEYS = [
    "WECOM_KF_SECRET",
    "WECOM_KF_TOKEN",
    "WECOM_KF_AES_KEY",
    "DASHSCOPE_API_KEY",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_REGION_SYNC_SOURCES",
    "FEISHU_REGION_SYNC_TARGET_SPREADSHEET_TOKEN",
    "FEISHU_REGION_SYNC_TARGET_DRIVE_FOLDER_TOKEN",
    "ROOM_DATABASE_PATH",
    "MEDIA_ROOT",
]

SERVICE_NAMES = [
    "wecom-room-robot.service",
    "wecom-room-robot-feishu-region-sync.timer",
    "wecom-room-robot-rag-cache-sync.timer",
]


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        not lowered
        or lowered.startswith("your_")
        or "xxxxxxxx" in lowered
        or lowered in {"changeme", "todo", "none", "null"}
    )


def _systemctl_is_active(name: str) -> str:
    try:
        completed = subprocess.run(
            ["systemctl", "is-active", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return f"error:{exc}"
    return completed.stdout.strip() or completed.stderr.strip() or f"exit:{completed.returncode}"


def _health_check(url: str) -> str:
    try:
        completed = subprocess.run(
            ["curl", "-fsS", url],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return f"error:{exc}"
    if completed.returncode == 0:
        return _health_payload_status(completed.stdout)
    return (completed.stderr or completed.stdout or f"exit:{completed.returncode}").strip()


def _health_payload_status(payload_text: str) -> str:
    try:
        payload = json.loads(payload_text)
    except Exception as exc:
        return f"invalid_json:{exc.__class__.__name__}"
    if not isinstance(payload, dict):
        return "invalid_json:not_object"
    if payload.get("ok") is not True:
        return "unhealthy:ok_false"
    service = str(payload.get("service") or "")
    if service != "wecom-room-robot-agentic-rag":
        return "unhealthy:service_mismatch"
    return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check unattended runtime readiness without printing secrets.")
    parser.add_argument("--project-dir", default=os.getcwd())
    parser.add_argument("--env-file", default="")
    parser.add_argument("--health-url", default="http://127.0.0.1:8000/health")
    parser.add_argument("--skip-systemd", action="store_true")
    parser.add_argument("--skip-health", action="store_true")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    env_file = Path(args.env_file).resolve() if args.env_file else project_dir / ".env"
    env_values = _parse_env_file(env_file)

    problems: list[str] = []
    print(f"project_dir={project_dir}")
    print(f"env_file={env_file} exists={env_file.exists()}")

    missing = [key for key in REQUIRED_ENV_KEYS if _is_placeholder(env_values.get(key, ""))]
    if missing:
        problems.append("missing_or_placeholder_env=" + ",".join(missing))
    else:
        print("env_required_keys=ok")

    for relative in ("room_database", "media", "data", "knowledge/kf"):
        path = project_dir / relative
        print(f"path:{relative}=exists:{path.exists()}")

    if not args.skip_systemd:
        for service in SERVICE_NAMES:
            status = _systemctl_is_active(service)
            print(f"systemd:{service}={status}")
            if service.endswith(".service") and status != "active":
                problems.append(f"{service}:{status}")
            if service.endswith(".timer") and status not in {"active", "activating"}:
                problems.append(f"{service}:{status}")

    if not args.skip_health:
        health = _health_check(args.health_url)
        print(f"health={health}")
        if health != "ok":
            problems.append(f"health:{health}")

    if problems:
        print("unattended_check=failed")
        for problem in problems:
            print(f"problem={problem}")
        return 1
    print("unattended_check=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
