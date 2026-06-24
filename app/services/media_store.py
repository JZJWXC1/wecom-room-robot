import json
from pathlib import Path
import re

from app.config import settings
from app.models import RoomMedia
from app.services.fuzzy_match import fuzzy_contains_score, normalize_search_text


GENERIC_MEDIA_WORDS = (
    "微信视频",
    "视频",
    "图片",
    "照片",
    "房源",
    "房子",
    "房间",
    "素材",
    "笔记",
    "发一下",
    "发给我",
    "发我",
    "发",
    "看一下",
    "看看",
    "看房",
    "还有",
    "目前",
    "在租",
    "有没有",
    "什么",
    "这套",
    "那套",
    "帮我",
    "给我",
    "可以",
    "需要",
    "要",
    "两个",
    "一个",
    "几条",
    "条",
    "个",
    "我",
    "的",
)


class MediaStore:
    video_extensions = {".mp4", ".mov", ".m4v"}
    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}

    def list_for_rooms(self, rooms: list[dict]) -> list[RoomMedia]:
        room_ids = self._extract_room_ids(rooms)
        return [self._read_room_media(room_id) for room_id in room_ids]

    def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
        return self._list_room_database_media(
            query,
            settings.room_database_path / "video",
            self.video_extensions,
            limit,
        )

    def list_room_database_images(self, query: str, limit: int = 6) -> list[Path]:
        return self._list_room_database_media(
            query,
            settings.room_database_path / "images",
            self.image_extensions,
            limit,
        )

    def _list_room_database_media(
        self,
        query: str,
        media_root: Path,
        extensions: set[str],
        limit: int,
    ) -> list[Path]:
        if not query.strip() or not media_root.exists():
            return []

        candidates = [
            path
            for path in media_root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in extensions
            and ".wecom_cache" not in path.parts
        ]
        scored: list[tuple[int, str, Path]] = []
        for path in candidates:
            score = self._score_media_path(query, path)
            if score > 0:
                scored.append((score, path.parent.name, path))

        if not scored:
            return []
        best_score = max(score for score, _, _ in scored)
        min_score = max(20, int(best_score * 0.6))
        scored = [item for item in scored if item[0] >= min_score]
        scored.sort(key=lambda item: (-item[0], item[1], item[2].name))
        return [path for _, _, path in scored[:limit]]

    def describe_paths(self, paths: list[Path]) -> list[str]:
        return [f"{path.parent.name}/{path.name}" for path in paths]

    def original_video_sources_for_paths(self, paths: list[Path]) -> dict[str, list]:
        manifest = self._load_media_source_manifest()
        if not manifest or not paths:
            return {
                "original_video_paths": [],
                "original_video_urls": [],
                "material_page_urls": [],
                "source_records": [],
            }
        by_key = self._media_source_records_by_key(manifest)
        source_records: list[dict] = []
        for path in paths:
            for key in self._source_lookup_keys(path):
                record = by_key.get(key)
                if record:
                    source_records.append(record)
                    break
        source_records = self._dedupe_source_records(source_records)
        return {
            "original_video_paths": self._source_record_values(source_records, ("original_path", "source_path")),
            "original_video_urls": self._source_record_urls(source_records, ("original_url", "source_url", "download_url")),
            "material_page_urls": self._source_record_urls(source_records, ("material_page_url", "feishu_url", "doc_url")),
            "source_records": source_records,
        }

    def public_urls(self, media: list[RoomMedia]) -> tuple[list[str], list[str]]:
        images: list[str] = []
        videos: list[str] = []
        base = settings.public_base_url.rstrip("/")
        for item in media:
            images.extend(f"{base}/{self._to_public_path(path)}" for path in item.images)
            videos.extend(f"{base}/{self._to_public_path(path)}" for path in item.videos)
        return images, videos

    def _load_media_source_manifest(self) -> dict:
        path = settings.room_database_path / "media_sources.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _media_source_records_by_key(self, manifest: dict) -> dict[str, dict]:
        records: list[dict] = []
        raw_records = manifest.get("sources") or manifest.get("records") or []
        if isinstance(raw_records, list):
            records.extend(record for record in raw_records if isinstance(record, dict))
        by_path = manifest.get("by_path") or {}
        if isinstance(by_path, dict):
            for key, record in by_path.items():
                if isinstance(record, dict):
                    records.append({"path": key, **record})
        result: dict[str, dict] = {}
        for record in records:
            for key in self._record_lookup_keys(record):
                result.setdefault(key, record)
        return result

    def _record_lookup_keys(self, record: dict) -> list[str]:
        keys: list[str] = []
        for field in ("path", "local_path", "file_path", "relative_path"):
            value = str(record.get(field) or "").strip()
            if value:
                keys.append(self._normalize_source_path_key(value))
                keys.append(self._normalize_text(value))
        room = str(record.get("room") or record.get("room_key") or record.get("label") or "").strip()
        filename = str(record.get("file_name") or record.get("name") or "").strip()
        if room and filename:
            keys.append(self._normalize_source_path_key(f"video/{room}/{filename}"))
            keys.append(self._normalize_text(f"{room}/{filename}"))
        return list(dict.fromkeys(key for key in keys if key))

    def _source_lookup_keys(self, path: Path) -> list[str]:
        keys = [
            self._normalize_source_path_key(str(path)),
            self._normalize_text(str(path)),
            self._normalize_text(path.name),
            self._normalize_text(f"{path.parent.name}/{path.name}"),
            self._normalize_source_path_key(f"video/{path.parent.name}/{path.name}"),
        ]
        try:
            relative = path.resolve().relative_to(settings.room_database_path.resolve())
            keys.extend(
                [
                    self._normalize_source_path_key(str(relative)),
                    self._normalize_text(str(relative)),
                ]
            )
        except (OSError, ValueError):
            pass
        return list(dict.fromkeys(key for key in keys if key))

    def _normalize_source_path_key(self, value: str) -> str:
        return value.replace("\\", "/").strip().lstrip("./").lower()

    def _source_record_values(self, records: list[dict], fields: tuple[str, ...]) -> list[str]:
        values: list[str] = []
        for record in records:
            for field in fields:
                value = str(record.get(field) or "").strip()
                if value:
                    values.append(value)
                    break
        return list(dict.fromkeys(values))

    def _source_record_urls(self, records: list[dict], fields: tuple[str, ...]) -> list[str]:
        values: list[str] = []
        for value in self._source_record_values(records, fields):
            if value.startswith(("http://", "https://")):
                values.append(value)
        return list(dict.fromkeys(values))

    def _dedupe_source_records(self, records: list[dict]) -> list[dict]:
        result: list[dict] = []
        seen: set[str] = set()
        for record in records:
            marker = "|".join(
                str(record.get(field) or "")
                for field in ("path", "local_path", "file_path", "original_url", "material_page_url", "source_url")
            )
            marker = marker or json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(record)
        return result

    def _extract_room_ids(self, rooms: list[dict]) -> list[str]:
        ids: list[str] = []
        room_keys = ("\u623f\u95f4\u53f7", "\u623f\u53f7", "room_id", "RoomID", "\u7f16\u53f7")
        for row in rooms:
            for key in room_keys:
                value = str(row.get(key, "")).strip()
                if value:
                    ids.append(value)
                    break
        return list(dict.fromkeys(ids))

    def _read_room_media(self, room_id: str) -> RoomMedia:
        root = settings.media_root / room_id
        if not root.exists():
            return RoomMedia(room_id=room_id, images=[], videos=[])
        images = [
            str(path)
            for path in root.iterdir()
            if path.suffix.lower() in self.image_extensions
        ]
        videos = [
            str(path)
            for path in root.iterdir()
            if path.suffix.lower() in self.video_extensions
        ]
        return RoomMedia(room_id=room_id, images=images, videos=videos)

    def _to_public_path(self, path: str) -> str:
        return Path(path).as_posix()

    def _score_media_path(self, query: str, path: Path) -> int:
        query_text = self._strip_generic_words(query)
        target_text = self._strip_generic_words(f"{path.parent.name} {path.stem}")
        query_norm = self._normalize_text(query_text)
        target_norm = self._normalize_text(target_text)
        if not query_norm or not target_norm:
            return 0

        query_room_tokens = set(self._room_tokens(query_text))
        target_room_tokens = set(self._room_tokens(target_text))
        parent_room_tokens = set(self._room_tokens(path.parent.name))
        filename_room_tokens = set(self._room_tokens(path.stem))
        if query_room_tokens:
            if not query_room_tokens.intersection(target_room_tokens):
                return 0
            if (
                filename_room_tokens
                and not query_room_tokens.intersection(filename_room_tokens)
                and not query_room_tokens.intersection(parent_room_tokens)
            ):
                return 0

        score = 0
        if query_norm in target_norm:
            score += 120

        if query_room_tokens & target_room_tokens:
            score += 90

        for chinese_token in re.findall(r"[一-鿿]{2,}", query_text):
            token_norm = self._normalize_text(chinese_token)
            if len(token_norm) >= 2 and token_norm in target_norm:
                score += 30 + len(token_norm)

        for token in self._query_tokens(query_text):
            token_norm = self._normalize_text(token)
            if len(token_norm) < 2:
                continue
            if token_norm in target_norm:
                score += 50 + len(token_norm)
                continue

            loose_token = self._loose_room_token(token_norm)
            if len(loose_token) >= 4 and loose_token in target_norm:
                score += 35 + len(loose_token)
                continue

            fuzzy_score = fuzzy_contains_score(token_norm, target_norm)
            if fuzzy_score:
                score += fuzzy_score

        for gram in self._char_grams(query_norm):
            if gram in target_norm:
                score += len(gram)
        return score

    def _strip_generic_words(self, text: str) -> str:
        cleaned = text
        for word in GENERIC_MEDIA_WORDS:
            cleaned = cleaned.replace(word, " ")
        return cleaned

    def _query_tokens(self, text: str) -> list[str]:
        tokens = re.findall(
            r"[一-鿿]*\d+(?:[-－—]\d+)+(?:[-－—]?[a-zA-Z0-9])?|[一-鿿]{2,}|[a-zA-Z0-9]{2,}",
            text,
        )
        return list(dict.fromkeys(tokens))

    def _normalize_text(self, text: str) -> str:
        return normalize_search_text(text)

    def _loose_room_token(self, token: str) -> str:
        if re.search(r"\d", token) and len(token) >= 5 and token[-1].isalnum():
            return token[:-1]
        return token

    def _room_tokens(self, text: str) -> list[str]:
        tokens = re.findall(r"\d+(?:[-－—][a-zA-Z0-9]+)+", text)
        normalized_tokens: list[str] = []
        for token in tokens:
            normalized_tokens.extend(
                self._normalize_text(alias)
                for alias in self._room_token_aliases(token)
            )
        return list(dict.fromkeys(normalized_tokens))

    def _room_token_aliases(self, token: str) -> list[str]:
        token = token.lower().replace("－", "-").replace("—", "-")
        aliases = [token]
        dash_suffix = re.fullmatch(r"(.+)-([1-9])", token)
        if dash_suffix:
            suffix_index = int(dash_suffix.group(2))
            aliases.append(f"{dash_suffix.group(1)}{chr(ord('a') + suffix_index - 1)}")
        letter_suffix = re.fullmatch(r"(.+\d)([a-z])", token)
        if letter_suffix:
            suffix_index = ord(letter_suffix.group(2)) - ord("a") + 1
            aliases.append(f"{letter_suffix.group(1)}-{suffix_index}")
        return aliases

    def _char_grams(self, text: str) -> list[str]:
        grams: list[str] = []
        for size in (4, 3, 2):
            grams.extend(text[index : index + size] for index in range(len(text) - size + 1))
        return list(dict.fromkeys(grams))
