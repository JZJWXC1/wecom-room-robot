# 入站幂等回归(回调重复投递防线):
# 生产实证 2026-07-04 16:01,企业微信对同一事件重复投递,同一 msgid 的
# "房源表"请求被完整处理两次,房源表图片对客重复外发。本文件守护前两层
# 防线:(1) sync_messages 整个"读游标-拉取-存游标-认领"临界区串行化,
# 并按 msgid 认领窗口幂等去重;(2) _restart_kf_turn 收到纯重复投递
# (无新增 msgid)且轮次在跑时不重启、不取消、不重放。
# 第三层(出站台账 msgid 域防重)见 tests/test_kf_outbox_msgid_scope_guard.py。
from __future__ import annotations

import asyncio
from pathlib import Path

from app import main
from app.services.wecom_kf import WeComKfClient, WeComKfStateStore


def _store(tmp_path: Path, **kwargs) -> WeComKfStateStore:
    return WeComKfStateStore(path=tmp_path / "state.json", **kwargs)


def test_claim_many_grants_once_within_ttl_and_dedups_same_batch(tmp_path) -> None:
    store = _store(tmp_path)
    granted = store.claim_many(["m1", "m2", "m1"], ttl_seconds=300, now=1000.0)
    assert granted == {"m1", "m2"}

    # 窗口内重推不再放行;窗口过期后放行(轮次失败时消息不永久丢失)。
    assert store.claim_many(["m1"], ttl_seconds=300, now=1200.0) == set()
    assert store.claim_many(["m1"], ttl_seconds=300, now=1300.0) == {"m1"}


def test_claim_many_rejects_processed_and_mark_processed_clears_inflight(tmp_path) -> None:
    store = _store(tmp_path)
    store.mark_processed("m-done")
    assert store.claim_many(["m-done"], ttl_seconds=300, now=1000.0) == set()

    assert store.claim_many(["m-live"], ttl_seconds=300, now=1000.0) == {"m-live"}
    store.mark_processed("m-live")
    assert store.load().get("inflight_msgids") == {}
    # 转正后即便认领窗口已过也不再放行。
    assert store.claim_many(["m-live"], ttl_seconds=300, now=9999.0) == set()


