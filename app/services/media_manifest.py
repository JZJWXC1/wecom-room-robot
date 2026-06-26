from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from app.services.feishu_base import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from app.services.fuzzy_match import fuzzy_contains_score, normalize_search_text
from app.services.inventory_snapshot_models import (
    canonical_json,
    is_safe_listing_id,
    now_utc_iso,
)
from app.services.region_inventory_utils import safe_name


MEDIA_MANIFEST_SCHEMA_VERSION = "media_manifest.v1"
MEDIA_MANIFEST_GENERATOR_VERSION = "media_manifest_foundation.v1"

MEDIA_KIND_IMAGE = "image"
MEDIA_KIND_VIDEO = "video"
MEDIA_KIND_ORIGINAL_VIDEO = "original_video"
MEDIA_KINDS = {MEDIA_KIND_IMAGE, MEDIA_KIND_VIDEO, MEDIA_KIND_ORIGINAL_VIDEO}

MEDIA_VARIANT_IMAGE = "image"
MEDIA_VARIANT_WECOM_VIDEO = "wecom_video"
MEDIA_VARIANT_ORIGINAL_VIDEO = "original_video"
MEDIA_VARIANTS = {
    MEDIA_VARIANT_IMAGE,
    MEDIA_VARIANT_WECOM_VIDEO,
    MEDIA_VARIANT_ORIGINAL_VIDEO,
}

BINDING_METHOD_LISTING_ID = "listing_id"
BINDING_METHOD_MANUAL = "manual"

LISTING_ID_RE = re.compile(r"lst_[0-9a-f]{16}", re.IGNORECASE)
ORIGINAL_VIDEO_MARKERS = (
    "原视频",
    "原片",
    "高清",
    "源文件",
    "未压缩",
    "下载链接",
    "original",
    "source",
    "raw",
)
URL_FIELDS = (
    "original_url",
    "source_url",
    "download_url",
    "url",
    "tmp_url",
)
MATERIAL_PAGE_FIELDS = (
    "material_page_url",
    "feishu_url",
    "doc_url",
    "page_url",
    "web_url",
)


