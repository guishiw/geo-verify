"""组装 out/alerts.json（结构见 docs/outputs.md）。

文件不含生成时间和路径：同一份输入在任何目录、任何时间计算，结果逐字节相同。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from geo_verify.alerts.citations import build_leads, citation_changes
from geo_verify.alerts.periods import MODE_DATE, MODE_WINDOW, select_comparison
from geo_verify.alerts.rules import Context, run_rules
from geo_verify.alerts.thresholds import resolve_thresholds
from geo_verify.alerts.wording import ensure_clean
from geo_verify.metrics.index import ProjectIndex

SCHEMA_VERSION = "1.0"
STATUS_LABELS = {"alert": "有告警", "clear": "无告警"}
NOTICE = (
    "告警是对两个比较对象（窗口或自然日）观测数据的预检线索，需人工核查；"
    "引用域名变化仅列为同期出现的相关线索，待核查。"
)


def compute_alerts(
    index: ProjectIndex,
    *,
    baseline_window: str | None = None,
    current_window: str | None = None,
    baseline_date: str | None = None,
    current_date: str | None = None,
    platform: str | None = None,
    threshold_items: Sequence[str] = (),
) -> dict[str, Any]:
    """比较两个窗口（或两个自然日），返回告警文档。

    无法比较时抛出 InsufficientData；参数非法时抛出 InputError。
    """
    thresholds, overrides = resolve_thresholds(index.project, list(threshold_items))
    comparison = select_comparison(
        index,
        baseline_window=baseline_window,
        current_window=current_window,
        baseline_date=baseline_date,
        current_date=current_date,
        platform=platform,
    )
    ctx = Context.create(index, comparison, thresholds)
    summaries = run_rules(ctx)
    changes, by_scope = citation_changes(ctx)
    leads = build_leads(ctx, by_scope)
    status = "alert" if ctx.alerts else "clear"
    baseline, current = comparison.baseline, comparison.current
    window_mode = comparison.mode == MODE_WINDOW
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "alerts",
        "project_id": index.project["project_id"],
        "target_entity_id": index.target_id,
        "prompt_set": {
            "version": index.prompt_set["version"],
            "content_hash": index.prompt_set["content_hash"],
        },
        "mode": comparison.mode,
        "baseline_window": baseline.label if window_mode else None,
        "current_window": current.label if window_mode else None,
        "baseline_date": baseline.label if comparison.mode == MODE_DATE else None,
        "current_date": current.label if comparison.mode == MODE_DATE else None,
        "baseline": baseline.to_dict(),
        "current": current.to_dict(),
        "available_windows": comparison.available_windows,
        "platform": comparison.platform,
        "platforms": ctx.platforms,
        "thresholds": thresholds,
        "threshold_overrides": overrides,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "alerts": ctx.alerts,
        "rule_summary": summaries,
        "citation_changes": changes,
        "leads": leads,
        "notice": ensure_clean(NOTICE),
    }
    if index.project.get("synthetic") is True:
        document["synthetic"] = True
    return document
