"""`geo-verify report` 的判定规则与预检结论。

每个用例只改动一个结果文件，期望的结论与规则状态按任务说明的判定顺序手工推出：
完整性硬伤 → 不通过；否则数据不足 → 数据不足；否则未达标 → 不通过；否则有保留 → 有条件通过；否则通过。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from helpers_report import (
    LEAD_TEXT,
    Case,
    alerts_doc,
    build_case,
    compare_doc,
    crosscheck_doc,
    disputes_doc,
    evidence_doc,
    exclusion_audit,
    interval,
    metric,
    notice,
    rule,
    selective_comparison,
    statuses,
)

from geo_verify.jsonl import write_json

ALL_RULES = [
    "R-EVIDENCE",
    "R-EXCLUSION-UNREGISTERED",
    "R-EXCLUSION-SELECTIVE",
    "R-PREREG-CHANGED",
    "R-PROMPTS-SEALED",
    "R-METRICS",
    "R-ACCEPTANCE-DEFINED",
    "R-SUFFICIENCY",
    "R-MODEL-VERSION",
    "R-THRESHOLD",
    "R-CROSSCHECK",
    "R-RECHECK",
    "R-DISPUTES",
    "R-ALERTS",
    "R-COMPARE",
    "R-PROMPT-VERSION",
    "R-DATA-NOTICES",
    "R-INPUT-CONSISTENCY",
]
EXIT = {"pass": 0, "conditional_pass": 0, "fail": 1, "insufficient_data": 3}
LABEL = {
    "pass": "通过",
    "conditional_pass": "有条件通过",
    "fail": "不通过",
    "insufficient_data": "数据不足",
}


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return build_case(tmp_path / "proj")


def _verdict(case: Case, expected: str) -> dict[str, Any]:
    code, data = case.report()
    assert data["verdict"]["code"] == expected, data["verdict"]
    assert data["verdict"]["label"] == LABEL[expected]
    assert code == EXIT[expected]
    report = case.read_report()
    assert {key: data[key] for key in report} == report
    return report


def test_everything_in_order_passes(case: Case) -> None:
    report = _verdict(case, "pass")
    assert [item["rule_id"] for item in report["rules"]] == ALL_RULES
    assert set(statuses(report).values()) == {"pass"}
    labels = {item["status"]: item["status_label"] for item in report["rules"]}
    assert labels == {"pass": "通过"}
    assert report["verdict"]["reservations"] == []
    for item in report["rules"]:
        assert item["title"].strip()
        assert item["basis"].strip()
        assert isinstance(item["details"], dict)


# ------------------------------------------------------------------ 1. 完整性硬伤


def test_evidence_failure_fails_even_when_data_is_insufficient(case: Case) -> None:
    case.write("evidence", evidence_doc("fail"))
    case.metrics([metric("mention_rate", 0.8, ci=interval(0.7, 0.9), sufficient=False, windows=1)])
    report = _verdict(case, "fail")
    evidence = rule(report, "R-EVIDENCE")
    assert evidence["status"] == "fail"
    assert evidence["details"]["problems"][0]["kind"] == "line_modified"
    assert rule(report, "R-SUFFICIENCY")["status"] == "insufficient"
    assert "R-EVIDENCE" in report["verdict"]["decided_by"]


def test_unregistered_exclusion_rule_fails_and_is_disclosed(case: Case) -> None:
    rules = [
        {
            "rule_id": "EX-DUP",
            "registered": True,
            "listed_observations": 1,
            "excluded_observations": 1,
        },
        {
            "rule_id": "EX-LOW-QUALITY",
            "registered": False,
            "listed_observations": 6,
            "excluded_observations": 6,
        },
    ]
    case.metrics(
        exclusion_audit=exclusion_audit(rules=rules),
        notices=[notice("unregistered_exclusion_rules", "error")],
    )
    report = _verdict(case, "fail")
    item = rule(report, "R-EXCLUSION-UNREGISTERED")
    assert item["status"] == "fail"
    assert item["details"]["unregistered_rules"] == ["EX-LOW-QUALITY"]
    assert item["details"]["excluded_observations"] == 6
    # S09：剔除数量与规则出现在报告里
    exclusions = report["exclusions"]
    assert exclusions["excluded_observations"] == 7
    assert exclusions["unregistered_rules"] == ["EX-LOW-QUALITY"]
    assert {row["rule_id"]: row["excluded_observations"] for row in exclusions["rules"]} == {
        "EX-DUP": 1,
        "EX-LOW-QUALITY": 6,
    }
    assert rule(report, "R-EXCLUSION-SELECTIVE")["status"] == "pass"
    # 剔除规则的提示已有专门的规则，不重复计入“指标计算提示”
    assert rule(report, "R-DATA-NOTICES")["status"] == "pass"


def test_selective_exclusion_fails_and_shows_retained_versus_excluded(case: Case) -> None:
    rules = [
        {
            "rule_id": "EX-DUP",
            "registered": True,
            "listed_observations": 8,
            "excluded_observations": 8,
        }
    ]
    case.metrics(
        exclusion_audit=exclusion_audit(
            rules=rules, selective=True, comparisons=[selective_comparison()]
        ),
        notices=[notice("selective_exclusion", "error")],
    )
    report = _verdict(case, "fail")
    item = rule(report, "R-EXCLUSION-SELECTIVE")
    assert item["status"] == "fail"
    (comparison,) = item["details"]["comparisons"]
    assert comparison["retained"] == {"numerator": 24, "denominator": 30, "value": 0.8}
    assert comparison["excluded"] == {"numerator": 0, "denominator": 8, "value": 0.0}
    assert item["details"]["excluded_observations"] == 8
    assert item["details"]["rules"] == [{"rule_id": "EX-DUP", "excluded_observations": 8}]
    assert report["exclusions"]["selective"] is True
    assert report["exclusions"]["comparisons"][0]["selective"] is True
    assert rule(report, "R-EXCLUSION-UNREGISTERED")["status"] == "pass"


def test_preregistration_change_reported_by_metrics_fails(case: Case) -> None:
    case.metrics(notices=[notice("preregistration_changed", "error")])
    report = _verdict(case, "fail")
    assert rule(report, "R-PREREG-CHANGED")["status"] == "fail"


def test_preregistration_change_is_detected_from_project_files(case: Case) -> None:
    edited = copy.deepcopy(case.project)
    edited["preregistration"]["metrics"][0]["threshold"]["value"] = 0.3  # 封存后放宽阈值
    write_json(case.root / "project.json", edited)
    report = _verdict(case, "fail")
    item = rule(report, "R-PREREG-CHANGED")
    assert item["status"] == "fail"
    assert item["details"]["sealed_hash"] != item["details"]["current_hash"]


def test_without_metrics_preregistration_is_still_checked_from_project_files(case: Case) -> None:
    case.remove("metrics")
    report = _verdict(case, "insufficient_data")
    assert rule(report, "R-PREREG-CHANGED")["status"] == "pass"


# ------------------------------------------------------------------ 2. 数据不足


def test_missing_metrics_file_means_insufficient_data(case: Case) -> None:
    case.remove("metrics")
    report = _verdict(case, "insufficient_data")
    found = statuses(report)
    assert found["R-METRICS"] == "insufficient"
    for rule_id in (
        "R-EXCLUSION-UNREGISTERED",
        "R-EXCLUSION-SELECTIVE",
        "R-ACCEPTANCE-DEFINED",
        "R-SUFFICIENCY",
        "R-MODEL-VERSION",
        "R-THRESHOLD",
        "R-PROMPT-VERSION",
        "R-DATA-NOTICES",
    ):
        assert found[rule_id] == "not_run", rule_id
    labels = {item["status"]: item["status_label"] for item in report["rules"]}
    assert labels["not_run"] == "未执行"
    assert labels["insufficient"] == "数据不足"
    assert report["metrics"] == {"status": "not_run"}
    assert report["exclusions"] == {"status": "not_run"}


def test_no_acceptance_metric_registered_means_insufficient_data(tmp_path: Path) -> None:
    prereg = {
        "metrics": [
            {
                "metric_id": "mention_rate",
                "definition_version": "1.0",
                "role": "descriptive",
                "threshold": None,
            }
        ]
    }
    case = build_case(tmp_path / "proj", prereg=prereg)
    case.metrics([metric("mention_rate", 0.8, role="descriptive", ci=interval(0.7, 0.9))])
    report = _verdict(case, "insufficient_data")
    assert rule(report, "R-ACCEPTANCE-DEFINED")["status"] == "insufficient"
    assert rule(report, "R-SUFFICIENCY")["status"] == "not_run"
    assert rule(report, "R-THRESHOLD")["status"] == "not_run"


def test_acceptance_metric_without_overall_result_means_insufficient_data(case: Case) -> None:
    case.metrics([metric("mention_rate", 0.8, platform="engine-a", ci=interval(0.7, 0.9))])
    report = _verdict(case, "insufficient_data")
    item = rule(report, "R-ACCEPTANCE-DEFINED")
    assert item["status"] == "insufficient"
    assert item["details"]["missing"] == ["mention_rate"]


def test_acceptance_result_that_metrics_marked_post_hoc_cannot_decide(case: Case) -> None:
    # 例如口径版本与登记不符：指标模块把它降为事后指标
    case.metrics([metric("mention_rate", 0.8, role="post_hoc", ci=interval(0.7, 0.9))])
    report = _verdict(case, "insufficient_data")
    assert rule(report, "R-ACCEPTANCE-DEFINED")["status"] == "insufficient"


def test_s02_one_question_once_in_one_window_is_insufficient(case: Case) -> None:
    reasons = ["时间窗口不足 3 个", "有效观测不足 30 次"]
    case.metrics(
        [
            metric(
                "mention_rate",
                1.0,
                10,
                10,
                ci=interval(0.7225, 1.0, "wilson"),
                sufficient=False,
                windows=1,
                valid_observations=10,
                reasons=reasons,
            )
        ]
    )
    report = _verdict(case, "insufficient_data")
    item = rule(report, "R-SUFFICIENCY")
    assert item["status"] == "insufficient"
    (entry,) = item["details"]["metrics"]
    assert entry["metric_id"] == "mention_rate"
    assert entry["reasons"] == reasons
    # 点估计 1.0 ≥ 0.6，但样本不充分时不得通过
    assert rule(report, "R-THRESHOLD")["status"] == "pass"


def test_s22_null_value_still_produces_a_report(case: Case) -> None:
    case.metrics(
        [
            metric(
                "mention_rate",
                None,
                0,
                0,
                null_reason="没有有效观测，分母为 0",
                sufficient=False,
                windows=0,
                valid_observations=0,
            ),
            metric(
                "top1_rate",
                None,
                0,
                0,
                role="post_hoc",
                null_reason="没有可排序的观测，分母为 0",
                sufficient=False,
            ),
        ]
    )
    report = _verdict(case, "insufficient_data")
    item = rule(report, "R-SUFFICIENCY")
    assert item["details"]["metrics"][0]["null_reason"] == "没有有效观测，分母为 0"
    assert rule(report, "R-THRESHOLD")["status"] == "not_run"
    values = {entry["metric_id"]: entry["value"] for entry in report["metrics"]["items"]}
    assert values == {"mention_rate": None, "top1_rate": None}


def test_model_version_change_requires_a_new_baseline(case: Case) -> None:
    case.metrics(
        notices=[
            notice("model_version_changed", message="平台 engine-a 出现 2 个模型版本，须重建基线")
        ]
    )
    report = _verdict(case, "insufficient_data")
    item = rule(report, "R-MODEL-VERSION")
    assert item["status"] == "insufficient"
    assert "重建基线" in item["basis"]


# ------------------------------------------------------------------ 3. 未达标


def test_point_estimate_below_threshold_fails(case: Case) -> None:
    case.metrics([metric("mention_rate", 0.5, 15, 30, ci=interval(0.35, 0.65))])
    report = _verdict(case, "fail")
    item = rule(report, "R-THRESHOLD")
    assert item["status"] == "fail"
    (entry,) = item["details"]["metrics"]
    assert entry["value"] == 0.5
    assert entry["threshold"] == {"op": ">=", "value": 0.6}
    assert entry["met"] is False


@pytest.mark.parametrize(
    ("value", "low", "high", "expected_rule", "expected_verdict"),
    [
        (0.03, 0.01, 0.045, "pass", "pass"),
        (0.05, 0.03, 0.05, "pass", "pass"),  # 等于阈值也满足 <=
        (0.04, 0.02, 0.07, "warning", "conditional_pass"),  # 区间上端跨过 0.05
        (0.08, 0.05, 0.12, "fail", "fail"),
    ],
)
def test_lower_is_better_threshold(
    tmp_path: Path, value: float, low: float, high: float, expected_rule: str, expected_verdict: str
) -> None:
    prereg = {
        "metrics": [
            {
                "metric_id": "negative_miscitation_rate",
                "definition_version": "1.0",
                "role": "acceptance",
                "threshold": {"op": "<=", "value": 0.05},
            }
        ]
    }
    case = build_case(tmp_path / "proj", prereg=prereg)
    case.metrics([metric("negative_miscitation_rate", value, ci=interval(low, high))])
    report = _verdict(case, expected_verdict)
    assert rule(report, "R-THRESHOLD")["status"] == expected_rule


@pytest.mark.parametrize(
    ("op", "value", "low", "expected"),
    [
        (">", 0.6, 0.55, "fail"),  # 等于阈值不满足 >
        (">", 0.7, 0.61, "pass"),
        (">", 0.7, 0.6, "warning"),  # 区间下端恰好落在不满足的一侧
        ("<", 0.6, 0.5, "fail"),
    ],
)
def test_strict_operators(tmp_path: Path, op: str, value: float, low: float, expected: str) -> None:
    prereg = {
        "metrics": [
            {
                "metric_id": "mention_rate",
                "definition_version": "1.0",
                "role": "acceptance",
                "threshold": {"op": op, "value": 0.6},
            }
        ]
    }
    case = build_case(tmp_path / "proj", prereg=prereg)
    high = value + 0.1 if op == ">" else value + 0.0
    case.metrics([metric("mention_rate", value, ci=interval(low, high))])
    case.report()
    assert rule(case.read_report(), "R-THRESHOLD")["status"] == expected


def test_crosscheck_failure_fails(case: Case) -> None:
    case.write("crosscheck", crosscheck_doc(case, "fail"))
    report = _verdict(case, "fail")
    assert rule(report, "R-CROSSCHECK")["status"] == "fail"
    assert report["crosscheck"]["status"] == "fail"


# ------------------------------------------------------------------ 4. 有保留


def _reserved(case: Case, rule_id: str, status: str = "warning") -> dict[str, Any]:
    report = _verdict(case, "conditional_pass")
    assert rule(report, rule_id)["status"] == status
    reserved = [item["rule_id"] for item in report["verdict"]["reservations"]]
    assert rule_id in reserved
    for item in report["verdict"]["reservations"]:
        assert item["text"].strip()
    return report


def test_interval_crossing_threshold_is_a_reservation(case: Case) -> None:
    case.metrics([metric("mention_rate", 0.7, 21, 30, ci=interval(0.55, 0.82))])
    report = _reserved(case, "R-THRESHOLD")
    (entry,) = rule(report, "R-THRESHOLD")["details"]["metrics"]
    assert entry["met"] is True
    assert entry["interval_crosses_threshold"] is True


def test_missing_interval_is_a_reservation(case: Case) -> None:
    case.metrics([metric("mention_rate", 0.8, 24, 30, ci=None)])
    _reserved(case, "R-THRESHOLD")


@pytest.mark.parametrize("missing", ["crosscheck", "evidence", "plan"])
def test_checks_not_run_are_reservations(case: Case, missing: str) -> None:
    case.remove(missing)
    if missing == "plan":
        case.remove("result")
    rule_id = {"crosscheck": "R-CROSSCHECK", "evidence": "R-EVIDENCE", "plan": "R-RECHECK"}[missing]
    report = _reserved(case, rule_id, "not_run")
    section = {"crosscheck": "crosscheck", "evidence": "evidence", "plan": "recheck"}[missing]
    assert report[section] == {"status": "not_run"}


def test_crosscheck_undetermined_is_a_reservation(case: Case) -> None:
    case.write("crosscheck", crosscheck_doc(case, "undetermined"))
    _reserved(case, "R-CROSSCHECK")


def test_crosscheck_not_preregistered_is_a_reservation(case: Case) -> None:
    case.write("crosscheck", crosscheck_doc(case, preregistered=False, threshold=0.5))
    _reserved(case, "R-CROSSCHECK")


def test_unresolved_disputes_are_a_reservation(case: Case) -> None:
    case.write("disputes", disputes_doc(unresolved=1))
    report = _reserved(case, "R-DISPUTES")
    assert report["disputes"]["unresolved"] == 1


def test_missing_disputes_alerts_or_compare_is_not_a_reservation(case: Case) -> None:
    for name in ("disputes", "alerts", "compare"):
        case.remove(name)
    report = _verdict(case, "pass")
    for rule_id in ("R-DISPUTES", "R-ALERTS", "R-COMPARE"):
        assert rule(report, rule_id)["status"] == "not_run"
    assert report["alerts"] == {"status": "not_run"}
    assert report["compare"] == {"status": "not_run"}
    assert report["disputes"] == {"status": "not_run"}


def test_alerts_are_a_reservation_and_leads_keep_their_wording(case: Case) -> None:
    case.write("alerts", alerts_doc("alert"))
    report = _reserved(case, "R-ALERTS")
    assert report["alerts"]["leads"][0]["text"] == LEAD_TEXT
    assert report["alerts"]["alerts"][0]["message"] == "提及率从 0.80 降至 0.40（测试构造）"


@pytest.mark.parametrize("status", ["refused", "insufficient"])
def test_compare_refused_or_insufficient_is_a_reservation(case: Case, status: str) -> None:
    case.write("compare", compare_doc(case, status))
    report = _reserved(case, "R-COMPARE")
    assert report["compare"]["status"] == status


def test_recheck_plan_without_result_is_a_reservation(case: Case) -> None:
    case.remove("result")
    report = _reserved(case, "R-RECHECK")
    assert report["recheck"]["plan"]["query_ids"] == ["Q003", "Q007"]
    assert report["recheck"]["result"] is None


def test_incomplete_recheck_is_a_reservation(case: Case) -> None:
    case.write_recheck(checked=1, agreed=1)
    report = _reserved(case, "R-RECHECK")
    assert report["recheck"]["result"]["not_checked"] == 1


def test_recheck_not_preregistered_is_a_reservation(case: Case) -> None:
    case.write_recheck(preregistered=False)
    _reserved(case, "R-RECHECK")


def test_recheck_result_for_another_plan_is_a_reservation(case: Case) -> None:
    result = case.path("result")
    data = __import__("json").loads(result.read_text(encoding="utf-8"))
    data["plan_hash"] = "c" * 64
    write_json(result, data)
    report = _reserved(case, "R-RECHECK")
    assert report["recheck"]["result"]["plan_matches"] is False


def test_observations_outside_the_selected_prompt_set_are_a_reservation(case: Case) -> None:
    case.metrics(
        notices=[notice("prompt_set_version_mismatch", message="有 1 条观测不属于所选题库版本")]
    )
    _reserved(case, "R-PROMPT-VERSION")


def test_other_metric_warnings_are_reservations_but_info_is_not(case: Case) -> None:
    case.metrics(
        notices=[
            notice("unannotated_observations", message="有 2 条有效观测缺少标注"),
            notice("trivia", "info"),
        ]
    )
    report = _reserved(case, "R-DATA-NOTICES")
    codes = [item["code"] for item in rule(report, "R-DATA-NOTICES")["details"]["notices"]]
    assert codes == ["unannotated_observations"]


def test_result_files_from_another_prompt_set_version_are_a_reservation(case: Case) -> None:
    case.write(
        "crosscheck", crosscheck_doc(case, prompt_set={"version": 9, "content_hash": "d" * 64})
    )
    _reserved(case, "R-INPUT-CONSISTENCY")


def test_reservations_list_every_reason(case: Case) -> None:
    case.write("disputes", disputes_doc(unresolved=2))
    case.write("alerts", alerts_doc("alert"))
    case.remove("evidence")
    report = _verdict(case, "conditional_pass")
    reserved = [item["rule_id"] for item in report["verdict"]["reservations"]]
    assert reserved == ["R-EVIDENCE", "R-DISPUTES", "R-ALERTS"]


# ------------------------------------------------------------------ 只有预先登记的验收指标参与结论


def test_post_hoc_metrics_are_shown_but_never_decide(case: Case) -> None:
    case.metrics(
        [
            metric("mention_rate", 0.8, 24, 30, ci=interval(0.7, 0.9)),
            metric(
                "top1_rate", 0.01, 0, 30, role="post_hoc", ci=interval(0.0, 0.1), sufficient=False
            ),
            metric("mention_rate", 0.2, 3, 15, platform="engine-b", ci=interval(0.05, 0.45)),
        ]
    )
    report = _verdict(case, "pass")
    items = {
        (item["metric_id"], item["scope"]["platform"]): item for item in report["metrics"]["items"]
    }
    post_hoc = items[("top1_rate", None)]
    assert post_hoc["use"] == "post_hoc"
    assert post_hoc["use_label"] == "事后指标，不能用于验收结论"
    assert items[("mention_rate", None)]["use"] == "acceptance"
    # 分平台的结果只展示，不参与结论
    assert items[("mention_rate", "engine-b")]["use"] == "display_only"


def test_accuracy_basis_selects_the_registered_accuracy_metric(tmp_path: Path) -> None:
    accuracy = [
        {
            "metric_id": "accuracy",
            "definition_version": "1.0",
            "role": "acceptance",
            "threshold": {"op": ">=", "value": 0.9},
        }
    ]
    for basis, expected in (("strict", "fail"), ("verifiable", "pass")):
        case = build_case(tmp_path / basis, prereg={"metrics": accuracy, "accuracy_basis": basis})
        roles = {"strict": "acceptance", "verifiable": "post_hoc"}
        if basis == "verifiable":
            roles = {"strict": "post_hoc", "verifiable": "acceptance"}
        case.metrics(
            [
                metric(
                    "accuracy_verifiable",
                    0.95,
                    38,
                    40,
                    role=roles["verifiable"],
                    ci=interval(0.92, 0.98),
                ),
                metric(
                    "accuracy_strict", 0.85, 34, 40, role=roles["strict"], ci=interval(0.8, 0.89)
                ),
            ]
        )
        report = _verdict(case, expected)
        (entry,) = rule(report, "R-THRESHOLD")["details"]["metrics"]
        assert entry["metric_id"] == f"accuracy_{basis}"


def test_registered_metrics_listing_from_metrics_file_is_used(case: Case) -> None:
    registered = [
        {
            "registered_id": "mention_rate",
            "metric_ids": ["mention_rate"],
            "role": "acceptance",
            "threshold": {"op": ">=", "value": 0.6},
            "definition_version": "1.0",
            "effective": True,
            "note": None,
        }
    ]
    case.metrics(registered_metrics=registered)
    _verdict(case, "pass")


def test_daily_acceptance_metric_must_hold_on_every_day(tmp_path: Path) -> None:
    prereg = {
        "metrics": [
            {
                "metric_id": "recommendation_rate_daily",
                "definition_version": "1.0",
                "role": "acceptance",
                "threshold": {"op": ">=", "value": 0.5},
            }
        ]
    }
    case = build_case(tmp_path / "proj", prereg=prereg)
    days = [("2026-10-08", 0.8, 0.7), ("2026-10-09", 0.4, 0.3)]
    case.metrics(
        [
            metric("recommendation_rate_daily", value, date=day, ci=interval(low, value + 0.1))
            for day, value, low in days
        ]
        + [
            metric(
                "recommendation_rate_daily",
                0.1,
                date="2026-10-09",
                platform="engine-a",
                ci=interval(0.0, 0.2),
            )
        ]
    )
    report = _verdict(case, "fail")
    entries = rule(report, "R-THRESHOLD")["details"]["metrics"]
    assert [(entry["scope"]["date"], entry["met"]) for entry in entries] == [
        ("2026-10-08", True),
        ("2026-10-09", False),
    ]


def test_exit_codes_follow_the_verdict(case: Case) -> None:
    assert case.report()[0] == 0
    case.write("disputes", disputes_doc(unresolved=1))
    assert case.report()[0] == 0
    case.write("crosscheck", crosscheck_doc(case, "fail"))
    assert case.report()[0] == 1
    case.remove("metrics")
    assert case.report()[0] == 3
    case.write("evidence", evidence_doc("fail"))
    assert case.report()[0] == 1


def test_tampered_sealed_prompt_set_fails_with_a_report(case: Case) -> None:
    sealed = case.root / "prompts" / "sealed" / "v0001.json"
    data = __import__("json").loads(sealed.read_text(encoding="utf-8"))
    data["prompts"][0]["text"] = "封存后被改过的题目"
    write_json(sealed, data)
    report = _verdict(case, "fail")
    item = rule(report, "R-PROMPTS-SEALED")
    assert item["status"] == "fail"
    assert "v0001.json" in item["basis"]
    assert "R-PROMPTS-SEALED" in report["verdict"]["decided_by"]
    assert report["project"]["prompt_set"] is None
    # 被改动的文件仍列入输入及其哈希，便于核对
    assert "prompts/sealed/v0001.json" in {entry["path"] for entry in report["inputs"]}


def test_unsealed_project_does_not_run_the_sealing_check(tmp_path: Path) -> None:
    from helpers_report import Case as Bare

    root = tmp_path / "proj"
    __import__("factory").make_project(root)
    case = Bare(root, {}, {})
    code, data = case.report("--formats", "json")
    assert code == 3
    assert rule(data, "R-PROMPTS-SEALED")["status"] == "not_run"
