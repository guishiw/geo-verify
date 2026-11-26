"""预检报告测试的辅助函数：按 docs/outputs.md 的结构手工构造各结果文件。

report 只读取结果文件，不重新计算；这里不调用任何指标、交叉验证、告警或复核的实现，
每个结果文件都直接按约定的结构写出，便于构造“通过 / 有条件通过 / 不通过 / 数据不足”的各种情形。
全部数据为虚构（synthetic: true）。
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import factory
from conftest import run_cli, run_cli_json

from geo_verify import audit
from geo_verify.jsonl import write_json

NOW = "2026-10-20T08:00:00Z"
SYNTHETIC_WATERMARK = "合成数据，仅供演示，不得作为真实效果宣传"
PRECHECK_NOTICE = "本报告为工具预检结论，不构成独立第三方验证"
FORBIDDEN = ("保证首推", "必然收录", "确保推荐", "官方", "认证", "第三方验证通过", "用户真实所见")

FILES = {
    "metrics": "out/metrics.json",
    "crosscheck": "out/crosscheck.json",
    "compare": "out/compare.json",
    "alerts": "out/alerts.json",
    "evidence": "out/evidence-verify.json",
    "disputes": "out/disputes.json",
    "plan": "recheck/plan.json",
    "result": "recheck/result.json",
}


def interval(low: float, high: float, method: str = "cluster_bootstrap") -> dict[str, Any]:
    return {"method": method, "level": 0.95, "low": low, "high": high}


def metric(
    metric_id: str,
    value: float | None,
    numerator: int | None = None,
    denominator: int | None = None,
    *,
    platform: str | None = None,
    channel: str | None = None,
    window_id: str | None = None,
    date: str | None = None,
    role: str = "acceptance",
    ci: dict[str, Any] | None = None,
    sufficient: bool = True,
    windows: int = 3,
    valid_observations: int = 30,
    reasons: list[str] | None = None,
    null_reason: str | None = None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一个 metric_result 契约对象。"""
    if value is not None and numerator is None:
        denominator = 30
        numerator = round(value * denominator)
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "metric_id": metric_id,
        "project_id": "auto-demo",
        "scope": {
            "platform": platform,
            "channel": channel,
            "window_id": window_id,
            "date": date,
            "entity_id": factory.TARGET,
        },
        "value": value,
        "null_reason": null_reason if value is None else None,
        "numerator": numerator if numerator is not None else 0,
        "denominator": denominator if denominator is not None else 0,
        "excluded": {"count": 1, "rules": [{"rule": "audit:EX-DUP", "count": 1}]},
        "interval": ci if value is not None else None,
        "sufficiency": {
            "sufficient": sufficient,
            "windows": windows,
            "valid_observations": valid_observations,
            "reasons": reasons
            if reasons is not None
            else ([] if sufficient else ["时间窗口不足 3 个"]),
        },
        "definition_version": "1.0",
        "preregistered": role != "post_hoc",
        "role": role,
        "drilldown": {
            "unit": "observation",
            "numerator_ids_hash": None,
            "denominator_ids_hash": None,
        },
        "ext": {
            "coverage": coverage
            or {
                "observations": denominator or 0,
                "queries": 10,
                "date_from": "2026-10-08",
                "date_to": "2026-10-10",
                "windows": ["W1", "W2", "W3"],
                "platforms": ["engine-a"],
                "channels": {"api": denominator or 0},
            }
        },
        "synthetic": True,
    }
    if value is None and null_reason is None:
        result["null_reason"] = "分母为 0"
    return result


def exclusion_audit(
    *,
    rules: list[dict[str, Any]] | None = None,
    selective: bool = False,
    comparisons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rules = (
        rules
        if rules is not None
        else [
            {
                "rule_id": "EX-DUP",
                "registered": True,
                "listed_observations": 1,
                "excluded_observations": 1,
            }
        ]
    )
    return {
        "registered_rules": ["EX-DUP"],
        "rules": rules,
        "unregistered_rules": [item["rule_id"] for item in rules if not item["registered"]],
        "excluded_observations": sum(item["excluded_observations"] for item in rules),
        "thresholds": {"alpha": 0.05, "min_excluded": 5, "min_difference": 0.1},
        "comparisons": comparisons if comparisons is not None else [],
        "selective": selective,
    }


def selective_comparison() -> dict[str, Any]:
    """S09：被剔除的 8 条全部未提及，保留的 30 条中 24 条提及。"""
    return {
        "metric_id": "mention_rate",
        "direction": "higher_is_better",
        "retained": {"numerator": 24, "denominator": 30, "value": 0.8},
        "excluded": {"numerator": 0, "denominator": 8, "value": 0.0},
        "value_if_included": 24 / 38,
        "p_value": 0.0001,
        "selective": True,
        "note": None,
    }


def notice(code: str, severity: str = "warning", message: str = "测试构造的提示") -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message}


