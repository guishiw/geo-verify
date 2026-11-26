"""`annotate rules` 端到端：经 CLI 运行，断言 annotations.jsonl 与审计事件。

S11（别名、全半角、否定句、竞品同名）的期望值来自人工阅读回答原文。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import rfc8785
from conftest import run_cli, run_cli_json
from factory import make_annotation, make_observation, read, write_records
from helpers_annotate import NOW, annotation_project
from helpers_basic import auto_entities, entity, write_entities

from geo_verify import audit, project
from geo_verify.annotate.records import annotated_entity_ids
from geo_verify.canon import sha256_file
from geo_verify.jsonl import append_jsonl, read_jsonl

LANXU = "brand-lanxu"
S7 = "product-lanxu-s7"
X5 = "product-lanxu-x5"
CENYE = "brand-cenye"
K5 = "product-cenye-k5"
CENYE_S7 = "product-cenye-s7"
DEFAULT_ENTITIES = [LANXU, S7, X5, CENYE, K5, CENYE_S7]

S11_ANSWERS: list[str | None] = [
    "澜序汽车旗下的 S7 值得考虑。",  # 别名：品牌别名 + 产品简称
    "推荐ＬＡＮＸＵ　Ｓ７。",  # 全角字母数字与全角空格
    "不推荐澜序 S7，建议看看其他车。",  # 否定句
    "推荐岑野 S7，续航更长。",  # 竞品同名车型
    None,  # 超时的无效观测
]


def run_rules(root: Path, *extra: str) -> tuple[int, Any]:
    return run_cli_json("annotate", "rules", str(root), "--now", NOW, *extra)


def by_key(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = read_jsonl(root / "annotations.jsonl")
    keyed = {(row["observation_id"], row["entity_id"]): row for row in rows}
    assert len(keyed) == len(rows)
    return keyed


def triple(row: dict[str, Any]) -> tuple[bool, bool | None, bool]:
    return row["mentioned"], row["recommended"], row["negated"]


def test_s11_alias_width_negation_and_same_name(tmp_path: Path) -> None:
    annotation_project(tmp_path, S11_ANSWERS)
    code, data = run_rules(tmp_path)
    assert code == 0
    assert data["entity_ids"] == DEFAULT_ENTITIES
    assert data["observations_total"] == 5
    assert data["observations_eligible"] == 4
    assert data["skipped_invalid"] == 1
    assert data["written"] == 4 * 6
    rows = by_key(tmp_path)
    assert {key[0] for key in rows} == {"obs-000001", "obs-000002", "obs-000003", "obs-000004"}

    # 1. 别名：“澜序汽车”是品牌别名，“S7”是目标车型
    assert triple(rows[("obs-000001", LANXU)]) == (True, True, False)
    assert triple(rows[("obs-000001", S7)]) == (True, True, False)
    assert triple(rows[("obs-000001", X5)]) == (False, False, False)
    assert triple(rows[("obs-000001", CENYE)]) == (False, False, False)
    # 澜(0)序(1)汽(2)车(3)旗(4)下(5)的(6) (7)S(8)7(9)
    assert {"kind": "mention", "start": 8, "end": 10} in rows[("obs-000001", S7)]["spans"]
    assert {"kind": "mention", "start": 0, "end": 4} in rows[("obs-000001", LANXU)]["spans"]

    # 2. 全角：推(0)荐(1)ＬＡＮＸＵ(2-6)　(7)Ｓ７(8-9)
    assert triple(rows[("obs-000002", LANXU)]) == (True, True, False)
    assert triple(rows[("obs-000002", S7)]) == (True, True, False)
    assert {"kind": "mention", "start": 8, "end": 10} in rows[("obs-000002", S7)]["spans"]

    # 3. 否定句：不推荐 ≠ 推荐
    assert triple(rows[("obs-000003", S7)]) == (True, False, True)
    assert triple(rows[("obs-000003", LANXU)]) == (True, False, True)
    assert rows[("obs-000003", S7)]["sentiment"] == "negative"
    assert rows[("obs-000003", S7)]["risk"]["unfounded_negative"] is None

    # 4. 竞品同名：“岑野 S7”整体归竞品，其中的“S7”不算目标车型
    assert triple(rows[("obs-000004", S7)]) == (False, False, False)
    assert triple(rows[("obs-000004", LANXU)]) == (False, False, False)
    assert rows[("obs-000004", S7)]["spans"] == []
    assert triple(rows[("obs-000004", CENYE_S7)]) == (True, True, False)
    assert triple(rows[("obs-000004", CENYE)]) == (True, True, False)

    # 这四条回答都没有推荐序列
    assert all(row["rankable"] is False and row["rank"] is None for row in rows.values())


def test_rule_annotations_follow_the_contract(tmp_path: Path) -> None:
    annotation_project(tmp_path, S11_ANSWERS)
    run_rules(tmp_path)
    code, out, _ = run_cli("validate", str(tmp_path / "annotations.jsonl"), "--type", "annotation")
    assert code == 0, out
    for row in read_jsonl(tmp_path / "annotations.jsonl"):
        assert row["annotator"] == {"type": "rule", "id": "geo-verify-rules", "version": "1.0"}
        assert row["supersedes"] is None
        assert row["created_at"] == NOW
        assert row["synthetic"] is True
        assert row["project_id"] == "auto-demo"
        assert row["risk"]["flags"] == []
        assert row["risk"]["identified"] is False
        assert row["risk"]["confirmed"] is None
        assert row["risk"]["closed"] is None
        assert "citation_checks" not in row
        assert isinstance(row["ext"]["rule_codes"], list)


def test_annotation_ids_are_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    for root in (first, second):
        annotation_project(root, S11_ANSWERS)
        run_rules(root)
    assert (first / "annotations.jsonl").read_bytes() == (second / "annotations.jsonl").read_bytes()
    rows = read_jsonl(first / "annotations.jsonl")
    ids = [row["annotation_id"] for row in rows]
    assert len(set(ids)) == len(ids)
    for row in rows:
        assert re.fullmatch(r"a-[0-9a-f]{24}", row["annotation_id"])
        # 独立重算：a- + sha256(RFC 8785 规范化的身份信息) 前 24 位
        identity = {
            "project_id": row["project_id"],
            "observation_id": row["observation_id"],
            "entity_id": row["entity_id"],
            "annotator": {"type": "rule", "id": "geo-verify-rules", "version": "1.0"},
        }
        digest = hashlib.sha256(rfc8785.dumps(identity)).hexdigest()
        assert row["annotation_id"] == "a-" + digest[:24]


def test_rerun_does_not_duplicate(tmp_path: Path) -> None:
    annotation_project(tmp_path, S11_ANSWERS)
    run_rules(tmp_path)
    before = (tmp_path / "annotations.jsonl").read_bytes()
    code, data = run_cli_json("annotate", "rules", str(tmp_path), "--now", "2026-10-09T03:00:00Z")
    assert code == 0
    assert data["written"] == 0
    assert data["skipped_existing"] == 24
    assert data["audit_event_id"] is None
    assert (tmp_path / "annotations.jsonl").read_bytes() == before
    assert [event["action"] for event in project.load_audit(tmp_path)] == ["annotate"]


def test_existing_effective_annotation_is_not_regenerated(tmp_path: Path) -> None:
    _, observations = annotation_project(tmp_path, S11_ANSWERS)
    human = make_annotation(
        observations[0],
        1,
        entity_id=LANXU,
        mentioned=True,
        recommended=False,
        annotator_type="human",
        annotator_id="reviewer-01",
    )
    write_records(tmp_path, annotations=[human])
    prior = sha256_file(tmp_path / "annotations.jsonl")
    code, data = run_rules(tmp_path)
    assert code == 0
    assert data["written"] == 23
    assert data["skipped_existing"] == 1
    rows = read_jsonl(tmp_path / "annotations.jsonl")
    assert rows[0] == human
    assert [
        row for row in rows if (row["observation_id"], row["entity_id"]) == ("obs-000001", LANXU)
    ] == [human]
    effective = project.effective_annotations(rows)
    assert effective[("obs-000001", LANXU)] == human
    assert effective[("obs-000001", S7)]["annotator"]["type"] == "rule"
    (event,) = project.load_audit(tmp_path)
    assert event["before_hash"] == prior


def test_audit_event_records_the_change(tmp_path: Path) -> None:
    annotation_project(tmp_path, S11_ANSWERS)
    _, data = run_rules(tmp_path)
    events = project.load_audit(tmp_path)
    assert len(events) == 1
    event = events[0]
    assert event["event_id"] == data["audit_event_id"]
    assert event["action"] == "annotate"
    assert event["occurred_at"] == NOW
    assert event["before_hash"] is None
    assert event["after_hash"] == sha256_file(tmp_path / "annotations.jsonl")
    assert event["details"]["written"] == 24
    assert event["details"]["annotator"]["id"] == "geo-verify-rules"
    assert event["synthetic"] is True
    assert audit.verify_chain(events) == []


def test_annotated_entities_follow_project_registration(tmp_path: Path) -> None:
    answer = "推荐朔风 T6。"
    default_root, listed_root = tmp_path / "default", tmp_path / "listed"
    annotation_project(default_root, [answer])
    _, data = run_rules(default_root)
    # 朔风未登记为竞品：即使被提及也不标注
    assert data["entity_ids"] == DEFAULT_ENTITIES
    assert not any(key[1].endswith("shuofeng") or "t6" in key[1] for key in by_key(default_root))

    annotation_project(
        listed_root, [answer], competitor_entity_ids=["brand-cenye", "brand-shuofeng"]
    )
    _, data = run_rules(listed_root)
    assert data["entity_ids"] == [*DEFAULT_ENTITIES, "brand-shuofeng", "product-shuofeng-t6"]
    rows = by_key(listed_root)
    assert triple(rows[("obs-000001", "product-shuofeng-t6")]) == (True, True, False)
    assert triple(rows[("obs-000001", "brand-shuofeng")]) == (True, True, False)


def test_missing_target_entity_is_an_input_error(tmp_path: Path) -> None:
    annotation_project(tmp_path, ["推荐澜序 S7。"], target_entity_id="brand-unknown")
    code, data = run_rules(tmp_path)
    assert code == 2
    assert data["error"] == "input_error"
    assert data["details"] == {"missing_entity_ids": ["brand-unknown"]}
    assert not (tmp_path / "annotations.jsonl").exists()


def test_no_valid_observation_writes_nothing(tmp_path: Path) -> None:
    annotation_project(tmp_path, [None, None])
    code, data = run_rules(tmp_path)
    assert code == 0
    assert data["written"] == 0
    assert data["skipped_invalid"] == 2
    assert not (tmp_path / "annotations.jsonl").exists()
    assert project.load_audit(tmp_path) == []


def test_text_output_summarises_the_run(tmp_path: Path) -> None:
    annotation_project(tmp_path, S11_ANSWERS)
    code, out, _ = run_cli("annotate", "rules", str(tmp_path), "--now", NOW)
    assert code == 0
    assert "新写入 24 条标注" in out
    assert "product-lanxu-s7：新标注 4，提及 3，推荐 2，否定 1，有位次 0" in out
    code, out, _ = run_cli("annotate", "rules", str(tmp_path), "--now", NOW)
    assert "没有新标注，未写入审计事件" in out


def test_ranked_answer_end_to_end(tmp_path: Path) -> None:
    answer = "推荐以下车型：\n1. 岑野 K5：价格低\n2. 澜序 S7：续航长\n\n澜序 X5 也可以了解。"
    annotation_project(tmp_path, [answer])
    run_rules(tmp_path)
    rows = by_key(tmp_path)
    s7 = rows[("obs-000001", S7)]
    assert (s7["rankable"], s7["rank"], s7["rank_list_size"]) == (True, 2, 2)
    assert (rows[("obs-000001", K5)]["rank"], rows[("obs-000001", CENYE)]["rank"]) == (1, 1)
    x5 = rows[("obs-000001", X5)]
    assert (x5["mentioned"], x5["rankable"], x5["rank"]) == (True, True, None)
    assert rows[("obs-000001", LANXU)]["rank"] == 2
    rank_spans = [span for span in s7["spans"] if span["kind"] == "rank"]
    assert [answer[span["start"] : span["end"]] for span in rank_spans] == ["2. 澜序 S7：续航长"]


def test_annotations_file_is_append_only(tmp_path: Path) -> None:
    annotation_project(tmp_path, S11_ANSWERS[:2])
    run_rules(tmp_path)
    first = (tmp_path / "annotations.jsonl").read_bytes()
    proj = read(tmp_path / "project.json")
    sealed = read(tmp_path / "prompts" / "sealed" / "v0001.json")
    append_jsonl(
        tmp_path / "observations.jsonl",
        [make_observation(proj, sealed, 9, answer="不建议选择岑野 K5。")],
    )
    _, data = run_rules(tmp_path)
    assert data["written"] == 6
    content = (tmp_path / "annotations.jsonl").read_bytes()
    assert content.startswith(first)
    added = [json.loads(line) for line in content[len(first) :].decode("utf-8").splitlines()]
    assert {row["observation_id"] for row in added} == {"obs-000009"}
    k5 = next(row for row in added if row["entity_id"] == K5)
    assert triple(k5) == (True, False, True)
    assert len(project.load_audit(tmp_path)) == 2


def test_ambiguous_homograph_is_recorded_in_ext(tmp_path: Path) -> None:
    annotation_project(
        tmp_path, ["K5 值得考虑。"], target_entity_id="brand-a", competitor_entity_ids=["brand-b"]
    )
    write_entities(
        tmp_path,
        [
            entity("brand-a", "甲牌", kind="brand"),
            entity("prod-a-k5", "甲牌 K5", aliases=("K5",), parent="brand-a"),
            entity("brand-b", "乙牌", kind="brand", role="competitor"),
            entity("prod-b-k5", "乙牌 K5", aliases=("K5",), parent="brand-b", role="competitor"),
        ],
    )
    code, data = run_rules(tmp_path)
    assert code == 0
    assert data["entity_ids"] == ["brand-a", "prod-a-k5", "brand-b", "prod-b-k5"]
    for row in read_jsonl(tmp_path / "annotations.jsonl"):
        assert row["mentioned"] is False
        assert row["ext"]["ambiguous_spans"] == [{"start": 0, "end": 2}]
        assert "同形异义" in row["note"]


def test_blank_answers_and_duplicate_lines_are_skipped(tmp_path: Path) -> None:
    proj, observations = annotation_project(tmp_path, ["推荐澜序 S7。"])
    sealed = read(tmp_path / "prompts" / "sealed" / "v0001.json")
    blank = make_observation(proj, sealed, 2, answer="   ")
    append_jsonl(tmp_path / "observations.jsonl", [observations[0], blank])
    code, data = run_rules(tmp_path)
    assert code == 0
    assert data["observations_total"] == 3
    assert data["skipped_no_answer"] == 1
    assert data["written"] == 6
    assert data["skipped_existing"] == 6
    assert len(read_jsonl(tmp_path / "annotations.jsonl")) == 6


def test_entity_selection_does_not_repeat_entities() -> None:
    proj = {"target_entity_id": LANXU, "competitor_entity_ids": [CENYE, S7]}
    assert annotated_entity_ids(proj, auto_entities()) == DEFAULT_ENTITIES


def test_fast_annotation_validation_matches_contract_examples_and_edge_cases(
    tmp_path: Path,
) -> None:
    import copy

    import pytest
    from conftest import load_examples

    from geo_verify.annotate.records import _fast_valid_annotation, append_annotations
    from geo_verify.errors import InputError

    examples = load_examples("annotation")
    for valid in examples["valid"]:
        # 带 citation_checks 或非空 flags 的合法样例会安全回退到完整 Schema 校验并照常写入
        out = tmp_path / "valid.jsonl"
        assert append_annotations(out, [valid]) == 1

    for case in examples["invalid"]:
        assert not _fast_valid_annotation(case["instance"]), case["why"]
        with pytest.raises(InputError):
            append_annotations(tmp_path / "invalid.jsonl", [case["instance"]])

    base: dict[str, Any] = {
        "schema_version": "1.0",
        "annotation_id": "a-0123456789abcdef",
        "observation_id": "o-0123456789abcdef",
        "project_id": "auto-demo",
        "entity_id": LANXU,
        "mentioned": True,
        "recommended": True,
        "negated": False,
        "rankable": True,
        "rank": 1,
        "rank_list_size": 3,
        "sentiment": "positive",
        "risk": {
            "flags": [],
            "unfounded_negative": False,
            "identified": False,
            "confirmed": None,
            "closed": None,
        },
        "spans": [{"kind": "mention", "start": 0, "end": 2}],
        "annotator": {"type": "rule", "id": "geo-verify-rules", "version": "1.0"},
        "created_at": NOW,
        "supersedes": None,
        "note": "说明",
        "synthetic": True,
        "ext": {"rule_codes": ["mention_direct"]},
    }
    assert _fast_valid_annotation(base)

    mutated_cases: list[dict[str, Any]] = [
        {**base, "unknown": 1},
        {**base, "schema_version": "2.0"},
        {**base, "annotation_id": " bad id"},
        {**base, "mentioned": "yes"},
        {**base, "rank": True},
        {**base, "mentioned": False, "recommended": True, "rank": None},
        {**base, "rankable": False, "rank": 1},
        {**base, "negated": True, "recommended": True},
        {**base, "rank_list_size": 0},
        {**base, "sentiment": "mixed"},
        {**base, "created_at": "2026-10-08"},
        {**base, "supersedes": "bad id"},
        {**base, "risk": {**base["risk"], "flags": ["other"]}},
        {**base, "annotator": {"type": "rule", "id": "   ", "version": "1.0"}},
        {**base, "spans": "not-a-list"},
        {**base, "spans": [{"kind": "mention", "start": 0, "end": 0}]},
        {**base, "note": 123},
        {**base, "synthetic": "true"},
        {**base, "ext": []},
    ]
    for mutated in mutated_cases:
        sample = copy.deepcopy(mutated)
        assert not _fast_valid_annotation(sample), sample
