"""两个窗口之间的变化判定：同时看幅度、相对幅度、样本量和简单的显著性检验。

- 阈值比较用分数精确进行，阈值按其十进制写法解释（0.15 就是 3/20），边界不受浮点误差左右；
- 比例的显著性用合并方差的两比例 z 检验（单侧），平均位次用 Welch z 检验（单侧，正态近似）；
- 同一道题的重复提问彼此相关，这里把观测当独立样本，p 值偏乐观，
  所以它只作为“避免小样本误报”的门槛之一，与最小样本数、绝对和相对幅度同时满足才触发。
- 输出的 p 值保留 6 位小数，避免不同平台数学库末位差异破坏逐字节复现；判定用未舍入的值。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from statistics import NormalDist
from typing import Any

TRIGGERED = "triggered"
NOT_TRIGGERED = "not_triggered"
INSUFFICIENT = "insufficient"
P_VALUE_DIGITS = 6

_NORMAL = NormalDist()


def exact(threshold: float) -> Fraction:
    """阈值的十进制精确值：0.15 → 3/20（而不是 0.15 的二进制近似值）。"""
    return Fraction(str(threshold))


def at_least(value: Fraction, threshold: float) -> bool:
    return value >= exact(threshold)


@dataclass(frozen=True)
class Count:
    numerator: int
    denominator: int

    @property
    def ratio(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def to_dict(self) -> dict[str, Any]:
        value = self.numerator / self.denominator if self.denominator else None
        return {"numerator": self.numerator, "denominator": self.denominator, "value": value}


@dataclass(frozen=True)
class Change:
    """一次变化判定的结果。status 为 insufficient 时其余统计量为 None。"""

    status: str
    reason: str | None = None
    difference: float | None = None
    relative_change: float | None = None
    p_value: float | None = None
    severity: str | None = None

    @property
    def triggered(self) -> bool:
        return self.status == TRIGGERED


def two_proportion_p_value(baseline: Count, current: Count, *, increase: bool) -> float:
    """合并方差的两比例 z 检验的单侧 p 值。increase=True 检验“当前高于基线”。

    合并比例为 0 或 1 时两窗没有任何变异，p 记为 1（没有变化的证据）。
    """
    n1, n2 = baseline.denominator, current.denominator
    pooled = (baseline.numerator + current.numerator) / (n1 + n2)
    variance = pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2)
    if variance <= 0.0:
        return 1.0
    z = (current.numerator / n2 - baseline.numerator / n1) / math.sqrt(variance)
    return _NORMAL.cdf(-z if increase else z)


def _mean_variance(values: Sequence[int]) -> tuple[float, float]:
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0
    return mean, sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def mean_shift_p_value(before: Sequence[int], after: Sequence[int]) -> float:
    """Welch z 检验“after 的均值大于 before”的单侧 p 值（正态近似）。

    两侧方差都为 0 时：均值确实变大记 p = 0，否则记 p = 1。
    """
    mean_b, var_b = _mean_variance(before)
    mean_a, var_a = _mean_variance(after)
    scale = math.sqrt(var_b / len(before) + var_a / len(after))
    shift = mean_a - mean_b
    if scale == 0.0:
        return 0.0 if shift > 0 else 1.0
    return _NORMAL.cdf(-shift / scale)


def _sample_reason(baseline: int, current: int, minimum: int, unit: str) -> str:
    return (
        f"样本不足：基线 {baseline} {unit}、当前 {current} {unit}，两边都至少需要 {minimum} {unit}"
    )


def proportion_change(
    baseline: Count,
    current: Count,
    *,
    increase: bool,
    min_n: int,
    min_abs: float,
    min_rel: float,
    alpha: float,
    critical: float,
    min_count: int = 0,
    unit: str = "条",
) -> Change:
    """比例从基线到当前是否“明显”下降（increase=False）或上升（increase=True）。

    触发条件同时满足：两窗分母都 ≥ min_n；变化幅度 ≥ min_abs；相对幅度 ≥ min_rel
    （基线为 0 时上升的相对幅度视为无穷大，下降则不可能）；当前分子 ≥ min_count；单侧 p ≤ alpha。
    变化幅度 ≥ critical 时为 critical，否则为 warning。
    """
    if min(baseline.denominator, current.denominator) < min_n:
        reason = _sample_reason(baseline.denominator, current.denominator, min_n, unit)
        return Change(INSUFFICIENT, reason=reason)
    before, after = baseline.ratio, current.ratio
    moved = after - before if increase else before - after
    relative = None if before == 0 else float((after - before) / before)
    p_value = two_proportion_p_value(baseline, current, increase=increase)
    relative_ok = moved > 0 if before == 0 else at_least(moved / before, min_rel)
    triggered = (
        at_least(moved, min_abs)
        and relative_ok
        and current.numerator >= min_count
        and p_value <= alpha
    )
    return Change(
        TRIGGERED if triggered else NOT_TRIGGERED,
        difference=float(after - before),
        relative_change=relative,
        p_value=round(p_value, P_VALUE_DIGITS),
        severity=("critical" if at_least(moved, critical) else "warning") if triggered else None,
    )


def mean_shift(
    before: Sequence[int],
    after: Sequence[int],
    *,
    min_n: int,
    min_increase: float,
    alpha: float,
    critical: float,
) -> Change:
    """平均值是否明显变大（位次数字变大即“后移”）。条件与 proportion_change 同理。"""
    if min(len(before), len(after)) < min_n:
        reason = _sample_reason(len(before), len(after), min_n, "次")
        return Change(INSUFFICIENT, reason=reason)
    shift = Fraction(sum(after), len(after)) - Fraction(sum(before), len(before))
    p_value = mean_shift_p_value(before, after)
    triggered = at_least(shift, min_increase) and p_value <= alpha
    return Change(
        TRIGGERED if triggered else NOT_TRIGGERED,
        difference=float(shift),
        p_value=round(p_value, P_VALUE_DIGITS),
        severity=("critical" if at_least(shift, critical) else "warning") if triggered else None,
    )
