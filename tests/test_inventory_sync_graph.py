from __future__ import annotations

import asyncio
from typing import Any

import app.main as main
import scripts.sync_feishu_region_inventory as sync_script
from app.services.inventory_sync_graph import InventorySyncGraphDeps, run_inventory_sync_graph


def run(coro):
    return asyncio.run(coro)


def test_inventory_sync_graph_runs_all_stages_in_order() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def stage(name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(name)
            assert kwargs["dry_run"] is True
            assert kwargs["sync_media"] is False
            assert "cache_result" in kwargs["previous_results"]
            return {"ok": True, "stage": name}

        async def write_report(**kwargs: Any) -> dict[str, Any]:
            calls.append("report")
            assert kwargs["status"] == "passed"
            assert kwargs["failures"] == []
            assert kwargs["results"]["snapshot_result"]["stage"] == "snapshot"
            return {"ok": True, "path": "sync-report.json"}

        result = await run_inventory_sync_graph(
            InventorySyncGraphDeps(
                refresh_inventory_cache=lambda **kwargs: stage("cache", **kwargs),
                sync_region_inventory=lambda **kwargs: stage("region", **kwargs),
                render_inventory_sheet_image=lambda **kwargs: stage("image", **kwargs),
                build_media_manifest=lambda **kwargs: stage("manifest", **kwargs),
                publish_snapshot=lambda **kwargs: stage("snapshot", **kwargs),
                write_report=write_report,
            ),
            dry_run=True,
            sync_media=False,
        )

        assert calls == ["region", "cache", "image", "manifest", "snapshot", "report"]
        assert result["status"] == "passed"
        assert result["trace"] == [
            "inventory_sync:sync_region_inventory",
            "inventory_sync:refresh_inventory_cache",
            "inventory_sync:render_inventory_sheet_image",
            "inventory_sync:build_media_manifest",
            "inventory_sync:publish_snapshot",
            "inventory_sync:write_report",
        ]

    run(run_case())


def test_inventory_sync_graph_fail_fast_writes_report_without_later_side_effects() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def cache(**kwargs: Any) -> dict[str, Any]:
            calls.append("cache")
            return {"ok": True}

        async def region(**kwargs: Any) -> dict[str, Any]:
            calls.append("region")
            return {"ok": False, "error": "feishu unavailable"}

        async def should_not_run(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("sync graph must stop before later side effects")

        async def write_report(**kwargs: Any) -> dict[str, Any]:
            calls.append("report")
            assert kwargs["status"] == "blocked"
            assert kwargs["blocked_stage"] == "sync_region_inventory"
            assert kwargs["failures"][0]["reason"] == "feishu unavailable"
            return {"ok": False, "path": "sync-fail.json"}

        result = await run_inventory_sync_graph(
            InventorySyncGraphDeps(
                refresh_inventory_cache=cache,
                sync_region_inventory=region,
                render_inventory_sheet_image=should_not_run,
                build_media_manifest=should_not_run,
                publish_snapshot=should_not_run,
                write_report=write_report,
            )
        )

        assert calls == ["region", "report"]
        assert result["status"] == "blocked"
        assert result["blocked_stage"] == "sync_region_inventory"

    run(run_case())


def test_inventory_sync_graph_can_continue_for_full_audit_when_fail_fast_disabled() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def stage(name: str, result: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            calls.append(name)
            return result or {"ok": True}

        async def write_report(**kwargs: Any) -> dict[str, Any]:
            calls.append("report")
            assert kwargs["status"] == "blocked"
            assert kwargs["failures"][0]["stage"] == "refresh_inventory_cache"
            assert kwargs["results"]["snapshot_result"]["ok"] is True
            return {"ok": False}

        result = await run_inventory_sync_graph(
            InventorySyncGraphDeps(
                refresh_inventory_cache=lambda **kwargs: stage("cache", {"ok": False, "reason": "locked"}, **kwargs),
                sync_region_inventory=lambda **kwargs: stage("region", **kwargs),
                render_inventory_sheet_image=lambda **kwargs: stage("image", **kwargs),
                build_media_manifest=lambda **kwargs: stage("manifest", **kwargs),
                publish_snapshot=lambda **kwargs: stage("snapshot", **kwargs),
                write_report=write_report,
            ),
            fail_fast=False,
        )

        assert calls == ["region", "cache", "image", "manifest", "snapshot", "report"]
        assert result["status"] == "blocked"
        assert result["failures"][0]["reason"] == "locked"

    run(run_case())


def test_inventory_sync_graph_treats_cutover_not_ready_as_failure() -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def stage(name: str, result: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
            calls.append(name)
            return result or {"ok": True}

        async def should_not_run(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("ready:false must stop later side effects")

        async def write_report(**kwargs: Any) -> dict[str, Any]:
            calls.append("report")
            assert kwargs["status"] == "blocked"
            assert kwargs["blocked_stage"] == "publish_snapshot"
            assert kwargs["failures"][0]["reason"] == "primary_replay_parity_failed"
            return {"ok": False}

        result = await run_inventory_sync_graph(
            InventorySyncGraphDeps(
                refresh_inventory_cache=lambda **kwargs: stage("cache", **kwargs),
                sync_region_inventory=lambda **kwargs: stage("region", **kwargs),
                render_inventory_sheet_image=lambda **kwargs: stage("image", **kwargs),
                build_media_manifest=lambda **kwargs: stage("manifest", **kwargs),
                publish_snapshot=lambda **kwargs: stage(
                    "snapshot",
                    {"ready": False, "not_ready_reasons": ["primary_replay_parity_failed"]},
                    **kwargs,
                ),
                write_report=write_report,
            )
        )

        assert calls == ["region", "cache", "image", "manifest", "snapshot", "report"]
        assert result["status"] == "blocked"

    run(run_case())


def test_sync_script_graph_dry_run_skips_runtime_side_effects(monkeypatch) -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def fake_run_sync(*, dry_run: bool, sync_media: bool) -> dict[str, Any]:
            calls.append("region")
            assert dry_run is True
            assert sync_media is False
            return {"ok": True, "dry_run": dry_run, "planned": True}

        async def fail_side_effect(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("dry-run graph must not run runtime side effects")

        monkeypatch.setattr(sync_script, "run_sync", fake_run_sync)
        monkeypatch.setattr(sync_script, "refresh_rewrite_inventory_index", fail_side_effect)
        monkeypatch.setattr(sync_script, "refresh_media_manifest", fail_side_effect)

        result = await sync_script.run_sync_graph_pipeline(dry_run=True, sync_media=False)

        assert calls == ["region"]
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["rewrite_index"]["skipped"] is True
        assert result["media_manifest"]["reason"] == "dry_run"
        assert result["cutover_rehearsal"]["reason"] == "dry_run"
        assert result["graph"]["trace"] == [
            "inventory_sync:sync_region_inventory",
            "inventory_sync:refresh_inventory_cache",
            "inventory_sync:render_inventory_sheet_image",
            "inventory_sync:build_media_manifest",
            "inventory_sync:publish_snapshot",
            "inventory_sync:write_report",
        ]

    run(run_case())


def test_sync_script_graph_fail_fast_blocks_after_region_failure(monkeypatch) -> None:
    async def run_case() -> None:
        async def fake_run_sync(*, dry_run: bool, sync_media: bool) -> dict[str, Any]:
            return {"ok": False, "reason": "feishu unavailable", "dry_run": dry_run, "sync_media": sync_media}

        async def should_not_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("region failure must stop later graph stages")

        monkeypatch.setattr(sync_script, "run_sync", fake_run_sync)
        monkeypatch.setattr(sync_script, "refresh_rewrite_inventory_index", should_not_run)
        monkeypatch.setattr(sync_script, "refresh_media_manifest", should_not_run)

        result = await sync_script.run_sync_graph_pipeline(dry_run=False, sync_media=True)

        assert result["ok"] is False
        assert result["graph"]["blocked_stage"] == "sync_region_inventory"
        assert result["graph"]["failures"][0]["reason"] == "feishu unavailable"
        assert result["graph"]["trace"] == [
            "inventory_sync:sync_region_inventory",
            "inventory_sync:write_report",
        ]

    run(run_case())


def test_sync_script_graph_success_preserves_runtime_artifact_fields(monkeypatch) -> None:
    async def run_case() -> None:
        calls: list[str] = []

        async def fake_run_sync(*, dry_run: bool, sync_media: bool) -> dict[str, Any]:
            calls.append("region")
            return {"ok": True, "dry_run": dry_run, "sync_media": sync_media, "region": "ok"}

        async def fake_refresh_rewrite_inventory_index() -> dict[str, Any]:
            calls.append("rewrite")
            return {"ok": True, "path": "rewrite-index.json", "row_count": 2}

        class FakeFrame:
            def fillna(self, _value: str) -> "FakeFrame":
                return self

            def to_dict(self, *, orient: str) -> list[dict[str, Any]]:
                assert orient == "records"
                return [{"listing_id": "lst-a", "小区": "星桥锦绣嘉苑", "房号": "20-1606A"}]

        class FakeInventoryService:
            cache_meta = {"hash": "inventory-hash"}

            async def refresh(self) -> FakeFrame:
                calls.append("inventory-refresh-for-manifest")
                return FakeFrame()

        async def fake_refresh_media_manifest(rows: list[dict[str, Any]]) -> dict[str, Any]:
            calls.append("manifest")
            assert rows[0]["listing_id"] == "lst-a"
            return {"ok": True, "ready": True, "path": "media_manifest.json"}

        async def fake_cutover_graph(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append("cutover")
            return {
                "status": "passed",
                "trace": [
                    "inventory_cutover:primary_replay",
                    "inventory_cutover:evaluate_readiness",
                    "inventory_cutover:rollback_rehearsal",
                    "inventory_cutover:write_report",
                ],
                "report": {"ok": True, "path": "cutover-report.json"},
            }

        monkeypatch.setattr(sync_script, "run_sync", fake_run_sync)
        monkeypatch.setattr(sync_script, "refresh_rewrite_inventory_index", fake_refresh_rewrite_inventory_index)
        monkeypatch.setattr(sync_script, "InventoryService", FakeInventoryService)
        monkeypatch.setattr(sync_script, "refresh_media_manifest", fake_refresh_media_manifest)
        monkeypatch.setattr(sync_script.inventory_cutover_graph, "build_local_inventory_cutover_deps", lambda: object())
        monkeypatch.setattr(sync_script.inventory_cutover_graph, "run_inventory_cutover_graph", fake_cutover_graph)

        result = await sync_script.run_sync_graph_pipeline(dry_run=False, sync_media=True)

        assert calls == ["region", "rewrite", "inventory-refresh-for-manifest", "manifest", "cutover"]
        assert result["ok"] is True
        assert result["region"] == "ok"
        assert result["rewrite_index"]["path"] == "rewrite-index.json"
        assert result["media_manifest"]["ready"] is True
        assert result["cutover_rehearsal"]["ready"] is True
        assert result["graph"]["status"] == "passed"

    run(run_case())


def test_sync_script_cli_defaults_to_graph_pipeline(monkeypatch) -> None:
    calls: list[str] = []

    class FakeLock:
        def __init__(self, _path) -> None:
            self.acquired = False

        def acquire(self) -> bool:
            self.acquired = True
            return True

        def release(self) -> None:
            self.acquired = False

    async def fake_graph_pipeline(*, dry_run: bool, sync_media: bool, fail_fast: bool) -> dict[str, Any]:
        calls.append("graph")
        assert dry_run is True
        assert sync_media is False
        assert fail_fast is False
        return {"ok": True, "graph": {"status": "passed"}}

    async def fail_legacy_sync(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("default CLI sync must use inventory_sync_graph")

    monkeypatch.setattr(sync_script.settings, "feishu_region_sync_sources", "[]")
    monkeypatch.setattr(sync_script, "FileLock", FakeLock)
    monkeypatch.setattr(sync_script, "run_sync_graph_pipeline", fake_graph_pipeline)
    monkeypatch.setattr(sync_script, "run_sync", fail_legacy_sync)
    monkeypatch.setattr(sync_script, "write_state", lambda _result: None)
    monkeypatch.setattr(sync_script, "print_json", lambda _result: None)
    monkeypatch.setattr(
        sync_script.sys,
        "argv",
        ["sync_feishu_region_inventory.py", "--dry-run", "--skip-media", "--no-fail-fast"],
    )

    assert sync_script.main() == 0
    assert calls == ["graph"]


def test_sync_script_cli_legacy_sync_requires_explicit_flag(monkeypatch) -> None:
    calls: list[str] = []

    class FakeLock:
        def __init__(self, _path) -> None:
            self.acquired = False

        def acquire(self) -> bool:
            self.acquired = True
            return True

        def release(self) -> None:
            self.acquired = False

    async def fail_graph_pipeline(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("--legacy-sync must bypass inventory_sync_graph")

    async def fake_legacy_sync(*, dry_run: bool, sync_media: bool) -> dict[str, Any]:
        calls.append("legacy")
        assert dry_run is True
        assert sync_media is False
        return {"ok": True, "dry_run": dry_run, "legacy": True}

    monkeypatch.setattr(sync_script.settings, "feishu_region_sync_sources", "[]")
    monkeypatch.setattr(sync_script, "FileLock", FakeLock)
    monkeypatch.setattr(sync_script, "run_sync_graph_pipeline", fail_graph_pipeline)
    monkeypatch.setattr(sync_script, "run_sync", fake_legacy_sync)
    monkeypatch.setattr(sync_script, "write_state", lambda _result: None)
    monkeypatch.setattr(sync_script, "print_json", lambda _result: None)
    monkeypatch.setattr(
        sync_script.sys,
        "argv",
        ["sync_feishu_region_inventory.py", "--legacy-sync", "--dry-run", "--skip-media"],
    )

    assert sync_script.main() == 0
    assert calls == ["legacy"]


def test_admin_region_inventory_sync_endpoint_uses_graph_for_dry_run(monkeypatch) -> None:
    async def run_case() -> None:
        calls: list[str] = []

        class FakeRegionInventorySyncService:
            async def sync(self, *, dry_run: bool, sync_media: bool) -> dict[str, Any]:
                calls.append("region")
                assert dry_run is True
                assert sync_media is False
                return {"ok": True, "dry_run": dry_run, "planned": True}

        async def fail_refresh_inventory() -> dict[str, Any]:
            raise AssertionError("dry-run admin sync graph must not refresh runtime inventory")

        monkeypatch.setattr(main, "RegionInventorySyncService", FakeRegionInventorySyncService)
        monkeypatch.setattr(main, "_refresh_inventory", fail_refresh_inventory)

        result = await main.sync_feishu_region_inventory(dry_run=True, sync_media=False)

        assert calls == ["region"]
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["rewrite_index"]["reason"] == "dry_run"
        assert result["media_manifest"]["reason"] == "dry_run"
        assert result["cutover_rehearsal"]["reason"] == "dry_run"
        assert result["graph"]["schema_version"] == "admin_feishu_region_inventory_sync_graph.v1"
        assert result["graph"]["trace"] == [
            "inventory_sync:sync_region_inventory",
            "inventory_sync:refresh_inventory_cache",
            "inventory_sync:render_inventory_sheet_image",
            "inventory_sync:build_media_manifest",
            "inventory_sync:publish_snapshot",
            "inventory_sync:write_report",
        ]

    run(run_case())


def test_admin_region_inventory_sync_endpoint_fail_fast_blocks_after_region_failure(monkeypatch) -> None:
    async def run_case() -> None:
        class FakeRegionInventorySyncService:
            async def sync(self, *, dry_run: bool, sync_media: bool) -> dict[str, Any]:
                return {"ok": False, "reason": "feishu unavailable", "dry_run": dry_run, "sync_media": sync_media}

        async def should_not_refresh_inventory() -> dict[str, Any]:
            raise AssertionError("region failure must stop admin graph before cache refresh")

        monkeypatch.setattr(main, "RegionInventorySyncService", FakeRegionInventorySyncService)
        monkeypatch.setattr(main, "_refresh_inventory", should_not_refresh_inventory)

        result = await main.sync_feishu_region_inventory(dry_run=False, sync_media=True)

        assert result["ok"] is False
        assert result["graph"]["status"] == "blocked"
        assert result["graph"]["blocked_stage"] == "sync_region_inventory"
        assert result["graph"]["failures"][0]["reason"] == "feishu unavailable"
        assert result["graph"]["trace"] == [
            "inventory_sync:sync_region_inventory",
            "inventory_sync:write_report",
        ]

    run(run_case())


def test_admin_region_inventory_sync_endpoint_success_refreshes_cache_inside_graph(monkeypatch) -> None:
    async def run_case() -> None:
        calls: list[str] = []

        class FakeRegionInventorySyncService:
            async def sync(self, *, dry_run: bool, sync_media: bool) -> dict[str, Any]:
                calls.append("region")
                return {"ok": True, "dry_run": dry_run, "sync_media": sync_media, "synced": 3}

        async def fake_refresh_inventory() -> dict[str, Any]:
            calls.append("cache")
            return {
                "ok": True,
                "rows": 3,
                "rewrite_index": {"ok": True, "path": "rewrite_inventory_index.json"},
            }

        monkeypatch.setattr(main, "RegionInventorySyncService", FakeRegionInventorySyncService)
        monkeypatch.setattr(main, "_refresh_inventory", fake_refresh_inventory)

        result = await main.sync_feishu_region_inventory(dry_run=False, sync_media=True)

        assert calls == ["region", "cache"]
        assert result["ok"] is True
        assert result["synced"] == 3
        assert result["rewrite_index"]["rows"] == 3
        assert result["rewrite_index"]["rewrite_index"]["path"] == "rewrite_inventory_index.json"
        assert result["media_manifest"]["reason"] == "admin_endpoint_does_not_publish_media_manifest"
        assert result["cutover_rehearsal"]["reason"] == "admin_endpoint_never_switches_snapshot_without_approve_deploy"
        assert result["graph"]["status"] == "passed"

    run(run_case())
