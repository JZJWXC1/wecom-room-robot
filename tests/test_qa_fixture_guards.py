from __future__ import annotations

"""QA fixture 生成器守卫测试(P0-1 裁决 ②b/②c 强制项)。

守卫对象是生成器逻辑本身(合成源数据 → tmp 输出),不依赖本地 data/ 运行时缓存,
干净检出也能跑。fixture 换血落地(批3)后,另有针对已提交 fixture 的守卫。

存在性 gate 探针约定(双探针,防华丰欣苑式语义反转):
- ROOM_LEVEL_PROBE(皋塘运都 9-402B):小区真实存在、房号不存在——最锋利的
  存在性测试。守卫同时证明「小区在产物中」与「该房号不在产物中」,任何一半
  失效都说明 fixture 或剧本口径漂移;
- TYPO_COMMUNITY_PROBE(高塘运都):真实小区「皋塘运都」的同音错别字变体,
  必须整小区不存在——防止剧本作者把探针写成错别字后被检索模糊匹配救活;
- SEMANTIC_REVERSAL_PROBE(华丰欣苑 14-2-901):旧 fixture 语义反转事故的
  纪念探针——它曾以"存在"身份进入合成 fixture,而真实房源表只有一字之差的
  「华丰新苑」。
"""

import csv
import hashlib
import importlib.util
import io
import json
import re
from pathlib import Path

import pytest

VIEWING_PASSWORD_RE = re.compile(r"\d{4,}#")

ROOM_LEVEL_PROBE = ("皋塘运都", "9-402B")
TYPO_COMMUNITY_PROBE = "高塘运都"
SEMANTIC_REVERSAL_PROBE = ("华丰欣苑", "14-2-901")


