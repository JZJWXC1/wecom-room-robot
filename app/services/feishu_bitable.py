from typing import Any

import pandas as pd

from app.config import settings


class FeishuBitableMixin:
    async def list_bitable_tables(
        self,
        *,
        app_token: str | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        app_token = app_token or settings.feishu_bitable_app_token
        if not app_token:
            raise ValueError("Feishu bitable app token is required")

        tables: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = await self._request_json(
                "GET",
                f"/bitable/v1/apps/{app_token}/tables",
                params=params,
            )
            payload = data.get("data") or {}
            tables.extend(payload.get("items") or payload.get("tables") or [])
            if not payload.get("has_more"):
                break
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                break
        return tables

    async def list_bitable_records(
        self,
        *,
        app_token: str | None = None,
        table_id: str | None = None,
        view_id: str | None = None,
        page_size: int = 500,
    ) -> list[dict[str, Any]]:
        app_token = app_token or settings.feishu_bitable_app_token
        table_id = table_id or settings.feishu_bitable_table_id
        view_id = view_id if view_id is not None else settings.feishu_bitable_view_id
        if not app_token or not table_id:
            raise ValueError("Feishu bitable app token and table id are required")

        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            body: dict[str, Any] = {}
            if view_id:
                body["view_id"] = view_id
            data = await self._request_json(
                "POST",
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
                params=params,
                json=body,
            )
            payload = data.get("data") or {}
            records.extend(payload.get("items") or payload.get("records") or [])
            if not payload.get("has_more"):
                break
            page_token = str(payload.get("page_token") or "")
            if not page_token:
                break
        return records

    async def create_bitable_record(
        self,
        *,
        app_token: str,
        table_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        if not app_token or not table_id:
            raise ValueError("Feishu bitable app token and table id are required")
        data = await self._request_json(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            json={"fields": fields},
        )
        return dict((data.get("data") or {}).get("record") or data.get("data") or {})

    async def update_bitable_record(
        self,
        *,
        app_token: str | None = None,
        table_id: str | None = None,
        record_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        app_token = app_token or settings.feishu_bitable_app_token
        table_id = table_id or settings.feishu_bitable_table_id
        if not app_token or not table_id or not record_id:
            raise ValueError("Feishu bitable app token, table id and record id are required")
        return await self._request_json(
            "PUT",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            json={"fields": fields},
        )

    async def read_bitable_dataframe(self) -> pd.DataFrame:
        records = await self.list_bitable_records()
        rows = [self._record_to_row(record) for record in records]
        return pd.DataFrame(rows)

    async def list_docx_blocks(
        self,
        *,
        document_id: str,
        page_size: int = 500,
    ) -> list[dict[str, Any]]:
        if not document_id:
            raise ValueError("Feishu docx document id is required")
        blocks: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {
                "page_size": page_size,
                "document_revision_id": -1,
            }
            if page_token:
                params["page_token"] = page_token
            data = await self._request_json(
                "GET",
                f"/docx/v1/documents/{document_id}/blocks",
                params=params,
            )
            payload = data.get("data") or {}
            blocks.extend(payload.get("items") or payload.get("blocks") or [])
            if not payload.get("has_more"):
                break
            page_token = str(payload.get("page_token") or payload.get("next_page_token") or "")
            if not page_token:
                break
        return blocks

    def _record_to_row(self, record: dict[str, Any]) -> dict[str, str]:
        fields = record.get("fields") or {}
        return {
            str(key).strip(): self._format_field_value(value)
            for key, value in fields.items()
            if str(key).strip()
        }

    def _format_field_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool)):
            return str(value).strip()
        if isinstance(value, list):
            return " ".join(
                item for item in (self._format_field_value(item) for item in value) if item
            ).strip()
        if isinstance(value, dict):
            preferred_keys = (
                "text",
                "name",
                "link",
                "url",
                "email",
                "phone",
                "en_us",
                "zh_cn",
            )
            values = [
                self._format_field_value(value[key])
                for key in preferred_keys
                if key in value
            ]
            if values:
                return " ".join(item for item in values if item).strip()
            return " ".join(
                item for item in (self._format_field_value(item) for item in value.values()) if item
            ).strip()
        return str(value).strip()

    def _extract_attachments(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        return self.extract_attachments(record)

    def extract_attachments(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        self._collect_attachments(record.get("fields") or {}, attachments)
        return attachments

    def _collect_attachments(self, value: Any, attachments: list[dict[str, Any]]) -> None:
        if isinstance(value, dict):
            if value.get("type") == "mention" or value.get("mentionType") or value.get("realMentionType"):
                return
            if value.get("file_token") or value.get("token") or value.get("fileKey"):
                attachments.append(value)
                return
            for item in value.values():
                self._collect_attachments(item, attachments)
        elif isinstance(value, list):
            for item in value:
                self._collect_attachments(item, attachments)
