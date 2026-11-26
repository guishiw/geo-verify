"""双通道交叉验证：同一指标在两个采集通道（如 api 与 manual）上的结果是否一致。

口径（输出结构见 docs/outputs.md 的 out/crosscheck.json）：
- 两个通道的分子、分母、按题聚合的（分子, 分母）全部取自 metrics 的 Engine（同一套“有效观测”）；
- 每个平台一行；合并行（platform = null）只合并两个通道都有数据（分母大于 0）的平台；
- 偏离度见 formula.py；任一通道样本不足（预先登记的窗口数、有效观测数）时该行“不可判定”，
  即使偏离度为 0 也不判通过；样本充分时偏离度 ≤ 阈值为通过，否则不通过；
- 差值 A − B 的区间：两个通道覆盖的题目完全相同时用同题配对的聚类 bootstrap；
  题目不完全相同时无法配对，改为两个通道各自独立按题重抽（偏保守），并给出各通道自己的区间；
  任一通道不足 2 道题时差值区间为 null。
- 整体结论：合并行或任何一个样本充分的平台行不通过 → 不通过；否则合并行不可判定 → 不可判定；
  否则通过。指定 --platform 时只算该平台，该平台行代替合并行。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from geo_verify import project as project_module
from geo_verify import spec
from geo_verify.crosscheck.formula import (
    FORMULA,
    FORMULA_NOTE,
    FORMULA_TEXT,
    deviation_from_counts,
    passes_threshold,
)
from geo_verify.errors import EXIT_CHECK_FAILED, EXIT_INSUFFICIENT, EXIT_OK, InputError
from geo_verify.metrics.definitions import KIND_RATIO, METRICS, resolve_registered_id
from geo_verify.metrics.engine import SCHEMA_VERSION, Engine, Parameters, Scope
from geo_verify.metrics.index import ProjectIndex
from geo_verify.metrics.pooled import Pooled, known_platforms, pool, pooled_interval
from geo_verify.stats import (
    paired_cluster_bootstrap_difference,
    unpaired_cluster_bootstrap_difference,
)

STATUS_LABELS = {"pass": "通过", "fail": "不通过", "undetermined": "不可判定"}
EXIT_CODES = {"pass": EXIT_OK, "fail": EXIT_CHECK_FAILED, "undetermined": EXIT_INSUFFICIENT}
SUPPORTED_METRICS: tuple[str, ...] = tuple(
    metric_id
    for metric_id, definition in METRICS.items()
    if definition.kind == KIND_RATIO and metric_id != "recommendation_rate_daily"
)
_OVERRIDABLE = ("metric_id", "channel_a", "channel_b", "threshold")


def channel_names() -> tuple[str, ...]:
    """契约允许的采集通道。"""
    return tuple(spec.load_schema("project")["$defs"]["channel"]["enum"])


@dataclass(frozen=True)
class Config:
    """本次交叉验证实际使用的公式、阈值、指标与通道，以及它们是否取自预先登记内容。"""

    formula: str
    threshold: float
    metric_id: str
    channel_a: str
    channel_b: str
    registered: dict[str, Any]
    overridden: tuple[str, ...]
    preregistration_changed: bool

    @property
    def preregistered(self) -> bool:
        return not self.overridden and not self.preregistration_changed


def _resolve_metric(metric_id: str, accuracy_basis: str, *, origin: str) -> str:
    resolved = resolve_registered_id(metric_id, accuracy_basis)
    if not resolved:
        raise InputError(
            f"{origin}未知指标：{metric_id}；交叉验证可用的指标：{', '.join(SUPPORTED_METRICS)}"
        )
    if len(resolved) != 1 or resolved[0] not in SUPPORTED_METRICS:
        raise InputError(
            f"{origin}交叉验证不支持指标 {metric_id}：只支持可按题聚合分子、分母的比例类指标"
            f"（可用：{', '.join(SUPPORTED_METRICS)}）"
        )
    return resolved[0]


def resolve_config(
    project: dict[str, Any],
    prompt_set: dict[str, Any],
    *,
    metric_id: str | None = None,
    channel_a: str | None = None,
    channel_b: str | None = None,
    threshold: float | None = None,
) -> Config:
    """默认取 preregistration.crosscheck；命令行给出且与登记值不同的项记为覆盖。"""
    prereg = project["preregistration"]
    registered: dict[str, Any] = dict(prereg["crosscheck"])
    basis: str = prereg["accuracy_basis"]
    overridden: list[str] = []
    if metric_id is None:
        metric = _resolve_metric(registered["metric_id"], basis, origin="预先登记的交叉验证指标：")
    else:
        metric = _resolve_metric(metric_id, basis, origin="")
        registered_ids = resolve_registered_id(registered["metric_id"], basis)
        if registered_ids != (metric,):
            overridden.append("metric_id")
    effective: dict[str, Any] = {"metric_id": metric}
    for name, value in (
        ("channel_a", channel_a),
        ("channel_b", channel_b),
        ("threshold", threshold),
    ):
        effective[name] = registered[name] if value is None else value
        if value is not None and value != registered[name]:
            overridden.append(name)
    allowed = channel_names()
    for name in ("channel_a", "channel_b"):
        if effective[name] not in allowed:
            raise InputError(f"未知通道：{effective[name]}；可选：{', '.join(allowed)}")
    if effective["channel_a"] == effective["channel_b"]:
        raise InputError("两个通道不能相同：交叉验证比较的是两个不同的采集通道")
    limit = effective["threshold"]
    if not (isinstance(limit, int | float) and math.isfinite(limit) and 0 < limit <= 2):
        raise InputError(f"阈值必须大于 0 且不超过 2（偏离度的取值范围是 [0, 2]）：{limit!r}")
    changed = project_module.preregistration_hash(project) != prompt_set["preregistration_hash"]
    return Config(
        formula=registered["formula"],
        threshold=float(limit),
        metric_id=metric,
        channel_a=effective["channel_a"],
        channel_b=effective["channel_b"],
        registered=registered,
        overridden=tuple(name for name in _OVERRIDABLE if name in overridden),
        preregistration_changed=changed,
    )


# ---------------------------------------------------------------- 单行


def _side(engine: Engine, pooled: Pooled, channel: str) -> dict[str, Any]:
    params = engine.params
    sufficiency = pooled.sufficiency(
        min_windows=params.min_windows, min_valid_observations=params.min_valid_observations
    )
    interval, note = pooled_interval(engine, pooled)
    return {
        "channel": channel,
        "value": pooled.value,
        "null_reason": pooled.null_reason,
        "numerator": pooled.numerator,
        "denominator": pooled.denominator,
        "queries": len(pooled.clusters),
        "windows": sufficiency["windows"],
        "valid_observations": sufficiency["valid_observations"],
        "sufficient": sufficiency["sufficient"],
        "sufficiency_reasons": sufficiency["reasons"],
        "interval": interval,
        "interval_note": note,
    }


def _difference_interval(
    a: Pooled, b: Pooled, params: Parameters
) -> tuple[dict[str, Any] | None, int, str | None]:
    """差值 A − B 的区间、两侧共同的题数、说明。"""
    queries_a, queries_b = set(a.clusters), set(b.clusters)
    paired = len(queries_a & queries_b)
    if not queries_a or not queries_b:
        return None, paired, "至少一个通道没有数据，无法计算差值区间"
    numerators_a, denominators_a = a.vectors()
    numerators_b, denominators_b = b.vectors()
    if queries_a == queries_b:
        if paired < 2:
            return None, paired, "两个通道只有 1 道题，不足 2 道，无法按题重抽差值区间"
        # 函数返回“第二组 − 第一组”，所以先传 B 再传 A，得到 A − B
        low, high = paired_cluster_bootstrap_difference(
            numerators_b,
            denominators_b,
            numerators_a,
            denominators_a,
            seed=params.seed,
            iterations=params.iterations,
            level=params.level,
        )
        method, note = "paired_cluster_bootstrap", None
    else:
        if len(queries_a) < 2 or len(queries_b) < 2:
            return (
                None,
                paired,
                "两个通道的题目不完全相同，无法同题配对；且有通道不足 2 道题，无法按题重抽差值区间",
            )
        low, high = unpaired_cluster_bootstrap_difference(
            numerators_b,
            denominators_b,
            numerators_a,
            denominators_a,
            seed=params.seed,
            iterations=params.iterations,
            level=params.level,
        )
        method = "unpaired_cluster_bootstrap"
        note = (
            f"两个通道的题目不完全相同（共同的题 {paired} 道），无法同题配对："
            "差值区间改为两个通道各自独立按题重抽，未利用同题相关，偏保守"
        )
    return {"method": method, "level": params.level, "low": low, "high": high}, paired, note


def _row_status(
    config: Config,
    platform: str | None,
    merged: list[str],
    sides: tuple[dict[str, Any], dict[str, Any]],
    deviation: Fraction | None,
) -> tuple[str, str]:
    if platform is None and not merged:
        return (
            "undetermined",
            "没有两个通道都有数据的平台：合并行没有可比较的数据，不可判定",
        )
    missing = [side["channel"] for side in sides if not side["denominator"]]
    if missing:
        return (
            "undetermined",
            f"通道 {'、'.join(missing)} 在该范围没有数据（分母为 0），不可判定",
        )
    short = [side for side in sides if not side["sufficient"]]
    if short:
        details = "；".join(
            f"通道 {side['channel']} 样本不足：{'；'.join(side['sufficiency_reasons'])}"
            for side in short
        )
        return "undetermined", f"{details}。样本不足时不可判定，即使偏离度为 0 也不判通过"
    assert deviation is not None
    text = f"偏离度 {float(deviation):.4f}"
    if passes_threshold(deviation, config.threshold):
        return "pass", f"{text} 不超过阈值 {config.threshold:g}，两个通道样本充分，通过"
    return "fail", f"{text} 超过阈值 {config.threshold:g}，两个通道样本充分，不通过"


def _row(
    engine: Engine,
    config: Config,
    platform: str | None,
    merged: list[str],
    a: Pooled,
    b: Pooled,
) -> dict[str, Any]:
    sides = (_side(engine, a, config.channel_a), _side(engine, b, config.channel_b))
    exact: Fraction | None = None
    difference: float | None = None
    if a.denominator and b.denominator:
        exact = deviation_from_counts(a.numerator, a.denominator, b.numerator, b.denominator)
        difference = float(
            Fraction(a.numerator, a.denominator) - Fraction(b.numerator, b.denominator)
        )
    interval, paired, note = _difference_interval(a, b, engine.params)
    status, reason = _row_status(config, platform, merged, sides, exact)
    return {
        "platform": platform,
        "merged_platforms": merged,
        "a": sides[0],
        "b": sides[1],
        "deviation": None if exact is None else float(exact),
        "difference": difference,
        "interval": interval,
        "interval_note": note,
        "paired_queries": paired,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "reason": reason,
    }


# ---------------------------------------------------------------- 整体


def _overall(lead: dict[str, Any], platform_rows: list[dict[str, Any]]) -> tuple[str, str]:
    lead_name = "合并行" if lead["platform"] is None else f"平台 {lead['platform']}"
    failing = [item for item in [lead, *platform_rows] if item["status"] == "fail"]
    if failing:
        names = "、".join(
            "合并行" if item["platform"] is None else f"平台 {item['platform']}" for item in failing
        )
        return "fail", f"{names}的偏离度超过阈值（样本充分），交叉验证不通过"
    if lead["status"] == "undetermined":
        return "undetermined", f"{lead_name}不可判定：{lead['reason']}"
    return (
        "pass",
        f"{lead_name}的偏离度不超过阈值，且样本充分的平台行均未超过阈值，交叉验证通过",
    )


def _notices(index: ProjectIndex, params: Parameters, config: Config) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    if config.preregistration_changed:
        notices.append(
            {
                "code": "preregistration_changed",
                "severity": "error",
                "message": "预先登记内容在题库封存后被改动：交叉验证的公式、阈值、指标和通道"
                "不能视为预先登记，结果只能作为事后参考，不能用于验收结论。",
                "details": {
                    "sealed_hash": index.prompt_set["preregistration_hash"],
                    "current_hash": project_module.preregistration_hash(index.project),
                },
            }
        )
    registered = index.project["preregistration"]["statistics"]
    if (params.seed, params.iterations) != (registered["seed"], registered["bootstrap_iterations"]):
        notices.append(
            {
                "code": "parameters_overridden",
                "severity": "warning",
                "message": "本次计算的 seed 或重抽次数与预先登记值不同，区间仅供参考"
                "（通过与否只取决于偏离度与样本充分性，不受影响）。",
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


def crosscheck_document(
    index: ProjectIndex, params: Parameters, config: Config, *, platform: str | None = None
) -> dict[str, Any]:
    """计算交叉验证，返回 out/crosscheck.json 的内容。"""
    platforms = known_platforms(index)
    if platform is not None:
        if platform not in platforms:
            raise InputError(f"未知平台：{platform}；项目中的平台：{', '.join(platforms)}")
        platforms = [platform]
    engine = Engine(index, params)
    metric = config.metric_id
    cells = {
        (name, channel): engine.compute(metric, Scope(platform=name, channel=channel))
        for name in platforms
        for channel in (config.channel_a, config.channel_b)
    }
    both = [
        name
        for name in platforms
        if cells[(name, config.channel_a)].result["denominator"]
        and cells[(name, config.channel_b)].result["denominator"]
    ]
    platform_rows = [
        _row(
            engine,
            config,
            name,
            [name] if name in both else [],
            pool(metric, [cells[(name, config.channel_a)]]),
            pool(metric, [cells[(name, config.channel_b)]]),
        )
        for name in platforms
    ]
    if platform is None:
        lead = _row(
            engine,
            config,
            None,
            both,
            pool(metric, [cells[(name, config.channel_a)] for name in both]),
            pool(metric, [cells[(name, config.channel_b)] for name in both]),
        )
        results = [lead, *platform_rows]
        status, reason = _overall(lead, platform_rows)
    else:
        results = platform_rows
        status, reason = _overall(platform_rows[0], [])
    project = index.project
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "crosscheck",
        "project_id": project["project_id"],
        "prompt_set": {
            "version": index.prompt_set["version"],
            "content_hash": index.prompt_set["content_hash"],
        },
        "formula": FORMULA,
        "formula_text": FORMULA_TEXT,
        "formula_note": FORMULA_NOTE,
        "threshold": config.threshold,
        "preregistered": config.preregistered,
        "registered": config.registered,
        "overridden": list(config.overridden),
        "metric_id": metric,
        "channel_a": config.channel_a,
        "channel_b": config.channel_b,
        "platform": platform,
        "parameters": {
            "seed": params.seed,
            "iterations": params.iterations,
            "confidence_level": params.level,
        },
        "sufficiency_thresholds": {
            "min_windows": params.min_windows,
            "min_valid_observations": params.min_valid_observations,
        },
        "status": status,
        "status_label": STATUS_LABELS[status],
        "reason": reason,
        "notices": _notices(index, params, config),
        "results": results,
    }
    if project.get("synthetic") is True:
        document["synthetic"] = True
    return document
