"""偏离度公式（本项目的定义）。

    偏离度 = |A − B| / ((A + B) / 2)；A = B = 0 时为 0

047 给出了 30% 的阈值，但没有给出偏离度的公式；这个公式是本项目的定义，
必须在封存前登记（project.json 的 preregistration.crosscheck），报告中必须注明。

判定用精确有理数计算：A = a/n、B = b/m 时
    偏离度 = 2·|a·m − b·n| / (a·m + b·n)
阈值按其十进制写法（如 0.3 即 3/10）比较，“偏离度 ≤ 阈值”为通过，
避免浮点误差在阈值边界上把“恰好等于阈值”判成超过。
"""

from __future__ import annotations

import math
from fractions import Fraction

FORMULA = "symmetric_relative_difference"
FORMULA_TEXT = "偏离度 = |A − B| / ((A + B) / 2)；A = B = 0 时为 0"
FORMULA_NOTE = (
    "此公式是本项目的定义：047 给出了 30% 的阈值，但没有给出偏离度的计算公式。"
    "公式与阈值须在题库封存前登记。"
)


def symmetric_relative_difference(a: float, b: float) -> float:
    """两个比例的对称相对差异；对非负输入取值范围为 [0, 2]。"""
    for value in (a, b):
        if not math.isfinite(value):
            raise ValueError(f"比例必须是有限数：{value!r}")
        if value < 0:
            raise ValueError(f"比例不能为负：{value!r}")
    if a == b:
        return 0.0
    return abs(a - b) / ((a + b) / 2.0)


def deviation_from_counts(
    numerator_a: int, denominator_a: int, numerator_b: int, denominator_b: int
) -> Fraction:
    """由两侧的分子、分母精确计算偏离度（有理数）。"""
    for numerator, denominator in ((numerator_a, denominator_a), (numerator_b, denominator_b)):
        if denominator <= 0:
            raise ValueError(f"分母必须为正：{denominator!r}")
        if not 0 <= numerator <= denominator:
            raise ValueError(f"分子必须在 0 与分母之间：{numerator!r}/{denominator!r}")
    cross_a = numerator_a * denominator_b
    cross_b = numerator_b * denominator_a
    if cross_a + cross_b == 0:
        return Fraction(0)
    return Fraction(2 * abs(cross_a - cross_b), cross_a + cross_b)


def passes_threshold(deviation: Fraction, threshold: float) -> bool:
    """偏离度 ≤ 阈值为通过；阈值按十进制写法转成精确有理数（0.3 → 3/10）。"""
    return deviation <= Fraction(repr(threshold))
