"""`geo-verify metrics` 命令测试：输出文件、可复现性、下钻、参数与错误处理。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from factory import make_citation, read
from helpers_metrics import Builder, copy_project, find, metrics_file, utc
from hypothesis import given, settings
from hypothesis import strategies as st
from test_metrics import (
    build_basic,
    build_citations,
    build_cross_platform,
    build_judgments,
    build_risk,
)

from geo_verify import metrics, spec
from geo_verify.canon import hash_obj
from geo_verify.jsonl import append_jsonl, write_json

METRIC_TABLE = [
    "mention_rate",
    "intent_coverage",
    "top1_rate",
    "top3_rate",
    "top5_rate",
    "recommendation_rate",
    "recommendation_rate_daily",
    "target_source_presence",
    "target_source_share",
    "citation_validity",
    "authority_share",
    "accuracy_verifiable",
    "accuracy_strict",
    "unverifiable_rate",
    "core_fact_presence",
    "negative_miscitation_rate",
    "sentiment_positive_share",
    "sentiment_neutral_share",
    "sentiment_negative_share",
    "temporal_std",
    "temporal_range",
    "temporal_cv",
    "cross_platform_core_facts",
    "cross_platform_sources",
    "cross_platform_recommendation",
    "valid_observation_rate",
    "environment_consistency_rate",
    "raw_record_completeness",
    "evidence_completeness",
    "recheck_pass_rate",
    "risk_identification_rate",
    "risk_closure_rate",
]


def build_rich(root: Path) -> Builder:
    """同时带引用、判定、多平台、多窗口、无效与被剔除观测的综合项目（用于复现性与下钻测试）。"""
    b = Builder(root, queries=tuple(f"Q{i:03d}" for i in range(1, 9)))
    domains = ["lanxu-auto.example", "gov-data.example", "other-media.example"]
    number = 0
    for query_index in range(8):
        for platform_index, platform in enumerate(("engine-a", "engine-b")):
            for window_index, window in enumerate(("W1", "W2", "W3")):
                for attempt in range(2):
                    number += 1
                    # 各题的提及比例差别明显（Q001 很低，Q008 全部提及），题间差异进入区间
                    slot = attempt + 2 * window_index + 6 * platform_index
                    mentioned = (slot * 5 + query_index) % 8 <= query_index
                    rankable = mentioned and number % 2 == 0
                    citations = [
                        make_citation(order, domains[(number + order) % 3])
                        for order in range(1, 1 + number % 3)
                    ]
                    status = "invalid" if number % 17 == 0 else "valid"
                    observation = b.obs(
                        f"Q{query_index + 1:03d}",
                        platform,
                        window,
                        mentioned=mentioned,
                        ann={
                            "rankable": rankable,
                            "rank": 1 + number % 5 if rankable else None,
                            "sentiment": ("positive", "neutral", "negative")[number % 3]
                            if mentioned
                            else None,
                        },
                        status=status,
                        attempt=attempt + 1,
                        citations=citations,
                        started_at=utc(8 + window_index, 10 + attempt * 7),
                    )
                    if status == "valid" and number % 4 == 0:
                        b.judge(
                            observation, ("supported", "contradicted", "unverifiable")[number % 3]
                        )
                        b.judge(observation, ("present_correct", "missing")[number % 2])
    b.write()
    b.exclude([b.observations[4], b.observations[20]])
    return b


def test_metrics_writes_document_with_all_metric_ids(tmp_path: Path) -> None:
    b = build_rich(tmp_path / "proj")
    code, stdout_data = run_cli_json("metrics", str(b.root))
    assert code == 0
    data = metrics_file(b.root)
    assert data["schema_version"] == "1.0"
    assert data["kind"] == "metrics"
    assert data["project_id"] == "auto-demo"
    assert data["synthetic"] is True
    assert data["prompt_set"] == {"version": 1, "content_hash": b.sealed["content_hash"]}
    assert data["parameters"] == {"seed": 20261001, "iterations": 1000, "confidence_level": 0.95}
    assert stdout_data["results"] == data["results"]
    assert stdout_data["notices"] == data["notices"]
    # docs/cli.md 指标编号表里的每个编号都有结果，且顺序与表一致
    seen: list[str] = []
    for result in data["results"]:
        if result["metric_id"] not in seen:
            seen.append(result["metric_id"])
    assert seen == METRIC_TABLE
    assert list(metrics.METRIC_IDS) == METRIC_TABLE
    for result in data["results"]:
        assert spec.iter_issues("metric_result", result) == []
    # 每个平台都有一条提及率结果
    for platform in ("engine-a", "engine-b"):
        assert find(data["results"], "mention_rate", platform=platform)["denominator"] > 0


def test_cli_table_lists_every_documented_metric_id() -> None:
    """METRIC_TABLE 与 docs/cli.md 的指标编号表逐项一致（文档是接口）。"""
    text = (Path(__file__).resolve().parent.parent / "docs" / "cli.md").read_text(encoding="utf-8")
    table = text.split("## 指标编号")[1].split("## 命令")[0]
    documented: list[str] = []
    for line in table.splitlines():
        if line.startswith("| `"):
            documented.extend(part for part in line.split("|")[1].replace("`", " ").split())
    assert documented == METRIC_TABLE


def test_output_has_no_timestamp_or_absolute_path(tmp_path: Path) -> None:
    b = build_rich(tmp_path / "proj")
    run_cli("metrics", str(b.root))
    text = (b.root / "out" / "metrics.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "generated_at" not in text
    assert text.endswith("\n")
    assert json.loads(text) == metrics_file(b.root)


def test_two_independent_directories_give_identical_bytes(tmp_path: Path) -> None:
    first = build_rich(tmp_path / "party-a" / "proj").root
    second = copy_project(first, tmp_path / "party-b" / "somewhere-else")
    assert run_cli("metrics", str(first))[0] == 0
    assert run_cli("metrics", str(second))[0] == 0
    first_bytes = (first / "out" / "metrics.json").read_bytes()
    assert first_bytes == (second / "out" / "metrics.json").read_bytes()
    # 再算一次仍然一致
    assert run_cli("metrics", str(first))[0] == 0
    assert (first / "out" / "metrics.json").read_bytes() == first_bytes


def test_record_order_does_not_change_results(tmp_path: Path) -> None:
    source = build_rich(tmp_path / "ordered")
    run_cli("metrics", str(source.root))
    shuffled = Builder(tmp_path / "shuffled", queries=tuple(f"Q{i:03d}" for i in range(1, 9)))
    append_jsonl(shuffled.root / "observations.jsonl", reversed(source.observations))
    append_jsonl(shuffled.root / "annotations.jsonl", reversed(source.annotations))
    append_jsonl(shuffled.root / "judgments.jsonl", reversed(source.judgments))
    (shuffled.root / "audit.jsonl").write_bytes((source.root / "audit.jsonl").read_bytes())
    run_cli("metrics", str(shuffled.root))
    assert (shuffled.root / "out" / "metrics.json").read_bytes() == (
        source.root / "out" / "metrics.json"
    ).read_bytes()


def test_seed_and_iterations_can_be_overridden_and_are_noticed(tmp_path: Path) -> None:
    b = build_rich(tmp_path / "proj")
    run_cli("metrics", str(b.root))
    default = metrics_file(b.root)
    custom_path = tmp_path / "custom.json"
    code, _ = run_cli_json(
        "metrics", str(b.root), "--seed", "7", "--iterations", "300", "--out", str(custom_path)
    )
    assert code == 0
    custom = read(custom_path)
    assert custom["parameters"] == {"seed": 7, "iterations": 300, "confidence_level": 0.95}
    notice = next(n for n in custom["notices"] if n["code"] == "parameters_overridden")
    assert notice["details"] == {
        "registered": {"seed": 20261001, "iterations": 1000},
        "used": {"seed": 7, "iterations": 300},
    }
    assert not any(n["code"] == "parameters_overridden" for n in default["notices"])
    a = find(default["results"], "mention_rate")
    c = find(custom["results"], "mention_rate")
    assert (a["numerator"], a["denominator"]) == (c["numerator"], c["denominator"])
    assert a["interval"] != c["interval"]
    # 默认输出文件没有被 --out 的那次运行改动
    assert metrics_file(b.root) == default


def test_drilldown_matches_committed_hashes_for_every_result(tmp_path: Path) -> None:
    b = build_rich(tmp_path / "proj")
    run_cli("metrics", str(b.root))
    results = metrics_file(b.root)["results"]
    checked = 0
    for result in results:
        scope = result["scope"]
        argv = ["metrics", str(b.root), "--drilldown", result["metric_id"]]
        for option, key in (
            ("--platform", "platform"),
            ("--channel", "channel"),
            ("--window", "window_id"),
            ("--date", "date"),
        ):
            if scope[key] is not None:
                argv += [option, scope[key]]
        code, drill = run_cli_json(*argv)
        assert code == 0, (result["metric_id"], scope, drill)
        commitment = result["drilldown"]
        assert drill["unit"] == commitment["unit"]
        for side in ("numerator", "denominator"):
            ids = drill[f"{side}_ids"]
            expected_hash = commitment[f"{side}_ids_hash"]
            if expected_hash is None:
                assert ids is None
                continue
            assert ids == sorted(ids)
            # 承诺的哈希 = 排序后编号列表的规范化 JSON 的 SHA-256
            assert hash_obj(ids) == expected_hash
            assert drill[f"{side}_ids_hash"] == expected_hash
            if result[side] is not None and commitment["unit"] != "window":
                assert len(ids) == result[side]
            checked += 1
        assert drill["result"] == result
    assert checked > 100


def test_drilldown_lists_records_with_local_dates(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001", started_at=utc(8, 15, 59, 59))
    b.obs("Q001", started_at=utc(8, 16, 0, 0), mentioned=False)
    b.write()
    code, drill = run_cli_json(
        "metrics", str(b.root), "--drilldown", "recommendation_rate_daily", "--date", "2026-10-09"
    )
    assert code == 0
    assert drill["denominator_ids"] == ["obs-000002"]
    assert drill["numerator_ids"] == []
    assert drill["records"] == [
        {
            "id": "obs-000002",
            "observation_id": "obs-000002",
            "query_id": "Q001",
            "platform": "engine-a",
            "channel": "api",
            "window_id": "W1",
            "date": "2026-10-09",
            "started_at": utc(8, 16, 0, 0),
            "in_numerator": False,
        }
    ]
    code, out, _ = run_cli("metrics", str(b.root), "--drilldown", "mention_rate", "--window", "W1")
    assert code == 0
    assert "obs-000001" in out
    assert "分母 2" in out
    assert not (b.root / "out" / "metrics.json").exists()


def test_large_id_lists_are_committed_by_hash_only(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    for index in range(metrics.INLINE_ID_LIMIT + 1):
        b.obs(f"Q00{index % 3 + 1}", window=f"W{index % 3 + 1}")
    b.write()
    run_cli("metrics", str(b.root))
    results = metrics_file(b.root)["results"]
    overall = find(results, "mention_rate")
    assert "denominator_ids" not in overall["drilldown"]
    assert "numerator_ids" not in overall["drilldown"]
    code, drill = run_cli_json("metrics", str(b.root), "--drilldown", "mention_rate")
    assert code == 0
    assert len(drill["denominator_ids"]) == metrics.INLINE_ID_LIMIT + 1
    assert hash_obj(drill["denominator_ids"]) == overall["drilldown"]["denominator_ids_hash"]
    # 小范围仍然内联
    assert "denominator_ids" in find(results, "mention_rate", window_id="W1")["drilldown"]


id_text = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x2FFF, exclude_characters='"\\'),
    min_size=1,
    max_size=12,
)


@settings(max_examples=80, deadline=None)
@given(
    ids=st.lists(
        st.one_of(st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._:|#-]{0,20}", fullmatch=True), id_text),
        max_size=30,
    )
)
def test_ids_hash_equals_canonical_json_hash(ids: list[str]) -> None:
    """编号列表哈希走了快速路径，必须与 RFC 8785 规范化 JSON 的哈希逐位相同。"""
    assert metrics.ids_hash(sorted(ids)) == hash_obj(sorted(ids))


def test_ids_hash_handles_escapes_like_canonical_json() -> None:
    tricky = ['a"b', "back\\slash", "tab\there", "newline\nhere", "ctrl" + chr(1), "中文", "é"]
    assert metrics.ids_hash(sorted(tricky)) == hash_obj(sorted(tricky))
    assert metrics.ids_hash([]) == hash_obj([])


def test_text_output_summarizes_results(tmp_path: Path) -> None:
    b = build_basic(tmp_path / "proj")
    code, out, _ = run_cli("metrics", str(b.root))
    assert code == 0
    assert "mention_rate" in out
    assert "5/9" in out
    assert "事后指标" in out
    assert "out/metrics.json" in out.replace("\\", "/")


@pytest.mark.parametrize(
    "builder", [build_basic, build_citations, build_judgments, build_cross_platform, build_risk]
)
def test_drilldown_agrees_for_small_fixtures(tmp_path: Path, builder: Any) -> None:
    b = builder(tmp_path / "proj")
    run_cli("metrics", str(b.root))
    for result in metrics_file(b.root)["results"]:
        commitment = result["drilldown"]
        for side in ("numerator", "denominator"):
            if f"{side}_ids" in commitment:
                assert hash_obj(commitment[f"{side}_ids"]) == commitment[f"{side}_ids_hash"]


# ---------------------------------------------------------------- 错误处理


def test_input_errors(tmp_path: Path) -> None:
    code, data = run_cli_json("metrics", str(tmp_path / "missing"))
    assert code == 2
    assert "项目目录不存在" in data["message"]

    b = build_basic(tmp_path / "proj")
    code, data = run_cli_json("metrics", str(b.root), "--drilldown", "no_such_metric")
    assert code == 2
    assert "未知指标" in data["message"]
    code, data = run_cli_json("metrics", str(b.root), "--platform", "engine-a")
    assert code == 2
    assert "--drilldown" in data["message"]
    code, data = run_cli_json(
        "metrics", str(b.root), "--drilldown", "cross_platform_sources", "--platform", "engine-a"
    )
    assert code == 2
    assert "不适用于单一平台" in data["message"]
    code, data = run_cli_json("metrics", str(b.root), "--iterations", "0")
    assert code == 2
    code, data = run_cli_json("metrics", str(b.root), "--prompt-set-version", "9")
    assert code == 2
    code, data = run_cli_json("metrics", str(b.root), "--drilldown", "mention_rate", "--date", "x")
    assert code == 2
    assert "日期" in data["message"]


def test_unsealed_project_and_missing_target_entity(tmp_path: Path) -> None:
    from factory import make_entities, make_project

    root = tmp_path / "unsealed"
    make_project(root)
    make_entities(root)
    code, data = run_cli_json("metrics", str(root))
    assert code == 2
    assert "尚未封存" in data["message"]

    b = Builder(tmp_path / "proj", target_entity_id="brand-unknown")
    code, data = run_cli_json("metrics", str(b.root))
    assert code == 2
    assert "brand-unknown" in data["message"]


def test_malformed_observation_is_an_input_error_with_line_number(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001")
    broken = b.obs("Q002")
    del broken["status"]
    b.write()
    code, data = run_cli_json("metrics", str(b.root))
    assert code == 2
    assert "第 2 行" in data["message"]
    assert "status" in data["message"]

    bad_time = Builder(tmp_path / "time")
    bad_time.obs("Q001", started_at="2026-10-08 10:00")
    bad_time.write()
    code, data = run_cli_json("metrics", str(bad_time.root))
    assert code == 2
    assert "第 1 行" in data["message"]

    bad_citations = Builder(tmp_path / "cit")
    wrong = bad_citations.obs("Q001")
    wrong["citations"] = "not-a-list"
    bad_citations.write()
    code, data = run_cli_json("metrics", str(bad_citations.root))
    assert code == 2
    assert "第 1 行" in data["message"]


def test_judgments_and_annotations_for_unknown_observations_are_noticed(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    observation = b.obs("Q001")
    ghost = dict(observation)
    ghost["observation_id"] = "obs-ghost"
    b.judge(ghost, "supported")
    b.judge(observation, "supported")
    b.annotate(ghost, mentioned=True)
    duplicate = dict(b.judgments[-1])
    b.judgments.append(duplicate)
    draft = dict(b.judgments[0])
    draft.update(judgment_id="jdg-draft", source={"type": "draft", "id": "draft-1"})
    b.judgments.append(draft)
    b.write()
    code, _ = run_cli_json("metrics", str(b.root))
    assert code == 0
    data = metrics_file(b.root)
    strict = find(data["results"], "accuracy_strict")
    assert (strict["numerator"], strict["denominator"]) == (1, 1)
    codes = {n["code"]: n for n in data["notices"]}
    assert codes["judgments_without_observation"]["details"]["count"] == 1
    assert codes["duplicate_judgment_ids"]["details"]["count"] == 1
    assert codes["annotations_without_observation"]["details"]["count"] == 1
    assert "judgments_from_drafts" not in codes


def test_unregistered_platform_is_noticed_but_computed(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001", platform="engine-z")
    b.write()
    run_cli("metrics", str(b.root))
    data = metrics_file(b.root)
    assert find(data["results"], "mention_rate", platform="engine-z")["denominator"] == 1
    notice = next(n for n in data["notices"] if n["code"] == "unregistered_platforms")
    assert notice["details"]["platforms"] == ["engine-z"]


def test_broken_recheck_result_is_an_input_error(tmp_path: Path) -> None:
    b = build_basic(tmp_path / "proj")
    write_json(b.root / "recheck" / "result.json", {"items": "oops"})
    code, data = run_cli_json("metrics", str(b.root))
    assert code == 2
    assert "recheck/result.json" in data["message"]


def test_python_api_scope_and_compute(tmp_path: Path) -> None:
    b = build_basic(tmp_path / "proj")
    index = metrics.build_index(b.root)
    params = metrics.parameters_for(index.project)
    assert (params.seed, params.iterations, params.level) == (20261001, 1000, 0.95)
    computation = metrics.compute(
        index, "mention_rate", metrics.Scope(platform="engine-a", window_id="W1"), params
    )
    # engine-a 的 W1：o1 提及、o4 提及、o9 未提及
    assert (computation.result["numerator"], computation.result["denominator"]) == (2, 3)
    assert computation.clusters == {"Q001": (1, 1), "Q002": (1, 1), "Q003": (0, 1)}
    assert computation.denominator_ids == ["obs-000001", "obs-000004", "obs-000009"]
    competitor = metrics.compute(
        index, "mention_rate", metrics.Scope(), params, entity_id="brand-cenye"
    )
    assert competitor.result["denominator"] == 0
    assert competitor.result["scope"]["entity_id"] == "brand-cenye"
    assert math.isclose(
        metrics.compute(index, "mention_rate", metrics.Scope(), params).result["value"], 5 / 9
    )
