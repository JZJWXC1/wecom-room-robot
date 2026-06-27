from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.services.inventory_snapshot_models import (
    CURRENT_POINTER_SCHEMA_VERSION,
    INVENTORY_SNAPSHOT_SCHEMA_VERSION,
    InventorySnapshot,
    InventorySnapshotManifest,
    SnapshotValidationResult,
    generate_listing_id,
    is_safe_listing_id,
    is_safe_relative_artifact_path,
    is_safe_snapshot_id,
)


REQUIRED_MANIFEST_FILES = {
    "manifest": "manifest.json",
    "inventory_json": "inventory.json",
    "inventory_csv": "inventory.csv",
    "rewrite_inventory_index": "rewrite_inventory_index.json",
    "sync_report": "sync_report.json",
}
PUBLIC_SECRET_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("public_payload_contains_password", re.compile(r"(?<!\d)\d{3,8}#(?!\d)")),
    ("public_payload_contains_secret_canary", re.compile(r"TEST_SECRET_[A-Za-z0-9_#-]+")),
    ("public_payload_contains_phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    (
        "public_payload_contains_password_context",
        re.compile(r"(?:看房方式|看房|门锁|门禁|钥匙|密码)[^0-9A-Za-z#]{0,12}[A-Za-z0-9][A-Za-z0-9_#-]{2,31}"),
    ),
)
PUBLIC_FORBIDDEN_KEYS = {
    "viewing",
    "viewing_text",
    "raw_viewing_text",
    "password",
    "password_text",
    "token",
    "secret",
    "phone",
    "mobile",
    "private_link",
    "看房方式密码",
    "看房密码",
    "密码",
}
PUBLIC_ALLOWED_KEYS = {
    "has_password",
    "password_available",
    "viewing_mode",
    "viewing_summary",
    "viewing_secret_ref",
    "availability_summary",
    "availability_status",
}
MACHINE_TEXT_PATH_SUFFIXES = (
    ".schema_version",
    ".snapshot_id",
    ".source_hash",
    ".listing_id",
    ".listing_key",
    ".normalized_community",
    ".normalized_room_no",
    ".viewing_secret_ref",
    ".sha256",
    ".path",
    ".source_record_id",
    ".source_record_ids",
    ".generator_version",
)


