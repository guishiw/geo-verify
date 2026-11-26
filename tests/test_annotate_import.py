"""`annotate import`：批量导入一名标注者的人工标注（CSV / JSONL），逐行报错，不自动覆盖其他标注者。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from factory import make_citation, make_observation, write_records
from helpers_annotate import NOW, annotation_project
from helpers_basic import sealed_project
from helpers_collect import write_csv

from geo_verify import audit, project
from geo_verify.annotate import run_rules
from geo_verify.canon import sha256_file

LATER = "2026-10-09T03:00:00Z"
LATEST = "2026-10-10T03:00:00Z"
FIELDS = [
    "observation_id",
    "entity_id",
    "mentioned",
    "recommended",
    "negated",
    "rankable",
    "rank",
    "rank_list_size",
    "sentiment",
    "risk.flags",
    "risk.unfounded_negative",
    "risk.identified",
    "risk.confirmed",
    "risk.closed",
    "citation_checks",
    "note",
]
REQUIRED = [*FIELDS[:7], "sentiment"]
ANSWERS = ["推荐澜序 S7。", "不推荐岑野 K5。", "澜序和岑野都可以看看。"]


def row(observation: str, entity: str, **values: str) -> dict[str, str]:
    base = {
        "observation_id": observation,
        "entity_id": entity,
        "mentioned": "true",
        "recommended": "false",
        "negated": "false",
        "rankable": "false",
        "rank": "",
        "sentiment": "neutral",
    }
    base.update(values)
    return base


def do_import(
    root: Path, source: Path, annotator: str = "annotator-a", now: str = LATER
) -> tuple[int, Any]:
    return run_cli_json(
        "annotate",
        "import",
        str(root),
        "--input",
        str(source),
        "--annotator",
        annotator,
        "--now",
        now,
    )


def human_rows(root: Path, annotator: str | None = None) -> list[dict[str, Any]]:
    return [
        item
        for item in project.load_annotations(root)
        if item["annotator"]["type"] == "human" and annotator in (None, item["annotator"]["id"])
    ]


def test_csv_import_writes_human_annotations(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    run_rules.annotate_project(tmp_path, now=NOW)
    rule_lines = (tmp_path / "annotations.jsonl").read_bytes()
    source = write_csv(
        tmp_path / "a.csv",
        FIELDS,
        [
            row(
                "obs-000001",
                "brand-lanxu",
                recommended="true",
                rankable="true",
                rank="1",
                rank_list_size="3",
                sentiment="positive",
            ),
            row("obs-000002", "product-cenye-k5", negated="true", sentiment="negative"),
        ],
    )
    code, data = do_import(tmp_path, source)
    assert code == 0, data
    assert (data["rows"], data["written"], data["rejected_rows"]) == (2, 2, 0)
    assert data["errors"] == []
    content = (tmp_path / "annotations.jsonl").read_bytes()
    assert content.startswith(rule_lines)
    first, second = human_rows(tmp_path)
    assert first["annotator"] == {"type": "human", "id": "annotator-a", "version": "manual"}
    assert first["supersedes"] is None
    assert first["created_at"] == LATER
    assert (first["mentioned"], first["recommended"], first["negated"]) == (True, True, False)
    assert (first["rankable"], first["rank"], first["rank_list_size"]) == (True, 1, 3)
    assert first["sentiment"] == "positive"
    # 未提供的风险字段：未评估（null），未识别（false），未复核（null）
    assert first["risk"] == {
        "flags": [],
        "unfounded_negative": None,
        "identified": False,
        "confirmed": None,
        "closed": None,
    }
    assert "citation_checks" not in first
    assert first["synthetic"] is True
    assert (second["negated"], second["recommended"], second["rank"], second["rank_list_size"]) == (
        True,
        False,
        None,
        None,
    )
    # 人工标注生效（人工优先于规则），规则标注仍在、没有被 supersede
    effective = project.effective_annotations(project.load_annotations(tmp_path))
    assert effective[("obs-000001", "brand-lanxu")] == first
    assert effective[("obs-000001", "product-lanxu-s7")]["annotator"]["type"] == "rule"
    assert all(item["supersedes"] is None for item in project.load_annotations(tmp_path))


def test_cell_values_accept_common_spellings(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    source = write_csv(
        tmp_path / "a.csv",
        FIELDS,
        [
            row(
                "obs-000001",
                "brand-lanxu",
                mentioned="是",
                recommended="TRUE",
                negated="0",
                rankable="否",
                rank="null",
                sentiment="",
                **{
                    "risk.flags": "unfounded_negative|wrong_fact",
                    "risk.unfounded_negative": "1",
                    "risk.identified": "yes",
                    "risk.confirmed": "no",
                    "risk.closed": "",
                    "note": "复核说明",
                },
            )
        ],
    )
    code, data = do_import(tmp_path, source)
    assert code == 0, data
    (item,) = human_rows(tmp_path)
    assert (item["mentioned"], item["recommended"], item["negated"], item["rankable"]) == (
        True,
        True,
        False,
        False,
    )
    assert (item["rank"], item["sentiment"]) == (None, None)
    assert item["risk"] == {
        "flags": ["unfounded_negative", "wrong_fact"],
        "unfounded_negative": True,
        "identified": True,
        "confirmed": False,
        "closed": None,
    }
    assert item["note"] == "复核说明"


def test_row_errors_are_reported_and_valid_rows_written(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    rows = [
        row("obs-000001", "brand-lanxu"),  # 第 2 行：合法
        row("obs-999999", "brand-lanxu"),  # 3
        row("obs-000001", "brand-unknown"),  # 4
        row("obs-000001", "product-lanxu-s7", mentioned="maybe"),  # 5
        row("obs-000001", "product-lanxu-x5", mentioned=""),  # 6
        row("obs-000002", "brand-lanxu", mentioned="false", recommended="true"),  # 7
        row("obs-000002", "product-lanxu-s7", rankable="true", rank="4", rank_list_size="3"),  # 8
        row("obs-000002", "product-lanxu-x5", citation_checks="不是 JSON"),  # 9
        row(
            "obs-000003",
            "brand-lanxu",
            citation_checks='[{"url": "https://unknown.example/x", "valid": true, "relevant": true}]',
        ),  # 10
        row("obs-000003", "brand-cenye", sentiment="happy"),  # 11
        row("obs-000003", "product-cenye-k5", rank="0"),  # 12
    ]
    source = write_csv(tmp_path / "a.csv", FIELDS, rows)
    code, data = do_import(tmp_path, source)
    assert code == 1
    assert (data["rows"], data["written"], data["rejected_rows"]) == (11, 1, 10)
    located = {(item["line"], item["column"]) for item in data["errors"]}
    assert located == {
        (3, "observation_id"),
        (4, "entity_id"),
        (5, "mentioned"),
        (6, "mentioned"),
        (7, None),
        (8, "rank"),
        (9, "citation_checks"),
        (10, "citation_checks"),
        (11, "sentiment"),
        (12, "rank"),
    }
    contract = next(item for item in data["errors"] if item["line"] == 7)
    assert "契约" in contract["reason"]
    (written,) = human_rows(tmp_path)
    assert (written["observation_id"], written["entity_id"]) == ("obs-000001", "brand-lanxu")


def test_missing_required_column_is_an_input_error(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    source = write_csv(tmp_path / "a.csv", [name for name in REQUIRED if name != "sentiment"], [])
    code, data = do_import(tmp_path, source)
    assert code == 2
    assert "缺少必需列" in data["message"]
    assert "sentiment" in data["message"]


def test_reimporting_identical_content_writes_nothing(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    rows = [row("obs-000001", "brand-lanxu"), row("obs-000002", "brand-cenye", negated="true")]
    source = write_csv(tmp_path / "a.csv", FIELDS, rows)
    do_import(tmp_path, source)
    before = (tmp_path / "annotations.jsonl").read_bytes()
    code, data = do_import(tmp_path, source, now=LATEST)
    assert code == 0
    assert (data["written"], data["skipped_identical"]) == (0, 2)
    assert data["audit_event_id"] is None
    assert (tmp_path / "annotations.jsonl").read_bytes() == before
    assert len(project.load_audit(tmp_path)) == 1

    rows[1]["negated"] = "false"
    changed = write_csv(tmp_path / "b.csv", FIELDS, rows)
    code, data = do_import(tmp_path, changed, now=LATEST)
    assert (data["written"], data["skipped_identical"]) == (1, 1)
    assert len(human_rows(tmp_path)) == 3


def test_two_annotators_coexist_without_superseding(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    a = write_csv(
        tmp_path / "a.csv", FIELDS, [row("obs-000001", "brand-lanxu", recommended="true")]
    )
    b = write_csv(
        tmp_path / "b.csv", FIELDS, [row("obs-000001", "brand-lanxu", recommended="false")]
    )
    do_import(tmp_path, a, "annotator-a", now=LATER)
    do_import(tmp_path, b, "annotator-b", now=LATEST)
    rows = human_rows(tmp_path)
    assert [item["annotator"]["id"] for item in rows] == ["annotator-a", "annotator-b"]
    assert all(item["supersedes"] is None for item in rows)
    effective = project.effective_annotations(project.load_annotations(tmp_path))
    # 生效规则仍由 project.effective_annotations 决定：同为人工时取较新的一条
    assert effective[("obs-000001", "brand-lanxu")]["annotator"]["id"] == "annotator-b"


def test_jsonl_import_with_nested_risk_and_url_citations(tmp_path: Path) -> None:
    proj, sealed = sealed_project(tmp_path)
    observation = make_observation(
        proj,
        sealed,
        1,
        answer="推荐澜序 S7。",
        citations=[make_citation(1, "lanxu-auto.example"), make_citation(2, "auto-review.example")],
    )
    write_records(tmp_path, observations=[observation])
    good = {
        "observation_id": "obs-000001",
        "entity_id": "brand-lanxu",
        "mentioned": True,
        "recommended": True,
        "negated": False,
        "rankable": False,
        "rank": None,
        "sentiment": "positive",
        "risk": {"unfounded_negative": False, "identified": False},
        "citation_checks": [
            {"url": "https://auto-review.example/page-2", "valid": True, "relevant": False},
            {"order": 1, "valid": True, "relevant": True},
        ],
        "note": "复核",
    }
    missing = {key: value for key, value in good.items() if key != "sentiment"}
    lines = [
        json.dumps(good, ensure_ascii=False),
        "{不是 JSON",
        "[1, 2]",
        json.dumps({**good, "entity_id": "product-lanxu-s7", "colour": "red"}),
        json.dumps({**missing, "entity_id": "product-lanxu-x5"}),
        json.dumps(
            {
                **good,
                "entity_id": "brand-cenye",
                "citation_checks": [{"order": 3, "valid": True, "relevant": True}],
            }
        ),
        json.dumps({**good, "entity_id": "product-cenye-k5", "risk": "high"}),
        json.dumps({**good, "entity_id": "product-cenye-s7", "mentioned": "true"}),
    ]
    source = tmp_path / "a.jsonl"
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, data = do_import(tmp_path, source)
    assert code == 1
    assert data["format"] == "jsonl"
    assert {(item["line"], item["column"]) for item in data["errors"]} == {
        (2, None),
        (3, None),
        (4, "colour"),
        (5, "sentiment"),
        (6, "citation_checks"),
        (7, "risk"),
        (8, "mentioned"),
    }
    (item,) = human_rows(tmp_path)
    assert item["citation_checks"] == [
        {"order": 1, "valid": True, "relevant": True},
        {"order": 2, "valid": True, "relevant": False},
    ]
    assert item["risk"] == {
        "flags": [],
        "unfounded_negative": False,
        "identified": False,
        "confirmed": None,
        "closed": None,
    }
    assert item["note"] == "复核"


def test_duplicates_inside_one_file(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    rows = [
        row("obs-000001", "brand-lanxu"),
        row("obs-000001", "brand-lanxu"),
        row("obs-000002", "brand-lanxu"),
        row("obs-000002", "brand-lanxu", recommended="true"),
    ]
    source = write_csv(tmp_path / "a.csv", FIELDS, rows)
    code, data = do_import(tmp_path, source)
    assert code == 1
    assert (data["written"], data["skipped_duplicate_in_input"], data["rejected_rows"]) == (2, 1, 1)
    (error,) = data["errors"]
    assert error["line"] == 5
    assert "重复" in error["reason"]


def test_import_writes_an_audit_event(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    source = write_csv(tmp_path / "a.csv", FIELDS, [row("obs-000001", "brand-lanxu")])
    code, data = do_import(tmp_path, source)
    assert code == 0
    (event,) = project.load_audit(tmp_path)
    assert event["event_id"] == data["audit_event_id"]
    assert event["action"] == "annotate"
    assert event["actor"] == "annotator-a"
    assert event["target"] == {"type": "annotations", "id": "a.csv"}
    assert event["before_hash"] is None
    assert event["after_hash"] == sha256_file(tmp_path / "annotations.jsonl")
    assert event["details"]["source_sha256"] == sha256_file(source)
    assert event["details"]["written"] == 1
    assert event["details"]["annotator"] == {
        "type": "human",
        "id": "annotator-a",
        "version": "manual",
    }
    assert audit.verify_chain(project.load_audit(tmp_path)) == []


def test_csv_template(tmp_path: Path) -> None:
    code, data = run_cli_json("annotate", "import", "--template", "csv")
    assert code == 0
    header = data["content"].splitlines()[0]
    assert header.split(",") == FIELDS
    assert [item["name"] for item in data["columns"]] == FIELDS
    target = tmp_path / "template.csv"
    code, _ = run_cli_json("annotate", "import", "--template", "csv", "--out", str(target))
    assert code == 0
    assert target.read_bytes().startswith(chr(0xFEFF).encode("utf-8"))
    code, data = run_cli_json("annotate", "import", "--template", "csv", "--out", str(target))
    assert code == 2
    assert "已存在" in data["message"]


def test_jsonl_template_is_one_object_per_line() -> None:
    code, data = run_cli_json("annotate", "import", "--template", "jsonl")
    assert code == 0
    (line,) = data["content"].splitlines()
    sample = json.loads(line)
    assert set(sample) <= {*FIELDS, "risk"}
    assert set(REQUIRED) <= set(sample)


def test_template_example_row_is_importable(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    for fmt in ("csv", "jsonl"):
        target = tmp_path / f"template.{fmt}"
        run_cli_json("annotate", "import", "--template", fmt, "--out", str(target))
        code, data = do_import(tmp_path, target, annotator=f"annotator-{fmt}")
        assert code == 0, data
        assert data["written"] == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["annotate", "import"],
        ["annotate", "import", "PROJECT"],
        ["annotate", "import", "PROJECT", "--input", "a.csv"],
        ["annotate", "import", "PROJECT", "--input", "missing.csv", "--annotator", "x"],
        ["annotate", "import", "PROJECT", "--input", "a.txt", "--annotator", "x"],
    ],
)
def test_import_argument_errors(tmp_path: Path, argv: list[str]) -> None:
    annotation_project(tmp_path, ANSWERS)
    write_csv(tmp_path / "a.csv", FIELDS, [row("obs-000001", "brand-lanxu")])
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    resolved = [str(tmp_path) if item == "PROJECT" else item for item in argv]
    resolved = [
        str(tmp_path / item) if item.startswith(("a.", "missing")) else item for item in resolved
    ]
    code, data = run_cli_json(*resolved)
    assert code == 2
    assert data["error"] == "input_error"


def test_text_output_lists_errors(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    source = write_csv(
        tmp_path / "a.csv",
        FIELDS,
        [row("obs-000001", "brand-lanxu"), row("obs-999999", "brand-lanxu")],
    )
    code, out, _ = run_cli(
        "annotate",
        "import",
        str(tmp_path),
        "--input",
        str(source),
        "--annotator",
        "annotator-a",
        "--now",
        LATER,
    )
    assert code == 1
    assert "写入 1 条" in out
    assert "第 3 行 [observation_id]" in out


def _citation_project(root: Path) -> None:
    proj, sealed = sealed_project(root)
    observation = make_observation(
        proj,
        sealed,
        1,
        answer="推荐澜序 S7。",
        citations=[make_citation(1, "lanxu-auto.example"), make_citation(2, "auto-review.example")],
    )
    write_records(root, observations=[observation])


GOOD_JSON: dict[str, Any] = {
    "observation_id": "obs-000001",
    "entity_id": "brand-lanxu",
    "mentioned": True,
    "recommended": True,
    "negated": False,
    "rankable": False,
    "rank": None,
    "sentiment": "positive",
}


@pytest.mark.parametrize(
    ("change", "column", "fragment"),
    [
        (
            {"citation_checks": {"order": 1, "valid": True, "relevant": True}},
            "citation_checks",
            "数组",
        ),
        (
            {"citation_checks": [{"order": 1, "valid": True, "relevant": True, "x": 1}]},
            "citation_checks",
            "只能含",
        ),
        ({"citation_checks": [{"order": 1, "valid": True}]}, "citation_checks", "relevant"),
        (
            {
                "citation_checks": [
                    {
                        "order": 2,
                        "url": "https://lanxu-auto.example/page-1",
                        "valid": True,
                        "relevant": True,
                    }
                ]
            },
            "citation_checks",
            "不一致",
        ),
        (
            {"citation_checks": [{"order": 1, "valid": "yes", "relevant": True}]},
            "citation_checks",
            "valid",
        ),
        (
            {
                "citation_checks": [
                    {"order": 1, "valid": True, "relevant": True},
                    {"url": "https://lanxu-auto.example/page-1", "valid": False, "relevant": True},
                ]
            },
            "citation_checks",
            "重复",
        ),
        (
            {"citation_checks": [{"order": True, "valid": True, "relevant": True}]},
            "citation_checks",
            "引用",
        ),
        ({"negated": None}, "negated", "null"),
        ({"rank": 0, "rankable": True}, "rank", "正整数"),
        ({"rank": True, "rankable": True}, "rank", "正整数"),
        ({"note": 123}, "note", "文本"),
        ({"risk": {"flags": "wrong_fact"}}, "risk.flags", "风险标记"),
        ({"risk": {"colour": 1}}, "risk.colour", "未知"),
    ],
)
def test_jsonl_value_errors(
    tmp_path: Path, change: dict[str, Any], column: str, fragment: str
) -> None:
    _citation_project(tmp_path)
    source = tmp_path / "a.jsonl"
    source.write_text(
        json.dumps({**GOOD_JSON, **change}, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    code, data = do_import(tmp_path, source)
    assert code == 1
    (error,) = data["errors"]
    assert (error["line"], error["column"]) == (1, column)
    assert fragment in error["reason"]
    assert not (tmp_path / "annotations.jsonl").exists()


def test_jsonl_null_citation_checks_and_flags(tmp_path: Path) -> None:
    _citation_project(tmp_path)
    source = tmp_path / "a.jsonl"
    record = {**GOOD_JSON, "citation_checks": None, "risk": {"flags": ["compliance"]}}
    source.write_text(json.dumps(record) + "\n", encoding="utf-8")
    code, _ = do_import(tmp_path, source)
    assert code == 0
    (item,) = human_rows(tmp_path)
    assert "citation_checks" not in item
    assert item["risk"]["flags"] == ["compliance"]


def test_csv_row_with_too_many_cells(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    source = tmp_path / "a.csv"
    header = ",".join(REQUIRED)
    source.write_text(
        f"{header}\nobs-000001,brand-lanxu,true,false,false,false,,neutral,extra\n",
        encoding="utf-8",
    )
    code, data = do_import(tmp_path, source)
    assert code == 1
    assert data["errors"] == [{"line": 2, "column": None, "reason": "单元格数量多于表头列数"}]


def test_text_output_truncates_long_error_lists(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    rows = [row("obs-999999", f"brand-{index}") for index in range(51)]
    source = write_csv(tmp_path / "a.csv", FIELDS, rows)
    code, out, _ = run_cli(
        "annotate",
        "import",
        str(tmp_path),
        "--input",
        str(source),
        "--annotator",
        "annotator-a",
        "--now",
        LATER,
    )
    assert code == 1
    assert "……共 102 处错误" in out


def test_blank_annotator_version_is_rejected(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    source = write_csv(tmp_path / "a.csv", FIELDS, [row("obs-000001", "brand-lanxu")])
    code, data = run_cli_json(
        "annotate",
        "import",
        str(tmp_path),
        "--input",
        str(source),
        "--annotator",
        "annotator-a",
        "--annotator-version",
        " ",
    )
    assert code == 2
    assert "版本" in data["message"]
