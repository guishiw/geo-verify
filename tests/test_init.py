"""`geo-verify init` 与虚构汽车品牌完整样例（examples/auto）的测试。"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import run_cli, run_cli_json

from geo_verify import spec
from geo_verify.seal.lint import INTENT_TYPES, prompt_issues

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_SCRIPT = REPO_ROOT / "examples" / "auto" / "run.sh"
AUTO_DATA_DIR = REPO_ROOT / "src" / "geo_verify" / "examples" / "auto"
VALID_VERDICTS = {"pass", "conditional_pass", "fail", "insufficient_data"}


def test_init_skeleton_creates_valid_template_and_directories(tmp_path: Path) -> None:
    target = tmp_path / "my-geo"
    code, data = run_cli_json(
        "init",
        str(target),
        "--project-id",
        "demo-skeleton",
        "--now",
        "2026-10-01T00:00:00Z",
    )
    assert code == 0, data
    assert data["project_id"] == "demo-skeleton"
    assert data["example"] is None

    project_doc = json.loads((target / "project.json").read_text(encoding="utf-8"))
    assert spec.iter_issues("project", project_doc) == []
    assert project_doc["project_id"] == "demo-skeleton"
    prereg = project_doc["preregistration"]
    assert prereg["registered_at"] == "2026-10-01T00:00:00Z"
    assert prereg["sufficiency"] == {"min_windows": 3, "min_valid_observations": 30}
    assert prereg["crosscheck"]["formula"] == "symmetric_relative_difference"
    assert prereg["crosscheck"]["threshold"] == 0.3
    assert prereg["recheck"]["fraction"] == 0.2
    assert isinstance(prereg["statistics"]["seed"], int)
    assert project_doc.get("ext")

    # entities.jsonl 存在且为空；draft.jsonl 含合法模板题
    assert (target / "entities.jsonl").is_file()
    assert (target / "entities.jsonl").read_text(encoding="utf-8") == ""
    draft_lines = (target / "prompts" / "draft.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(draft_lines) >= 1
    for line in draft_lines:
        assert prompt_issues(json.loads(line)) == []

    for subdir in (
        "prompts/sealed",
        "prompts/diffs",
        "snapshots",
        "recheck",
        "evidence",
        "out",
    ):
        assert (target / subdir).is_dir(), subdir


def test_init_skeleton_text_output_and_reproducible_bytes(tmp_path: Path) -> None:
    first = tmp_path / "a" / "proj-alpha"
    second = tmp_path / "b" / "proj-alpha"
    code1, out1, err1 = run_cli("init", str(first), "--now", "2026-10-01T02:00:00Z")
    code2, out2, err2 = run_cli("init", str(second), "--now", "2026-10-01T02:00:00Z")
    assert code1 == 0
    assert code2 == 0
    assert not err1
    assert not err2
    assert "proj-alpha" in out1
    assert "project.json" in out1
    assert out1 != ""
    assert out2 != ""
    for rel in ("project.json", "entities.jsonl", "prompts/draft.jsonl"):
        assert (first / rel).read_bytes() == (second / rel).read_bytes(), rel


def test_init_skeleton_allows_existing_empty_dir_and_sanitizes_default_id(tmp_path: Path) -> None:
    empty = tmp_path / "中文项目目录"
    empty.mkdir()
    code, data = run_cli_json("init", str(empty), "--now", "2026-10-01T02:00:00Z")
    assert code == 0, data
    project_doc = json.loads((empty / "project.json").read_text(encoding="utf-8"))
    assert spec.iter_issues("project", project_doc) == []
    assert data["project_id"] == project_doc["project_id"]


def test_init_rejects_nonempty_dir_file_and_bad_args(tmp_path: Path) -> None:
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("hi", encoding="utf-8")
    code, err = run_cli_json("init", str(nonempty))
    assert code == 2
    assert "非空" in err["message"]

    as_file = tmp_path / "not-a-dir"
    as_file.write_text("x", encoding="utf-8")
    code, err = run_cli_json("init", str(as_file))
    assert code == 2

    code, err = run_cli_json("init", str(tmp_path / "bad-id"), "--project-id", "非法 ID!")
    assert code == 2

    code, err = run_cli_json("init", str(tmp_path / "bad-now"), "--now", "2026-10-01 00:00:00")
    assert code == 2

    code, _, stderr = run_cli("init", str(tmp_path / "bad-ex"), "--example", "unknown")
    assert code == 2
    assert stderr


def test_init_example_auto_contracts_and_data_requirements(tmp_path: Path) -> None:
    # 随包样例总大小必须小于 1 MB
    assert AUTO_DATA_DIR.is_dir()
    total_bytes = sum(p.stat().st_size for p in AUTO_DATA_DIR.rglob("*") if p.is_file())
    assert 0 < total_bytes < 1024 * 1024, f"样例数据大小超限：{total_bytes} 字节"

    target = tmp_path / "auto-demo"
    code, data = run_cli_json("init", str(target), "--example", "auto")
    assert code == 0, data
    assert data["example"] == "auto"
    assert data["synthetic"] is True

    # 1. project.json 契约与关键配置
    project_doc = json.loads((target / "project.json").read_text(encoding="utf-8"))
    assert spec.iter_issues("project", project_doc) == []
    assert project_doc.get("synthetic") is True
    platforms = {item["platform_id"]: set(item["channels"]) for item in project_doc["platforms"]}
    assert {"engine-a", "engine-b"} <= set(platforms)
    assert {"api", "manual"} <= platforms["engine-a"]
    assert {"api", "manual"} <= platforms["engine-b"]
    assert any(
        item["role"] == "acceptance" and item["threshold"] is not None
        for item in project_doc["preregistration"]["metrics"]
    )

    # 2. entities.jsonl 契约与虚构世界
    entity_lines = (target / "entities.jsonl").read_text(encoding="utf-8").splitlines()
    entities = [json.loads(line) for line in entity_lines]
    assert entities
    for entity in entities:
        assert spec.iter_issues("entity", entity) == []
        assert entity.get("synthetic") is True
    entity_ids = {item["entity_id"] for item in entities}
    assert {"brand-lanxu", "product-lanxu-s7", "brand-cenye", "brand-shuofeng"} <= entity_ids

    # 3. prompts/draft.jsonl ≥ 22 题、覆盖全部 11 类意图、部分带 expects_recommendation 与 core_fact_ids
    prompt_lines = (target / "prompts" / "draft.jsonl").read_text(encoding="utf-8").splitlines()
    prompts = [json.loads(line) for line in prompt_lines]
    assert len(prompts) >= 22
    for prompt in prompts:
        assert prompt_issues(prompt) == []
    intents = {item["intent_type"] for item in prompts}
    assert set(INTENT_TYPES) <= intents
    assert any(item.get("expects_recommendation") is True for item in prompts)
    assert any(bool(item.get("core_fact_ids")) for item in prompts)

    # 4. prompts lint 应通过（0 error、0 warning）
    lint_code, lint_data = run_cli_json("prompts", "lint", str(target))
    assert lint_code == 0, lint_data
    assert lint_data["errors"] == 0
    assert lint_data["warnings"] == 0

    # 5. inbox/observations.csv：≥ 3 窗口、2 通道、自然回答（推荐序列/否定/别名/引用）
    obs_csv = target / "inbox" / "observations.csv"
    assert obs_csv.is_file()
    with obs_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) >= 22 * 3 * 2
    windows = {row["window_id"] for row in rows}
    channels = {row["channel"] for row in rows}
    assert len(windows) >= 3
    assert {"api", "manual"} <= channels
    answers = "\n".join(row["answer_text"] for row in rows)
    assert "1." in answers
    assert "2." in answers
    assert any(word in answers for word in ("不推荐", "不建议", "不太建议"))
    assert any(alias in answers for alias in ("LANXU", "Lan-Xu", "澜序汽车"))
    assert any(row["citations"].strip() for row in rows)
    assert all(row["synthetic"] == "true" for row in rows)

    # 6. inbox/judgments.jsonl 契约、synthetic、注明 geo-facts 产出
    jdg_path = target / "inbox" / "judgments.jsonl"
    assert jdg_path.is_file()
    judgments = [json.loads(line) for line in jdg_path.read_text(encoding="utf-8").splitlines()]
    assert judgments
    directions = set()
    for item in judgments:
        assert spec.iter_issues("judgment", item) == []
        assert item.get("synthetic") is True
        directions.add(item["direction"])
    assert directions == {"answer_to_facts", "facts_to_answer"}
    assert any("geo-facts" in json.dumps(item, ensure_ascii=False) for item in judgments)


def test_init_example_auto_supports_custom_project_id_and_now(tmp_path: Path) -> None:
    target = tmp_path / "custom"
    code, data = run_cli_json(
        "init",
        str(target),
        "--example",
        "auto",
        "--project-id",
        "custom-auto-01",
        "--now",
        "2026-10-01T08:00:00Z",
    )
    assert code == 0, data
    assert data["project_id"] == "custom-auto-01"
    project_doc = json.loads((target / "project.json").read_text(encoding="utf-8"))
    assert spec.iter_issues("project", project_doc) == []
    assert project_doc["project_id"] == "custom-auto-01"
    assert project_doc["preregistration"]["registered_at"] == "2026-10-01T08:00:00Z"
    for name in ("entities.jsonl", "inbox/judgments.jsonl"):
        for line in (target / name).read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["project_id"] == "custom-auto-01", name


def test_example_auto_run_script_end_to_end(tmp_path: Path) -> None:
    assert RUN_SCRIPT.is_file()
    assert os.access(RUN_SCRIPT, os.X_OK), "examples/auto/run.sh 必须有可执行权限"
    out_dir = tmp_path / "auto-run-out"
    env = dict(os.environ)
    env["GEO_VERIFY_BIN"] = str(Path(sys.executable).parent / "geo-verify")
    completed = subprocess.run(
        [str(RUN_SCRIPT), str(out_dir)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"run.sh 失败（退出码 {completed.returncode}）\n"
        f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )

    for rel in (
        "prompts/sealed/v0001.json",
        "observations.jsonl",
        "annotations.jsonl",
        "judgments.jsonl",
        "recheck/plan.json",
        "evidence/manifest-0001.json",
        "out/metrics.json",
        "out/crosscheck.json",
        "out/alerts.json",
        "out/evidence-verify.json",
        "out/report.json",
        "out/report.html",
        "out/observations.csv",
    ):
        assert (out_dir / rel).is_file(), f"缺少产出文件：{rel}"

    report = json.loads((out_dir / "out" / "report.json").read_text(encoding="utf-8"))
    assert report["verdict"]["code"] in VALID_VERDICTS
    assert report.get("synthetic") is True
    assert report.get("watermark")

    html = (out_dir / "out" / "report.html").read_text(encoding="utf-8")
    assert 'class="watermark"' in html
    assert "合成" in html

    metrics_doc = json.loads((out_dir / "out" / "metrics.json").read_text(encoding="utf-8"))
    by_id = {
        item["metric_id"]: item
        for item in metrics_doc["results"]
        if all(
            item["scope"].get(key) is None for key in ("platform", "channel", "window_id", "date")
        )
    }
    assert by_id["mention_rate"]["value"] is not None
    assert by_id["accuracy_verifiable"]["value"] is not None
    assert by_id["core_fact_presence"]["value"] is not None

    annotations = [
        json.loads(line)
        for line in (out_dir / "annotations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(item["negated"] is True for item in annotations)


def test_init_missing_packaged_example_raises_input_error() -> None:
    from geo_verify.commands import init as init_cmd
    from geo_verify.errors import InputError

    with pytest.raises(InputError, match="nonexistent"):
        init_cmd._example_root("nonexistent")
