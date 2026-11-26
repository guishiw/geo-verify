"""Wilson 区间（无连续性校正）。

用于单题范围的比例，或其他以相互独立的单元计数的比例。
跨题汇总不得使用本函数把重复提问当独立样本，见 bootstrap.py。

公式（p = x/n，z 为标准正态分布的双侧分位数）：

    中心 = (p + z²/2n) / (1 + z²/n)
    半宽 = z · sqrt(p(1-p)/n + z²/4n²) / (1 + z²/n)
"""

from __future__ import annotations

import math
from statistics import NormalDist


def z_quantile(level: float) -> float:
    """双侧置信水平对应的标准正态分位数，如 0.95 → 1.959964。"""
    if not 0.0 < level < 1.0:
        raise ValueError(f"置信水平必须在 0 与 1 之间：{level!r}")
    return NormalDist().inv_cdf(1.0 - (1.0 - level) / 2.0)


def wilson_interval(successes: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """返回 (下界, 上界)。x=0 时下界恰为 0，x=n 时上界恰为 1。"""
    if n <= 0:
        raise ValueError("n 必须为正：分母为 0 时没有区间")
    if not 0 <= successes <= n:
        raise ValueError(f"successes 必须在 0 与 n 之间：{successes!r}/{n!r}")
    z = z_quantile(level)
    z2 = z * z
    p = successes / n
    scale = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / scale
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / scale
    low = 0.0 if successes == 0 else max(0.0, center - half)
    high = 1.0 if successes == n else min(1.0, center + half)
    return low, high
