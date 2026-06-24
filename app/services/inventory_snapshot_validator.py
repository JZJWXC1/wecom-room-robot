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
)


REQUIRED_MANIFEST_FILES = {
    "manifest": "manifest.json",
    "inventory_json": "inventory.json",
    "inventory_csv": "inventory.csv",
    "rewrite_inventory_index": "rewrite_inventory_index.json",
    "sync_report": "sync_report.json",
}
PASSWORD_TEXT_PATTERN = re.compile(r"(?<!\d)\d{3,8}#(?!\d)")
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


class SnapshotValidator:
    def validate_snapshot(self, snapshot: InventorySnapshot) -> SnapshotValidationResult:
        result = SnapshotValidationResult()
        self._validate_snapshot_headers(snapshot, result)
        self._validate_manifest(snapshot, result)
        self._validate_listings(snapshot, result)
        self._validate_rewrite_index(snapshot.rewrite_index, result)
        self._validate_public_payload(snapshot.inventory_payload(redact_sensitive=True), "inventory", result)
        self._validate_utf8_serializable(snapshot.inventory_payload(redact_sensitive=True), "inventory", result)
        self._validate_utf8_serializable(snapshot.rewrite_index, "rewrite_inventory_index", result)
        return result

    def validate_directory(self, snapshot_dir: Path) -> SnapshotValidationResult:
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
        result = SnapshotValidationResult()
        if pointer_data.get("schema_version") != CURRENT_POINTER_SCHEMA_VERSION:
            result.add("error", "invalid_pointer_schema", "current_snapshot.json schema_version 不正确。", path="current_snapshot.json")
        snapshot_id = str(pointer_data.get("snapshot_id") or "")
        snapshot_path = str(pointer_data.get("snapshot_path") or "")
        if not snapshot_id:
            result.add("error", "missing_pointer_snapshot_id", "current pointer 缺少 snapshot_id。", path="current_snapshot.json")
        if not snapshot_path:
            result.add("error", "missing_pointer_snapshot_path", "current pointer 缺少 snapshot_path。", path="current_snapshot.json")
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

    def _validate_listings(self, snapshot: InventorySnapshot, result: SnapshotValidationResult) -> None:
        seen_listing_ids: set[str] = set()
        seen_keys: dict[str, str] = {}
        for index, listing in enumerate(snapshot.listings):
            path = f"listings[{index}]"
            if not listing.listing_id:
                result.add("error", "missing_listing_id", "房源缺少 listing_id。", path=path)
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
        if isinstance(value, str) and PASSWORD_TEXT_PATTERN.search(value):
            result.add("error", "public_payload_contains_password", "公共快照产物包含疑似真实密码。", path=path)

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
            if relative_path.endswith("/") or entry.get("status") == "reserved":
                continue
            target = snapshot_dir / relative_path
            if not target.exists():
                result.add("error", "snapshot_file_missing", "manifest 声明的文件不存在。", path=relative_path)
                continue
            declared_hash = str(entry.get("sha256") or "")
            if declared_hash and declared_hash != _file_sha256(target):
                result.add("error", "snapshot_file_hash_mismatch", "manifest 文件 hash 与实际内容不一致。", path=relative_path)


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path} is not a JSON object")
    return data


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
