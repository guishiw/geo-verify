"""`geo-verify evidence verify`：清单链与文件校验，定位到文件、类型和行号（S08、S18 的前提）。

每个篡改测试只做一处改动；期望的文件和行号就是测试改动的位置（项目里写了 5 条观测、
3 条标注、2 条判定、2 个审计事件，见 helpers_evidence.make_evidence_project）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli
from helpers_evidence import (  # noqa: F401  (synthetic_installed_lock 是自动生效的 fixture)
    LATER,
    NOW,
    OBSERVATIONS,
    SNAPSHOT_HTML,
    SNAPSHOT_PNG,
    append_line,
    build,
    delete_line,
    lines_of,
    located,
    make_evidence_project,
    read_manifest,
    rewrite_field,
    sha,
    swap_lines,
    synthetic_installed_lock,
    verify,
    write_lines,
    write_manifest,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from geo_verify.evidence.verify import verify_evidence
from geo_verify.jsonl import append_jsonl, write_json

OBS = "observations.jsonl"
M1, M2, M3 = (f"evidence/manifest-{n:04d}.json" for n in (1, 2, 3))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    make_evidence_project(path)
    code, data = build(path)
    assert code == 0, data
    return path


def new_observation(root: Path, number: int) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(lines_of(root / OBS)[0])
    record.update(observation_id=f"obs-{number:06d}", trace_id=f"trace-{number:06d}")
    return record


def three_manifests(root: Path) -> None:
    append_line(root / OBS, new_observation(root, 90))
    assert build(root, now=LATER)[0] == 0
    append_line(root / OBS, new_observation(root, 91))
    assert build(root, now="2026-10-11T01:00:00Z")[0] == 0


def test_freshly_built_package_passes(root: Path) -> None:
    code, data = verify(root)
    assert code == 0, data["problems"]
    manifest = read_manifest(root, 1)
    assert data["status"] == "pass"
    assert data["status_label"] == "通过"
    assert data["kind"] == "evidence_verify"
    assert data["schema_version"] == "1.0"
    assert data["project_id"] == "auto-demo"
    assert data["synthetic"] is True
    assert data["manifests"] == 1
    assert data["latest_manifest"] == {"sequence": 1, "manifest_hash": manifest["manifest_hash"]}
    assert data["checked_files"] == len(manifest["files"])
    assert data["problems"] == []
    assert "不能证明采集时间真实" in data["notice"]
    written = json.loads((root / "out" / "evidence-verify.json").read_text(encoding="utf-8"))
    assert written == data


def test_no_manifest_fails(tmp_path: Path) -> None:
    path = tmp_path / "proj"
    make_evidence_project(path)
    code, data = verify(path)
    assert code == 1
    assert data["status"] == "fail"
    assert data["manifests"] == 0
    assert data["latest_manifest"] is None
    assert data["checked_files"] == 0
    assert located(data) == [("evidence", "no_manifest", None)]
    assert (path / "out" / "evidence-verify.json").is_file()


def test_tampered_line_is_located(root: Path) -> None:
    rewrite_field(root / OBS, 3, "answer_text", "篡改后的回答")
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(OBS, "file_modified", None), (OBS, "line_modified", 3)]


def test_deleted_line_is_located(root: Path) -> None:
    delete_line(root / OBS, 2)
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(OBS, "file_modified", None), (OBS, "line_deleted", 2)]


def test_reordered_lines_are_located(root: Path) -> None:
    swap_lines(root / OBS, 2, 4)
    code, data = verify(root)
    assert code == 1
    assert located(data) == [
        (OBS, "file_modified", None),
        (OBS, "line_reordered", 2),
        (OBS, "line_reordered", 4),
    ]


def test_appended_line_is_located(root: Path) -> None:
    append_line(root / OBS, new_observation(root, 77))
    code, data = verify(root)
    assert code == 1
    assert located(data) == [
        (OBS, "file_modified", None),
        (OBS, "line_inserted", OBSERVATIONS + 1),
    ]


def test_inserted_line_in_the_middle_is_located(root: Path) -> None:
    lines = lines_of(root / OBS)
    lines.insert(1, json.dumps(new_observation(root, 78)).encode())
    write_lines(root / OBS, lines)
    code, data = verify(root)
    assert code == 1
    assert located(data, "line_inserted") == [(OBS, "line_inserted", 2)]


def test_deleted_file_is_located(root: Path) -> None:
    (root / SNAPSHOT_PNG).unlink()
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(SNAPSHOT_PNG, "file_missing", None)]


def test_added_file_is_located(root: Path) -> None:
    extra = root / "snapshots" / "obs-000003" / "late.png"
    extra.parent.mkdir()
    extra.write_bytes(b"late synthetic snapshot")
    write_json(root / "out" / "metrics.json", {"kind": "metrics"})
    (root / "prompts" / "draft.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "snapshots" / ".DS_Store").write_bytes(b"finder")
    code, data = verify(root)
    assert code == 1
    assert located(data) == [("snapshots/obs-000003/late.png", "file_added", None)]


def test_new_jsonl_after_build_is_reported_as_added(root: Path) -> None:
    (root / "judgments.jsonl").rename(root / "judgments.backup")
    assert build(root, now=LATER)[0] == 1  # 判定文件从清单中消失，违反只追加
    (root / "judgments.backup").rename(root / "judgments.jsonl")
    code, data = verify(root)
    assert code == 1
    assert ("judgments.jsonl", "file_added", None) in located(data)


def test_modified_snapshot_is_located(root: Path) -> None:
    (root / SNAPSHOT_HTML).write_text("<p>换过的快照</p>\n", encoding="utf-8")
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(SNAPSHOT_HTML, "file_modified", None)]


def test_modified_manifest_is_located(root: Path) -> None:
    manifest = read_manifest(root, 1)
    manifest["seed"] = 7
    write_manifest(root, 1, manifest, rehash=False)
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(M1, "manifest_modified", None)]


def test_rehashed_manifest_breaks_the_link_from_the_next_one(root: Path) -> None:
    three_manifests(root)
    manifest = read_manifest(root, 2)
    manifest["seed"] = 7
    write_manifest(root, 2, manifest, rehash=True)
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(M3, "manifest_chain_broken", None)]


def test_deleted_middle_manifest_is_located(root: Path) -> None:
    three_manifests(root)
    (root / M2).unlink()
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(M2, "manifest_chain_broken", None)]


def test_deleted_latest_manifest_is_detected_through_the_audit_log(root: Path) -> None:
    three_manifests(root)
    (root / M3).unlink()
    code, data = verify(root)
    assert code == 1
    assert (M3, "manifest_chain_broken", None) in located(data)
    assert data["latest_manifest"]["sequence"] == 2


def test_inserted_manifest_without_audit_event_is_located(root: Path) -> None:
    manifest = read_manifest(root, 1)
    forged = dict(manifest)
    forged.update(
        sequence=2,
        manifest_id="auto-demo-manifest-0002",
        previous_manifest_hash=manifest["manifest_hash"],
        created_at=LATER,
    )
    write_manifest(root, 2, forged, rehash=True)
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(M2, "manifest_chain_broken", None)]
    assert "build_evidence" in data["problems"][0]["message"]


def test_renamed_manifest_is_located(root: Path) -> None:
    three_manifests(root)
    (root / M3).rename(root / "evidence" / "manifest-0004.json")
    code, data = verify(root)
    assert code == 1
    problems = located(data)
    assert (M3, "manifest_chain_broken", None) in problems
    assert ("evidence/manifest-0004.json", "manifest_chain_broken", None) in problems


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("manifest-0001.json", b"{broken"),
        ("manifest-0001.json", b'{"schema_version": "1.0"}'),
        ("manifest-0001.json", b"\xff\xfe"),
        ("manifest-1.json", b"{}"),
    ],
    ids=["broken-json", "not-contract", "not-utf8", "bad-name"],
)
def test_invalid_manifest_is_located(root: Path, name: str, content: bytes) -> None:
    (root / "evidence" / name).write_bytes(content)
    code, data = verify(root)
    assert code == 1
    assert (f"evidence/{name}", "manifest_invalid", None) in located(data)


def test_manifest_of_another_project_is_invalid(root: Path) -> None:
    manifest = read_manifest(root, 1)
    manifest["project_id"] = "other-demo"
    write_manifest(root, 1, manifest, rehash=True)
    code, data = verify(root)
    assert code == 1
    assert (M1, "manifest_invalid", None) in located(data)


def test_modified_audit_event_is_located(root: Path) -> None:
    rewrite_field(root / "audit.jsonl", 2, "reason", "事后改写的理由")
    code, data = verify(root)
    assert code == 1
    assert located(data) == [
        ("audit.jsonl", "file_modified", None),
        ("audit.jsonl", "line_modified", 2),
        ("audit.jsonl", "audit_chain", 2),
    ]


def test_unparseable_audit_line_is_reported(root: Path) -> None:
    lines = lines_of(root / "audit.jsonl")
    lines[0] = b"{not json"
    write_lines(root / "audit.jsonl", lines)
    code, data = verify(root)
    assert code == 1
    assert ("audit.jsonl", "audit_chain", 1) in located(data)


def test_modified_sealed_prompt_set_is_located(root: Path) -> None:
    path = root / "prompts" / "sealed" / "v0001.json"
    sealed = json.loads(path.read_text(encoding="utf-8"))
    sealed["prompts"][0]["text"] = "封存后被偷偷改掉的题目"
    write_json(path, sealed)
    code, data = verify(root)
    assert code == 1
    assert located(data) == [
        ("prompts/sealed/v0001.json", "file_modified", None),
        ("prompts/sealed/v0001.json", "sealed_prompt_set", None),
    ]


def test_sealed_prompt_set_problem_has_no_absolute_path(root: Path) -> None:
    path = root / "prompts" / "sealed" / "v0001.json"
    path.write_text("{broken", encoding="utf-8")
    code, data = verify(root)
    assert code == 1
    messages = [item["message"] for item in data["problems"] if item["kind"] == "sealed_prompt_set"]
    assert len(messages) == 1
    assert str(root) not in messages[0]
    assert ("prompts/sealed/v0001.json", "sealed_prompt_set", None) in located(data)


def test_misnumbered_sealed_version_is_reported(root: Path) -> None:
    sealed_dir = root / "prompts" / "sealed"
    (sealed_dir / "v0001.json").rename(sealed_dir / "v0002.json")
    code, data = verify(root)
    assert code == 1
    assert ("prompts/sealed/v0002.json", "sealed_prompt_set", None) in located(data)


def test_append_only_violation_between_builds_is_located(root: Path) -> None:
    rewrite_field(root / OBS, 2, "answer_text", "两次构建之间改写的历史行")
    assert build(root, now=LATER)[0] == 1
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(OBS, "append_only_violated", 2)]


def test_truncation_between_builds_is_located_at_the_first_missing_line(root: Path) -> None:
    lines = lines_of(root / OBS)
    write_lines(root / OBS, lines[:3])
    assert build(root, now=LATER)[0] == 1
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(OBS, "append_only_violated", 4)]


def test_sealed_file_change_between_builds_violates_append_only(root: Path) -> None:
    path = root / "prompts" / "sealed" / "v0001.json"
    path.write_bytes(path.read_bytes().replace(b"\n}", b',\n  "ext": {}\n}', 1))
    assert build(root, now=LATER)[0] == 1
    code, data = verify(root)
    assert code == 1
    assert located(data) == [("prompts/sealed/v0001.json", "append_only_violated", None)]


def test_raw_file_removed_between_builds_violates_append_only(root: Path) -> None:
    (root / "judgments.jsonl").unlink()
    assert build(root, now=LATER)[0] == 1
    code, data = verify(root)
    assert code == 1
    assert located(data) == [("judgments.jsonl", "append_only_violated", None)]


def test_lost_old_index_makes_append_only_unverifiable(root: Path) -> None:
    rewrite_field(root / OBS, 2, "answer_text", "改写")
    assert build(root, now=LATER)[0] == 1
    old_index = root / "evidence" / "index" / "observations.jsonl.lines"
    old_index.write_bytes(old_index.read_bytes()[:-65])  # 截掉最后一行哈希
    manifest = read_manifest(root, 2)
    for item in manifest["files"]:
        if item["path"] == "evidence/index/observations.jsonl.lines":
            item["sha256"] = sha(old_index.read_bytes())
            item["bytes"] = old_index.stat().st_size
            item["records"] = OBSERVATIONS - 1
    write_manifest(root, 2, manifest, rehash=True)
    code, data = verify(root)
    assert code == 1
    assert (OBS, "append_only_violated", None) in located(data)


def test_tampered_index_makes_lines_unlocatable(root: Path) -> None:
    index = root / "evidence" / "index" / "observations.jsonl.lines"
    index.write_bytes(index.read_bytes()[:-1])
    rewrite_field(root / OBS, 1, "answer_text", "改")
    code, data = verify(root)
    assert code == 1
    problems = located(data)
    assert ("evidence/index/observations.jsonl.lines", "file_modified", None) in problems
    assert (OBS, "file_modified", None) in problems
    assert not [item for item in problems if item[1].startswith("line_")]
    message = next(item["message"] for item in data["problems"] if item["file"] == OBS)
    assert "行索引不可用" in message


def test_inconsistent_index_is_reported_as_invalid_manifest(root: Path) -> None:
    index = root / "evidence" / "index" / "observations.jsonl.lines"
    content = index.read_bytes()
    swapped = content[65:130] + content[:65] + content[130:]
    index.write_bytes(swapped)
    manifest = read_manifest(root, 1)
    for item in manifest["files"]:
        if item["path"] == "evidence/index/observations.jsonl.lines":
            item["sha256"] = sha(swapped)
    write_manifest(root, 1, manifest, rehash=True)
    code, data = verify(root)
    assert code == 1
    assert located(data) == [("evidence/index/observations.jsonl.lines", "manifest_invalid", None)]


def test_trailing_newline_change_is_a_file_level_problem_only(root: Path) -> None:
    path = root / OBS
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(OBS, "file_modified", None)]
    assert "换行" in data["problems"][0]["message"]


def test_package_copied_elsewhere_still_verifies(root: Path, tmp_path: Path) -> None:
    three_manifests(root)
    code, original = verify(root)
    assert code == 0, original["problems"]
    copy = tmp_path / "elsewhere" / "复核目录"
    shutil.copytree(root, copy)
    code, copied = verify(copy)
    assert code == 0, copied["problems"]
    assert (copy / "out" / "evidence-verify.json").read_bytes() == (
        root / "out" / "evidence-verify.json"
    ).read_bytes()


def test_verify_works_without_readable_project_file(root: Path) -> None:
    (root / "project.json").write_text("{broken", encoding="utf-8")
    code, data = verify(root)
    assert code == 1
    assert data["project_id"] == "auto-demo"
    assert located(data) == [("project.json", "file_modified", None)]


def test_verify_text_output(root: Path) -> None:
    code, out, _ = run_cli("evidence", "verify", str(root))
    assert code == 0
    assert "证据校验通过" in out
    assert "不能证明采集时间真实" in out
    delete_line(root / OBS, 2)
    code, out, _ = run_cli("evidence", "verify", str(root))
    assert code == 1
    assert "第 2 行" in out
    assert "observations.jsonl" in out


def test_verify_rejects_missing_project(tmp_path: Path) -> None:
    code, _, _ = run_cli("evidence", "verify", str(tmp_path / "missing"))
    assert code == 2


def test_problems_are_sorted_and_have_exactly_four_fields(root: Path) -> None:
    delete_line(root / OBS, 4)
    (root / SNAPSHOT_PNG).unlink()
    rewrite_field(root / "annotations.jsonl", 1, "sentiment", "positive")
    data = verify_evidence(root)
    keys = [(item["file"], item["line"] or 0) for item in data["problems"]]
    assert keys == sorted(keys)
    assert all(set(item) == {"file", "kind", "line", "message"} for item in data["problems"])


def _single_edit_project(base: Path, lines: list[str]) -> Path:
    path = base / "proj"
    make_evidence_project(path)
    (path / OBS).unlink()
    append_jsonl(path / OBS, [{"n": index, "text": text} for index, text in enumerate(lines)])
    assert build(path)[0] == 0
    return path


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    lines=st.lists(st.text(max_size=8), min_size=1, max_size=12),
    data=st.data(),
)
def test_random_single_edit_is_always_reported_and_located(
    lines: list[str], data: st.DataObject
) -> None:
    kind = data.draw(st.sampled_from(["line_modified", "line_deleted", "line_inserted"]))
    upper = len(lines) + 1 if kind == "line_inserted" else len(lines)
    position = data.draw(st.integers(min_value=1, max_value=upper))
    with tempfile.TemporaryDirectory() as temp:
        root = _single_edit_project(Path(temp), lines)
        current = lines_of(root / OBS)
        fresh = json.dumps({"n": -1, "text": "新"}).encode()
        if kind == "line_modified":
            current[position - 1] = fresh
        elif kind == "line_deleted":
            del current[position - 1]
        else:
            current.insert(position - 1, fresh)
        write_lines(root / OBS, current)
        result = verify_evidence(root)
    assert result["status"] == "fail"
    assert [item for item in located(result) if item[1].startswith("line_")] == [
        (OBS, kind, position)
    ]


def test_snapshot_removed_between_builds_violates_append_only(root: Path) -> None:
    (root / SNAPSHOT_PNG).unlink()
    assert build(root, now=LATER)[0] == 1
    code, data = verify(root)
    assert code == 1
    assert located(data) == [(SNAPSHOT_PNG, "append_only_violated", None)]


def test_duplicate_manifest_number_is_reported(root: Path) -> None:
    shutil.copy(root / M1, root / "evidence" / "manifest-00001.json")
    code, data = verify(root)
    assert code == 1
    assert ("evidence/manifest-0001.json", "manifest_chain_broken", None) in located(data)


def test_missing_audit_log_is_reported(root: Path) -> None:
    (root / "audit.jsonl").unlink()
    code, data = verify(root)
    assert code == 1
    assert located(data) == [
        ("audit.jsonl", "file_missing", None),
        (M1, "manifest_chain_broken", None),
    ]


def test_project_file_that_is_not_an_object(root: Path) -> None:
    (root / "project.json").write_text("[]", encoding="utf-8")
    code, data = verify(root)
    assert code == 1
    assert data["project_id"] == "auto-demo"
    assert located(data) == [("project.json", "file_modified", None)]
