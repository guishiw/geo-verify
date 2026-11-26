"""`geo-verify recheck-plan` 测试：抽样数量、确定性、有效观测口径、争议项并入、CSV 导出。

期望值来自手工推算（题数 × 每题次数 × 实体数）和 helpers_recheck 里的独立参考实现。
"""

from __future__ import annotations

import codecs
import copy
import hashlib
import random
from pathlib import Path
from typing import Any

import factory
import pytest
from conftest import run_cli, run_cli_json
from factory import COMPETITOR, TARGET
from helpers_recheck import (
    REGISTERED_SEED,
    build_project,
    dispute,
    read_csv,
    write_disputes,
)

from geo_verify import audit
from geo_verify.jsonl import write_json

NOW = "2026-10-09T03:00:00Z"
CSV_HEADER = [
    "observation_id",
    "entity_id",
    "entity_name",
    "query_id",
    "platform",
    "source",
    "prompt_text",
    "answer_text",
    "original_mentioned",
    "original_recommended",
    "original_rank",
    "recheck_mentioned",
    "recheck_recommended",
    "recheck_rank",
    "recheck_note",
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "proj"


def _plan(root: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    code, data = run_cli_json("recheck-plan", str(root), *argv)
    assert isinstance(data, dict)
    return code, data


def _pairs(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(item["observation_id"], item["entity_id"]) for item in items]


def _codes(data: dict[str, Any]) -> set[str]:
    return {notice["code"] for notice in data["notices"]}


def test_plan_lists_every_valid_annotated_observation_of_the_sampled_queries(root: Path) -> None:
    built = build_project(root, queries=10, per_query=3)
    sampled = built.sampled()
    assert len(sampled) == 2  # ⌈0.2 × 10⌉

    code, data = _plan(root)

    assert code == 0
    plan = factory.read(root / "recheck" / "plan.json")
    assert plan["schema_version"] == "1.0"
    assert plan["kind"] == "recheck_plan"
    assert plan["project_id"] == "auto-demo"
    assert plan["synthetic"] is True
    assert plan["prompt_set"] == {"version": 1, "content_hash": built.prompt_set["content_hash"]}
    assert plan["fraction"] == 0.2
    assert plan["seed"] == REGISTERED_SEED
    assert plan["preregistered"] is True
    assert plan["total_queries"] == 10
    assert plan["query_ids"] == sampled
    expected = sorted(
        (item["query_id"], item["observation_id"], entity_id)
        for item in built.observations_of(sampled)
        for entity_id in (TARGET, COMPETITOR)
    )
    listed = [
        (item["query_id"], item["observation_id"], item["entity_id"]) for item in plan["items"]
    ]
    assert listed == expected
    assert len(plan["items"]) == 12  # 2 题 × 3 次 × 2 个实体
    assert {item["source"] for item in plan["items"]} == {"sample"}
    assert {item["disputed"] for item in plan["items"]} == {False}
    assert plan["counts"] == {
        "sampled_queries": 2,
        "observations": 6,
        "sample_items": 12,
        "dispute_items": 0,
        "items": 12,
        "unannotated_observations": 0,
    }
    assert plan["disputes"] == {
        "present": False,
        "unresolved": 0,
        "merged": 0,
        "already_in_sample": 0,
        "skipped": [],
    }
    assert plan["notices"] == []
    # 抽样方法写在计划文件里，第三方可据此复算
    assert plan["sampling"]["method"] == "sha256_rank"
    assert "sha256" in plan["sampling"]["rule"]

    # 命令输出 = 计划文件内容 + 文件位置与哈希
    assert {key: data[key] for key in plan} == plan
    assert data["outputs"] == {"plan": "recheck/plan.json", "csv": "recheck/plan.csv"}
    digest = hashlib.sha256((root / "recheck" / "plan.json").read_bytes()).hexdigest()
    assert data["plan_hash"] == digest
    assert data["previous_plan_hash"] is None
    assert data["changed"] is True


def test_each_item_points_to_the_effective_annotation(root: Path) -> None:
    built = build_project(root, queries=5, per_query=1)
    (query,) = built.sampled()
    (observation,) = built.observations_of([query])
    by_entity = {
        item["entity_id"]: item["annotation_id"]
        for item in built.annotations
        if item["observation_id"] == observation["observation_id"]
    }
    _, data = _plan(root)
    assert {item["entity_id"]: item["annotation_id"] for item in data["items"]} == by_entity


@pytest.mark.parametrize(("queries", "expected"), [(50, 10), (51, 11), (3, 1)])
def test_sampled_query_count_is_ceiling_of_twenty_percent(
    root: Path, queries: int, expected: int
) -> None:
    built = build_project(root, queries=queries, per_query=1, entities=(TARGET,))
    code, data = _plan(root)
    assert code == 0
    assert data["total_queries"] == queries
    assert len(data["query_ids"]) == expected
    assert len(set(data["query_ids"])) == expected
    assert data["query_ids"] == built.sampled()
    assert len(data["items"]) == expected  # 每题 1 次 × 1 个实体


def test_only_valid_observations_with_effective_annotations_become_items(root: Path) -> None:
    built = build_project(root, queries=5, per_query=4, write=False)
    (query,) = built.sampled()  # 5 题抽 1 题
    invalid, other_version, excluded, kept = built.observations_of([query])
    invalid.update(status="invalid", invalid_reason="timeout", answer_text=None)
    other_version["prompt_set_hash"] = "0" * 64
    unannotated = factory.make_observation(built.project, built.prompt_set, 900, query_id=query)
    original = next(
        item
        for item in built.annotations
        if item["observation_id"] == kept["observation_id"] and item["entity_id"] == TARGET
    )
    override = factory.make_annotation(
        kept,
        901,
        entity_id=TARGET,
        mentioned=False,
        recommended=False,
        annotator_type="human",
        annotator_id="复核员（虚构）",
        created_at="2026-10-08T04:00:00Z",
        supersedes=original["annotation_id"],
    )
    factory.write_records(
        root,
        observations=[*built.observations, unannotated],
        annotations=[*built.annotations, override],
    )
    audit.append_event(
        root,
        actor="数据管理员（虚构）",
        action="exclude",
        target_type="observation",
        target_id="batch",
        reason="同一 trace_id 重复导入",
        occurred_at=NOW,
        details={"rule_id": "EX-DUP", "count": 1, "observation_ids": [excluded["observation_id"]]},
    )

    code, data = _plan(root)

    assert code == 0
    assert _pairs(data["items"]) == [
        (kept["observation_id"], COMPETITOR),
        (kept["observation_id"], TARGET),
    ]
    annotation_ids = {item["entity_id"]: item["annotation_id"] for item in data["items"]}
    assert annotation_ids[TARGET] == "ann-000901"  # 人工覆盖后的标注才是生效标注
    assert data["counts"]["observations"] == 1
    assert data["counts"]["unannotated_observations"] == 1
    assert "unannotated_observations" in _codes(data)
    _, rows = read_csv(root / "recheck" / "plan.csv")
    target_row = next(row for row in rows if row["entity_id"] == TARGET)
    assert target_row["original_mentioned"] == "false"
    assert target_row["original_recommended"] == "false"
    assert target_row["original_rank"] == ""


def test_duplicate_observation_ids_are_listed_once(root: Path) -> None:
    built = build_project(root, queries=5, per_query=1, write=False)
    (query,) = built.sampled()
    (observation,) = built.observations_of([query])
    duplicate = copy.deepcopy(observation)
    factory.write_records(
        root, observations=[*built.observations, duplicate], annotations=built.annotations
    )
    code, data = _plan(root)
    assert code == 0
    assert _pairs(data["items"]) == [
        (observation["observation_id"], COMPETITOR),
        (observation["observation_id"], TARGET),
    ]


def test_plan_is_byte_identical_across_runs_directories_and_row_order(tmp_path: Path) -> None:
    first = build_project(tmp_path / "a" / "proj", queries=20, per_query=2)
    assert _plan(first.root)[0] == 0
    plan_bytes = (first.root / "recheck" / "plan.json").read_bytes()
    csv_bytes = (first.root / "recheck" / "plan.csv").read_bytes()

    code, again = _plan(first.root)
    assert code == 0
    assert again["changed"] is False
    assert again["previous_plan_hash"] == again["plan_hash"]
    assert (first.root / "recheck" / "plan.json").read_bytes() == plan_bytes
    assert (first.root / "recheck" / "plan.csv").read_bytes() == csv_bytes

    # 换一个目录，并打乱观测、标注文件的行序
    second = build_project(
        tmp_path / "elsewhere" / "deeper" / "proj", queries=20, per_query=2, write=False
    )
    observations = list(second.observations)
    annotations = list(second.annotations)
    random.Random(5).shuffle(observations)
    random.Random(6).shuffle(annotations)
    assert observations != second.observations
    factory.write_records(second.root, observations=observations, annotations=annotations)
    assert _plan(second.root, "--now", NOW)[0] == 0
    assert (second.root / "recheck" / "plan.json").read_bytes() == plan_bytes
    assert (second.root / "recheck" / "plan.csv").read_bytes() == csv_bytes
    assert str(tmp_path).encode() not in plan_bytes
    assert str(tmp_path).encode() not in csv_bytes
    assert b"2026-10-09" not in plan_bytes  # 计划文件不含生成时间


def test_command_line_overrides_are_marked_as_not_preregistered(root: Path) -> None:
    built = build_project(root, queries=50, per_query=1, entities=(TARGET,))
    code, registered = _plan(root)
    assert code == 0
    assert registered["preregistered"] is True

    code, data = _plan(root, "--seed", "7")
    assert code == 0
    assert data["seed"] == 7
    assert data["fraction"] == 0.2
    assert data["preregistered"] is False
    assert "not_preregistered" in _codes(data)
    assert data["query_ids"] == built.sampled(seed=7)
    assert data["query_ids"] != registered["query_ids"]
    assert data["changed"] is True
    assert data["previous_plan_hash"] == registered["plan_hash"]

    code, data = _plan(root, "--fraction", "0.5")
    assert code == 0
    assert data["fraction"] == 0.5
    assert data["seed"] == REGISTERED_SEED
    assert data["preregistered"] is False
    assert len(data["query_ids"]) == 25
    assert data["query_ids"] == built.sampled(1, 2)

    # 显式传入与登记值相同的参数：口径没有被改动
    code, data = _plan(root, "--fraction", "0.2", "--seed", str(REGISTERED_SEED))
    assert code == 0
    assert data["preregistered"] is True
    assert data["query_ids"] == registered["query_ids"]
    assert data["plan_hash"] == registered["plan_hash"]


def test_registration_edited_after_sealing_is_not_preregistered(root: Path) -> None:
    built = build_project(root, queries=10, per_query=1)
    edited = copy.deepcopy(built.project)
    edited["preregistration"]["recheck"]["seed"] = 99
    write_json(root / "project.json", edited)

    code, data = _plan(root)

    assert code == 0
    assert data["seed"] == 99
    assert data["query_ids"] == built.sampled(seed=99)
    assert data["preregistered"] is False
    assert "preregistration_changed" in _codes(data)


def test_unresolved_disputes_are_merged_into_the_plan(root: Path) -> None:
    built = build_project(root, queries=10, per_query=2)
    sampled = built.sampled()
    outside = built.observation_ids_of(q for q in built.query_ids if q not in sampled)
    inside = built.observation_ids_of(sampled)
    second_field = dispute(outside[0], TARGET)
    second_field["field"] = "recommended"
    write_disputes(
        root,
        [
            dispute(outside[0], TARGET),
            second_field,  # 同一（观测，实体）在两个字段上有分歧，只算一个复核项
            dispute(outside[1], COMPETITOR, resolved=True),  # 已裁决，不并入
            dispute(inside[0], TARGET),  # 本来就在抽样范围内
            dispute("obs-999999", TARGET),  # 观测不存在
        ],
    )

    code, data = _plan(root)

    assert code == 0
    sample_items = [item for item in data["items"] if item["source"] == "sample"]
    dispute_items = [item for item in data["items"] if item["source"] == "dispute"]
    assert len(sample_items) == 8  # 2 题 × 2 次 × 2 个实体
    assert _pairs(dispute_items) == [(outside[0], TARGET)]
    assert data["items"][-1] == dispute_items[0]  # 争议项排在抽样项之后
    owner = next(
        item["query_id"] for item in built.observations if item["observation_id"] == outside[0]
    )
    assert dispute_items[0]["query_id"] == owner
    assert owner not in data["query_ids"]
    flagged = sorted(_pairs([item for item in data["items"] if item["disputed"]]))
    assert flagged == sorted([(outside[0], TARGET), (inside[0], TARGET)])
    assert data["disputes"] == {
        "present": True,
        "unresolved": 3,
        "merged": 1,
        "already_in_sample": 1,
        "skipped": [
            {"observation_id": "obs-999999", "entity_id": TARGET, "reason": "unknown_observation"}
        ],
    }
    assert data["counts"]["sample_items"] == 8
    assert data["counts"]["dispute_items"] == 1
    assert data["counts"]["items"] == 9
    assert "disputes_skipped" in _codes(data)
    _, rows = read_csv(root / "recheck" / "plan.csv")
    assert [row["source"] for row in rows] == ["sample"] * 8 + ["dispute"]


def test_disputes_on_unusable_observations_are_listed_not_dropped(root: Path) -> None:
    built = build_project(root, queries=10, per_query=2, write=False)
    sampled = built.sampled()
    others = built.observations_of(q for q in built.query_ids if q not in sampled)
    invalid = others[0]
    invalid.update(status="invalid", invalid_reason="timeout", answer_text=None)
    unannotated = factory.make_observation(
        built.project, built.prompt_set, 900, query_id=others[1]["query_id"]
    )
    off_bank = factory.make_observation(built.project, built.prompt_set, 901, query_id="Q999")
    factory.write_records(
        root,
        observations=[*built.observations, unannotated, off_bank],
        annotations=[*built.annotations, factory.make_annotation(off_bank, 950)],
    )
    write_disputes(
        root,
        [
            dispute(off_bank["observation_id"], TARGET),
            dispute(unannotated["observation_id"], TARGET),
            dispute(invalid["observation_id"], TARGET),
        ],
    )

    code, data = _plan(root)

    assert code == 0
    assert [item for item in data["items"] if item["source"] == "dispute"] == []
    assert data["disputes"]["merged"] == 0
    assert data["disputes"]["skipped"] == sorted(
        [
            {
                "observation_id": invalid["observation_id"],
                "entity_id": TARGET,
                "reason": "not_valid_observation",
            },
            {
                "observation_id": "obs-000900",
                "entity_id": TARGET,
                "reason": "no_effective_annotation",
            },
            {
                "observation_id": "obs-000901",
                "entity_id": TARGET,
                "reason": "not_valid_observation",
            },
        ],
        key=lambda item: item["observation_id"],
    )


@pytest.mark.parametrize(
    "content",
    [
        [],
        {"kind": "disputes"},
        {"kind": "disputes", "items": "none"},
        {"kind": "disputes", "items": ["obs-000001"]},
        {"kind": "disputes", "items": [{"observation_id": "obs-000001"}]},
        {"kind": "disputes", "items": [{"observation_id": 1, "entity_id": TARGET}]},
    ],
)
def test_malformed_disputes_file_is_an_input_error(root: Path, content: Any) -> None:
    build_project(root, queries=5, per_query=1)
    write_json(root / "out" / "disputes.json", content)
    code, data = _plan(root)
    assert code == 2
    assert data["error"] == "input_error"
    assert "disputes.json" in data["message"]
    assert not (root / "recheck" / "plan.json").exists()


def test_plan_csv_has_bom_guards_formulas_and_leaves_recheck_columns_blank(root: Path) -> None:
    built = build_project(root, queries=5, per_query=4, write=False)
    (query,) = built.sampled()
    first, second, third, fourth = built.observations_of([query])
    tab, carriage_return = chr(9), chr(13)
    first["answer_text"] = '=HYPERLINK("http://evil.example","点我")'
    second["answer_text"] = "+1 澜序 S7"
    third["answer_text"] = "-1 岑野 K5"
    fourth["answer_text"] = "@SUM(A1:A9)"
    first["prompt_text"] = tab + "制表符开头的题目"
    second["prompt_text"] = carriage_return + "回车开头的题目"
    factory.write_records(root, observations=built.observations, annotations=built.annotations)

    assert _plan(root)[0] == 0

    raw = (root / "recheck" / "plan.csv").read_bytes()
    assert raw.startswith(codecs.BOM_UTF8)
    header, rows = read_csv(root / "recheck" / "plan.csv")
    assert header == CSV_HEADER
    assert len(rows) == 8  # 1 题 × 4 次 × 2 个实体
    target = {row["observation_id"]: row for row in rows if row["entity_id"] == TARGET}
    assert target[first["observation_id"]]["answer_text"] == "'" + first["answer_text"]
    assert target[second["observation_id"]]["answer_text"] == "'+1 澜序 S7"
    assert target[third["observation_id"]]["answer_text"] == "'-1 岑野 K5"
    assert target[fourth["observation_id"]]["answer_text"] == "'@SUM(A1:A9)"
    assert target[first["observation_id"]]["prompt_text"] == "'" + tab + "制表符开头的题目"
    assert (
        target[second["observation_id"]]["prompt_text"] == "'" + carriage_return + "回车开头的题目"
    )
    assert target[third["observation_id"]]["prompt_text"] == third["prompt_text"]
    for row in rows:
        assert row["query_id"] == query
        assert row["platform"] == "engine-a"
        assert row["source"] == "sample"
        assert row["recheck_mentioned"] == ""
        assert row["recheck_recommended"] == ""
        assert row["recheck_rank"] == ""
        assert row["recheck_note"] == ""
        if row["entity_id"] == TARGET:
            assert row["entity_name"] == "澜序"
            assert (row["original_mentioned"], row["original_recommended"]) == ("true", "true")
            assert row["original_rank"] == "1"
        else:
            assert row["entity_name"] == "岑野"
            assert (row["original_mentioned"], row["original_recommended"]) == ("true", "false")
            assert row["original_rank"] == "2"
    # CSV 的行与计划文件的 items 一一对应、顺序一致
    plan = factory.read(root / "recheck" / "plan.json")
    assert [(row["observation_id"], row["entity_id"]) for row in rows] == _pairs(plan["items"])


def test_empty_plan_is_written_and_reported_as_insufficient_data(root: Path) -> None:
    built = build_project(root, queries=5, per_query=1, write=False)
    code, data = _plan(root)
    assert code == 3
    assert data["items"] == []
    assert data["query_ids"] == built.sampled()
    assert "empty_plan" in _codes(data)
    assert factory.read(root / "recheck" / "plan.json")["items"] == []
    header, rows = read_csv(root / "recheck" / "plan.csv")
    assert header == CSV_HEADER
    assert rows == []


def test_sampled_queries_without_reviewable_observations_are_reported(root: Path) -> None:
    built = build_project(root, queries=10, per_query=1, write=False)
    first, second = built.sampled()
    kept = [item for item in built.observations if item["query_id"] != second]
    factory.write_records(root, observations=kept, annotations=built.annotations)
    code, data = _plan(root)
    assert code == 0
    notice = next(n for n in data["notices"] if n["code"] == "sampled_queries_without_items")
    assert notice["query_ids"] == [second]
    assert {item["query_id"] for item in data["items"]} == {first}


def test_text_output_summarises_the_plan(root: Path) -> None:
    build_project(root, queries=10, per_query=3)
    code, out, _ = run_cli("recheck-plan", str(root))
    assert code == 0
    assert "10 道题" in out
    assert "抽取 2 道" in out
    assert "12 项" in out
    assert "recheck/plan.json" in out
    assert "recheck/plan.csv" in out
    assert "预先登记" in out
    code, out, _ = run_cli("recheck-plan", str(root), "--seed", "3")
    assert code == 0
    assert "未采用预先登记" in out
    assert "已被替换" in out


def test_selects_requested_prompt_set_version(root: Path) -> None:
    built = build_project(root, queries=5, per_query=1)
    prompts = [*built.prompt_set["prompts"], factory.make_prompt("Q006")]
    second = factory.seal_prompts(root, built.project, prompts, previous=built.prompt_set)
    # 最新版本（v2）下还没有任何观测
    code, data = _plan(root)
    assert code == 3
    assert data["prompt_set"] == {"version": 2, "content_hash": second["content_hash"]}
    assert data["total_queries"] == 6
    code, data = _plan(root, "--prompt-set-version", "1")
    assert code == 0
    assert data["prompt_set"]["version"] == 1
    assert data["total_queries"] == 5
    assert len(data["items"]) == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--fraction", "0"],
        ["--fraction", "1.2"],
        ["--fraction", "nan"],
        ["--fraction", "abc"],
        ["--seed", "-1"],
        ["--seed", "1.5"],
        ["--now", "2026-10-09 03:00:00"],
        ["--prompt-set-version", "9"],
    ],
)
def test_invalid_arguments_exit_2(root: Path, argv: list[str]) -> None:
    build_project(root, queries=5, per_query=1)
    code, _, _ = run_cli("recheck-plan", str(root), *argv)
    assert code == 2
    assert not (root / "recheck" / "plan.json").exists()


def test_missing_project_or_unsealed_prompts_exit_2(tmp_path: Path) -> None:
    code, data = _plan(tmp_path / "nowhere")
    assert code == 2
    assert data["error"] == "input_error"
    unsealed = tmp_path / "unsealed"
    factory.make_project(unsealed)
    factory.make_entities(unsealed)
    code, data = _plan(unsealed)
    assert code == 2
    assert "尚未封存" in data["message"]
