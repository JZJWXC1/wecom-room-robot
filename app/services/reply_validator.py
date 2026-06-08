import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.services.config_check import is_missing_or_placeholder


MISSING_VIDEO_PATTERNS = (
    "暂时没找到这套的视频",
    "暂时没有这套的视频",
    "暂时没找到视频",
    "暂时没有视频",
    "目前没找到视频",
    "目前没有视频",
    "没有对应视频",
    "没找到对应视频",
    "视频暂时没有",
    "没视频",
)
MISSING_VIDEO_HINTS = (
    "暂时没素材",
    "暂时没有素材",
    "目前没素材",
    "目前没有素材",
    "暂时没挂上",
    "暂时没挂",
    "还没挂上",
    "还没挂",
    "没挂上",
    "没有挂上",
    "暂时没上传",
    "暂时没有上传",
    "还没上传",
    "没上传",
    "暂时没整理",
    "暂时没有整理",
    "还没整理出来",
    "没整理出来",
    "需要再确认下",
    "需要再确认一下",
    "需要人工再确认",
    "再确认一下素材",
    "确认一下素材",
)
SEND_VIDEO_PATTERNS = (
    "我把视频发你",
    "把视频发你",
    "马上发你",
    "直接发你",
    "已直接发送相关视频",
)
ROOM_IMAGE_PATTERNS = (
    "房间图片",
    "房源图片",
    "实拍图",
    "照片",
    "相片",
    "图片素材",
    "图片发你",
    "把图片发你",
    "把照片发你",
)
FALLBACK_TEXT = (
    "我这边再确认一下房源和素材，避免发错房间。"
    "你要先看房的话，可以联系 18758141785 / 13282125992 / 19941091943。"
)
ROOM_IMAGE_DISABLED_TEXT = (
    "房间图片这边不单独发送了，素材现在只发视频。"
    "你把小区和房号发我，我先查房源表；有视频的话我直接发你，"
    "也可以把房间详细信息发你。"
)


@dataclass
class ReplyValidationDraft:
    customer_text: str
    reply_text: str = ""
    inventory_rows: list[dict[str, Any]] = field(default_factory=list)
    reference_inventory_rows: list[dict[str, Any]] = field(default_factory=list)
    available_video_paths: list[Path] = field(default_factory=list)
    available_image_paths: list[Path] = field(default_factory=list)
    send_video_paths: list[Path] = field(default_factory=list)
    send_image_paths: list[Path] = field(default_factory=list)
    conversation_context: str = ""


@dataclass
class ReplyValidationResult:
    ok: bool
    reply_text: str
    problems: list[str] = field(default_factory=list)
    extra_video_paths: list[Path] = field(default_factory=list)
    extra_image_paths: list[Path] = field(default_factory=list)