class SnapshotValidator:
    """Validate snapshot artifacts before publish and before read."""

    def validate_snapshot(self, snapshot: InventorySnapshot) -> SnapshotValidationResult:
        """Validate an in-memory snapshot model and public payload boundary."""
        result = SnapshotValidationResult()
        self._validate_snapshot_headers(snapshot, result)
        self._validate_manifest(snapshot, result)
        self._validate_listings(snapshot, result)
        self._validate_rewrite_index(snapshot.rewrite_index, result)
        self._validate_rewrite_index_headers(snapshot, result)
        self._validate_public_payload(snapshot.inventory_payload(redact_sensitive=True), "inventory", result)
        self._validate_utf8_serializable(snapshot.inventory_payload(redact_sensitive=True), "inventory", result)
        self._validate_utf8_serializable(snapshot.rewrite_index, "rewrite_inventory_index", result)
        return result

    def validate_directory(self, snapshot_dir: Path) -> SnapshotValidationResult:
        """Validate a snapshot directory, including public and private integrity."""
        result = SnapshotValidationResult()
        manifest_path = snapshot_dir / "manifest.json"
        inventory_path = snapshot_dir / "inventory.json"
        rewrite_path = snapshot_dir / "rewrite_inventory_index.json"
        if not manifest_path.exists():
            result.add("error", "missing_manifest", "快照目录缺少 manifest.json。", path="manifest.json")
            return result
        try:
            manifest_data = _read_json(manifest_path)
            manifest = InventorySnapshotManifest.from_dict(manifest_data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            result.add("error", "invalid_manifest_json", "manifest.json 不是有效 JSON。", path="manifest.json", context={"error": str(exc)})
            return result

        self._validate_manifest_files(snapshot_dir, manifest, result)
        self._validate_private_manifest_files(snapshot_dir, result)
        if not inventory_path.exists() or not rewrite_path.exists():
            return result
        try:
            inventory_data = _read_json(inventory_path)
            rewrite_index = _read_json(rewrite_path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            result.add("error", "invalid_snapshot_json", "快照 JSON 文件不可读。", context={"error": str(exc)})
            return result

        try:
            from app.services.inventory_snapshot_models import InventorySnapshot

            snapshot = InventorySnapshot.from_inventory_payload(
                inventory_data,
                manifest=manifest,
                rewrite_index=rewrite_index,
            )
        except (TypeError, ValueError) as exc:
            result.add("error", "invalid_inventory_payload", "inventory.json 结构不可解析。", path="inventory.json", context={"error": str(exc)})
            return result
        result.extend(self.validate_snapshot(snapshot))
        return result

    def validate_pointer(self, pointer_data: dict[str, Any], root: Path) -> SnapshotValidationResult:
        """Validate current_snapshot.json without guessing a latest directory."""
        result = SnapshotValidationResult()
        if pointer_data.get("schema_version") != CURRENT_POINTER_SCHEMA_VERSION:
            result.add("error", "invalid_pointer_schema", "current_snapshot.json schema_version 不正确。", path="current_snapshot.json")
        snapshot_id = str(pointer_data.get("snapshot_id") or "")
        snapshot_path = str(pointer_data.get("snapshot_path") or "")
        if not snapshot_id:
            result.add("error", "missing_pointer_snapshot_id", "current pointer 缺少 snapshot_id。", path="current_snapshot.json")
        elif not is_safe_snapshot_id(snapshot_id):
            result.add("error", "invalid_pointer_snapshot_id", "current pointer snapshot_id 含非法字符。", path="current_snapshot.json")
        if not snapshot_path:
            result.add("error", "missing_pointer_snapshot_path", "current pointer 缺少 snapshot_path。", path="current_snapshot.json")
            return result
        if not is_safe_relative_artifact_path(snapshot_path):
            result.add("error", "pointer_path_unsafe", "current pointer snapshot_path 不是安全相对路径。", path="current_snapshot.json")
            return result
        expected_path = f"snapshots/{snapshot_id}" if snapshot_id else ""
        if expected_path and snapshot_path != expected_path:
            result.add("error", "pointer_path_snapshot_id_mismatch", "current pointer snapshot_path 与 snapshot_id 不一致。", path="current_snapshot.json")
            return result
        target = (root / Path(snapshot_path)).resolve()
        root_resolved = root.resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError:
            result.add("error", "pointer_path_outside_root", "current pointer 指向 snapshot root 外部路径。", path="current_snapshot.json")
            return result
        if not target.exists():
            result.add("error", "pointer_snapshot_missing", "current pointer 指向的 snapshot 目录不存在。", path=snapshot_path)
            return result
        result.extend(self.validate_directory(target))
        return result

    def _validate_snapshot_headers(self, snapshot: InventorySnapshot, result: SnapshotValidationResult) -> None:
        if snapshot.schema_version != INVENTORY_SNAPSHOT_SCHEMA_VERSION:
            result.add("error", "invalid_schema_version", "snapshot schema_version 不正确。", path="inventory.schema_version")
        if not snapshot.snapshot_id:
            result.add("error", "missing_snapshot_id", "snapshot_id 不能为空。", path="inventory.snapshot_id")
        elif not is_safe_snapshot_id(snapshot.snapshot_id):
            result.add("error", "invalid_snapshot_id", "snapshot_id 含非法字符或不符合 v1 格式。", path="inventory.snapshot_id")
        if not snapshot.source_hash or not re.fullmatch(r"[0-9a-f]{64}", snapshot.source_hash):
            result.add("error", "invalid_source_hash", "source_hash 必须是 SHA-256 hex。", path="inventory.source_hash")
        if snapshot.source_hash and snapshot.snapshot_id and snapshot.source_hash[:12] not in snapshot.snapshot_id:
            result.add("error", "snapshot_id_source_hash_mismatch", "snapshot_id 必须包含 source_hash 前 12 位。", path="inventory.snapshot_id")

    def _validate_manifest(self, snapshot: InventorySnapshot, result: SnapshotValidationResult) -> None:
        manifest = snapshot.manifest
        if manifest.schema_version != INVENTORY_SNAPSHOT_SCHEMA_VERSION:
            result.add("error", "invalid_manifest_schema", "manifest schema_version 不正确。", path="manifest.schema_version")
        if manifest.snapshot_id != snapshot.snapshot_id:
            result.add("error", "manifest_snapshot_id_mismatch", "manifest.snapshot_id 与 inventory 不一致。", path="manifest.snapshot_id")
        if manifest.source_hash != snapshot.source_hash:
            result.add("error", "manifest_source_hash_mismatch", "manifest.source_hash 与 inventory 不一致。", path="manifest.source_hash")
        if manifest.listing_count != len(snapshot.listings):
            result.add("error", "manifest_listing_count_mismatch", "manifest listing_count 与 inventory listings 不一致。", path="manifest.listing_count")
        if manifest.valid_listing_count != len(snapshot.listings):
            result.add("error", "manifest_valid_listing_count_mismatch", "manifest valid_listing_count 与 listings 不一致。", path="manifest.valid_listing_count")
        for logical_name in REQUIRED_MANIFEST_FILES:
            if logical_name not in manifest.files:
                result.add("error", "manifest_file_missing", "manifest 缺少必要文件声明。", path=f"manifest.files.{logical_name}")
        for logical_name, entry in manifest.files.items():
            relative_path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
            if logical_name.startswith("private") or relative_path.startswith("private/"):
                result.add("error", "public_manifest_private_file", "公共 manifest 不应声明 private 文件。", path=f"manifest.files.{logical_name}")

    def _validate_listings(self, snapshot: InventorySnapshot, result: SnapshotValidationResult) -> None:
        seen_listing_ids: set[str] = set()
        seen_keys: dict[str, str] = {}
        for index, listing in enumerate(snapshot.listings):
            path = f"listings[{index}]"
            if not listing.listing_id:
                result.add("error", "missing_listing_id", "房源缺少 listing_id。", path=path)
            elif not is_safe_listing_id(listing.listing_id):
                result.add("error", "invalid_listing_id", "listing_id 含非法字符或不符合 v1 格式。", path=f"{path}.listing_id")
            if listing.listing_id in seen_listing_ids:
                result.add("error", "duplicate_listing_id", "listing_id 不唯一。", path=f"{path}.listing_id", context={"listing_id": listing.listing_id})
            seen_listing_ids.add(listing.listing_id)
            expected_listing_id = generate_listing_id(listing.community, listing.room_no)
            if listing.listing_id and listing.listing_id != expected_listing_id:
                result.add("error", "listing_id_integrity_failed", "listing_id 与小区/房号稳定规则不一致。", path=f"{path}.listing_id")
            if not listing.community.strip():
                result.add("error", "missing_community", "标准小区不能为空。", path=f"{path}.community")
            if not listing.room_no.strip():
                result.add("error", "missing_room_no", "标准房号不能为空。", path=f"{path}.room_no")
            if not isinstance(listing.room_no, str):
                result.add("error", "room_no_not_string", "房号必须保持字符串。", path=f"{path}.room_no")
            self._validate_rent(listing.rent_monthly_pay1, f"{path}.rent_monthly_pay1", result)
            self._validate_rent(listing.rent_monthly_pay2, f"{path}.rent_monthly_pay2", result)
            key = f"{listing.normalized_community}\0{listing.normalized_room_no}"
            if key in seen_keys:
                result.add(
                    "error",
                    "duplicate_listing_key",
                    "同一标准小区和房号重复出现。",
                    path=path,
                    context={"first_listing_id": seen_keys[key], "listing_id": listing.listing_id},
                )
            seen_keys[key] = listing.listing_id
            if listing.viewing_secret_ref and listing.viewing_secret_ref != f"private/viewing_secrets.json#{listing.listing_id}":
                result.add("error", "invalid_viewing_secret_ref", "viewing_secret_ref 必须指向同快照 private 文件中的当前 listing_id。", path=f"{path}.viewing_secret_ref")
            self._validate_public_payload(listing.raw_fields, f"{path}.raw_fields", result)

    def _validate_rent(self, value: Any, path: str, result: SnapshotValidationResult) -> None:
        if value is None:
            return
        if not isinstance(value, int):
            result.add("error", "rent_not_integer_or_null", "月租必须是整数或 null。", path=path)
            return
        if value < 100 or value > 100000:
            result.add("error", "rent_out_of_range", "月租数值超出允许范围。", path=path, context={"value": value})

    def _validate_rewrite_index(self, rewrite_index: dict[str, Any], result: SnapshotValidationResult) -> None:
        if not isinstance(rewrite_index, dict):
            result.add("error", "rewrite_index_not_object", "rewrite index 必须是对象。", path="rewrite_inventory_index")
            return
        self._validate_public_payload(rewrite_index, "rewrite_inventory_index", result)

    def _validate_rewrite_index_headers(self, snapshot: InventorySnapshot, result: SnapshotValidationResult) -> None:
        rewrite_index = snapshot.rewrite_index
        if not isinstance(rewrite_index, dict):
            return
        source = str(rewrite_index.get("source") or "")
        snapshot_id = str(rewrite_index.get("snapshot_id") or "")
        source_hash = str(rewrite_index.get("source_hash") or "")
        if source != "inventory_snapshot":
            result.add("error", "rewrite_index_source_not_snapshot", "snapshot rewrite index 必须声明 inventory_snapshot source。", path="rewrite_inventory_index.source")
        if snapshot_id != snapshot.snapshot_id:
            result.add("error", "rewrite_index_snapshot_id_mismatch", "rewrite index snapshot_id 与 inventory 不一致。", path="rewrite_inventory_index.snapshot_id")
        if source_hash != snapshot.source_hash:
            result.add("error", "rewrite_index_source_hash_mismatch", "rewrite index source_hash 与 inventory 不一致。", path="rewrite_inventory_index.source_hash")

    def _validate_public_payload(self, value: Any, path: str, result: SnapshotValidationResult) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                lowered = key_text.lower()
                child_path = f"{path}.{key_text}" if path else key_text
                if key_text not in PUBLIC_ALLOWED_KEYS and lowered not in PUBLIC_ALLOWED_KEYS:
                    if key_text in PUBLIC_FORBIDDEN_KEYS or lowered in PUBLIC_FORBIDDEN_KEYS:
                        result.add("error", "public_payload_sensitive_key", "公共快照产物包含敏感字段名。", path=child_path)
                    elif "password" in lowered and lowered != "has_password":
                        result.add("error", "public_payload_password_key", "公共快照产物包含 password 字段。", path=child_path)
                    elif "secret" in lowered or "token" in lowered:
                        result.add("error", "public_payload_secret_key", "公共快照产物包含 secret/token 字段。", path=child_path)
                self._validate_public_payload(item, child_path, result)
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                self._validate_public_payload(item, f"{path}[{index}]", result)
            return
        if isinstance(value, str) and not _is_machine_text_path(path):
            for code, pattern in PUBLIC_SECRET_TEXT_PATTERNS:
                if pattern.search(value):
                    result.add("error", code, "公共快照产物包含疑似敏感文本。", path=path)

    def _validate_utf8_serializable(self, value: Any, path: str, result: SnapshotValidationResult) -> None:
        try:
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        except (TypeError, UnicodeEncodeError) as exc:
            result.add("error", "not_utf8_json_serializable", "快照产物无法 UTF-8 JSON 序列化。", path=path, context={"error": str(exc)})

    def _validate_manifest_files(
        self,
        snapshot_dir: Path,
        manifest: InventorySnapshotManifest,
        result: SnapshotValidationResult,
    ) -> None:
        for logical_name, expected_path in REQUIRED_MANIFEST_FILES.items():
            entry = manifest.files.get(logical_name)
            if not isinstance(entry, dict):
                result.add("error", "manifest_file_entry_invalid", "manifest 文件声明必须是对象。", path=f"manifest.files.{logical_name}")
                continue
            relative_path = str(entry.get("path") or "")
            if relative_path != expected_path:
                result.add("error", "manifest_file_path_mismatch", "manifest 文件路径声明不符合约定。", path=f"manifest.files.{logical_name}.path")
        for logical_name, entry in manifest.files.items():
            if not isinstance(entry, dict):
                result.add("error", "manifest_file_entry_invalid", "manifest 文件声明必须是对象。", path=f"manifest.files.{logical_name}")
                continue
            relative_path = str(entry.get("path") or "")
            if not relative_path:
                result.add("error", "manifest_file_path_missing", "manifest 文件声明缺少 path。", path=f"manifest.files.{logical_name}.path")
                continue
            allow_directory = relative_path.endswith("/") or entry.get("status") == "reserved"
            if not is_safe_relative_artifact_path(relative_path, allow_directory=allow_directory):
                result.add("error", "manifest_file_path_unsafe", "manifest 文件路径不是安全相对路径。", path=f"manifest.files.{logical_name}.path")
                continue
            if relative_path.endswith("/") or entry.get("status") == "reserved":
                continue
            target = snapshot_dir / relative_path
            if not target.exists():
                result.add("error", "snapshot_file_missing", "manifest 声明的文件不存在。", path=relative_path)
                continue
            declared_hash = str(entry.get("sha256") or "")
            if declared_hash and declared_hash != _file_sha256(target):
                result.add("error", "snapshot_file_hash_mismatch", "manifest 文件 hash 与实际内容不一致。", path=relative_path)
            if "bytes" in entry:
                try:
                    declared_bytes = int(entry.get("bytes"))
                except (TypeError, ValueError):
                    result.add("error", "snapshot_file_size_invalid", "manifest 文件大小声明不是整数。", path=relative_path)
                    continue
                if declared_bytes != target.stat().st_size:
                    result.add("error", "snapshot_file_size_mismatch", "manifest 文件大小与实际内容不一致。", path=relative_path)

    def _validate_private_manifest_files(self, snapshot_dir: Path, result: SnapshotValidationResult) -> None:
        private_dir = snapshot_dir / "private"
        private_manifest_path = private_dir / "manifest.json"
        private_secrets_path = private_dir / "viewing_secrets.json"
        if not private_dir.exists() and not private_manifest_path.exists() and not private_secrets_path.exists():
            return
        if not private_manifest_path.exists():
            result.add("error", "private_manifest_missing", "private 目录缺少 manifest.json。", path="private/manifest.json")
            return
        try:
            private_manifest = _read_json(private_manifest_path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            result.add("error", "invalid_private_manifest_json", "private manifest 不是有效 JSON。", path="private/manifest.json", context={"error": str(exc)})
            return
        files = private_manifest.get("files")
        if not isinstance(files, dict):
            result.add("error", "private_manifest_files_invalid", "private manifest files 必须是对象。", path="private/manifest.json")
            return
        entry = files.get("viewing_secrets")
        if not isinstance(entry, dict):
            result.add("error", "private_manifest_viewing_secrets_missing", "private manifest 缺少 viewing_secrets 声明。", path="private/manifest.json")
            return
        relative_path = str(entry.get("path") or "")
        if relative_path != "viewing_secrets.json":
            result.add("error", "private_manifest_file_path_mismatch", "private manifest 文件路径声明不符合约定。", path="private/manifest.json")
            return
        target = private_dir / relative_path
        if not target.exists():
            result.add("error", "private_snapshot_file_missing", "private manifest 声明的文件不存在。", path="private/viewing_secrets.json")
            return
        declared_hash = str(entry.get("sha256") or "")
        if not declared_hash or declared_hash != _file_sha256(target):
            result.add("error", "private_snapshot_file_hash_mismatch", "private 文件 hash 与实际内容不一致。", path="private/viewing_secrets.json")
        try:
            declared_bytes = int(entry.get("bytes"))
        except (TypeError, ValueError):
            result.add("error", "private_snapshot_file_size_invalid", "private 文件大小声明不是整数。", path="private/viewing_secrets.json")
            return
        if declared_bytes != target.stat().st_size:
            result.add("error", "private_snapshot_file_size_mismatch", "private 文件大小与实际内容不一致。", path="private/viewing_secrets.json")


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path} is not a JSON object")
    return data


def _is_machine_text_path(path: str) -> bool:
    return any(path.endswith(suffix) for suffix in MACHINE_TEXT_PATH_SUFFIXES)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
