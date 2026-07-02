from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.check_unattended_runtime import REQUIRED_ENV_KEYS


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
UNTRACKED_RELEASE_SOURCE_PREFIXES = (
    "AGENTS.md",
    "app/",
    "docs/",
    "infra/",
    "requirements.txt",
    "scripts/",
    "tests/",
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
    health_contract = validate_health_contract()
    release_ready = (
        all(candidate["required_paths"].values())
        and candidate["excluded_runtime_paths_present_in_manifest"] == []
        and rollback["ok"]
        and approval_guard["ok"]
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


def run_release_preflight_graph_pipeline(
    project_dir: Path,
    rehearsal_root: Path,
    *,
    version: str = "",
    fail_fast: bool = True,
    local_tests_command: list[str] | None = None,
    random_guard_command: list[str] | None = None,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    from app.services import release_preflight_graph

    deps = release_preflight_graph.build_local_release_preflight_deps(
        local_tests_command=local_tests_command,
        random_guard_command=random_guard_command,
        timeout_seconds=timeout_seconds,
    )
    state = asyncio.run(
        release_preflight_graph.run_release_preflight_graph(
            deps,
            project_dir=project_dir,
            rehearsal_root=rehearsal_root,
            version=version,
            fail_fast=fail_fast,
            conversation_id=f"release-preflight:{version or 'local'}",
        )
    )
    return {
        "schema_version": "rag_v2_release_preflight_graph_cli.v1",
        "ok": state.get("status") == "passed",
        "status": state.get("status") or "",
        "blocked_stage": state.get("blocked_stage") or "",
        "failures": list(state.get("failures") or []),
        "trace": list(state.get("trace") or []),
        "report": dict(state.get("report") or {}),
        "local_tests": dict(state.get("local_tests") or {}),
        "random_guard": dict(state.get("random_guard") or {}),
        "config_status": dict(state.get("config_status") or {}),
        "release_rehearsal": dict(state.get("release_rehearsal") or {}),
    }


def build_release_manifest(project_dir: Path, *, version: str, git_head: str) -> dict[str, Any]:
    tracked_files = _tracked_files(project_dir)
    untracked_source_files = _untracked_release_source_files(project_dir)
    files = sorted(set(tracked_files + untracked_source_files))
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
        "untracked_release_source_paths_in_manifest": [
            path for path in untracked_source_files if path in release_files
        ],
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
    local_credential_file = project_dir / ".local" / "server-credentials.ps1"
    return {
        "env_file_exists": env_file.exists(),
        "env_file_read": False,
        "env_file_read_policy": "skipped_in_local_rehearsal_to_avoid_reading_secrets",
        "required_env_count": len(REQUIRED_ENV_KEYS),
        "missing_or_placeholder_keys": [],
        "required_env_completeness_checked": False,
        "local_ssh_credential_file_exists": local_credential_file.exists(),
        "secret_values_printed": False,
    }


def inspect_server_ops_approval_guard(project_dir: Path) -> dict[str, Any]:
    script = (project_dir / "scripts" / "server-ops.ps1").read_text(encoding="utf-8")
    approval_call_lines: list[int] = []
    credential_load_lines: list[int] = []
    function_definition_lines: list[int] = []
    for line_no, line in enumerate(script.splitlines(), start=1):
        if _is_require_deploy_approval_definition(line):
            function_definition_lines.append(line_no)
            continue
        if _is_require_deploy_approval_call(line):
            approval_call_lines.append(line_no)
        if _is_credential_file_read(line):
            credential_load_lines.append(line_no)
    approval_call_line = approval_call_lines[0] if approval_call_lines else None
    credential_load_line = credential_load_lines[0] if credential_load_lines else None
    requires_approve_deploy = "APPROVE_DEPLOY" in script and approval_call_line is not None
    approval_guard_before_credential_load = (
        approval_call_line is not None
        and credential_load_line is not None
        and approval_call_line < credential_load_line
    )
    return {
        "ok": requires_approve_deploy and approval_guard_before_credential_load,
        "requires_approve_deploy": requires_approve_deploy,
        "approval_guard_before_credential_load": approval_guard_before_credential_load,
        "approval_guard_line": approval_call_line,
        "credential_load_line": credential_load_line,
        "function_definition_lines": function_definition_lines,
    }


def _is_require_deploy_approval_definition(line: str) -> bool:
    return re.match(r"^\s*function\s+Require-DeployApproval\b", line, flags=re.IGNORECASE) is not None


def _is_require_deploy_approval_call(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    if _is_require_deploy_approval_definition(stripped):
        return False
    return re.match(r"^(?:[&.]\s*)?Require-DeployApproval\b", stripped, flags=re.IGNORECASE) is not None


def _is_credential_file_read(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    return (
        re.search(r"\bGet-Content\b", stripped, flags=re.IGNORECASE) is not None
        and re.search(r"(^|\s)-Path\b\s+\$CredentialFile\b", stripped, flags=re.IGNORECASE) is not None
    )


def validate_health_payload(payload: dict[str, Any]) -> dict[str, Any]:
    service = str(payload.get("service") or "")
    ok = bool(payload.get("ok")) and service == "wecom-room-robot-agentic-rag"
    if payload.get("ok") is not True:
        reason = "ok_false"
    elif service != "wecom-room-robot-agentic-rag":
        reason = "service_mismatch"
    else:
        reason = ""
    return {
        "ok": ok,
        "service": service,
        "reason": reason,
    }


def validate_health_contract() -> dict[str, Any]:
    samples = {
        "healthy": validate_health_payload({"ok": True, "service": "wecom-room-robot-agentic-rag"}),
        "ok_false": validate_health_payload({"ok": False, "service": "wecom-room-robot-agentic-rag"}),
        "service_mismatch": validate_health_payload({"ok": True, "service": "legacy-room-robot"}),
        "missing_service": validate_health_payload({"ok": True}),
    }
    return {
        "schema": "rag_v2_health_contract.v1",
        "ok": (
            samples["healthy"]["ok"] is True
            and samples["ok_false"]["ok"] is False
            and samples["ok_false"]["reason"] == "ok_false"
            and samples["service_mismatch"]["ok"] is False
            and samples["service_mismatch"]["reason"] == "service_mismatch"
            and samples["missing_service"]["ok"] is False
        ),
        "required_service": "wecom-room-robot-agentic-rag",
        "samples": samples,
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


def _untracked_release_source_files(project_dir: Path) -> list[str]:
    output = _git_output(project_dir, ["ls-files", "--others", "--exclude-standard"])
    if not output:
        return []
    paths = [path.replace("\\", "/") for path in output.splitlines() if path.strip()]
    return sorted(
        path
        for path in paths
        if _is_release_source_path(path) and not _is_excluded_release_path(path) and (project_dir / path).is_file()
    )


def _is_release_source_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in UNTRACKED_RELEASE_SOURCE_PREFIXES
    )


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
    parser.add_argument(
        "--graph-preflight",
        action="store_true",
        help="Run the full local StateGraph preflight. This is the default; the flag is kept for old scripts.",
    )
    parser.add_argument(
        "--legacy-rehearsal",
        action="store_true",
        help="Run only the legacy release rehearsal, skipping the StateGraph preflight.",
    )
    parser.add_argument(
        "--no-fail-fast",
        action="store_true",
        help="With the StateGraph preflight, continue later local preflight stages after a failure.",
    )
    parser.add_argument(
        "--preflight-timeout-seconds",
        type=int,
        default=600,
        help="Per-command timeout for StateGraph local test and random QA stages.",
    )
    args = parser.parse_args()
    if args.graph_preflight and args.legacy_rehearsal:
        parser.error("--graph-preflight and --legacy-rehearsal cannot be used together")

    if not args.legacy_rehearsal:
        result = run_release_preflight_graph_pipeline(
            args.project_dir,
            args.rehearsal_root,
            version=args.version,
            fail_fast=not args.no_fail_fast,
            timeout_seconds=args.preflight_timeout_seconds,
        )
    else:
        result = rehearse_release_pipeline(args.project_dir, args.rehearsal_root, version=args.version)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
