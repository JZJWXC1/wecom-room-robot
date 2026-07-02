from __future__ import annotations

import operator
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.config import settings


class KfLangGraphSmokeState(TypedDict, total=False):
    conversation_id: str
    user_text: str
    normalized_text: str
    route: str
    trace: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class KfLangGraphRuntimeConfig:
    enabled: bool
    checkpoint_path: Path
    smoke_thread_id: str

    @classmethod
    def from_settings(cls, app_settings: Any = settings) -> "KfLangGraphRuntimeConfig":
        return cls(
            enabled=bool(app_settings.kf_langgraph_enabled),
            checkpoint_path=Path(app_settings.kf_langgraph_checkpoint_path),
            smoke_thread_id=str(app_settings.kf_langgraph_smoke_thread_id).strip()
            or "kf-langgraph-smoke",
        )


def build_sqlite_checkpointer(path: Path | str) -> SqliteSaver:
    checkpoint_path = Path(path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    return SqliteSaver(connection)


def build_memory_checkpointer() -> InMemorySaver:
    return InMemorySaver()


def build_kf_langgraph_smoke_app(*, checkpointer: Any | None = None) -> Any:
    graph = StateGraph(KfLangGraphSmokeState)
    graph.add_node("rewrite_intent", _rewrite_intent)
    graph.add_node("planner", _planner)
    graph.add_edge(START, "rewrite_intent")
    graph.add_edge("rewrite_intent", "planner")
    graph.add_edge("planner", END)
    return graph.compile(checkpointer=checkpointer)


def invoke_kf_langgraph_smoke(
    user_text: str,
    *,
    conversation_id: str = "kf-langgraph-smoke",
    checkpointer: Any | None = None,
) -> KfLangGraphSmokeState:
    app = build_kf_langgraph_smoke_app(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": conversation_id}} if checkpointer else None
    state: KfLangGraphSmokeState = {
        "conversation_id": conversation_id,
        "user_text": user_text,
        "trace": [],
    }
    return app.invoke(state, config=config)


def _rewrite_intent(state: KfLangGraphSmokeState) -> KfLangGraphSmokeState:
    normalized = "".join(str(state.get("user_text", "")).split()).lower()
    return {
        "normalized_text": normalized,
        "trace": ["rewrite_intent"],
    }


def _planner(state: KfLangGraphSmokeState) -> KfLangGraphSmokeState:
    normalized = state.get("normalized_text", "")
    if any(keyword in normalized for keyword in ("房源表", "表发", "发一下表")):
        route = "send_inventory_table"
    elif any(keyword in normalized for keyword in ("视频", "笔记", "实拍")):
        route = "resolve_room_video"
    elif any(keyword in normalized for keyword in ("图片", "照片", "房间图")):
        route = "resolve_room_image"
    else:
        route = "agentic_rag_reply"
    return {
        "route": route,
        "trace": ["planner"],
    }