def test_claim_many_ignores_empty_msgids(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.claim_many(["", "  "], ttl_seconds=300, now=1000.0) == set()
    assert store.load().get("inflight_msgids") == {}


def test_state_file_roundtrip_preserves_inflight_claims(tmp_path) -> None:
    store = _store(tmp_path)
    store.claim_many(["m1"], ttl_seconds=300, now=1000.0)
    store.save_cursor("cursor-1")

    reloaded = _store(tmp_path)
    assert reloaded.load()["cursor"] == "cursor-1"
    # save_cursor 等读改写路径不得丢弃认领状态。
    assert reloaded.claim_many(["m1"], ttl_seconds=300, now=1100.0) == set()


def test_sync_messages_drops_redelivered_msgid_within_claim_window(tmp_path) -> None:
    store = _store(tmp_path)
    client = WeComKfClient(state_store=store)

    async def fake_sync_pages(open_kfid: str, token: str, cursor: str):
        # 模拟平台重复投递:游标竞态下两次都拉到同一条消息。
        return ([{"msgid": "m1", "msgtype": "text", "text": {"content": "房源表"}}], "cursor-1")

    client._sync_message_pages = fake_sync_pages  # type: ignore[method-assign]

    first = asyncio.run(client.sync_messages("kf_x", "token_a"))
    second = asyncio.run(client.sync_messages("kf_x", "token_b"))
    assert [item["msgid"] for item in first] == ["m1"]
    assert second == []


def test_sync_messages_dedups_same_batch_pagination_overlap(tmp_path) -> None:
    store = _store(tmp_path)
    client = WeComKfClient(state_store=store)

    async def fake_sync_pages(open_kfid: str, token: str, cursor: str):
        # 分页边界可能在同一批次返回重复 msgid。
        message = {"msgid": "m1", "msgtype": "text", "text": {"content": "房源表"}}
        return ([dict(message), dict(message)], "cursor-1")

    client._sync_message_pages = fake_sync_pages  # type: ignore[method-assign]
    messages = asyncio.run(client.sync_messages("kf_x", "token_a"))
    assert [item["msgid"] for item in messages] == ["m1"]


def test_sync_messages_serializes_concurrent_callbacks(tmp_path) -> None:
    store = _store(tmp_path)
    client = WeComKfClient(state_store=store)
    concurrency = {"current": 0, "peak": 0}

    async def fake_sync_pages(open_kfid: str, token: str, cursor: str):
        concurrency["current"] += 1
        concurrency["peak"] = max(concurrency["peak"], concurrency["current"])
        await asyncio.sleep(0.01)
        concurrency["current"] -= 1
        return ([{"msgid": "m1", "msgtype": "text", "text": {"content": "房源表"}}], "cursor-1")

    client._sync_message_pages = fake_sync_pages  # type: ignore[method-assign]

    async def run_case():
        return await asyncio.gather(
            client.sync_messages("kf_x", "token_a"),
            client.sync_messages("kf_x", "token_b"),
        )

    results = asyncio.run(run_case())
    # 临界区必须串行(游标读写不交叠),且同一 msgid 只放行一次。
    assert concurrency["peak"] == 1
    delivered = [item["msgid"] for batch in results for item in batch]
    assert delivered == ["m1"]


def test_restart_kf_turn_ignores_pure_duplicate_delivery(monkeypatch) -> None:
    async def run_case():
        main.kf_turn_tasks.clear()
        main.kf_turn_generations.clear()
        main.kf_turn_pending_messages.clear()

        turn_started = asyncio.Event()
        follow_up_started = asyncio.Event()
        release = asyncio.Event()
        started_turns: list[tuple[int, list[str]]] = []
        cancelled_generations: list[int] = []

        async def fake_process_text_turn(*, open_kfid, external_userid, pending_items, generation):
            started_turns.append((generation, [str(item.get("msgid")) for item in pending_items]))
            turn_started.set()
            if len(started_turns) >= 2:
                follow_up_started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled_generations.append(generation)
                raise

        monkeypatch.setattr(main, "_process_text_turn", fake_process_text_turn)

        item = {"msgid": "m1", "content": "房源表", "created_at": 0.0}
        first_call = asyncio.create_task(
            main._restart_kf_turn(open_kfid="kf_x", external_userid="wm_x", new_items=[dict(item)])
        )
        await asyncio.wait_for(turn_started.wait(), timeout=2)
        generations_before = dict(main.kf_turn_generations)

        # 平台重推同一 msgid:不得重启轮次、不得取消在跑轮次、不得重放。
        await main._restart_kf_turn(open_kfid="kf_x", external_userid="wm_x", new_items=[dict(item)])
        assert main.kf_turn_generations == generations_before
        assert len(started_turns) == 1
        assert cancelled_generations == []

        # 真正的客户追问(新 msgid)仍然按既有语义合并重启。
        follow_up = {"msgid": "m2", "content": "有燃气吗", "created_at": 0.0}
        second_call = asyncio.create_task(
            main._restart_kf_turn(open_kfid="kf_x", external_userid="wm_x", new_items=[dict(follow_up)])
        )
        await asyncio.wait_for(follow_up_started.wait(), timeout=2)
        assert cancelled_generations == [1]
        assert started_turns[1] == (2, ["m1", "m2"])

        release.set()
        await asyncio.gather(first_call, second_call, return_exceptions=True)
        main.kf_turn_tasks.clear()
        main.kf_turn_generations.clear()
        main.kf_turn_pending_messages.clear()

    asyncio.run(run_case())
