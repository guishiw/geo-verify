"""抽样复核的抽样规则与 CSV 安全导出（单元测试）。

抽样数量的期望值来自手工推算（⌈fraction × 题数⌉）；抽中哪些题的期望值来自
tests/helpers_recheck.py 里的独立参考实现。
"""

from __future__ import annotations

import codecs
import csv
import io
import math
import random
from pathlib import Path

import pytest
from helpers_recheck import reference_sample
from hypothesis import given, settings
from hypothesis import strategies as st

from geo_verify.errors import InputError
from geo_verify.report import csvsafe
from geo_verify.report.recheck import sample_query_ids, sample_size


def _ids(count: int) -> list[str]:
    return [f"Q{index:03d}" for index in range(1, count + 1)]


@pytest.mark.parametrize(
    ("total", "fraction", "expected"),
    [
        (50, 0.2, 10),
        (51, 0.2, 11),  # 10.2 向上取整
        (3, 0.2, 1),  # 0.6 向上取整
        (1, 0.2, 1),
        (5, 0.2, 1),
        (6, 0.2, 2),
        (30, 0.1, 3),  # 浮点直接相乘得 3.0000000000000004，不能因此多抽一题
        (10, 0.7, 7),  # 浮点直接相乘得 7.000000000000001
        (10, 0.25, 3),
        (7, 1.0, 7),
        (7, 1, 7),
        (1000, 0.001, 1),
        (1001, 0.001, 2),
        (3, 1e-05, 1),
    ],
)
def test_sample_size_is_ceiling_of_fraction_times_total(
    total: int, fraction: float, expected: int
) -> None:
    assert sample_size(total, fraction) == expected


@pytest.mark.parametrize("fraction", [0, 0.0, -0.1, 1.0000001, 1.5, math.nan, math.inf])
def test_sample_size_rejects_fraction_outside_unit_interval(fraction: float) -> None:
    with pytest.raises(InputError):
        sample_size(10, fraction)


def test_sample_size_rejects_non_numeric_fraction_and_empty_bank() -> None:
    with pytest.raises(InputError):
        sample_size(10, True)
    with pytest.raises(InputError):
        sample_size(10, "0.2")  # type: ignore[arg-type]
    with pytest.raises(InputError):
        sample_size(0, 0.2)


def test_sample_matches_independent_reference() -> None:
    ids = _ids(50)
    sampled = sample_query_ids(ids, 0.2, 20261001)
    assert sampled == reference_sample(ids, 1, 5, 20261001)
    assert len(sampled) == 10
    assert sampled == sorted(sampled)
    assert set(sampled) <= set(ids)
    assert sample_query_ids(_ids(51), 0.2, 7) == reference_sample(_ids(51), 1, 5, 7)
    assert sample_query_ids(_ids(3), 0.2, 7) == reference_sample(_ids(3), 1, 5, 7)


def test_sample_is_stable_for_same_seed_and_ignores_input_order() -> None:
    ids = _ids(50)
    first = sample_query_ids(ids, 0.2, 42)
    assert sample_query_ids(ids, 0.2, 42) == first
    shuffled = list(ids)
    random.Random(1).shuffle(shuffled)
    assert sample_query_ids(shuffled, 0.2, 42) == first
    assert sample_query_ids(reversed(ids), 0.2, 42) == first
    # 重复的题目编号只算一题
    assert sample_query_ids([*ids, *ids], 0.2, 42) == first


def test_different_seeds_give_different_samples() -> None:
    ids = _ids(50)
    samples = {tuple(sample_query_ids(ids, 0.2, seed)) for seed in range(8)}
    # 50 取 10 有一百亿种以上的组合，8 个 seed 的结果不应相同
    assert len(samples) == 8


def test_fraction_one_selects_every_query() -> None:
    ids = _ids(9)
    assert sample_query_ids(ids, 1.0, 3) == ids


@pytest.mark.parametrize("seed", [-1, 1.5, True, "7"])
def test_sample_rejects_invalid_seed(seed: object) -> None:
    with pytest.raises(InputError):
        sample_query_ids(_ids(5), 0.2, seed)  # type: ignore[arg-type]


