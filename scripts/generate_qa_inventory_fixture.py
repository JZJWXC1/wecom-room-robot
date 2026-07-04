from __future__ import annotations

"""QA fixture 生成器:从真实房源缓存快照生成 tests/fixtures/qa/test_inventory_cache.csv。

单一事实源整改(P0-1)核心脚本。裁决要求(2026-07-04):
1. 数据源 = data/inventory_cache.csv + data/inventory_cache_meta.json,
   生成前校验 meta.hash(源文件原始字节 sha256)与 status=success;
2. 溯源 meta 必带 source_snapshot_time(= 源 meta synced_at_iso),验收摘要必须引用;
3. 密码列清洗内置于生成器:全字段移除看房密码 token(\\d{4,}#),
   看房方式密码列清洗后为空则回填「看房提前联系」;
4. 生成产物(fixture+溯源 meta)全文不得命中 \\d{4,}#,由守卫测试
   tests/test_qa_fixture_guards.py 强制。

fixture 本体不含时间戳,同一快照重复生成字节级一致(便于 diff 与审计);
生成时间只进溯源 meta。
"""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

GENERATOR_NAME = "scripts/generate_qa_inventory_fixture.py"
GENERATOR_VERSION = "1.0.0"

# 看房密码样式:连续 4 位以上数字后跟 #(与守卫测试、敏感红线口径一致)
VIEWING_PASSWORD_RE = re.compile(r"\d{4,}#")

# 输出列契约:真实缓存 9 列 + 素材计数 2 列(消费方 inventory_legacy_parser /
# inventory_snapshot_builder / rewrite_inventory_index 均声明这 11 列)
SOURCE_COLUMNS = [
    "区域",
    "小区",
    "房号",
    "户型描述",
    "户型分类",
    "押一付一",
    "押二付一",
    "看房方式密码",
    "备注",
]
OUTPUT_COLUMNS = SOURCE_COLUMNS + ["视频数量", "图片数量"]

VIEWING_FALLBACK_TEXT = "看房提前联系"

DEFAULT_SOURCE = PROJECT_ROOT / "data" / "inventory_cache.csv"
DEFAULT_SOURCE_META = PROJECT_ROOT / "data" / "inventory_cache_meta.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "tests" / "fixtures" / "qa" / "test_inventory_cache.csv"
DEFAULT_PROVENANCE = PROJECT_ROOT / "tests" / "fixtures" / "qa" / "test_inventory_cache_provenance.json"
DEFAULT_INDEX = PROJECT_ROOT / "tests" / "fixtures" / "qa" / "test_rewrite_inventory_index.json"


def scrub_secret_tokens(value: str) -> str:
    """移除文本中的看房密码 token 并压缩空白;不改变其余语义(如「6.19空出」)。"""
    cleaned = VIEWING_PASSWORD_RE.sub("", str(value or ""))
    return " ".join(cleaned.split())


def scrub_row(row: dict[str, str]) -> dict[str, str]:
    scrubbed: dict[str, str] = {}
    for column in SOURCE_COLUMNS:
        scrubbed[column] = scrub_secret_tokens(row.get(column, ""))
    if not scrubbed["看房方式密码"]:
        scrubbed["看房方式密码"] = VIEWING_FALLBACK_TEXT
    scrubbed["视频数量"] = "0"
    scrubbed["图片数量"] = "0"
    return scrubbed


def load_source(source_path: Path, source_meta_path: Path) -> tuple[list[dict[str, str]], dict[str, Any], str]:
    raw = source_path.read_bytes()
    file_sha256 = hashlib.sha256(raw).hexdigest()
    meta = json.loads(source_meta_path.read_text(encoding="utf-8"))
    if str(meta.get("status")) != "success":
        raise SystemExit(f"源 meta status 不是 success: {meta.get('status')!r}")
    if str(meta.get("hash")) != file_sha256:
        raise SystemExit(
            "源缓存与 meta.hash 不一致(缓存可能被部分写入或篡改): "
            f"meta={meta.get('hash')!r} file={file_sha256!r}"
        )
    if not str(meta.get("synced_at_iso") or "").strip():
        raise SystemExit("源 meta 缺少 synced_at_iso,无法记录 source_snapshot_time")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    if not rows:
        raise SystemExit("源缓存为空,拒绝生成空 fixture")
    missing = [column for column in SOURCE_COLUMNS if column not in rows[0]]
    if missing:
        raise SystemExit(f"源缓存缺少必需列: {missing}")
    return rows, meta, file_sha256


