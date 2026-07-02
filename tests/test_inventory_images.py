from app.config import settings
from app.services.inventory_images import inventory_image_glob_paths


def test_inventory_image_glob_paths_accepts_absolute_pattern(tmp_path, monkeypatch):
    room_database = tmp_path / "room_database"
    room_database.mkdir()
    image = room_database / "inventory_01.png"
    image.write_bytes(b"png")
    monkeypatch.setattr(settings, "room_database_path", room_database)
    monkeypatch.setattr(settings, "inventory_image_glob", str(room_database / "inventory_*.png"))

    assert inventory_image_glob_paths() == [image]


def test_inventory_image_glob_paths_accepts_relative_pattern_with_absolute_room_database(
    tmp_path,
    monkeypatch,
):
    room_database = tmp_path / "room_database"
    room_database.mkdir()
    image = room_database / "inventory_02.png"
    image.write_bytes(b"png")
    monkeypatch.setattr(settings, "room_database_path", room_database)
    monkeypatch.setattr(settings, "inventory_image_glob", "room_database/inventory_*.png")

    assert inventory_image_glob_paths() == [image]
