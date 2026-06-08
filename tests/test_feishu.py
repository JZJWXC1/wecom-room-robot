import tempfile
import unittest
from pathlib import Path

import httpx
from openpyxl import Workbook

from app.config import settings
from app.services.feishu import FeishuClient
from app.services.inventory_image_sync import InventoryImageSyncer


class FeishuClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_bitable_records_as_dataframe(self) -> None:
        previous_app_id = settings.feishu_app_id
        previous_secret = settings.feishu_app_secret
        previous_app_token = settings.feishu_bitable_app_token
        previous_table_id = settings.feishu_bitable_table_id
        try:
            settings.feishu_app_id = "cli_xxx"
            settings.feishu_app_secret = "secret_xxx"
            settings.feishu_bitable_app_token = "app_token"
            settings.feishu_bitable_table_id = "table_id"

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
                    return httpx.Response(
                        200,
                        json={"code": 0, "tenant_access_token": "tenant_token"},
                    )
                self.assertEqual(request.headers["Authorization"], "Bearer tenant_token")
                self.assertTrue(
                    request.url.path.endswith(
                        "/bitable/v1/apps/app_token/tables/table_id/records/search"
                    )
                )
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "record_id": "rec_1",
                                    "fields": {
                                        "小区": "小洋坝",
                                        "租金": 1800,
                                        "标签": [{"text": "可短租"}],
                                    },
                                }
                            ],
                            "has_more": False,
                        },
                    },
                )

            client = FeishuClient(transport=httpx.MockTransport(handler))
            frame = await client.read_bitable_dataframe()

            self.assertEqual(len(frame), 1)
            self.assertEqual(frame.iloc[0]["小区"], "小洋坝")
            self.assertEqual(frame.iloc[0]["租金"], "1800")
            self.assertEqual(frame.iloc[0]["标签"], "可短租")
        finally:
            settings.feishu_app_id = previous_app_id
            settings.feishu_app_secret = previous_secret
            settings.feishu_bitable_app_token = previous_app_token
            settings.feishu_bitable_table_id = previous_table_id

    async def test_retries_when_tenant_access_token_is_invalid(self) -> None:
        previous_app_id = settings.feishu_app_id
        previous_secret = settings.feishu_app_secret
        previous_sheet_token = settings.feishu_inventory_sheet_token
        try:
            settings.feishu_app_id = "cli_xxx"
            settings.feishu_app_secret = "secret_xxx"
            settings.feishu_inventory_sheet_token = "sheet_token"
            auth_tokens = ["old_token", "new_token"]
            auth_calls = 0
            query_tokens: list[str] = []

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal auth_calls
                if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
                    token = auth_tokens[auth_calls]
                    auth_calls += 1
                    return httpx.Response(
                        200,
                        json={"code": 0, "tenant_access_token": token},
                    )
                query_tokens.append(request.headers["Authorization"])
                if query_tokens[-1] == "Bearer old_token":
                    return httpx.Response(
                        400,
                        json={
                            "code": 99991663,
                            "msg": "Invalid access token for authorization.",
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "sheets": [
                                {"sheet_id": "sheet_1", "title": "总", "index": 0}
                            ]
                        },
                    },
                )

            client = FeishuClient(transport=httpx.MockTransport(handler))
            sheets = await client.list_spreadsheet_sheets()

            self.assertEqual(sheets[0]["sheet_id"], "sheet_1")
            self.assertEqual(auth_calls, 2)
            self.assertEqual(query_tokens, ["Bearer old_token", "Bearer new_token"])
        finally:
            settings.feishu_app_id = previous_app_id
            settings.feishu_app_secret = previous_secret
            settings.feishu_inventory_sheet_token = previous_sheet_token

    async def test_syncs_drive_media_to_room_database(self) -> None:
        previous_app_id = settings.feishu_app_id
        previous_secret = settings.feishu_app_secret
        try:
            settings.feishu_app_id = "cli_xxx"
            settings.feishu_app_secret = "secret_xxx"

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
                    return httpx.Response(
                        200,
                        json={"code": 0, "tenant_access_token": "tenant_token"},
                    )
                if request.url.path.endswith("/drive/v1/files"):
                    folder_token = request.url.params.get("folder_token")
                    if folder_token == "root":
                        return httpx.Response(
                            200,
                            json={
                                "code": 0,
                                "data": {
                                    "files": [
                                        {
                                            "name": "小洋坝三区12-1003-2",
                                            "type": "folder",
                                            "token": "folder_1",
                                        }
                                    ],
                                    "has_more": False,
                                },
                            },
                        )
                    if folder_token == "folder_1":
                        return httpx.Response(
                            200,
                            json={
                                "code": 0,
                                "data": {
                                    "files": [
                                        {
                                            "name": "看房视频.mp4",
                                            "type": "file",
                                            "token": "video_token",
                                        },
                                        {
                                            "name": "客厅.jpg",
                                            "type": "file",
                                            "token": "image_token",
                                        },
                                        {
                                            "name": "说明.txt",
                                            "type": "file",
                                            "token": "text_token",
                                        },
                                    ],
                                    "has_more": False,
                                },
                            },
                        )
                if request.url.path.endswith("/drive/v1/files/video_token/download"):
                    return httpx.Response(200, content=b"video")
                if request.url.path.endswith("/drive/v1/files/image_token/download"):
                    return httpx.Response(200, content=b"image")
                raise AssertionError(f"Unexpected request: {request.url}")

            with tempfile.TemporaryDirectory() as directory:
                target_root = Path(directory) / "room_database"
                client = FeishuClient(transport=httpx.MockTransport(handler))
                result = await client.sync_drive_media(
                    folder_token="root",
                    target_root=target_root,
                )

                self.assertEqual(len(result["downloaded"]), 2)
                self.assertEqual(result["skipped"], ["说明.txt"])
                self.assertEqual(
                    (target_root / "video" / "小洋坝三区12-1003-2" / "看房视频.mp4").read_bytes(),
                    b"video",
                )
                self.assertEqual(
                    (target_root / "images" / "小洋坝三区12-1003-2" / "客厅.jpg").read_bytes(),
                    b"image",
                )
        finally:
            settings.feishu_app_id = previous_app_id
            settings.feishu_app_secret = previous_secret

    async def test_sync_drive_media_skips_existing_local_file(self) -> None:
        previous_app_id = settings.feishu_app_id
        previous_secret = settings.feishu_app_secret
        try:
            settings.feishu_app_id = "cli_xxx"
            settings.feishu_app_secret = "secret_xxx"

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
                    return httpx.Response(
                        200,
                        json={"code": 0, "tenant_access_token": "tenant_token"},
                    )
                if request.url.path.endswith("/drive/v1/files"):
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {
                                "files": [
                                    {
                                        "name": "room.jpg",
                                        "type": "file",
                                        "token": "image_token",
                                    }
                                ],
                                "has_more": False,
                            },
                        },
                    )
                raise AssertionError(f"Unexpected request: {request.url}")

            with tempfile.TemporaryDirectory() as directory:
                target_root = Path(directory) / "room_database"
                existing_path = target_root / "images" / "room.jpg"
                existing_path.parent.mkdir(parents=True, exist_ok=True)
                existing_path.write_bytes(b"old-image")

                client = FeishuClient(transport=httpx.MockTransport(handler))
                result = await client.sync_drive_media(
                    folder_token="root",
                    target_root=target_root,
                )

                self.assertEqual(result["downloaded"], [])
                self.assertEqual(result["skipped"], [str(existing_path)])
                self.assertEqual(existing_path.read_bytes(), b"old-image")
        finally:
            settings.feishu_app_id = previous_app_id
            settings.feishu_app_secret = previous_secret

    async def test_syncs_bitable_attachments_to_room_database(self) -> None:
        previous_app_id = settings.feishu_app_id
        previous_secret = settings.feishu_app_secret
        previous_app_token = settings.feishu_bitable_app_token
        previous_table_id = settings.feishu_bitable_table_id
        try:
            settings.feishu_app_id = "cli_xxx"
            settings.feishu_app_secret = "secret_xxx"
            settings.feishu_bitable_app_token = "app_token"
            settings.feishu_bitable_table_id = "table_id"

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
                    return httpx.Response(
                        200,
                        json={"code": 0, "tenant_access_token": "tenant_token"},
                    )
                self.assertEqual(request.headers["Authorization"], "Bearer tenant_token")
                if request.url.path.endswith(
                    "/bitable/v1/apps/app_token/tables/table_id/records/search"
                ):
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {
                                "items": [
                                    {
                                        "record_id": "rec_1",
                                        "fields": {
                                            "小区": "棠润府",
                                            "房号": "1-602A",
                                            "房源图片": [
                                                {
                                                    "name": "客厅.jpg",
                                                    "file_token": "image_token",
                                                    "url": "/attachment/image",
                                                }
                                            ],
                                            "房源视频": [
                                                {
                                                    "name": "看房视频.mp4",
                                                    "file_token": "video_token",
                                                    "url": "/attachment/video",
                                                }
                                            ],
                                            "说明": [
                                                {
                                                    "name": "说明.txt",
                                                    "file_token": "text_token",
                                                    "url": "/attachment/text",
                                                }
                                            ],
                                        },
                                    }
                                ],
                                "has_more": False,
                            },
                        },
                    )
                if request.url.path.endswith("/attachment/image"):
                    return httpx.Response(200, content=b"image")
                if request.url.path.endswith("/attachment/video"):
                    return httpx.Response(200, content=b"video")
                raise AssertionError(f"Unexpected request: {request.url}")

            with tempfile.TemporaryDirectory() as directory:
                target_root = Path(directory) / "room_database"
                client = FeishuClient(transport=httpx.MockTransport(handler))
                result = await client.sync_bitable_media(target_root=target_root)

                self.assertEqual(len(result["downloaded"]), 2)
                self.assertEqual(result["skipped"], ["说明.txt"])
                self.assertEqual(
                    (target_root / "images" / "棠润府-1-602A" / "客厅.jpg").read_bytes(),
                    b"image",
                )
                self.assertEqual(
                    (target_root / "video" / "棠润府-1-602A" / "看房视频.mp4").read_bytes(),
                    b"video",
                )
        finally:
            settings.feishu_app_id = previous_app_id
            settings.feishu_app_secret = previous_secret
            settings.feishu_bitable_app_token = previous_app_token
            settings.feishu_bitable_table_id = previous_table_id

    async def test_exports_sheet_as_xlsx(self) -> None:
        previous_app_id = settings.feishu_app_id
        previous_secret = settings.feishu_app_secret
        previous_sheet_token = settings.feishu_inventory_sheet_token
        try:
            settings.feishu_app_id = "cli_xxx"
            settings.feishu_app_secret = "secret_xxx"
            settings.feishu_inventory_sheet_token = "sheet_token"

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
                    return httpx.Response(
                        200,
                        json={"code": 0, "tenant_access_token": "tenant_token"},
                    )
                self.assertEqual(request.headers["Authorization"], "Bearer tenant_token")
                if request.url.path.endswith("/drive/v1/export_tasks"):
                    return httpx.Response(200, json={"code": 0, "data": {"ticket": "ticket_1"}})
                if request.url.path.endswith("/drive/v1/export_tasks/ticket_1"):
                    self.assertEqual(request.url.params.get("token"), "sheet_token")
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {
                                "result": {
                                    "job_status": "success",
                                    "file_token": "export_file",
                                }
                            },
                        },
                    )
                if request.url.path.endswith("/drive/v1/export_tasks/file/export_file/download"):
                    return httpx.Response(200, content=b"xlsx-bytes")
                raise AssertionError(f"Unexpected request: {request.url}")

            with tempfile.TemporaryDirectory() as directory:
                target_path = Path(directory) / "inventory.xlsx"
                client = FeishuClient(transport=httpx.MockTransport(handler))
                await client.export_sheet_xlsx(target_path=target_path)

                self.assertEqual(target_path.read_bytes(), b"xlsx-bytes")
        finally:
            settings.feishu_app_id = previous_app_id
            settings.feishu_app_secret = previous_secret
            settings.feishu_inventory_sheet_token = previous_sheet_token

    async def test_inventory_image_syncer_renders_only_when_sheet_changes(self) -> None:
        class FakeFeishuClient:
            def __init__(self) -> None:
                self.version = "old"
                self.exports = 0

            async def read_spreadsheet_values(self, *, spreadsheet_token: str) -> dict:
                self.exports += 1
                revision = 1 if self.version == "old" else 2
                return {
                    "sheet_id": "sheet_1",
                    "title": "总",
                    "range": "sheet_1!A1:B2",
                    "revision": revision,
                    "values": [
                        ["小区", "房号"],
                        ["星桥", self.version],
                    ],
                }

            async def export_sheet_xlsx(self, *, sheet_token: str, target_path: Path) -> Path:
                target_path.write_bytes(b"xlsx")
                return target_path

        class FakeInventoryImageSyncer(InventoryImageSyncer):
            def render_xlsx_to_inventory_images_from_values(
                self,
                xlsx_path: Path,
                values: list[list],
            ):
                return self.render_values_to_inventory_images(values)

        previous_sheet_token = settings.feishu_inventory_sheet_token
        previous_room_database_path = settings.room_database_path
        previous_image_glob = settings.inventory_image_glob
        previous_image_path = settings.inventory_image_path
        previous_state_path = settings.inventory_image_sync_state_path
        previous_check_seconds = settings.feishu_inventory_sheet_check_seconds
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "room_database"
                settings.feishu_inventory_sheet_token = "sheet_token"
                settings.room_database_path = root
                settings.inventory_image_glob = "room_database/inventory_*.png"
                settings.inventory_image_path = root / "inventory.png"
                settings.inventory_image_sync_state_path = Path(directory) / "state.json"
                settings.feishu_inventory_sheet_check_seconds = 0

                fake_client = FakeFeishuClient()
                syncer = FakeInventoryImageSyncer(client=fake_client)
                first_result = await syncer.refresh_if_changed()
                second_result = await syncer.refresh_if_changed()
                fake_client.version = "new"
                third_result = await syncer.refresh_if_changed()

                self.assertTrue(first_result["changed"])
                self.assertFalse(second_result["changed"])
                self.assertEqual(second_result["reason"], "unchanged")
                self.assertTrue(third_result["changed"])
                self.assertEqual(fake_client.exports, 3)
                self.assertTrue((root / "inventory_01.png").is_file())
                self.assertEqual(
                    sorted(path.name for path in root.glob("inventory_*.png")),
                    ["inventory_01.png"],
                )
        finally:
            settings.feishu_inventory_sheet_token = previous_sheet_token
            settings.room_database_path = previous_room_database_path
            settings.inventory_image_glob = previous_image_glob
            settings.inventory_image_path = previous_image_path
            settings.inventory_image_sync_state_path = previous_state_path
            settings.feishu_inventory_sheet_check_seconds = previous_check_seconds

    def test_inventory_image_render_ignores_styled_blank_cells(self) -> None:
        from openpyxl.styles import PatternFill
        from PIL import Image

        previous_room_database_path = settings.room_database_path
        previous_image_glob = settings.inventory_image_glob
        previous_image_path = settings.inventory_image_path
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "room_database"
                xlsx_path = Path(directory) / "inventory.xlsx"
                settings.room_database_path = root
                settings.inventory_image_glob = "room_database/inventory_*.png"
                settings.inventory_image_path = root / "inventory.png"

                workbook = Workbook()
                sheet = workbook.active
                sheet["A1"] = "小区"
                sheet["B1"] = "房号"
                sheet["A2"] = "星桥"
                sheet["B2"] = "1-101"
                sheet["Z200"].fill = PatternFill(
                    fill_type="solid",
                    fgColor="FFFF00",
                )
                workbook.save(xlsx_path)

                result = InventoryImageSyncer().render_xlsx_to_inventory_images(xlsx_path)

                self.assertEqual(result.rows, 2)
                self.assertEqual(result.columns, 2)
                with Image.open(result.paths[0]) as image:
                    width, height = image.size
                self.assertLess(width, 400)
                self.assertLess(height, 120)
        finally:
            settings.room_database_path = previous_room_database_path
            settings.inventory_image_glob = previous_image_glob
            settings.inventory_image_path = previous_image_path

    def test_inventory_image_render_uses_csv_content_bounds(self) -> None:
        from PIL import Image

        previous_room_database_path = settings.room_database_path
        previous_image_glob = settings.inventory_image_glob
        previous_image_path = settings.inventory_image_path
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "room_database"
                csv_path = Path(directory) / "inventory.csv"
                settings.room_database_path = root
                settings.inventory_image_glob = "room_database/inventory_*.png"
                settings.inventory_image_path = root / "inventory.png"
                csv_path.write_text(
                    "\n\n,,\n,小区,房号,,\n,星桥,1-101,,\n,,,,\n",
                    encoding="utf-8-sig",
                )

                result = InventoryImageSyncer().render_csv_to_inventory_images(csv_path)

                self.assertEqual(result.rows, 2)
                self.assertEqual(result.columns, 2)
                with Image.open(result.paths[0]) as image:
                    width, height = image.size
                self.assertLess(width, 400)
                self.assertLess(height, 120)
        finally:
            settings.room_database_path = previous_room_database_path
            settings.inventory_image_glob = previous_image_glob
            settings.inventory_image_path = previous_image_path


if __name__ == "__main__":
    unittest.main()
