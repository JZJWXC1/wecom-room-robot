from typing import Any

from app.config import settings
from app.services.feishu_base import MAX_SHEET_SYNC_COLUMNS, MAX_SHEET_SYNC_ROWS


class FeishuSheetMixin:
    async def list_spreadsheet_sheets(
        self,
        *,
        spreadsheet_token: str | None = None,
    ) -> list[dict[str, Any]]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        data = await self._request_json(
            "GET",
            f"/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        )
        return list((data.get("data") or {}).get("sheets") or [])

    async def read_spreadsheet_values(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        sheets = await self.list_spreadsheet_sheets(spreadsheet_token=spreadsheet_token)
        sheet = self._select_sheet(sheets, sheet_id=sheet_id)
        selected_sheet_id = str(sheet.get("sheet_id") or "")
        if not selected_sheet_id:
            raise RuntimeError(f"Feishu sheet id is empty: {sheet}")
        grid = sheet.get("grid_properties") or {}
        row_count = max(1, min(int(grid.get("row_count") or 200), MAX_SHEET_SYNC_ROWS))
        column_count = max(1, min(int(grid.get("column_count") or 20), MAX_SHEET_SYNC_COLUMNS))
        end_column = self._column_letter(column_count)
        range_name = f"{selected_sheet_id}!A1:{end_column}{row_count}"
        data = await self._request_json(
            "GET",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_name}",
        )
        value_range = (data.get("data") or {}).get("valueRange") or {}
        values = value_range.get("values") or []
        return {
            "sheet_id": selected_sheet_id,
            "title": sheet.get("title") or "",
            "range": value_range.get("range") or range_name,
            "revision": value_range.get("revision") or (data.get("data") or {}).get("revision"),
            "values": [
                [self._format_field_value(cell) for cell in row]
                for row in values
            ],
        }

    def _select_sheet(
        self,
        sheets: list[dict[str, Any]],
        *,
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        visible_sheets = [sheet for sheet in sheets if not sheet.get("hidden")]
        if sheet_id:
            for sheet in visible_sheets or sheets:
                if str(sheet.get("sheet_id") or "") == sheet_id:
                    return sheet
            raise ValueError(f"Feishu sheet id not found: {sheet_id}")
        if visible_sheets:
            return sorted(visible_sheets, key=lambda item: int(item.get("index") or 0))[0]
        if sheets:
            return sorted(sheets, key=lambda item: int(item.get("index") or 0))[0]
        raise RuntimeError("Feishu spreadsheet has no sheets")

    def _column_letter(self, column_number: int) -> str:
        letters = ""
        while column_number > 0:
            column_number, remainder = divmod(column_number - 1, 26)
            letters = chr(65 + remainder) + letters
        return letters or "A"

    async def write_spreadsheet_values(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        start_cell: str = "A1",
        values: list[list[Any]],
    ) -> dict[str, Any]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        if not values:
            raise ValueError("Spreadsheet values are required")
        sheets = await self.list_spreadsheet_sheets(spreadsheet_token=spreadsheet_token)
        sheet = self._select_sheet(sheets, sheet_id=sheet_id)
        selected_sheet_id = str(sheet.get("sheet_id") or "")
        if not selected_sheet_id:
            raise RuntimeError(f"Feishu sheet id is empty: {sheet}")
        row_count = len(values)
        column_count = max(len(row) for row in values) if values else 1
        start_column = "".join(ch for ch in start_cell if ch.isalpha()).upper() or "A"
        start_row_text = "".join(ch for ch in start_cell if ch.isdigit()) or "1"
        start_row = int(start_row_text)
        start_index = self._column_number(start_column)
        end_column = self._column_letter(start_index + column_count - 1)
        end_row = start_row + row_count - 1
        range_name = f"{selected_sheet_id}!{start_column}{start_row}:{end_column}{end_row}"
        data = await self._request_json(
            "PUT",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
            json={"valueRange": {"range": range_name, "values": values}},
        )
        return dict(data.get("data") or data)

    async def insert_spreadsheet_rows(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        start_row: int,
        count: int,
        inherit_style: str = "BEFORE",
    ) -> dict[str, Any]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        if start_row < 1:
            raise ValueError("Spreadsheet start row must be 1 or greater")
        if count < 1:
            raise ValueError("Spreadsheet row count must be 1 or greater")
        inherit_style = inherit_style.upper()
        if inherit_style not in {"BEFORE", "AFTER"}:
            raise ValueError("Spreadsheet inherit_style must be BEFORE or AFTER")
        sheets = await self.list_spreadsheet_sheets(spreadsheet_token=spreadsheet_token)
        sheet = self._select_sheet(sheets, sheet_id=sheet_id)
        selected_sheet_id = str(sheet.get("sheet_id") or "")
        if not selected_sheet_id:
            raise RuntimeError(f"Feishu sheet id is empty: {sheet}")
        start_index = start_row - 1
        data = await self._request_json(
            "POST",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/insert_dimension_range",
            json={
                "dimension": {
                    "sheetId": selected_sheet_id,
                    "majorDimension": "ROWS",
                    "startIndex": start_index,
                    "endIndex": start_index + count,
                },
                "inheritStyle": inherit_style,
            },
        )
        return dict(data.get("data") or data)

    async def delete_spreadsheet_rows(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        start_row: int,
        count: int,
    ) -> dict[str, Any]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        if start_row < 1:
            raise ValueError("Spreadsheet start row must be 1 or greater")
        if count < 1:
            raise ValueError("Spreadsheet row count must be 1 or greater")
        sheets = await self.list_spreadsheet_sheets(spreadsheet_token=spreadsheet_token)
        sheet = self._select_sheet(sheets, sheet_id=sheet_id)
        selected_sheet_id = str(sheet.get("sheet_id") or "")
        if not selected_sheet_id:
            raise RuntimeError(f"Feishu sheet id is empty: {sheet}")
        start_index = start_row - 1
        data = await self._request_json(
            "DELETE",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/dimension_range",
            json={
                "dimension": {
                    "sheetId": selected_sheet_id,
                    "majorDimension": "ROWS",
                    "startIndex": start_index,
                    "endIndex": start_index + count,
                }
            },
        )
        return dict(data.get("data") or data)

    async def update_spreadsheet_row_height(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        start_row: int,
        end_row: int,
        height_px: int,
    ) -> dict[str, Any]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        if start_row < 1:
            raise ValueError("Spreadsheet start row must be 1 or greater")
        if end_row < start_row:
            raise ValueError("Spreadsheet end row must be greater than or equal to start row")
        if height_px < 1:
            raise ValueError("Spreadsheet row height must be 1 or greater")
        sheets = await self.list_spreadsheet_sheets(spreadsheet_token=spreadsheet_token)
        sheet = self._select_sheet(sheets, sheet_id=sheet_id)
        selected_sheet_id = str(sheet.get("sheet_id") or "")
        if not selected_sheet_id:
            raise RuntimeError(f"Feishu sheet id is empty: {sheet}")
        data = await self._request_json(
            "PUT",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/dimension_range",
            json={
                "dimension": {
                    "sheetId": selected_sheet_id,
                    "majorDimension": "ROWS",
                    "startIndex": start_row,
                    "endIndex": end_row,
                },
                "dimensionProperties": {
                    "fixedSize": height_px,
                },
            },
        )
        return dict(data.get("data") or data)

    async def merge_spreadsheet_cells(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        range_name: str,
        merge_type: str = "MERGE_ALL",
    ) -> dict[str, Any]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        sheets = await self.list_spreadsheet_sheets(spreadsheet_token=spreadsheet_token)
        sheet = self._select_sheet(sheets, sheet_id=sheet_id)
        selected_sheet_id = str(sheet.get("sheet_id") or "")
        if not selected_sheet_id:
            raise RuntimeError(f"Feishu sheet id is empty: {sheet}")
        qualified_range = range_name if "!" in range_name else f"{selected_sheet_id}!{range_name}"
        data = await self._request_json(
            "POST",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/merge_cells",
            json={"range": qualified_range, "mergeType": merge_type},
        )
        return dict(data.get("data") or data)

    async def batch_update_spreadsheet_styles(
        self,
        *,
        updates: list[dict[str, Any]],
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        if not updates:
            raise ValueError("Spreadsheet style updates are required")
        sheets = await self.list_spreadsheet_sheets(spreadsheet_token=spreadsheet_token)
        sheet = self._select_sheet(sheets, sheet_id=sheet_id)
        selected_sheet_id = str(sheet.get("sheet_id") or "")
        if not selected_sheet_id:
            raise RuntimeError(f"Feishu sheet id is empty: {sheet}")
        data = []
        for update in updates:
            ranges = [
                str(range_name if "!" in range_name else f"{selected_sheet_id}!{range_name}")
                for range_name in (update.get("ranges") or [])
            ]
            if not ranges:
                continue
            style = dict(update.get("style") or {})
            if not style:
                continue
            style.setdefault("clean", False)
            data.append({"ranges": ranges, "style": style})
        if not data:
            raise ValueError("Spreadsheet style updates are empty")
        response = await self._request_json(
            "PUT",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/styles_batch_update",
            json={"data": data},
        )
        return dict(response.get("data") or response)

    def _column_number(self, column_letters: str) -> int:
        value = 0
        for char in column_letters.upper():
            if not ("A" <= char <= "Z"):
                continue
            value = value * 26 + (ord(char) - ord("A") + 1)
        return value or 1

    async def unmerge_spreadsheet_cells(
        self,
        *,
        spreadsheet_token: str | None = None,
        sheet_id: str | None = None,
        range_name: str,
    ) -> dict[str, Any]:
        spreadsheet_token = spreadsheet_token or settings.feishu_inventory_sheet_token
        if not spreadsheet_token:
            raise ValueError("Feishu spreadsheet token is required")
        sheets = await self.list_spreadsheet_sheets(spreadsheet_token=spreadsheet_token)
        sheet = self._select_sheet(sheets, sheet_id=sheet_id)
        selected_sheet_id = str(sheet.get("sheet_id") or "")
        if not selected_sheet_id:
            raise RuntimeError(f"Feishu sheet id is empty: {sheet}")
        qualified_range = range_name if "!" in range_name else f"{selected_sheet_id}!{range_name}"
        data = await self._request_json(
            "POST",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/unmerge_cells",
            json={"range": qualified_range},
        )
        return dict(data.get("data") or data)
