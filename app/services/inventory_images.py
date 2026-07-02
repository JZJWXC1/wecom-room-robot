from __future__ import annotations

from pathlib import Path

from app.config import settings


_GLOB_MAGIC = frozenset("*?[]")


def inventory_image_glob_paths(pattern: str | None = None) -> list[Path]:
    raw_pattern = str(pattern or settings.inventory_image_glob or "").strip()
    if not raw_pattern:
        return []
    glob_path = Path(raw_pattern)
    if glob_path.is_absolute():
        if any(char in glob_path.name for char in _GLOB_MAGIC):
            candidates = glob_path.parent.glob(glob_path.name)
        else:
            candidates = [glob_path]
    else:
        candidates = settings.room_database_path.parent.glob(raw_pattern)
    return sorted(path for path in candidates if path.exists())
