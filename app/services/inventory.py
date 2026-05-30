from io import BytesIO, StringIO
import base64
import mimetypes
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI
import pandas as pd

from app.config import settings
from app.services.config_check import is_missing_or_placeholder
from app.services.feishu import FeishuClient


GENERIC_QUERY_WORDS = (
    "看房视频",
    "房间视频",
    "内部视频",
    "视频",
    "房源",
    "房子",
    "房间",
    "还有什么",
    "有什么",
    "还有",
    "哪些",
    "哪个",
    "附近",
    "类似价格",
    "类似",
    "同价位",
    "差不多",
    "什么",
    "发一下",
    "发我",
    "发",
    "看一下",
    "看看",
    "在租",
    "有吗",
    "有没有",
    "现在",
    "目前",
    "的",
    "呢",
    "吗",
)


class InventoryService:
    def __init__(self) -> None:
        self._cache: pd.DataFrame | None = None
        self._image_text: str = ""
        self._last_error: str = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    async def refresh(self) -> pd.DataFrame:
        if settings.inventory_source == "local_image":
            self._image_text = await self._read_image_inventory_text()
            frame = pd.DataFrame()
            self._cache = frame
            self._last_error = ""
            return frame
        if settings.inventory_source == "local_cache":
            frame = self._read_cache()
            self._cache = frame
            self._last_error = ""
            return frame
        if settings.inventory_source == "feishu_bitable":
            try:
                frame = await FeishuClient().read_bitable_dataframe()
                frame = self._normalize(frame)
                self._save_cache(frame)
                self._cache = frame
                self._last_error = ""
                return frame
            except Exception as exc:
                self._last_error = str(exc)
                frame = self._read_cache()
                self._cache = frame
                return frame
        try:
            frame = await self._read_public_document(settings.kdocs_public_url)
            frame = self._normalize(frame)
            self._save_cache(frame)
            self._cache = frame
            self._last_error = ""
            return frame
        except Exception as exc:
            self._last_error = str(exc)
            frame = self._read_cache()
            self._cache = frame
            return frame

    async def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if settings.inventory_source == "local_image":
            if not self._image_text:
                await self.refresh()
            rows = self._parse_image_rows(self._image_text)
            text = self._normalize_query(query)
            scored: list[tuple[int, dict[str, Any]]] = []
            for row in rows:
                merged = " ".join(str(value).lower() for value in row.values())
                score = self._score_row(text, merged, row)
                if score > 0 or not text.strip():
                    scored.append((score, row))
            scored = self._filter_scored_rows(scored)
            return [row for _, row in scored[:limit]]
        frame = self._cache if self._cache is not None else await self.refresh()
        if frame.empty:
            return []
        text = query.lower()
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in frame.fillna("").to_dict(orient="records"):
            merged = " ".join(str(value).lower() for value in row.values())
            score = self._score_row(text, merged, row)
            if score > 0 or not text.strip():
                scored.append((score, row))
        scored = self._filter_scored_rows(scored)
        return [row for _, row in scored[:limit]]

    def _score_row(self, text: str, merged: str, row: dict[str, Any]) -> int:
        score = 0
        search_text = self._strip_generic_query_words(text)
        for token in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{2,}", search_text):
            if token.lower() in merged:
                score += 10 + len(token)
            for gram in self._char_grams(token.lower()):
                if gram in merged:
                    score += len(gram)
        for value in row.values():
            value_text = str(value).strip().lower()
            if len(value_text) >= 2 and value_text in search_text:
                score += 3
        return score

    def _filter_scored_rows(
        self, scored: list[tuple[int, dict[str, Any]]]
    ) -> list[tuple[int, dict[str, Any]]]:
        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score = scored[0][0]
        if best_score <= 0:
            return scored
        min_score = max(6, int(best_score * 0.6))
        return [item for item in scored if item[0] >= min_score]

    def format_rows(self, rows: list[dict[str, Any]], limit: int = 8) -> str:
        if not rows:
            return ""
        lines: list[str] = []
        for index, row in enumerate(rows[:limit], start=1):
            compact = "；".join(
                f"{key}:{value}" for key, value in row.items() if str(value).strip()
            )
            lines.append(f"{index}. {compact}")
        return "\n".join(lines)

    async def snapshot(self, limit: int = 40) -> str:
        if settings.inventory_source == "local_image":
            if not self._image_text:
                await self.refresh()
            return self._image_text or (
                "\u5f53\u524d\u4f7f\u7528\u672c\u5730\u623f\u6e90\u8868\u56fe\u7247\uff1a"
                f"{settings.inventory_image_path}\u3002"
                "\u5c1a\u672a\u63d0\u53d6\u6210\u7ed3\u6784\u5316\u5e93\u5b58\u6570\u636e\u3002"
            )
        frame = self._cache if self._cache is not None else await self.refresh()
        if frame.empty:
            return "\u5f53\u524d\u6ca1\u6709\u53ef\u7528\u623f\u6e90\u5e93\u5b58\u6570\u636e\u3002"
        records = frame.fillna("").head(limit).to_dict(orient="records")
        lines: list[str] = []
        for index, row in enumerate(records, start=1):
            compact = "\uff1b".join(
                f"{key}:{value}" for key, value in row.items() if str(value).strip()
            )
            lines.append(f"{index}. {compact}")
        return "\n".join(lines)

    async def _read_image_inventory_text(self) -> str:
        image_paths = self._image_paths()
        cache_path = settings.inventory_image_cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        image_signature = "|".join(f"{path.name}:{path.stat().st_mtime_ns}" for path in image_paths)
        cache_marker = f"<!-- model:{settings.dashscope_vision_model}; images:{image_signature} -->"

        if cache_path.exists() and image_paths:
            cache_text = cache_path.read_text(encoding="utf-8").strip()
            latest_image_mtime = max(path.stat().st_mtime for path in image_paths)
            if (
                cache_text
                and cache_text.startswith(cache_marker)
                and cache_path.stat().st_mtime >= latest_image_mtime
            ):
                return cache_text.replace(cache_marker, "", 1).strip()

        if not image_paths:
            raise FileNotFoundError(
                f"Inventory images not found: {settings.inventory_image_glob}"
            )

        if is_missing_or_placeholder(settings.dashscope_api_key):
            if cache_path.exists():
                return cache_path.read_text(encoding="utf-8").strip()
            return ""

        client = AsyncOpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_base_url,
        )
        prompt = (
            "你是专业的房源表格 OCR 和结构化转写助手。"
            "下面有多张连续的房源表截图，请按图片顺序逐行识别。"
            "请严格保留原始表格的列关系，不要总结、不要推理、不要改写。"
            "输出要求："
            "1. 用 Markdown 表格输出。"
            "2. 表头固定为：区域、小区、房号、户型、户型分类、押一付、押二付、密码、备注。"
            "3. 遇到黄色区域标题行，要把区域名称补到后续房源行的“区域”列，直到下一个区域标题。"
            "4. 合并单元格的小区名称要补齐到后续空白行。"
            "5. 每一条房源都要保留，不要只挑部分房源。"
            "6. 不确定的单元格写“看不清”，禁止猜测。"
            "7. 不要把押一付、押二付、密码、备注互相串列。"
            "8. 不要输出解释，只输出识别后的 Markdown 表格。"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image_path in image_paths:
            mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
            image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
            data_url = f"data:{mime_type};base64,{image_data}"
            content.append({"type": "image_url", "image_url": {"url": data_url}})

        response = await client.chat.completions.create(
            model=settings.dashscope_vision_model,
            messages=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
            temperature=0,
        )
        text = (response.choices[0].message.content or "").strip()
        if text:
            cache_path.write_text(f"{cache_marker}\n{text}", encoding="utf-8")
        return text

    def _image_paths(self) -> list:
        paths = sorted(settings.room_database_path.parent.glob(settings.inventory_image_glob))
        if not paths and settings.inventory_image_path.exists():
            paths = [settings.inventory_image_path]
        return [path for path in paths if path.is_file()]

    def _parse_image_rows(self, text: str) -> list[dict[str, str]]:
        headers = ["区域", "小区", "房号", "户型", "户型分类", "押一付", "押二付", "密码", "备注"]
        rows: list[dict[str, str]] = []
        current_area = ""
        current_community = ""

        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("|") or "---" in line:
                continue
            cells = [self._clean_markdown_cell(cell) for cell in line.strip("|").split("|")]
            if len(cells) < len(headers):
                continue
            cells = cells[: len(headers)]
            if cells == headers or cells[0] == "区域":
                continue

            row = dict(zip(headers, cells))
            if row["区域"]:
                current_area = row["区域"]
            if row["小区"]:
                current_community = row["小区"]
            if not row["房号"]:
                continue

            row["区域"] = row["区域"] or current_area
            row["小区"] = row["小区"] or current_community
            rows.append(row)
        return rows

    def _clean_markdown_cell(self, value: str) -> str:
        value = value.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
        value = re.sub(r"[*`]+", "", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    def _normalize_query(self, query: str) -> str:
        replacements = {
            "星期": "星桥",
            "星琪": "星桥",
            "星乔": "星桥",
            "小阳坝": "小洋坝",
            "小洋吧": "小洋坝",
        }
        text = query.lower()
        for source, target in replacements.items():
            text = text.replace(source, target)
        return text

    def _strip_generic_query_words(self, text: str) -> str:
        cleaned = text
        for word in GENERIC_QUERY_WORDS:
            cleaned = cleaned.replace(word, " ")
        return cleaned

    def _char_grams(self, text: str) -> list[str]:
        grams: list[str] = []
        for size in (4, 3, 2):
            grams.extend(text[index : index + size] for index in range(len(text) - size + 1))
        return list(dict.fromkeys(grams))

    async def _read_public_document(self, url: str) -> pd.DataFrame:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/125 Safari/537.36"
            ),
            "Accept": "text/csv,application/vnd.ms-excel,*/*",
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=40) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            content = response.content
            final_host = urlparse(str(response.url)).hostname or ""

        if final_host.endswith("account.kdocs.cn"):
            raise ValueError("KDocs public CSV redirected to login page")

        lowered_url = url.lower()
        if "f=csv" in lowered_url or lowered_url.endswith(".csv") or "text/csv" in content_type:
            return pd.read_csv(BytesIO(content))
        if lowered_url.endswith((".xlsx", ".xls")) or "spreadsheet" in content_type:
            return pd.read_excel(BytesIO(content))

        html = content.decode("utf-8", errors="ignore")
        tables = pd.read_html(StringIO(html))
        if not tables:
            raise ValueError("No table found in public KDocs page")
        return tables[0]

    def _normalize(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.dropna(how="all").copy()
        frame.columns = [str(column).strip() for column in frame.columns]
        column_aliases = {
            "押一付一": "押一付",
            "押二付一": "押二付",
        }
        frame = frame.rename(
            columns={column: column_aliases.get(column, column) for column in frame.columns}
        )
        frame = frame.loc[:, [column for column in frame.columns if column]]
        return frame

    def _save_cache(self, frame: pd.DataFrame) -> None:
        path = settings.inventory_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False, encoding="utf-8-sig")

    def _read_cache(self) -> pd.DataFrame:
        path = settings.inventory_cache_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame().to_csv(path, index=False, encoding="utf-8-sig")
            return pd.DataFrame()
        if path.stat().st_size == 0:
            return pd.DataFrame()
        try:
            return pd.read_csv(path)
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
