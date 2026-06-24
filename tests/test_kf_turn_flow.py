from app.services.kf_turn_flow import RagStageTimer, merge_timing_payload


def test_rag_stage_timer_accumulates_repeated_stage() -> None:
    timer = RagStageTimer()

    with timer.stage("rewrite_intent"):
        pass
    with timer.stage("rewrite_intent"):
        pass

    payload = timer.snapshot()

    assert payload["total_ms"] >= 0
    assert payload["stage_counts"]["rewrite_intent"] == 2
    assert payload["stages_ms"]["rewrite_intent"] >= 0


def test_merge_timing_payload_combines_stages_and_counts() -> None:
    merged = merge_timing_payload(
        {
            "total_ms": 10,
            "stages_ms": {"rewrite_intent": 3, "send": 1},
            "stage_counts": {"rewrite_intent": 1, "send": 1},
        },
        {
            "total_ms": 8,
            "stages_ms": {"rewrite_intent": 2, "tool_execution": 4},
            "stage_counts": {"rewrite_intent": 2, "tool_execution": 1},
        },
    )

    assert merged["total_ms"] == 10
    assert merged["stages_ms"]["rewrite_intent"] == 5
    assert merged["stages_ms"]["tool_execution"] == 4
    assert merged["stage_counts"]["rewrite_intent"] == 3