class ReplyValidator:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.dashscope_api_key or "missing-key",
            base_url=settings.dashscope_base_url,
        )

    async def validate(self, draft: ReplyValidationDraft) -> ReplyValidationResult:
        result = self._hard_validate(draft)
        if not self._should_run_llm_review(draft, result):
            return result
        if is_missing_or_placeholder(settings.dashscope_api_key):
            return result
        try:
            return await asyncio.wait_for(
                self._llm_review(draft, result),
                timeout=8,
            )
        except Exception:
            return result

    def _hard_validate(self, draft: ReplyValidationDraft) -> ReplyValidationResult:
        reply_text = draft.reply_text.strip()
        problems: list[str] = []
        available_videos = _dedupe_paths(draft.available_video_paths + draft.send_video_paths)
        send_videos = _dedupe_paths(draft.send_video_paths)
        send_images = _dedupe_paths(draft.send_image_paths)
        reference_rows = draft.reference_inventory_rows or draft.inventory_rows

        disallowed_images = [path for path in send_images if not _is_inventory_image_path(path)]
        if disallowed_images:
            problems.append("除房源表外不允许发送房间图片素材")
            return ReplyValidationResult(
                ok=False,
                reply_text=ROOM_IMAGE_DISABLED_TEXT,
                problems=problems,
            )

        if reply_text and _claims_room_image_material(reply_text):
            problems.append("话术承诺或提到了房间图片素材，但当前策略只允许房源表 PNG 和视频")
            reply_text = _fix_room_image_text(reply_text)

        if (
            reply_text
            and _mentions_missing_video(reply_text)
            and available_videos
            and _available_video_matches_missing_claim(reply_text, available_videos)
        ):
            problems.append("话术说没有视频，但真实素材库存在视频")
            reply_text = _fix_missing_video_text(reply_text)

        if reply_text and _claims_will_send_video(reply_text) and not available_videos:
            problems.append("话术承诺发送视频，但真实素材库没有视频")
            reply_text = _fix_unavailable_video_text(reply_text)

        if send_videos:
            mismatched = _mismatched_video_labels(send_videos, draft.inventory_rows)
            if mismatched:
                problems.append("待发送视频和房源表命中房源不一致：" + "、".join(mismatched))
                return ReplyValidationResult(
                    ok=False,
                    reply_text=FALLBACK_TEXT,
                    problems=problems,
                )

        if reply_text and _claims_rented_out(reply_text) and draft.inventory_rows:
            problems.append("话术说房源表查不到，但房源表存在命中房源")
            reply_text = _fix_rented_out_text(reply_text)

        if reply_text:
            unknown_tokens = _unknown_positive_room_tokens(reply_text, reference_rows)
            if unknown_tokens:
                problems.append(
                    "话术说房源还在，但最新房源表没有这些房号：" + "、".join(unknown_tokens)
                )
                reply_text = _fix_unverified_available_text()

        extra_videos: list[Path] = []
        if problems and available_videos and not send_videos and not _claims_unavailable_after_fix(reply_text):
            extra_videos = available_videos

        return ReplyValidationResult(
            ok=not problems,
            reply_text=reply_text,
            problems=problems,
            extra_video_paths=extra_videos,
        )

    def _should_run_llm_review(
        self,
        draft: ReplyValidationDraft,
        hard_result: ReplyValidationResult,
    ) -> bool:
        return bool(hard_result.problems)

    async def _llm_review(
        self,
        draft: ReplyValidationDraft,
        hard_result: ReplyValidationResult,
    ) -> ReplyValidationResult:
        prompt = self._build_review_prompt(draft, hard_result)
        response = await self._client.chat.completions.create(
            model=settings.dashscope_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是租房客服回复的最终质检员。"
                        "只能根据输入里的房源表命中和真实素材列表判断。"
                        "只输出 JSON，不要解释。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        content = response.choices[0].message.content or "{}"
        data = _parse_json_object(content)
        if data.get("ok") is False:
            fixed_text = str(data.get("fixed_text") or "").strip()
            problems = [str(item) for item in data.get("problems") or [] if str(item).strip()]
            return ReplyValidationResult(
                ok=False,
                reply_text=fixed_text or hard_result.reply_text or FALLBACK_TEXT,
                problems=problems or hard_result.problems,
                extra_video_paths=hard_result.extra_video_paths,
                extra_image_paths=hard_result.extra_image_paths,
            )
        return hard_result

    def _build_review_prompt(
        self,
        draft: ReplyValidationDraft,
        hard_result: ReplyValidationResult,
    ) -> str:
        inventory_text = "\n".join(_format_row(row) for row in draft.inventory_rows) or "无"
        reference_inventory_text = (
            "\n".join(_format_row(row) for row in draft.reference_inventory_rows) or inventory_text
        )
        available_videos = "\n".join(str(path) for path in draft.available_video_paths) or "无"
        send_videos = "\n".join(str(path) for path in draft.send_video_paths) or "无"
        available_images = "\n".join(str(path) for path in draft.available_image_paths) or "无"
        send_images = "\n".join(str(path) for path in draft.send_image_paths) or "无"
        problems = "\n".join(hard_result.problems) or "无"
        return f"""
客户消息：
{draft.customer_text}

最近上下文：
{draft.conversation_context or "无"}

房源表命中：
{inventory_text}

最新房源表可用行：
{reference_inventory_text}

真实可用视频素材：
{available_videos}

即将发送视频：
{send_videos}

真实可用房源表图片素材（房间图片不允许发送）：
{available_images}

即将发送图片：
{send_images}

即将发送话术：
{hard_result.reply_text or draft.reply_text}

程序硬校验问题：
{problems}

请检查：
1. 话术正向提到“还在、在租、有房、可以看”的具体房号，必须存在于最新房源表可用行里。
2. 话术说有/没有视频，是否符合真实视频素材列表。
3. 即将发送的视频是否和客户询问房源一致。
4. 除房源表 PNG 外，不允许承诺或发送房间图片、照片、实拍图。

只输出 JSON：
{{"ok": true, "problems": [], "fixed_text": ""}}
或：
{{"ok": false, "problems": ["问题"], "fixed_text": "修正后话术"}}
"""


def _mentions_missing_video(text: str) -> bool:
    if any(pattern in text for pattern in MISSING_VIDEO_PATTERNS):
        return True
    return "视频" in text and any(hint in text for hint in MISSING_VIDEO_HINTS)


def _claims_will_send_video(text: str) -> bool:
    return "视频" in text and any(pattern in text for pattern in SEND_VIDEO_PATTERNS)


def _claims_rented_out(text: str) -> bool:
    return any(pattern in text for pattern in ("已经租掉", "应该已经租掉", "房源表里查不到"))


def _claims_available_room(text: str) -> bool:
    return any(
        pattern in text
        for pattern in (
            "还在",
            "在租",
            "有的",
            "有一套",
            "有两套",
            "还有一套",
            "还有两套",
            "目前就",
            "目前有",
            "我这边看到",
            "我这边查到",
            "房源表里还在",
            "可以直接看",
            "门没锁",
        )
    )


def _unknown_positive_room_tokens(text: str, rows: list[dict[str, Any]]) -> list[str]:
    if not _claims_available_room(text):
        return []
    reply_tokens = set(_room_tokens(text))
    if not reply_tokens:
        return []
    row_tokens: set[str] = set()
    for row in rows:
        row_tokens.update(_room_tokens(_row_label(row)))
    if not row_tokens:
        return sorted(reply_tokens)
    return sorted(token for token in reply_tokens if token not in row_tokens)


def _fix_unverified_available_text() -> str:
    return (
        "这套最新房源表里查不到了，可能已经租掉了。"
        "你要看其他房源的话，可以发小区、预算或户型，我按最新房源表帮你查。"
    )


def _claims_room_image_material(text: str) -> bool:
    if not any(pattern in text for pattern in ROOM_IMAGE_PATTERNS):
        return False
    if "房源表" in text or "表格" in text:
        text_without_inventory = re.sub(r"房源表.{0,8}图片|表格.{0,8}图片|图片.{0,8}房源表", "", text)
        return any(pattern in text_without_inventory for pattern in ROOM_IMAGE_PATTERNS)
    return True


def _fix_room_image_text(text: str) -> str:
    fixed = text
    replacements = (
        ("图片或视频", "视频"),
        ("图片/视频", "视频"),
        ("图片、视频", "视频"),
        ("图片和视频", "视频"),
        ("照片或视频", "视频"),
        ("照片/视频", "视频"),
        ("实拍图或视频", "视频"),
    )
    for source, target in replacements:
        fixed = fixed.replace(source, target)
    if _claims_room_image_material(fixed):
        return ROOM_IMAGE_DISABLED_TEXT
    return fixed


def _claims_unavailable_after_fix(text: str) -> bool:
    return _mentions_missing_video(text) or _claims_rented_out(text)


def _available_video_matches_missing_claim(text: str, available_videos: list[Path]) -> bool:
    missing_tokens = set()
    for sentence in re.split(r"[。！？!?\n]", text):
        if _mentions_missing_video(sentence):
            missing_tokens.update(_room_tokens(sentence))
    if not missing_tokens:
        return True
    for path in available_videos:
        label = f"{path.parent.name} {path.stem}"
        if missing_tokens.intersection(_room_tokens(label)):
            return True
    return False


def _fix_missing_video_text(text: str) -> str:
    fixed = _remove_missing_video_sentences(text)
    suffix = "我这边有对应视频，马上发你。"
    if not fixed:
        return suffix
    if suffix in fixed:
        return fixed
    return fixed.rstrip("。") + "。\n" + suffix


def _remove_missing_video_sentences(text: str) -> str:
    paragraphs: list[str] = []
    for line in text.splitlines():
        segments = re.findall(r"[^。！？!?\n]+[。！？!?]?", line)
        kept = [segment for segment in segments if not _mentions_missing_video(segment)]
        if kept:
            paragraphs.append("".join(kept).strip())
    return "\n".join(paragraph for paragraph in paragraphs if paragraph).strip()


def _fix_unavailable_video_text(text: str) -> str:
    if _mentions_missing_video(text):
        return text
    return re.sub(
        r"我把视频发你|把视频发你|马上发你|直接发你|已直接发送相关视频",
        "视频暂时没有，需要人工再确认一下素材",
        text,
    )


def _fix_rented_out_text(text: str) -> str:
    if "视频" in text:
        return "这套房源表里还在，我再确认一下对应视频素材。"
    return "这套房源表里还在，我再确认一下最新细节。"


def _mismatched_video_labels(video_paths: list[Path], rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    row_infos: list[tuple[set[str], set[str]]] = []
    for row in rows:
        label = _row_label(row)
        row_tokens = set(_room_tokens(label))
        community = _first_row_value(row, "小区", "社区", "楼盘")
        row_infos.append((row_tokens, _community_match_tokens(community)))
    if not any(tokens for tokens, _ in row_infos):
        return []
    mismatched = []
    for path in video_paths:
        label = path.parent.name or path.stem
        tokens = set(_room_tokens(label))
        path_norm = _normalize_label_text(label)
        path_has_chinese = bool(re.search(r"[\u4e00-\u9fff]", label))
        matches_row = any(
            tokens.intersection(row_tokens)
            and (not community_tokens or any(token in path_norm for token in community_tokens) or not path_has_chinese)
            for row_tokens, community_tokens in row_infos
        )
        if tokens and not matches_row:
            mismatched.append(label)
    return mismatched


def _room_tokens(text: str) -> list[str]:
    expanded: list[str] = []
    for token in re.findall(r"\d+(?:[-－—]\d+)+(?:[-－—]?[A-Za-z])?", text):
        expanded.append(token)
        expanded.extend(_room_token_aliases(token))
    return [_normalize_label_text(token) for token in dict.fromkeys(expanded)]


def _room_token_aliases(token: str) -> list[str]:
    normalized = re.sub(r"[－—]", "-", token)
    aliases: list[str] = []
    if re.search(r"-1$", normalized):
        aliases.append(re.sub(r"-1$", "A", normalized))
    if re.search(r"(?i)A$", normalized):
        aliases.append(f"{normalized[:-1]}-1")
    return aliases


def _row_label(row: dict[str, Any]) -> str:
    community = _first_row_value(row, "小区", "社区", "楼盘")
    room_no = _first_row_value(row, "房号", "房间号", "room_id", "RoomID", "编号")
    return "".join(part for part in (community, room_no) if part)


def _normalize_label_text(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", text).lower()


def _community_match_tokens(text: str) -> set[str]:
    tokens = {
        _normalize_label_text(token)
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", text)
        if len(_normalize_label_text(token)) >= 2
    }
    normalized = _normalize_label_text(text)
    if normalized:
        tokens.add(normalized)
    return tokens


def _first_row_value(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return ""


def _format_row(row: dict[str, Any]) -> str:
    return "；".join(f"{key}:{value}" for key, value in row.items() if str(value).strip())


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _is_inventory_image_path(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith("inventory_") or name == settings.inventory_image_path.name.lower():
        return True
    try:
        resolved = path.resolve()
        if settings.inventory_image_path.exists() and resolved == settings.inventory_image_path.resolve():
            return True
        glob_path = Path(settings.inventory_image_glob)
        if glob_path.is_absolute():
            if any(char in glob_path.name for char in "*?[]"):
                candidates = glob_path.parent.glob(glob_path.name)
            else:
                candidates = [glob_path]
        else:
            candidates = settings.room_database_path.parent.glob(settings.inventory_image_glob)
        return any(resolved == item.resolve() for item in candidates)
    except Exception:
        return False


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