@settings(max_examples=200, deadline=None)
@given(
    total=st.integers(min_value=1, max_value=300),
    thousandths=st.integers(min_value=1, max_value=1000),
    seed=st.integers(min_value=0, max_value=2**32),
)
def test_sample_size_and_uniqueness_for_any_bank_and_fraction(
    total: int, thousandths: int, seed: int
) -> None:
    ids = _ids(total)
    fraction = thousandths / 1000
    sampled = sample_query_ids(ids, fraction, seed)
    expected_size = (thousandths * total + 999) // 1000  # 整数算术的向上取整
    assert len(sampled) == expected_size
    assert sample_size(total, fraction) == expected_size
    assert len(set(sampled)) == len(sampled)
    assert set(sampled) <= set(ids)
    assert 1 <= len(sampled) <= total
    assert sampled == reference_sample(ids, thousandths, 1000, seed)


@settings(max_examples=200, deadline=None)
@given(
    total=st.integers(min_value=1, max_value=500),
    fraction=st.floats(min_value=0, max_value=1, exclude_min=True, allow_nan=False),
)
def test_sample_size_bounds_for_any_float_fraction(total: int, fraction: float) -> None:
    size = sample_size(total, fraction)
    assert 1 <= size <= total
    # 向上取整：不少于 fraction × 题数，且多出的不到 1 题（留出浮点误差余量）
    assert size >= fraction * total - 1e-9
    assert size < fraction * total + 1 + 1e-9


def test_safe_cell_prefixes_formula_triggers() -> None:
    tab, carriage_return = chr(9), chr(13)
    assert csvsafe.safe_cell("=1+1") == "'=1+1"
    assert csvsafe.safe_cell("+8600") == "'+8600"
    assert csvsafe.safe_cell("-5") == "'-5"
    assert csvsafe.safe_cell("@SUM(A1)") == "'@SUM(A1)"
    assert csvsafe.safe_cell(tab + "=1") == "'" + tab + "=1"
    assert csvsafe.safe_cell(carriage_return + "=1") == "'" + carriage_return + "=1"


def test_safe_cell_leaves_ordinary_values_alone() -> None:
    assert csvsafe.safe_cell("可以考虑澜序 S7。") == "可以考虑澜序 S7。"
    assert csvsafe.safe_cell("a=b") == "a=b"
    assert csvsafe.safe_cell("续航 620 km - CLTC") == "续航 620 km - CLTC"
    assert csvsafe.safe_cell("") == ""
    assert csvsafe.safe_cell(None) == ""
    assert csvsafe.safe_cell(True) == "true"
    assert csvsafe.safe_cell(False) == "false"
    assert csvsafe.safe_cell(3) == "3"
    assert csvsafe.safe_cell(0.25) == "0.25"
    # 负数同样以减号开头，按规则一并加前缀
    assert csvsafe.safe_cell(-2) == "'-2"


def test_render_csv_has_bom_and_round_trips(tmp_path: Path) -> None:
    newline = chr(10)
    rows = [
        ["obs-000001", "=HYPERLINK(1)", '含"引号"和,逗号', None, True],
        ["obs-000002", "第一行" + newline + "第二行", "-1", 2, False],
    ]
    data = csvsafe.render_csv(["a", "b", "c", "d", "e"], rows)
    assert data.startswith(codecs.BOM_UTF8)
    assert not data[len(codecs.BOM_UTF8) :].startswith(codecs.BOM_UTF8)
    parsed = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    assert parsed == [
        ["a", "b", "c", "d", "e"],
        ["obs-000001", "'=HYPERLINK(1)", '含"引号"和,逗号', "", "true"],
        ["obs-000002", "第一行" + newline + "第二行", "'-1", "2", "false"],
    ]
    target = tmp_path / "nested" / "sheet.csv"
    csvsafe.write_csv(target, ["a", "b", "c", "d", "e"], rows)
    assert target.read_bytes() == data


def test_render_csv_rejects_ragged_rows() -> None:
    with pytest.raises(ValueError, match="列数"):
        csvsafe.render_csv(["a", "b"], [["only-one"]])
