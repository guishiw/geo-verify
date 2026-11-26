"""逐行哈希索引与行级差异定位的测试。

期望值全部由测试自己构造的行序列决定：在第 k 行做了什么改动，就期望在第 k 行报出对应类型。
行哈希的期望值用 hashlib 独立计算，不经过被测实现。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from geo_verify.evidence.lines import (
    LineChange,
    first_divergence,
    line_hash,
    locate_changes,
    parse_index,
    render_index,
    scan_file,
    split_lines,
)


def h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hashes(*texts: str) -> list[str]:
    return [h(text) for text in texts]


def kinds_and_lines(changes: list[LineChange]) -> list[tuple[str, int]]:
    return [(item.kind, item.line) for item in changes]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", []),
        (b"a\nb\n", [b"a", b"b"]),
        (b"a\nb", [b"a", b"b"]),
        (b"a\n\n", [b"a", b""]),
        (b"\n", [b""]),
        (b"a\r\nb\n", [b"a\r", b"b"]),
    ],
)
def test_split_lines(data: bytes, expected: list[bytes]) -> None:
    assert split_lines(data) == expected


def test_line_hash_is_sha256_of_raw_line_bytes() -> None:
    assert line_hash("澜序 S7".encode()) == h("澜序 S7")
    assert line_hash(b"") == hashlib.sha256(b"").hexdigest()


def test_scan_file_hashes_whole_file_and_each_line(tmp_path: Path) -> None:
    content = '{"a":1}\n{"b":"澜序"}\n{"c":3}'.encode()
    path = tmp_path / "x.jsonl"
    path.write_bytes(content)
    scan = scan_file(path, lines=True)
    assert scan.sha256 == hashlib.sha256(content).hexdigest()
    assert scan.size == len(content)
    assert scan.line_hashes == hashes('{"a":1}', '{"b":"澜序"}', '{"c":3}')
    plain = scan_file(path, lines=False)
    assert plain.sha256 == scan.sha256
    assert plain.line_hashes is None


def test_scan_file_of_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    scan = scan_file(path, lines=True)
    assert scan.size == 0
    assert scan.line_hashes == []
    assert scan.sha256 == hashlib.sha256(b"").hexdigest()


def test_index_round_trip_and_format() -> None:
    values = hashes("x", "y")
    rendered = render_index(values)
    assert rendered == (values[0] + "\n" + values[1] + "\n").encode("ascii")
    assert parse_index(rendered) == values
    assert render_index([]) == b""
    assert parse_index(b"") == []


@pytest.mark.parametrize(
    "data",
    [
        b"not-a-hash\n",
        ("A" * 64 + "\n").encode(),
        ("a" * 63 + "\n").encode(),
        ("a" * 64).encode(),
        "澜序\n".encode(),
    ],
)
def test_parse_index_rejects_malformed_content(data: bytes) -> None:
    with pytest.raises(ValueError, match="行索引"):
        parse_index(data)


BASE = ["l1", "l2", "l3", "l4", "l5"]


def test_identical_sequences_have_no_changes() -> None:
    assert locate_changes(hashes(*BASE), hashes(*BASE)) == []
    assert locate_changes([], []) == []


def test_modified_line_is_located() -> None:
    new = ["l1", "l2", "l3-篡改", "l4", "l5"]
    changes = locate_changes(hashes(*BASE), hashes(*new))
    assert changes == [LineChange("line_modified", 3, old_line=3, new_line=3)]


def test_deleted_line_is_located_by_recorded_line_number() -> None:
    new = ["l1", "l2", "l4", "l5"]
    changes = locate_changes(hashes(*BASE), hashes(*new))
    assert changes == [LineChange("line_deleted", 3, old_line=3, new_line=None)]


def test_inserted_line_is_located_by_current_line_number() -> None:
    new = ["l1", "l2", "新增", "l3", "l4", "l5"]
    changes = locate_changes(hashes(*BASE), hashes(*new))
    assert changes == [LineChange("line_inserted", 3, old_line=None, new_line=3)]


def test_appended_line_counts_as_insertion_at_the_end() -> None:
    new = [*BASE, "追加"]
    changes = locate_changes(hashes(*BASE), hashes(*new))
    assert changes == [LineChange("line_inserted", 6, old_line=None, new_line=6)]


def test_swapped_lines_are_both_reported_as_reordered() -> None:
    new = ["l1", "l4", "l3", "l2", "l5"]
    changes = locate_changes(hashes(*BASE), hashes(*new))
    assert changes == [
        LineChange("line_reordered", 2, old_line=4, new_line=2),
        LineChange("line_reordered", 4, old_line=2, new_line=4),
    ]


def test_adjacent_swap_reports_both_lines() -> None:
    new = ["l1", "l3", "l2", "l4", "l5"]
    assert kinds_and_lines(locate_changes(hashes(*BASE), hashes(*new))) == [
        ("line_reordered", 2),
        ("line_reordered", 3),
    ]


def test_truncation_reports_each_deleted_line() -> None:
    assert kinds_and_lines(locate_changes(hashes(*BASE), hashes("l1", "l2", "l3"))) == [
        ("line_deleted", 4),
        ("line_deleted", 5),
    ]


def test_everything_deleted_and_everything_new() -> None:
    assert kinds_and_lines(locate_changes(hashes("a", "b"), [])) == [
        ("line_deleted", 1),
        ("line_deleted", 2),
    ]
    assert kinds_and_lines(locate_changes([], hashes("a"))) == [("line_inserted", 1)]


def test_replacing_one_line_by_two_is_one_modification_plus_one_insertion() -> None:
    new = ["l1", "l2", "x", "y", "l4", "l5"]
    assert kinds_and_lines(locate_changes(hashes(*BASE), hashes(*new))) == [
        ("line_modified", 3),
        ("line_inserted", 4),
    ]


def test_combined_deletion_and_later_modification() -> None:
    # 删除第 2 行，并改动原第 4 行（当前第 3 行）
    new = ["l1", "l3", "l4-改", "l5"]
    changes = locate_changes(hashes(*BASE), hashes(*new))
    assert changes == [
        LineChange("line_deleted", 2, old_line=2, new_line=None),
        LineChange("line_modified", 3, old_line=4, new_line=3),
    ]


def test_duplicate_lines_deletion_is_located_inside_the_identical_run() -> None:
    # 删除第 1 行；第 1、3 行内容相同，只要报出“删除”且行号落在合理位置即可
    old = hashes("x", "y", "x")
    new = hashes("y", "x")
    assert kinds_and_lines(locate_changes(old, new)) == [("line_deleted", 1)]


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (["a", "b"], ["a", "b", "c"], None),
        (["a", "b"], ["a", "b"], None),
        ([], ["a"], None),
        (["a", "b", "c"], ["a", "X", "c"], 1),
        (["a", "b", "c"], ["a", "b"], 2),
        (["a", "b"], [], 0),
        (["a", "b", "c"], ["b", "c"], 0),
    ],
)
def test_first_divergence(old: list[str], new: list[str], expected: int | None) -> None:
    assert first_divergence(hashes(*old), hashes(*new)) == expected


distinct_lines = st.lists(st.text(min_size=1, max_size=12), min_size=1, max_size=30, unique=True)


@given(lines=distinct_lines, data=st.data())
def test_single_edit_is_always_located_exactly(lines: list[str], data: st.DataObject) -> None:
    kind = data.draw(st.sampled_from(["line_modified", "line_deleted", "line_inserted"]))
    fresh = "新内容-" + "|".join(lines)  # 与已有任何一行都不同
    edited = list(lines)
    if kind == "line_modified":
        position = data.draw(st.integers(min_value=1, max_value=len(lines)))
        edited[position - 1] = fresh
    elif kind == "line_deleted":
        position = data.draw(st.integers(min_value=1, max_value=len(lines)))
        del edited[position - 1]
    else:
        position = data.draw(st.integers(min_value=1, max_value=len(lines) + 1))
        edited.insert(position - 1, fresh)
    changes = locate_changes(hashes(*lines), hashes(*edited))
    assert kinds_and_lines(changes) == [(kind, position)]


@given(lines=distinct_lines, data=st.data())
def test_swapping_two_lines_reports_exactly_those_two(
    lines: list[str], data: st.DataObject
) -> None:
    if len(lines) < 2:
        return
    first = data.draw(st.integers(min_value=1, max_value=len(lines) - 1))
    second = data.draw(st.integers(min_value=first + 1, max_value=len(lines)))
    edited = list(lines)
    edited[first - 1], edited[second - 1] = edited[second - 1], edited[first - 1]
    changes = locate_changes(hashes(*lines), hashes(*edited))
    assert kinds_and_lines(changes) == [("line_reordered", first), ("line_reordered", second)]


small_lines = st.lists(st.sampled_from(["a", "b", "c", "d"]), max_size=12)


@given(old=small_lines, new=small_lines)
def test_any_difference_is_reported_even_with_duplicate_lines(
    old: list[str], new: list[str]
) -> None:
    changes = locate_changes(hashes(*old), hashes(*new))
    assert (changes == []) == (old == new)
    for item in changes:
        if item.kind == "line_deleted":
            assert item.old_line is not None
            assert 1 <= item.line == item.old_line <= len(old)
        else:
            assert item.new_line is not None
            assert 1 <= item.line == item.new_line <= len(new)


@given(old=small_lines, new=small_lines)
def test_first_divergence_matches_prefix_definition(old: list[str], new: list[str]) -> None:
    result = first_divergence(hashes(*old), hashes(*new))
    if new[: len(old)] == old:
        assert result is None
    else:
        assert result is not None
        assert old[:result] == new[:result]
        assert result == len(new) or old[result] != new[result]
