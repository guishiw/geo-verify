"""`geo-verify recheck-compare` 测试：人工复核结果与标注的比对。

期望值全部来自手工算例：每个用例都在注释里写明哪些项一致、哪些项不一致。
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from pathlib import Path
from typing import Any

import factory
import pytest
from conftest import run_cli, run_cli_json
from factory import COMPETITOR, TARGET
from helpers_recheck import (
    Built,
    build_project,
    dispute,
    fill,
    read_csv,
    write_csv,
    write_disputes,
    write_jsonl_rows,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from geo_verify.jsonl import write_json
from geo_verify.report.recheck import FIELDS, diff_fields

Answers = dict[tuple[str, str], tuple[str, str, str]]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "proj"


def _compare(root: Path, source: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    code, data = run_cli_json("recheck-compare", str(root), "--input", str(source), *argv)
    assert isinstance(data, dict)
    return code, data


def _planned(root: Path, *, queries: int = 5, per_query: int = 3) -> tuple[Built, list[str]]:
    """建项目并生成复核计划；返回项目和被抽中题目的观测编号（升序）。"""
    built = build_project(root, queries=queries, per_query=per_query)
    code, _ = run_cli_json("recheck-plan", str(root))
    assert code == 0
    return built, built.observation_ids_of(built.sampled())


def _sheet(root: Path, answers: Answers, name: str = "filled.csv") -> Path:
    """模拟人工在 plan.csv 上填写复核列后另存。"""
    header, rows = read_csv(root / "recheck" / "plan.csv")
    target = root.parent / name
    write_csv(target, header, fill(rows, answers))
    return target


def _view(items: list[dict[str, Any]]) -> list[tuple[str, str, bool, list[dict[str, Any]]]]:
    return [
        (item["observation_id"], item["entity_id"], item["agree"], item["differences"])
        for item in items
    ]


def _codes(data: dict[str, Any]) -> set[str]:
    return {notice["code"] for notice in data["notices"]}


def _hand_example(observation_ids: list[str]) -> Answers:
    """手工算例（原标注：目标品牌 提及/推荐/第 1；竞品 提及/未推荐/第 2）。

    6 个计划项里填了 5 个：3 个一致、2 个不一致，1 个没填。
    """
    first, second, third = observation_ids
    return {
        (first, TARGET): ("true", "true", "1"),  # 一致
        (first, COMPETITOR): ("TRUE", "FALSE", "2"),  # 一致（表格软件改成了大写）
        (second, TARGET): ("否", "否", "无"),  # 不一致：三个字段都不同
        (second, COMPETITOR): ("是", "否", "3"),  # 不一致：位次 2 → 3
        (third, TARGET): ("yes", "1", "1"),  # 一致（1 表示“是”）
        # (third, COMPETITOR) 没有填写
    }


def test_hand_worked_example_from_a_filled_plan_sheet(root: Path) -> None:
    _, observation_ids = _planned(root)
    first, second, third = observation_ids
    source = _sheet(root, _hand_example(observation_ids))

    code, data = _compare(root, source)

    assert code == 0
    assert data["schema_version"] == "1.0"
    assert data["kind"] == "recheck_result"
    assert data["project_id"] == "auto-demo"
    assert data["synthetic"] is True
    plan_bytes = (root / "recheck" / "plan.json").read_bytes()
    assert data["plan_hash"] == hashlib.sha256(plan_bytes).hexdigest()
    assert data["fields"] == ["mentioned", "recommended", "rank"]
    assert data["planned"] == 6
    assert data["checked"] == 5
    assert data["agreed"] == 3
    assert data["disagreed"] == 2
    assert data["not_checked"] == 1
    assert data["pass_rate"] == 0.6
    assert data["null_reason"] is None
    assert _view(data["items"]) == [
        (first, COMPETITOR, True, []),
        (first, TARGET, True, []),
        (second, COMPETITOR, False, [{"field": "rank", "original": 2, "rechecked": 3}]),
        (
            second,
            TARGET,
            False,
            [
                {"field": "mentioned", "original": True, "rechecked": False},
                {"field": "recommended", "original": True, "rechecked": False},
                {"field": "rank", "original": 1, "rechecked": None},
            ],
        ),
        (third, TARGET, True, []),
    ]
    assert {item["source"] for item in data["items"]} == {"sample"}
    assert data["not_checked_items"] == [
        {"observation_id": third, "entity_id": COMPETITOR, "source": "sample"}
    ]
    assert "incomplete_recheck" in _codes(data)
    assert data["unplanned"] == 0
    assert data["unplanned_items"] == []
    assert data["by_source"] == {
        "sample": {
            "planned": 6,
            "checked": 5,
            "agreed": 3,
            "disagreed": 2,
            "not_checked": 1,
            "pass_rate": 0.6,
        },
        "dispute": {
            "planned": 0,
            "checked": 0,
            "agreed": 0,
            "disagreed": 0,
            "not_checked": 0,
            "pass_rate": None,
        },
    }
    assert data["input"] == {
        "format": "csv",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "rows": 6,
        "blank_rows": 1,
        "invalid_rows": 0,
    }
    assert data["errors"] == []
    assert data["outputs"] == {"result": "recheck/result.json"}
    # 结果文件 = 命令输出去掉文件位置
    written = factory.read(root / "recheck" / "result.json")
    assert written == {key: value for key, value in data.items() if key != "outputs"}


def test_each_result_item_names_the_annotation_it_was_compared_with(root: Path) -> None:
    built, observation_ids = _planned(root)
    source = _sheet(root, _hand_example(observation_ids))
    _, data = _compare(root, source)
    expected = {
        (item["observation_id"], item["entity_id"]): item["annotation_id"]
        for item in built.annotations
    }
    for item in data["items"]:
        assert item["annotation_id"] == expected[(item["observation_id"], item["entity_id"])]


def test_jsonl_input_gives_the_same_numbers(root: Path) -> None:
    _, observation_ids = _planned(root)
    first, second, third = observation_ids
    source = root.parent / "review.jsonl"
    write_jsonl_rows(
        source,
        [
            {
                "observation_id": first,
                "entity_id": TARGET,
                "mentioned": True,
                "recommended": True,
                "rank": 1,
            },
            {
                "observation_id": first,
                "entity_id": COMPETITOR,
                "mentioned": True,
                "recommended": False,
                "rank": 2,
            },
            {
                "observation_id": second,
                "entity_id": TARGET,
                "mentioned": False,
                "recommended": None,
                "rank": None,
            },
            {
                "observation_id": second,
                "entity_id": COMPETITOR,
                "recheck_mentioned": "是",
                "recheck_recommended": "否",
                "recheck_rank": "3",
            },
            {
                "observation_id": third,
                "entity_id": TARGET,
                "mentioned": True,
                "recommended": True,
                "rank": 1,
            },
            {"observation_id": third, "entity_id": COMPETITOR},  # 只有编号，视为未复核
        ],
    )

    code, data = _compare(root, source)

    assert code == 0
    assert (data["planned"], data["checked"], data["agreed"], data["disagreed"]) == (6, 5, 3, 2)
    assert data["not_checked"] == 1
    assert data["pass_rate"] == 0.6
    assert data["input"]["format"] == "jsonl"
    assert data["input"]["rows"] == 6
    assert data["input"]["blank_rows"] == 1
    by_key = {(item["observation_id"], item["entity_id"]): item for item in data["items"]}
    # JSON 的 null 表示“未判断推荐”，与原标注的 true 不同
    assert by_key[(second, TARGET)]["differences"] == [
        {"field": "mentioned", "original": True, "rechecked": False},
        {"field": "recommended", "original": True, "rechecked": None},
        {"field": "rank", "original": 1, "rechecked": None},
    ]
    assert by_key[(second, COMPETITOR)]["differences"] == [
        {"field": "rank", "original": 2, "rechecked": 3}
    ]


def test_fields_option_limits_the_comparison(root: Path) -> None:
    _, observation_ids = _planned(root)
    _, second, _ = observation_ids
    source = _sheet(root, _hand_example(observation_ids))

    # 只比对“提及”：竞品位次 2 → 3 的那一项不再算不一致
    code, data = _compare(root, source, "--fields", "mentioned")
    assert code == 0
    assert data["fields"] == ["mentioned"]
    assert (data["checked"], data["agreed"], data["disagreed"]) == (5, 4, 1)
    assert data["pass_rate"] == 0.8
    by_key = {(item["observation_id"], item["entity_id"]): item for item in data["items"]}
    assert by_key[(second, TARGET)]["differences"] == [
        {"field": "mentioned", "original": True, "rechecked": False}
    ]
    assert by_key[(second, COMPETITOR)]["agree"] is True

    # 字段顺序不影响结果，输出按固定顺序
    code, data = _compare(root, source, "--fields", "rank, mentioned")
    assert code == 0
    assert data["fields"] == ["mentioned", "rank"]
    assert (data["checked"], data["agreed"], data["disagreed"]) == (5, 3, 2)
    assert by_key[(second, TARGET)]["agree"] is False
    by_key = {(item["observation_id"], item["entity_id"]): item for item in data["items"]}
    assert by_key[(second, TARGET)]["differences"] == [
        {"field": "mentioned", "original": True, "rechecked": False},
        {"field": "rank", "original": 1, "rechecked": None},
    ]


def test_only_selected_field_columns_are_required(root: Path) -> None:
    _, observation_ids = _planned(root)
    first, _, _ = observation_ids
    source = root.parent / "mentioned-only.csv"
    write_csv(
        source,
        ["observation_id", "entity_id", "recheck_mentioned"],
        [
            {"observation_id": first, "entity_id": TARGET, "recheck_mentioned": "是"},
            {"observation_id": first, "entity_id": COMPETITOR, "recheck_mentioned": "否"},
        ],
    )
    code, data = _compare(root, source, "--fields", "mentioned")
    assert code == 0
    assert (data["checked"], data["agreed"], data["disagreed"], data["not_checked"]) == (2, 1, 1, 4)
    assert data["pass_rate"] == 0.5
    # 默认比对三个字段时，这份文件缺少必需的列
    code, error = _compare(root, source)
    assert code == 2
    assert "recheck_recommended" in error["message"]
    assert "recheck_rank" in error["message"]


def test_illegal_rows_are_reported_line_by_line(root: Path) -> None:
    _, observation_ids = _planned(root)
    first, second, third = observation_ids
    header = [
        "observation_id",
        "entity_id",
        "recheck_mentioned",
        "recheck_recommended",
        "recheck_rank",
    ]

    def row(observation_id: str, entity_id: str, *values: str) -> dict[str, str]:
        return dict(zip(header, (observation_id, entity_id, *values), strict=True))

    source = root.parent / "bad.csv"
    write_csv(
        source,
        header,
        [
            row(first, TARGET, "true", "true", "1"),  # 第 2 行：合法，一致
            row("obs-999999", TARGET, "true", "true", "1"),  # 第 3 行：未知观测
            row(first, "brand-unknown", "true", "false", "无"),  # 第 4 行：未知实体
            row(first, COMPETITOR, "maybe", "false", "2"),  # 第 5 行：提及取值不合法
            row(second, TARGET, "true", "true", "0"),  # 第 6 行：位次必须 ≥ 1
            row(second, COMPETITOR, "true", "", "2"),  # 第 7 行：漏填推荐
            row(first, TARGET, "true", "true", "1"),  # 第 8 行：重复
            row(third, TARGET, "false", "true", "无"),  # 第 9 行：未提及却被推荐，自相矛盾
            row("", TARGET, "true", "true", "1"),  # 第 10 行：缺观测编号
            row(third, COMPETITOR, "true", "false", "abc"),  # 第 11 行：位次不是整数
            row(third, COMPETITOR, "null", "false", "2"),  # 第 12 行：提及不能为空值
            row(second, TARGET, "false", "false", "2"),  # 第 13 行：未提及却有位次
        ],
    )

    code, data = _compare(root, source)

    assert code == 1
    assert [(item["line"], item["code"], item["column"]) for item in data["errors"]] == [
        (3, "unknown_observation", "observation_id"),
        (4, "unknown_entity", "entity_id"),
        (5, "invalid_value", "recheck_mentioned"),
        (6, "invalid_value", "recheck_rank"),
        (7, "incomplete", "recheck_recommended"),
        (8, "duplicate", None),
        (9, "inconsistent", None),
        (10, "missing_id", "observation_id"),
        (11, "invalid_value", "recheck_rank"),
        (12, "invalid_value", "recheck_mentioned"),
        (13, "inconsistent", None),
    ]
    assert all(item["message"].strip() for item in data["errors"])
    assert data["input"]["rows"] == 12
    assert data["input"]["invalid_rows"] == 11
    assert data["input"]["blank_rows"] == 0
    # 合法的那一行照常计入，非法行不计入
    assert (data["planned"], data["checked"], data["agreed"], data["disagreed"]) == (6, 1, 1, 0)
    assert data["not_checked"] == 5
    assert data["pass_rate"] == 1.0
    assert "invalid_rows" in _codes(data)
    assert factory.read(root / "recheck" / "result.json")["errors"] == data["errors"]

    code, out, _ = run_cli("recheck-compare", str(root), "--input", str(source))
    assert code == 1
    assert "第 5 行" in out
    assert "recheck_mentioned" in out
    assert "11 行" in out


def test_one_row_can_carry_several_errors(root: Path) -> None:
    _planned(root)
    source = root.parent / "bad.jsonl"
    write_jsonl_rows(
        source,
        [
            {
                "observation_id": "obs-999999",
                "entity_id": "brand-unknown",
                "mentioned": "maybe",
                "recommended": 3,
                "rank": 2.5,
            },
            {
                "observation_id": 12,
                "entity_id": TARGET,
                "mentioned": True,
                "recommended": True,
                "rank": 1,
            },
            {
                "observation_id": "obs-000001",
                "entity_id": None,
                "mentioned": True,
                "recommended": True,
                "rank": 1,
            },
        ],
    )
    code, data = _compare(root, source)
    assert code == 1
    assert [(item["line"], item["code"], item["column"]) for item in data["errors"]] == [
        (1, "unknown_observation", "observation_id"),
        (1, "unknown_entity", "entity_id"),
        (1, "invalid_value", "mentioned"),
        (1, "invalid_value", "recommended"),
        (1, "invalid_value", "rank"),
        (2, "missing_id", "observation_id"),
        (3, "missing_id", "entity_id"),
    ]
    assert data["input"]["invalid_rows"] == 3
    # 没有任何合法的复核行：一致率无法计算，但退出码仍指向“输入有非法行”
    assert data["checked"] == 0
    assert data["pass_rate"] is None
    assert data["null_reason"]


@pytest.mark.parametrize(
    ("value", "column"),
    [
        ({"mentioned": None}, "mentioned"),
        ({"mentioned": 1}, "mentioned"),
        ({"recommended": "也许"}, "recommended"),
        ({"rank": True}, "rank"),
        ({"rank": 0}, "rank"),
        ({"rank": -2}, "rank"),
        ({"rank": "1.0"}, "rank"),
        ({"rank": [1]}, "rank"),
    ],
)
def test_illegal_json_values_are_rejected(root: Path, value: dict[str, Any], column: str) -> None:
    _, observation_ids = _planned(root)
    record = {
        "observation_id": observation_ids[0],
        "entity_id": TARGET,
        "mentioned": True,
        "recommended": True,
        "rank": 1,
    }
    record.update(value)
    source = root.parent / "one.jsonl"
    write_jsonl_rows(source, [record])
    code, data = _compare(root, source)
    assert code == 1
    assert [(item["code"], item["column"]) for item in data["errors"]] == [
        ("invalid_value", column)
    ]


@pytest.mark.parametrize(
    ("values", "agree"),
    [
        (("Yes", "Y", "1"), True),
        (("是", "是", " 1 "), True),
        (("1", "TRUE", "1"), True),
        (("true", "none", "1"), False),  # 推荐未判断 ≠ 原标注的“推荐”
        (("true", "true", "NULL"), False),
        (("true", "true", "N/A"), False),
        (("no", "n", "无"), False),
        (("0", "0", "none"), False),
        (("否", "FALSE", "null"), False),
    ],
)
def test_accepted_spellings_of_values(
    root: Path, values: tuple[str, str, str], agree: bool
) -> None:
    _, observation_ids = _planned(root)
    source = _sheet(root, {(observation_ids[0], TARGET): values})
    code, data = _compare(root, source)
    assert code == 0
    assert data["errors"] == []
    assert data["checked"] == 1
    assert data["items"][0]["agree"] is agree


def test_no_checked_items_means_null_rate_and_exit_3(root: Path) -> None:
    _planned(root)
    source = _sheet(root, {})  # 复核表原样交回，一项都没填

    code, data = _compare(root, source)

    assert code == 3
    assert (data["planned"], data["checked"], data["agreed"], data["disagreed"]) == (6, 0, 0, 0)
    assert data["not_checked"] == 6
    assert data["pass_rate"] is None
    assert data["null_reason"]
    assert data["items"] == []
    assert data["input"]["blank_rows"] == 6
    assert len(data["not_checked_items"]) == 6
    assert factory.read(root / "recheck" / "result.json")["pass_rate"] is None
    code, out, _ = run_cli("recheck-compare", str(root), "--input", str(source))
    assert code == 3
    assert "无法计算" in out


def test_complete_recheck_has_no_incomplete_notice(root: Path) -> None:
    _, observation_ids = _planned(root)
    answers: Answers = {}
    for observation_id in observation_ids:
        answers[(observation_id, TARGET)] = ("true", "true", "1")
        answers[(observation_id, COMPETITOR)] = ("true", "false", "2")
    code, data = _compare(root, _sheet(root, answers))
    assert code == 0
    assert (data["planned"], data["checked"], data["agreed"], data["not_checked"]) == (6, 6, 6, 0)
    assert data["pass_rate"] == 1.0
    assert data["not_checked_items"] == []
    assert data["notices"] == []


def test_missing_plan_file_gives_null_plan_hash(root: Path) -> None:
    built = build_project(root, queries=5, per_query=1)
    first, second = sorted(item["observation_id"] for item in built.observations)[:2]
    source = root.parent / "review.csv"
    write_csv(
        source,
        ["observation_id", "entity_id", "mentioned", "recommended", "rank"],
        [
            {
                "observation_id": first,
                "entity_id": TARGET,
                "mentioned": "true",
                "recommended": "true",
                "rank": "1",
            },
            {
                "observation_id": first,
                "entity_id": COMPETITOR,
                "mentioned": "true",
                "recommended": "true",
                "rank": "2",
            },
            {
                "observation_id": second,
                "entity_id": TARGET,
                "mentioned": "true",
                "recommended": "true",
                "rank": "1",
            },
        ],
    )

    code, data = _compare(root, source)

    assert code == 0
    assert data["plan_hash"] is None
    assert "no_plan" in _codes(data)
    assert data["planned"] == 0
    assert data["not_checked"] == 0
    # 没有计划时，文件里每个合法的复核行都计入：3 项里 2 项一致（竞品的“推荐”与原标注不同）
    assert (data["checked"], data["agreed"], data["disagreed"]) == (3, 2, 1)
    assert data["pass_rate"] == 2 / 3
    assert {item["source"] for item in data["items"]} == {None}
    assert data["by_source"] == {}
    assert data["unplanned"] == 0
    assert factory.read(root / "recheck" / "result.json")["plan_hash"] is None


def test_rows_outside_the_plan_do_not_enter_the_rate(root: Path) -> None:
    built, observation_ids = _planned(root)
    first = observation_ids[0]
    sampled = set(built.sampled())
    outside = built.observation_ids_of(q for q in built.query_ids if q not in sampled)[0]
    source = root.parent / "extra.csv"
    header = [
        "observation_id",
        "entity_id",
        "recheck_mentioned",
        "recheck_recommended",
        "recheck_rank",
    ]
    write_csv(
        source,
        header,
        [
            dict(zip(header, (first, TARGET, "true", "true", "1"), strict=True)),  # 计划内，一致
            dict(
                zip(header, (first, COMPETITOR, "true", "false", "5"), strict=True)
            ),  # 计划内，不一致
            dict(zip(header, (outside, TARGET, "true", "true", "1"), strict=True)),  # 计划外
            dict(zip(header, (outside, COMPETITOR, "true", "true", "2"), strict=True)),  # 计划外
        ],
    )

    code, data = _compare(root, source)

    assert code == 0
    # 计划外多交的两项不计入：否则一致率会从 1/2 变成 2/4
    assert (data["planned"], data["checked"], data["agreed"], data["disagreed"]) == (6, 2, 1, 1)
    assert data["pass_rate"] == 0.5
    assert data["not_checked"] == 4
    assert data["unplanned"] == 2
    assert _view(data["unplanned_items"]) == [
        (
            outside,
            COMPETITOR,
            False,
            [{"field": "recommended", "original": False, "rechecked": True}],
        ),
        (outside, TARGET, True, []),
    ]
    assert "unplanned_items" in _codes(data)
    assert all(item["observation_id"] == first for item in data["items"])


def test_dispute_items_are_counted_separately_by_source(root: Path) -> None:
    built = build_project(root, queries=5, per_query=3)
    sampled = set(built.sampled())
    observation_ids = built.observation_ids_of(sampled)
    outside = built.observation_ids_of(q for q in built.query_ids if q not in sampled)
    write_disputes(root, [dispute(outside[0], TARGET), dispute(outside[1], COMPETITOR)])
    assert run_cli_json("recheck-plan", str(root))[0] == 0
    answers = _hand_example(observation_ids)  # 抽样项：5 项已复核，3 项一致
    answers[(outside[0], TARGET)] = ("true", "true", "1")  # 争议项：一致
    answers[(outside[1], COMPETITOR)] = ("false", "false", "无")  # 争议项：不一致

    code, data = _compare(root, _sheet(root, answers))

    assert code == 0
    assert (data["planned"], data["checked"], data["agreed"], data["disagreed"]) == (8, 7, 4, 3)
    assert data["not_checked"] == 1
    assert data["pass_rate"] == 4 / 7
    assert data["by_source"]["sample"] == {
        "planned": 6,
        "checked": 5,
        "agreed": 3,
        "disagreed": 2,
        "not_checked": 1,
        "pass_rate": 0.6,
    }
    assert data["by_source"]["dispute"] == {
        "planned": 2,
        "checked": 2,
        "agreed": 1,
        "disagreed": 1,
        "not_checked": 0,
        "pass_rate": 0.5,
    }
    sources = {
        (item["observation_id"], item["entity_id"]): item["source"] for item in data["items"]
    }
    assert sources[(outside[0], TARGET)] == "dispute"
    assert sources[(observation_ids[0], TARGET)] == "sample"


def test_comparison_uses_the_annotation_that_was_effective_when_planned(root: Path) -> None:
    built, observation_ids = _planned(root)
    first = observation_ids[0]
    original = next(
        item
        for item in built.annotations
        if item["observation_id"] == first and item["entity_id"] == TARGET
    )
    source = _sheet(root, {(first, TARGET): ("false", "false", "无")})  # 复核认为未提及
    code, before = _compare(root, source)
    assert code == 0
    assert (before["checked"], before["agreed"], before["disagreed"]) == (1, 0, 1)

    # 复核之后，有人把这条标注改成与复核结论相同
    observation = next(item for item in built.observations if item["observation_id"] == first)
    override = factory.make_annotation(
        observation,
        900,
        entity_id=TARGET,
        mentioned=False,
        recommended=False,
        annotator_type="human",
        annotator_id="复核员（虚构）",
        created_at="2026-10-09T04:00:00Z",
        supersedes=original["annotation_id"],
    )
    factory.write_records(root, annotations=[override])

    # 计划没有变：比对的仍是生成计划时生效的那条标注，一致率不会因为事后改标注而变好
    code, after = _compare(root, source)
    assert code == 0
    assert (after["checked"], after["agreed"], after["disagreed"]) == (1, 0, 1)
    assert after["items"][0]["annotation_id"] == original["annotation_id"]
    assert after["plan_hash"] == before["plan_hash"]
    notice = next(n for n in after["notices"] if n["code"] == "annotations_changed_since_plan")
    assert notice["count"] == 1
    assert "annotations_changed_since_plan" not in _codes(before)

    # 重新生成计划后，计划指向新的生效标注，plan_hash 随之变化
    assert run_cli_json("recheck-plan", str(root))[0] == 0
    code, replanned = _compare(root, source)
    assert code == 0
    assert (replanned["checked"], replanned["agreed"]) == (1, 1)
    assert replanned["items"][0]["annotation_id"] == "ann-000900"
    assert replanned["plan_hash"] != before["plan_hash"]


def test_plan_without_annotation_ids_falls_back_to_current_annotations(root: Path) -> None:
    built, observation_ids = _planned(root)
    first = observation_ids[0]
    plan = factory.read(root / "recheck" / "plan.json")
    minimal = copy.deepcopy(plan)
    minimal["items"] = [
        {key: item[key] for key in ("observation_id", "query_id", "entity_id", "source")}
        for item in plan["items"]
    ]
    write_json(root / "recheck" / "plan.json", minimal)
    source = _sheet(root, {(first, TARGET): ("true", "true", "1")})
    code, data = _compare(root, source)
    assert code == 0
    assert (data["planned"], data["checked"], data["agreed"]) == (6, 1, 1)
    expected = next(
        item["annotation_id"]
        for item in built.annotations
        if item["observation_id"] == first and item["entity_id"] == TARGET
    )
    assert data["items"][0]["annotation_id"] == expected


def test_rows_without_a_comparable_annotation_are_errors(root: Path) -> None:
    built, observation_ids = _planned(root)
    first = observation_ids[0]
    # 计划指向一条不存在的标注（标注文件被截断时会出现）
    plan = factory.read(root / "recheck" / "plan.json")
    for item in plan["items"]:
        if (item["observation_id"], item["entity_id"]) == (first, TARGET):
            item["annotation_id"] = "ann-999999"
    write_json(root / "recheck" / "plan.json", plan)
    # 另有一条有效观测完全没有标注
    bare = factory.make_observation(built.project, built.prompt_set, 900, query_id="Q001")
    factory.write_records(root, observations=[bare])
    source = root.parent / "review.jsonl"
    write_jsonl_rows(
        source,
        [
            {
                "observation_id": first,
                "entity_id": TARGET,
                "mentioned": True,
                "recommended": True,
                "rank": 1,
            },
            {
                "observation_id": "obs-000900",
                "entity_id": TARGET,
                "mentioned": True,
                "recommended": True,
                "rank": 1,
            },
        ],
    )
    code, data = _compare(root, source)
    assert code == 1
    assert [(item["line"], item["code"]) for item in data["errors"]] == [
        (1, "no_annotation"),
        (2, "no_annotation"),
    ]
    assert data["checked"] == 0


def test_result_is_reproducible_and_independent_of_row_order(tmp_path: Path) -> None:
    root = tmp_path / "a" / "proj"
    _, observation_ids = _planned(root)
    answers = _hand_example(observation_ids)
    source = _sheet(root, answers)
    assert _compare(root, source)[0] == 0
    first_bytes = (root / "recheck" / "result.json").read_bytes()
    assert _compare(root, source)[0] == 0
    assert (root / "recheck" / "result.json").read_bytes() == first_bytes
    assert str(tmp_path).encode() not in first_bytes

    # 另一个目录里的同一份数据、同一份复核表
    other = tmp_path / "b" / "deeper" / "proj"
    _planned(other)
    copied = other.parent / "filled.csv"
    copied.write_bytes(source.read_bytes())
    assert _compare(other, copied)[0] == 0
    assert (other / "recheck" / "result.json").read_bytes() == first_bytes

    # 复核表的行序打乱：除输入文件哈希外，结果完全相同
    header, rows = read_csv(source)
    random.Random(3).shuffle(rows)
    shuffled = root.parent / "shuffled.csv"
    write_csv(shuffled, header, rows)
    assert shuffled.read_bytes() != source.read_bytes()
    code, data = _compare(root, shuffled)
    assert code == 0
    baseline = json.loads(first_bytes)
    assert {k: v for k, v in data.items() if k not in ("input", "outputs")} == {
        k: v for k, v in baseline.items() if k != "input"
    }


def test_text_output_reports_counts_rate_and_unfinished_items(root: Path) -> None:
    _, observation_ids = _planned(root)
    source = _sheet(root, _hand_example(observation_ids))
    code, out, _ = run_cli("recheck-compare", str(root), "--input", str(source))
    assert code == 0
    assert "计划 6 项" in out
    assert "已复核 5 项" in out
    assert "一致 3 项" in out
    assert "不一致 2 项" in out
    assert "未复核 1 项" in out
    assert "0.6000" in out
    assert "3/5" in out
    assert "还有 1 项没有复核" in out
    assert "不判定是否达标" in out
    assert "recheck/result.json" in out
    assert observation_ids[1] in out  # 列出不一致的项


@pytest.mark.parametrize("fields", ["", ",", "mentioned,sentiment", "Mentioned"])
def test_unknown_fields_exit_2(root: Path, fields: str) -> None:
    _, observation_ids = _planned(root)
    source = _sheet(root, _hand_example(observation_ids))
    code, data = _compare(root, source, "--fields", fields)
    assert code == 2
    assert data["error"] == "input_error"
    assert not (root / "recheck" / "result.json").exists()


def test_input_file_problems_exit_2(root: Path) -> None:
    _, observation_ids = _planned(root)
    folder = root.parent

    code, data = _compare(root, folder / "missing.csv")
    assert code == 2
    assert "不存在" in data["message"]

    unknown = folder / "review.txt"
    unknown.write_text("observation_id,entity_id\n", encoding="utf-8")
    code, data = _compare(root, unknown)
    assert code == 2
    assert "--format" in data["message"]

    empty = folder / "empty.csv"
    empty.write_bytes(b"")
    code, data = _compare(root, empty)
    assert code == 2
    assert "空文件" in data["message"]

    no_entity = folder / "no-entity.csv"
    write_csv(no_entity, ["observation_id", "recheck_mentioned"], [])
    code, data = _compare(root, no_entity, "--fields", "mentioned")
    assert code == 2
    assert "entity_id" in data["message"]

    repeated = folder / "repeated.csv"
    write_csv(repeated, ["observation_id", "entity_id", "mentioned", "mentioned"], [])
    code, data = _compare(root, repeated, "--fields", "mentioned")
    assert code == 2
    assert "重复" in data["message"]

    gbk = folder / "gbk.csv"
    gbk.write_bytes("observation_id,entity_id,mentioned\nobs-000001,brand-lanxu,是\n".encode("gbk"))
    code, data = _compare(root, gbk, "--fields", "mentioned")
    assert code == 2
    assert "UTF-8" in data["message"]

    huge = folder / "huge.csv"
    huge.write_text(
        "observation_id,entity_id,mentioned\n" + "x" * (17 * 1024 * 1024) + ",brand-lanxu,是\n",
        encoding="utf-8",
    )
    code, data = _compare(root, huge, "--fields", "mentioned")
    assert code == 2
    assert "CSV" in data["message"]

    assert not (root / "recheck" / "result.json").exists()
    # 用 --format 指明格式后，扩展名不再重要
    as_text = folder / "review-as-text.txt"
    as_text.write_bytes(_sheet(root, _hand_example(observation_ids)).read_bytes())
    code, data = _compare(root, as_text, "--format", "csv")
    assert code == 0
    assert data["checked"] == 5


def test_csv_row_with_too_many_cells_is_reported(root: Path) -> None:
    _, observation_ids = _planned(root)
    source = root.parent / "ragged.csv"
    source.write_text(
        "observation_id,entity_id,mentioned\n"
        f"{observation_ids[0]},{TARGET},true\n"
        "\n"
        f"{observation_ids[1]},{TARGET},true,多出来的单元格\n"
        f"{observation_ids[2]},{TARGET}\n",
        encoding="utf-8",
    )
    code, data = _compare(root, source, "--fields", "mentioned")
    assert code == 1
    assert [(item["line"], item["code"]) for item in data["errors"]] == [(4, "malformed_row")]
    # 第 5 行缺少复核列的单元格，视为未填写
    assert data["input"] == {
        "format": "csv",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "rows": 3,
        "blank_rows": 1,
        "invalid_rows": 1,
    }
    assert data["checked"] == 1


@pytest.mark.parametrize(
    "content",
    [
        [],
        {"kind": "recheck_result", "items": []},
        {"kind": "recheck_plan"},
        {"kind": "recheck_plan", "items": [{"observation_id": "obs-000001"}]},
        {"kind": "recheck_plan", "items": ["obs-000001"]},
    ],
)
def test_malformed_plan_file_exits_2(root: Path, content: Any) -> None:
    _, observation_ids = _planned(root)
    source = _sheet(root, _hand_example(observation_ids))
    write_json(root / "recheck" / "plan.json", content)
    code, data = _compare(root, source)
    assert code == 2
    assert "plan.json" in data["message"]
    assert not (root / "recheck" / "result.json").exists()


_VALUES = st.fixed_dictionaries(
    {
        "mentioned": st.booleans(),
        "recommended": st.sampled_from([True, False, None]),
        "rank": st.sampled_from([None, 1, 2, 3]),
    }
)


@settings(max_examples=200, deadline=None)
@given(
    original=_VALUES,
    rechecked=_VALUES,
    fields=st.lists(st.sampled_from(FIELDS), unique=True, min_size=1),
)
def test_diff_fields_reports_exactly_the_fields_that_differ(
    original: dict[str, Any], rechecked: dict[str, Any], fields: list[str]
) -> None:
    differences = diff_fields(original, rechecked, fields)
    expected = [
        name
        for name in ("mentioned", "recommended", "rank")
        if name in fields
        and original[name] is not rechecked[name]
        and original[name] != rechecked[name]
    ]
    assert [item["field"] for item in differences] == expected
    for item in differences:
        assert item["original"] is original[item["field"]]
        assert item["rechecked"] is rechecked[item["field"]]


def test_diff_fields_distinguishes_false_from_null_and_true_from_one() -> None:
    original = {"mentioned": True, "recommended": False, "rank": 1}
    assert diff_fields(original, {"mentioned": True, "recommended": None, "rank": 1}, FIELDS) == [
        {"field": "recommended", "original": False, "rechecked": None}
    ]
    assert diff_fields(original, dict(original), FIELDS) == []


@pytest.fixture(scope="module")
def shared_project(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str]]:
    root = tmp_path_factory.mktemp("recheck-property") / "proj"
    _, observation_ids = _planned(root, queries=5, per_query=4)
    return root, observation_ids


_ANSWER = st.sampled_from(
    [
        None,  # 不填
        ("true", "true", "1"),  # 与目标品牌的原标注一致
        ("true", "false", "2"),  # 与竞品的原标注一致
        ("false", "false", "无"),
        ("true", "true", "3"),
    ]
)


@settings(max_examples=25, deadline=None)
@given(answers=st.lists(_ANSWER, min_size=8, max_size=8))
def test_counts_always_add_up(
    shared_project: tuple[Path, list[str]], answers: list[tuple[str, str, str] | None]
) -> None:
    root, observation_ids = shared_project
    keys = [(oid, entity) for oid in observation_ids for entity in (TARGET, COMPETITOR)]
    truth = {TARGET: ("true", "true", "1"), COMPETITOR: ("true", "false", "2")}
    filled: Answers = {}
    agreed = 0
    for key, answer in zip(keys, answers, strict=True):
        if answer is None:
            continue
        filled[key] = answer
        agreed += answer == truth[key[1]]
    code, data = _compare(root, _sheet(root, filled, name="property.csv"))
    assert data["planned"] == 8
    assert data["checked"] == len(filled)
    assert data["agreed"] == agreed
    assert data["disagreed"] == len(filled) - agreed
    assert data["not_checked"] == 8 - len(filled)
    assert len(data["items"]) == data["checked"]
    assert sum(item["agree"] for item in data["items"]) == data["agreed"]
    if filled:
        assert code == 0
        assert data["pass_rate"] == agreed / len(filled)
    else:
        assert code == 3
        assert data["pass_rate"] is None
