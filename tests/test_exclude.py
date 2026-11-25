"""`geo-verify exclude`：剔除只写审计事件，不改动原始观测。"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import run_cli, run_cli_json
from factory import make_observation, write_records
from helpers_basic import sealed_project

from geo_verify import audit, project

NOW = "2026-10-09T03:00:00Z"
ACTOR = "数据管理员（虚构）"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    proj, sealed = sealed_project(path)
    write_records(
        path,
        observations=[make_observation(proj, sealed, number) for number in range(1, 5)],
    )
    return path


def exclude(root: Path, *extra: str) -> tuple[int, dict]:
    return run_cli_json(
        "exclude", str(root), "--actor", ACTOR, "--reason", "重复导入", "--now", NOW, *extra
    )


def test_exclude_writes_audit_event_and_leaves_observations_untouched(root: Path) -> None:
    before = (root / "observations.jsonl").read_bytes()
    code, data = exclude(root, "--rule-id", "EX-DUP", "--ids", "obs-000002,obs-000004")
    assert code == 0
    assert data["count"] == 2
    assert data["observation_ids"] == ["obs-000002", "obs-000004"]
    assert data["rule_registered"] is True
    assert (root / "observations.jsonl").read_bytes() == before

    events = project.load_audit(root)
    assert len(events) == 1
    event = events[0]
    assert event["action"] == "exclude"
    assert event["actor"] == ACTOR
    assert event["reason"] == "重复导入"
    assert event["occurred_at"] == NOW
    assert event["details"]["rule_id"] == "EX-DUP"
    assert event["details"]["count"] == 2
    assert event["details"]["observation_ids"] == ["obs-000002", "obs-000004"]
    assert audit.verify_chain(events) == []
    assert project.exclusions(events) == {"obs-000002": "EX-DUP", "obs-000004": "EX-DUP"}


def test_unknown_observation_is_an_input_error(root: Path) -> None:
    code, data = exclude(root, "--rule-id", "EX-DUP", "--ids", "obs-000001,obs-999999")
    assert code == 2
    assert data["error"] == "input_error"
    assert "obs-999999" in data["message"]
    assert project.load_audit(root) == []


def test_unregistered_rule_is_recorded_but_flagged(root: Path) -> None:
    code, data = exclude(root, "--rule-id", "EX-ADHOC", "--ids", "obs-000001")
    assert code == 0
    assert data["rule_registered"] is False
    event = project.load_audit(root)[0]
    assert event["details"]["rule_id"] == "EX-ADHOC"
    assert event["details"]["rule_registered"] is False
    code, out, _ = run_cli(
        "exclude",
        str(root),
        "--rule-id",
        "EX-ADHOC2",
        "--ids",
        "obs-000003",
        "--actor",
        ACTOR,
        "--reason",
        "临时剔除",
        "--now",
        NOW,
    )
    assert code == 0
    assert "未预先登记" in out
    assert "不通过" in out


def test_ids_file_and_duplicates(root: Path, tmp_path: Path) -> None:
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("obs-000001\n\nobs-000003, obs-000001\n", encoding="utf-8")
    code, data = exclude(root, "--rule-id", "EX-DUP", "--ids-file", str(ids_file))
    assert code == 0
    assert data["observation_ids"] == ["obs-000001", "obs-000003"]
    assert project.load_audit(root)[0]["details"]["count"] == 2


def test_already_excluded_observations_are_not_counted_twice(root: Path) -> None:
    assert exclude(root, "--rule-id", "EX-DUP", "--ids", "obs-000001")[0] == 0
    code, data = exclude(root, "--rule-id", "EX-DUP", "--ids", "obs-000001,obs-000002")
    assert code == 0
    assert data["observation_ids"] == ["obs-000002"]
    assert data["already_excluded"] == ["obs-000001"]
    events = project.load_audit(root)
    assert [event["details"]["count"] for event in events] == [1, 1]

    code, data = exclude(root, "--rule-id", "EX-DUP", "--ids", "obs-000001,obs-000002")
    assert code == 0
    assert data["count"] == 0
    assert len(project.load_audit(root)) == 2, "没有新剔除的观测时不写事件"
    code, out, _ = run_cli(
        "exclude", str(root), "--rule-id", "EX-DUP", "--ids", "obs-000001", "--actor", ACTOR,
        "--reason", "重复导入",
    )  # fmt: skip
    assert code == 0
    assert "此前已被剔除" in out


def test_argument_errors(root: Path, tmp_path: Path) -> None:
    base = ("exclude", str(root), "--rule-id", "EX-DUP", "--actor", ACTOR)
    assert run_cli(*base, "--reason", "x")[0] == 2, "必须给 --ids 或 --ids-file"
    assert run_cli(*base, "--ids", "obs-000001")[0] == 2, "必须给 --reason"
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("obs-000001\n", encoding="utf-8")
    code, _, _ = run_cli(*base, "--reason", "x", "--ids", "obs-000001", "--ids-file", str(ids_file))
    assert code == 2, "--ids 与 --ids-file 互斥"
    code, data = run_cli_json(*base, "--reason", "x", "--ids", " , ")
    assert code == 2
    assert "没有给出" in data["message"]
    code, data = run_cli_json(*base, "--reason", "x", "--ids-file", str(tmp_path / "none.txt"))
    assert code == 2
    code, data = run_cli_json(*base, "--reason", "  ", "--ids", "obs-000001")
    assert code == 2
    assert "理由" in data["message"]
    assert project.load_audit(root) == []
