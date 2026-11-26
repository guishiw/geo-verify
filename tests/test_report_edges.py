"""报告的边界情形：分范围标签、缺少覆盖信息、区间说明、只有复核结果、无法解析的时间、版本选择。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import factory
import pytest
from helpers_report import Case, build_case, interval, metric, result_doc, rule

from geo_verify.jsonl import write_json


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return build_case(tmp_path / "proj")


def test_scope_labels_and_channel_fallbacks(case: Case) -> None:
    by_channel = metric("mention_rate", 0.7, 7, 10, channel="manual", ci=interval(0.4, 0.9))
    by_channel["ext"] = {}  # 没有覆盖信息：按范围里的通道判断
    by_window = metric("mention_rate", 0.8, 8, 10, window_id="W1", ci=interval(0.5, 0.95))
    by_window.pop("ext")  # 没有覆盖信息、也不限通道：按项目登记的通道判断
    daily = metric(
        "recommendation_rate_daily",
        0.5,
        5,
        10,
        date="2026-10-08",
        role="post_hoc",
        ci=interval(0.2, 0.8),
    )
    noted = metric("mention_rate", 0.8, 24, 30, ci=interval(0.7, 0.9))
    noted["components"] = {"interval_note": "只有 2 道题，区间仅供参考（测试构造）"}
    case.metrics([noted, by_channel, by_window, daily])
    case.report("--formats", "json")
    items = case.read_report()["metrics"]["items"]
    assert [item["scope_label"] for item in items] == [
        "整体",
        "通道 manual（人工采集通道）",
        "窗口 W1",
        "日期 2026-10-08",
    ]
    assert "只有 2 道题，区间仅供参考（测试构造）" in items[0]["limitations"]
    assert items[1]["coverage"] is None
    assert not any("API 通道" in text for text in items[1]["limitations"])
    assert any("API 通道" in text for text in items[2]["limitations"])  # 项目登记了 api 通道


def test_recheck_result_without_plan_is_a_reservation(case: Case) -> None:
    case.remove("plan")
    case.write("result", result_doc(None))
    code, data = case.report("--formats", "json,html")
    assert code == 0
    item = rule(data, "R-RECHECK")
    assert item["status"] == "warning"
    assert "缺少 recheck/plan.json" in item["basis"]
    assert data["recheck"]["plan"] is None
    assert data["recheck"]["result"]["plan_matches"] is None
    html = (case.root / "out" / "report.html").read_text(encoding="utf-8")
    assert "没有复核计划文件" in html


def test_recheck_result_with_errors_or_without_checked_items(case: Case) -> None:
    case.write_recheck(checked=0, agreed=0)
    result = json.loads(case.path("result").read_text(encoding="utf-8"))
    result["errors"] = [
        {"line": 3, "code": "invalid_value", "column": "recheck_rank", "message": "测试构造"}
    ]
    write_json(case.path("result"), result)
    code, data = case.report("--formats", "json")
    assert code == 0
    problems = rule(data, "R-RECHECK")["details"]["problems"]
    assert "没有已复核的项，一致率不可判定" in problems
    assert "复核文件有 1 处错误未计入" in problems
    assert data["recheck"]["result"]["errors"] == 1


def test_unparseable_start_times_do_not_break_the_report(tmp_path: Path) -> None:
    def mutate(observations: list[dict[str, Any]]) -> None:
        for item in observations:
            item["started_at"] = "昨天下午"

    case = build_case(tmp_path / "proj", mutate_observations=mutate)
    code, data = case.report("--formats", "json,csv")
    assert code == 0
    assert (data["project"]["period"]["date_from"], data["project"]["period"]["date_to"]) == (
        None,
        None,
    )
    assert data["observation_index"]["preview"][0]["local_date"] is None


def test_prompt_set_follows_the_metrics_file(tmp_path: Path) -> None:
    case = build_case(tmp_path / "proj")
    newer = factory.seal_prompts(
        case.root,
        case.project,
        [*case.prompt_set["prompts"], factory.make_prompt("Q011")],
        previous=case.prompt_set,
    )
    code, data = case.report("--formats", "json")
    assert code == 0
    # metrics.json 依据第 1 版：报告也用第 1 版，题量 10
    assert data["project"]["prompt_set"]["version"] == 1
    assert data["project"]["prompt_set"]["total_queries"] == 10
    case.remove("metrics")
    code, data = case.report("--formats", "json")
    assert data["project"]["prompt_set"]["content_hash"] == newer["content_hash"]
    # 复核计划依据第 1 版：题目文字仍取自第 1 版
    assert data["recheck"]["plan"]["queries"][0]["text"].startswith("第 3 题")
    assert rule(data, "R-INPUT-CONSISTENCY")["status"] == "warning"


def test_plan_query_texts_without_any_sealed_prompt_set(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    factory.make_project(root)
    factory.make_entities(root)
    case = Case(root, {}, {"content_hash": "e" * 64})
    case.write(
        "plan",
        {
            "schema_version": "1.0",
            "kind": "recheck_plan",
            "project_id": "auto-demo",
            "fraction": 0.2,
            "seed": 1,
            "preregistered": True,
            "query_ids": ["Q001"],
            "items": [],
        },
    )
    code, data = case.report("--formats", "json,html")
    assert code == 3
    assert data["recheck"]["plan"]["queries"] == [{"query_id": "Q001", "text": None}]
    assert rule(data, "R-INPUT-CONSISTENCY")["status"] == "pass"


def test_metrics_without_exclusion_audit(case: Case) -> None:
    case.metrics()
    doc = json.loads(case.path("metrics").read_text(encoding="utf-8"))
    doc.pop("exclusion_audit")
    write_json(case.path("metrics"), doc)
    code, data = case.report("--formats", "json")
    # 指标结果在、剔除审计不在：无法确认剔除是否合规，列为保留意见
    assert code == 0
    assert data["verdict"]["code"] == "conditional_pass"
    for rule_id in ("R-EXCLUSION-UNREGISTERED", "R-EXCLUSION-SELECTIVE"):
        item = rule(data, rule_id)
        assert item["status"] == "warning"
        assert "exclusion_audit" in item["basis"]
    reserved = [item["rule_id"] for item in data["verdict"]["reservations"]]
    assert reserved == ["R-EXCLUSION-UNREGISTERED", "R-EXCLUSION-SELECTIVE"]
    assert data["exclusions"] == {"status": "not_run"}
