"""基线对比：同题配对。

对每道在基线和当前都有数据的题，计算前后比例差 d_q = 当前比例 − 基线比例；
差值取各题 d_q 的平均（每题等权，避免两期各题提问次数不同带来的结构性偏差），
区间为按题重抽的 bootstrap 百分位区间。

结论只有三种：区间下界 > 0 为“提升”，上界 < 0 为“下降”，其余为“不能判定”。
模型或产品版本变化时默认禁止前后比较，须重建基线，见 model_version_changes。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from geo_verify.stats.bootstrap import cluster_bootstrap_mean

CONCLUSION_UP = "提升"
CONCLUSION_DOWN = "下降"
CONCLUSION_UNDETERMINED = "不能判定"


@dataclass(frozen=True)
class PairedComparison:
    pairs: int
    unpaired: int
    difference: float | None
    low: float | None
    high: float | None
    level: float
    conclusion: str


def conclude(low: float | None, high: float | None) -> str:
    """由差值区间给出结论；区间缺失或包含 0（含端点）时不能判定。"""
    if low is None or high is None:
        return CONCLUSION_UNDETERMINED
    if low > 0:
        return CONCLUSION_UP
    if high < 0:
        return CONCLUSION_DOWN
    return CONCLUSION_UNDETERMINED


def paired_comparison(
    before: Mapping[str, tuple[int, int]],
    after: Mapping[str, tuple[int, int]],
    *,
    seed: int,
    iterations: int,
    level: float,
) -> PairedComparison:
    """before / after：{题目编号: (分子, 分母)}。只有两侧分母都大于 0 的题参与配对。

    配对题不足 2 道时无法按题重抽，不给区间，结论为“不能判定”。
    """
    paired = sorted(
        key for key in before.keys() & after.keys() if before[key][1] > 0 and after[key][1] > 0
    )
    unpaired = len(before.keys() | after.keys()) - len(paired)
    if not paired:
        return PairedComparison(0, unpaired, None, None, None, level, CONCLUSION_UNDETERMINED)
    differences = [
        after[key][0] / after[key][1] - before[key][0] / before[key][1] for key in paired
    ]
    difference = sum(differences) / len(differences)
    if len(paired) < 2:
        return PairedComparison(1, unpaired, difference, None, None, level, CONCLUSION_UNDETERMINED)
    low, high = cluster_bootstrap_mean(differences, seed=seed, iterations=iterations, level=level)
    return PairedComparison(
        len(paired), unpaired, difference, low, high, level, conclude(low, high)
    )


def model_version_changes(
    baseline: Mapping[str, set[str]], current: Mapping[str, set[str]]
) -> list[dict[str, object]]:
    """列出模型或产品版本集合在基线与当前不一致的平台（只比较两侧都有数据的平台）。"""
    changes: list[dict[str, object]] = []
    for platform in sorted(baseline.keys() & current.keys()):
        if baseline[platform] != current[platform]:
            changes.append(
                {
                    "platform": platform,
                    "baseline": sorted(baseline[platform]),
                    "current": sorted(current[platform]),
                }
            )
    return changes
