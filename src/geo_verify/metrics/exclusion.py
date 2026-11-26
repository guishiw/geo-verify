"""剔除审计：剔除了多少、按什么规则、规则是否预先登记、是否偏向不利样本。

剔除只能通过审计事件表达。这里把“被剔除但原本有效”的观测与保留下来的有效观测放在一起比较：
如果被剔除样本在目标指标上明显更差（或对“越低越好”的指标明显更高），
就说明剔除抬高了结果，记为“选择性剔除”。

判定条件（三项同时满足）：
- 被剔除样本中可比较的条数 ≥ SELECTIVE_MIN_EXCLUDED；
- 保留样本与被剔除样本的比例差（朝不利方向）≥ SELECTIVE_MIN_DIFFERENCE；
- 单侧超几何检验（Fisher 精确检验的单侧形式）p < SELECTIVE_ALPHA。
"""

from __future__ import annotations

import dataclasses
import math
from collections import Counter
from typing import Any

import numpy as np

from geo_verify.metrics.definitions import METRICS
from geo_verify.metrics.engine import Engine, Parameters
from geo_verify.metrics.index import AUDIT_PREFIX, ProjectIndex

SELECTIVE_ALPHA = 0.05
SELECTIVE_MIN_EXCLUDED = 5
SELECTIVE_MIN_DIFFERENCE = 0.1
COMPARABLE_METRICS = (
    "mention_rate",
    "top1_rate",
    "top3_rate",
    "top5_rate",
    "recommendation_rate",
    "negative_miscitation_rate",
    "target_source_presence",
)


def hypergeometric_tail(
    population: int, successes: int, draws: int, observed: int, *, lower: bool
) -> float:
    """超几何分布的单侧尾概率。

    从 population 个样本（其中 successes 个命中）里不放回地取 draws 个，
    命中数 X ≤ observed（lower=True）或 X ≥ observed（lower=False）的概率。
    """

    def log_choose(n: int, k: int) -> float:
        return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)

    low = max(0, draws - (population - successes))
    high = min(draws, successes)
    span = range(low, min(observed, high) + 1) if lower else range(max(observed, low), high + 1)
    total = log_choose(population, draws)
    probability = sum(
        math.exp(
            log_choose(successes, hits) + log_choose(population - successes, draws - hits) - total
        )
        for hits in span
    )
    return min(1.0, float(probability))


def _counterfactual(index: ProjectIndex) -> ProjectIndex:
    """把“被审计事件剔除、但本身有效”的观测放回去后的索引（只用于比较，不用于任何指标）。"""
    audit_codes = [code for code, rule in enumerate(index.rules) if rule.startswith(AUDIT_PREFIX)]
    restorable = (
        np.isin(index.state, audit_codes) & index.status_valid & (index.query < len(index.queries))
    )
    state = index.state.copy()
    state[restorable] = 0
    return dataclasses.replace(index, state=state, _mask_cache={})


def _lower_is_better(engine: Engine, metric_id: str) -> bool:
    registration = engine.registrations.get(metric_id)
    if registration is not None and registration.threshold is not None:
        return registration.threshold["op"] in ("<", "<=")
    return METRICS[metric_id].higher_is_better is False


def exclusion_audit(index: ProjectIndex, engine: Engine, params: Parameters) -> dict[str, Any]:
    prereg = index.project["preregistration"]
    registered = sorted(item["rule_id"] for item in prereg["exclusion_rules"])
    listed = Counter(index.excluded_by_audit.values())
    state_counts = np.bincount(index.state, minlength=len(index.rules))
    effective = {
        rule[len(AUDIT_PREFIX) :]: int(state_counts[code])
        for code, rule in enumerate(index.rules)
        if rule.startswith(AUDIT_PREFIX)
    }
    rules = [
        {
            "rule_id": rule_id,
            "registered": rule_id in registered,
            "listed_observations": listed[rule_id],
            "excluded_observations": effective.get(rule_id, 0),
        }
        for rule_id in sorted(listed)
    ]
    comparisons: list[dict[str, Any]] = []
    if effective:
        restored = Engine(_counterfactual(index), params)
        acceptance = [
            metric_id
            for metric_id, registration in engine.registrations.items()
            if registration.role == "acceptance" and metric_id in COMPARABLE_METRICS
        ]
        for metric_id in dict.fromkeys(["mention_rate", *sorted(acceptance)]):
            comparisons.append(_compare(engine, restored, metric_id))
    return {
        "registered_rules": registered,
        "rules": rules,
        "unregistered_rules": [item["rule_id"] for item in rules if not item["registered"]],
        "excluded_observations": sum(effective.values()),
        "thresholds": {
            "alpha": SELECTIVE_ALPHA,
            "min_excluded": SELECTIVE_MIN_EXCLUDED,
            "min_difference": SELECTIVE_MIN_DIFFERENCE,
        },
        "comparisons": comparisons,
        "selective": any(item["selective"] for item in comparisons),
    }


def _compare(engine: Engine, restored: Engine, metric_id: str) -> dict[str, Any]:
    """比较保留样本与被剔除样本在某个指标上的比例。"""
    kept = engine.compute(metric_id).result
    everything = restored.compute(metric_id).result
    kept_num, kept_den = int(kept["numerator"]), int(kept["denominator"])
    all_num, all_den = int(everything["numerator"]), int(everything["denominator"])
    out_num, out_den = all_num - kept_num, all_den - kept_den
    lower_is_better = _lower_is_better(engine, metric_id)
    comparison: dict[str, Any] = {
        "metric_id": metric_id,
        "direction": "lower_is_better" if lower_is_better else "higher_is_better",
        "retained": _side(kept_num, kept_den),
        "excluded": _side(out_num, out_den),
        "value_if_included": all_num / all_den if all_den else None,
        "p_value": None,
        "selective": False,
        "note": None,
    }
    if out_den == 0 or kept_den == 0:
        comparison["note"] = (
            "被剔除或保留的样本中没有可比较的观测（可能缺少标注），无法判断是否偏向"
        )
        return comparison
    # 剔除“抬高结果”的方向：越高越好的指标，被剔除样本的比例更低；越低越好的指标则相反
    gap = kept_num / kept_den - out_num / out_den
    unfavorable_gap = -gap if lower_is_better else gap
    p_value = hypergeometric_tail(all_den, all_num, out_den, out_num, lower=not lower_is_better)
    comparison["p_value"] = p_value
    if out_den < SELECTIVE_MIN_EXCLUDED:
        comparison["note"] = (
            f"被剔除的可比较样本只有 {out_den} 条，少于 {SELECTIVE_MIN_EXCLUDED} 条，"
            "不做选择性判定；上面的数字与 p 值照常列出，仅供人工核查"
        )
        return comparison
    failed: list[str] = []
    if unfavorable_gap < SELECTIVE_MIN_DIFFERENCE:
        failed.append(
            f"朝不利方向的比例差 {unfavorable_gap:.4f} 小于登记的下限 {SELECTIVE_MIN_DIFFERENCE:g}"
        )
    if p_value >= SELECTIVE_ALPHA:
        failed.append(f"单侧检验 p 值 {p_value:.4g} 不小于 {SELECTIVE_ALPHA:g}")
    comparison["selective"] = not failed
    if failed:
        comparison["note"] = "未判为选择性剔除：" + "；".join(failed)
    return comparison


def _side(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }
