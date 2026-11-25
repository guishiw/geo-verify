"""按题聚类的 bootstrap。

跨题汇总的比例 = Σ分子 / Σ分母。同一道题的多次提问彼此相关，不能当独立样本，
所以重抽的单位是“题”：每轮有放回地抽取 k 道题（k 为有数据的题数），
把抽中题目的分子、分母整体带入，再计算汇总比例；重复多轮后取百分位区间。

确定性：
- 重抽下标由 numpy 的 PCG64 生成器按 seed 产生；
- 每轮的分子、分母是整数矩阵按行求和（精确，无浮点累加误差），最后只做一次除法；
- 因此同输入、同 seed、同一份依赖锁，输出逐字节一致。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from functools import lru_cache

import numpy as np
import numpy.typing as npt

IntArray = npt.NDArray[np.int64]
FloatArray = npt.NDArray[np.float64]

DEFAULT_ITERATIONS = 1000
DEFAULT_LEVEL = 0.95
# 单块重抽下标的元素上限（约 32 MB）。题数很多时分块生成，限制内存峰值。
CHUNK_CELLS = 1 << 22


def _check(iterations: int, level: float) -> None:
    if iterations < 1:
        raise ValueError("iterations 必须为正整数")
    if not 0.0 < level < 1.0:
        raise ValueError(f"置信水平必须在 0 与 1 之间：{level!r}")


def _as_counts(values: Sequence[int], name: str) -> IntArray:
    array = np.asarray(values, dtype=np.int64)
    if array.ndim != 1:
        raise ValueError(f"{name} 必须是一维序列")
    if array.size and int(array.min()) < 0:
        raise ValueError(f"{name} 不能为负")
    return array


@lru_cache(maxsize=8)
def _single_chunk(seed: int, iterations: int, clusters: int) -> IntArray:
    indices = np.random.default_rng(seed).integers(
        0, clusters, size=(iterations, clusters), dtype=np.int64
    )
    indices.flags.writeable = False
    return indices


def _index_chunks(seed: int, iterations: int, clusters: int) -> Iterator[tuple[int, IntArray]]:
    """产出 (起始轮次, 下标矩阵)。分块方式只取决于 (iterations, clusters)，结果可复现。"""
    if iterations * clusters <= CHUNK_CELLS:
        yield 0, _single_chunk(seed, iterations, clusters)
        return
    rows = max(1, CHUNK_CELLS // clusters)
    rng = np.random.default_rng(seed)
    for start in range(0, iterations, rows):
        count = min(rows, iterations - start)
        yield start, rng.integers(0, clusters, size=(count, clusters), dtype=np.int64)


def _resampled_sums(columns: Sequence[IntArray], seed: int, iterations: int) -> list[IntArray]:
    """对若干列整数按同一组重抽下标求每轮的和（保持配对关系）。"""
    clusters = int(columns[0].size)
    sums = [np.empty(iterations, dtype=np.int64) for _ in columns]
    for start, indices in _index_chunks(seed, iterations, clusters):
        stop = start + indices.shape[0]
        for column, target in zip(columns, sums, strict=True):
            target[start:stop] = column[indices].sum(axis=1)
    return sums


def _percentile_interval(estimates: FloatArray, level: float) -> tuple[float, float]:
    tail = (1.0 - level) / 2.0
    low, high = np.quantile(estimates, [tail, 1.0 - tail])
    return float(low), float(high)


def cluster_bootstrap_ratio(
    numerators: Sequence[int],
    denominators: Sequence[int],
    *,
    seed: int,
    iterations: int = DEFAULT_ITERATIONS,
    level: float = DEFAULT_LEVEL,
) -> tuple[float, float]:
    """汇总比例 Σ分子/Σ分母 的聚类 bootstrap 百分位区间。

    每个位置是一道题的分子与分母；分母为 0 的题没有信息，不参与重抽。
    调用方须按确定的顺序（如题目编号升序）传入，结果才可复现。
    """
    _check(iterations, level)
    num = _as_counts(numerators, "numerators")
    den = _as_counts(denominators, "denominators")
    if num.size != den.size:
        raise ValueError("numerators 与 denominators 长度不一致")
    keep = den > 0
    num, den = num[keep], den[keep]
    if den.size == 0:
        raise ValueError("没有分母大于 0 的聚类，无法重抽")
    num_sums, den_sums = _resampled_sums([num, den], seed, iterations)
    return _percentile_interval(num_sums / den_sums, level)


def cluster_bootstrap_mean(
    values: Sequence[float],
    *,
    seed: int,
    iterations: int = DEFAULT_ITERATIONS,
    level: float = DEFAULT_LEVEL,
) -> tuple[float, float]:
    """每题一个数值（如同题前后比例差）时，其均值的聚类 bootstrap 百分位区间。"""
    _check(iterations, level)
    data = np.asarray(values, dtype=np.float64)
    if data.ndim != 1 or data.size == 0:
        raise ValueError("至少需要 1 个聚类的数值")
    clusters = int(data.size)
    means = np.empty(iterations, dtype=np.float64)
    for start, indices in _index_chunks(seed, iterations, clusters):
        means[start : start + indices.shape[0]] = data[indices].sum(axis=1) / clusters
    return _percentile_interval(means, level)


def paired_cluster_bootstrap_difference(
    numerators_a: Sequence[int],
    denominators_a: Sequence[int],
    numerators_b: Sequence[int],
    denominators_b: Sequence[int],
    *,
    seed: int,
    iterations: int = DEFAULT_ITERATIONS,
    level: float = DEFAULT_LEVEL,
) -> tuple[float, float]:
    """同题配对的差值区间：B 的汇总比例 − A 的汇总比例。

    每个位置是同一道题在 A、B 两侧的数据；重抽时两侧使用同一组题目下标，保持配对。
    两侧的分母都必须大于 0（调用方先取两侧都有数据的题）。
    """
    _check(iterations, level)
    columns = [
        _as_counts(numerators_a, "numerators_a"),
        _as_counts(denominators_a, "denominators_a"),
        _as_counts(numerators_b, "numerators_b"),
        _as_counts(denominators_b, "denominators_b"),
    ]
    if len({int(column.size) for column in columns}) != 1 or columns[0].size == 0:
        raise ValueError("配对数据的四个序列长度必须一致且不为空")
    if int(columns[1].min()) <= 0 or int(columns[3].min()) <= 0:
        raise ValueError("配对重抽要求每道题两侧的分母都大于 0")
    num_a, den_a, num_b, den_b = _resampled_sums(columns, seed, iterations)
    return _percentile_interval(num_b / den_b - num_a / den_a, level)


def unpaired_cluster_bootstrap_difference(
    numerators_a: Sequence[int],
    denominators_a: Sequence[int],
    numerators_b: Sequence[int],
    denominators_b: Sequence[int],
    *,
    seed: int,
    iterations: int = DEFAULT_ITERATIONS,
    level: float = DEFAULT_LEVEL,
) -> tuple[float, float]:
    """两侧题目不同、无法配对时的差值区间（B − A）：两侧各自独立地按题重抽。

    A 侧用 seed，B 侧用 seed + 1，保证两侧的重抽相互独立且可复现。
    """
    _check(iterations, level)
    sides: list[FloatArray] = []
    for offset, (numerators, denominators) in enumerate(
        ((numerators_a, denominators_a), (numerators_b, denominators_b))
    ):
        num = _as_counts(numerators, "numerators")
        den = _as_counts(denominators, "denominators")
        if num.size != den.size:
            raise ValueError("numerators 与 denominators 长度不一致")
        keep = den > 0
        num, den = num[keep], den[keep]
        if den.size == 0:
            raise ValueError("没有分母大于 0 的聚类，无法重抽")
        num_sums, den_sums = _resampled_sums([num, den], seed + offset, iterations)
        sides.append(num_sums / den_sums)
    return _percentile_interval(sides[1] - sides[0], level)