def metrics_doc(case: Case, results: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "metrics",
        "project_id": "auto-demo",
        "synthetic": True,
        "target_entity_id": factory.TARGET,
        "timezone": "Asia/Shanghai",
        "prompt_set": {"version": 1, "content_hash": case.prompt_set["content_hash"]},
        "parameters": {"seed": 20261001, "iterations": 1000, "confidence_level": 0.95},
        "sufficiency_thresholds": {"min_windows": 3, "min_valid_observations": 30},
        "observations": {"total": 40, "in_selected_version": 39, "valid": 36},
        "registered_metrics": [],
        "notices": [],
        "exclusion_audit": exclusion_audit(),
        "results": results,
    }
    doc.update(fields)
    return doc


def good_results() -> list[dict[str, Any]]:
    return [
        metric("mention_rate", 0.8, 24, 30, ci=interval(0.7, 0.9)),
        metric("mention_rate", 0.8, 12, 15, platform="engine-a", ci=interval(0.6, 0.95)),
        metric("sentiment_positive_share", 0.5, 12, 24, role="descriptive", ci=interval(0.3, 0.7)),
        metric("top1_rate", 0.1, 3, 30, role="post_hoc", ci=interval(0.02, 0.25)),
    ]


def crosscheck_doc(case: Case, status: str = "pass", **fields: Any) -> dict[str, Any]:
    labels = {"pass": "通过", "fail": "不通过", "undetermined": "不可判定"}
    sufficient = status != "undetermined"
    doc: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "crosscheck",
        "project_id": "auto-demo",
        "synthetic": True,
        "prompt_set": {"version": 1, "content_hash": case.prompt_set["content_hash"]},
        "formula": "symmetric_relative_difference",
        "formula_text": "偏离度 = |A − B| / ((A + B) / 2)；A = B = 0 时为 0",
        "formula_note": "此公式是本项目的定义：047 给出了 30% 的阈值，但没有给出公式。",
        "threshold": 0.3,
        "preregistered": True,
        "metric_id": "mention_rate",
        "channel_a": "api",
        "channel_b": "manual",
        "parameters": {"seed": 20261001, "iterations": 1000, "confidence_level": 0.95},
        "status": status,
        "status_label": labels[status],
        "reason": f"测试构造的整体结论：{labels[status]}",
        "results": [
            {
                "platform": None,
                "a": {
                    "channel": "api",
                    "value": 0.6,
                    "numerator": 18,
                    "denominator": 30,
                    "windows": 3,
                    "sufficient": True,
                },
                "b": {
                    "channel": "manual",
                    "value": 0.45 if status != "fail" else 0.1,
                    "numerator": 9,
                    "denominator": 20,
                    "windows": 3 if sufficient else 1,
                    "sufficient": sufficient,
                },
                "deviation": 0.2857 if status != "fail" else 1.4286,
                "difference": 0.15,
                "interval": {
                    "method": "paired_cluster_bootstrap",
                    "level": 0.95,
                    "low": 0.05,
                    "high": 0.25,
                },
                "paired_queries": 10,
                "status": status,
                "status_label": labels[status],
                "reason": "测试构造的行结论",
            }
        ],
    }
    doc.update(fields)
    return doc


def compare_doc(case: Case, status: str = "ok") -> dict[str, Any]:
    prompt_set = {"version": 1, "content_hash": case.prompt_set["content_hash"]}
    refusal = (
        {
            "code": "model_version_changed",
            "message": "模型版本变化，须重建基线（测试构造）",
            "details": {},
        }
        if status == "refused"
        else None
    )
    results = []
    if status != "refused":
        results = [
            {
                "metric_id": "mention_rate",
                "platform": None,
                "baseline": {"value": 0.4, "numerator": 12, "denominator": 30},
                "current": {"value": 0.8, "numerator": 24, "denominator": 30},
                "difference": 0.4,
                "interval": {
                    "method": "paired_cluster_bootstrap",
                    "level": 0.95,
                    "low": 0.2,
                    "high": 0.6,
                },
                "paired_queries": 10,
                "sufficient": status == "ok",
                "conclusion": "提升" if status == "ok" else "不能判定",
                "conclusion_code": "improved" if status == "ok" else "inconclusive",
                "reason": "测试构造的对比结论",
            }
        ]
    return {
        "schema_version": "1.0",
        "kind": "compare",
        "project_id": "auto-demo",
        "synthetic": True,
        "baseline": {
            "project_id": "auto-baseline",
            "prompt_set": prompt_set,
            "windows": ["W0"],
            "valid_observations": 30,
        },
        "current": {
            "project_id": "auto-demo",
            "prompt_set": prompt_set,
            "windows": ["W1", "W2", "W3"],
            "valid_observations": 30,
        },
        "parameters": {"seed": 20261001, "iterations": 1000, "confidence_level": 0.95},
        "status": status,
        "refusal": refusal,
        "results": results,
    }


