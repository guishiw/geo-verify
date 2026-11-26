"""告警阈值：每个阈值都是有名字的常量，可由 project.json 的 ext 或命令行覆盖。

覆盖顺序：默认值 < project.json 的 `ext.alerts.thresholds` < 命令行 `--threshold 规则.键=值`。
实际使用的阈值全部写进输出的 `thresholds`，非默认值另列在 `threshold_overrides`（含来源）。

阈值的设计原则：幅度和样本量同时把关，避免小样本误报——
比例类规则同时要求两窗最小样本数、绝对变化、相对变化和单侧显著性（见 change.py）。
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

from geo_verify.errors import InputError

Number = int | float

# ---------------------------------------------------------------- mention_drop：提及率骤降与掉位
MENTION_DROP_MIN_OBSERVATIONS = 20  # 每窗至少多少条带目标主体标注的有效观测
MENTION_DROP_MIN_ABS_DROP = 0.15  # 提及率至少下降 15 个百分点
MENTION_DROP_MIN_REL_DROP = 0.2  # 且相对基线至少下降 20%
MENTION_DROP_ALPHA = 0.05  # 单侧检验的显著性水平
MENTION_DROP_CRITICAL_ABS_DROP = 0.3  # 下降达到 30 个百分点记为 critical（前三推荐率同）
MENTION_DROP_MIN_RANKABLE = 10  # 前三推荐率：每窗至少多少条可排序观测
MENTION_DROP_MIN_TOP3_ABS_DROP = 0.2
MENTION_DROP_MIN_TOP3_REL_DROP = 0.2
MENTION_DROP_MIN_RANKED = 10  # 平均位次：每窗至少多少条给出了目标位次的观测
MENTION_DROP_MIN_MEAN_RANK_INCREASE = 1.0  # 平均位次至少后移 1 位
MENTION_DROP_CRITICAL_MEAN_RANK_INCREASE = 2.0

# ---------------------------------------------------------------- negative_spread：错误负面扩散
NEGATIVE_SPREAD_MIN_OBSERVATIONS = 20
NEGATIVE_SPREAD_MIN_ABS_RISE = 0.05
NEGATIVE_SPREAD_MIN_REL_RISE = 0.5
NEGATIVE_SPREAD_MIN_CURRENT_COUNT = 3  # 当前窗口至少 3 条才可能触发
NEGATIVE_SPREAD_ALPHA = 0.05
NEGATIVE_SPREAD_CRITICAL_ABS_RISE = 0.15
NEGATIVE_SPREAD_MIN_PLATFORM_COUNT = 3  # 一个平台至少 3 条才算“出现”
NEGATIVE_SPREAD_MIN_PLATFORM_INCREASE = 1
NEGATIVE_SPREAD_CRITICAL_PLATFORM_INCREASE = 2

# ---------------------------------------------------------------- citation_failure：引用大面积失效
CITATION_FAILURE_MIN_VERIFIED = 20  # 每窗至少多少条已核验引用
CITATION_FAILURE_INVALID_SHARE_THRESHOLD = 0.3  # 当前无效占比达到即触发
CITATION_FAILURE_MIN_INVALID_COUNT = 5
CITATION_FAILURE_MIN_ABS_RISE = 0.15  # 或较基线上升 15 个百分点（同时满足相对升幅与显著性）
CITATION_FAILURE_MIN_REL_RISE = 0.5
CITATION_FAILURE_ALPHA = 0.05
CITATION_FAILURE_CRITICAL_SHARE = 0.5

# ---------------------------------------------------------------- competitor_surge：竞品异常提升
COMPETITOR_SURGE_MIN_OBSERVATIONS = 20
COMPETITOR_SURGE_MIN_ABS_RISE = 0.15
COMPETITOR_SURGE_MIN_REL_RISE = 0.25
COMPETITOR_SURGE_ALPHA = 0.05
COMPETITOR_SURGE_CRITICAL_ABS_RISE = 0.3

# ---------------------------------------------------------------- platform_conflict：同平台口径冲突
PLATFORM_CONFLICT_MIN_EACH = 3  # 正确呈现、错误呈现各至少 3 条观测

# ---------------------------------------------------------------- 线索
LEADS_MAX_DOMAINS = 10  # 每条线索最多列出的域名数

DEFAULT_THRESHOLDS: dict[str, dict[str, Number]] = {
    "mention_drop": {
        "min_observations": MENTION_DROP_MIN_OBSERVATIONS,
        "min_abs_drop": MENTION_DROP_MIN_ABS_DROP,
        "min_rel_drop": MENTION_DROP_MIN_REL_DROP,
        "alpha": MENTION_DROP_ALPHA,
        "critical_abs_drop": MENTION_DROP_CRITICAL_ABS_DROP,
        "min_rankable": MENTION_DROP_MIN_RANKABLE,
        "min_top3_abs_drop": MENTION_DROP_MIN_TOP3_ABS_DROP,
        "min_top3_rel_drop": MENTION_DROP_MIN_TOP3_REL_DROP,
        "min_ranked": MENTION_DROP_MIN_RANKED,
        "min_mean_rank_increase": MENTION_DROP_MIN_MEAN_RANK_INCREASE,
        "critical_mean_rank_increase": MENTION_DROP_CRITICAL_MEAN_RANK_INCREASE,
    },
    "negative_spread": {
        "min_observations": NEGATIVE_SPREAD_MIN_OBSERVATIONS,
        "min_abs_rise": NEGATIVE_SPREAD_MIN_ABS_RISE,
        "min_rel_rise": NEGATIVE_SPREAD_MIN_REL_RISE,
        "min_current_count": NEGATIVE_SPREAD_MIN_CURRENT_COUNT,
        "alpha": NEGATIVE_SPREAD_ALPHA,
        "critical_abs_rise": NEGATIVE_SPREAD_CRITICAL_ABS_RISE,
        "min_platform_count": NEGATIVE_SPREAD_MIN_PLATFORM_COUNT,
        "min_platform_increase": NEGATIVE_SPREAD_MIN_PLATFORM_INCREASE,
        "critical_platform_increase": NEGATIVE_SPREAD_CRITICAL_PLATFORM_INCREASE,
    },
    "citation_failure": {
        "min_verified": CITATION_FAILURE_MIN_VERIFIED,
        "invalid_share_threshold": CITATION_FAILURE_INVALID_SHARE_THRESHOLD,
        "min_invalid_count": CITATION_FAILURE_MIN_INVALID_COUNT,
        "min_abs_rise": CITATION_FAILURE_MIN_ABS_RISE,
        "min_rel_rise": CITATION_FAILURE_MIN_REL_RISE,
        "alpha": CITATION_FAILURE_ALPHA,
        "critical_share": CITATION_FAILURE_CRITICAL_SHARE,
    },
    "competitor_surge": {
        "min_observations": COMPETITOR_SURGE_MIN_OBSERVATIONS,
        "min_abs_rise": COMPETITOR_SURGE_MIN_ABS_RISE,
        "min_rel_rise": COMPETITOR_SURGE_MIN_REL_RISE,
        "alpha": COMPETITOR_SURGE_ALPHA,
        "critical_abs_rise": COMPETITOR_SURGE_CRITICAL_ABS_RISE,
    },
    "platform_conflict": {"min_each": PLATFORM_CONFLICT_MIN_EACH},
    "leads": {"max_domains": LEADS_MAX_DOMAINS},
}

# 取值约束：count 为正整数；share 在 [0, 1]；ratio 为非负数（相对幅度可以超过 100%）；
# alpha 在 (0, 1]；positive 为正数。
_COUNT, _SHARE, _RATIO, _ALPHA, _POSITIVE = "count", "share", "ratio", "alpha", "positive"
_KINDS = {
    "min_observations": _COUNT,
    "min_rankable": _COUNT,
    "min_ranked": _COUNT,
    "min_verified": _COUNT,
    "min_current_count": _COUNT,
    "min_platform_count": _COUNT,
    "min_platform_increase": _COUNT,
    "critical_platform_increase": _COUNT,
    "min_invalid_count": _COUNT,
    "min_each": _COUNT,
    "max_domains": _COUNT,
    "min_abs_drop": _SHARE,
    "min_top3_abs_drop": _SHARE,
    "critical_abs_drop": _SHARE,
    "min_abs_rise": _SHARE,
    "critical_abs_rise": _SHARE,
    "invalid_share_threshold": _SHARE,
    "critical_share": _SHARE,
    "min_rel_drop": _RATIO,
    "min_top3_rel_drop": _RATIO,
    "min_rel_rise": _RATIO,
    "alpha": _ALPHA,
    "min_mean_rank_increase": _POSITIVE,
    "critical_mean_rank_increase": _POSITIVE,
}
_KIND_LABELS = {
    _COUNT: "正整数",
    _SHARE: "0 到 1 之间的数",
    _RATIO: "非负数",
    _ALPHA: "大于 0、不超过 1 的数",
    _POSITIVE: "正数",
}


def _check_value(rule: str, key: str, value: Any, where: str) -> Number:
    """校验并规范化一个阈值；整数类阈值写成 4.0 也接受并转为 4。"""
    name = f"{rule}.{key}"
    if rule not in DEFAULT_THRESHOLDS:
        raise InputError(f"{where}：未知的告警规则 {rule}；可选：{', '.join(DEFAULT_THRESHOLDS)}")
    if key not in DEFAULT_THRESHOLDS[rule]:
        known = ", ".join(DEFAULT_THRESHOLDS[rule])
        raise InputError(f"{where}：规则 {rule} 没有阈值 {key}；可选：{known}")
    kind = _KINDS[key]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise InputError(f"{where}：阈值 {name} 必须是{_KIND_LABELS[kind]}，实际为 {value!r}")
    if kind == _COUNT:
        if value != int(value) or value < 1:
            raise InputError(f"{where}：阈值 {name} 必须是{_KIND_LABELS[kind]}，实际为 {value!r}")
        return int(value)
    valid = {
        _SHARE: 0 <= value <= 1,
        _RATIO: value >= 0,
        _ALPHA: 0 < value <= 1,
        _POSITIVE: value > 0,
    }[kind]
    if not valid:
        raise InputError(f"{where}：阈值 {name} 必须是{_KIND_LABELS[kind]}，实际为 {value!r}")
    return float(value)


def parse_override(item: str) -> tuple[str, str, Number]:
    """解析命令行的 `规则.键=值`。"""
    where = f"--threshold {item}"
    name, sep, text = item.partition("=")
    rule, dot, key = name.strip().partition(".")
    if not sep or not dot or not text.strip():
        raise InputError(f"{where}：格式应为 规则.键=值，如 mention_drop.alpha=0.1")
    try:
        value = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise InputError(f"{where}：值必须是数字") from exc
    return rule, key, _check_value(rule, key, value, where)


def _project_overrides(project: dict[str, Any]) -> list[tuple[str, str, Number]]:
    ext = project.get("ext")
    if not isinstance(ext, dict) or "alerts" not in ext:
        return []
    where = "project.json 的 ext.alerts.thresholds"
    section = ext["alerts"]
    if not isinstance(section, dict):
        raise InputError("project.json 的 ext.alerts 必须是对象")
    configured = section.get("thresholds", {})
    if not isinstance(configured, dict):
        raise InputError(f"{where} 必须是对象（规则 → 阈值）")
    items: list[tuple[str, str, Number]] = []
    for rule, values in sorted(configured.items()):
        if not isinstance(values, dict):
            raise InputError(f"{where} 中 {rule} 的值必须是对象（阈值名 → 数值）")
        for key, value in sorted(values.items()):
            items.append((rule, key, _check_value(rule, key, value, where)))
    return items


def resolve_thresholds(
    project: dict[str, Any], cli_items: list[str]
) -> tuple[dict[str, dict[str, Number]], list[dict[str, Any]]]:
    """返回 (实际使用的阈值, 非默认值清单 [{key, value, source}])。"""
    resolved = copy.deepcopy(DEFAULT_THRESHOLDS)
    sources: dict[tuple[str, str], str] = {}
    layers = [
        ("project", _project_overrides(project)),
        ("cli", [parse_override(item) for item in cli_items]),
    ]
    for source, items in layers:
        for rule, key, value in items:
            resolved[rule][key] = value
            sources[(rule, key)] = source
    overrides = [
        {"key": f"{rule}.{key}", "value": resolved[rule][key], "source": source}
        for (rule, key), source in sorted(sources.items())
    ]
    return resolved, overrides