def _load_generator():
    path = Path(__file__).resolve().parents[1] / "scripts" / "generate_qa_inventory_fixture.py"
    spec = importlib.util.spec_from_file_location("generate_qa_inventory_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = _load_generator()

# 合成源样本:覆盖密码 token 的四种出现位置(密码列独占/密码列混排/备注列/户型描述列),
# 以及清洗后密码列为空需回填的情形
SOURCE_ROWS = [
    {
        "区域": "拱墅万达 北部软件园 城北万象城",
        "小区": "棠润府",
        "房号": "15-2-801B",
        "户型描述": "一室一厅独立厨卫",
        "户型分类": "一室一厅",
        "押一付一": "1600",
        "押二付一": "1400",
        "看房方式密码": "101004# 6.19空出",
        "备注": "水30/月 电1元/度",
    },
    {
        "区域": "石桥街道 华丰 石桥 永佳 半山",
        "小区": "石桥铭苑",
        "房号": "6-1102",
        "户型描述": "两室一厅整租 门锁88991#",
        "户型分类": "两室一厅",
        "押一付一": "4800",
        "押二付一": "4300",
        "看房方式密码": "334455#",
        "备注": "民用水电 备用密码667788#",
    },
    {
        "区域": "闸弄口 新塘 元宝塘 东站",
        "小区": "骏塘名庭",
        "房号": "8-1101A",
        "户型描述": "一室一厅",
        "户型分类": "一室一厅",
        "押一付一": "1500",
        "押二付一": "1300",
        "看房方式密码": "看房提前联系",
        "备注": "民用水电",
    },
    {
        "区域": "闸弄口 新塘 元宝塘 东站",
        "小区": "皋塘运都",
        "房号": "16-1-2206",
        "户型描述": "两室一厅整租",
        "户型分类": "两室一厅",
        "押一付一": "4500",
        "押二付一": "4200",
        "看房方式密码": "看房提前联系",
        "备注": "民用水电",
    },
]


def _write_source(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source_path = tmp_path / "inventory_cache.csv"
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=generator.SOURCE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in SOURCE_ROWS:
        writer.writerow(row)
    source_path.write_text(output.getvalue(), encoding="utf-8")
    meta_path = tmp_path / "inventory_cache_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "source": "feishu_bitable",
                "source_detail": "spreadsheet:test",
                "status": "success",
                "hash": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "row_count": len(SOURCE_ROWS),
                "synced_at_iso": "2026-07-02 15:12:23",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source_path, meta_path


def _generate(tmp_path: Path) -> tuple[Path, Path, dict]:
    source_path, meta_path = _write_source(tmp_path)
    output_path = tmp_path / "out" / "test_inventory_cache.csv"
    provenance_path = tmp_path / "out" / "test_inventory_cache_provenance.json"
    provenance = generator.generate(source_path, meta_path, output_path, provenance_path)
    return output_path, provenance_path, provenance


def test_generated_fixture_never_contains_viewing_password_tokens(tmp_path: Path) -> None:
    output_path, provenance_path, _ = _generate(tmp_path)

    fixture_text = output_path.read_text(encoding="utf-8")
    provenance_text = provenance_path.read_text(encoding="utf-8")

    assert not VIEWING_PASSWORD_RE.search(fixture_text)
    assert not VIEWING_PASSWORD_RE.search(provenance_text)
    # 清洗只摘除密码 token,不吞掉同字段的正常语义
    assert "6.19空出" in fixture_text
    assert "民用水电 备用密码" in fixture_text


def test_generated_fixture_backfills_emptied_viewing_field(tmp_path: Path) -> None:
    output_path, _, _ = _generate(tmp_path)

    rows = list(csv.DictReader(io.StringIO(output_path.read_text(encoding="utf-8"))))
    by_room = {(row["小区"], row["房号"]): row for row in rows}

    assert by_room[("石桥铭苑", "6-1102")]["看房方式密码"] == "看房提前联系"
    assert by_room[("棠润府", "15-2-801B")]["看房方式密码"] == "6.19空出"


def test_provenance_carries_source_snapshot_time_and_counts(tmp_path: Path) -> None:
    output_path, _, provenance = _generate(tmp_path)

    rows = list(csv.DictReader(io.StringIO(output_path.read_text(encoding="utf-8"))))

    assert provenance["source_snapshot_time"] == "2026-07-02 15:12:23"
    assert provenance["fixture_row_count"] == len(rows) == len(SOURCE_ROWS)
    assert provenance["source_row_count"] == len(SOURCE_ROWS)
    assert provenance["generator"] == "scripts/generate_qa_inventory_fixture.py"
    assert provenance["fixture_sha256"] == hashlib.sha256(
        output_path.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()


def _assert_existence_gate_probes(rows: list[dict[str, str]]) -> None:
    communities = {row["小区"] for row in rows}
    labels = {(row["小区"], row["房号"]) for row in rows}

    # 房号级探针:小区必须存在(探针保持锋利),该房号必须不存在
    assert ROOM_LEVEL_PROBE[0] in communities
    assert ROOM_LEVEL_PROBE not in labels
    # 探针房号全局不存在(任何小区都不得有此房号,机器判分按房号 token 匹配)
    assert all(row["房号"] != ROOM_LEVEL_PROBE[1] for row in rows)
    # 错别字小区探针:整小区不得存在
    assert TYPO_COMMUNITY_PROBE not in communities
    # 语义反转纪念探针:整小区不得存在
    assert SEMANTIC_REVERSAL_PROBE[0] not in communities
    assert SEMANTIC_REVERSAL_PROBE not in labels


def test_existence_gate_probes_absent_from_generated_fixture(tmp_path: Path) -> None:
    output_path, _, _ = _generate(tmp_path)

    rows = list(csv.DictReader(io.StringIO(output_path.read_text(encoding="utf-8"))))

    _assert_existence_gate_probes(rows)


def test_generated_fixture_columns_match_consumer_contract(tmp_path: Path) -> None:
    output_path, _, _ = _generate(tmp_path)

    reader = csv.reader(io.StringIO(output_path.read_text(encoding="utf-8")))
    header = next(reader)

    assert header == [
        "区域",
        "小区",
        "房号",
        "户型描述",
        "户型分类",
        "押一付一",
        "押二付一",
        "看房方式密码",
        "备注",
        "视频数量",
        "图片数量",
    ]


def test_generation_is_deterministic_for_same_snapshot(tmp_path: Path) -> None:
    first_output, _, first_provenance = _generate(tmp_path / "a")
    second_output, _, second_provenance = _generate(tmp_path / "b")

    assert first_output.read_bytes() == second_output.read_bytes()
    assert first_provenance["fixture_version"] == second_provenance["fixture_version"]
    assert first_provenance["fixture_sha256"] == second_provenance["fixture_sha256"]


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "qa"


def test_committed_fixture_artifacts_contain_no_viewing_password_tokens() -> None:
    fixture_text = (FIXTURES_DIR / "test_inventory_cache.csv").read_text(encoding="utf-8-sig")
    provenance_text = (FIXTURES_DIR / "test_inventory_cache_provenance.json").read_text(encoding="utf-8")
    index_text = (FIXTURES_DIR / "test_rewrite_inventory_index.json").read_text(encoding="utf-8")

    assert not VIEWING_PASSWORD_RE.search(fixture_text)
    assert not VIEWING_PASSWORD_RE.search(provenance_text)
    assert not VIEWING_PASSWORD_RE.search(index_text)


def test_committed_fixture_respects_existence_gate_probes() -> None:
    fixture_text = (FIXTURES_DIR / "test_inventory_cache.csv").read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(fixture_text)))

    _assert_existence_gate_probes(rows)


def test_committed_fixture_matches_provenance_and_index_counts() -> None:
    fixture_bytes = (FIXTURES_DIR / "test_inventory_cache.csv").read_bytes()
    rows = list(csv.DictReader(io.StringIO(fixture_bytes.decode("utf-8-sig"))))
    provenance = json.loads(
        (FIXTURES_DIR / "test_inventory_cache_provenance.json").read_text(encoding="utf-8")
    )
    index = json.loads(
        (FIXTURES_DIR / "test_rewrite_inventory_index.json").read_text(encoding="utf-8")
    )

    # fixture_sha256 口径 = LF 规范文本(生成器与守卫同口径,对 autocrlf 免疫)
    canonical_text = fixture_bytes.decode("utf-8-sig").replace("\r\n", "\n")

    assert provenance["fixture_row_count"] == len(rows)
    assert index["row_count"] == len(rows)
    assert provenance["fixture_sha256"] == hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    assert str(provenance["source_snapshot_time"]).strip()
    assert index["cache_meta"]["source_detail"] == provenance["fixture"]


def test_generator_rejects_source_hash_mismatch(tmp_path: Path) -> None:
    source_path, meta_path = _write_source(tmp_path)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["hash"] = "0" * 64
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SystemExit, match="meta.hash"):
        generator.generate(
            source_path,
            meta_path,
            tmp_path / "out" / "fixture.csv",
            tmp_path / "out" / "provenance.json",
        )


def test_existence_probe_constants_locked_between_runner_and_guards() -> None:
    from qa_artifacts import run_rag_10windows_10turns_utf8 as runner

    assert runner.EXISTENCE_PROBE_ROOM_LABEL == "".join(ROOM_LEVEL_PROBE)
    assert runner.EXISTENCE_PROBE_ROOM_NO == ROOM_LEVEL_PROBE[1]
    assert runner.EXISTENCE_PROBE_TYPO_COMMUNITY == TYPO_COMMUNITY_PROBE
    window_ids = [window["id"] for window in runner.WINDOWS]
    assert "existence_gate_gaotang" in window_ids
    existence_turns = "\n".join(
        turn
        for window in runner.WINDOWS
        if window["id"] == "existence_gate_gaotang"
        for turn in window["turns"]
    )
    assert runner.EXISTENCE_PROBE_ROOM_LABEL in existence_turns
    assert runner.EXISTENCE_PROBE_TYPO_COMMUNITY in existence_turns
    assert runner.EXPECTED_FULL_WINDOW_COUNT == len(runner.WINDOWS)


def test_existence_probe_problem_flags_hallucinated_binding() -> None:
    from qa_artifacts import run_rag_10windows_10turns_utf8 as runner

    reason = runner._existence_probe_problem(
        "皋塘运都9-402B这套还在吗？客户存了个旧房源链接。",
        ["皋塘运都9-402B"],
        "在的，这套还在租。",
    )

    assert "存在性探针被绑定" in reason


def test_existence_probe_problem_flags_confirmation_without_correction() -> None:
    from qa_artifacts import run_rag_10windows_10turns_utf8 as runner

    reason = runner._existence_probe_problem(
        "那客户记的9-402B到底有没有？帮我再确认一遍。",
        [],
        "在的，可以看，视频发你。",
    )

    assert "未做反问纠偏" in reason


def test_existence_probe_problem_accepts_correction_reply() -> None:
    from qa_artifacts import run_rag_10windows_10turns_utf8 as runner

    assert (
        runner._existence_probe_problem(
            "皋塘运都9-402B这套还在吗？",
            [],
            "你说的9-402B没有查到，皋塘运都现在在租的是16-1-2206，要不要看这套？",
        )
        == ""
    )
    assert (
        runner._existence_probe_problem(
            "高塘运都是不是也有房子？客户发来这个名字。",
            [],
            "没有高塘运都这个小区，你说的应该是皋塘运都，在租的是16-1-2206。",
        )
        == ""
    )
    # 非探针轮不触发
    assert runner._existence_probe_problem("新天地4000左右两室有吗？", [], "有的，发你两套。") == ""