LEAD_TEXT = "引用域名 review-a.example 的减少与提及率下降相关，待核查"


def alerts_doc(status: str = "clear") -> dict[str, Any]:
    alerts = []
    leads = []
    if status == "alert":
        alerts = [
            {
                "rule_id": "mention_drop",
                "severity": "critical",
                "platform": "engine-a",
                "entity_id": factory.TARGET,
                "message": "提及率从 0.80 降至 0.40（测试构造）",
                "evidence": {"baseline": 0.8, "current": 0.4},
                "observation_ids": ["obs-000001"],
            }
        ]
        leads = [
            {"alert_rule_id": "mention_drop", "text": LEAD_TEXT, "domains": ["review-a.example"]}
        ]
    return {
        "schema_version": "1.0",
        "kind": "alerts",
        "project_id": "auto-demo",
        "synthetic": True,
        "baseline_window": "W1",
        "current_window": "W2",
        "thresholds": {"mention_drop": 0.2},
        "status": status,
        "alerts": alerts,
        "citation_changes": {
            "added": [],
            "removed": [{"domain": "review-a.example", "baseline": 5}] if status == "alert" else [],
            "changed": [],
            "note": "引用域名的变化只作为待核查的相关线索，不表示因果。",
        },
        "leads": leads,
    }


def evidence_doc(status: str = "pass") -> dict[str, Any]:
    problems = (
        [
            {
                "file": "observations.jsonl",
                "kind": "line_modified",
                "line": 3,
                "message": "第 3 行被改动（测试构造）",
            }
        ]
        if status == "fail"
        else []
    )
    return {
        "schema_version": "1.0",
        "kind": "evidence_verify",
        "project_id": "auto-demo",
        "synthetic": True,
        "status": status,
        "manifests": 1,
        "latest_manifest": {"sequence": 1, "manifest_hash": "b" * 64},
        "checked_files": 6,
        "problems": problems,
    }


def disputes_doc(unresolved: int = 0) -> dict[str, Any]:
    items = [
        {
            "observation_id": f"obs-00000{index}",
            "entity_id": factory.TARGET,
            "field": "mentioned",
            "a": True,
            "b": False,
            "resolved": index > unresolved,
        }
        for index in range(1, 3)
    ]
    return {
        "schema_version": "1.0",
        "kind": "disputes",
        "project_id": "auto-demo",
        "synthetic": True,
        "annotator_a": "标注员甲（虚构）",
        "annotator_b": "标注员乙（虚构）",
        "fields": [
            {
                "field": "mentioned",
                "compared": 20,
                "agreements": 18,
                "disagreements": 2,
                "kappa": 0.78,
                "null_reason": None,
            }
        ],
        "items": items,
        "unresolved": sum(1 for item in items if not item["resolved"]),
    }


def plan_doc(case: Case, *, preregistered: bool = True) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "kind": "recheck_plan",
        "project_id": "auto-demo",
        "synthetic": True,
        "prompt_set": {"version": 1, "content_hash": case.prompt_set["content_hash"]},
        "fraction": 0.2,
        "seed": 20261001,
        "preregistered": preregistered,
        "total_queries": 10,
        "query_ids": ["Q003", "Q007"],
        "items": [
            {
                "observation_id": "obs-000003",
                "query_id": "Q003",
                "entity_id": factory.TARGET,
                "source": "sample",
            },
            {
                "observation_id": "obs-000009",
                "query_id": "Q003",
                "entity_id": factory.TARGET,
                "source": "sample",
            },
        ],
    }


def result_doc(plan_hash: str | None, *, checked: int = 2, agreed: int = 2) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "kind": "recheck_result",
        "project_id": "auto-demo",
        "synthetic": True,
        "plan_hash": plan_hash,
        "fields": ["mentioned", "recommended", "rank"],
        "planned": 2,
        "checked": checked,
        "agreed": agreed,
        "disagreed": checked - agreed,
        "not_checked": 2 - checked,
        "pass_rate": agreed / checked if checked else None,
        "null_reason": None if checked else "没有已复核的项",
        "items": [
            {
                "observation_id": "obs-000003",
                "entity_id": factory.TARGET,
                "agree": True,
                "differences": [],
            }
        ][:checked],
    }


