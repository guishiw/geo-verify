"""`geo-verify evidence build`：清单覆盖范围、逐行索引、清单链、无变化重建与依赖锁。

期望值来自测试自己写入的文件：文件哈希、行哈希用 hashlib 独立计算，清单哈希用 rfc8785 独立计算。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import rfc8785
from conftest import run_cli, run_cli_json
from helpers_evidence import (  # noqa: F401  (synthetic_installed_lock 是自动生效的 fixture)
    ACTOR,
    ANNOTATIONS,
    FAKE_LOCK,
    JUDGMENTS,
    LATER,
    NOW,
    OBSERVATIONS,
    SNAPSHOT_HTML,
    SNAPSHOT_PNG,
    append_line,
    build,
    independent_manifest_hash,
    lines_of,
    make_evidence_project,
    read_manifest,
    rewrite_field,
    sha,
    synthetic_installed_lock,
    verify,
    write_manifest,
)

from geo_verify import __version__, audit, project, spec
from geo_verify.evidence import environment
from geo_verify.evidence.manifest import build_evidence, content_digest, index_as_of
from geo_verify.evidence.timestamp import NullTimestampProvider, TimestampProvider
from geo_verify.jsonl import read_jsonl, write_json

INDEXED = {
    "entities.jsonl": None,
    "observations.jsonl": OBSERVATIONS,
    "annotations.jsonl": ANNOTATIONS,
    "judgments.jsonl": JUDGMENTS,
    "audit.jsonl": None,
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    make_evidence_project(path)
    return path


def files_of(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["path"]: item for item in manifest["files"]}


def test_first_build_writes_a_contract_valid_manifest(root: Path) -> None:
    proj = json.loads((root / "project.json").read_text(encoding="utf-8"))
    code, data = build(root)
    assert code == 0, data
    assert data["created"] is True
    assert data["unchanged"] is False
    assert data["manifest"] == "evidence/manifest-0001.json"
    manifest = read_manifest(root, 1)
    assert spec.iter_issues("manifest", manifest) == []
    assert manifest["sequence"] == 1
    assert manifest["previous_manifest_hash"] is None
    assert manifest["manifest_id"] == "auto-demo-manifest-0001"
    assert manifest["project_id"] == "auto-demo"
    assert manifest["created_at"] == NOW
    assert manifest["tool"] == {"name": "geo-verify", "version": __version__}
    assert manifest["code_version"].startswith(__version__)
    assert manifest["seed"] == proj["preregistration"]["statistics"]["seed"]
    assert manifest["definitions"] == {
        item["metric_id"]: item["definition_version"] for item in proj["preregistration"]["metrics"]
    }
    assert manifest["timestamp_token"] is None
    assert manifest["synthetic"] is True
    assert manifest["manifest_hash"] == independent_manifest_hash(manifest)
    assert data["manifest_hash"] == manifest["manifest_hash"]
    assert data["sequence"] == 1


def test_manifest_covers_exactly_the_evidence_files_with_roles(root: Path) -> None:
    (root / "prompts" / "draft.jsonl").write_text('{"query_id":"Q001"}\n', encoding="utf-8")
    write_json(root / "out" / "metrics.json", {"kind": "metrics"})
    (root / "snapshots" / ".DS_Store").write_bytes(b"finder metadata")
    (root / "notes.txt").write_text("不在证据范围内", encoding="utf-8")
    build(root)
    files = files_of(read_manifest(root, 1))
    expected_roles = {
        "project.json": "config",
        "entities.jsonl": "raw",
        "observations.jsonl": "raw",
        "annotations.jsonl": "raw",
        "judgments.jsonl": "raw",
        "audit.jsonl": "raw",
        "prompts/sealed/v0001.json": "raw",
        SNAPSHOT_PNG: "snapshot",
        SNAPSHOT_HTML: "snapshot",
        "recheck/plan.json": "derived",
        "evidence/lock/uv.lock": "lock",
        **{f"evidence/index/{name}.lines": "index" for name in INDEXED},
    }
    assert {path: item["role"] for path, item in files.items()} == expected_roles
    assert [item["path"] for item in read_manifest(root, 1)["files"]] == sorted(expected_roles)
    for path, item in files.items():
        content = (root / path).read_bytes()
        assert item["sha256"] == sha(content), path
        assert item["bytes"] == len(content), path


def test_each_jsonl_gets_a_line_index_with_one_hash_per_line(root: Path) -> None:
    build(root)
    files = files_of(read_manifest(root, 1))
    for name, count in INDEXED.items():
        entry = files[name]
        source_lines = lines_of(root / name)
        if count is not None:
            assert len(source_lines) == count
        assert entry["records"] == len(source_lines)
        assert entry["line_index"] == f"evidence/index/{name}.lines"
        index = files[entry["line_index"]]
        expected = "".join(sha(line) + "\n" for line in source_lines).encode("ascii")
        assert (root / entry["line_index"]).read_bytes() == expected
        assert index["records"] == len(source_lines)
        assert index["line_index"] is None
    for path in ("project.json", "prompts/sealed/v0001.json", SNAPSHOT_PNG):
        assert files[path]["records"] is None
        assert files[path]["line_index"] is None


def test_build_writes_its_audit_event_before_the_manifest(root: Path) -> None:
    before = len(project.load_audit(root))
    code, data = build(root, "--actor", ACTOR)
    assert code == 0
    events = project.load_audit(root)
    assert len(events) == before + 1
    event = events[-1]
    assert event["action"] == "build_evidence"
    assert event["actor"] == ACTOR
    assert event["occurred_at"] == NOW
    assert event["details"]["sequence"] == 1
    assert event["details"]["manifest"] == "evidence/manifest-0001.json"
    assert event["before_hash"] is None
    assert data["audit_event_id"] == event["event_id"]
    assert audit.verify_chain(events) == []
    # 清单覆盖的是“已含本次事件”的审计日志，生成后立即校验仍然通过
    assert files_of(read_manifest(root, 1))["audit.jsonl"]["records"] == before + 1
    code, result = verify(root)
    assert code == 0, result["problems"]


def test_default_actor_is_recorded_when_not_given(root: Path) -> None:
    build(root)
    assert project.load_audit(root)[-1]["actor"] == "geo-verify evidence build（未指明操作人）"


def test_rebuild_without_changes_creates_nothing(root: Path) -> None:
    build(root)
    audit_before = (root / "audit.jsonl").read_bytes()
    code, data = build(root, now=LATER)
    assert code == 0
    assert data["created"] is False
    assert data["unchanged"] is True
    assert data["manifest"] == "evidence/manifest-0001.json"
    assert data["audit_event_id"] is None
    assert not (root / "evidence" / "manifest-0002.json").exists()
    assert (root / "audit.jsonl").read_bytes() == audit_before
    code, out, _ = run_cli("evidence", "build", str(root), "--now", LATER)
    assert code == 0
    assert "无变化" in out


def test_legal_append_between_builds_extends_the_chain(root: Path) -> None:
    build(root)
    first = read_manifest(root, 1)
    first_index = (root / "evidence/index/observations.jsonl.lines").read_bytes()
    record = json.loads(lines_of(root / "observations.jsonl")[0])
    record.update(observation_id="obs-000099", trace_id="trace-000099")
    append_line(root / "observations.jsonl", record)
    code, data = build(root, now=LATER)
    assert code == 0, data
    assert data["created"] is True
    assert data["append_only_violations"] == []
    second = read_manifest(root, 2)
    assert second["sequence"] == 2
    assert second["previous_manifest_hash"] == first["manifest_hash"]
    assert second["created_at"] == LATER
    assert files_of(second)["observations.jsonl"]["records"] == OBSERVATIONS + 1
    assert files_of(second)["observations.jsonl"]["line_index"] == (
        "evidence/index/observations.jsonl.lines"
    )
    grown = (root / "evidence/index/observations.jsonl.lines").read_bytes()
    assert grown.startswith(first_index)
    assert len(grown) > len(first_index)
    assert project.load_audit(root)[-1]["before_hash"] == first["manifest_hash"]
    code, result = verify(root)
    assert code == 0, result["problems"]
    assert result["manifests"] == 2


def test_history_rewrite_between_builds_is_flagged_and_keeps_old_index(root: Path) -> None:
    build(root)
    old_index = (root / "evidence/index/observations.jsonl.lines").read_bytes()
    rewrite_field(root / "observations.jsonl", 2, "answer_text", "改写后的回答")
    code, data = build(root, now=LATER)
    assert code == 1
    assert data["created"] is True
    assert [(item["file"], item["line"]) for item in data["append_only_violations"]] == [
        ("observations.jsonl", 2)
    ]
    second = files_of(read_manifest(root, 2))
    assert second["observations.jsonl"]["line_index"] == (
        "evidence/index/observations.jsonl.0002.lines"
    )
    assert (root / "evidence/index/observations.jsonl.lines").read_bytes() == old_index
    assert second["evidence/index/observations.jsonl.lines"]["role"] == "index"
    code, out, _ = run_cli("evidence", "verify", str(root))
    assert code == 1
    assert "只追加" in out


def test_same_input_and_now_give_byte_identical_manifests(tmp_path: Path) -> None:
    first, second = tmp_path / "a" / "proj", tmp_path / "b" / "other-name"
    make_evidence_project(first)
    make_evidence_project(second)
    assert build(first)[0] == 0
    assert build(second)[0] == 0
    for relative in (
        "evidence/manifest-0001.json",
        "evidence/index/observations.jsonl.lines",
        "audit.jsonl",
    ):
        assert (first / relative).read_bytes() == (second / relative).read_bytes(), relative


def test_project_lock_file_is_preferred(root: Path) -> None:
    (root / "uv.lock").write_bytes(b"version = 1\n# project lock (synthetic)\n")
    code, data = build(root)
    assert code == 0
    manifest = read_manifest(root, 1)
    files = files_of(manifest)
    assert files["uv.lock"]["role"] == "lock"
    assert manifest["lock_hash"] == sha((root / "uv.lock").read_bytes())
    assert not (root / "evidence" / "lock").exists()
    assert data["lock"]["origin"] == "project"
    assert data["lock"]["path"] == "uv.lock"


def test_installed_lock_is_copied_into_the_package(root: Path) -> None:
    code, data = build(root)
    assert code == 0
    assert (root / "evidence/lock/uv.lock").read_bytes() == FAKE_LOCK
    assert read_manifest(root, 1)["lock_hash"] == sha(FAKE_LOCK)
    assert data["lock"] == {
        "origin": "installed",
        "path": "evidence/lock/uv.lock",
        "lock_hash": sha(FAKE_LOCK),
        "note": data["lock"]["note"],
    }
    assert "安装元数据" in data["lock"]["note"]


def test_missing_lock_is_null_and_explained(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environment, "installed_lock_path", lambda: None)
    code, data = build(root)
    assert code == 0
    manifest = read_manifest(root, 1)
    assert manifest["lock_hash"] is None
    assert not any(item["role"] == "lock" for item in manifest["files"])
    assert data["lock"]["origin"] is None
    assert data["lock"]["lock_hash"] is None
    assert "未找到依赖锁" in data["lock"]["note"]
    code, out, _ = run_cli("evidence", "build", str(root), "--now", LATER)
    assert "未找到依赖锁" in out


def test_null_timestamp_provider_returns_nothing() -> None:
    provider: TimestampProvider = NullTimestampProvider()
    assert provider.stamp("0" * 64) is None
    assert provider.name == "none"


class RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.digests: list[str] = []

    def stamp(self, digest: str) -> dict[str, Any] | None:
        self.digests.append(digest)
        return {"provider": self.name, "digest": digest, "token": "synthetic-token"}


def test_timestamp_provider_stamps_the_content_digest(root: Path) -> None:
    provider = RecordingProvider()
    data = build_evidence(root, now=NOW, actor=ACTOR, provider=provider)
    manifest = read_manifest(root, 1)
    assert manifest["timestamp_token"] == {
        "provider": "recording",
        "digest": provider.digests[0],
        "token": "synthetic-token",
    }
    unsigned = {
        key: value
        for key, value in manifest.items()
        if key not in ("manifest_hash", "timestamp_token")
    }
    assert provider.digests == [content_digest(manifest)]
    assert content_digest(manifest) == sha(rfc8785.dumps(unsigned))
    assert manifest["manifest_hash"] == independent_manifest_hash(manifest)
    assert data["timestamp"]["provider"] == "recording"
    assert verify(root)[0] == 0


def test_build_refuses_to_extend_a_tampered_manifest(root: Path) -> None:
    build(root)
    manifest = read_manifest(root, 1)
    manifest["seed"] = 1
    write_manifest(root, 1, manifest, rehash=False)
    append_line(root / "annotations.jsonl", {"synthetic": True})
    code, data = build(root, now=LATER)
    assert code == 1
    assert data["error"] == "check_failed"
    assert "manifest-0001.json" in data["message"]
    assert not (root / "evidence" / "manifest-0002.json").exists()


@pytest.mark.parametrize(
    "content",
    [b"{not json", b'{"schema_version": "1.0"}', None],
    ids=["broken-json", "not-contract", "renamed"],
)
def test_build_refuses_unreadable_or_misnumbered_latest_manifest(
    root: Path, content: bytes | None
) -> None:
    build(root)
    path = root / "evidence" / "manifest-0001.json"
    if content is None:
        path.rename(root / "evidence" / "manifest-0002.json")
    else:
        path.write_bytes(content)
    code, data = build(root, now=LATER)
    assert code == 1
    assert data["error"] == "check_failed"


def test_build_rejects_missing_project(tmp_path: Path) -> None:
    code, data = build(tmp_path / "missing")
    assert code == 2
    assert data["error"] == "input_error"


def test_build_rejects_bad_now(root: Path) -> None:
    code, data = run_cli_json("evidence", "build", str(root), "--now", "2026-10-09 01:00")
    assert code == 2
    assert data["error"] == "input_error"
    assert not (root / "evidence" / "manifest-0001.json").exists()


def test_build_text_output_mentions_proof_boundary(root: Path) -> None:
    code, out, _ = run_cli("evidence", "build", str(root), "--now", NOW)
    assert code == 0
    assert "evidence/manifest-0001.json" in out
    assert "不能证明采集时间真实" in out


def test_stray_index_file_is_not_overwritten(root: Path) -> None:
    # 首次构建前 index 目录里已有一份来历不明的行索引：不覆盖它，新索引另起一代
    stray = root / "evidence" / "index" / "observations.jsonl.lines"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"stray\n")
    code, _ = build(root)
    assert code == 0
    files = files_of(read_manifest(root, 1))
    assert files["observations.jsonl"]["line_index"] == (
        "evidence/index/observations.jsonl.0001.lines"
    )
    assert stray.read_bytes() == b"stray\n"
    assert files["evidence/index/observations.jsonl.lines"]["records"] == 1
    assert verify(root)[0] == 0


def test_raw_json_change_and_raw_file_removal_are_flagged_at_build(root: Path) -> None:
    build(root)
    sealed = root / "prompts" / "sealed" / "v0001.json"
    sealed.write_bytes(sealed.read_bytes().replace(b"\n}", b',\n  "ext": {}\n}', 1))
    (root / "judgments.jsonl").unlink()
    code, data = build(root, now=LATER)
    assert code == 1
    assert sorted((item["file"], item["line"]) for item in data["append_only_violations"]) == [
        ("judgments.jsonl", None),
        ("prompts/sealed/v0001.json", None),
    ]


def test_unreadable_previous_index_is_flagged_at_build(root: Path) -> None:
    build(root)
    (root / "evidence/index/annotations.jsonl.lines").write_bytes(b"")
    code, data = build(root, now=LATER)
    assert code == 1
    assert [(item["file"], item["line"]) for item in data["append_only_violations"]] == [
        ("annotations.jsonl", None)
    ]
    assert files_of(read_manifest(root, 2))["annotations.jsonl"]["line_index"] == (
        "evidence/index/annotations.jsonl.0002.lines"
    )


def test_jsonl_that_appears_later_gets_a_base_index(root: Path) -> None:
    # 观测文件后来才出现：首次清单没有它，第二次构建为它新建索引
    observations = root / "observations.jsonl"
    saved = observations.read_bytes()
    observations.unlink()
    build(root)
    observations.write_bytes(saved)
    code, data = build(root, now=LATER)
    assert code == 0, data
    assert files_of(read_manifest(root, 2))["observations.jsonl"]["line_index"] == (
        "evidence/index/observations.jsonl.lines"
    )
    assert read_jsonl(root / "observations.jsonl")


def test_snapshot_removal_or_replacement_is_flagged_at_build(root: Path) -> None:
    build(root)
    (root / SNAPSHOT_PNG).unlink()
    (root / SNAPSHOT_HTML).write_text("<p>换过的快照</p>\n", encoding="utf-8")
    code, data = build(root, now=LATER)
    assert code == 1
    assert sorted((item["file"], item["line"]) for item in data["append_only_violations"]) == [
        (SNAPSHOT_PNG, None),
        (SNAPSHOT_HTML, None),
    ]


def test_new_snapshot_between_builds_is_fine(root: Path) -> None:
    build(root)
    extra = root / "snapshots" / "obs-000003" / "late.png"
    extra.parent.mkdir()
    extra.write_bytes(b"late synthetic snapshot")
    code, data = build(root, now=LATER)
    assert code == 0, data
    assert data["append_only_violations"] == []


def test_build_ignores_misnamed_manifest_files(root: Path) -> None:
    build(root)
    (root / "evidence" / "manifest-1.json").write_text("{}", encoding="utf-8")
    append_line(root / "annotations.jsonl", {"synthetic": True})
    code, data = build(root, now=LATER)
    assert code == 0, data
    assert data["sequence"] == 2


def test_build_refuses_manifest_of_another_project(root: Path) -> None:
    build(root)
    manifest = read_manifest(root, 1)
    manifest["project_id"] = "other-demo"
    write_manifest(root, 1, manifest, rehash=True)
    code, data = build(root, now=LATER)
    assert code == 1
    assert "另一个项目" in data["message"]


def test_non_synthetic_project_has_no_synthetic_flag(tmp_path: Path) -> None:
    path = tmp_path / "proj"
    make_evidence_project(path, synthetic=False)
    assert build(path)[0] == 0
    assert "synthetic" not in read_manifest(path, 1)
    code, data = verify(path)
    assert code == 0, data["problems"]
    assert "synthetic" not in data


def _index_manifest(content: bytes, records: int | None) -> dict[str, Any]:
    return {
        "files": [
            {
                "path": "evidence/index/x.jsonl.lines",
                "sha256": sha(content),
                "bytes": len(content),
                "role": "index",
                "records": records,
                "line_index": None,
            }
        ]
    }


def test_index_as_of_reads_the_recorded_prefix_only(tmp_path: Path) -> None:
    first = sha(b"a") + "\n"
    content = first.encode("ascii")
    path = tmp_path / "evidence" / "index" / "x.jsonl.lines"
    path.parent.mkdir(parents=True)
    path.write_bytes(content + (sha(b"b") + "\n").encode("ascii"))
    manifest = _index_manifest(content, 1)
    assert index_as_of(tmp_path, manifest, "evidence/index/x.jsonl.lines") == [sha(b"a")]
    assert index_as_of(tmp_path, manifest, "evidence/index/other.lines") is None
    assert (
        index_as_of(tmp_path, _index_manifest(content, 2), "evidence/index/x.jsonl.lines") is None
    )
    path.unlink()
    assert index_as_of(tmp_path, manifest, "evidence/index/x.jsonl.lines") is None


def test_index_as_of_rejects_malformed_content_even_with_matching_hash(tmp_path: Path) -> None:
    content = b"not an index\n"
    path = tmp_path / "evidence" / "index" / "x.jsonl.lines"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    assert (
        index_as_of(tmp_path, _index_manifest(content, 1), "evidence/index/x.jsonl.lines") is None
    )
