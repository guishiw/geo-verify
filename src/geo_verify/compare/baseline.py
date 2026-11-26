"""基线对比：同一套封存题库下，基线期与当前期按题配对比较。

口径（输出结构见 docs/outputs.md 的 out/compare.json）：
- 拒绝比较（status = refused，结果为空）：两期所选封存题库的 content_hash 不同（题库版本不同，禁止混算）；
  或任一（平台, 通道）的模型 / 产品版本集合在两期之间不同，或任一期内部出现多个版本（须重建基线）。
  不提供绕过开关。指定 --platform 时只检查该平台。
- 每个指标一行整体（platform = null）加每个平台一行；每行只合并两期都有数据的（平台, 通道），
  避免两期采集的平台、通道构成不同造成虚假的“提升”。
- 两期的分子、分母、按题聚合的（分子, 分母）全部取自 metrics 的 Engine（同一套“有效观测”）。
- 差值 = 两期共有的题逐题计算比例差（当前 − 基线）后取平均（每题等权）；
  区间 = 对题重抽的 bootstrap 百分位区间（stats.paired_comparison）。
- 结论表示数值方向：区间下界 > 0 为“提升”，上界 < 0 为“下降”，否则“不能判定”；
  任一期不满足各自预先登记的样本充分性（窗口数、有效观测数）时一律“不能判定”。
  favorable 表示对目标主体是否有利：越低越好的指标（如负面误引率）数值下降为有利；仅描述的指标为 null。
- 整体 status：拒绝 → refused；否则任一指标的主行（整体行，或 --platform 指定的平台行）样本不足 → insufficient；
  否则 ok。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from geo_verify import project as project_module
from geo_verify.errors import EXIT_CHECK_FAILED, EXIT_INSUFFICIENT, EXIT_OK, InputError
from geo_verify.metrics.definitions import (
    KIND_RATIO,
    METRIC_IDS,
    METRICS,
    resolve_registered_id,
)
from geo_verify.metrics.engine import SCHEMA_VERSION, Computation, Engine, Parameters, Scope
from geo_verify.metrics.index import ProjectIndex
from geo_verify.metrics.pooled import Pooled, known_channels, known_platforms, pool
from geo_verify.stats import (
    CONCLUSION_DOWN,
    CONCLUSION_UNDETERMINED,
    CONCLUSION_UP,
    model_version_changes,
    paired_comparison,
)

STATUS_LABELS = {"ok": "已比较", "refused": "拒绝比较", "insufficient": "样本不足"}
EXIT_CODES = {"ok": EXIT_OK, "refused": EXIT_CHECK_FAILED, "insufficient": EXIT_INSUFFICIENT}
CONCLUSION_CODES = {
    CONCLUSION_UP: "improved",
    CONCLUSION_DOWN: "declined",
    CONCLUSION_UNDETERMINED: "inconclusive",
}
DEFAULT_METRIC = "mention_rate"
# 只支持按观测计、可按题聚合的比例类指标；按日指标与按复核项计的复核通过率除外。
SUPPORTED_METRICS: tuple[str, ...] = tuple(
    metric_id
    for metric_id, definition in METRICS.items()
    if definition.kind == KIND_RATIO
    and definition.unit == "observation"
    and metric_id not in ("recommendation_rate_daily", "recheck_pass_rate")
)
METHOD = {
    "difference": "两期共有的题逐题计算比例差（当前 − 基线），再对题取平均（每题等权）；"
    "baseline / current 的 value 是各自合并后的总体比例，与差值的口径不同",
    "interval": "按题重抽的 bootstrap 百分位区间（同题配对：每轮抽中的题同时带入两期的数据）",
    "conclusion": "conclusion 表示数值方向：区间下界 > 0 为提升，上界 < 0 为下降，否则不能判定；"
    "任一期样本不足时一律不能判定",
    "favorable": "对目标主体是否有利：越高越好的指标提升为有利；越低越好的指标（如负面误引率）"
    "下降为有利；仅描述的指标为 null；不能判定时为 null",
    "scope": "每行只合并两期都有数据的（平台, 通道）",
}
LIMITATIONS = [
    "模型或产品版本变化时一律拒绝比较，本期不提供人工例外开关；确需例外时只能重建基线。",
    "版本变化只能从观测记录的 model_version 识别；未展示模型版本的通道（如部分网页端）无法识别版本变化。",
    "两期必须使用同一版封存题库；题库改版后旧基线不能沿用。",
]


@dataclass(frozen=True)
class Limits:
    """样本充分性阈值：时间窗口数、有效观测数。"""

    min_windows: int
    min_valid_observations: int

    @classmethod
    def of(cls, params: Parameters) -> Limits:
        return cls(params.min_windows, params.min_valid_observations)


# ---------------------------------------------------------------- 指标选择


def resolve_metrics(project: dict[str, Any], requested: list[str] | None) -> tuple[list[str], bool]:
    """返回 (要比较的指标, 是否因为没有登记可比较的验收指标而退回默认指标)。

    未指定 --metric 时取当前项目预先登记为 acceptance 的可比较指标；没有则用 mention_rate。
    """
    basis: str = project["preregistration"]["accuracy_basis"]
    chosen: set[str] = set()
    if requested:
        for item in requested:
            resolved = resolve_registered_id(item, basis)
            if not resolved:
                raise InputError(f"未知指标：{item}；基线对比可用：{', '.join(SUPPORTED_METRICS)}")
            unsupported = [metric for metric in resolved if metric not in SUPPORTED_METRICS]
            if unsupported:
                raise InputError(
                    f"基线对比不支持指标 {item}：只支持按观测计、可按题聚合的比例类指标"
                    f"（可用：{', '.join(SUPPORTED_METRICS)}）"
                )
            chosen.update(resolved)
        return [metric for metric in METRIC_IDS if metric in chosen], False
    for item in project["preregistration"]["metrics"]:
        if item["role"] != "acceptance":
            continue
        chosen.update(
            metric
            for metric in resolve_registered_id(item["metric_id"], basis)
            if metric in SUPPORTED_METRICS
        )
    if not chosen:
        return [DEFAULT_METRIC], True
    return [metric for metric in METRIC_IDS if metric in chosen], False


# ---------------------------------------------------------------- 拒绝比较


def _version_sets(index: ProjectIndex, channel: str, platform: str | None) -> dict[str, set[str]]:
    return {
        name: set(versions)
        for (name, current_channel), versions in index.model_versions.items()
        if current_channel == channel and (platform is None or name == platform)
    }


def refusal_for(
    baseline: ProjectIndex, current: ProjectIndex, platform: str | None = None
) -> dict[str, Any] | None:
    """两期不可比较时返回 refusal 对象，否则返回 None。"""
    before, after = baseline.prompt_set, current.prompt_set
    if before["content_hash"] != after["content_hash"]:
        return {
            "code": "prompt_set_mismatch",
            "message": "两期所选的封存题库不是同一版本（content_hash 不同）：题库版本不同，禁止混算。"
            "须用同一版封存题库重新采集基线后再比较。",
            "details": {
                "baseline": {"version": before["version"], "content_hash": before["content_hash"]},
                "current": {"version": after["version"], "content_hash": after["content_hash"]},
            },
        }
    within: list[dict[str, Any]] = [
        {"side": side, "platform": name, "channel": channel, "versions": sorted(versions)}
        for side, index in (("baseline", baseline), ("current", current))
        for (name, channel), versions in sorted(index.model_versions.items())
        if len(versions) > 1 and (platform is None or name == platform)
    ]
    channels = sorted(
        {channel for _, channel in baseline.model_versions}
        | {channel for _, channel in current.model_versions}
    )
    between: list[dict[str, Any]] = [
        {**change, "channel": channel}
        for channel in channels
        for change in model_version_changes(
            _version_sets(baseline, channel, platform), _version_sets(current, channel, platform)
        )
    ]
    between.sort(key=lambda item: (item["platform"], item["channel"]))
    if not within and not between:
        return None
    places = sorted({f"{item['platform']}（{item['channel']}）" for item in [*between, *within]})
    return {
        "code": "model_version_changed",
        "message": f"模型或产品版本发生变化（{'、'.join(places)}）：版本变化前后的数据默认禁止比较，"
        "须重建基线。",
        "details": {"between": between, "within": within},
    }


# ---------------------------------------------------------------- 单行


def _side(pooled: Pooled, limits: Limits) -> dict[str, Any]:
    sufficiency = pooled.sufficiency(
        min_windows=limits.min_windows, min_valid_observations=limits.min_valid_observations
    )
    return {
        "value": pooled.value,
        "null_reason": pooled.null_reason,
        "numerator": pooled.numerator,
        "denominator": pooled.denominator,
        "queries": len(pooled.clusters),
        "windows": sufficiency["windows"],
        "valid_observations": sufficiency["valid_observations"],
        "sufficient": sufficiency["sufficient"],
        "sufficiency_reasons": sufficiency["reasons"],
    }


def _paired_means(
    baseline: Pooled, current: Pooled, queries: list[str]
) -> tuple[float | None, float | None]:
    if not queries:
        return None, None
    rates = [
        [side.clusters[query][0] / side.clusters[query][1] for query in queries]
        for side in (baseline, current)
    ]
    return sum(rates[0]) / len(queries), sum(rates[1]) / len(queries)


def _favorable(metric_id: str, code: str) -> bool | None:
    better = METRICS[metric_id].higher_is_better
    if better is None or code == "inconclusive":
        return None
    return better if code == "improved" else not better


def compare_pooled(
    metric_id: str,
    platform: str | None,
    baseline: Pooled,
    current: Pooled,
    *,
    params: Parameters,
    baseline_limits: Limits,
    current_limits: Limits,
) -> dict[str, Any]:
    """一行比较：同题配对的差值、区间与结论（样本不足时一律不能判定）。"""
    sides = {
        "baseline": _side(baseline, baseline_limits),
        "current": _side(current, current_limits),
    }
    comparison = paired_comparison(
        baseline.clusters,
        current.clusters,
        seed=params.seed,
        iterations=params.iterations,
        level=params.level,
    )
    common = sorted(baseline.clusters.keys() & current.clusters.keys())
    baseline_mean, current_mean = _paired_means(baseline, current, common)
    sufficient = all(side["sufficient"] for side in sides.values())
    short = [
        f"{'基线' if name == 'baseline' else '当前'}期样本不足：{'；'.join(side['sufficiency_reasons'])}"
        for name, side in sides.items()
        if not side["sufficient"]
    ]
    if not baseline.cells or not current.cells:
        conclusion = CONCLUSION_UNDETERMINED
        reason = "基线与当前没有共同的（平台, 通道）数据，无法比较，不能判定"
    elif short:
        conclusion = CONCLUSION_UNDETERMINED
        reason = "；".join(short) + "。样本不足时一律不能判定"
    elif comparison.pairs == 0:
        conclusion = CONCLUSION_UNDETERMINED
        reason = "两期没有共同的题，无法同题配对，不能判定"
    elif comparison.low is None or comparison.high is None:
        conclusion = CONCLUSION_UNDETERMINED
        reason = "两期共同的题只有 1 道，不足 2 道，无法按题重抽区间，不能判定"
    else:
        conclusion = comparison.conclusion
        position = {
            CONCLUSION_UP: "区间下界大于 0",
            CONCLUSION_DOWN: "区间上界小于 0",
            CONCLUSION_UNDETERMINED: "区间包含 0",
        }[conclusion]
        reason = (
            f"按 {comparison.pairs} 道共同的题同题配对：差值 {comparison.difference:+.4f}，"
            f"{params.level:.0%} 区间 [{comparison.low:+.4f}, {comparison.high:+.4f}]，"
            f"{position}，{conclusion}"
        )
    interval = (
        None
        if comparison.low is None or comparison.high is None
        else {
            "method": "paired_cluster_bootstrap",
            "level": params.level,
            "low": comparison.low,
            "high": comparison.high,
        }
    )
    code = CONCLUSION_CODES[conclusion]
    return {
        "metric_id": metric_id,
        "platform": platform,
        "higher_is_better": METRICS[metric_id].higher_is_better,
        **sides,
        "cells": [{"platform": name, "channel": channel} for name, channel in baseline.cells],
        "difference": comparison.difference,
        "baseline_paired_mean": baseline_mean,
        "current_paired_mean": current_mean,
        "interval": interval,
        "paired_queries": comparison.pairs,
        "baseline_only_queries": len(baseline.clusters.keys() - current.clusters.keys()),
        "current_only_queries": len(current.clusters.keys() - baseline.clusters.keys()),
        "sufficient": sufficient,
        "conclusion": conclusion,
        "conclusion_code": code,
        "favorable": _favorable(metric_id, code),
        "reason": reason,
    }


# ---------------------------------------------------------------- 整体


def _summary(index: ProjectIndex, platform: str | None) -> dict[str, Any]:
    mask = index.valid & index.scope_mask(platform=platform)
    return {
        "project_id": index.project["project_id"],
        "prompt_set": {
            "version": index.prompt_set["version"],
            "content_hash": index.prompt_set["content_hash"],
        },
        "windows": int(np.unique(index.window[mask]).size),
        "valid_observations": int(np.count_nonzero(mask)),
    }


def _notices(
    baseline: ProjectIndex, current: ProjectIndex, params: Parameters, fallback: bool
) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    if fallback:
        notices.append(
            {
                "code": "default_metric",
                "severity": "info",
                "message": f"当前项目没有预先登记为验收指标的可比较比例类指标，默认比较 {DEFAULT_METRIC}。",
            }
        )
    for side, index in (("baseline", baseline), ("current", current)):
        if (
            project_module.preregistration_hash(index.project)
            != index.prompt_set["preregistration_hash"]
        ):
            notices.append(
                {
                    "code": "preregistration_changed",
                    "severity": "warning",
                    "message": f"{'基线' if side == 'baseline' else '当前'}项目的预先登记内容在题库封存后被改动："
                    "样本充分性阈值等口径不能视为预先登记，比较结果只能作为事后参考。",
                    "details": {"side": side},
                }
            )
    registered = current.project["preregistration"]["statistics"]
    if (params.seed, params.iterations) != (registered["seed"], registered["bootstrap_iterations"]):
        notices.append(
            {
                "code": "parameters_overridden",
                "severity": "warning",
                "message": "本次计算的 seed 或重抽次数与当前项目预先登记的值不同，区间与结论仅供参考。",
                "details": {
                    "registered": {
                        "seed": registered["seed"],
                        "iterations": registered["bootstrap_iterations"],
                    },
                    "used": {"seed": params.seed, "iterations": params.iterations},
                },
            }
        )
    return notices


def _cells(
    engine: Engine, metric_id: str, platforms: list[str], channels: list[str]
) -> dict[tuple[str, str], Computation]:
    return {
        (name, channel): engine.compute(metric_id, Scope(platform=name, channel=channel))
        for name in platforms
        for channel in channels
    }


def _rows(
    metric_id: str,
    scopes: list[str | None],
    engines: tuple[Engine, Engine],
    platforms: list[str],
    channels: list[str],
    params: Parameters,
) -> list[dict[str, Any]]:
    before = _cells(engines[0], metric_id, platforms, channels)
    after = _cells(engines[1], metric_id, platforms, channels)
    limits = (Limits.of(engines[0].params), Limits.of(engines[1].params))
    rows: list[dict[str, Any]] = []
    for scope in scopes:
        keys = [key for key in before if scope is None or key[0] == scope]
        has = {
            key: (bool(before[key].result["denominator"]), bool(after[key].result["denominator"]))
            for key in keys
        }
        common = [key for key in keys if all(has[key])]
        row = compare_pooled(
            metric_id,
            scope,
            pool(metric_id, [before[key] for key in common]),
            pool(metric_id, [after[key] for key in common]),
            params=params,
            baseline_limits=limits[0],
            current_limits=limits[1],
        )
        row["unmatched_cells"] = [
            {
                "platform": key[0],
                "channel": key[1],
                "side": "baseline" if has[key][0] else "current",
            }
            for key in keys
            if any(has[key]) and not all(has[key])
        ]
        registration = engines[1].registrations.get(metric_id)
        row["preregistered"] = registration is not None
        row["role"] = registration.role if registration is not None else "post_hoc"
        rows.append(row)
    return rows


def _baseline_params(index: ProjectIndex, params: Parameters) -> Parameters:
    """基线项目的参数：样本充分性阈值取基线项目自己的预先登记值，重抽参数与当前一致。"""
    sufficiency = index.project["preregistration"]["sufficiency"]
    return Parameters(
        seed=params.seed,
        iterations=params.iterations,
        level=params.level,
        min_windows=sufficiency["min_windows"],
        min_valid_observations=sufficiency["min_valid_observations"],
    )


def compare_document(
    baseline: ProjectIndex,
    current: ProjectIndex,
    params: Parameters,
    metric_ids: list[str],
    *,
    platform: str | None = None,
    fallback: bool = False,
) -> dict[str, Any]:
    """计算基线对比，返回 out/compare.json 的内容（写入当前项目）。"""
    platforms = sorted(set(known_platforms(baseline)) | set(known_platforms(current)))
    if platform is not None:
        if platform not in platforms:
            raise InputError(f"未知平台：{platform}；两期项目中的平台：{', '.join(platforms)}")
        platforms = [platform]
    refusal = refusal_for(baseline, current, platform)
    results: list[dict[str, Any]] = []
    if refusal is None:
        channels = sorted(set(known_channels(baseline)) | set(known_channels(current)))
        baseline_params = _baseline_params(baseline, params)
        engines = (Engine(baseline, baseline_params), Engine(current, params))
        scopes: list[str | None] = [None, *platforms] if platform is None else [platform]
        for metric_id in metric_ids:
            results.extend(_rows(metric_id, scopes, engines, platforms, channels, params))
    lead = [item for item in results if item["platform"] == platform]
    if refusal is not None:
        status, reason = "refused", refusal["message"]
    elif any(not item["sufficient"] for item in lead):
        status = "insufficient"
        names = "、".join(item["metric_id"] for item in lead if not item["sufficient"])
        reason = f"样本不足（{names}）：任一期不满足各自预先登记的样本充分性时一律不能判定"
    else:
        status = "ok"
        reason = "；".join(f"{item['metric_id']}：{item['conclusion']}" for item in lead)
    project = current.project
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "compare",
        "project_id": project["project_id"],
        "baseline": _summary(baseline, platform),
        "current": _summary(current, platform),
        "metrics": metric_ids,
        "platform": platform,
        "parameters": {
            "seed": params.seed,
            "iterations": params.iterations,
            "confidence_level": params.level,
        },
        "sufficiency_thresholds": {
            side: {
                "min_windows": limits.min_windows,
                "min_valid_observations": limits.min_valid_observations,
            }
            for side, limits in (
                ("baseline", Limits.of(_baseline_params(baseline, params))),
                ("current", Limits.of(params)),
            )
        },
        "method": METHOD,
        "limitations": LIMITATIONS,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "reason": reason,
        "refusal": refusal,
        "notices": _notices(baseline, current, params, fallback),
        "results": results,
    }
    if project.get("synthetic") is True or baseline.project.get("synthetic") is True:
        document["synthetic"] = True
    return document