def render_fixture_csv(rows: list[dict[str, str]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(scrub_row(row))
    return output.getvalue()


def build_provenance(
    *,
    meta: dict[str, Any],
    source_path: Path,
    source_sha256: str,
    fixture_text: str,
    row_count: int,
) -> dict[str, Any]:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "fixture": "tests/fixtures/qa/test_inventory_cache.csv",
        "fixture_version": hashlib.sha256(fixture_text.encode("utf-8")).hexdigest()[:16],
        "fixture_sha256": hashlib.sha256(fixture_text.encode("utf-8")).hexdigest(),
        "fixture_row_count": row_count,
        "generated_at": generated_at,
        "generator": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "source": str(meta.get("source") or ""),
        "source_detail": str(meta.get("source_detail") or ""),
        "source_path": source_path.as_posix(),
        "source_sha256": source_sha256,
        "source_row_count": len_source_rows(meta, row_count),
        "source_snapshot_time": str(meta.get("synced_at_iso")),
        "scrub_rule": "全字段移除 \\d{4,}# 看房密码 token;看房方式密码列为空时回填「看房提前联系」",
    }


def len_source_rows(meta: dict[str, Any], fallback: int) -> int:
    try:
        return int(meta.get("row_count"))
    except (TypeError, ValueError):
        return fallback


def generate(
    source_path: Path,
    source_meta_path: Path,
    output_path: Path,
    provenance_path: Path,
) -> dict[str, Any]:
    rows, meta, source_sha256 = load_source(source_path, source_meta_path)
    fixture_text = render_fixture_csv(rows)
    if VIEWING_PASSWORD_RE.search(fixture_text):
        raise SystemExit("清洗后 fixture 仍命中看房密码样式,拒绝写出")
    provenance = build_provenance(
        meta=meta,
        source_path=source_path,
        source_sha256=source_sha256,
        fixture_text=fixture_text,
        row_count=len(rows),
    )
    provenance_text = json.dumps(provenance, ensure_ascii=False, indent=2) + "\n"
    if VIEWING_PASSWORD_RE.search(provenance_text):
        raise SystemExit("溯源 meta 命中看房密码样式,拒绝写出")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" 禁用平台换行翻译:fixture_sha256 的口径是 LF 规范文本,
    # 守卫测试重算时也按 LF 规范化,双方对 git autocrlf 均免疫
    output_path.write_text(fixture_text, encoding="utf-8", newline="")
    provenance_path.write_text(provenance_text, encoding="utf-8", newline="")
    return provenance


def regenerate_index(fixture_path: Path, index_path: Path) -> dict[str, Any]:
    """从生成的 fixture 再生 QA 重写索引(复用生产同源的索引构建器,含密码脱敏)。"""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from app.services.rewrite_inventory_index import (
        DEFAULT_AREA_ALIASES,
        write_rewrite_inventory_index,
    )

    rows = list(csv.DictReader(io.StringIO(fixture_path.read_text(encoding="utf-8-sig"))))
    return write_rewrite_inventory_index(
        rows,
        path=index_path,
        area_aliases=DEFAULT_AREA_ALIASES,
        cache_meta={
            "source": "qa_fixture",
            "source_detail": "tests/fixtures/qa/test_inventory_cache.csv",
            "status": "success",
            "row_count": len(rows),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="从真实房源缓存快照生成 QA fixture(内置密码清洗)")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--source-meta", type=Path, default=DEFAULT_SOURCE_META)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--skip-index", action="store_true", help="只生成 fixture,不再生重写索引")
    args = parser.parse_args()
    provenance = generate(args.source, args.source_meta, args.output, args.provenance)
    summary = {
        "output": args.output.as_posix(),
        "fixture_version": provenance["fixture_version"],
        "fixture_row_count": provenance["fixture_row_count"],
        "source_snapshot_time": provenance["source_snapshot_time"],
    }
    if not args.skip_index:
        index = regenerate_index(args.output, args.index)
        if VIEWING_PASSWORD_RE.search(args.index.read_text(encoding="utf-8")):
            raise SystemExit("再生索引命中看房密码样式,拒绝保留")
        summary["index"] = args.index.as_posix()
        summary["index_row_count"] = int(index.get("row_count") or 0)
        summary["index_signature"] = str(index.get("signature") or "")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
