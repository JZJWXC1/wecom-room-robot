from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.check_unattended_runtime import REQUIRED_ENV_KEYS, _is_placeholder, _parse_env_file


RELEASE_REHEARSAL_SCHEMA_VERSION = "rag_v2_release_rehearsal.v1"
REQUIRED_RELEASE_PATHS = (
    "app/main.py",
    "app/services",
    "requirements.txt",
    "scripts/rag-v2-test-gates.ps1",
    "scripts/server-ops.ps1",
    "scripts/check_unattended_runtime.py",
    "infra/systemd/wecom-room-robot-feishu-region-sync.service",
    "infra/systemd/wecom-room-robot-feishu-region-sync.timer",
    "infra/systemd/wecom-room-robot-rag-cache-sync.service",
    "infra/systemd/wecom-room-robot-rag-cache-sync.timer",
)
EXCLUDED_RELEASE_PREFIXES = (
    ".git/",
    ".local/",
    ".env",
    "data/",
    "media/",
    "room_database/",
    "server_snapshots/",
    "qa_artifacts/",
)


def rehearse_release_pipeline(project_dir: Path, rehearsal_root: Path, *, version: str = "") -> dict[str, Any]:
    project_dir = project_dir.resolve()
    rehearsal_root = rehearsal_root.resolve()
    release_root = rehearsal_root / "release"
    releases_dir = release_root / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)

    head = _git_output(project_dir, ["rev-parse", "--short", "HEAD"]) or "unknown"
    version = version or f"local-{head}-{int(time.time())}"
    previous_version = "previous-good"

    previous_manifest = _write_release_manifest(
        releases_dir / previous_version,
        {
            "schema_version": RELEASE_REHEARSAL_SCHEMA_VERSION,
            "version": previous_version,
            "git_head": "previous",
            "file_count": 0,
            "files_sha256": _sha256_text("previous"),
            "required_paths": {path: True for path in REQUIRED_RELEASE_PATHS},
        },
    )
    _write_current_pointer(release_root, previous_version, previous_manifest["manifest_sha256"])

    candidate = build_release_manifest(project_dir, version=version, git_head=head)
    candidate_manifest = _write_release_manifest(releases_dir / version, candidate)
    _write_current_pointer(release_root, version, candidate_manifest["manifest_sha256"])
    rollback = rehearse_rollback(release_root, to_version=previous_version)

    env_summary = build_unattended_env_summary(project_dir)
    approval_guard = inspect_server_ops_approval_guard(project_dir)
    health_contract = validate_health_payload({"ok": True, "service": "wecom-room-robot-agentic-rag"})
    release_ready = (
        all(candidate["required_paths"].values())
        and candidate["excluded_runtime_paths_present_in_manifest"] == []
        and rollback["ok"]
        and approval_guard["requires_approve_deploy"]
        and health_contract["ok"]
    )
    result = {
        "schema_version": RELEASE_REHEARSAL_SCHEMA_VERSION,
        "ok": release_ready,
        "version": version,
        "git_head": head,
        "release_root": _safe_path(release_root),
        "current_pointer": json.loads((release_root / "current_release.json").read_text(encoding="utf-8")),
        "candidate_manifest": candidate_manifest,
        "rollback": rollback,
        "unattended_env_summary": env_summary,
        "server_ops_approval_guard": approval_guard,
        "health_contract": health_contract,
    }
    (rehearsal_root / "release_rehearsal_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


def build_release_manifest(project_dir: Path, *, version: str, git_head: str) -> dict[str, Any]:
    files = _tracked_files(project_dir)
    release_files = [path for path in files if not _is_excluded_release_path(path)]
    required = {path: (project_dir / path).exists() for path in REQUIRED_RELEASE_PATHS}
    excluded_present = [path for path in release_files if _is_excluded_release_path(path)]
    return {
        "schema_version": RELEASE_REHEARSAL_SCHEMA_VERSION,
        "version": version,
        "git_head": git_head,
        "file_count": len(release_files),
        "files_sha256": _sha256_text("\n".join(release_files)),
        "required_paths": required,
        "excluded_runtime_paths_present_in_manifest": excluded_present,
    }


def rehearse_rollback(release_root: Path, *, to_version: str) -> dict[str, Any]:
    pointer_path = release_root / "current_release.json"
    before = json.loads(pointer_path.read_text(encoding="utf-8"))
    target_manifest_path = release_root / "releases" / to_version / "manifest.json"
    target_manifest = json.loads(target_manifest_path.read_text(encoding="utf-8"))
    _write_current_pointer(release_root, to_version, target_manifest["manifest_sha256"])
    after = json.loads(pointer_path.read_text(encoding="utf-8"))
    return {
        "ok": after["version"] == to_version and before["version"] != after["version"],
        "from_version": before["version"],
        "to_version": after["version"],
        "current_pointer_sha256": after["manifest_sha256"],
    }


def build_unattended_env_summary(project_dir: Path) -> dict[str, Any]:
    env_file = project_dir / ".env"
    values = _parse_env_file(env_file)
    missing = [key for key in REQUIRED_ENV_KEYS if _is_placeholder(values.get(key, ""))]
    local_credential_file = project_dir / ".local" / "server-credentials.ps1"
    return {
        "env_file_exists": env_file.exists(),
        "required_env_count": len(REQUIRED_ENV_KEYS),
        "missing_or_placeholder_keys": missing,
        "local_ssh_credential_file_exists": local_credential_file.exists(),
        "secret_values_printed": False,
    }


def inspect_server_ops_approval_guard(project_dir: Path) -> dict[str, Any]:
    script = (project_dir / "scripts" / "server-ops.ps1").read_text(encoding="utf-8")
    credential_index = script.find("$CredentialFile")
    guard_index = script.find("Require-DeployApproval")
    return {
        "requires_approve_deploy": "APPROVE_DEPLOY" in script and "Require-DeployApproval" in script,
        "approval_guard_before_credential_load": guard_index >= 0 and credential_index >= 0 and guard_index < credential_index,
    }


def validate_health_payload(payload: dict[str, Any]) -> dict[str, Any]:
    service = str(payload.get("service") or "")
    return {
        "ok": bool(payload.get("ok")) and service == "wecom-room-robot-agentic-rag",
        "service": service,
    }


def _write_release_manifest(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    path.mkdir(parents=True, exist_ok=True)
    manifest_path = path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    manifest_sha256 = _sha256_bytes(manifest_path.read_bytes())
    enriched = dict(manifest)
    enriched["manifest_sha256"] = manifest_sha256
    manifest_path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return enriched


def _write_current_pointer(release_root: Path, version: str, manifest_sha256: str) -> None:
    pointer = {
        "schema_version": RELEASE_REHEARSAL_SCHEMA_VERSION,
        "version": version,
        "manifest_sha256": manifest_sha256,
        "updated_at": int(time.time()),
    }
    tmp_path = release_root / "current_release.json.tmp"
    tmp_path.write_text(json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(release_root / "current_release.json")


def _tracked_files(project_dir: Path) -> list[str]:
    output = _git_output(project_dir, ["ls-files"])
    if not output:
        return []
    return sorted(path.replace("\\", "/") for path in output.splitlines() if path.strip())


def _is_excluded_release_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in EXCLUDED_RELEASE_PREFIXES)


def _git_output(project_dir: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=project_dir,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _safe_path(path: Path) -> str:
    return str(path)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local release/current and rollback rehearsal without server access.")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--rehearsal-root", type=Path, required=True)
    parser.add_argument("--version", default="")
    args = parser.parse_args()

    result = rehearse_release_pipeline(args.project_dir, args.rehearsal_root, version=args.version)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
