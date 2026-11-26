"""人工采集包：生成任务清单（pack）与回填导入（fill）。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from factory import make_observation, make_project, make_prompt, seal_prompts, write_records
from helpers_basic import clean_prompts, sealed_project, write_entities
from helpers_collect import independent_digest
from hypothesis import given
from hypothesis import strategies as st

from geo_verify import audit, project, spec
from geo_verify.collect import manual
from geo_verify.collect.tasks import Task, TaskPlan, plan_tasks, split_ids
from geo_verify.commands import collect as collect_command

NOW = "2026-10-08T06:00:00Z"

READ_ONLY_COLUMNS = [
    "trace_id",
    "query_id",
    "attempt",
    "platform",
    "window_id",
    "prompt_set_hash",
    "prompt_text",
]
FILL_COLUMNS = [
    "entry_form",
    "answer_text",
    "citations",
    "snapshot_files",
    "model_version",
    "entry_features",
    "logged_in",
    "history_cleared",
    "web_search",
    "region",
    "device",
    "started_at",
    "ended_at",
    "operator",
    "status",
    "invalid_reason",
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    sealed_project(path)
    return path


def pack(root: Path, out: Path, *extra: str) -> tuple[int, Any]:
    return run_cli_json(
        "collect", str(root), "--adapter", "manual", "--action", "pack", "--platform", "engine-a",
        "--window", "W1", "--out", str(out), *extra,
    )  # fmt: skip


def fill(root: Path, source: Path, *extra: str) -> tuple[int, Any]:
    return run_cli_json(
        "collect", str(root), "--adapter", "manual", "--action", "fill", "--input", str(source),
        "--now", NOW, *extra,
    )  # fmt: skip


def read_tasks(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline=""))
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def write_tasks(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8-sig", newline="")


def answered(row: dict[str, str], **fields: str) -> dict[str, str]:
    row.update(
        {
            "answer_text": "可以考虑澜序 S7（虚构回答）。",
            "entry_features": "网页端默认模式，未展示模型名",
            "started_at": "2026-10-08T10:00:00+08:00",
            "ended_at": "2026-10-08T10:00:25+08:00",
        }
    )
    row.update(fields)
    return row


# ------------------------------------------------------------------ pack


def test_pack_writes_task_list_and_readme(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    code, data = pack(root, out, "--repeats", "2", "--queries", "Q001,Q002")
    assert code == 0
    assert data["tasks"] == 4
    assert data["files"] == ["tasks.csv", "README.md"]

    assert (out / "tasks.csv").read_bytes().startswith(b"\xef\xbb\xbf"), "导出 CSV 带 UTF-8 BOM"
    fieldnames, rows = read_tasks(out / "tasks.csv")
    assert fieldnames == READ_ONLY_COLUMNS + FILL_COLUMNS
    assert [(row["query_id"], row["attempt"]) for row in rows] == [
        ("Q001", "1"),
        ("Q001", "2"),
        ("Q002", "1"),
        ("Q002", "2"),
    ]
    sealed = project.select_prompt_set(root)
    for row in rows:
        digest = independent_digest(
            "auto-demo",
            sealed["content_hash"],
            "engine-a",
            "manual",
            "W1",
            row["query_id"],
            int(row["attempt"]),
        )
        assert row["trace_id"] == f"t-{digest}"
        assert row["platform"] == "engine-a"
        assert row["window_id"] == "W1"
        assert row["prompt_set_hash"] == sealed["content_hash"]
        assert all(row[name] == "" for name in FILL_COLUMNS), "待回填列留空，不预填任何值"
    assert rows[0]["prompt_text"] == "澜序这个品牌怎么样？"
    assert rows[2]["prompt_text"] == "澜序 S7 的续航是多少公里？"

    readme = (out / "README.md").read_text(encoding="utf-8")
    for column in FILL_COLUMNS:
        assert column in readme
    assert "manual" in readme
    assert "留空" in readme
    assert project.load_observations(root) == []
    assert project.load_audit(root) == []


def test_pack_defaults_to_all_queries_once(root: Path, tmp_path: Path) -> None:
    code, data = pack(root, tmp_path / "pack")
    assert code == 0
    assert data["tasks"] == 11
    code, out, _ = run_cli(
        "collect", str(root), "--adapter", "manual", "--action", "pack", "--platform", "engine-a",
        "--window", "W2", "--out", str(tmp_path / "pack2"),
    )  # fmt: skip
    assert code == 0
    assert "11 个任务" in out


def test_pack_guards_against_formula_injection(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    proj = make_project(root)
    write_entities(root)
    seal_prompts(
        root,
        proj,
        [
            make_prompt("Q001", text="=SUM(1,2) 是什么意思？"),
            make_prompt("Q002", text="+86 开头的号码是哪里的？"),
            make_prompt("Q003", text="-5 度时续航掉多少？"),
            make_prompt("Q004", text="@澜序 这个账号是谁？"),
            make_prompt("Q005", text="澜序 S7 = 好车吗？"),
        ],
    )
    out = tmp_path / "pack"
    assert pack(root, out)[0] == 0
    _, rows = read_tasks(out / "tasks.csv")
    assert [row["prompt_text"] for row in rows] == [
        "'=SUM(1,2) 是什么意思？",
        "'+86 开头的号码是哪里的？",
        "'-5 度时续航掉多少？",
        "'@澜序 这个账号是谁？",
        "澜序 S7 = 好车吗？",
    ]


def test_pack_input_errors(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    base = ("collect", str(root), "--adapter", "manual", "--action", "pack", "--window", "W1")
    code, data = run_cli_json(*base, "--platform", "engine-b", "--out", str(out))
    assert code == 2
    assert "manual" in data["message"], "engine-b 只登记了 api 通道"
    code, data = run_cli_json(*base, "--platform", "engine-zz", "--out", str(out))
    assert code == 2
    code, data = run_cli_json(
        *base, "--platform", "engine-a", "--out", str(out), "--queries", "Q404"
    )
    assert code == 2
    assert "Q404" in data["message"]
    code, data = run_cli_json(*base, "--platform", "engine-a", "--out", str(out), "--repeats", "0")
    assert code == 2
    code, data = run_cli_json(*base, "--platform", "engine-a")
    assert code == 2
    assert "--out" in data["message"]
    code, data = run_cli_json(*base, "--out", str(out))
    assert code == 2
    assert "--platform" in data["message"]
    assert not out.exists()

    assert pack(root, out)[0] == 0
    code, data = pack(root, out)
    assert code == 2
    assert "已存在" in data["message"]


def test_action_is_required_for_manual(root: Path) -> None:
    code, data = run_cli_json("collect", str(root), "--adapter", "manual")
    assert code == 2
    assert "--action" in data["message"]
    code, data = run_cli_json("collect", str(root), "--adapter", "manual", "--action", "fill")
    assert code == 2
    assert "--input" in data["message"]


# ------------------------------------------------------------------ fill


def test_fill_imports_answered_rows_and_reports_the_rest(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001,Q002,Q003,Q004")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    (out / "shots").mkdir()
    (out / "shots" / "q1.png").write_bytes(b"fake-png-bytes")
    answered(
        rows[0],
        citations="inline | https://lanxu-auto.example/s7 | 澜序 S7（虚构）\n"
        "source_list | https://auto-review.example/list",
        snapshot_files="shots/q1.png",
        logged_in="否",
        history_cleared="是",
        web_search="是",
        region="CN-SH",
        device="desktop-chrome",
        operator="采集员甲（虚构）",
    )
    # rows[1] 保持空白：尚未采集
    rows[2].update(
        status="invalid",
        invalid_reason="timeout",
        entry_form="app",
        model_version="engine-a-app-3.2",
        started_at="2026-10-08T02:10:00Z",
        ended_at="2026-10-08T02:11:00Z",
    )
    answered(rows[3], started_at="2026-10-08 10:00:00")  # 时间没有时区
    write_tasks(out / "tasks.csv", fieldnames, rows)

    code, data = fill(root, out / "tasks.csv", "--operator", "回填员乙（虚构）")
    assert code == 1
    assert data["rows"] == 4
    assert data["written"] == 2
    assert data["valid"] == 1
    assert data["invalid_by_reason"] == {"timeout": 1}
    assert data["pending"] == 1
    assert data["rejected_rows"] == 1
    assert [(item["line"], item["column"]) for item in data["errors"]] == [(6, "started_at")]

    sealed = project.select_prompt_set(root)
    first, third = project.load_observations(root)
    for record in (first, third):
        assert spec.iter_issues("observation", record) == []
        assert record["channel"] == "manual"
        assert record["prompt_set_hash"] == sealed["content_hash"]
        assert record["platform"] == "engine-a"
        assert record["window_id"] == "W1"
        assert record["attempt"] == 1
        assert record["synthetic"] is True
    digest = independent_digest(
        "auto-demo", sealed["content_hash"], "engine-a", "manual", "W1", "Q001", 1
    )
    assert first["trace_id"] == f"t-{digest}"
    assert first["observation_id"] == f"o-{digest}"
    assert first["entry_form"] == "web", "入口形态留空时默认 web"
    assert first["prompt_text"] == "澜序这个品牌怎么样？"
    assert first["answer_text"] == "可以考虑澜序 S7（虚构回答）。"
    assert first["model_version"] is None
    assert first["entry_features"] == "网页端默认模式，未展示模型名"
    assert (first["logged_in"], first["history_cleared"], first["web_search"]) == (
        False,
        True,
        True,
    )
    assert (first["region"], first["device"]) == ("CN-SH", "desktop-chrome")
    assert (first["started_at"], first["ended_at"]) == (
        "2026-10-08T02:00:00Z",
        "2026-10-08T02:00:25Z",
    )
    assert first["operator"] == "采集员甲（虚构）"
    assert [(c["order"], c["domain"], c["placement"]) for c in first["citations"]] == [
        (1, "lanxu-auto.example", "inline"),
        (2, "auto-review.example", "source_list"),
    ]
    stored = f"snapshots/{first['observation_id']}/q1.png"
    assert first["snapshots"] == [
        {
            "path": stored,
            "sha256": hashlib.sha256(b"fake-png-bytes").hexdigest(),
            "media_type": "image/png",
        }
    ]
    assert (root / stored).read_bytes() == b"fake-png-bytes"

    assert third["status"] == "invalid"
    assert third["invalid_reason"] == "timeout"
    assert third["answer_text"] is None
    assert third["entry_form"] == "app"
    assert third["model_version"] == "engine-a-app-3.2"
    assert third["operator"] == "回填员乙（虚构）"
    assert third["snapshots"] == []
    assert {"answer_text", "snapshots", "logged_in", "region", "device"} <= set(
        third["null_reasons"]
    )

    events = project.load_audit(root)
    assert [event["action"] for event in events] == ["collect"]
    assert events[0]["details"]["adapter"] == "manual"
    assert events[0]["details"]["written"] == 2
    assert events[0]["occurred_at"] == NOW
    assert audit.verify_chain(events) == []

    # 续填：补上漏填的一行、修正出错的一行，再次导入只写新增的两条
    answered(rows[1], model_version="engine-a-web-2026.10", entry_features="")
    rows[3]["started_at"] = "2026-10-08T10:00:00+08:00"
    write_tasks(out / "tasks.csv", fieldnames, rows)
    code, data = fill(root, out / "tasks.csv")
    assert code == 0
    assert (data["written"], data["skipped_existing"], data["pending"]) == (2, 2, 0)
    records = project.load_observations(root)
    assert [record["query_id"] for record in records] == ["Q001", "Q003", "Q002", "Q004"]
    assert records[2]["model_version"] == "engine-a-web-2026.10"
    assert records[2]["operator"] is None
    assert "operator" in records[2]["null_reasons"]
    assert len({record["trace_id"] for record in records}) == 4

    code, out_text, _ = run_cli(
        "collect", str(root), "--adapter", "manual", "--action", "fill",
        "--input", str(out / "tasks.csv"),
    )  # fmt: skip
    assert code == 0
    assert "跳过已存在 4 条" in out_text


def test_fill_detects_altered_task_rows(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001,Q002,Q003,Q004,Q005,Q006")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    for row in rows:
        answered(row)
    rows[0]["query_id"] = "Q009"  # 任务被换成别的题
    rows[1]["trace_id"] = "t-" + "0" * 24  # 编号被改
    rows[2]["attempt"] = "x"  # 次数不是整数
    rows[3]["prompt_set_hash"] = "f" * 64  # 不属于任何封存版本
    rows[4]["platform"] = "engine-b"  # 该平台未登记 manual 通道
    write_tasks(out / "tasks.csv", fieldnames, rows)
    code, data = fill(root, out / "tasks.csv")
    assert code == 1
    assert {(item["line"], item["column"]) for item in data["errors"]} == {
        (2, "trace_id"),
        (3, "trace_id"),
        (4, "attempt"),
        (5, "prompt_set_hash"),
        (6, "channel"),
    }
    assert [record["query_id"] for record in project.load_observations(root)] == ["Q006"]


def test_fill_row_level_validation(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001,Q002,Q003,Q004,Q005,Q006,Q007,Q008")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    for row in rows:
        answered(row)
    rows[0]["snapshot_files"] = "shots/none.png"  # 快照文件不存在
    rows[1]["entry_features"] = ""  # 模型版本与入口特征都没填
    rows[2]["status"] = "invalid"  # 无效却没写原因
    rows[3]["entry_form"] = "api"  # 人工通道不能标成 api 入口
    rows[4]["web_search"] = "不清楚"
    rows[5]["citations"] = "https://lanxu-auto.example/only-url"
    rows[6]["answer_text"] = ""
    rows[6]["status"] = "valid"  # 标了有效却没有回答
    rows[7]["ended_at"] = "2026-10-08T09:00:00+08:00"  # 结束早于开始
    write_tasks(out / "tasks.csv", fieldnames, rows)
    code, data = fill(root, out / "tasks.csv")
    assert code == 1
    assert data["written"] == 0
    assert {(item["line"], item["column"]) for item in data["errors"]} == {
        (2, "snapshot_files"),
        (3, "entry_features"),
        (4, "invalid_reason"),
        (5, "entry_form"),
        (6, "web_search"),
        (7, "citations"),
        (8, "answer_text"),
        (9, "ended_at"),
    }
    assert not (root / "snapshots").exists(), "被拒绝的行不留下快照文件"
    assert project.load_audit(root) == []


def test_fill_snapshots_never_overwrite_existing_files(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001,Q002")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    (out / "a").mkdir()
    (out / "b").mkdir()
    (out / "a" / "shot.png").write_bytes(b"one")
    (out / "b" / "shot.png").write_bytes(b"two")
    answered(rows[0], snapshot_files="a/shot.png; b/shot.png")  # 同名文件会互相覆盖
    answered(rows[1], snapshot_files="a/shot.png;a/shot.png")
    write_tasks(out / "tasks.csv", fieldnames, rows)
    sealed = project.select_prompt_set(root)
    digest = independent_digest(
        "auto-demo", sealed["content_hash"], "engine-a", "manual", "W1", "Q002", 1
    )
    occupied = root / "snapshots" / f"o-{digest}" / "shot.png"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"someone else's file")

    code, data = fill(root, out / "tasks.csv")
    assert code == 1
    assert {(item["line"], item["column"]) for item in data["errors"]} == {
        (2, "snapshot_files"),
        (3, "snapshot_files"),
    }
    assert occupied.read_bytes() == b"someone else's file"

    occupied.write_bytes(b"one")  # 内容相同则视为同一份快照
    rows[0]["snapshot_files"] = "a/shot.png"
    write_tasks(out / "tasks.csv", fieldnames, rows)
    code, data = fill(root, out / "tasks.csv")
    assert code == 0
    assert data["written"] == 2
    second = project.load_observations(root)[1]
    assert [item["sha256"] for item in second["snapshots"]] == [hashlib.sha256(b"one").hexdigest()]


def test_fill_file_level_errors(root: Path, tmp_path: Path) -> None:
    assert fill(root, tmp_path / "missing.csv")[0] == 2
    bad = tmp_path / "bad.csv"
    bad.write_text("trace_id,query_id,mood\n", encoding="utf-8")
    code, data = fill(root, bad)
    assert code == 2
    assert "mood" in data["message"]
    short = tmp_path / "short.csv"
    short.write_text("trace_id,query_id\n", encoding="utf-8")
    code, data = fill(root, short)
    assert code == 2
    assert "缺少必需列" in data["message"]

    unsealed = tmp_path / "unsealed"
    make_project(unsealed)
    write_entities(unsealed)
    code, data = run_cli_json(
        "collect", str(unsealed), "--adapter", "manual", "--action", "pack", "--platform", "engine-a",
        "--window", "W1", "--out", str(tmp_path / "x"),
    )  # fmt: skip
    assert code == 2
    assert "尚未封存" in data["message"]


def test_pack_uses_latest_sealed_version(root: Path, tmp_path: Path) -> None:
    proj = project.load_project(root)
    first = project.select_prompt_set(root)
    second = seal_prompts(root, proj, clean_prompts()[:3], previous=first)
    out = tmp_path / "pack"
    code, data = pack(root, out)
    assert code == 0
    assert data["tasks"] == 3
    assert data["prompt_set_version"] == 2
    _, rows = read_tasks(out / "tasks.csv")
    assert {row["prompt_set_hash"] for row in rows} == {second["content_hash"]}


# ------------------------------------------------------------------ 适配器分发


def test_adapters_without_implementation_are_explicit_input_errors(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert {"manual", "openai", "share"} <= {*collect_command.ADAPTERS, *collect_command.PLANNED}
    monkeypatch.setitem(collect_command.PLANNED, "later", "测试用的占位适配器")
    pending = [name for name in collect_command.PLANNED if name not in collect_command.ADAPTERS]
    assert "later" in pending
    for name in pending:
        code, data = run_cli_json("collect", str(root), "--adapter", name, "--platform", "engine-a")
        assert code == 2
        assert data["error"] == "input_error"
        assert name in data["message"]
        assert "尚未实现" in data["message"]
        assert "manual" in data["message"], "提示当前可用的适配器"
    assert project.load_observations(root) == []
    assert project.load_audit(root) == []


def test_unknown_or_missing_adapter_is_a_usage_error(root: Path) -> None:
    code, out, err = run_cli("collect", str(root), "--adapter", "crawler")
    assert (code, out) == (2, "")
    assert "输入错误" in err
    code, out, err = run_cli("collect", str(root))
    assert (code, out) == (2, "")
    assert "--adapter" in err


def test_options_of_the_other_action_are_rejected(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    code, data = pack(root, out, "--input", str(tmp_path / "x.csv"))
    assert code == 2
    assert "--input" in data["message"]
    code, data = pack(root, out, "--snapshots-dir", str(tmp_path))
    assert code == 2
    assert "--snapshots-dir" in data["message"]
    assert not out.exists()

    assert pack(root, out)[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    write_tasks(out / "tasks.csv", fieldnames, [answered(row) for row in rows])
    for extra in (
        ("--platform", "engine-a"),
        ("--window", "W9"),
        ("--repeats", "0"),
        ("--queries", "Q001"),
        ("--out", str(tmp_path / "other")),
    ):
        code, data = fill(root, out / "tasks.csv", *extra)
        assert code == 2
        assert extra[0] in data["message"]
    assert project.load_observations(root) == [], "参数被拒绝时不写入任何观测"


# ------------------------------------------------------------------ pack 的边角


def test_pack_is_reproducible_and_follows_sealed_order(root: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    second.mkdir()  # 已存在的空目录可以直接使用
    code, _ = pack(root, first, "--queries", "Q003, Q001,Q003", "--repeats", "2")
    assert code == 0
    code, data = pack(root, second, "--queries", "Q001,Q003", "--repeats", "2")
    assert code == 0
    assert (data["queries"], data["repeats"], data["tasks"]) == (2, 2, 4)
    assert data["out"] == "b", "输出里不带绝对路径"
    for name in ("tasks.csv", "README.md"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert data["tasks_sha256"] == hashlib.sha256((second / "tasks.csv").read_bytes()).hexdigest()
    _, rows = read_tasks(first / "tasks.csv")
    assert [(row["query_id"], row["attempt"]) for row in rows] == [
        ("Q001", "1"),
        ("Q001", "2"),
        ("Q003", "1"),
        ("Q003", "2"),
    ]
    assert str(tmp_path) not in (first / "README.md").read_text(encoding="utf-8")


def test_pack_rejects_bad_window_empty_queries_and_file_target(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    base = (
        "collect",
        str(root),
        "--adapter",
        "manual",
        "--action",
        "pack",
        "--platform",
        "engine-a",
    )
    # 窗口编号会成为观测的 window_id：契约要求以字母或数字开头、不含空格、最长 128 个字符
    for window in ("W 1", "_W1", "W" * 129, "窗口一"):
        code, data = run_cli_json(*base, "--window", window, "--out", str(out))
        assert code == 2
        assert "窗口" in data["message"]
    assert not out.exists()
    code, data = run_cli_json(*base, "--window", "2026-10.W1:a_b", "--out", str(tmp_path / "ok"))
    assert code == 0
    assert data["window_id"] == "2026-10.W1:a_b"
    code, data = pack(root, out, "--queries", " , ")
    assert code == 2
    assert "--queries" in data["message"]
    assert not out.exists()

    occupied = tmp_path / "occupied"
    occupied.write_text("不要覆盖我", encoding="utf-8")
    code, data = pack(root, occupied)
    assert code == 2
    assert "已存在" in data["message"]
    assert occupied.read_text(encoding="utf-8") == "不要覆盖我"


def test_readme_shows_registered_environment_and_synthetic_mark(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    sealed_project(root)
    out = tmp_path / "pack"
    assert pack(root, out)[0] == 0
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "synthetic: true" in readme, "合成数据项目的说明带可见标记"
    assert "模拟平台 A" in readme
    # 样例项目登记的环境：未登录、清除历史、联网、地域 CN、设备不作要求
    for line in (
        "- 是否登录：否",
        "- 提问前是否清除历史：是",
        "- 是否联网：是",
        "- 地域：CN",
        "- 设备：未登记（不作要求）",
    ):
        assert line in readme.splitlines()
    for column in READ_ONLY_COLUMNS:
        assert f"`{column}`" in readme
    for word in ("官方", "认证", "对标", "保证"):
        assert word not in readme

    plain = tmp_path / "plain"
    sealed_project(plain, synthetic=False)
    out = tmp_path / "pack-plain"
    assert pack(plain, out)[0] == 0
    assert "synthetic" not in (out / "README.md").read_text(encoding="utf-8")
    fieldnames, rows = read_tasks(out / "tasks.csv")
    write_tasks(out / "tasks.csv", fieldnames, [answered(rows[0])])
    code, data = fill(plain, out / "tasks.csv")
    assert (code, data["written"]) == (0, 1)
    assert "synthetic" not in project.load_observations(plain)[0], "不替非合成项目编造标记"
    assert "synthetic" not in project.load_audit(plain)[0]


def test_plan_tasks_gives_deterministic_ids_for_any_channel(root: Path) -> None:
    sealed = project.select_prompt_set(root)
    plan = plan_tasks(
        root, platform="engine-b", channel="api", window="W3", repeats=2, queries=["Q002"]
    )
    assert (plan.project_id, plan.prompt_set_version) == ("auto-demo", 1)
    assert (plan.platform, plan.channel, plan.window_id, plan.repeats) == (
        "engine-b",
        "api",
        "W3",
        2,
    )
    assert plan.query_ids == ("Q002",)
    assert [(task.query_id, task.attempt) for task in plan.tasks] == [("Q002", 1), ("Q002", 2)]
    digest = independent_digest(
        "auto-demo", sealed["content_hash"], "engine-b", "api", "W3", "Q002", 2
    )
    assert plan.tasks[1].trace_id == f"t-{digest}"
    assert plan.tasks[1].observation_id == f"o-{digest}"
    assert plan.tasks[1].prompt_text == "澜序 S7 的续航是多少公里？"
    assert split_ids(None) is None
    assert split_ids(" Q001, ,Q002 ") == ["Q001", "Q002"]


FORMULA_STARTS = "=+-@\t\r"


@given(st.lists(st.text(min_size=1).filter(lambda text: text.strip()), min_size=1, max_size=4))
def test_task_cells_never_start_a_formula(texts: list[str]) -> None:
    tasks = tuple(
        Task(
            query_id=f"Q{index:03d}",
            attempt=1,
            prompt_text=text,
            trace_id=f"t-{index:024d}",
            observation_id=f"o-{index:024d}",
        )
        for index, text in enumerate(texts, start=1)
    )
    plan = TaskPlan(
        project_id="auto-demo",
        prompt_set_hash="0" * 64,
        prompt_set_version=1,
        platform="engine-a",
        channel="manual",
        window_id="W1",
        repeats=1,
        query_ids=tuple(task.query_id for task in tasks),
        tasks=tasks,
    )
    rows = list(csv.DictReader(io.StringIO(manual.tasks_csv(plan), newline="")))
    assert len(rows) == len(texts)
    for text, row in zip(texts, rows, strict=True):
        cell = row["prompt_text"]
        assert cell[0] not in FORMULA_STARTS
        if text[0] in FORMULA_STARTS:
            assert cell == "'" + text
        else:
            assert cell == text, "不需要防护的题目原样写出"


# ------------------------------------------------------------------ fill 的边角


def test_fill_read_only_column_checks(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001,Q002,Q003,Q004,Q005")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    for row in rows:
        answered(row)
    rows[0]["window_id"] = ""  # 只读列被清空
    rows[1]["query_id"] = "Q404"  # 题库里没有这道题
    rows[2]["platform"] = "engine-zz"  # 平台未登记
    rows[3]["prompt_text"] = "澜序这个品牌值得买吗？"  # 题目原文被改写
    rows[4]["attempt"] = "0"
    write_tasks(out / "tasks.csv", fieldnames, rows)
    with (out / "tasks.csv").open("a", encoding="utf-8", newline="") as handle:
        handle.write(",".join(["x"] * (len(fieldnames) + 1)) + "\n")  # 比表头多一个单元格

    code, data = fill(root, out / "tasks.csv")
    assert code == 1
    assert (data["rows"], data["written"], data["rejected_rows"]) == (6, 0, 6)
    assert [(item["line"], item["column"]) for item in data["errors"]] == [
        (2, "window_id"),
        (3, "query_id"),
        (4, "platform"),
        (5, "prompt_text"),
        (6, "attempt"),
        (7, None),
    ]
    assert data["errors_total"] == 6
    assert project.load_observations(root) == []

    code, text, _ = run_cli(
        "collect", str(root), "--adapter", "manual", "--action", "fill",
        "--input", str(out / "tasks.csv"),
    )  # fmt: skip
    assert code == 1
    assert "第 5 行 [prompt_text]：" in text
    assert "第 7 行：单元格数量多于表头列数" in text
    assert "非法 6 行" in text


def test_fill_keeps_sealed_prompt_for_guarded_and_reformatted_cells(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    proj = make_project(root)
    write_entities(root)
    texts = ["=SUM(1,2) 是什么意思？", "第一行\n第二行怎么理解？", "-5 度时续航掉多少？"]
    seal_prompts(
        root,
        proj,
        [make_prompt(f"Q{index:03d}", text=text) for index, text in enumerate(texts, start=1)],
    )
    out = tmp_path / "pack"
    assert pack(root, out)[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    assert rows[0]["prompt_text"] == "'" + texts[0]
    rows[1]["prompt_text"] = rows[1]["prompt_text"].replace("\n", "\r\n")  # 表格软件改了换行符
    rows[2]["prompt_text"] = rows[2]["prompt_text"].removeprefix("'")  # 表格软件去掉了单引号
    write_tasks(out / "tasks.csv", fieldnames, [answered(row) for row in rows])
    code, data = fill(root, out / "tasks.csv")
    assert (code, data["written"]) == (0, 3)
    assert [record["prompt_text"] for record in project.load_observations(root)] == texts


def test_fill_status_rules(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001,Q002,Q003,Q004,Q005,Q006")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    # 没有回答、只写了无效原因：按无效观测记录
    rows[0].update(
        invalid_reason="rate_limited",
        entry_features="网页端默认模式，未展示模型名",
        started_at="2026-10-08T02:00:00Z",
        ended_at="2026-10-08T02:00:05Z",
    )
    answered(rows[1], status="valid", invalid_reason="other")  # 有效却写了无效原因
    answered(rows[2], status="done")
    answered(rows[3], invalid_reason="bored")
    answered(rows[4], entry_form="desktop")
    answered(rows[5], status="INVALID", invalid_reason="Truncated", entry_form="APP")
    write_tasks(out / "tasks.csv", fieldnames, rows)

    code, data = fill(root, out / "tasks.csv")
    assert code == 1
    assert [(item["line"], item["column"]) for item in data["errors"]] == [
        (3, "invalid_reason"),
        (4, "status"),
        (5, "invalid_reason"),
        (6, "entry_form"),
    ]
    assert (data["written"], data["valid"], data["invalid"]) == (2, 0, 2)
    assert data["invalid_by_reason"] == {"rate_limited": 1, "truncated": 1}
    limited, truncated = project.load_observations(root)
    assert (limited["query_id"], limited["status"], limited["answer_text"]) == (
        "Q001",
        "invalid",
        None,
    )
    assert (truncated["query_id"], truncated["status"], truncated["invalid_reason"]) == (
        "Q006",
        "invalid",
        "truncated",
    )
    assert truncated["answer_text"] == "可以考虑澜序 S7（虚构回答）。", "被截断的回答原样保留"
    assert truncated["entry_form"] == "app"
    for record in (limited, truncated):
        assert spec.iter_issues("observation", record) == []


def test_fill_snapshot_paths_and_snapshots_dir(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001,Q002,Q003,Q004,Q005")[0] == 0
    shots = tmp_path / "shots"
    (shots / "sub").mkdir(parents=True)
    (shots / "a.png").write_bytes(b"A")
    (shots / "sub" / "b.html").write_bytes(b"<p>B</p>")
    (shots / "c.txt").write_bytes(b"C")
    fieldnames, rows = read_tasks(out / "tasks.csv")
    for row in rows:
        answered(row)
    rows[0]["snapshot_files"] = "a.png；sub\\b.html\nc.txt"  # 全角分号、反斜杠、换行都能分隔
    rows[1]["snapshot_files"] = "../secret.png"
    rows[2]["snapshot_files"] = "/abs/shot.png"
    rows[3]["snapshot_files"] = "sub/"
    rows[4]["snapshot_files"] = "sub"  # 是目录，不是文件
    write_tasks(out / "tasks.csv", fieldnames, rows)

    code, data = fill(root, out / "tasks.csv", "--snapshots-dir", str(tmp_path / "missing"))
    assert code == 2
    assert "快照目录不存在" in data["message"]

    code, data = fill(root, out / "tasks.csv", "--snapshots-dir", str(shots))
    assert code == 1
    assert [(item["line"], item["column"]) for item in data["errors"]] == [
        (4, "snapshot_files"),
        (5, "snapshot_files"),
        (6, "snapshot_files"),
        (7, "snapshot_files"),
    ], "第一行的单元格跨两行，后面的行号顺延"
    assert (data["written"], data["snapshots_copied"]) == (1, 3)
    record = project.load_observations(root)[0]
    prefix = f"snapshots/{record['observation_id']}"
    assert record["snapshots"] == [
        {
            "path": f"{prefix}/a.png",
            "sha256": hashlib.sha256(b"A").hexdigest(),
            "media_type": "image/png",
        },
        {
            "path": f"{prefix}/b.html",
            "sha256": hashlib.sha256(b"<p>B</p>").hexdigest(),
            "media_type": "text/html",
        },
        {
            "path": f"{prefix}/c.txt",
            "sha256": hashlib.sha256(b"C").hexdigest(),
            "media_type": "text/plain",
        },
    ]
    assert (root / prefix / "b.html").read_bytes() == b"<p>B</p>"
    assert [path.name for path in (root / "snapshots").iterdir()] == [record["observation_id"]]
    assert "snapshots" not in record["null_reasons"]
    assert project.load_audit(root)[0]["details"]["snapshots_copied"] == 3


def test_fill_refuses_snapshot_target_occupied_by_a_directory(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    (out / "shot.png").write_bytes(b"one")
    write_tasks(out / "tasks.csv", fieldnames, [answered(rows[0], snapshot_files="shot.png")])
    sealed = project.select_prompt_set(root)
    digest = independent_digest(
        "auto-demo", sealed["content_hash"], "engine-a", "manual", "W1", "Q001", 1
    )
    (root / "snapshots" / f"o-{digest}" / "shot.png").mkdir(parents=True)
    code, data = fill(root, out / "tasks.csv")
    assert code == 1
    assert [(item["line"], item["column"]) for item in data["errors"]] == [(2, "snapshot_files")]
    assert "拒绝覆盖" in data["errors"][0]["reason"]
    assert project.load_observations(root) == []


def test_fill_duplicate_rows_and_occupied_observation_id(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001,Q002")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    for row in rows:
        answered(row)
    rows.append(dict(rows[0], answer_text="另一个回答（虚构）"))  # 同一任务被复制了一行
    write_tasks(out / "tasks.csv", fieldnames, rows)
    # 第二个任务的观测编号已被另一条来历不同的记录占用
    proj, sealed = project.load_project(root), project.select_prompt_set(root)
    digest = independent_digest(
        "auto-demo", sealed["content_hash"], "engine-a", "manual", "W1", "Q002", 1
    )
    existing = make_observation(proj, sealed, 1, query_id="Q002", observation_id=f"o-{digest}")
    write_records(root, observations=[existing])

    code, data = fill(root, out / "tasks.csv")
    assert code == 1
    assert (data["written"], data["skipped_duplicate_in_input"], data["rejected_rows"]) == (1, 1, 1)
    assert data["duplicates"] == [{"line": 4, "trace_id": rows[0]["trace_id"], "first_line": 2}]
    assert [(item["line"], item["column"]) for item in data["errors"]] == [(3, "trace_id")]
    records = project.load_observations(root)
    assert records[0] == existing, "已有记录原样保留"
    assert [record["answer_text"] for record in records[1:]] == ["可以考虑澜序 S7（虚构回答）。"]
    assert len({record["observation_id"] for record in records}) == 2


def test_fill_reports_contract_violations_per_row(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    citation = {"url": "https://lanxu-auto.example/s7", "placement": "inline", "order": 0}
    write_tasks(
        out / "tasks.csv", fieldnames, [answered(rows[0], citations=json.dumps([citation]))]
    )
    code, data = fill(root, out / "tasks.csv")
    assert code == 1, "引用序号从 1 开始，契约不接受 0"
    assert [(item["line"], item["column"]) for item in data["errors"]] == [(2, "citations")]
    assert project.load_observations(root) == []


def test_fill_text_output_truncates_long_error_lists(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--repeats", "6")[0] == 0  # 11 题 × 6 次 = 66 个任务
    fieldnames, rows = read_tasks(out / "tasks.csv")
    write_tasks(out / "tasks.csv", fieldnames, [answered(row, status="done") for row in rows])
    code, text, _ = run_cli(
        "collect", str(root), "--adapter", "manual", "--action", "fill",
        "--input", str(out / "tasks.csv"),
    )  # fmt: skip
    assert code == 1
    assert "共 66 行" in text
    assert "非法 66 行" in text
    assert text.count("[status]") == 50, "文本输出最多列 50 处，其余提示用 --json 查看"
    assert "共 66 处错误" in text
    code, data = fill(root, out / "tasks.csv")
    assert len(data["errors"]) == data["errors_total"] == 66, "JSON 输出不截断"


def test_fill_rejects_malformed_now_before_writing(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    (out / "shot.png").write_bytes(b"one")
    write_tasks(out / "tasks.csv", fieldnames, [answered(rows[0], snapshot_files="shot.png")])
    code, _data = run_cli_json(
        "collect", str(root), "--adapter", "manual", "--action", "fill",
        "--input", str(out / "tasks.csv"), "--now", "2026-10-08 06:00",
    )  # fmt: skip
    assert code == 2
    assert project.load_observations(root) == []
    assert not (root / "snapshots").exists()
    assert project.load_audit(root) == []


def test_fill_is_reproducible_byte_for_byte(tmp_path: Path) -> None:
    results = []
    for name in ("first", "second"):
        root = tmp_path / name / "proj"
        root.parent.mkdir()
        sealed_project(root)
        out = tmp_path / name / "pack"
        assert pack(root, out, "--queries", "Q001,Q002")[0] == 0
        fieldnames, rows = read_tasks(out / "tasks.csv")
        (out / "shot.png").write_bytes(b"fake-png-bytes")
        answered(
            rows[0], snapshot_files="shot.png", citations="inline | https://lanxu-auto.example/s7"
        )
        rows[1].update(
            status="invalid",
            invalid_reason="empty_answer",
            model_version="engine-a-web-2026.10",
            started_at="2026-10-08T02:10:00Z",
            ended_at="2026-10-08T02:10:30Z",
        )
        write_tasks(out / "tasks.csv", fieldnames, rows)
        code, data = fill(root, out / "tasks.csv", "--operator", "回填员乙（虚构）")
        assert (code, data["written"]) == (0, 2)
        observations = (root / "observations.jsonl").read_bytes()
        assert str(tmp_path).encode() not in observations, "观测里不出现绝对路径"
        results.append((observations, (root / "audit.jsonl").read_bytes(), data))
    assert results[0] == results[1]


def test_fill_accepts_tasks_of_an_older_sealed_version(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q011")[0] == 0
    proj = project.load_project(root)
    first = project.select_prompt_set(root)
    seal_prompts(root, proj, clean_prompts()[:3], previous=first)  # 新版本里已没有 Q011
    fieldnames, rows = read_tasks(out / "tasks.csv")
    write_tasks(out / "tasks.csv", fieldnames, [answered(rows[0])])
    code, data = fill(root, out / "tasks.csv")
    assert (code, data["written"]) == (0, 1)
    record = project.load_observations(root)[0]
    assert record["prompt_set_hash"] == first["content_hash"], (
        "按任务所属的题库版本记录，不与新版混淆"
    )
    assert record["query_id"] == "Q011"


def test_fill_tolerates_missing_optional_columns(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001")[0] == 0
    _, rows = read_tasks(out / "tasks.csv")
    fieldnames = [*READ_ONLY_COLUMNS, "answer_text", "entry_features", "started_at", "ended_at"]
    row = {name: value for name, value in answered(rows[0]).items() if name in fieldnames}
    write_tasks(out / "tasks.csv", fieldnames, [row])
    code, data = fill(root, out / "tasks.csv")
    assert (code, data["written"]) == (0, 1)
    record = project.load_observations(root)[0]
    assert spec.iter_issues("observation", record) == []
    assert record["entry_form"] == "web"
    assert (record["citations"], record["snapshots"]) == ([], [])
    for name in ("model_version", "logged_in", "history_cleared", "web_search", "region", "device"):
        assert record[name] is None, "没有回填的字段记为 null，不编造默认值"
    assert set(record["null_reasons"]) == {
        "model_version",
        "logged_in",
        "history_cleared",
        "web_search",
        "region",
        "device",
        "operator",
        "snapshots",
    }


def test_fill_unreadable_snapshot_is_a_row_error(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 快照文件存在但读不了（没有权限等）：该行报错，不能按内部错误退出
    out = tmp_path / "pack"
    assert pack(root, out, "--queries", "Q001,Q002")[0] == 0
    fieldnames, rows = read_tasks(out / "tasks.csv")
    (out / "shots").mkdir()
    (out / "shots" / "locked.png").write_bytes(b"png")
    (out / "shots" / "ok.png").write_bytes(b"png2")
    answered(rows[0], snapshot_files="shots/locked.png")
    answered(rows[1], snapshot_files="shots/ok.png")
    write_tasks(out / "tasks.csv", fieldnames, rows)
    original = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self.name == "locked.png":
            raise PermissionError(13, "Permission denied", str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    code, data = fill(root, out / "tasks.csv")
    assert code == 1
    assert data["written"] == 1
    assert [(item["line"], item["column"]) for item in data["errors"]] == [(2, "snapshot_files")]
    assert "locked.png" in data["errors"][0]["reason"]
