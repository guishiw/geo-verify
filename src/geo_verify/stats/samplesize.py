"""样本量计算。

两比例比较（每组）：正态近似、非合并方差、双侧 α、无连续性校正、向上取整。

    n = (z_{1−α/2} + z_{power})² · (p1(1−p1) + p2(1−p2)) / (p1 − p2)²

零失败样本量：连续 n 次无失败才能以给定单侧置信度说明“合格率 ≥ 目标”。

    n = ⌈ ln(1 − 置信度) / ln(目标) ⌉
"""

from __future__ import annotations

import math
from statistics import NormalDist

from geo_verify.errors import InputError

TWO_PROPORTION_BASIS = "正态近似、非合并方差、双侧检验、无连续性校正、向上取整；结果为每组所需样本量。假定各次观测相互独立：同题重复提问时须再乘以设计效应。"
ZERO_FAILURE_BASIS = "零失败口径：n = ⌈ln(1 − 置信度) / ln(目标)⌉，单侧置信度；n 次观测全部合格时才能说明合格率不低于目标。假定各次观测相互独立。"
DESIGN_EFFECT_BASIS = "设计效应 deff = 1 + (m − 1) · ICC，m 为每题提问次数；有效样本量 = n / deff。"

# 向上取整前扣除的浮点容差：避免恰为整数的结果因舍入误差被多取 1。
_EPSILON = 1e-9


def _ceil(value: float) -> int:
    return max(1, math.ceil(value - _EPSILON))


def two_proportion_sample_size(
    p1: float, p2: float, *, alpha: float = 0.05, power: float = 0.8
) -> int:
    """检出 p1 与 p2 差异所需的每组样本量。"""
    for value in (p1, p2):
        if not 0.0 <= value <= 1.0:
            raise InputError(f"比例必须在 0 到 1 之间：{value!r}")
    if p1 == p2:
        raise InputError("两个比例不能相等：差异为 0 时样本量无穷大")
    if not 0.0 < alpha < 1.0:
        raise InputError(f"alpha 必须在 0 与 1 之间：{alpha!r}")
    if not 0.0 < power < 1.0:
        raise InputError(f"功效必须在 0 与 1 之间：{power!r}")
    normal = NormalDist()
    z_total = normal.inv_cdf(1.0 - alpha / 2.0) + normal.inv_cdf(power)
    variance = p1 * (1.0 - p1) + p2 * (1.0 - p2)
    return _ceil(z_total * z_total * variance / ((p1 - p2) ** 2))


def zero_failure_sample_size(target: float, *, confidence: float = 0.95) -> int:
    """证明合格率 ≥ target 所需的连续无失败次数。"""
    if not 0.0 < target < 1.0:
        raise InputError(f"目标必须在 0 与 1 之间（不含端点）：{target!r}")
    if not 0.0 < confidence < 1.0:
        raise InputError(f"置信度必须在 0 与 1 之间：{confidence!r}")
    return _ceil(math.log(1.0 - confidence) / math.log(target))
