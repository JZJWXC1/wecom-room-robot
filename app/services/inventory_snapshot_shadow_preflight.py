from __future__ import annotations

from dataclasses import dataclass
import ast
import glob
import importlib
import json
import os
from pathlib import Path
import shutil
from typing import Any

from app.config import settings
from app.services.inventory_snapshot_models import sanitize_for_log
from app.services.inventory_snapshot_shadow import (
    InventorySnapshotShadowConfigError,
    parse_inventory_snapshot_mode,
)


PREFLIGHT_SCHEMA_VERSION = "inventory_snapshot_shadow_preflight.v1"


@dataclass(frozen=True)
class ShadowPreflightOptions:
    mode: str | None = None
    shadow_root: Path | None = None
    production_snapshot_root: Path | None = None
    inventory_cache_path: Path | None = None
    rewrite_inventory_index_path: Path | None = None
    inventory_image_glob: str | None = None
    room_database_path: Path | None = None
    min_free_bytes: int = 256 * 1024 * 1024
    project_root: Path | None = None


def run_shadow_preflight(options: ShadowPreflightOptions | None = None) -> dict[str, Any]:
    options = options or ShadowPreflightOptions()
    project_root = Path(options.project_root or Path.cwd())
    shadow_root = Path(options.shadow_root or settings.inventory_snapshot_shadow_root)
    production_root = Path(options.production_snapshot_root or Path("data/inventory_snapshots"))
    inventory_cache_path = Path(options.inventory_cache_path or settings.inventory_cache_path)
    rewrite_index_path = Path(options.rewrite_inventory_index_path or settings.rewrite_inventory_index_path)
    image_glob = options.inventory_image_glob or settings.inventory_image_glob
    room_database_path = Path(options.room_database_path or settings.room_database_path)
    mode_value = settings.inventory_snapshot_mode if options.mode is None else options.mode

    checks: list[dict[str, Any]] = []
    _check_mode(checks, mode_value)
    _check_config_values(checks)
    _check_imports(checks)
    _check_directory_creatable(checks, shadow_root)
    _check_disk_space(checks, shadow_root, options.min_free_bytes)
    _check_old_production_files(
        checks,
        project_root=project_root,
        inventory_cache_path=inventory_cache_path,
        rewrite_index_path=rewrite_index_path,
        image_glob=image_glob,
        room_database_path=room_database_path,
    )
    _check_path_isolation(checks, shadow_root, production_root)
    _check_pointer_not_production_read(checks, project_root)

    payload = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "ok": not any(item["severity"] == "error" for item in checks),
        "mode": str(mode_value or ""),
        "shadow_root_label": _path_label(shadow_root),
        "production_snapshot_root_label": _path_label(production_root),
        "checks": checks,
        "network_access": "not_attempted",
        "writes": "none",
    }
    return sanitize_for_log(payload)


def format_shadow_preflight(payload: dict[str, Any]) -> str:
    lines = [
        "InventorySnapshot Shadow 发布前检查",
        f"ok: {str(bool(payload.get('ok'))).lower()}",
        f"mode: {payload.get('mode') or ''}",
        f"shadow_root: {payload.get('shadow_root_label') or ''}",
        f"production_snapshot_root: {payload.get('production_snapshot_root_label') or ''}",
    ]
    for check in payload.get("checks") or []:
        lines.append(
            f"- {check.get('severity')}: {check.get('name')} "
            f"{check.get('status')} {check.get('message') or ''}".rstrip()
        )
    return "\n".join(lines) + "\n"


def _check_mode(checks: list[dict[str, Any]], value: str | None) -> None:
    try:
        parsed = parse_inventory_snapshot_mode(value)
    except InventorySnapshotShadowConfigError as exc:
        checks.append(_check("mode", "error", "invalid", str(exc)))
        return
    if parsed.value == "shadow":
        checks.append(_check("mode", "ok", "shadow", "Shadow 模式仅用于旁路构建，不切生产读取。"))
    elif parsed.value == "disabled":
        checks.append(_check("mode", "ok", "disabled", "未配置时保持 disabled。"))
    else:
        checks.append(_check("mode", "error", "invalid", "primary 模式不允许。"))


def _check_config_values(checks: list[dict[str, Any]]) -> None:
    values = {
        "inventory_snapshot_shadow_stale_seconds": settings.inventory_snapshot_shadow_stale_seconds,
        "inventory_snapshot_shadow_required_passes": settings.inventory_snapshot_shadow_required_passes,
        "inventory_snapshot_shadow_timeout_seconds": settings.inventory_snapshot_shadow_timeout_seconds,
        "inventory_snapshot_shadow_report_retention": settings.inventory_snapshot_shadow_report_retention,
    }
    invalid = [
        name
        for name, value in values.items()
        if float(value) <= 0
    ]
    if invalid:
        checks.append(_check("config_values", "error", "invalid", ",".join(invalid)))
    else:
        checks.append(_check("config_values", "ok", "valid", "Shadow 配置值合法。"))


