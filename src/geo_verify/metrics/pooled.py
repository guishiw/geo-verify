"""把若干个互不重叠的范围（平台 × 通道）合并成一个范围。

交叉验证的合并行、基线对比的整体行都只合并“两侧都有数据”的平台或通道，
而 Scope 只能表达单个平台、单个通道。这里不另写一套“有效观测”逻辑：
每个范围的分子、分母和按题聚合的（分子, 分母）都直接取自 Engine 的计算结果，
合并时按题相加；窗口取并集，进入分母的观测数相加（范围互不重叠，所以不会重复计数）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from geo_verify.metrics.engine import Computation, Engine
from geo_verify.metrics.index import ProjectIndex
from geo_verify.stats import assess_sufficiency

NO_CELL_REASON = "没有可合并的范围"


@dataclass(frozen=True)
class Pooled:
    """合并后的一个比例：按题聚合的（分子, 分母）、涉及的窗口和进入分母的观测数。

    clusters 只含分母大于 0 的题，按题目编号升序；cells 是实际并入（分母大于 0）的
    (平台, 通道)；分母为 0 时 null_reason 写明原因。
    """

    metric_id: str
    clusters: dict[str, tuple[int, int]]
    windows: tuple[str, ...]
    observations: int
    cells: tuple[tuple[str | None, str | None], ...]
    null_reason: str | None

    @property
    def numerator(self) -> int:
        return sum(numerator for numerator, _ in self.clusters.values())

    @property
    def denominator(self) -> int:
        return sum(denominator for _, denominator in self.clusters.values())

    @property
    def value(self) -> float | None:
        denominator = self.denominator
        return self.numerator / denominator if denominator else None

    @property
    def queries(self) -> list[str]:
        return list(self.clusters)

    def vectors(self) -> tuple[list[int], list[int]]:
        """按题目编号升序的分子向量与分母向量。"""
        return (
            [numerator for numerator, _ in self.clusters.values()],
            [denominator for _, denominator in self.clusters.values()],
        )

    def sufficiency(self, *, min_windows: int, min_valid_observations: int) -> dict[str, Any]:
        return assess_sufficiency(
            windows=len(self.windows),
            valid_observations=self.observations,
            min_windows=min_windows,
            min_valid_observations=min_valid_observations,
        )


def pool(metric_id: str, computations: Sequence[Computation]) -> Pooled:
    """合并同一指标在若干互不重叠范围内的计算结果；分母为 0 的范围不并入。"""
    totals: dict[str, list[int]] = {}
    windows: set[str] = set()
    observations = 0
    cells: list[tuple[str | None, str | None]] = []
    reasons: list[str] = []
    for computation in computations:
        result = computation.result
        if result["metric_id"] != metric_id:
            raise ValueError(f"不能把 {result['metric_id']} 的结果并入 {metric_id}")
        if not result["denominator"]:
            if result["null_reason"] and result["null_reason"] not in reasons:
                reasons.append(result["null_reason"])
            continue
        cells.append((result["scope"]["platform"], result["scope"]["channel"]))
        for query, (numerator, denominator) in computation.clusters.items():
            bucket = totals.setdefault(query, [0, 0])
            bucket[0] += numerator
            bucket[1] += denominator
        windows.update(result["ext"]["coverage"]["windows"])
        observations += result["sufficiency"]["valid_observations"]
    clusters = {query: (totals[query][0], totals[query][1]) for query in sorted(totals)}
    null_reason = None if clusters else ("；".join(reasons) or NO_CELL_REASON)
    return Pooled(
        metric_id=metric_id,
        clusters=clusters,
        windows=tuple(sorted(windows)),
        observations=observations,
        cells=tuple(cells),
        null_reason=null_reason,
    )


def pooled_interval(engine: Engine, pooled: Pooled) -> tuple[dict[str, Any] | None, str | None]:
    """合并比例的区间与说明，规则与 metrics 的单个范围相同（Wilson 或按题聚类 bootstrap）。"""
    numerators, denominators = pooled.vectors()
    return engine._interval(numerators, denominators)


def _in_universe(index: ProjectIndex, codes: Any, labels: list[str]) -> set[str]:
    return {labels[int(code)] for code in np.unique(codes[index.universe])}


def known_platforms(index: ProjectIndex) -> list[str]:
    """项目登记的平台 ∪ 属于所选题库版本的观测中出现的平台。"""
    registered = {item["platform_id"] for item in index.project["platforms"]}
    return sorted(registered | _in_universe(index, index.platform, index.platforms))


def known_channels(index: ProjectIndex) -> list[str]:
    """项目登记的通道 ∪ 属于所选题库版本的观测中出现的通道。"""
    registered = {channel for item in index.project["platforms"] for channel in item["channels"]}
    return sorted(registered | _in_universe(index, index.channel, index.channels))
