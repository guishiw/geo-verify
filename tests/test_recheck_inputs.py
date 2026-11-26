"""抽样复核的输入边界：JSONL 坏行、备注列、计划文件与争议文件的结构错误。

期望值为手工推算：原标注为目标品牌 提及/推荐/第 1，竞品 提及/未推荐/第 2。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import factory
import pytest
from conftest import run_cli, run_cli_json
from factory import COMPETITOR, TARGET
from helpers_recheck import build_project, dispute, write_csv, write_disputes

from geo_verify.jsonl import write_json


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "proj"


def _planned(root: Path) -> list[str]:
    built = build_project(root, queries=5, per_query=3)
    assert run_cli_json("recheck-plan", str(root))[0] == 0
    return built.observation_ids_of(built.sampled())


def _compare(root: Path, source: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    code, data = run_cli_json("recheck-compare", str(root), "--input", str(source), *argv)
    assert isinstance(data, dict)
    return code, data


def test_broken_jsonl_lines_are_reported_and_blank_lines_skipped(root: Path) -> None:
    first, second, _ = _planned(root)
    source = root.parent / "review.jsonl"
    source.write_text(
        "\n".join(
            [
                f'{{"observation_id": "{first}", "entity_id": "{TARGET}", "mentioned": true,'
                ' "recommended": true, "rank": 1, "recheck_note": "  核对无误  "}',
                "",  # 空行：跳过，不计行数
                "{这不是 JSON",
                '["不是对象"]',
                # 空字符串视为未填写；三列都空 → 未复核
                f'{{"observation_id": "{second}", "entity_id": "{TARGET}", "mentioned": " ",'
                ' "recommended": "", "rank": ""}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    code, data = _compare(root, source)
    assert code == 1
    assert [(item["line"], item["code"], item["column"]) for item in data["errors"]] == [
        (3, "malformed_row", None),
        (4, "malformed_row", None),
    ]
    assert data["input"]["rows"] == 4
    assert data["input"]["blank_rows"] == 1
    assert data["input"]["invalid_rows"] == 2
    assert (data["checked"], data["agreed"]) == (1, 1)
    (item,) = data["items"]
    assert item["note"] == "核对无误"
    assert item["original"] == {"mentioned": True, "recommended": True, "rank": 1}
    assert item["rechecked"] == {"mentioned": True, "recommended": True, "rank": 1}


def test_csv_note_column_is_carried_into_the_result(root: Path) -> None:
    first, _, _ = _planned(root)
    source = root.parent / "noted.csv"
    header = ["observation_id", "entity_id", "mentioned", "recheck_note"]
    write_csv(
        source,
        header,
        [
            {
                "observation_id": first,
                "entity_id": COMPETITOR,
                "mentioned": "否",
                "recheck_note": "竞品只出现在引用标题里",
            },
            {"observation_id": first, "entity_id": TARGET, "mentioned": "是", "recheck_note": ""},
        ],
    )
    code, data = _compare(root, source, "--fields", "mentioned")
    assert code == 0
    notes = {item["entity_id"]: item["note"] for item in data["items"]}
    assert notes == {COMPETITOR: "竞品只出现在引用标题里", TARGET: None}
    # 竞品：原标注“提及”，复核为“否” → 不一致；目标品牌一致
    assert (data["checked"], data["agreed"], data["disagreed"]) == (2, 1, 1)


def test_text_output_lists_errors_and_unplanned_items(root: Path) -> None:
    built = build_project(root, queries=5, per_query=3)
    assert run_cli_json("recheck-plan", str(root))[0] == 0
    sampled = set(built.sampled())
    outside = built.observation_ids_of(q for q in built.query_ids if q not in sampled)[0]
    source = root.parent / "mixed.csv"
    write_csv(
        source,
        ["observation_id", "entity_id", "mentioned"],
        [{"observation_id": outside, "entity_id": TARGET, "mentioned": "是"}]
        + [
            {"observation_id": f"obs-9{index:05d}", "entity_id": TARGET, "mentioned": "是"}
            for index in range(55)
        ],
    )
    code, out, _ = run_cli(
        "recheck-compare", str(root), "--input", str(source), "--fields", "mentioned"
    )
    assert code == 1
    assert "一致率无法计算" in out
    assert "另有 1 项不在计划内" in out
    assert "55 行不合法" in out
    assert "仅列出前 50 条" in out


def test_text_output_without_plan_says_so(root: Path) -> None:
    built = build_project(root, queries=5, per_query=1)
    first = sorted(item["observation_id"] for item in built.observations)[0]
    source = root.parent / "review.csv"
    write_csv(
        source,
        ["observation_id", "entity_id", "mentioned"],
        [{"observation_id": first, "entity_id": TARGET, "mentioned": "是"}],
    )
    code, out, _ = run_cli(
        "recheck-compare", str(root), "--input", str(source), "--fields", "mentioned"
    )
    assert code == 0
    assert "没有复核计划文件" in out
    assert "1.0000（1/1）" in out


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda items: items[0].update(source="other"), "source"),
        (lambda items: items[0].update(annotation_id=7), "annotation_id"),
        (lambda items: items.append(dict(items[0])), "重复"),
        (lambda items: items[0].update(entity_id=None), "entity_id"),
    ],
)
def test_plan_item_structure_errors_exit_2(root: Path, mutate: Any, needle: str) -> None:
    first, _, _ = _planned(root)
    plan = factory.read(root / "recheck" / "plan.json")
    mutate(plan["items"])
    write_json(root / "recheck" / "plan.json", plan)
    source = root.parent / "review.csv"
    write_csv(
        source,
        ["observation_id", "entity_id", "mentioned"],
        [{"observation_id": first, "entity_id": TARGET, "mentioned": "是"}],
    )
    code, data = _compare(root, source, "--fields", "mentioned")
    assert code == 2
    assert "plan.json" in data["message"]
    assert needle in data["message"]
    assert not (root / "recheck" / "result.json").exists()


def test_dispute_resolved_flag_must_be_boolean(root: Path) -> None:
    build_project(root, queries=5, per_query=1)
    item = dispute("obs-000001", TARGET)
    item["resolved"] = "yes"
    write_disputes(root, [item])
    code, data = run_cli_json("recheck-plan", str(root))
    assert code == 2
    assert "disputes.json" in data["message"]
    assert "resolved" in data["message"]


def test_dispute_without_resolved_flag_counts_as_unresolved(root: Path) -> None:
    built = build_project(root, queries=5, per_query=1)
    (query,) = built.sampled()
    outside = next(item for item in built.observations if item["query_id"] != query)
    item = dispute(outside["observation_id"], TARGET)
    del item["resolved"]
    write_disputes(root, [item])
    code, data = run_cli_json("recheck-plan", str(root))
    assert code == 0
    assert data["disputes"]["unresolved"] == 1
    assert data["disputes"]["merged"] == 1
    assert data["items"][-1]["source"] == "dispute"


def test_valid_now_is_accepted_and_not_written(root: Path) -> None:
    build_project(root, queries=5, per_query=1)
    code, _ = run_cli_json("recheck-plan", str(root), "--now", "2031-01-02T03:04:05Z")
    assert code == 0
    assert b"2031-01-02" not in (root / "recheck" / "plan.json").read_bytes()
    code, out, _ = run_cli("recheck-plan", str(root))
    assert code == 0
    assert "复核计划与上次相同" in out
