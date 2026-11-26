"""五类告警规则。全部从指标索引取数：有效观测、生效标注、引用核验和判定与 metrics 同一口径。

- mention_drop：目标主体提及率骤降（整体及分平台），以及掉位（前三推荐率下降、平均位次后移）；
- negative_spread：含无依据或错误负面的观测占比上升，或出现这类观测的平台数增加；
- citation_failure：已核验引用中无效的占比达到阈值，或较基线明显上升；
- competitor_surge：竞品提及率或推荐率明显上升；
- platform_conflict：同一平台、当前比较对象内，对同一核心事实既有正确呈现又有错误呈现，且两者都达到最小数量。

告警文字只描述数据的变化，结尾统一写“待核查”，不作因果表述（见 wording.py）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from geo_verify.alerts.change import (
    INSUFFICIENT,
    Change,
    Count,
    at_least,
    mean_shift,
    proportion_change,
)
from geo_verify.alerts.periods import Comparison, Period
from geo_verify.alerts.thresholds import Number
from geo_verify.alerts.wording import ensure_clean
from geo_verify.metrics.index import (
    CHECK_INVALID,
    CHECK_NOT_RELEVANT,
    CHECK_UNVERIFIED,
    LABELS,
    BoolArray,
    ProjectIndex,
)

RULES = {
    "mention_drop": "提及率骤降与位次下滑",
    "negative_spread": "错误负面扩散",
    "citation_failure": "引用大面积失效",
    "competitor_surge": "竞品异常提升",
    "platform_conflict": "同平台口径冲突",
}
TOP_N = 3
P_TEXT_FLOOR = 0.0001  # 文字里小于此值的 p 写成“p < 0.0001”
PRESENT_CORRECT = LABELS["present_correct"]
PRESENT_WRONG = LABELS["present_wrong"]
FACTS_TO_ANSWER = 1


def _count(mask: BoolArray) -> int:
    return int(np.count_nonzero(mask))


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _ratio_text(count: Count) -> str:
    return f"{_pct(count.numerator / count.denominator)}（{count.numerator}/{count.denominator}）"


def _scope_text(platform: str | None) -> str:
    return "全部平台合并" if platform is None else f"平台 {platform}"


def _relative_text(change: Change) -> str:
    if change.relative_change is None:
        return "基线为 0"
    return f"相对变化 {change.relative_change * 100:+.1f}%"


def _p_text(p_value: float | None) -> str:
    if p_value is not None and p_value < P_TEXT_FLOOR:
        return f"p < {P_TEXT_FLOOR}"
    return f"p = {p_value:.4f}"


@dataclass
class RuleSummary:
    rule_id: str
    evaluated: int = 0
    triggered: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self, change: Change, *, platform: str | None, entity_id: str | None, metric: str
    ) -> None:
        if change.status == INSUFFICIENT:
            self.skip(platform=platform, entity_id=entity_id, metric=metric, reason=change.reason)
            return
        self.evaluated += 1
        self.triggered += change.triggered

    def skip(
        self, *, platform: str | None, entity_id: str | None, metric: str, reason: str | None
    ) -> None:
        self.skipped.append(
            {
                "platform": platform,
                "entity_id": entity_id,
                "metric": metric,
                "reason": ensure_clean(str(reason)),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": RULES[self.rule_id],
            "evaluated": self.evaluated,
            "triggered": self.triggered,
            "skipped": self.skipped,
        }


@dataclass
class Context:
    index: ProjectIndex
    comparison: Comparison
    thresholds: dict[str, dict[str, Number]]
    platforms: list[str]
    scopes: list[str | None]
    alerts: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        index: ProjectIndex,
        comparison: Comparison,
        thresholds: dict[str, dict[str, Number]],
    ) -> Context:
        either = comparison.baseline.mask | comparison.current.mask
        platforms = [index.platforms[int(code)] for code in np.unique(index.platform[either])]
        scopes: list[str | None]
        if comparison.platform is not None or len(platforms) < 2:
            scopes = list(platforms)
        else:
            scopes = [None, *platforms]
        return cls(index, comparison, thresholds, platforms, scopes)

    def scope(self, platform: str | None) -> BoolArray:
        if platform is None:
            return np.ones(self.index.size, dtype=np.bool_)
        return self.index.scope_mask(platform=platform)

    def sides(self) -> tuple[str, str]:
        return (
            self.comparison.baseline.describe("基线"),
            self.comparison.current.describe("当前"),
        )

    def ids(self, mask: BoolArray) -> list[str]:
        result: list[str] = self.index.observations.ids[mask].tolist()
        return result

    def emit(
        self,
        rule_id: str,
        *,
        platform: str | None,
        entity_id: str | None,
        key: str,
        severity: str,
        message: str,
        evidence: dict[str, Any],
        observation_ids: list[str],
    ) -> None:
        self.alerts.append(
            {
                "alert_id": f"{rule_id}:{platform or 'all'}:{entity_id or '-'}:{key}",
                "rule_id": rule_id,
                "severity": severity,
                "platform": platform,
                "entity_id": entity_id,
                "message": ensure_clean(message),
                "evidence": evidence,
                "observation_ids": observation_ids,
            }
        )


def _evidence(metric: str, baseline: Count, current: Count, change: Change) -> dict[str, Any]:
    return {
        "metric": metric,
        "baseline": baseline.to_dict(),
        "current": current.to_dict(),
        "difference": change.difference,
        "relative_change": change.relative_change,
        "p_value": change.p_value,
        "test": "two_proportion_z_one_sided",
    }


def _proportion_message(
    ctx: Context,
    platform: str | None,
    subject: str,
    baseline: Count,
    current: Count,
    change: Change,
) -> str:
    before, after = ctx.sides()
    verb = "升至" if (change.difference or 0) > 0 else "降至"
    moved = abs(change.difference or 0) * 100
    return (
        f"{_scope_text(platform)}：{subject}从{before} 的 {_ratio_text(baseline)}"
        f"{verb}{after} 的 {_ratio_text(current)}，变化 {moved:.1f} 个百分点"
        f"（{_relative_text(change)}，单侧检验 {_p_text(change.p_value)}）。待核查。"
    )


# ---------------------------------------------------------------- mention_drop


def mention_drop(ctx: Context, summary: RuleSummary) -> None:
    t = ctx.thresholds["mention_drop"]
    index = ctx.index
    target = index.target_id
    columns = index.entity(target)
    baseline, current = ctx.comparison.baseline, ctx.comparison.current
    top = (columns.rank > 0) & (columns.rank <= TOP_N)
    for platform in ctx.scopes:
        scope = ctx.scope(platform) & columns.present
        b_base, c_base = baseline.mask & scope, current.mask & scope
        checks: list[tuple[str, str, BoolArray, BoolArray, BoolArray, dict[str, Number]]] = [
            (
                "mention_rate",
                f"目标主体 {target} 的提及率",
                b_base,
                c_base,
                columns.mentioned,
                {
                    "min_n": t["min_observations"],
                    "min_abs": t["min_abs_drop"],
                    "min_rel": t["min_rel_drop"],
                },
            ),
            (
                "top3_rate",
                f"目标主体 {target} 的前三推荐率（可排序回答）",
                b_base & columns.rankable,
                c_base & columns.rankable,
                top,
                {
                    "min_n": t["min_rankable"],
                    "min_abs": t["min_top3_abs_drop"],
                    "min_rel": t["min_top3_rel_drop"],
                },
            ),
        ]
        for metric, subject, b_den, c_den, hit, limits in checks:
            b = Count(_count(b_den & hit), _count(b_den))
            c = Count(_count(c_den & hit), _count(c_den))
            change = proportion_change(
                b,
                c,
                increase=False,
                min_n=int(limits["min_n"]),
                min_abs=limits["min_abs"],
                min_rel=limits["min_rel"],
                alpha=t["alpha"],
                critical=t["critical_abs_drop"],
            )
            summary.record(change, platform=platform, entity_id=target, metric=metric)
            if change.triggered:
                ctx.emit(
                    "mention_drop",
                    platform=platform,
                    entity_id=target,
                    key=metric,
                    severity=str(change.severity),
                    message=_proportion_message(ctx, platform, subject, b, c, change),
                    evidence=_evidence(metric, b, c, change),
                    observation_ids=ctx.ids(c_den & ~hit),
                )
        _mean_rank(ctx, summary, platform, b_base, c_base)


def _mean_rank(
    ctx: Context, summary: RuleSummary, platform: str | None, b_base: BoolArray, c_base: BoolArray
) -> None:
    t = ctx.thresholds["mention_drop"]
    target = ctx.index.target_id
    columns = ctx.index.entity(target)
    ranked = columns.rankable & (columns.rank > 0)
    before: list[int] = columns.rank[b_base & ranked].tolist()
    after: list[int] = columns.rank[c_base & ranked].tolist()
    change = mean_shift(
        before,
        after,
        min_n=int(t["min_ranked"]),
        min_increase=t["min_mean_rank_increase"],
        alpha=t["alpha"],
        critical=t["critical_mean_rank_increase"],
    )
    summary.record(change, platform=platform, entity_id=target, metric="mean_rank")
    if not change.triggered:
        return
    mean_b, mean_c = sum(before) / len(before), sum(after) / len(after)
    first, second = ctx.sides()
    message = (
        f"{_scope_text(platform)}：目标主体 {target} 在可排序回答中的平均位次从{first} 的 "
        f"{mean_b:.2f}（{len(before)} 次）后移到{second} 的 {mean_c:.2f}（{len(after)} 次），"
        f"后移 {change.difference:.2f} 位（单侧检验 {_p_text(change.p_value)}）。待核查。"
    )
    ctx.emit(
        "mention_drop",
        platform=platform,
        entity_id=target,
        key="mean_rank",
        severity=str(change.severity),
        message=message,
        evidence={
            "metric": "mean_rank",
            "baseline": {"mean": mean_b, "count": len(before)},
            "current": {"mean": mean_c, "count": len(after)},
            "difference": change.difference,
            "p_value": change.p_value,
            "test": "welch_z_one_sided",
        },
        observation_ids=ctx.ids(c_base & ranked),
    )


# ---------------------------------------------------------------- negative_spread


def negative_spread(ctx: Context, summary: RuleSummary) -> None:
    t = ctx.thresholds["negative_spread"]
    target = ctx.index.target_id
    columns = ctx.index.entity(target)
    baseline, current = ctx.comparison.baseline, ctx.comparison.current
    flagged = columns.negative_miscitation
    metric = "negative_miscitation_rate"
    for platform in ctx.scopes:
        scope = ctx.scope(platform) & columns.present
        b_den, c_den = baseline.mask & scope, current.mask & scope
        b = Count(_count(b_den & flagged), _count(b_den))
        c = Count(_count(c_den & flagged), _count(c_den))
        change = proportion_change(
            b,
            c,
            increase=True,
            min_n=int(t["min_observations"]),
            min_abs=t["min_abs_rise"],
            min_rel=t["min_rel_rise"],
            alpha=t["alpha"],
            critical=t["critical_abs_rise"],
            min_count=int(t["min_current_count"]),
        )
        summary.record(change, platform=platform, entity_id=target, metric=metric)
        if change.triggered:
            subject = "含无依据或错误负面的观测占比"
            ctx.emit(
                "negative_spread",
                platform=platform,
                entity_id=target,
                key=metric,
                severity=str(change.severity),
                message=_proportion_message(ctx, platform, subject, b, c, change),
                evidence=_evidence(metric, b, c, change),
                observation_ids=ctx.ids(c_den & flagged),
            )
    _platform_spread(ctx, summary)


def _platform_spread(ctx: Context, summary: RuleSummary) -> None:
    t = ctx.thresholds["negative_spread"]
    target = ctx.index.target_id
    if ctx.comparison.platform is not None or len(ctx.platforms) < 2:
        summary.skip(
            platform=ctx.comparison.platform,
            entity_id=target,
            metric="platform_count",
            reason="比较范围内只有一个平台，不评估出现平台数的变化",
        )
        return
    columns = ctx.index.entity(target)
    flagged = columns.present & columns.negative_miscitation
    minimum = int(t["min_platform_count"])

    def platforms_with_cases(period: Period) -> list[str]:
        return [
            platform
            for platform in ctx.platforms
            if _count(period.mask & flagged & ctx.scope(platform)) >= minimum
        ]

    before = platforms_with_cases(ctx.comparison.baseline)
    after = platforms_with_cases(ctx.comparison.current)
    increase = len(after) - len(before)
    summary.evaluated += 1
    if increase < int(t["min_platform_increase"]):
        return
    summary.triggered += 1
    new = sorted(set(after) - set(before))
    first, second = ctx.sides()
    message = (
        f"全部平台合并：出现无依据或错误负面（每个平台至少 {minimum} 条）的平台数从{first} 的 "
        f"{len(before)} 个增至{second} 的 {len(after)} 个，新出现的平台：{'、'.join(new)}。待核查。"
    )
    on_new = np.zeros(ctx.index.size, dtype=np.bool_)
    for platform in new:
        on_new |= ctx.scope(platform)
    critical = increase >= int(t["critical_platform_increase"])
    ctx.emit(
        "negative_spread",
        platform=None,
        entity_id=target,
        key="platform_count",
        severity="critical" if critical else "warning",
        message=message,
        evidence={
            "metric": "platform_count",
            "baseline": {"count": len(before), "platforms": before},
            "current": {"count": len(after), "platforms": after},
            "new_platforms": new,
            "min_platform_count": minimum,
        },
        observation_ids=ctx.ids(ctx.comparison.current.mask & flagged & on_new),
    )


# ---------------------------------------------------------------- citation_failure


def citation_failure(ctx: Context, summary: RuleSummary) -> None:
    t = ctx.thresholds["citation_failure"]
    citations = ctx.index.citations
    owner = citations.table.obs
    verified = citations.check != CHECK_UNVERIFIED
    invalid = citations.check == CHECK_INVALID
    metric = "invalid_citation_share"
    minimum = int(t["min_verified"])
    for platform in ctx.scopes:
        scope = ctx.scope(platform)
        b_rows = (ctx.comparison.baseline.mask & scope)[owner]
        c_rows = (ctx.comparison.current.mask & scope)[owner]
        b = Count(_count(b_rows & invalid), _count(b_rows & verified))
        c = Count(_count(c_rows & invalid), _count(c_rows & verified))
        if c.denominator < minimum:
            summary.skip(
                platform=platform,
                entity_id=None,
                metric=metric,
                reason=f"样本不足：当前已核验引用 {c.denominator} 条，至少需要 {minimum} 条",
            )
            continue
        summary.evaluated += 1
        rise = proportion_change(
            b,
            c,
            increase=True,
            min_n=minimum,
            min_abs=t["min_abs_rise"],
            min_rel=t["min_rel_rise"],
            alpha=t["alpha"],
            critical=t["critical_share"],
            min_count=int(t["min_invalid_count"]),
        )
        triggers = []
        if c.numerator >= int(t["min_invalid_count"]) and at_least(
            c.ratio, t["invalid_share_threshold"]
        ):
            triggers.append("share_above_threshold")
        if rise.triggered:
            triggers.append("rise_from_baseline")
        if not triggers:
            continue
        summary.triggered += 1
        first, second = ctx.sides()
        parts = [f"{_scope_text(platform)}：{second} 已核验引用中无效的占比为 {_ratio_text(c)}"]
        if "share_above_threshold" in triggers:
            parts.append(f"，达到阈值 {_pct(float(t['invalid_share_threshold']))}")
        if rise.triggered:
            moved = (rise.difference or 0) * 100
            parts.append(
                f"，较{first} 的 {_ratio_text(b)}上升 {moved:.1f} 个百分点"
                f"（单侧检验 {_p_text(rise.p_value)}）"
            )
        parts.append("。待核查。")
        critical = at_least(c.ratio, t["critical_share"])
        ctx.emit(
            "citation_failure",
            platform=platform,
            entity_id=None,
            key=metric,
            severity="critical" if critical else "warning",
            message="".join(parts),
            evidence={
                **_evidence(metric, b, c, rise),
                "triggers": triggers,
                "current_not_relevant": _count(c_rows & (citations.check == CHECK_NOT_RELEVANT)),
                "current_unverified": _count(c_rows & ~verified),
            },
            observation_ids=ctx.ids(_owners(ctx.index, owner[c_rows & invalid])),
        )


def _owners(index: ProjectIndex, positions: Any) -> BoolArray:
    mask = np.zeros(index.size, dtype=np.bool_)
    mask[positions] = True
    return mask


# ---------------------------------------------------------------- competitor_surge


def competitor_surge(ctx: Context, summary: RuleSummary) -> None:
    t = ctx.thresholds["competitor_surge"]
    baseline, current = ctx.comparison.baseline, ctx.comparison.current
    for entity_id in sorted(set(ctx.index.project["competitor_entity_ids"])):
        columns = ctx.index.entity(entity_id)
        measures: list[tuple[str, str, BoolArray]] = [
            ("mention_rate", f"竞品 {entity_id} 的提及率", columns.mentioned),
            (
                "recommended_rate",
                f"竞品 {entity_id} 的推荐率（被推荐的观测 / 有该竞品标注的有效观测）",
                columns.recommended,
            ),
        ]
        for platform in ctx.scopes:
            scope = ctx.scope(platform) & columns.present
            b_den, c_den = baseline.mask & scope, current.mask & scope
            for metric, subject, hit in measures:
                b = Count(_count(b_den & hit), _count(b_den))
                c = Count(_count(c_den & hit), _count(c_den))
                change = proportion_change(
                    b,
                    c,
                    increase=True,
                    min_n=int(t["min_observations"]),
                    min_abs=t["min_abs_rise"],
                    min_rel=t["min_rel_rise"],
                    alpha=t["alpha"],
                    critical=t["critical_abs_rise"],
                )
                summary.record(change, platform=platform, entity_id=entity_id, metric=metric)
                if change.triggered:
                    ctx.emit(
                        "competitor_surge",
                        platform=platform,
                        entity_id=entity_id,
                        key=metric,
                        severity=str(change.severity),
                        message=_proportion_message(ctx, platform, subject, b, c, change),
                        evidence=_evidence(metric, b, c, change),
                        observation_ids=ctx.ids(c_den & hit),
                    )


# ---------------------------------------------------------------- platform_conflict


def platform_conflict(ctx: Context, summary: RuleSummary) -> None:
    t = ctx.thresholds["platform_conflict"]
    minimum = int(t["min_each"])
    index = ctx.index
    judgments = index.judgments
    owner = judgments.table.obs
    backward = (judgments.direction == FACTS_TO_ANSWER) & np.isin(
        judgments.label, (PRESENT_CORRECT, PRESENT_WRONG)
    )

    def presented(period: Period, platform: str, fact: int) -> tuple[BoolArray, BoolArray]:
        """(正确呈现该事实的观测, 错误呈现该事实的观测)，按观测去重。"""
        rows = (period.mask & ctx.scope(platform))[owner] & backward & (judgments.fact == fact)
        return (
            _owners(index, owner[rows & (judgments.label == PRESENT_CORRECT)]),
            _owners(index, owner[rows & (judgments.label == PRESENT_WRONG)]),
        )

    def tally(masks: tuple[BoolArray, BoolArray]) -> dict[str, int]:
        return {"present_correct": _count(masks[0]), "present_wrong": _count(masks[1])}

    for platform in ctx.platforms:
        current_rows = (ctx.comparison.current.mask & ctx.scope(platform))[owner] & backward
        for fact in np.unique(judgments.fact[current_rows]).tolist():
            fact_id = judgments.facts[int(fact)]
            if not fact_id:
                continue
            summary.evaluated += 1
            now = presented(ctx.comparison.current, platform, int(fact))
            correct, wrong = _count(now[0]), _count(now[1])
            if min(correct, wrong) < minimum:
                continue
            summary.triggered += 1
            before = presented(ctx.comparison.baseline, platform, int(fact))
            _, second = ctx.sides()
            message = (
                f"平台 {platform} 在{second} 内对核心事实 {fact_id} 的呈现相互矛盾："
                f"正确呈现 {correct} 条观测、错误呈现 {wrong} 条观测"
                f"（两者都达到 {minimum} 条）。待核查。"
            )
            ctx.emit(
                "platform_conflict",
                platform=platform,
                entity_id=None,
                key=fact_id,
                severity="critical" if wrong >= correct else "warning",
                message=message,
                evidence={
                    "metric": "core_fact_conflict",
                    "fact_id": fact_id,
                    "current": tally(now),
                    "baseline": tally(before),
                    "min_each": minimum,
                },
                observation_ids=ctx.ids(now[0] | now[1]),
            )


RULE_FUNCTIONS: dict[str, Callable[[Context, RuleSummary], None]] = {
    "mention_drop": mention_drop,
    "negative_spread": negative_spread,
    "citation_failure": citation_failure,
    "competitor_surge": competitor_surge,
    "platform_conflict": platform_conflict,
}


def run_rules(ctx: Context) -> list[dict[str, Any]]:
    """依次执行五类规则，告警写入 ctx.alerts，返回各规则的执行摘要。"""
    summaries = []
    for rule_id, function in RULE_FUNCTIONS.items():
        summary = RuleSummary(rule_id)
        function(ctx, summary)
        summaries.append(summary.to_dict())
    return summaries
