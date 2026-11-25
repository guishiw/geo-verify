"""题库封存、版本链与差异记录（prompts seal / diff）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from factory import make_project, make_prompt
from helpers_basic import clean_prompts, write_draft, write_entities

from geo_verify import audit, project, spec
from geo_verify.canon import hash_obj
from geo_verify.jsonl import read_json, write_json
from geo_verify.seal.diff import diff_prompts

NOW = "2026-10-02T03:00:00Z"
LATER = "2026-10-05T03:00:00Z"
SEALER = "甲方项目负责人（虚构）"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    make_project(path)
    write_entities(path)
    write_draft(path, clean_prompts())
    return path


def seal(root: Path, *extra: str, now: str = NOW) -> tuple[int, Any]:
    return run_cli_json("prompts", "seal", str(root), "--by", SEALER, "--now", now, *extra)


def independent_content_hash(prompt_set: dict[str, Any]) -> str:
    """按约定独立重算内容哈希：只覆盖题目和版本链，不含封存人与封存时间。"""
    fields = ("prompt_set_id", "project_id", "version", "previous_hash", "prompts")
    return hash_obj({key: prompt_set[key] for key in fields})


# ------------------------------------------------------------------ 首次封存


def test_first_seal_writes_v1_and_audit_event(root: Path) -> None:
    code, data = seal(root)
    assert code == 0
    assert data["sealed"] is True
    assert data["version"] == 1
    assert data["file"] == "prompts/sealed/v0001.json"

    sealed = project.load_sealed_prompt_sets(root)
    assert len(sealed) == 1
    prompt_set = sealed[0]
    assert spec.iter_issues("prompt_set", prompt_set) == []
    assert prompt_set["version"] == 1
    assert prompt_set["previous_hash"] is None
    assert prompt_set["prompts"] == clean_prompts()
    assert prompt_set["sealed_by"] == SEALER
    assert prompt_set["sealed_at"] == NOW
    assert prompt_set["project_id"] == "auto-demo"
    assert prompt_set["synthetic"] is True
    assert prompt_set["content_hash"] == independent_content_hash(prompt_set)
    assert data["content_hash"] == prompt_set["content_hash"]
    registered = read_json(root / "project.json")["preregistration"]
    assert prompt_set["preregistration_hash"] == hash_obj(registered)

    events = project.load_audit(root)
    assert len(events) == 1
    assert events[0]["action"] == "seal"
    assert events[0]["actor"] == SEALER
    assert events[0]["occurred_at"] == NOW
    assert events[0]["before_hash"] is None
    assert events[0]["after_hash"] == prompt_set["content_hash"]
    assert events[0]["target"] == {"type": "prompt_set", "id": "auto-demo-prompts@1"}
    assert audit.verify_chain(events) == []
    assert not (root / "prompts" / "diffs").exists()


def test_seal_output_is_reproducible_with_fixed_now(tmp_path: Path) -> None:
    contents = []
    for name in ("a", "b"):
        path = tmp_path / name
        make_project(path)
        write_entities(path)
        write_draft(path, clean_prompts())
        assert seal(path)[0] == 0
        contents.append((path / "prompts" / "sealed" / "v0001.json").read_bytes())
        contents.append((path / "audit.jsonl").read_bytes())
    assert contents[0] == contents[2]
    assert contents[1] == contents[3]


def test_text_output_mentions_version_and_hash(root: Path) -> None:
    code, out, _ = run_cli("prompts", "seal", str(root), "--by", SEALER, "--now", NOW)
    assert code == 0
    assert "第 1 版" in out
    assert project.select_prompt_set(root)["content_hash"][:12] in out


# ------------------------------------------------------------------ 拒绝封存


def test_seal_refuses_when_lint_has_errors(root: Path) -> None:
    prompts = [*clean_prompts(), make_prompt("Q900", text="为什么澜序 S7 是最好的？")]
    write_draft(root, prompts)
    code, data = seal(root)
    assert code == 1
    assert data["sealed"] is False
    assert [item["rule"] for item in data["findings"] if item["severity"] == "error"] == [
        "leading_presupposition"
    ]
    assert project.load_sealed_prompt_sets(root) == []
    assert project.load_audit(root) == []
    code, out, _ = run_cli("prompts", "seal", str(root), "--by", SEALER, "--now", NOW)
    assert code == 1
    assert "拒绝封存" in out


def test_seal_allows_warnings(root: Path) -> None:
    write_draft(root, clean_prompts()[:4])
    code, data = seal(root)
    assert code == 0
    assert data["sealed"] is True
    assert [item["rule"] for item in data["findings"]] == ["missing_intent_type"]


def test_seal_requires_sealer_name(root: Path) -> None:
    code, data = run_cli_json("prompts", "seal", str(root), "--by", "  ", "--now", NOW)
    assert code == 2
    assert project.load_sealed_prompt_sets(root) == []
    assert "封存人" in data["message"]


# ------------------------------------------------------------------ 再次封存


def test_reseal_without_change_creates_no_new_version(root: Path) -> None:
    assert seal(root)[0] == 0
    code, data = seal(root, now=LATER)
    assert code == 0
    assert data["sealed"] is False
    assert data["changed"] is False
    assert data["version"] == 1
    assert len(project.load_sealed_prompt_sets(root)) == 1
    assert len(project.load_audit(root)) == 1
    code, out, _ = run_cli("prompts", "seal", str(root), "--by", SEALER, "--now", LATER)
    assert code == 0
    assert "无变化" in out


def test_reordering_alone_is_not_a_change(root: Path) -> None:
    assert seal(root)[0] == 0
    write_draft(root, list(reversed(clean_prompts())))
    code, data = seal(root, "--reason", "调整顺序", now=LATER)
    assert code == 0
    assert data["changed"] is False
    assert len(project.load_sealed_prompt_sets(root)) == 1


def test_changed_draft_needs_reason(root: Path) -> None:
    assert seal(root)[0] == 0
    prompts = clean_prompts()
    prompts[1]["text"] = "澜序 S7 的 CLTC 续航是多少公里？"
    write_draft(root, prompts)
    code, data = seal(root, now=LATER)
    assert code == 2
    assert "--reason" in data["message"]
    assert len(project.load_sealed_prompt_sets(root)) == 1
    assert len(project.load_audit(root)) == 1


def test_amendment_creates_new_version_diff_and_audit_event(root: Path) -> None:
    assert seal(root)[0] == 0
    first = project.select_prompt_set(root)
    prompts = clean_prompts()
    prompts[1]["text"] = "澜序 S7 的 CLTC 续航是多少公里？"
    prompts[1]["priority"] = "P0"
    removed = prompts.pop(4)
    added = make_prompt("Q050", text="澜序 X5 的后备厢容积有多大？", intent_type="product")
    prompts.append(added)
    write_draft(root, prompts)

    code, data = seal(root, "--reason", "委托方补充产品题并修正表述", now=LATER)
    assert code == 0
    assert data["sealed"] is True
    assert data["changed"] is True
    assert data["version"] == 2
    assert data["diff_file"] == "prompts/diffs/v0001-v0002.json"

    sealed = project.load_sealed_prompt_sets(root)
    assert [item["version"] for item in sealed] == [1, 2]
    assert sealed[0] == first, "旧版本文件不得被改动"
    second = sealed[1]
    assert second["previous_hash"] == first["content_hash"]
    assert second["content_hash"] == independent_content_hash(second)
    assert second["content_hash"] != first["content_hash"]
    assert second["sealed_at"] == LATER

    diff = read_json(root / "prompts" / "diffs" / "v0001-v0002.json")
    assert diff["from_version"] == 1
    assert diff["to_version"] == 2
    assert diff["from_hash"] == first["content_hash"]
    assert diff["to_hash"] == second["content_hash"]
    assert diff["reason"] == "委托方补充产品题并修正表述"
    assert diff["added"] == [added]
    assert diff["removed"] == [removed]
    assert [(item["query_id"], item["changed_fields"]) for item in diff["modified"]] == [
        ("Q002", ["priority", "text"])
    ]
    assert diff["modified"][0]["before"]["text"] == "澜序 S7 的续航是多少公里？"
    assert diff["modified"][0]["after"]["text"] == "澜序 S7 的 CLTC 续航是多少公里？"

    events = project.load_audit(root)
    assert [item["action"] for item in events] == ["seal", "amend_prompts"]
    amend = events[1]
    assert amend["reason"] == "委托方补充产品题并修正表述"
    assert amend["before_hash"] == first["content_hash"]
    assert amend["after_hash"] == second["content_hash"]
    assert amend["details"]["added"] == 1
    assert amend["details"]["removed"] == 1
    assert amend["details"]["modified"] == 1
    assert audit.verify_chain(events) == []


def test_preregistration_change_after_seal_is_reported(root: Path) -> None:
    assert seal(root)[0] == 0
    proj = read_json(root / "project.json")
    proj["preregistration"]["metrics"][0]["threshold"]["value"] = 0.4
    write_json(root / "project.json", proj)
    code, data = seal(root, now=LATER)
    assert code == 0
    assert data["changed"] is False
    assert data["preregistration_changed"] is True
    code, out, _ = run_cli("prompts", "seal", str(root), "--by", SEALER, "--now", LATER)
    assert "预先登记" in out


def test_tampered_sealed_file_blocks_further_sealing(root: Path) -> None:
    assert seal(root)[0] == 0
    path = root / "prompts" / "sealed" / "v0001.json"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["prompts"][0]["text"] = "澜序是不是最好的品牌？"
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    code, data = seal(root, "--reason", "再次封存", now=LATER)
    assert code == 1
    assert "被改动" in data["message"]


# ------------------------------------------------------------------ 差异


def test_diff_prompts_aligns_by_query_id() -> None:
    old = [
        {"query_id": "A", "text": "甲", "priority": "P1"},
        {"query_id": "B", "text": "乙", "priority": "P1"},
        {"query_id": "C", "text": "丙", "priority": "P1"},
    ]
    new = [
        {"query_id": "C", "text": "丙", "priority": "P1"},
        {"query_id": "B", "text": "乙改", "priority": "P1", "core_fact_ids": ["F1"]},
        {"query_id": "D", "text": "丁", "priority": "P2"},
    ]
    result = diff_prompts(old, new)
    assert result["added"] == [new[2]]
    assert result["removed"] == [old[0]]
    assert result["modified"] == [
        {
            "query_id": "B",
            "changed_fields": ["core_fact_ids", "text"],
            "before": old[1],
            "after": new[1],
        }
    ]
    assert result["unchanged"] == 1
    assert diff_prompts(old, list(reversed(old))) == {
        "added": [],
        "removed": [],
        "modified": [],
        "unchanged": 3,
    }


def test_cli_diff_defaults_to_last_two_versions(root: Path) -> None:
    assert seal(root)[0] == 0
    code, data = run_cli_json("prompts", "diff", str(root))
    assert code == 2, "只有一个版本时无可比较"
    assert "至少两个" in data["message"]

    prompts = clean_prompts()
    prompts[0]["text"] = "澜序这个品牌的口碑怎么样？"
    write_draft(root, prompts)
    assert seal(root, "--reason", "修正表述", now=LATER)[0] == 0
    prompts.append(make_prompt("Q060", text="充电桩安装需要哪些条件？", intent_type="problem"))
    write_draft(root, prompts)
    assert seal(root, "--reason", "补充题目", now="2026-10-06T03:00:00Z")[0] == 0

    code, data = run_cli_json("prompts", "diff", str(root))
    assert code == 0
    assert (data["from_version"], data["to_version"]) == (2, 3)
    assert [item["query_id"] for item in data["added"]] == ["Q060"]
    assert data["modified"] == []

    code, data = run_cli_json("prompts", "diff", str(root), "--from", "1", "--to", "3")
    assert code == 0
    assert [item["query_id"] for item in data["added"]] == ["Q060"]
    assert [(item["query_id"], item["changed_fields"]) for item in data["modified"]] == [
        ("Q001", ["text"])
    ]
    assert data["removed"] == []

    code, out, _ = run_cli("prompts", "diff", str(root), "--from", "1", "--to", "3")
    assert code == 0
    assert "新增 1 题" in out
    assert "修改 1 题" in out
    assert "Q060" in out

    code, data = run_cli_json("prompts", "diff", str(root), "--from", "1", "--to", "9")
    assert code == 2
    code, data = run_cli_json("prompts", "diff", str(root), "--from", "2", "--to", "2")
    assert code == 2


def test_cli_diff_without_sealed_versions(root: Path) -> None:
    code, data = run_cli_json("prompts", "diff", str(root))
    assert code == 2
    assert data["error"] == "input_error"
