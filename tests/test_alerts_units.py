"""告警内部部件的单元测试：比例变化判定、显著性检验、阈值解析、措辞检查。

期望值来自手工推算与 helpers_alerts 中独立写的公式。
"""

from __future__ import annotations

from fractions import Fraction

import pytest
from helpers_alerts import CAUSAL_TERMS, two_proportion_p, welch_p
from hypothesis import given, settings
from hypothesis import strategies as st

from geo_verify.alerts import change, thresholds, wording
from geo_verify.errors import InputError


def proportion(
    base: tuple[int, int], cur: tuple[int, int], *, increase: bool, min_count: int = 0
) -> change.Change:
    return change.proportion_change(
        change.Count(*base),
        change.Count(*cur),
        increase=increase,
        min_n=20,
        min_abs=0.15,
        min_rel=0.2,
        alpha=0.05,
        critical=0.3,
        min_count=min_count,
    )


def test_two_proportion_p_value_matches_hand_formula() -> None:
    assert change.two_proportion_p_value(
        change.Count(24, 30), change.Count(12, 30), increase=False
    ) == pytest.approx(two_proportion_p(24, 30, 12, 30, increase=False), abs=1e-12)
    assert change.two_proportion_p_value(
        change.Count(6, 30), change.Count(18, 30), increase=True
    ) == pytest.approx(two_proportion_p(6, 30, 18, 30, increase=True), abs=1e-12)
    # 合并比例为 0 或 1：没有变异，没有证据 → p = 1
    assert (
        change.two_proportion_p_value(change.Count(0, 30), change.Count(0, 30), increase=True)
        == 1.0
    )
    assert (
        change.two_proportion_p_value(change.Count(30, 30), change.Count(20, 20), increase=False)
        == 1.0
    )


def test_mean_shift_p_value() -> None:
    before = [2] * 18 + [5] * 2
    after = [2] * 8 + [6] * 12
    assert change.mean_shift_p_value(before, after) == pytest.approx(
        welch_p(before, after), abs=1e-12
    )
    # 两侧方差都为 0：均值后移 → p = 0；不后移 → p = 1；单条观测的方差按 0 处理
    assert change.mean_shift_p_value([1, 1], [3, 3]) == 0.0
    assert change.mean_shift_p_value([3, 3], [3, 3]) == 1.0
    assert change.mean_shift_p_value([1], [2]) == 0.0


def test_proportion_change_statuses() -> None:
    triggered = proportion((24, 30), (12, 30), increase=False)
    assert triggered.status == "triggered"
    assert triggered.severity == "critical"
    assert triggered.difference == pytest.approx(-0.4)
    assert triggered.relative_change == pytest.approx(-0.5)
    small = proportion((5, 5), (1, 5), increase=False)
    assert small.status == "insufficient"
    assert small.reason is not None
    assert "20" in small.reason
    assert small.p_value is None
    assert proportion((80, 100), (70, 100), increase=False).status == "not_triggered"
    # 基线为 0 不可能“下降”；上升时相对升幅视为无穷大
    zero = proportion((0, 30), (0, 30), increase=False)
    assert zero.status == "not_triggered"
    assert zero.relative_change is None
    rise = proportion((0, 30), (6, 30), increase=True, min_count=3)
    assert rise.status == "triggered"
    assert rise.relative_change is None
    assert rise.severity == "warning"  # 升 0.2 < 0.3
    assert proportion((0, 30), (6, 30), increase=True, min_count=7).status == "not_triggered"


def test_proportion_change_boundaries_are_exact() -> None:
    # 0.7 − 0.55 用二进制浮点算是 0.1499999…；阈值按十进制精确比较，恰好等于 0.15 时触发。
    assert 0.7 - 0.55 < 0.15
    result = change.proportion_change(
        change.Count(70, 100),
        change.Count(55, 100),
        increase=False,
        min_n=20,
        min_abs=0.15,
        min_rel=0.2,
        alpha=0.05,
        critical=0.3,
    )
    assert result.status == "triggered"
    assert change.at_least(Fraction(3, 20), 0.15)
    assert not change.at_least(Fraction(299, 2000), 0.15)


@settings(max_examples=200, deadline=None)
@given(
    nb=st.integers(1, 120),
    nc=st.integers(1, 120),
    fb=st.floats(0, 1),
    fc=st.floats(0, 1),
    increase=st.booleans(),
)
def test_proportion_change_properties(
    nb: int, nc: int, fb: float, fc: float, increase: bool
) -> None:
    xb, xc = round(fb * nb), round(fc * nc)
    result = proportion((xb, nb), (xc, nc), increase=increase, min_count=3 if increase else 0)
    if min(nb, nc) < 20:
        # 小样本无论波动多大都不触发
        assert result.status == "insufficient"
        return
    pb, pc = Fraction(xb, nb), Fraction(xc, nc)
    moved = pc - pb if increase else pb - pc
    if result.status == "triggered":
        assert moved >= Fraction(3, 20)
        assert result.p_value is not None
        assert result.p_value <= 0.05
        if pb > 0:
            assert moved / pb >= Fraction(1, 5)
        if increase:
            assert xc >= 3
    else:
        assert result.status == "not_triggered"
        # 反过来：幅度、相对幅度、例数、显著性全部满足时必须触发
        pooled = Fraction(xb + xc, nb + nc)
        meets = (
            moved >= Fraction(3, 20)
            and (pb == 0 or moved / pb >= Fraction(1, 5))
            and (not increase or xc >= 3)
            and 0 < pooled < 1
        )
        if meets:
            assert two_proportion_p(xb, nb, xc, nc, increase=increase) > 0.05 - 1e-9
    assert result.difference == pytest.approx(float(pc - pb))


def test_parse_override() -> None:
    assert thresholds.parse_override("mention_drop.alpha=0.1") == ("mention_drop", "alpha", 0.1)
    assert thresholds.parse_override("leads.max_domains=5") == ("leads", "max_domains", 5)
    for bad in ("alpha=0.1", "mention_drop.alpha", "mention_drop.alpha=", "a.b.c=1"):
        with pytest.raises(InputError):
            thresholds.parse_override(bad)


def test_defaults_are_named_constants() -> None:
    assert thresholds.DEFAULT_THRESHOLDS["mention_drop"]["min_abs_drop"] == (
        thresholds.MENTION_DROP_MIN_ABS_DROP
    )
    assert thresholds.DEFAULT_THRESHOLDS["platform_conflict"]["min_each"] == (
        thresholds.PLATFORM_CONFLICT_MIN_EACH
    )
    resolved, overrides = thresholds.resolve_thresholds({}, [])
    assert resolved == thresholds.DEFAULT_THRESHOLDS
    assert resolved is not thresholds.DEFAULT_THRESHOLDS
    assert overrides == []


def test_wording_guard() -> None:
    for term in CAUSAL_TERMS[:5]:
        assert term in wording.FORBIDDEN_TERMS
        assert wording.find_forbidden(f"提及率下降，{term}待查") == [term]
        with pytest.raises(RuntimeError):
            wording.ensure_clean(f"提及率下降，{term}待查")
    assert wording.ensure_clean("同期出现的相关线索，待核查") == "同期出现的相关线索，待核查"
    assert wording.find_forbidden("仅为相关线索") == []
