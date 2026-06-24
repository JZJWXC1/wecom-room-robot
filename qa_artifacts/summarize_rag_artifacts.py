from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Any


def _texts(turn: dict[str, Any]) -> str:
    return " / ".join(str(item) for item in (turn.get("bot") or {}).get("texts") or [])


def _summaries(turn: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("stage")): item.get("summary") or {}
        for item in turn.get("stage_timings") or []
        if isinstance(item, dict)
    }


def _bad_reasons(turn: dict[str, Any]) -> list[str]:
    text = _texts(turn)
    reasons: list[str] = []
    if "最新房源表里暂时没查到" in text and "这个小区" in text:
        reasons.append("误把接话短语当小区")
    if "你确认一下小区名" in text:
        reasons.append("要求用户重复小区")
    if "暂时没查到完全匹配" in text:
        reasons.append("无匹配或约束过严")
    if turn.get("error"):
        reasons.append("执行错误")
    bot = turn.get("bot") or {}
    if not bot.get("texts") and not bot.get("images") and not bot.get("videos"):
        reasons.append("无客户可见输出")
    summaries = _summaries(turn)
    final = summaries.get("final_selfcheck") or {}
    if final.get("needs_planner_retry"):
        reasons.append("自检触发重试")
    return reasons


def summarize(files: list[Path]) -> dict[str, Any]:
    windows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        turns = data.get("turns") or []
        issue_count = 0
        for turn in turns:
            reasons = _bad_reasons(turn)
            if not reasons:
                continue
            issue_count += 1
            summaries = _summaries(turn)
            issues.append(
                {
                    "file": str(path),
                    "offset": data.get("offset"),
                    "turn": turn.get("turn"),
                    "user": turn.get("user"),
                    "reasons": reasons,
                    "rewrite": summaries.get("rewrite_intent") or {},
                    "planner": summaries.get("planner") or {},
                    "tools": summaries.get("tools") or {},
                    "selfcheck": summaries.get("final_selfcheck") or {},
                    "bot_text": _texts(turn),
                }
            )
        windows.append(
            {
                "file": str(path),
                "offset": data.get("offset"),
                "completed": data.get("completed"),
                "turn_count": len(turns),
                "issue_count": issue_count,
            }
        )
    return {"window_count": len(windows), "windows": windows, "issues": issues}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("patterns", nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    files: list[Path] = []
    for pattern in args.patterns:
        files.extend(Path(item) for item in glob.glob(pattern))
    files = sorted(set(files), key=lambda item: str(item))[-10:]
    report = summarize(files)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
