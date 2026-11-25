"""题内相关系数（ICC）、设计效应与有效样本量。

ICC 采用适用于各题次数不等的二值数据的单因素方差分析（ANOVA）估计：

    k   = 有数据的题数，n_i = 第 i 题的次数，y_i = 第 i 题的命中数
    N   = Σ n_i，p = Σ y_i / N，p_i = y_i / n_i
    MSB = Σ n_i (p_i − p)² / (k − 1)
    MSW = Σ y_i (1 − p_i) / (N − k)
    n0  = (N − Σ n_i² / N) / (k − 1)
    ICC = (MSB − MSW) / (MSB + (n0 − 1) · MSW)

ANOVA 估计可能为负（题间差异小于纯随机时），此时截断为 0。
以下情形无法估计，返回 None：题数不足 2、每题只有 1 次（N = k）、全部命中或全部未命中。

设计效应 deff = 1 + (m − 1) · ICC；各题次数相等时 m 即每题次数，
不等时取 m = Σ n_i² / N（按观测加权的平均每题次数）。有效样本量 = N / deff。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from geo_verify.errors import InputError


@dataclass(frozen=True)
class ClusterSummary:
    clusters: int
    n: int
    icc: float | None
    design_effect: float | None
    effective_n: float | None

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "clusters": self.clusters,
            "icc": self.icc,
            "design_effect": self.design_effect,
            "effective_n": self.effective_n,
        }


def _nonempty(successes: Sequence[int], sizes: Sequence[int]) -> list[tuple[int, int]]:
    if len(successes) != len(sizes):
        raise ValueError("successes 与 sizes 长度不一致")
    return [(int(y), int(n)) for y, n in zip(successes, sizes, strict=True) if n > 0]


def anova_icc(successes: Sequence[int], sizes: Sequence[int]) -> float | None:
    """ANOVA 估计的题内相关系数，截断到 [0, 1]；无法估计时返回 None。"""
    data = _nonempty(successes, sizes)
    clusters = len(data)
    total = sum(n for _, n in data)
    if clusters < 2 or total <= clusters:
        return None
    p = sum(y for y, _ in data) / total
    between = sum(n * (y / n - p) ** 2 for y, n in data) / (clusters - 1)
    within = sum(y * (1.0 - y / n) for y, n in data) / (total - clusters)
    n0 = (total - sum(n * n for _, n in data) / total) / (clusters - 1)
    denominator = between + (n0 - 1.0) * within
    if denominator <= 0.0:
        return None
    return min(1.0, max(0.0, (between - within) / denominator))


def design_effect(cluster_size: float, icc: float) -> float:
    """deff = 1 + (m − 1) · ICC。"""
    if cluster_size < 1:
        raise InputError(f"每题次数必须 ≥ 1：{cluster_size!r}")
    if not 0.0 <= icc <= 1.0:
        raise InputError(f"ICC 必须在 0 与 1 之间：{icc!r}")
    return 1.0 + (cluster_size - 1.0) * icc


def effective_sample_size(n: float, deff: float) -> float:
    """有效样本量 = n / deff。"""
    if n < 0:
        raise InputError(f"样本量不能为负：{n!r}")
    if deff <= 0:
        raise InputError(f"设计效应必须为正：{deff!r}")
    return n / deff


def summarize_clusters(successes: Sequence[int], sizes: Sequence[int]) -> ClusterSummary:
    """给出聚类数、ICC、设计效应和有效样本量；ICC 无法估计时后三项为 None。"""
    data = _nonempty(successes, sizes)
    total = sum(n for _, n in data)
    icc = anova_icc(successes, sizes)
    if icc is None:
        return ClusterSummary(len(data), total, None, None, None)
    weighted_size = sum(n * n for _, n in data) / total
    deff = design_effect(weighted_size, icc)
    return ClusterSummary(len(data), total, icc, deff, effective_sample_size(total, deff))