@dataclass(frozen=True)
class MediaItem:
    listing_id: str
    kind: str
    file_name: str
    relative_path: str = ""
    variant: str = ""
    size: int = 0
    sha256: str = ""
    local_path: str = ""
    media_id: str = ""
    source: str = "feishu_drive"
    source_path: str = ""
    source_id_hash: str = ""
    source_file_token: str = ""
    source_url: str = ""
    original_url: str = ""
    material_page_url: str = ""
    modified_at: str = ""
    binding_method: str = BINDING_METHOD_LISTING_ID
    access_verified: bool = False
    wecom_sendable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in MEDIA_KINDS:
            raise ValueError(f"unsupported media kind: {self.kind}")
        if not is_safe_listing_id(self.listing_id):
            raise ValueError(f"invalid listing_id: {self.listing_id}")
        if not self.variant:
            object.__setattr__(self, "variant", _variant_for_kind(self.kind))
        if self.variant not in MEDIA_VARIANTS:
            raise ValueError(f"unsupported media variant: {self.variant}")
        if not self.local_path and self.relative_path:
            object.__setattr__(self, "local_path", self.relative_path)
        if not self.source_file_token and self.source_id_hash:
            object.__setattr__(self, "source_file_token", self.source_id_hash)
        if not self.source_id_hash and self.source_file_token:
            object.__setattr__(self, "source_id_hash", self.source_file_token)
        if not self.source_url and self.original_url:
            object.__setattr__(self, "source_url", self.original_url)
        if not self.original_url and self.source_url and self.kind == MEDIA_KIND_ORIGINAL_VIDEO:
            object.__setattr__(self, "original_url", self.source_url)
        if not self.media_id:
            object.__setattr__(self, "media_id", self._build_media_id())
        if self.kind == MEDIA_KIND_VIDEO and not self.wecom_sendable:
            object.__setattr__(self, "wecom_sendable", True)

    def _build_media_id(self) -> str:
        payload = canonical_json(
            {
                "listing_id": self.listing_id,
                "kind": self.kind,
                "variant": self.variant,
                "relative_path": self.relative_path,
                "local_path": self.local_path,
                "file_name": self.file_name,
                "source_path": self.source_path,
                "source_file_token": self.source_file_token,
                "source_url": self.source_url,
                "original_url": self.original_url,
                "material_page_url": self.material_page_url,
                "modified_at": self.modified_at,
                "binding_method": self.binding_method,
                "access_verified": self.access_verified,
                "size": self.size,
                "sha256": self.sha256,
            }
        )
        return "med_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "listing_id": self.listing_id,
            "kind": self.kind,
            "variant": self.variant,
            "file_name": self.file_name,
            "relative_path": self.relative_path,
            "local_path": self.local_path,
            "size": self.size,
            "sha256": self.sha256,
            "source": self.source,
            "source_path": self.source_path,
            "source_id_hash": self.source_id_hash,
            "source_file_token": self.source_file_token,
            "source_url": self.source_url,
            "original_url": self.original_url,
            "material_page_url": self.material_page_url,
            "modified_at": self.modified_at,
            "binding_method": self.binding_method,
            "access_verified": self.access_verified,
            "wecom_sendable": self.wecom_sendable,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaItem":
        return cls(
            media_id=str(data.get("media_id") or ""),
            listing_id=str(data.get("listing_id") or ""),
            kind=str(data.get("kind") or ""),
            file_name=str(data.get("file_name") or ""),
            relative_path=str(data.get("relative_path") or ""),
            variant=str(data.get("variant") or ""),
            local_path=str(data.get("local_path") or ""),
            size=int(data.get("size") or 0),
            sha256=str(data.get("sha256") or ""),
            source=str(data.get("source") or "feishu_drive"),
            source_path=str(data.get("source_path") or ""),
            source_id_hash=str(data.get("source_id_hash") or ""),
            source_file_token=str(data.get("source_file_token") or ""),
            source_url=str(data.get("source_url") or ""),
            original_url=str(data.get("original_url") or ""),
            material_page_url=str(data.get("material_page_url") or ""),
            modified_at=str(data.get("modified_at") or ""),
            binding_method=str(data.get("binding_method") or BINDING_METHOD_LISTING_ID),
            access_verified=bool(data.get("access_verified")),
            wecom_sendable=bool(data.get("wecom_sendable")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class MediaManifest:
    listing_ids: list[str] = field(default_factory=list)
    items: list[MediaItem] = field(default_factory=list)
    generated_at: str = field(default_factory=now_utc_iso)
    source_hash: str = ""
    schema_version: str = MEDIA_MANIFEST_SCHEMA_VERSION
    generator_version: str = MEDIA_MANIFEST_GENERATOR_VERSION

    def __post_init__(self) -> None:
        self.listing_ids = _dedupe_listing_ids(self.listing_ids)
        indexed_ids = set(self.listing_ids)
        for item in self.items:
            if item.listing_id not in indexed_ids:
                self.listing_ids.append(item.listing_id)
                indexed_ids.add(item.listing_id)
        if not self.source_hash:
            self.source_hash = self._build_source_hash()

    @classmethod
    def build(
        cls,
        *,
        listing_ids: Iterable[str],
        items: Iterable[MediaItem],
        generated_at: str | None = None,
    ) -> "MediaManifest":
        sorted_items = sorted(
            list(items),
            key=lambda item: (
                item.listing_id,
                item.kind,
                item.relative_path,
                item.original_url,
                item.file_name,
            ),
        )
        return cls(
            listing_ids=_dedupe_listing_ids(listing_ids),
            items=sorted_items,
            generated_at=generated_at or now_utc_iso(),
        )

    def _build_source_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "generator_version": self.generator_version,
            "listing_ids": self.listing_ids,
            "items": [item.to_dict() for item in self.items],
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def items_for_listing(self, listing_id: str, *, kind: str | None = None) -> list[MediaItem]:
        if kind is not None and kind not in MEDIA_KINDS:
            raise ValueError(f"unsupported media kind: {kind}")
        return [
            item
            for item in self.items
            if item.listing_id == listing_id and (kind is None or item.kind == kind)
        ]

    def images_for_listing(self, listing_id: str) -> list[MediaItem]:
        return self.items_for_listing(listing_id, kind=MEDIA_KIND_IMAGE)

    def videos_for_listing(self, listing_id: str) -> list[MediaItem]:
        return self.items_for_listing(listing_id, kind=MEDIA_KIND_VIDEO)

    def original_videos_for_listing(self, listing_id: str) -> list[MediaItem]:
        return self.items_for_listing(listing_id, kind=MEDIA_KIND_ORIGINAL_VIDEO)

    def has_original_video(self, listing_id: str) -> bool:
        return bool(self.original_videos_for_listing(listing_id))

    def has_wecom_sendable_video(self, listing_id: str) -> bool:
        return any(item.wecom_sendable for item in self.videos_for_listing(listing_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generator_version": self.generator_version,
            "generated_at": self.generated_at,
            "source_hash": self.source_hash,
            "listing_ids": self.listing_ids,
            "listings": self._grouped_items(),
            "items": [item.to_dict() for item in self.items],
        }

    def _grouped_items(self) -> dict[str, dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for listing_id in self.listing_ids:
            grouped[listing_id] = {
                "images": [item.to_dict() for item in self.images_for_listing(listing_id)],
                "videos": [item.to_dict() for item in self.videos_for_listing(listing_id)],
                "original_videos": [
                    item.to_dict()
                    for item in self.original_videos_for_listing(listing_id)
                ],
                "has_wecom_sendable_video": self.has_wecom_sendable_video(listing_id),
                "has_original_video": self.has_original_video(listing_id),
            }
        return grouped

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaManifest":
        items = [
            MediaItem.from_dict(item)
            for item in data.get("items") or []
            if isinstance(item, dict)
        ]
        if not items and isinstance(data.get("listings"), dict):
            for listing in data["listings"].values():
                if not isinstance(listing, dict):
                    continue
                for key in ("images", "videos", "original_videos"):
                    items.extend(
                        MediaItem.from_dict(item)
                        for item in listing.get(key) or []
                        if isinstance(item, dict)
                    )
        return cls(
            schema_version=str(data.get("schema_version") or MEDIA_MANIFEST_SCHEMA_VERSION),
            generator_version=str(data.get("generator_version") or MEDIA_MANIFEST_GENERATOR_VERSION),
            generated_at=str(data.get("generated_at") or now_utc_iso()),
            source_hash=str(data.get("source_hash") or ""),
            listing_ids=[str(item) for item in data.get("listing_ids") or []],
            items=items,
        )

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(self.to_dict()), encoding="utf-8")
        return path

    @classmethod
    def read_json(cls, path: Path) -> "MediaManifest":
        import json

        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


@dataclass
class MediaBindingReport:
    listing_ids: list[str]
    bound_items: list[dict[str, Any]] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)
    ambiguous_items: list[dict[str, Any]] = field(default_factory=list)
    orphan_items: list[dict[str, Any]] = field(default_factory=list)
    fuzzy_candidates: list[dict[str, Any]] = field(default_factory=list)
    isolated_items: list[dict[str, Any]] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    manifest_path: str = ""
    generated_at: str = field(default_factory=now_utc_iso)

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "generated_at": self.generated_at,
            "listing_ids": self.listing_ids,
            "bound_count": len(self.bound_items),
            "missing_count": len(self.missing),
            "ambiguous_count": len(self.ambiguous_items),
            "orphan_count": len(self.orphan_items),
            "fuzzy_candidate_count": len(self.fuzzy_candidates),
            "isolated_count": len(self.isolated_items),
            "downloaded_count": len(self.downloaded),
            "reused_count": len(self.reused),
            "skipped_count": len(self.skipped),
            "failed_count": len(self.failed),
            "manifest_path": self.manifest_path,
            "bound_items": self.bound_items,
            "missing": self.missing,
            "ambiguous_items": self.ambiguous_items,
            "orphan_items": self.orphan_items,
            "fuzzy_candidates": self.fuzzy_candidates,
            "isolated_items": self.isolated_items,
            "downloaded": self.downloaded,
            "reused": self.reused,
            "skipped": self.skipped,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class MediaManifestEvidence:
    media_id: str
    listing_id: str
    variant: str
    sha256: str = ""
    local_path: str = ""
    source_file_token: str = ""
    source_url: str = ""
    modified_at: str = ""
    binding_method: str = ""
    access_verified: bool = False
    kind: str = ""
    file_name: str = ""
    source_path: str = ""
    material_page_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "listing_id": self.listing_id,
            "variant": self.variant,
            "sha256": self.sha256,
            "local_path": self.local_path,
            "source_file_token": self.source_file_token,
            "source_url": self.source_url,
            "modified_at": self.modified_at,
            "binding_method": self.binding_method,
            "access_verified": self.access_verified,
            "kind": self.kind,
            "file_name": self.file_name,
            "source_path": self.source_path,
            "material_page_url": self.material_page_url,
        }


class MediaManifestShadowAdapter:
    """Read-only evidence adapter for listing_id -> media_id -> local/source material."""

    def __init__(self, manifest: MediaManifest, *, local_root: Path | None = None) -> None:
        self.manifest = manifest
        self.local_root = local_root
        self._by_media_id = {item.media_id: item for item in manifest.items}

    @classmethod
    def from_path(cls, path: Path, *, local_root: Path | None = None) -> "MediaManifestShadowAdapter":
        return cls(MediaManifest.read_json(path), local_root=local_root or path.parent)

    def evidence_for_listing(
        self,
        listing_id: str,
        *,
        variant: str | None = None,
    ) -> list[MediaManifestEvidence]:
        items = self.manifest.items_for_listing(listing_id)
        if variant is not None:
            items = [item for item in items if item.variant == variant]
        return [self._evidence_for_item(item) for item in items]

    def evidence_by_media_id(self, media_id: str) -> MediaManifestEvidence | None:
        item = self._by_media_id.get(media_id)
        return self._evidence_for_item(item) if item else None

    def local_file_for_media_id(self, media_id: str) -> Path | None:
        item = self._by_media_id.get(media_id)
        if not item or not item.local_path:
            return None
        path = Path(item.local_path)
        return path if path.is_absolute() else (self.local_root / path if self.local_root else path)

    def _evidence_for_item(self, item: MediaItem) -> MediaManifestEvidence:
        resolved_local = self.local_file_for_media_id(item.media_id)
        local_path = str(resolved_local) if resolved_local else item.local_path
        access_verified = item.access_verified
        if resolved_local and item.sha256:
            access_verified = resolved_local.is_file() and _file_sha256(resolved_local) == item.sha256
        elif resolved_local:
            access_verified = resolved_local.is_file()
        elif item.source_url:
            access_verified = item.access_verified
        return MediaManifestEvidence(
            media_id=item.media_id,
            listing_id=item.listing_id,
            variant=item.variant,
            sha256=item.sha256,
            local_path=local_path,
            source_file_token=item.source_file_token,
            source_url=item.source_url,
            modified_at=item.modified_at,
            binding_method=item.binding_method,
            access_verified=access_verified,
            kind=item.kind,
            file_name=item.file_name,
            source_path=item.source_path,
            material_page_url=item.material_page_url,
        )


class FeishuDriveMediaManifestAdapter:
    """Build a local media manifest from Feishu Drive without fuzzy binding."""

    def __init__(
        self,
        *,
        client: Any,
        listing_ids: Iterable[str],
        target_root: Path,
        listing_labels: dict[str, str] | None = None,
        quarantine_dir: Path | None = None,
    ) -> None:
        self.client = client
        self.listing_ids = _dedupe_listing_ids(listing_ids)
        self.known_listing_ids = set(self.listing_ids)
        self.target_root = target_root
        self.listing_labels = {
            listing_id: label
            for listing_id, label in (listing_labels or {}).items()
            if listing_id in self.known_listing_ids and str(label).strip()
        }
        self.quarantine_dir = quarantine_dir or target_root / "_manual_review"

    async def sync_from_drive(
        self,
        *,
        root_folder_token: str,
        expected_kinds: Iterable[str] = (MEDIA_KIND_IMAGE, MEDIA_KIND_VIDEO),
        manifest_path: Path | None = None,
    ) -> tuple[MediaManifest, MediaBindingReport]:
        if not root_folder_token:
            raise ValueError("root_folder_token is required")
        report = MediaBindingReport(listing_ids=self.listing_ids)
        items: list[MediaItem] = []
        await self._walk_drive(root_folder_token, [], items, report)
        manifest = MediaManifest.build(listing_ids=self.listing_ids, items=items)
        report.missing = self._missing_media(manifest, expected_kinds)
        if manifest_path:
            manifest.write_json(manifest_path)
            report.manifest_path = str(manifest_path)
        return manifest, report

    async def _walk_drive(
        self,
        folder_token: str,
        path_parts: list[str],
        items: list[MediaItem],
        report: MediaBindingReport,
    ) -> None:
        for item in await self.client.list_folder_files(folder_token):
            name = _item_name(item)
            item_type = _item_type(item)
            token = _item_token(item)
            if item_type == "folder":
                if token:
                    await self._walk_drive(token, path_parts + [name], items, report)
                else:
                    report.skipped.append(
                        {
                            "source_path": _source_path(path_parts, name),
                            "reason": "folder_missing_token",
                        }
                    )
                continue
            await self._handle_file_item(item, path_parts, items, report)

    async def _handle_file_item(
        self,
        item: dict[str, Any],
        path_parts: list[str],
        items: list[MediaItem],
        report: MediaBindingReport,
    ) -> None:
        name = _item_name(item)
        source_path = _source_path(path_parts, name)
        kind = _media_kind(item, path_parts)
        if not kind:
            report.skipped.append({"source_path": source_path, "reason": "unsupported_media_type"})
            return

        listing_ids = self._listing_ids_for_item(item, path_parts)
        known_ids = [listing_id for listing_id in listing_ids if listing_id in self.known_listing_ids]
        unknown_ids = [listing_id for listing_id in listing_ids if listing_id not in self.known_listing_ids]
        if len(known_ids) == 1 and not unknown_ids:
            media_item = await self._bind_item(
                item,
                known_ids[0],
                kind,
                source_path,
                report,
            )
            if media_item:
                items.append(media_item)
                report.bound_items.append(
                    {
                        "listing_id": media_item.listing_id,
                        "kind": media_item.kind,
                        "relative_path": media_item.relative_path,
                        "source_path": source_path,
                    }
                )
            return

        record = {
            "source_path": source_path,
            "kind": kind,
            "candidate_listing_ids": listing_ids,
            "reason": "multiple_listing_ids" if len(listing_ids) > 1 else "missing_listing_id",
        }
        if len(listing_ids) > 1:
            report.ambiguous_items.append(record)
            await self._isolate_item(item, "ambiguous", source_path, report)
            return

        if unknown_ids:
            record["reason"] = "unknown_listing_id"
            report.orphan_items.append(record)
            await self._isolate_item(item, "orphan", source_path, report)
            return

        report.orphan_items.append(record)
        fuzzy = self._fuzzy_candidates(source_path)
        if fuzzy:
            report.fuzzy_candidates.append(
                {
                    "source_path": source_path,
                    "kind": kind,
                    "candidates": fuzzy,
                    "reason": "fuzzy_candidate_only_not_bound",
                }
            )
        await self._isolate_item(item, "orphan", source_path, report)

    async def _bind_item(
        self,
        item: dict[str, Any],
        listing_id: str,
        kind: str,
        source_path: str,
        report: MediaBindingReport,
    ) -> MediaItem | None:
        name = safe_name(_item_name(item))
        original_url = _first_http_url(item, URL_FIELDS)
        material_page_url = _first_http_url(item, MATERIAL_PAGE_FIELDS)
        source_token_hash = _token_hash(_item_token(item))

        relative_path = ""
        size = 0
        sha256 = ""
        if _item_token(item) and _has_downloadable_media_file(item, kind):
            relative_path = _manifest_relative_path(kind, listing_id, name)
            target_path = self.target_root / Path(relative_path)
            synced = await self._sync_file(
                item,
                target_path,
                source_path,
                report,
            )
            if synced is None:
                return None
            size = synced.stat().st_size
            sha256 = _file_sha256(synced)
        elif kind != MEDIA_KIND_ORIGINAL_VIDEO:
            report.failed.append(
                {
                    "source_path": source_path,
                    "reason": "downloadable_media_missing_token",
                }
            )
            return None

        return MediaItem(
            listing_id=listing_id,
            kind=kind,
            file_name=name,
            relative_path=relative_path,
            variant=_variant_for_kind(kind),
            size=size,
            sha256=sha256,
            local_path=relative_path,
            source_path=source_path,
            source_id_hash=source_token_hash,
            source_file_token=source_token_hash,
            source_url=original_url,
            original_url=original_url if kind == MEDIA_KIND_ORIGINAL_VIDEO else "",
            material_page_url=material_page_url,
            modified_at=_item_modified_at(item),
            binding_method=BINDING_METHOD_LISTING_ID,
            access_verified=bool(sha256 or original_url),
            wecom_sendable=kind == MEDIA_KIND_VIDEO,
        )

    async def _sync_file(
        self,
        item: dict[str, Any],
        target_path: Path,
        source_path: str,
        report: MediaBindingReport,
    ) -> Path | None:
        expected_size = _item_size(item)
        if _existing_file_matches(target_path, expected_size):
            report.reused.append(str(target_path))
            return target_path

        token = _item_token(item)
        if not token:
            report.failed.append({"source_path": source_path, "reason": "missing_file_token"})
            return None
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_name(f"{target_path.name}.part")
        try:
            await self.client.download_file(token, temp_path)
            if not temp_path.is_file():
                report.failed.append(
                    {
                        "source_path": source_path,
                        "target_path": str(target_path),
                        "reason": "download_missing_temp_file",
                    }
                )
                return None
            temp_path.replace(target_path)
            report.downloaded.append(str(target_path))
            return target_path
        except Exception as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            report.failed.append(
                {
                    "source_path": source_path,
                    "target_path": str(target_path),
                    "reason": str(exc),
                }
            )
            return None

    async def _isolate_item(
        self,
        item: dict[str, Any],
        bucket: str,
        source_path: str,
        report: MediaBindingReport,
    ) -> None:
        token = _item_token(item)
        name = safe_name(_item_name(item))
        if not token or not _has_downloadable_media_file(item, _media_kind(item, [])):
            report.isolated_items.append(
                {
                    "source_path": source_path,
                    "bucket": bucket,
                    "reason": "not_downloaded",
                }
            )
            return
        marker = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:10]
        target_path = self.quarantine_dir / bucket / f"{marker}_{name}"
        synced = await self._sync_file(item, target_path, source_path, report)
        report.isolated_items.append(
            {
                "source_path": source_path,
                "bucket": bucket,
                "target_path": str(target_path) if synced else "",
            }
        )

    def _listing_ids_for_item(self, item: dict[str, Any], path_parts: list[str]) -> list[str]:
        ids: list[str] = []
        for key in ("listing_id", "listingId", "房源ID", "房源编号"):
            value = str(item.get(key) or "").strip()
            if is_safe_listing_id(value):
                ids.append(value.lower())
        texts = [*path_parts, _item_name(item)]
        for text in texts:
            ids.extend(match.group(0).lower() for match in LISTING_ID_RE.finditer(str(text)))
        return list(dict.fromkeys(ids))

    def _fuzzy_candidates(self, source_path: str) -> list[dict[str, Any]]:
        source_text = normalize_search_text(source_path)
        candidates: list[dict[str, Any]] = []
        if not source_text:
            return candidates
        for listing_id, label in self.listing_labels.items():
            label_text = normalize_search_text(label)
            if not label_text:
                continue
            score = 0
            if label_text in source_text:
                score = 100 + len(label_text)
            else:
                score = fuzzy_contains_score(label_text, source_text)
            if score:
                candidates.append({"listing_id": listing_id, "label": label, "score": score})
        candidates.sort(key=lambda item: (-int(item["score"]), item["listing_id"]))
        return candidates[:5]

    def _missing_media(
        self,
        manifest: MediaManifest,
        expected_kinds: Iterable[str],
    ) -> list[dict[str, Any]]:
        expected = [kind for kind in dict.fromkeys(expected_kinds) if kind in MEDIA_KINDS]
        missing: list[dict[str, Any]] = []
        for listing_id in self.listing_ids:
            missing_kinds = [
                kind
                for kind in expected
                if not manifest.items_for_listing(listing_id, kind=kind)
            ]
            if missing_kinds:
                missing.append({"listing_id": listing_id, "missing_kinds": missing_kinds})
        return missing


