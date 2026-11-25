"""基础设施测试：规范化哈希、JSONL、时间、CLI 骨架与退出码。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from conftest import run_cli
from hypothesis import given
from hypothesis import strategies as st

from geo_verify import __version__, canon, jsonl, timeutil
from geo_verify.errors import (
    CheckFailed,
    GeoError,
    InputError,
    InsufficientData,
)

# ------------------------------------------------------------------ canon

# RFC 8785 附带的已知取值：键按 UTF-16 码元排序、数字按 ECMAScript 规则序列化。
BS = chr(92)  # 反斜杠；用拼接而不是转义，避免工具链改写字面量
RFC8785_NUMBERS = {
    "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
    "string": "€$" + chr(0x0F) + chr(0x0A) + "A'B" + '"' + BS + BS + '"' + "/",
    "literals": [None, True, False],
}
RFC8785_EXPECTED = (
    '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
    '"string":"€$' + BS + "u000f" + BS + "nA'B" + BS + '"' + BS * 4 + BS + '"/"}'
)


def test_canonical_json_matches_rfc8785_sample() -> None:
    assert canon.canonical_bytes(RFC8785_NUMBERS).decode("utf-8") == RFC8785_EXPECTED


def test_hash_is_independent_of_key_order_and_whitespace() -> None:
    first = json.loads('{"b": 1, "a": {"y": [1, 2], "x": "中文"}}')
    second = json.loads('{"a":{"x":"中文","y":[1,2]},"b":1}')
    assert canon.hash_obj(first) == canon.hash_obj(second)
    assert canon.hash_obj(first) != canon.hash_obj({"a": {"x": "中文", "y": [2, 1]}, "b": 1})


def test_known_sha256() -> None:
    # SHA-256("{}") 的公开已知值
    assert canon.hash_obj({}) == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    assert canon.sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_hash_without_ignores_named_fields() -> None:
    record = {"a": 1, "content_hash": "x"}
    assert canon.hash_without(record, "content_hash") == canon.hash_obj({"a": 1})


def test_unhashable_value_is_input_error() -> None:
    with pytest.raises(InputError):
        canon.canonical_bytes({"n": float("nan")})
    with pytest.raises(InputError):
        canon.canonical_bytes({"n": 2**60})


def test_sha256_file(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"abc")
    assert canon.sha256_file(path) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**40), 2**40) | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)


@given(json_values)
def test_canonical_json_roundtrip_is_stable(value: object) -> None:
    encoded = canon.canonical_bytes(value)
    assert json.loads(encoded) == value
    assert canon.canonical_bytes(json.loads(encoded)) == encoded


# ------------------------------------------------------------------ jsonl


def test_append_only_and_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "records.jsonl"
    assert jsonl.append_jsonl(path, [{"b": 1, "a": "中"}]) == 1
    before = path.read_bytes()
    assert jsonl.append_jsonl(path, [{"c": 2}]) == 1
    after = path.read_bytes()
    assert after.startswith(before), "追加不得改动已有内容"
    assert before == '{"a":"中","b":1}\n'.encode()
    assert jsonl.read_jsonl(path) == [{"a": "中", "b": 1}, {"c": 2}]
    assert [number for number, _ in jsonl.iter_jsonl(path)] == [1, 2]


def test_jsonl_module_has_no_destructive_api() -> None:
    public = {name for name in dir(jsonl) if not name.startswith("_")}
    assert not {
        name
        for name in public
        if any(word in name for word in ("delete", "remove", "truncate", "overwrite"))
    }


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("{bad json}\n", "第 1 行"),
        ('{"a":1}\n\n{"a":2}\n', "第 2 行"),
        ("[1,2]\n", "不是 JSON 对象"),
    ],
)
def test_jsonl_errors_report_line(tmp_path: Path, content: str, fragment: str) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(InputError, match=fragment):
        jsonl.read_jsonl(path)


def test_jsonl_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="文件不存在"):
        jsonl.read_jsonl(tmp_path / "none.jsonl")
    assert jsonl.read_jsonl(tmp_path / "none.jsonl", missing_ok=True) == []


def test_write_json_is_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    jsonl.write_json(first, {"b": [1, 2], "a": "值"})
    jsonl.write_json(second, {"a": "值", "b": [1, 2]})
    assert first.read_bytes() == second.read_bytes()
    assert first.read_text(encoding="utf-8").endswith("}\n")
    assert jsonl.read_json(first) == {"a": "值", "b": [1, 2]}


def test_read_json_errors(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="文件不存在"):
        jsonl.read_json(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(InputError, match="不是合法 JSON"):
        jsonl.read_json(bad)


# ------------------------------------------------------------------ timeutil


def test_local_date_uses_project_timezone_at_day_boundary() -> None:
    # 手工推算：UTC 15:59:59 是上海 23:59:59（当日），UTC 16:00:00 是上海次日 00:00:00
    assert timeutil.local_date("2026-10-08T15:59:59Z") == date(2026, 10, 8)
    assert timeutil.local_date("2026-10-08T16:00:00Z") == date(2026, 10, 9)
    assert timeutil.local_date("2026-10-08T16:00:00Z", "UTC") == date(2026, 10, 8)
    # 纽约 2026-10-08 为夏令时（UTC-4）：UTC 03:59 仍是 10 月 7 日
    assert timeutil.local_date("2026-10-08T03:59:00Z", "America/New_York") == date(2026, 10, 7)


@pytest.mark.parametrize(
    "text", ["2026-10-08T10:00:00+08:00", "2026-10-08 10:00:00", "2026-13-40T00:00:00Z", "", "now"]
)
def test_parse_utc_rejects_non_utc(text: str) -> None:
    with pytest.raises(InputError):
        timeutil.parse_utc(text)


def test_format_utc_roundtrip_and_naive_rejected() -> None:
    from datetime import datetime, timedelta, timezone

    moment = datetime(2026, 10, 8, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert timeutil.format_utc(moment) == "2026-10-08T02:00:00Z"
    assert timeutil.parse_utc("2026-10-08T02:00:00.250Z").microsecond == 250000
    with pytest.raises(InputError):
        timeutil.format_utc(datetime(2026, 10, 8))
    with pytest.raises(InputError):
        timeutil.zone("Mars/Olympus")


# ------------------------------------------------------------------ errors & CLI


def test_exit_codes_follow_convention() -> None:
    assert CheckFailed("x").exit_code == 1
    assert InputError("x").exit_code == 2
    assert InsufficientData("x").exit_code == 3
    assert GeoError("x").exit_code == 4
    assert InputError("坏输入", details=[1]).to_dict() == {
        "error": "input_error",
        "message": "坏输入",
        "details": [1],
    }


def test_cli_version_and_usage_errors() -> None:
    code, out, _ = run_cli("--version")
    assert code == 0
    assert __version__ in out
    code, _, err = run_cli("no-such-command")
    assert code == 2
    assert "输入错误" in err
    code, _, _ = run_cli()
    assert code == 2


def test_python_dash_m_entry() -> None:
    import runpy

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("geo_verify", run_name="__main__", alter_sys=True)
    assert excinfo.value.code == 2
