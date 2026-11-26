"""证据包测试的公共构造器与文件改写工具。

项目数据直接按契约拼装（不经过被测的 evidence 实现）；改写工具按“第几行”操作，
测试里期望的行号就是这里传入的行号，不取自程序输出。全部数据为虚构。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import rfc8785
from conftest import run_cli_json
from factory import make_annotation, make_judgment, make_observation, write_records
from helpers_basic import sealed_project

from geo_verify import audit
from geo_verify.evidence import environment
from geo_verify.jsonl import write_json

NOW = "2026-10-09T01:00:00Z"
LATER = "2026-10-10T01:00:00Z"
ACTOR = "证据管理员（虚构）"
OBSERVATIONS = 5
ANNOTATIONS = 3
JUDGMENTS = 2
SNAPSHOT_PNG = "snapshots/obs-000001/answer.png"
SNAPSHOT_HTML = "snapshots/obs-000002/page.html"
FAKE_LOCK = b'# synthetic lock for tests\nversion = 1\n[[package]]\nname = "demo"\n'


@pytest.fixture(autouse=True)
def synthetic_installed_lock(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """让“安装元数据里的锁文件”固定为一份合成锁，测试结果不随运行环境变化。"""
    path = tmp_path_factory.mktemp("installed") / "uv.lock"
    path.write_bytes(FAKE_LOCK)
    monkeypatch.setattr(environment, "installed_lock_path", lambda: path)
    return path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def independent_manifest_hash(manifest: dict[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    return sha(rfc8785.dumps(body))


def make_evidence_project(root: Path, **project_overrides: Any) -> dict[str, Any]:
    """项目配置、实体、一版封存题库、5 条观测、3 条标注、2 条判定、2 个快照、1 个复核计划、2 个审计事件。"""
    proj, sealed = sealed_project(root, **project_overrides)
    observations = [
        make_observation(proj, sealed, number, query_id=f"Q{number:03d}")
        for number in range(1, OBSERVATIONS + 1)
    ]
    annotations = [make_annotation(observations[index], index + 1) for index in range(ANNOTATIONS)]
    judgments = [make_judgment(observations[index], index + 1) for index in range(JUDGMENTS)]
    write_records(root, observations=observations, annotations=annotations, judgments=judgments)
    (root / SNAPSHOT_PNG).parent.mkdir(parents=True)
    (root / SNAPSHOT_PNG).write_bytes(b"\x89PNG synthetic snapshot 1")
    (root / SNAPSHOT_HTML).parent.mkdir(parents=True)
    (root / SNAPSHOT_HTML).write_text("<p>虚构快照：澜序 S7</p>\n", encoding="utf-8")
    write_json(root / "recheck" / "plan.json", {"kind": "recheck_plan", "synthetic": True})
    for action in ("seal", "import"):
        audit.append_event(
            root,
            actor=ACTOR,
            action=action,
            target_type="test",
            target_id=action,
            reason="测试构造（虚构）",
            occurred_at="2026-10-08T00:00:00Z",
            synthetic=True,
        )
    return proj


def build(root: Path, *extra: str, now: str = NOW) -> tuple[int, dict[str, Any]]:
    return run_cli_json("evidence", "build", str(root), "--now", now, *extra)


def verify(root: Path) -> tuple[int, dict[str, Any]]:
    return run_cli_json("evidence", "verify", str(root))


def located(data: dict[str, Any], kind: str | None = None) -> list[tuple[str, str, int | None]]:
    """问题列表压缩成 (file, kind, line)，便于断言。"""
    return [
        (item["file"], item["kind"], item["line"])
        for item in data["problems"]
        if kind is None or item["kind"] == kind
    ]


def lines_of(path: Path) -> list[bytes]:
    data = path.read_bytes()
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    return parts


def write_lines(path: Path, lines: list[bytes]) -> None:
    path.write_bytes(b"".join(line + b"\n" for line in lines))


def rewrite_field(path: Path, line: int, field: str, value: Any) -> None:
    """把第 line 行 JSON 对象的某个字段改掉（模拟篡改历史记录）。"""
    lines = lines_of(path)
    record = json.loads(lines[line - 1])
    record[field] = value
    lines[line - 1] = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    write_lines(path, lines)


def delete_line(path: Path, line: int) -> None:
    lines = lines_of(path)
    del lines[line - 1]
    write_lines(path, lines)


def swap_lines(path: Path, first: int, second: int) -> None:
    lines = lines_of(path)
    lines[first - 1], lines[second - 1] = lines[second - 1], lines[first - 1]
    write_lines(path, lines)


def append_line(path: Path, record: dict[str, Any]) -> None:
    with path.open("ab") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True).encode() + b"\n")


def read_manifest(root: Path, sequence: int) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (root / "evidence" / f"manifest-{sequence:04d}.json").read_text(encoding="utf-8")
    )
    return data


def write_manifest(root: Path, sequence: int, manifest: dict[str, Any], *, rehash: bool) -> None:
    if rehash:
        manifest["manifest_hash"] = independent_manifest_hash(manifest)
    write_json(root / "evidence" / f"manifest-{sequence:04d}.json", manifest)
