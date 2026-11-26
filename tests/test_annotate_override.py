"""`annotate override`：人工覆盖追加新标注、supersedes 指向被覆盖的生效标注、历史全部保留、写审计事件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from factory import make_annotation, write_records
from helpers_annotate import NOW, annotation_project

from geo_verify import audit, project
from geo_verify.annotate import run_rules
from geo_verify.canon import hash_obj
from geo_verify.jsonl import read_jsonl

LATER = "2026-10-09T03:00:00Z"
S7 = "product-lanxu-s7"
LANXU = "brand-lanxu"
OBS = "obs-000001"
ANSWERS = ["推荐澜序 S7，续航出色。", "不推荐岑野 K5。"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    annotation_project(tmp_path, ANSWERS)
    run_rules.annotate_project(tmp_path, now=NOW)
    return tmp_path


def override(
    root: Path, *pairs: str, entity: str = S7, now: str = LATER, **options: str
) -> tuple[int, Any]:
    argv = [
        "annotate",
        "override",
        str(root),
        "--observation",
        options.get("observation", OBS),
        "--entity",
        entity,
        "--set",
        ",".join(pairs),
        "--annotator",
        options.get("annotator", "reviewer-01"),
        "--reason",
        options.get("reason", "人工复核：回答只是列举，并未推荐"),
        "--now",
        now,
    ]
    return run_cli_json(*argv)


def effective(root: Path, entity: str = S7, observation: str = OBS) -> dict[str, Any]:
    return project.effective_annotations(project.load_annotations(root))[(observation, entity)]


def test_override_appends_a_human_annotation_and_keeps_history(root: Path) -> None:
    before_bytes = (root / "annotations.jsonl").read_bytes()
    base = effective(root)
    assert base["annotator"]["type"] == "rule"
    assert base["recommended"] is True

    code, data = override(root, "recommended=false")
    assert code == 0, data

    content = (root / "annotations.jsonl").read_bytes()
    assert content.startswith(before_bytes)  # 历史行一字不改
    rows = read_jsonl(root / "annotations.jsonl")
    new = rows[-1]
    assert len(rows) == len(read_jsonl_bytes(before_bytes)) + 1
    assert new["annotation_id"] == data["annotation_id"]
    assert new["supersedes"] == base["annotation_id"]
    assert new["annotator"] == {"type": "human", "id": "reviewer-01", "version": "manual"}
    assert new["created_at"] == LATER
    assert new["recommended"] is False
    # 未指定的字段沿用被覆盖标注的值
    for key in ("mentioned", "negated", "rankable", "rank", "rank_list_size", "sentiment", "risk"):
        assert new[key] == base[key], key
    assert new["note"] == "人工复核：回答只是列举，并未推荐"
    assert new["ext"]["override"]["fields"] == ["recommended"]
    assert "spans" not in new
    assert new["synthetic"] is True
    assert effective(root) == new
    assert data["supersedes"] == base["annotation_id"]
    assert data["changes"] == {"recommended": {"from": True, "to": False}}


def read_jsonl_bytes(content: bytes) -> list[str]:
    return [line for line in content.decode("utf-8").split("\n") if line]


def test_override_writes_an_audit_event_with_before_and_after_hashes(root: Path) -> None:
    base = effective(root)
    code, data = override(root, "recommended=false", "sentiment=neutral")
    assert code == 0
    events = project.load_audit(root)
    event = events[-1]
    new = effective(root)
    assert event["event_id"] == data["audit_event_id"]
    assert event["action"] == "override"
    assert event["actor"] == "reviewer-01"
    assert event["reason"] == "人工复核：回答只是列举，并未推荐"
    assert event["occurred_at"] == LATER
    assert event["target"] == {"type": "annotation", "id": new["annotation_id"]}
    assert event["before_hash"] == hash_obj(base)
    assert event["after_hash"] == hash_obj(new)
    assert event["details"]["observation_id"] == OBS
    assert event["details"]["entity_id"] == S7
    assert event["details"]["supersedes"] == base["annotation_id"]
    assert event["details"]["changes"] == {
        "recommended": {"from": True, "to": False},
        "sentiment": {"from": "positive", "to": "neutral"},
    }
    assert audit.verify_chain(events) == []


def test_override_can_set_risk_fields_and_nulls(root: Path) -> None:
    code, data = override(
        root,
        "risk.unfounded_negative=true",
        "risk.identified=true",
        "risk.confirmed=true",
        "risk.closed=false",
        "risk.flags=unfounded_negative|wrong_fact",
        "recommended=null",
        "sentiment=null",
    )
    assert code == 0, data
    new = effective(root)
    assert new["risk"] == {
        "flags": ["unfounded_negative", "wrong_fact"],
        "unfounded_negative": True,
        "identified": True,
        "confirmed": True,
        "closed": False,
    }
    assert new["recommended"] is None
    assert new["sentiment"] is None


def test_override_of_rank_fields(root: Path) -> None:
    code, _ = override(root, "rankable=true", "rank=2", "rank_list_size=3")
    assert code == 0
    new = effective(root)
    assert (new["rankable"], new["rank"], new["rank_list_size"]) == (True, 2, 3)


def test_second_override_supersedes_the_first(root: Path) -> None:
    override(root, "recommended=false")
    first = effective(root)
    code, _ = override(
        root,
        "recommended=true",
        now="2026-10-10T03:00:00Z",
        annotator="reviewer-02",
        reason="二审改回",
    )
    assert code == 0
    second = effective(root)
    assert second["supersedes"] == first["annotation_id"]
    assert second["annotator"]["id"] == "reviewer-02"
    history = [
        row
        for row in project.load_annotations(root)
        if (row["observation_id"], row["entity_id"]) == (OBS, S7)
    ]
    assert len(history) == 3
    assert [row["annotator"]["type"] for row in history] == ["rule", "human", "human"]


def test_override_without_change_is_still_recorded(root: Path) -> None:
    # 裁决者认同现有结论时也要留下覆盖记录（用于争议项裁决）
    code, data = override(root, "recommended=true")
    assert code == 0
    assert data["changes"] == {"recommended": {"from": True, "to": True}}
    assert effective(root)["annotator"]["type"] == "human"


@pytest.mark.parametrize(
    ("pairs", "options", "fragment"),
    [
        (("recommended=false",), {"observation": "obs-999999"}, "obs-999999"),
        (("recommended=false",), {"entity": "brand-unknown"}, "brand-unknown"),
        (("color=red",), {}, "color"),
        (("mentioned=maybe",), {}, "mentioned"),
        (("rank=0",), {}, "rank"),
        (("rank=abc",), {}, "rank"),
        (("sentiment=happy",), {}, "sentiment"),
        (("recommended",), {}, "recommended"),
        (("recommended=false", "recommended=true"), {}, "recommended"),
        (("mentioned=null",), {}, "mentioned"),
        (("risk.flags=bad_flag",), {}, "risk.flags"),
        (("risk.flags=wrong_fact|wrong_fact",), {}, "risk.flags"),
        ((), {}, "--set"),
        (("mentioned=false",), {}, "annotation"),
        (("rankable=true", "rank=4", "rank_list_size=3"), {}, "rank"),
        (("recommended=false",), {"reason": "  "}, "理由"),
        (("recommended=false",), {"annotator": " "}, "标注者"),
    ],
)
def test_invalid_overrides_are_input_errors(
    root: Path, pairs: tuple[str, ...], options: dict[str, str], fragment: str
) -> None:
    before = (root / "annotations.jsonl").read_bytes()
    events = len(project.load_audit(root))
    entity = options.pop("entity", S7)
    code, data = override(root, *pairs, entity=entity, **options)
    assert code == 2, data
    assert data["error"] == "input_error"
    assert fragment in data["message"]
    assert (root / "annotations.jsonl").read_bytes() == before
    assert len(project.load_audit(root)) == events


def test_override_needs_an_existing_annotation(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS)
    code, data = override(tmp_path, "recommended=false")
    assert code == 2
    assert "annotate rules" in data["message"]


def test_override_must_not_predate_the_annotation_it_replaces(root: Path) -> None:
    override(root, "recommended=false", now="2026-10-10T03:00:00Z")
    code, data = override(root, "recommended=true", now="2026-10-09T03:00:00Z")
    assert code == 2
    assert "时间" in data["message"]


def test_override_must_become_the_effective_annotation(root: Path) -> None:
    observation = project.load_observations(root)[0]
    tied = "2026-10-09T00:00:00Z"
    first = make_annotation(
        observation,
        1,
        entity_id=S7,
        annotator_type="human",
        annotator_id="reviewer-a",
        created_at=tied,
    )
    second = make_annotation(
        observation,
        2,
        entity_id=S7,
        annotator_type="human",
        annotator_id="reviewer-b",
        created_at=tied,
    )
    write_records(root, annotations=[first, second])
    assert effective(root)["annotation_id"] == "ann-000002"
    # 覆盖 ann-000002 后，ann-000001 与新标注同为人工、同一时间，按编号排序新标注不会生效：拒绝写入
    code, data = override(root, "recommended=false", now=tied)
    assert code == 2
    assert "时间" in data["message"]
    assert effective(root)["annotation_id"] == "ann-000002"


def test_text_output(root: Path) -> None:
    code, out, _ = run_cli(
        "annotate",
        "override",
        str(root),
        "--observation",
        OBS,
        "--entity",
        LANXU,
        "--set",
        "recommended=false",
        "--annotator",
        "reviewer-01",
        "--reason",
        "复核",
        "--now",
        LATER,
    )
    assert code == 0
    assert "已追加人工覆盖标注" in out
    assert "recommended：true → false" in out


def test_override_can_clear_risk_flags(root: Path) -> None:
    override(root, "risk.flags=wrong_fact")
    code, _ = override(root, "risk.flags=", now="2026-10-10T03:00:00Z")
    assert code == 0
    assert effective(root)["risk"]["flags"] == []


def test_override_in_a_non_synthetic_project(tmp_path: Path) -> None:
    annotation_project(tmp_path, ANSWERS, synthetic=False)
    run_rules.annotate_project(tmp_path, now=NOW)
    code, _ = override(tmp_path, "recommended=false")
    assert code == 0
    assert "synthetic" not in effective(tmp_path)
