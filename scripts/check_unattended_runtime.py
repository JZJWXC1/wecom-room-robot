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

PRODUCTION_MODE_VALUES = {"production", "prod"}


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


CALLBACK_PROBE_URL = "http://127.0.0.1/wecom/kf/callback"
CALLBACK_PROBE_HOST = "114.55.168.97"
CALLBACK_HEALTHY_CODES = {"200", "422"}


def _classify_callback_code(code: str) -> str:
    """归类企微客服回调路由探测的 HTTP 状态码。

    背景：企微 KF 回调 URL 走 http://114.55.168.97/wecom/kf/callback（IP 主机 + 端口 80）。
    若 nginx 的 `listen 80` 块被 Certbot 重写抹掉 /wecom/ 转发，IP:80 /wecom 会落到
    `return 404` → 回调对客失聪（2026-07-04、2026-07-06 两次实证）。
    判据：404 = 路由已断（坏）；200 或 422 = 已路由到应用
    （422 为缺签名参数的裸探测，属正常）。
    """
    text = str(code).strip()
    if not text:
        return "no_code"
    if text == "404":
        return "route_404"
    if text in CALLBACK_HEALTHY_CODES:
        return "ok"
    return f"unexpected:{text}"


def _callback_route_status(url: str, host_header: str) -> str:
    try:
        completed = subprocess.run(
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "-H",
                f"Host: {host_header}",
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return f"error:{exc.__class__.__name__}"
    if completed.returncode != 0:
        return (
            completed.stderr or completed.stdout or f"exit:{completed.returncode}"
        ).strip() or f"exit:{completed.returncode}"
    return _classify_callback_code(completed.stdout)


def _systemd_environment_value(service_name: str, key: str) -> str:
    service_path = Path("/etc/systemd/system") / service_name
    if not service_path.exists():
        return ""
    try:
        lines = service_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    prefix = f"{key}="
    for raw_line in lines:
        line = raw_line.strip()
        if not line.startswith("Environment="):
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        if value.startswith(prefix):
            return value[len(prefix) :].strip().strip('"').strip("'")
    return ""


def _dual_llm_mode(env_values: dict[str, str]) -> str:
    return (
        env_values.get("KF_DUAL_LLM_MODE")
        or os.environ.get("KF_DUAL_LLM_MODE", "")
        or _systemd_environment_value("wecom-room-robot.service", "KF_DUAL_LLM_MODE")
    ).strip().lower()


def _manifest_status(project_dir: Path, env_values: dict[str, str]) -> str:
    configured_root = env_values.get("ROOM_DATABASE_PATH") or os.environ.get("ROOM_DATABASE_PATH") or "room_database"
    root = Path(configured_root)
    if not root.is_absolute():
        root = project_dir / root
    manifest_path = root / "media_manifest.json"
    if not manifest_path.exists():
        return "missing"
    try:
        if str(project_dir) not in sys.path:
            sys.path.insert(0, str(project_dir))
        from app.services.media_manifest import MediaManifest, MediaManifestProductionAdapter

        manifest = MediaManifest.read_json(manifest_path)
    except Exception as exc:
        return f"invalid:{exc.__class__.__name__}"
    if not manifest.items:
        return "empty"
    production = MediaManifestProductionAdapter(manifest, local_root=root)
    evidence = []
    for listing_id in manifest.listing_ids:
        evidence.extend(production.evidence_for_listing(listing_id))
    if not evidence:
        return "no_send_ready_evidence"
    video_count = sum(1 for item in evidence if item.media_type == "video")
    image_count = sum(1 for item in evidence if item.media_type == "image")
    return (
        f"ok:items={len(manifest.items)},send_ready={len(evidence)},"
        f"videos={video_count},images={image_count}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check unattended runtime readiness without printing secrets.")
    parser.add_argument("--project-dir", default=os.getcwd())
    parser.add_argument("--env-file", default="")
    parser.add_argument("--health-url", default="http://127.0.0.1:8000/health")
    parser.add_argument("--skip-systemd", action="store_true")
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--skip-callback", action="store_true")
    parser.add_argument("--callback-url", default=CALLBACK_PROBE_URL)
    parser.add_argument("--callback-host", default=CALLBACK_PROBE_HOST)
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

    dual_llm_mode = _dual_llm_mode(env_values)
    manifest_status = _manifest_status(project_dir, env_values)
    print(f"dual_llm_mode={dual_llm_mode or 'unset'}")
    print(f"media_manifest={manifest_status}")
    if dual_llm_mode in PRODUCTION_MODE_VALUES and not manifest_status.startswith("ok:"):
        problems.append(f"media_manifest:{manifest_status}")

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

    if not args.skip_callback:
        callback_status = _callback_route_status(args.callback_url, args.callback_host)
        print(f"callback_route={callback_status}")
        if callback_status != "ok":
            problems.append(f"callback_route:{callback_status}")

    if problems:
        print("unattended_check=failed")
        for problem in problems:
            print(f"problem={problem}")
        return 1
    print("unattended_check=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