@dataclass
class Case:
    root: Path
    project: dict[str, Any]
    prompt_set: dict[str, Any]
    observations: list[dict[str, Any]] = field(default_factory=list)

    def path(self, name: str) -> Path:
        return self.root / FILES[name]

    def write(self, name: str, doc: Any) -> None:
        write_json(self.path(name), doc)

    def remove(self, name: str) -> None:
        self.path(name).unlink()

    def write_recheck(
        self, *, checked: int = 2, agreed: int = 2, preregistered: bool = True
    ) -> None:
        self.write("plan", plan_doc(self, preregistered=preregistered))
        plan_hash = hashlib.sha256(self.path("plan").read_bytes()).hexdigest()
        self.write("result", result_doc(plan_hash, checked=checked, agreed=agreed))

    def metrics(self, results: list[dict[str, Any]] | None = None, **fields: Any) -> None:
        self.write(
            "metrics", metrics_doc(self, good_results() if results is None else results, **fields)
        )

    def report(self, *argv: str, now: str | None = NOW) -> tuple[int, dict[str, Any]]:
        extra = ("--now", now) if now else ()
        code, data = run_cli_json("report", str(self.root), *extra, *argv)
        assert isinstance(data, dict)
        return code, data

    def report_text(self, *argv: str) -> tuple[int, str]:
        code, out, _ = run_cli("report", str(self.root), "--now", NOW, *argv)
        return code, out

    def read_report(self) -> dict[str, Any]:
        data: dict[str, Any] = factory.read(self.root / "out" / "report.json")
        return data


def observations_for(project: dict[str, Any], prompt_set: dict[str, Any]) -> list[dict[str, Any]]:
    """12 条观测：6 道题 × 2 个窗口；第 2 条超时无效，第 4 条属于另一版本题库。"""
    observations = []
    number = 0
    for window, started in (("W1", "2026-10-08T02:00:00Z"), ("W2", "2026-10-09T17:30:00Z")):
        for index in range(1, 7):
            number += 1
            fields: dict[str, Any] = {}
            if number == 2:
                fields.update(status="invalid", invalid_reason="timeout")
            if number == 4:
                fields.update(prompt_set_hash="0" * 64)
            observations.append(
                factory.make_observation(
                    project,
                    prompt_set,
                    number,
                    query_id=f"Q{index:03d}",
                    window=window,
                    started_at=started,
                    channel="api" if index % 2 else "manual",
                    citations=[factory.make_citation(1, "lanxu-auto.example")]
                    if index == 1
                    else None,
                    **fields,
                )
            )
    return observations


def build_case(
    root: Path,
    *,
    prereg: dict[str, Any] | None = None,
    project_fields: dict[str, Any] | None = None,
    entity_extra: list[dict[str, Any]] | None = None,
    complete: bool = True,
    mutate_observations: Any = None,
) -> Case:
    """一个已封存、已采集的合成项目；complete=True 时写入一套“全部通过”的结果文件。"""
    project = factory.make_project(root, prereg=prereg, **(project_fields or {}))
    factory.make_entities(root, extra=entity_extra)
    prompts = [factory.make_prompt(f"Q{index:03d}") for index in range(1, 11)]
    prompt_set = factory.seal_prompts(root, project, prompts)
    observations = observations_for(project, prompt_set)
    if mutate_observations is not None:
        mutate_observations(observations)
    factory.write_records(root, observations=observations)
    audit.append_event(
        root,
        actor="数据管理员（虚构）",
        action="exclude",
        target_type="observation",
        target_id="batch",
        reason="同一 trace_id 重复导入",
        occurred_at="2026-10-10T01:00:00Z",
        details={"rule_id": "EX-DUP", "count": 1, "observation_ids": ["obs-000005"]},
    )
    case = Case(root, project, prompt_set, observations)
    if complete:
        case.metrics()
        case.write("crosscheck", crosscheck_doc(case))
        case.write("compare", compare_doc(case))
        case.write("alerts", alerts_doc())
        case.write("evidence", evidence_doc())
        case.write("disputes", disputes_doc())
        case.write_recheck()
    return case


def rule(report: dict[str, Any], rule_id: str) -> dict[str, Any]:
    matches = [item for item in report["rules"] if item["rule_id"] == rule_id]
    assert len(matches) == 1, f"规则 {rule_id} 应出现且只出现一次"
    return matches[0]


def statuses(report: dict[str, Any]) -> dict[str, str]:
    return {item["rule_id"]: item["status"] for item in report["rules"]}


def strings(value: Any) -> list[str]:
    """JSON 值中的全部字符串（含键）。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (key, *strings(item))]
    if isinstance(value, list):
        return [text for item in value for text in strings(item)]
    return []


def deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)