def _check_imports(checks: list[dict[str, Any]]) -> None:
    modules = [
        "app.services.inventory_snapshot_builder",
        "app.services.inventory_snapshot_shadow",
        "app.services.inventory_snapshot_reconciliation",
        "app.services.inventory_snapshot_store",
    ]
    try:
        for module in modules:
            importlib.import_module(module)
    except Exception as exc:
        checks.append(_check("snapshot_imports", "error", "failed", str(exc)))
        return
    checks.append(_check("snapshot_imports", "ok", "importable", "Snapshot 模块可导入。"))


def _check_directory_creatable(checks: list[dict[str, Any]], shadow_root: Path) -> None:
    parent = _nearest_existing_parent(shadow_root)
    if parent is None:
        checks.append(_check("shadow_directory_creatable", "error", "missing_parent", "找不到可检查的父目录。"))
        return
    writable = os.access(parent, os.W_OK)
    checks.append(
        _check(
            "shadow_directory_creatable",
            "ok" if writable else "error",
            "parent_writable" if writable else "parent_not_writable",
            "只读检查父目录写权限，不创建目录。",
        )
    )


def _check_disk_space(checks: list[dict[str, Any]], shadow_root: Path, min_free_bytes: int) -> None:
    parent = _nearest_existing_parent(shadow_root)
    if parent is None:
        checks.append(_check("disk_space", "error", "missing_parent", "无法检查磁盘空间。"))
        return
    usage = shutil.disk_usage(parent)
    severity = "ok" if usage.free >= min_free_bytes else "error"
    checks.append(
        _check(
            "disk_space",
            severity,
            "sufficient" if severity == "ok" else "insufficient",
            f"free_mb={usage.free // (1024 * 1024)}",
        )
    )


def _check_old_production_files(
    checks: list[dict[str, Any]],
    *,
    project_root: Path,
    inventory_cache_path: Path,
    rewrite_index_path: Path,
    image_glob: str,
    room_database_path: Path,
) -> None:
    cache = _resolve_under_project(project_root, inventory_cache_path)
    rewrite = _resolve_under_project(project_root, rewrite_index_path)
    room_db = _resolve_under_project(project_root, room_database_path)
    image_pattern = str(_resolve_under_project(project_root, Path(image_glob)))
    image_matches = [Path(item) for item in glob.glob(image_pattern)]
    checks.append(_file_check("legacy_inventory_cache", cache))
    checks.append(_file_check("legacy_rewrite_index", rewrite))
    checks.append(_dir_check("legacy_room_database", room_db))
    checks.append(
        _check(
            "legacy_inventory_png",
            "ok" if image_matches else "error",
            "present" if image_matches else "missing",
            f"match_count={len(image_matches)}",
        )
    )


def _check_path_isolation(checks: list[dict[str, Any]], shadow_root: Path, production_root: Path) -> None:
    shadow = _safe_resolve(shadow_root)
    production = _safe_resolve(production_root)
    overlap = shadow == production or _is_relative_to(shadow, production) or _is_relative_to(production, shadow)
    checks.append(
        _check(
            "shadow_production_path_isolation",
            "error" if overlap else "ok",
            "overlap" if overlap else "isolated",
            "Shadow 根目录必须与正式 Snapshot 根目录隔离。",
        )
    )


def _check_pointer_not_production_read(checks: list[dict[str, Any]], project_root: Path) -> None:
    main_path = project_root / "app" / "main.py"
    try:
        source = main_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception as exc:
        checks.append(_check("production_pointer_reader", "error", "unreadable", str(exc)))
        return
    imports_snapshot_reader = "inventory_snapshot_reader" in source or "SnapshotReader" in source
    shadow_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_inventory_snapshot_shadow"
    ]
    if imports_snapshot_reader:
        checks.append(_check("production_pointer_reader", "error", "snapshot_reader_present", "app/main.py 已出现 Snapshot Reader。"))
        return
    if len(shadow_calls) != 1:
        checks.append(_check("production_pointer_reader", "error", "unexpected_shadow_call_count", f"count={len(shadow_calls)}"))
        return
    checks.append(_check("production_pointer_reader", "ok", "legacy_reader_unchanged", "生产读取仍未接入 Snapshot Reader。"))


def _nearest_existing_parent(path: Path) -> Path | None:
    current = _safe_resolve(path)
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return current if current.is_dir() else current.parent


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _resolve_under_project(project_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else project_root / path


def _file_check(name: str, path: Path) -> dict[str, Any]:
    return _check(
        name,
        "ok" if path.is_file() else "error",
        "present" if path.is_file() else "missing",
        "旧生产读取文件必须保留。",
    )


def _dir_check(name: str, path: Path) -> dict[str, Any]:
    return _check(
        name,
        "ok" if path.is_dir() else "error",
        "present" if path.is_dir() else "missing",
        "旧生产目录必须保留。",
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _path_label(path: Path) -> str:
    text = path.as_posix()
    if path.is_absolute():
        return f"<absolute>/{path.name}"
    return text


def _check(name: str, severity: str, status: str, message: str) -> dict[str, Any]:
    return {
        "name": name,
        "severity": severity,
        "status": status,
        "message": str(sanitize_for_log(message)),
    }
