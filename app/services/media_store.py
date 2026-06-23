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

    def public_urls(self, media: list[RoomMedia]) -> tuple[list[str], list[str]]:
        images: list[str] = []
        videos: list[str] = []
        base = settings.public_base_url.rstrip("/")
        for item in media:
            images.extend(f"{base}/{self._to_public_path(path)}" for path in item.images)
            videos.extend(f"{base}/{self._to_public_path(path)}" for path in item.videos)
        return images, videos

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
