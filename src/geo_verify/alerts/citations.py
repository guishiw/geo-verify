"""引用域名变化与“待核查的相关线索”。

- 计数口径与指标模块相同：只计有效观测中域名可识别的引用，同一观测内 URL 相同的引用只算一次；
- 变化分三类：新增（基线 0 次）、消失（当前 0 次）、次数变化（两边都有但次数不同）；
- 线索只说明“同期出现”，措辞限于“相关”“待核查”，不表达因果（见 wording.py）。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from geo_verify.alerts.rules import RULES, Context
from geo_verify.alerts.wording import ensure_clean
from geo_verify.metrics.index import BoolArray

NOTE_WITH_DATA = (
    "引用按观测内 URL 去重后计数（只计有效观测中域名可识别的引用）；"
    "域名变化仅作为同期出现的相关线索，待核查。"
)
NOTE_WITHOUT_DATA = "两个比较对象都没有可识别的引用，没有引用域名变化可列。"


def domain_counts(ctx: Context, observations: BoolArray) -> dict[str, int]:
    citations = ctx.index.citations
    rows = observations[citations.table.obs]
    counts = np.bincount(citations.domain[rows], minlength=len(citations.domains))
    return {
        citations.domains[code]: int(count) for code, count in enumerate(counts.tolist()) if count
    }


def diff_counts(
    baseline: dict[str, int], current: dict[str, int]
) -> dict[str, list[dict[str, Any]]]:
    domains = sorted(baseline.keys() | current.keys())
    return {
        "added": [
            {"domain": domain, "current": current[domain]}
            for domain in domains
            if domain not in baseline
        ],
        "removed": [
            {"domain": domain, "baseline": baseline[domain]}
            for domain in domains
            if domain not in current
        ],
        "changed": [
            {
                "domain": domain,
                "baseline": baseline[domain],
                "current": current[domain],
                "delta": current[domain] - baseline[domain],
            }
            for domain in domains
            if domain in baseline and domain in current and baseline[domain] != current[domain]
        ],
    }


def _has_changes(changes: dict[str, list[dict[str, Any]]]) -> bool:
    return any(changes[kind] for kind in ("added", "removed", "changed"))


def _scope_changes(ctx: Context, platform: str | None) -> tuple[dict[str, Any], int, int]:
    scope = ctx.scope(platform)
    before = domain_counts(ctx, ctx.comparison.baseline.mask & scope)
    after = domain_counts(ctx, ctx.comparison.current.mask & scope)
    return diff_counts(before, after), sum(before.values()), sum(after.values())


def citation_changes(ctx: Context) -> tuple[dict[str, Any], dict[str | None, dict[str, Any]]]:
    """返回 (输出中的 citation_changes, 各范围的变化表：None 为整体，其余为平台)。"""
    overall, before, after = _scope_changes(ctx, ctx.comparison.platform)
    by_scope: dict[str | None, dict[str, Any]] = {None: overall}
    by_platform = []
    for platform in ctx.platforms:
        changes = _scope_changes(ctx, platform)[0]
        by_scope[platform] = changes
        if _has_changes(changes):
            by_platform.append({"platform": platform, **changes})
    document = {
        **overall,
        "baseline_citations": before,
        "current_citations": after,
        "by_platform": by_platform,
        "note": ensure_clean(NOTE_WITH_DATA if before or after else NOTE_WITHOUT_DATA),
    }
    return document, by_scope


def _ranked(changes: dict[str, Any]) -> list[tuple[str, str]]:
    """(域名, 变化描述)，按变化次数的绝对值从大到小、再按域名排序。"""
    items = [
        (item["current"], item["domain"], f"新增，0→{item['current']}") for item in changes["added"]
    ]
    items += [
        (item["baseline"], item["domain"], f"消失，{item['baseline']}→0")
        for item in changes["removed"]
    ]
    items += [
        (abs(item["delta"]), item["domain"], f"{item['baseline']}→{item['current']}")
        for item in changes["changed"]
    ]
    return [(domain, text) for _, domain, text in sorted(items, key=lambda x: (-x[0], x[1]))]


def build_leads(ctx: Context, by_scope: dict[str | None, dict[str, Any]]) -> list[dict[str, Any]]:
    """每条告警附一条线索：列出同一范围（整体或该平台）内同期变化的引用域名。"""
    limit = int(ctx.thresholds["leads"]["max_domains"])
    first, second = ctx.sides()
    leads = []
    for alert in ctx.alerts:
        platform = alert["platform"]
        ranked = _ranked(by_scope[platform])
        if not ranked:
            continue
        shown = ranked[:limit]
        scope = "全部平台合并" if platform is None else f"平台 {platform}"
        listed = "；".join(f"{domain}（{text}）" for domain, text in shown)
        text = (
            f"与“{RULES[alert['rule_id']]}”告警同期出现的引用域名变化"
            f"（{scope}，{first} → {second}，按引用次数）：{listed}。"
        )
        if len(ranked) > limit:
            text += f"另有 {len(ranked) - limit} 个域名有变化，未逐一列出，见 citation_changes。"
        text += "以上仅为相关线索，待核查。"
        leads.append(
            {
                "alert_id": alert["alert_id"],
                "alert_rule_id": alert["rule_id"],
                "platform": platform,
                "text": ensure_clean(text),
                "domains": [domain for domain, _ in shown],
            }
        )
    return leads
