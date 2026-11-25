"""项目目录约定、标注生效规则、剔除记录与审计链的测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from factory import (
    make_annotation,
    make_entities,
    make_judgment,
    make_observation,
    make_project,
    make_prompt,
    seal_prompts,
    write_records,
)

from geo_verify import audit, project, spec
from geo_verify.errors import CheckFailed, InputError
from geo_verify.jsonl import read_jsonl, write_json

NOW = "2026-10-09T03:00:00Z"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "proj"


def test_factory_outputs_satisfy_contracts(root: Path) -> None:
    proj = make_project(root)
    entities = make_entities(root)
    sealed = seal_prompts(root, proj, [make_prompt("Q001"), make_prompt("Q002")])
    valid = make_observation(proj, sealed, 1)
    invalid = make_observation(proj, sealed, 2, status="invalid", invalid_reason="rate_limited")
    manual = make_observation(proj, sealed, 3, channel="manual", model_version=None)
    assert spec.iter_issues("project", proj) == []
    assert all(spec.is_valid("entity", item) for item in entities)
    assert spec.iter_issues("prompt_set", sealed) == []
    for observation in (valid, invalid, manual):
        assert spec.iter_issues("observation", observation) == []
    assert spec.iter_issues("annotation", make_annotation(valid, 1, rankable=True, rank=2)) == []
    assert spec.iter_issues("annotation", make_annotation(valid, 2, mentioned=False)) == []
    for number, (direction, label) in enumerate(
        [
            ("answer_to_facts", "supported"),
            ("answer_to_facts", "unverifiable"),
            ("facts_to_answer", "missing"),
        ],
        start=1,
    ):
        judgment = make_judgment(valid, number, direction=direction, label=label)
        assert spec.iter_issues("judgment", judgment) == []


def test_paths_and_loaders(root: Path) -> None:
    proj = make_project(root)
    make_entities(root)
    sealed = seal_prompts(root, proj, [make_prompt("Q001")])
    observation = make_observation(proj, sealed, 1)
    write_records(
        root,
        observations=[observation],
        annotations=[make_annotation(observation, 1)],
        judgments=[make_judgment(observation, 1)],
    )
    project_paths = project.paths(root)
    assert project_paths.sealed_file(3).name == "v0003.json"
    assert project_paths.draft == root / "prompts" / "draft.jsonl"
    assert project_paths.diffs_dir.parent == project_paths.prompts_dir
    assert project_paths.out_dir == root / "out"
    assert project_paths.evidence_dir == root / "evidence"
    assert project_paths.recheck_dir == root / "recheck"
    assert project_paths.snapshots_dir == root / "snapshots"
    assert project.load_project(root)["project_id"] == "auto-demo"
    assert [item["entity_id"] for item in project.load_entities(root)] == [
        "brand-lanxu",
        "brand-cenye",
    ]
    assert len(project.load_observations(root)) == 1
    assert len(project.load_annotations(root)) == 1
    assert len(project.load_judgments(root)) == 1
    assert project.load_audit(root) == []


def test_missing_project_dir_and_invalid_project(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="项目目录不存在"):
        project.paths(tmp_path / "nope")
    root = tmp_path / "bad"
    root.mkdir()
    write_json(root / "project.json", {"schema_version": "1.0", "project_id": "x"})
    with pytest.raises(InputError, match="不符合契约 project"):
        project.load_project(root)


def test_duplicate_entity_id_rejected(root: Path) -> None:
    make_project(root)
    entities = make_entities(root)
    make_entities(root, extra=[entities[0]])
    with pytest.raises(InputError, match="实体编号重复"):
        project.load_entities(root)


def test_preregistration_hash_changes_with_threshold(root: Path) -> None:
    proj = make_project(root)
    before = project.preregistration_hash(proj)
    proj["preregistration"]["metrics"][0]["threshold"]["value"] = 0.5
    assert project.preregistration_hash(proj) != before
    proj["name"] = "改名不影响登记内容"
    proj["preregistration"]["metrics"][0]["threshold"]["value"] = 0.6
    assert project.preregistration_hash(proj) == before


def test_sealed_prompt_sets_load_in_order_and_select(root: Path) -> None:
    proj = make_project(root)
    assert project.load_sealed_prompt_sets(root) == []
    with pytest.raises(InputError, match="题库尚未封存"):
        project.select_prompt_set(root)
    first = seal_prompts(root, proj, [make_prompt("Q001")])
    second = seal_prompts(root, proj, [make_prompt("Q001"), make_prompt("Q002")], previous=first)
    sealed = project.load_sealed_prompt_sets(root)
    assert [item["version"] for item in sealed] == [1, 2]
    assert sealed[1]["previous_hash"] == first["content_hash"]
    assert project.select_prompt_set(root)["content_hash"] == second["content_hash"]
    assert project.select_prompt_set(root, 1)["content_hash"] == first["content_hash"]
    with pytest.raises(InputError, match="没有第 3 版"):
        project.select_prompt_set(root, 3)


def test_editing_sealed_prompt_text_is_detected(root: Path) -> None:
    proj = make_project(root)
    seal_prompts(root, proj, [make_prompt("Q001"), make_prompt("Q002")])
    path = root / "prompts" / "sealed" / "v0001.json"
    sealed = json.loads(path.read_text(encoding="utf-8"))
    sealed["prompts"][1]["text"] = "澜序是不是最好的品牌？"
    path.write_text(json.dumps(sealed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CheckFailed, match="封存题库被改动") as excinfo:
        project.load_sealed_prompt_sets(root)
    assert excinfo.value.exit_code == 1


def test_sealed_version_gap_and_broken_chain_detected(root: Path) -> None:
    proj = make_project(root)
    first = seal_prompts(root, proj, [make_prompt("Q001")])
    second = seal_prompts(root, proj, [make_prompt("Q002")], previous=first)
    path = root / "prompts" / "sealed" / "v0002.json"

    forged = dict(second, previous_hash="0" * 64)
    forged["content_hash"] = project.prompt_content_hash(forged)
    write_json(path, forged)
    with pytest.raises(CheckFailed, match="版本链断裂"):
        project.load_sealed_prompt_sets(root)

    write_json(path, second)
    (root / "prompts" / "sealed" / "v0001.json").unlink()
    with pytest.raises(CheckFailed, match="版本不连续"):
        project.load_sealed_prompt_sets(root)


def test_effective_annotation_prefers_human_then_latest_and_keeps_history(root: Path) -> None:
    proj = make_project(root)
    sealed = seal_prompts(root, proj, [make_prompt("Q001")])
    observation = make_observation(proj, sealed, 1)
    rule = make_annotation(observation, 1, mentioned=False)
    model = make_annotation(
        observation, 2, annotator_type="model", created_at="2026-10-08T09:00:00Z"
    )
    human_old = make_annotation(
        observation, 3, annotator_type="human", annotator_id="A", created_at="2026-10-08T04:00:00Z"
    )
    human_new = make_annotation(
        observation,
        4,
        annotator_type="human",
        annotator_id="B",
        created_at="2026-10-08T05:00:00Z",
        mentioned=False,
    )
    other_entity = make_annotation(observation, 5, entity_id="brand-cenye")
    history = [rule, model, human_old, human_new, other_entity]
    effective = project.effective_annotations(history)
    assert effective[("obs-000001", "brand-lanxu")]["annotation_id"] == "ann-000004"
    assert effective[("obs-000001", "brand-cenye")]["annotation_id"] == "ann-000005"
    assert len(history) == 5, "解析生效标注不得改动历史"
    assert project.effective_annotations([rule, model])[("obs-000001", "brand-lanxu")] is model
    assert project.effective_annotations(list(reversed(history))) == effective, "结果与输入顺序无关"


def test_superseded_annotation_is_not_effective(root: Path) -> None:
    proj = make_project(root)
    sealed = seal_prompts(root, proj, [make_prompt("Q001")])
    observation = make_observation(proj, sealed, 1)
    human = make_annotation(
        observation, 1, annotator_type="human", created_at="2026-10-08T09:00:00Z"
    )
    correction = make_annotation(
        observation,
        2,
        annotator_type="human",
        created_at="2026-10-08T04:00:00Z",
        supersedes="ann-000001",
        mentioned=False,
    )
    effective = project.effective_annotations([human, correction])
    assert effective[("obs-000001", "brand-lanxu")]["annotation_id"] == "ann-000002"


def test_same_time_annotations_tie_break_is_deterministic(root: Path) -> None:
    proj = make_project(root)
    sealed = seal_prompts(root, proj, [make_prompt("Q001")])
    observation = make_observation(proj, sealed, 1)
    first = make_annotation(observation, 1, annotator_type="human")
    second = make_annotation(observation, 2, annotator_type="human")
    for order in ([first, second], [second, first]):
        chosen = project.effective_annotations(order)[("obs-000001", "brand-lanxu")]
        assert chosen["annotation_id"] == "ann-000002"


# ------------------------------------------------------------------ audit


def _event(root: Path, **overrides: object) -> dict:
    fields: dict = {
        "actor": "数据管理员（虚构）",
        "action": "import",
        "target_type": "observations",
        "target_id": "batch-1",
        "reason": "首批导入",
        "occurred_at": NOW,
    }
    fields.update(overrides)
    return audit.append_event(root, **fields)


def test_audit_chain_append_and_verify(root: Path) -> None:
    make_project(root)
    first = _event(root)
    second = _event(root, action="seal", after_hash="a" * 64, synthetic=True)
    assert first["event_id"] == "evt-000001"
    assert first["previous_event_hash"] is None
    assert second["previous_event_hash"] == first["event_hash"]
    assert second["synthetic"] is True
    events = read_jsonl(root / "audit.jsonl")
    assert audit.verify_chain(events) == []
    assert all(spec.is_valid("audit_event", item) for item in events)


def test_audit_requires_reason_and_actor(root: Path) -> None:
    make_project(root)
    with pytest.raises(InputError, match="理由"):
        _event(root, reason="  ")
    with pytest.raises(InputError, match="变更人"):
        _event(root, actor="")
    assert not (root / "audit.jsonl").exists(), "失败的调用不得写入任何记录"


def test_audit_requires_project_id(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    root.mkdir()
    write_json(root / "project.json", ["not", "an", "object"])
    with pytest.raises(InputError, match="缺少 project_id"):
        _event(root)


def test_audit_chain_detects_modify_delete_reorder_and_invalid(root: Path) -> None:
    make_project(root)
    for index in range(4):
        _event(root, target_id=f"batch-{index}")
    events = read_jsonl(root / "audit.jsonl")

    modified = json.loads(json.dumps(events))
    modified[1]["reason"] = "事后改写的理由"
    kinds = {(item["index"], item["kind"]) for item in audit.verify_chain(modified)}
    assert (2, "modified") in kinds

    deleted = [events[0], events[2], events[3]]
    kinds = {(item["index"], item["kind"]) for item in audit.verify_chain(deleted)}
    assert (2, "chain_broken") in kinds
    assert (2, "sequence") in kinds

    reordered = [events[0], events[2], events[1], events[3]]
    assert {item["kind"] for item in audit.verify_chain(reordered)} >= {"chain_broken", "sequence"}

    broken = json.loads(json.dumps(events))
    del broken[2]["actor"]
    problems = audit.verify_chain(broken)
    assert {"index": 3, "kind": "invalid", "message": "不符合 audit_event 契约"} in problems

    garbage = [{"event_hash": 123}]
    assert audit.verify_chain(garbage)[0]["kind"] == "invalid"


def test_exclusions_come_only_from_audit_events_and_leave_raw_data_untouched(root: Path) -> None:
    proj = make_project(root)
    sealed = seal_prompts(root, proj, [make_prompt("Q001")])
    observations = [make_observation(proj, sealed, number) for number in (1, 2, 3)]
    write_records(root, observations=observations)
    before = (root / "observations.jsonl").read_bytes()
    _event(
        root,
        action="exclude",
        reason="同一 trace_id 重复导入",
        details={"rule_id": "EX-DUP", "count": 2, "observation_ids": ["obs-000001", "obs-000002"]},
    )
    _event(
        root,
        action="exclude",
        reason="后来又按另一条规则剔除",
        details={
            "rule_id": "EX-OTHER",
            "count": 2,
            "observation_ids": ["obs-000002", "obs-000003"],
        },
    )
    _event(root)
    excluded = project.exclusions(project.load_audit(root))
    assert excluded == {"obs-000001": "EX-DUP", "obs-000002": "EX-DUP", "obs-000003": "EX-OTHER"}
    assert (root / "observations.jsonl").read_bytes() == before, "剔除不得改动原始记录"


def test_exclude_event_without_rule_and_count_is_rejected(root: Path) -> None:
    make_project(root)
    with pytest.raises(InputError, match="不符合契约 audit_event"):
        _event(root, action="exclude", reason="没有规则的剔除")


def test_resolve_now() -> None:
    assert audit.resolve_now(NOW) == NOW
    generated = audit.resolve_now(None)
    assert generated.endswith("Z")
    assert len(generated) == 20
    with pytest.raises(InputError):
        audit.resolve_now("2026-10-09 11:00")