def _dedupe_listing_ids(listing_ids: Iterable[str]) -> list[str]:
    return list(
        dict.fromkeys(
            str(listing_id).strip().lower()
            for listing_id in listing_ids
            if is_safe_listing_id(str(listing_id).strip())
        )
    )


def _item_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("title") or item.get("file_name") or "unnamed").strip()


def _item_type(item: dict[str, Any]) -> str:
    return str(item.get("type") or item.get("file_type") or "").strip().lower()


def _item_token(item: dict[str, Any]) -> str:
    return str(item.get("token") or item.get("file_token") or item.get("fileKey") or "").strip()


def _item_size(item: dict[str, Any]) -> int:
    try:
        return int(item.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def _item_modified_at(item: dict[str, Any]) -> str:
    for field in ("modified_at", "modified_time", "updated_at", "update_time", "created_at"):
        value = str(item.get(field) or "").strip()
        if value:
            return value
    return ""


def _source_path(path_parts: list[str], name: str) -> str:
    return PurePosixPath(*(safe_name(part) for part in [*path_parts, name] if str(part).strip())).as_posix()


def _media_kind(item: dict[str, Any], path_parts: list[str]) -> str:
    name = _item_name(item)
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return MEDIA_KIND_IMAGE
    if suffix in VIDEO_EXTENSIONS:
        return MEDIA_KIND_ORIGINAL_VIDEO if _looks_original_video(item, path_parts) else MEDIA_KIND_VIDEO
    if _looks_original_video(item, path_parts) and _first_http_url(item, URL_FIELDS):
        return MEDIA_KIND_ORIGINAL_VIDEO
    return ""


def _looks_original_video(item: dict[str, Any], path_parts: list[str]) -> bool:
    text = " ".join([*path_parts, _item_name(item), str(item.get("kind") or ""), str(item.get("media_kind") or "")])
    normalized = normalize_search_text(text).lower()
    lowered = text.casefold()
    return any(marker in normalized or marker in lowered for marker in ORIGINAL_VIDEO_MARKERS)


def _has_downloadable_media_file(item: dict[str, Any], kind: str) -> bool:
    if kind == MEDIA_KIND_ORIGINAL_VIDEO and not Path(_item_name(item)).suffix:
        return False
    return Path(_item_name(item)).suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def _manifest_relative_path(kind: str, listing_id: str, file_name: str) -> str:
    root = {
        MEDIA_KIND_IMAGE: "images",
        MEDIA_KIND_VIDEO: "video",
        MEDIA_KIND_ORIGINAL_VIDEO: "original_video",
    }[kind]
    return PurePosixPath(root, listing_id, file_name).as_posix()


def _variant_for_kind(kind: str) -> str:
    return {
        MEDIA_KIND_IMAGE: MEDIA_VARIANT_IMAGE,
        MEDIA_KIND_VIDEO: MEDIA_VARIANT_WECOM_VIDEO,
        MEDIA_KIND_ORIGINAL_VIDEO: MEDIA_VARIANT_ORIGINAL_VIDEO,
    }[kind]


def _existing_file_matches(path: Path, expected_size: int) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    return expected_size <= 0 or path.stat().st_size == expected_size


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _token_hash(token: str) -> str:
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _first_http_url(item: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = str(item.get(field) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


__all__ = [
    "MEDIA_MANIFEST_SCHEMA_VERSION",
    "MEDIA_KIND_IMAGE",
    "MEDIA_KIND_VIDEO",
    "MEDIA_KIND_ORIGINAL_VIDEO",
    "MEDIA_VARIANT_IMAGE",
    "MEDIA_VARIANT_WECOM_VIDEO",
    "MEDIA_VARIANT_ORIGINAL_VIDEO",
    "BINDING_METHOD_LISTING_ID",
    "MediaBindingReport",
    "MediaManifestEvidence",
    "MediaItem",
    "MediaManifest",
    "MediaManifestShadowAdapter",
    "FeishuDriveMediaManifestAdapter",
]
