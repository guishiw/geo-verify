"""采集公共部分：确定性编号、域名解析、时间换算、引用与快照解析、只追加写入器。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli_json
from factory import make_observation
from helpers_basic import sealed_project
from helpers_collect import IMPORT_FIELDS, import_row, independent_digest, write_csv
from hypothesis import given
from hypothesis import strategies as st

from geo_verify import project
from geo_verify.collect import base, fileimport
from geo_verify.collect.base import FieldError
from geo_verify.errors import InputError
from geo_verify.jsonl import read_json

# ------------------------------------------------------------------ 编号


def test_task_ids_follow_the_documented_formula() -> None:
    parts = ("auto-demo", "ab" * 32, "engine-a", "api", "W1", "Q001", 3)
    raw = "auto-demo|" + "ab" * 32 + "|engine-a|api|W1|Q001|3"
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    assert base.task_digest(*parts) == expected
    assert base.trace_id_for(*parts) == "t-" + expected
    assert base.observation_id_for(*parts) == "o-" + expected
    assert len(base.trace_id_for(*parts)) == 26


IDS = st.text(alphabet="abcXYZ019-_.", min_size=1, max_size=12)


@given(IDS, IDS, IDS, IDS, st.integers(min_value=1, max_value=99))
def test_task_ids_are_deterministic_and_distinguish_every_part(
    platform: str, window: str, query: str, other: str, attempt: int
) -> None:
    parts = ("p", "0" * 64, platform, "api", window, query, attempt)
    assert base.trace_id_for(*parts) == "t-" + independent_digest(*parts)
    assert base.trace_id_for(*parts) == base.trace_id_for(*parts)
    assert base.trace_id_for(*parts) != base.trace_id_for(*parts[:6], attempt + 1)
    assert base.trace_id_for(*parts) != base.trace_id_for(*parts[:3], "manual", *parts[4:])
    if other != query:
        assert base.trace_id_for(*parts) != base.trace_id_for(*parts[:5], other, attempt)


# ------------------------------------------------------------------ 域名


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://lanxu-auto.example/s7/spec", "lanxu-auto.example"),
        ("HTTP://News.Auto-Review.EXAMPLE:8443/a?b=1#c", "news.auto-review.example"),
        ("https://user:pw@docs.example./x", "docs.example"),
        ("  https://a.example  ", "a.example"),
        ("https://澜序.example/车型", "xn--8wt929a.example"),
        ("//cdn.example/x.js", "cdn.example"),
        ("lanxu-auto.example/s7", None),
        ("not a url", None),
        ("https://localhost/x", None),
        ("https:///path-only", None),
        ("mailto:someone", None),
        ("http://[::1", None),
        ("https://" + "a" * 70 + ".example/", None),
        ("", None),
    ],
)
def test_domain_of(url: str, expected: str | None) -> None:
    assert base.domain_of(url) == expected


def test_punycode_expectation_is_independent() -> None:
    # 上面参数里的 punycode 由标准库 punycode 编码器给出，不经过被测函数
    assert "xn--" + "澜序".encode("punycode").decode("ascii") == "xn--8wt929a"


# ------------------------------------------------------------------ 是否与时间


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        (" TRUE ", True),
        ("是", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("否", False),
        ("0", False),
        ("N", False),
        ("", None),
        ("   ", None),
        (None, None),
        (True, True),
        (False, False),
    ],
)
def test_parse_bool(value: Any, expected: bool | None) -> None:
    assert base.parse_bool(value, "logged_in") is expected


@pytest.mark.parametrize("value", ["也许", "2", 1, 0.0, ["true"]])
def test_parse_bool_rejects_unknown_tokens(value: Any) -> None:
    with pytest.raises(FieldError) as excinfo:
        base.parse_bool(value, "web_search")
    assert excinfo.value.column == "web_search"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-08T02:00:00Z", "2026-10-08T02:00:00Z"),
        ("2026-10-08T10:00:00+08:00", "2026-10-08T02:00:00Z"),
        ("2026-10-08 10:00:00+08:00", "2026-10-08T02:00:00Z"),
        ("2026-10-07T21:30:00-04:30", "2026-10-08T02:00:00Z"),
        ("2026-10-08T00:30:00+08:00", "2026-10-07T16:30:00Z"),
        ("2026-10-08T02:00:00.250Z", "2026-10-08T02:00:00.25Z"),
        (" 2026-10-08T02:00:00Z ", "2026-10-08T02:00:00Z"),
    ],
)
def test_to_utc_converts_offsets(text: str, expected: str) -> None:
    assert base.to_utc(text, "started_at")[0] == expected


@pytest.mark.parametrize("value", ["", None, "2026-10-08T10:00:00", "2026-10-08", "昨天", 20261008])
def test_to_utc_rejects_missing_naive_and_malformed(value: Any) -> None:
    with pytest.raises(FieldError) as excinfo:
        base.to_utc(value, "ended_at")
    assert excinfo.value.column == "ended_at"


@given(
    st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1)),
    st.integers(min_value=-14 * 60, max_value=14 * 60),
)
def test_to_utc_preserves_the_instant(moment: datetime, offset_minutes: int) -> None:
    aware = moment.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
    text, parsed = base.to_utc(aware.isoformat(), "started_at")
    assert parsed == aware
    assert text.endswith("Z")
    assert datetime.fromisoformat(text) == aware.astimezone(UTC)


def test_text_or_none() -> None:
    assert base.text_or_none(None, "region") is None
    assert base.text_or_none("  ", "region") is None
    assert base.text_or_none(" CN ", "region") == " CN "
    with pytest.raises(FieldError):
        base.text_or_none(7, "region")


# ------------------------------------------------------------------ 引用


def test_parse_citations_shorthand_and_json() -> None:
    assert base.parse_citations(None) == []
    assert base.parse_citations("  ") == []
    shorthand = "inline | https://a.example/x | 标题 | 含竖线\n\nsource_list|https://b.example/y"
    assert base.parse_citations(shorthand) == [
        {
            "order": 1,
            "url": "https://a.example/x",
            "domain": "a.example",
            "title": "标题 | 含竖线",
            "placement": "inline",
        },
        {
            "order": 2,
            "url": "https://b.example/y",
            "domain": "b.example",
            "title": None,
            "placement": "source_list",
        },
    ]
    given_fields = [
        {
            "order": 5,
            "url": " https://c.example ",
            "domain": None,
            "title": " ",
            "placement": "inline",
        }
    ]
    assert base.parse_citations(json.dumps(given_fields)) == [
        {
            "order": 5,
            "url": "https://c.example",
            "domain": None,
            "title": None,
            "placement": "inline",
        }
    ]


@pytest.mark.parametrize(
    "value",
    [
        "只有一段",
        '{"url": "https://a.example"}',
        "[1]",
        '[{"url": "", "placement": "inline"}]',
        '[{"url": "https://a.example", "placement": "footnote"}]',
        '[{"url": "https://a.example", "placement": "inline", "rank": 1}]',
        "[oops",
        7,
    ],
)
def test_parse_citations_rejects_malformed_input(value: Any) -> None:
    with pytest.raises(FieldError) as excinfo:
        base.parse_citations(value)
    assert excinfo.value.column == "citations"


# ------------------------------------------------------------------ 快照


def test_snapshot_entry(tmp_path: Path) -> None:
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "a.bin").write_bytes(b"abc")
    # SHA-256("abc") 是公开已知值
    known = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert base.snapshot_entry(tmp_path, "snapshots\\a.bin") == {
        "path": "snapshots/a.bin",
        "sha256": known,
        "media_type": "application/octet-stream",
    }
    assert base.snapshot_entry(tmp_path, "snapshots/a.bin", sha256=known)["sha256"] == known
    for bad_path in ("/etc/hosts", "snapshots/../x", " "):
        with pytest.raises(FieldError):
            base.snapshot_entry(tmp_path, bad_path)
    with pytest.raises(FieldError, match="64 位"):
        base.snapshot_entry(tmp_path, "snapshots/a.bin", sha256="ABC")
    with pytest.raises(FieldError, match="不符"):
        base.snapshot_entry(tmp_path, "snapshots/a.bin", sha256="0" * 64)
    with pytest.raises(FieldError, match="不存在"):
        base.snapshot_entry(tmp_path, "snapshots/none.png")


# ------------------------------------------------------------------ 原因与范围


def test_fill_null_reasons_only_fills_missing_reasons() -> None:
    observation = {
        "model_version": None,
        "logged_in": None,
        "region": "CN",
        "device": None,
        "snapshots": [],
        "null_reasons": {"logged_in": "用户自述未登录状态不明"},
    }
    base.fill_null_reasons(
        observation, {"model_version": "甲", "logged_in": "乙", "region": "丙", "snapshots": "丁"}
    )
    assert observation["null_reasons"] == {
        "logged_in": "用户自述未登录状态不明",
        "model_version": "甲",
        "snapshots": "丁",
    }
    complete = dict.fromkeys(base.NULLABLE_FIELDS, "x") | {"snapshots": [{"path": "a"}]}
    base.fill_null_reasons(complete, {"snapshots": "丁"})
    assert "null_reasons" not in complete


def test_scope_problem(tmp_path: Path) -> None:
    proj, _ = sealed_project(tmp_path / "p")
    assert base.scope_problem(proj, "engine-a", "manual") is None
    column, message = base.scope_problem(proj, "engine-b", "manual") or ("", "")
    assert column == "channel"
    assert "engine-b" in message
    column, message = base.scope_problem(proj, "engine-x", "api") or ("", "")
    assert column == "platform"


# ------------------------------------------------------------------ 写入器


def test_sink_appends_in_batches_and_counts(tmp_path: Path) -> None:
    root = tmp_path / "p"
    proj, sealed = sealed_project(root)
    records = [make_observation(proj, sealed, 1)]
    records.append(make_observation(proj, sealed, 2, status="invalid", invalid_reason="garbled"))
    records.append(make_observation(proj, sealed, 3, status="invalid", invalid_reason="timeout"))
    records.append(make_observation(proj, sealed, 4, status="invalid", invalid_reason="timeout"))
    with base.ObservationSink(root, batch_size=3) as sink:
        for record in records[:2]:
            sink.add(record)
        assert not (root / "observations.jsonl").exists(), "未满一批时还在缓冲区"
        sink.add(records[2])
        assert len(project.load_observations(root)) == 3, "满一批立即落盘"
        sink.add(records[3])
        assert sink.has_trace("trace-000004")
        assert not sink.has_trace("trace-000009")
        assert sink.id_conflict("obs-000001", "trace-other")
        assert not sink.id_conflict("obs-000001", "trace-000001")
        assert not sink.id_conflict("obs-new", "trace-new")
    assert project.load_observations(root) == records
    assert sink.counts() == {
        "written": 4,
        "valid": 1,
        "invalid": 3,
        "invalid_by_reason": {"garbled": 1, "timeout": 2},
    }
    reopened = base.ObservationSink(root)
    assert reopened.has_trace("trace-000002")
    assert reopened.id_conflict("obs-000002", "trace-other")


# ------------------------------------------------------------------ 导入的边角


def test_unknown_template_format_and_csv_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csv

    with pytest.raises(InputError, match="未知模板格式"):
        fileimport.template("xlsx")
    root = tmp_path / "p"
    sealed_project(root)
    source = write_csv(tmp_path / "in.csv", IMPORT_FIELDS, [import_row()])

    def broken_reader(*args: Any, **kwargs: Any) -> Any:
        raise csv.Error("模拟的 CSV 解析故障")

    monkeypatch.setattr(csv, "reader", broken_reader)
    code, data = run_cli_json("import", str(root), "--input", str(source))
    assert code == 2
    assert "不是合法的 CSV" in data["message"]


def test_import_cell_level_errors(tmp_path: Path) -> None:
    root = tmp_path / "p"
    sealed_project(root)
    fields = [*IMPORT_FIELDS, "null_reasons", "ext", "synthetic"]
    rows = [
        import_row(attempt="两次"),  # 2
        import_row(query_id="Q002", null_reasons="{bad"),  # 3
        import_row(query_id="Q003", snapshots='["a", 5]'),  # 4：快照元素类型不对
        import_row(query_id="Q004", snapshots="[bad"),  # 5
        import_row(query_id="Q005", ext="[1]"),  # 6：ext 不是对象
        import_row(query_id="Q006", invalid_reason="timeout"),  # 7：有效观测不能带无效原因
        import_row(query_id="Q007", snapshots='{"path": "x"}'),  # 8：快照不是列表
        import_row(query_id="Q008", null_reasons='{"mood": "不存在的字段"}'),  # 9：契约校验
        import_row(query_id="Q009", status="invalid", invalid_reason="bored"),  # 10
        import_row(
            query_id="Q010",
            ext='{"batch": "b-1"}',
            null_reasons='{"region": "页面未显示地域"}',
            synthetic="false",
        ),  # 11：合法
        *[import_row(query_id="Q011", attempt=str(n), status="x") for n in range(1, 61)],
    ]
    source = write_csv(tmp_path / "in.csv", fields, rows)
    code, data = run_cli_json("import", str(root), "--input", str(source))
    assert code == 1
    located = {(item["line"], item["column"]) for item in data["errors"] if item["line"] <= 11}
    assert located == {
        (2, "attempt"),
        (3, "null_reasons"),
        (4, "snapshots"),
        (5, "snapshots"),
        (6, "ext"),
        (7, "invalid_reason"),
        (8, "snapshots"),
        (9, "null_reasons"),
        (10, "invalid_reason"),
    }
    assert data["written"] == 1
    record = project.load_observations(root)[0]
    assert record["ext"] == {"batch": "b-1"}
    assert record["null_reasons"]["region"] == "页面未显示地域"
    assert record["synthetic"] is True, "项目为合成数据时，观测一律带合成标记"
    assert data["errors_total"] == 69
    assert len(read_json(root / "out" / "import-errors.json")["errors"]) == 69

    from conftest import run_cli

    code, out, _ = run_cli("import", str(root), "--input", str(source))
    assert code == 1
    assert "完整清单见 out/import-errors.json" in out
    assert out.count("第 ") <= 55


def test_prompt_text_difference_is_mentioned_in_text_output(tmp_path: Path) -> None:
    from conftest import run_cli

    root = tmp_path / "p"
    sealed_project(root)
    fields = ["prompt_text", *IMPORT_FIELDS]
    source = write_csv(tmp_path / "in.csv", fields, [import_row(prompt_text="换了一种问法？")])
    code, out, _ = run_cli("import", str(root), "--input", str(source))
    assert code == 0
    assert "提问原文与封存题目不一致" in out
