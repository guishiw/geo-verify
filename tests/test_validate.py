"""`geo-verify validate` 命令测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import load_examples, run_cli, run_cli_json

from geo_verify import jsonl


def test_cli_validate_json_and_exit_codes(tmp_path: Path) -> None:
    examples = load_examples("observation")
    good = tmp_path / "good.jsonl"
    jsonl.append_jsonl(good, examples["valid"])
    code, data = run_cli_json("validate", str(good), "--type", "observation")
    assert code == 0
    assert data["records"] == 2
    assert data["invalid"] == 0

    bad = tmp_path / "bad.jsonl"
    jsonl.append_jsonl(bad, [examples["valid"][0], examples["invalid"][0]["instance"]])
    code, data = run_cli_json("validate", str(bad), "--type", "observation")
    assert code == 1
    assert data["invalid"] == 1
    assert data["problems"][0]["where"] == "第 2 行"

    code, out, _ = run_cli("validate", str(bad), "--type", "observation")
    assert code == 1
    assert "未通过" in out
    assert "第 2 行" in out


def test_cli_validate_single_json_and_list(tmp_path: Path) -> None:
    project = tmp_path / "project.json"
    jsonl.write_json(project, load_examples("project")["valid"][0])
    code, out, _ = run_cli("validate", str(project), "--type", "project")
    assert code == 0
    assert "通过" in out
    entities = tmp_path / "entities.json"
    jsonl.write_json(entities, load_examples("entity")["valid"])
    code, data = run_cli_json("validate", str(entities), "--type", "entity")
    assert code == 0
    assert data["records"] == 2


def test_cli_validate_input_errors(tmp_path: Path) -> None:
    code, data = run_cli_json("validate", str(tmp_path / "missing.json"), "--type", "project")
    assert code == 2
    assert data["error"] == "input_error"
    code, _, err = run_cli("validate", str(tmp_path / "missing.json"), "--type", "nope")
    assert code == 2
    assert "未知契约" in err


def test_cli_validate_truncates_long_problem_lists(tmp_path: Path) -> None:
    bad = tmp_path / "many.jsonl"
    jsonl.append_jsonl(bad, [{"schema_version": "1.0"} for _ in range(60)])
    code, data = run_cli_json("validate", str(bad), "--type", "entity")
    assert code == 1
    assert data["invalid"] == 60
    assert len(data["problems"]) == 50
    assert data["problems_truncated"] is True
    code, out, _ = run_cli("validate", str(bad), "--type", "entity")
    assert "仅列出前 50 条" in out


def test_cli_internal_error_returns_4(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from geo_verify.commands import validate

    def boom(path: Path) -> list[object]:
        raise RuntimeError("模拟内部故障")

    monkeypatch.setattr(validate, "_records", boom)
    code, _, err = run_cli("validate", str(tmp_path / "x.json"), "--type", "project")
    assert code == 4
    assert "模拟内部故障" in err
    code, data = run_cli_json("validate", str(tmp_path / "x.json"), "--type", "project")
    assert code == 4
    assert data["error"] == "internal_error"
