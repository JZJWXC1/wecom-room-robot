from dataclasses import dataclass, field
from typing import Any


@dataclass
class IncomingMessage:
    source: str
    user_id: str
    msg_type: str
    content: str = ""
    media_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoomMedia:
    room_id: str
    images: list[str]
    videos: list[str]


@dataclass
class ReplyPlan:
    text: str
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
