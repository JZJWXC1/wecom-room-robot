from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import time
from typing import Iterator


@dataclass
class RagStageTimer:
    """Collect per-turn Agentic RAG stage timings without affecting flow logic."""

    started_at: float = field(default_factory=time.perf_counter)
    _durations: dict[str, float] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        stage_name = str(name or "unknown").strip() or "unknown"
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            self._durations[stage_name] = self._durations.get(stage_name, 0.0) + elapsed
            self._counts[stage_name] = self._counts.get(stage_name, 0) + 1

    def snapshot(self) -> dict[str, object]:
        total = time.perf_counter() - self.started_at
        return {
            "total_ms": round(total * 1000, 2),
            "stages_ms": {
                key: round(value * 1000, 2)
                for key, value in sorted(self._durations.items())
            },
            "stage_counts": dict(sorted(self._counts.items())),
        }


def merge_timing_payload(*payloads: dict[str, object] | None) -> dict[str, object]:
    merged_stages: dict[str, float] = {}
    merged_counts: dict[str, int] = {}
    total_ms = 0.0
    for payload in payloads:
        if not payload:
            continue
        total_ms = max(total_ms, float(payload.get("total_ms") or 0.0))
        for key, value in (payload.get("stages_ms") or {}).items():
            merged_stages[str(key)] = merged_stages.get(str(key), 0.0) + float(value or 0.0)
        for key, value in (payload.get("stage_counts") or {}).items():
            merged_counts[str(key)] = merged_counts.get(str(key), 0) + int(value or 0)
    return {
        "total_ms": round(total_ms, 2),
        "stages_ms": {key: round(value, 2) for key, value in sorted(merged_stages.items())},
        "stage_counts": dict(sorted(merged_counts.items())),
    }
