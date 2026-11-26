"""`out/report.json` 的内容：声明、项目与登记口径、指标披露、各结果摘要、限制、披露栏、签字栏、输入哈希。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import factory
import pytest
from helpers_report import (
    FORBIDDEN,
    NOW,
    PRECHECK_NOTICE,
    SYNTHETIC_WATERMARK,
    Case,
    alerts_doc,
    build_case,
    crosscheck_doc,
    disputes_doc,
    evidence_doc,
    exclusion_audit,
    interval,
    metric,
    notice,
    selective_comparison,
    strings,
)

from geo_verify.jsonl import write_json


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return build_case(tmp_path / "proj")


def _report(case: Case, *argv: str) -> dict[str, Any]:
    code, _ = case.report("--formats", "json", *argv)
    assert code in (0, 1, 3)
    return case.read_report()


def test_header_notice_and_watermark(case: Case) -> None:
    report = _report(case)
    assert report["schema_version"] == "1.0"
    assert report["kind"] == "report"
    assert report["project_id"] == "auto-demo"
    assert report["notice"] == PRECHECK_NOTICE
    assert report["synthetic"] is True
    assert report["watermark"] == SYNTHETIC_WATERMARK
    assert report["generated_at"] == NOW


def test_real_project_has_no_watermark(tmp_path: Path) -> None:
    case = build_case(tmp_path / "proj", project_fields={"synthetic": False})
    report = _report(case)
    assert "synthetic" not in report
    assert report["watermark"] is None
    assert not any(item["code"] == "synthetic_data" for item in report["limitations"])


def test_project_section(case: Case) -> None:
    project = _report(case)["project"]
    assert project["name"] == case.project["name"]
    assert project["client"] == {"id": "client-lanxu", "name": "澜序汽车（虚构委托方）"}
    assert project["provider"] == {"id": "provider-demo", "name": "示例服务方（虚构）"}
    assert project["timezone"] == "Asia/Shanghai"
    assert project["target"] == {"entity_id": factory.TARGET, "name": "澜序"}
    assert project["competitors"] == [{"entity_id": factory.COMPETITOR, "name": "岑野"}]
    platforms = {item["platform_id"]: item for item in project["platforms"]}
    assert platforms["engine-a"]["channels"] == [
        {"channel": "api", "label": "API 通道"},
        {"channel": "manual", "label": "人工采集通道"},
    ]
    assert project["environment"] == case.project["environment"]
    assert project["prompt_set"] == {
        "version": 1,
        "content_hash": case.prompt_set["content_hash"],
        "total_queries": 10,
        "sealed_at": "2026-10-02T03:00:00Z",
        "sealed_by": "甲方项目负责人（虚构）",
    }
    # 观测在项目时区下的日期：W2 的 17:30Z 是上海时间次日 01:30
    period = project["period"]
    assert period["date_from"] == "2026-10-08"
    assert period["date_to"] == "2026-10-10"
    assert period["windows"] == ["W1", "W2"]
    assert period["observations"] == {
        "total": 12,
        "valid": 11,
        "in_selected_prompt_set": 11,
        "excluded": 1,
    }
    assert period["channels"] == {"api": 6, "manual": 6}


def test_preregistration_section(case: Case) -> None:
    prereg = _report(case)["preregistration"]
    assert prereg["registered_at"] == "2026-10-01T01:00:00Z"
    assert prereg["changed_after_seal"] is False
    assert prereg["metrics"][0] == {
        "metric_id": "mention_rate",
        "title": "品牌提及率",
        "definition_version": "1.0",
        "role": "acceptance",
        "role_label": "验收指标",
        "threshold": {"op": ">=", "value": 0.6},
    }
    assert prereg["accuracy_basis"] == "verifiable"
    assert prereg["crosscheck"]["threshold"] == 0.3
    assert (
        prereg["crosscheck"]["formula_text"] == "偏离度 = |A − B| / ((A + B) / 2)；A = B = 0 时为 0"
    )
    assert "本项目的定义" in prereg["crosscheck"]["formula_note"]
    assert prereg["recheck"] == {"fraction": 0.2, "seed": 20261001}
    assert prereg["exclusion_rules"] == [
        {"rule_id": "EX-DUP", "description": "同一 trace_id 重复导入的记录"}
    ]


def test_each_disclosed_metric_carries_its_context(case: Case) -> None:
    items = _report(case)["metrics"]["items"]
    assert [item["metric_id"] for item in items] == [
        "mention_rate",
        "mention_rate",
        "sentiment_positive_share",
        "top1_rate",
    ]
    overall = items[0]
    assert overall["title"] == "品牌提及率"
    assert overall["scope_label"] == "整体"
    assert items[1]["scope_label"] == "平台 engine-a"
    assert (overall["value"], overall["numerator"], overall["denominator"]) == (0.8, 24, 30)
    assert overall["interval"] == {
        "method": "cluster_bootstrap",
        "level": 0.95,
        "low": 0.7,
        "high": 0.9,
    }
    assert overall["method_label"] == "按题聚类 bootstrap"
    assert overall["sufficiency"]["sufficient"] is True
    assert overall["excluded"] == {"count": 1, "rules": [{"rule": "audit:EX-DUP", "count": 1}]}
    assert overall["coverage"]["queries"] == 10
    assert (overall["coverage"]["date_from"], overall["coverage"]["date_to"]) == (
        "2026-10-08",
        "2026-10-10",
    )
    assert overall["environment"] == case.project["environment"]
    assert overall["threshold"] == {"op": ">=", "value": 0.6}
    assert overall["use"] == "acceptance"
    assert overall["use_label"] == "参与验收结论"
    assert isinstance(overall["limitations"], list)
    assert any("API 通道" in text for text in overall["limitations"])
    descriptive = items[2]
    assert descriptive["use"] == "descriptive"
    assert descriptive["threshold"] is None
    post_hoc = items[3]
    assert post_hoc["use_label"] == "事后指标，不能用于验收结论"
    assert "事后指标，不能用于验收结论" in post_hoc["limitations"]


def test_wilson_method_label(case: Case) -> None:
    case.metrics([metric("mention_rate", 0.8, 24, 30, ci=interval(0.6269, 0.9049, "wilson"))])
    assert _report(case)["metrics"]["items"][0]["method_label"] == "Wilson 区间"


def test_crosscheck_section_names_the_formula_as_project_definition(case: Case) -> None:
    crosscheck = _report(case)["crosscheck"]
    assert crosscheck["status"] == "pass"
    assert crosscheck["status_label"] == "通过"
    assert crosscheck["formula_text"] == "偏离度 = |A − B| / ((A + B) / 2)；A = B = 0 时为 0"
    assert (
        crosscheck["formula_note"]
        == "此公式是本项目的定义：047 给出了 30% 的阈值，但没有给出公式。"
    )
    assert crosscheck["formula_declaration"] == "此公式是本项目的定义"
    assert crosscheck["threshold"] == 0.3
    assert crosscheck["channel_a"] == {"channel": "api", "label": "API 通道"}
    assert crosscheck["channel_b"] == {"channel": "manual", "label": "人工采集通道"}
    (row,) = crosscheck["results"]
    assert row["deviation"] == 0.2857
    assert row["difference_pp"] == pytest.approx(15.0)
    assert row["interval_pp"] == {"low": pytest.approx(5.0), "high": pytest.approx(25.0)}
    assert row["a"]["label"] == "API 通道"


def test_summaries_of_other_result_files(case: Case) -> None:
    case.write("alerts", alerts_doc("alert"))
    case.write("disputes", disputes_doc(unresolved=1))
    report = _report(case)
    assert report["compare"]["status"] == "ok"
    assert report["compare"]["status_label"] == "已比较"
    assert report["compare"]["results"][0]["conclusion"] == "提升"
    assert report["alerts"]["status_label"] == "有告警"
    assert (
        report["alerts"]["citation_changes"]["note"]
        == "引用域名的变化只作为待核查的相关线索，不表示因果。"
    )
    assert report["evidence"]["status"] == "pass"
    assert report["evidence"]["checked_files"] == 6
    disputes = report["disputes"]
    assert (disputes["annotator_a"], disputes["unresolved"]) == ("标注员甲（虚构）", 1)
    assert [item["observation_id"] for item in disputes["unresolved_items"]] == ["obs-000001"]
    recheck = report["recheck"]
    assert recheck["status"] == "ok"
    assert recheck["plan"]["fraction"] == 0.2
    assert recheck["plan"]["seed"] == 20261001
    assert recheck["plan"]["item_count"] == 2
    assert recheck["plan"]["queries"] == [
        {"query_id": "Q003", "text": "第 3 题：20 万左右的纯电 SUV 怎么选？（虚构题目 Q003）"},
        {"query_id": "Q007", "text": "第 7 题：20 万左右的纯电 SUV 怎么选？（虚构题目 Q007）"},
    ]
    assert recheck["result"]["pass_rate"] == 1.0
    assert recheck["result"]["plan_matches"] is True


def test_exclusions_section_lists_rules_counts_and_comparisons(case: Case) -> None:
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
        )
    )
    exclusions = _report(case)["exclusions"]
    assert exclusions["status"] == "ok"
    assert exclusions["excluded_observations"] == 8
    assert exclusions["rules"] == [
        {
            "rule_id": "EX-DUP",
            "description": "同一 trace_id 重复导入的记录",
            "registered": True,
            "listed_observations": 8,
            "excluded_observations": 8,
        }
    ]
    assert exclusions["comparisons"] == [selective_comparison()]


def test_limitations_contain_fixed_and_triggered_items(case: Case) -> None:
    case.remove("crosscheck")
    case.metrics(notices=[notice("model_version_changed")])
    limitations = {item["code"]: item["text"] for item in _report(case)["limitations"]}
    assert "哈希只能证明" in limitations["hash_scope"]
    assert "不能证明采集时间真实" in limitations["hash_scope"]
    assert "预检结论" in limitations["precheck_only"]
    assert "合成集 / 模拟环境通过不等于真实平台可用" in limitations["synthetic_data"]
    assert "API 通道" in limitations["api_channel"]
    assert "R-CROSSCHECK" in limitations["checks_not_run"]
    assert "重建基线" in limitations["model_version_changed"]
    assert "operator_is_provider" in limitations


def test_disclosure_is_shown_verbatim_with_operator_warning(case: Case) -> None:
    disclosure = _report(case)["disclosure"]
    original = case.project["disclosure"]
    for key in ("statement", "relationships", "tool_operator", "operator_is_provider"):
        assert disclosure[key] == original[key]
    assert "工具操作方与服务方为同一主体" in disclosure["operator_warning"]


def test_no_operator_warning_when_operator_is_independent(tmp_path: Path) -> None:
    project = factory.valid_example("project")
    disclosure = copy.deepcopy(project["disclosure"])
    disclosure.update(tool_operator="独立复核机构（虚构）", operator_is_provider=False)
    case = build_case(tmp_path / "proj", project_fields={"disclosure": disclosure})
    report = _report(case)
    assert report["disclosure"]["operator_warning"] is None
    assert "operator_is_provider" not in {item["code"] for item in report["limitations"]}


def test_signature_block_is_left_blank(case: Case) -> None:
    signatures = _report(case)["signatures"]
    assert [item["role"] for item in signatures] == ["委托方", "服务方", "复核人"]
    assert signatures[0]["party"] == "澜序汽车（虚构委托方）"
    assert signatures[1]["party"] == "示例服务方（虚构）"
    for item in signatures:
        assert (item["name"], item["signature"], item["date"]) == (None, None, None)


def test_inputs_list_every_file_read_with_its_hash(case: Case) -> None:
    report = _report(case)
    paths = [item["path"] for item in report["inputs"]]
    assert paths == sorted(paths)
    assert set(paths) == {
        "audit.jsonl",
        "entities.jsonl",
        "observations.jsonl",
        "out/alerts.json",
        "out/compare.json",
        "out/crosscheck.json",
        "out/disputes.json",
        "out/evidence-verify.json",
        "out/metrics.json",
        "project.json",
        "prompts/sealed/v0001.json",
        "recheck/plan.json",
        "recheck/result.json",
    }
    for item in report["inputs"]:
        assert item["sha256"] == hashlib.sha256((case.root / item["path"]).read_bytes()).hexdigest()


def test_missing_files_are_not_listed_as_inputs(case: Case) -> None:
    case.remove("alerts")
    paths = {item["path"] for item in _report(case)["inputs"]}
    assert "out/alerts.json" not in paths


def test_without_now_there_is_no_generation_time(case: Case) -> None:
    code, data = case.report("--formats", "json", now=None)
    assert code == 0
    assert data["generated_at"] is None
    assert case.read_report()["generated_at"] is None


def test_report_json_is_byte_identical_across_directories(tmp_path: Path) -> None:
    first = build_case(tmp_path / "a" / "proj")
    second = build_case(tmp_path / "elsewhere" / "deeper" / "proj")
    for current in (first, second):
        assert current.report("--formats", "json")[0] == 0
    one = (first.root / "out" / "report.json").read_bytes()
    assert one == (second.root / "out" / "report.json").read_bytes()
    assert str(tmp_path).encode() not in one
    # 重复生成也逐字节相同（报告本身不是自己的输入）
    assert first.report("--formats", "json")[0] == 0
    assert (first.root / "out" / "report.json").read_bytes() == one


def test_out_dir_option(case: Case, tmp_path: Path) -> None:
    target = tmp_path / "exported"
    code, data = case.report("--formats", "json", "--out-dir", str(target))
    assert code == 0
    assert (target / "report.json").is_file()
    assert not (case.root / "out" / "report.json").exists()
    assert data["outputs"] == {"json": str(target / "report.json")}


@pytest.mark.parametrize("formats", ["", "json,docx", "pdf", "json,,html"])
def test_unknown_formats_exit_2(case: Case, formats: str) -> None:
    code, data = case.report("--formats", formats)
    assert code == 2
    assert data["error"] == "input_error"
    assert not (case.root / "out" / "report.json").exists()


def test_invalid_now_exits_2(case: Case) -> None:
    code, _ = case.report(now="2026-10-20 08:00")
    assert code == 2


def _break(case: Case, name: str, mutate: Any) -> None:
    path = case.path(name)
    data = json.loads(path.read_text(encoding="utf-8"))
    result = mutate(data)
    write_json(path, data if result is None else result)


STRUCTURE_ERRORS = [
    ("metrics", lambda d: []),
    ("metrics", lambda d: d.update(kind="crosscheck")),
    ("metrics", lambda d: d.update(results={})),
    ("metrics", lambda d: d["results"][0].pop("value")),
    ("metrics", lambda d: d["results"][0].update(role="unknown")),
    ("metrics", lambda d: d.update(notices=[{"severity": "warning"}])),
    ("metrics", lambda d: d.update(exclusion_audit={"selective": "no"})),
    ("metrics", lambda d: d.update(project_id="other-project")),
    ("crosscheck", lambda d: d.update(status="ok")),
    ("crosscheck", lambda d: d.update(results=None)),
    ("crosscheck", lambda d: d.update(formula_note=None)),
    ("compare", lambda d: d.update(status="improved")),
    ("alerts", lambda d: d.update(alerts={})),
    ("alerts", lambda d: d.update(leads=[{"text": 3}])),
    ("evidence", lambda d: d.pop("status")),
    ("evidence", lambda d: d.update(problems="none")),
    ("disputes", lambda d: d.update(items=[1])),
    ("disputes", lambda d: d.update(unresolved="1")),
    ("plan", lambda d: d.update(query_ids="Q003")),
    ("plan", lambda d: d.update(fraction="0.2")),
    ("result", lambda d: d.update(pass_rate="1")),
    ("result", lambda d: d.update(checked=None)),
]


@pytest.mark.parametrize(("name", "mutate"), STRUCTURE_ERRORS)
def test_malformed_result_files_exit_2(case: Case, name: str, mutate: Any) -> None:
    _break(case, name, mutate)
    code, data = case.report()
    assert code == 2, data
    assert data["error"] == "input_error"
    assert Path(case.path(name)).name in data["message"]
    assert not (case.root / "out" / "report.json").exists()


def test_invalid_json_exits_2(case: Case) -> None:
    case.path("alerts").write_text("{不是 JSON", encoding="utf-8")
    code, data = case.report()
    assert code == 2
    assert "alerts.json" in data["message"]


def test_missing_project_exits_2(tmp_path: Path) -> None:
    code, data = Case(tmp_path / "nowhere", {}, {}).report()
    assert code == 2
    assert data["error"] == "input_error"


def test_unsealed_project_is_reported_as_insufficient_data(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    factory.make_project(root)
    factory.make_entities(root)
    code, data = Case(root, {}, {}).report("--formats", "json")
    assert code == 3
    assert data["verdict"]["code"] == "insufficient_data"
    assert data["project"]["prompt_set"] is None
    assert data["rules"][3]["rule_id"] == "R-PREREG-CHANGED"
    assert data["rules"][3]["status"] == "not_run"


SCENARIOS = {
    "pass": lambda case: None,
    "fail_evidence": lambda case: case.write("evidence", evidence_doc("fail")),
    "fail_crosscheck": lambda case: case.write("crosscheck", crosscheck_doc(case, "fail")),
    "alerts": lambda case: case.write("alerts", alerts_doc("alert")),
    "no_metrics": lambda case: case.remove("metrics"),
    "null_value": lambda case: case.metrics([metric("mention_rate", None, 0, 0, sufficient=False)]),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_json_report_has_no_forbidden_wording(case: Case, scenario: str) -> None:
    SCENARIOS[scenario](case)
    report = _report(case)
    text = "\n".join(strings(report))
    for phrase in FORBIDDEN:
        assert phrase not in text, phrase
