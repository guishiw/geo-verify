"""CSV / JSONL 观测导入：模板、逐行错误、去重与续导。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from factory import make_observation, make_project, make_prompt, seal_prompts
from helpers_basic import clean_prompts, sealed_project, write_entities
from helpers_collect import IMPORT_FIELDS, csv_text, import_row, independent_digest, write_csv

from geo_verify import audit, project, spec
from geo_verify.jsonl import append_jsonl, read_json

NOW = "2026-10-08T06:00:00Z"

EXPECTED_COLUMNS = [
    "trace_id",
    "observation_id",
    "query_id",
    "prompt_text",
    "prompt_set_hash",
    "platform",
    "channel",
    "entry_form",
    "model_version",
    "entry_features",
    "window_id",
    "attempt",
    "started_at",
    "ended_at",
    "logged_in",
    "history_cleared",
    "web_search",
    "region",
    "device",
    "answer_text",
    "citations",
    "snapshots",
    "status",
    "invalid_reason",
    "operator",
    "collector_tool",
    "collector_version",
    "null_reasons",
    "ext",
    "synthetic",
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    sealed_project(path)
    return path


def do_import(root: Path, source: Path, *extra: str) -> tuple[int, Any]:
    return run_cli_json("import", str(root), "--input", str(source), "--now", NOW, *extra)


def observations(root: Path) -> list[dict[str, Any]]:
    return project.load_observations(root)


# ------------------------------------------------------------------ 模板


def test_csv_template_lists_all_columns_and_is_importable(root: Path, tmp_path: Path) -> None:
    code, out, _ = run_cli("import", "--template", "csv")
    assert code == 0
    assert out.splitlines()[0].split(",") == EXPECTED_COLUMNS

    target = tmp_path / "template.csv"
    code, data = run_cli_json("import", "--template", "csv", "--out", str(target))
    assert code == 0
    assert [item["name"] for item in data["columns"]] == EXPECTED_COLUMNS
    assert {item["name"] for item in data["columns"] if item["required"] == "是"} == {
        "query_id",
        "platform",
        "window_id",
        "attempt",
        "started_at",
        "ended_at",
        "status",
    }
    assert target.read_bytes().startswith(b"\xef\xbb\xbf"), "CSV 模板带 BOM，便于表格软件识别中文"
    code, data = do_import(root, target)
    assert code == 0
    assert data["written"] == 1
    record = observations(root)[0]
    assert record["channel"] == "manual"
    assert record["started_at"] == "2026-10-08T02:00:00Z", "+08:00 的 10:00 换算为 UTC 02:00"
    assert [item["placement"] for item in record["citations"]] == ["inline", "source_list"]

    code, data = run_cli_json("import", "--template", "csv", "--out", str(target))
    assert code == 2, "不覆盖已有文件"


def test_jsonl_template_is_importable(root: Path, tmp_path: Path) -> None:
    target = tmp_path / "template.jsonl"
    code, _out, _ = run_cli("import", "--template", "jsonl", "--out", str(target))
    assert code == 0
    sample = json.loads(target.read_text(encoding="utf-8"))
    assert sample["query_id"] == "Q001"
    code, data = do_import(root, target)
    assert code == 0
    assert data["written"] == 1
    assert observations(root)[0]["citations"][1]["domain"] == "auto-review.example"


def test_import_without_project_or_input_is_an_input_error(root: Path) -> None:
    assert run_cli_json("import")[0] == 2
    assert run_cli_json("import", str(root))[0] == 2


# ------------------------------------------------------------------ 正常导入


def test_csv_import_builds_contract_valid_observations(root: Path, tmp_path: Path) -> None:
    sealed_hash = project.select_prompt_set(root)["content_hash"]
    rows = [
        import_row(
            citations=json.dumps(
                [
                    {
                        "url": "https://lanxu-auto.example/s7/spec",
                        "placement": "inline",
                        "title": "参数",
                    },
                    {"url": "https://auto-review.example/ev-suv", "placement": "source_list"},
                    {"url": "https://lanxu-auto.example/s7/spec", "placement": "inline"},
                ]
            ),
        ),
        import_row(
            query_id="Q002",
            channel="manual",
            entry_form="app",
            model_version="",
            entry_features="App 默认模式，未展示模型名",
            logged_in="是",
            history_cleared="否",
            web_search="",
            region="CN-SH",
            device="phone",
            started_at="2026-10-08T10:00:00+08:00",
            ended_at="2026-10-08T10:00:30+08:00",
            citations="inline | https://lanxu-auto.example/a | 标题甲\nsource_list | not a url",
            operator="",
        ),
        import_row(
            query_id="Q003",
            attempt="2",
            answer_text="",
            status="invalid",
            invalid_reason="timeout",
        ),
    ]
    source = write_csv(tmp_path / "in.csv", IMPORT_FIELDS, rows)
    code, data = do_import(root, source, "--operator", "导入员乙（虚构）")
    assert code == 0
    assert data["rows"] == 3
    assert data["written"] == 3
    assert data["valid"] == 2
    assert data["invalid"] == 1
    assert data["invalid_by_reason"] == {"timeout": 1}
    assert data["rejected_rows"] == 0
    assert data["errors"] == []

    records = observations(root)
    assert len(records) == 3
    for record in records:
        assert spec.iter_issues("observation", record) == []
        assert record["project_id"] == "auto-demo"
        assert record["prompt_set_hash"] == sealed_hash
        assert record["synthetic"] is True

    first, second, third = records
    digest = independent_digest("auto-demo", sealed_hash, "engine-a", "api", "W1", "Q001", 1)
    assert first["trace_id"] == f"t-{digest}"
    assert first["observation_id"] == f"o-{digest}"
    assert first["prompt_text"] == "澜序这个品牌怎么样？", "提问原文默认取封存题目"
    assert first["channel"] == "api"
    assert first["entry_form"] == "api"
    assert first["operator"] == "采集员甲（虚构）"
    assert [(c["order"], c["domain"], c["placement"], c["title"]) for c in first["citations"]] == [
        (1, "lanxu-auto.example", "inline", "参数"),
        (2, "auto-review.example", "source_list", None),
        (3, "lanxu-auto.example", "inline", None),
    ], "重复引用原样保留"
    assert first["logged_in"] is None
    assert set(first["null_reasons"]) == {"logged_in", "region", "device", "snapshots"}

    assert second["channel"] == "manual"
    assert second["entry_form"] == "app"
    assert second["model_version"] is None
    assert second["entry_features"] == "App 默认模式，未展示模型名"
    assert (second["logged_in"], second["history_cleared"], second["web_search"]) == (
        True,
        False,
        None,
    )
    assert second["started_at"] == "2026-10-08T02:00:00Z"
    assert second["ended_at"] == "2026-10-08T02:00:30Z"
    assert second["operator"] == "导入员乙（虚构）", "行内未写采集人时取 --operator"
    assert second["citations"][0] == {
        "order": 1,
        "url": "https://lanxu-auto.example/a",
        "domain": "lanxu-auto.example",
        "title": "标题甲",
        "placement": "inline",
    }
    assert second["citations"][1]["domain"] is None, "解析不出域名时为 null"
    assert {"model_version", "web_search", "snapshots"} <= set(second["null_reasons"])

    assert third["status"] == "invalid"
    assert third["invalid_reason"] == "timeout"
    assert third["answer_text"] is None
    assert "answer_text" in third["null_reasons"]
    assert third["attempt"] == 2

    events = project.load_audit(root)
    assert [event["action"] for event in events] == ["import"]
    assert events[0]["actor"] == "导入员乙（虚构）"
    assert events[0]["occurred_at"] == NOW
    assert events[0]["before_hash"] is None
    assert (
        events[0]["after_hash"]
        == hashlib.sha256((root / "observations.jsonl").read_bytes()).hexdigest()
    )
    assert events[0]["details"]["written"] == 3
    assert events[0]["details"]["source_file"] == "in.csv"
    assert events[0]["details"]["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert audit.verify_chain(events) == []


def test_text_output_summarises_counts(root: Path, tmp_path: Path) -> None:
    source = write_csv(tmp_path / "in.csv", IMPORT_FIELDS, [import_row()])
    code, out, _ = run_cli("import", str(root), "--input", str(source), "--now", NOW)
    assert code == 0
    assert "写入 1 条" in out
    assert "非法 0 行" in out


def test_bom_and_multiline_cells_are_handled(root: Path, tmp_path: Path) -> None:
    rows = [
        import_row(answer_text='第一行\n第二行，含逗号和"引号"'),
        import_row(query_id="Q002"),
    ]
    source = tmp_path / "in.csv"
    source.write_text(csv_text(IMPORT_FIELDS, rows), encoding="utf-8-sig", newline="")
    code, data = do_import(root, source)
    assert code == 0
    assert data["written"] == 2
    assert observations(root)[0]["answer_text"] == '第一行\n第二行，含逗号和"引号"'


# ------------------------------------------------------------------ 逐行错误


def test_row_errors_report_line_column_and_reason(root: Path, tmp_path: Path) -> None:
    rows = [
        import_row(),  # 第 2 行：合法
        import_row(query_id="Q999"),  # 3：题库外的题目
        import_row(query_id="Q002", status=""),  # 4：缺 status
        import_row(query_id="Q003", started_at="2026-10-08 10:00:00"),  # 5：时间没有时区
        import_row(query_id="Q004", ended_at="2026-10-08T01:59:00Z"),  # 6：结束早于开始
        import_row(query_id="Q005", logged_in="也许"),  # 7：无法识别的是否
        import_row(query_id="Q006", citations="[{"),  # 8：引用不是合法 JSON
        import_row(
            query_id="Q007", citations='[{"url": "https://a.example/x"}]'
        ),  # 9：缺 placement
        import_row(query_id="Q008", platform="engine-z"),  # 10：平台未登记
        import_row(query_id="Q009", platform="engine-b", channel="manual", entry_form="web"),  # 11
        import_row(query_id="Q010", answer_text=""),  # 12：有效观测却没有回答
        import_row(query_id="Q011", attempt="0"),  # 13：次数非法
        import_row(query_id="Q011", attempt="3", status="invalid", invalid_reason=""),  # 14
        import_row(query_id="Q011", attempt="4", channel="manual", entry_form=""),  # 15
        import_row(query_id="Q011", attempt="5", model_version=""),  # 16：缺入口特征
        import_row(query_id="Q011", attempt="6", status="done"),  # 17：status 取值非法
        import_row(query_id="Q011", attempt="7"),  # 第 18 行：合法
    ]
    source = write_csv(tmp_path / "in.csv", IMPORT_FIELDS, rows)
    code, data = do_import(root, source)
    assert code == 1
    assert data["rows"] == 17
    assert data["written"] == 2
    assert data["rejected_rows"] == 15
    located = {(item["line"], item["column"]) for item in data["errors"]}
    assert located == {
        (3, "query_id"),
        (4, "status"),
        (5, "started_at"),
        (6, "ended_at"),
        (7, "logged_in"),
        (8, "citations"),
        (9, "citations"),
        (10, "platform"),
        (11, "channel"),
        (12, "answer_text"),
        (13, "attempt"),
        (14, "invalid_reason"),
        (15, "entry_form"),
        (16, "entry_features"),
        (17, "status"),
    }
    reasons = {item["line"]: item["reason"] for item in data["errors"]}
    assert "Q999" in reasons[3]
    assert "时区" in reasons[5]
    assert "早于" in reasons[6]
    assert "placement" in reasons[9]
    assert "engine-z" in reasons[10]
    assert [item["query_id"] for item in observations(root)] == ["Q001", "Q011"]

    report = read_json(root / "out" / "import-errors.json")
    assert report["input"] == "in.csv"
    assert len(report["errors"]) == data["errors_total"] == 15

    code, out, _ = run_cli("import", str(root), "--input", str(source), "--now", NOW)
    assert code == 1
    assert "第 3 行 [query_id]" in out
    assert "非法 15 行" in out


def test_line_numbers_account_for_multiline_cells(root: Path, tmp_path: Path) -> None:
    rows = [
        import_row(answer_text="甲\n乙\n丙"),  # 占第 2～4 行
        import_row(query_id="Q002", status="bad"),  # 从第 5 行开始
    ]
    source = write_csv(tmp_path / "in.csv", IMPORT_FIELDS, rows)
    code, data = do_import(root, source)
    assert code == 1
    assert [(item["line"], item["column"]) for item in data["errors"]] == [(5, "status")]


def test_header_problems_are_input_errors(root: Path, tmp_path: Path) -> None:
    source = write_csv(tmp_path / "a.csv", [*IMPORT_FIELDS, "mood"], [import_row()])
    code, data = do_import(root, source)
    assert code == 2
    assert "mood" in data["message"]
    fields = [name for name in IMPORT_FIELDS if name != "status"]
    code, data = do_import(root, write_csv(tmp_path / "b.csv", fields, [import_row()]))
    assert code == 2
    assert "status" in data["message"]
    (tmp_path / "c.csv").write_text("query_id,query_id\n", encoding="utf-8")
    assert do_import(root, tmp_path / "c.csv")[0] == 2
    (tmp_path / "d.csv").write_text("", encoding="utf-8")
    assert do_import(root, tmp_path / "d.csv")[0] == 2
    (tmp_path / "e.csv").write_bytes(b"query_id," + bytes([0xFF, 0xFE]) + b"\n")
    code, data = do_import(root, tmp_path / "e.csv")
    assert code == 2
    assert "UTF-8" in data["message"]
    assert do_import(root, tmp_path / "missing.csv")[0] == 2
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    code, data = do_import(root, tmp_path / "f.txt")
    assert code == 2
    assert "--format" in data["message"]
    assert observations(root) == []


def test_extra_cells_are_reported(root: Path, tmp_path: Path) -> None:
    text = csv_text(IMPORT_FIELDS, [import_row()]).rstrip("\n") + ",多出来的\n"
    source = tmp_path / "in.csv"
    source.write_text(text, encoding="utf-8")
    code, data = do_import(root, source)
    assert code == 1
    assert [(item["line"], item["column"]) for item in data["errors"]] == [(2, None)]


def test_import_requires_sealed_prompts(tmp_path: Path) -> None:
    path = tmp_path / "unsealed"
    make_project(path)
    write_entities(path)
    source = write_csv(tmp_path / "in.csv", IMPORT_FIELDS, [import_row()])
    code, data = do_import(path, source)
    assert code == 2
    assert "尚未封存" in data["message"]


# ------------------------------------------------------------------ 去重与续导


def test_reimport_skips_existing_and_counts(root: Path, tmp_path: Path) -> None:
    rows = [import_row(query_id=f"Q{index:03d}") for index in range(1, 6)]
    source = write_csv(tmp_path / "in.csv", IMPORT_FIELDS, rows)
    assert do_import(root, source)[1]["written"] == 5
    size = (root / "observations.jsonl").stat().st_size

    code, data = do_import(root, source, "--resume")
    assert code == 0
    assert data["written"] == 0
    assert data["skipped_existing"] == 5
    assert (root / "observations.jsonl").stat().st_size == size, "重复执行不得重复写入"
    assert len(project.load_audit(root)) == 1, "没有写入时不产生审计事件"

    code, data = do_import(root, source)
    assert (data["written"], data["skipped_existing"]) == (0, 5)


def test_resume_after_fixing_a_bad_row_only_writes_the_fixed_row(
    root: Path, tmp_path: Path
) -> None:
    rows = [
        import_row(),
        import_row(query_id="Q002", status="oops"),
        import_row(query_id="Q003"),
    ]
    source = write_csv(tmp_path / "in.csv", IMPORT_FIELDS, rows)
    code, data = do_import(root, source)
    assert code == 1
    assert (data["written"], data["rejected_rows"]) == (2, 1)

    rows[1]["status"] = "valid"
    write_csv(source, IMPORT_FIELDS, rows)
    code, data = do_import(root, source, "--resume")
    assert code == 0
    assert (data["written"], data["skipped_existing"], data["rejected_rows"]) == (1, 2, 0)
    assert [item["query_id"] for item in observations(root)] == ["Q001", "Q003", "Q002"]
    assert read_json(root / "out" / "import-errors.json")["errors"] == []


def test_duplicate_trace_inside_one_file_is_written_once(root: Path, tmp_path: Path) -> None:
    rows = [import_row(), import_row(answer_text="另一份回答"), import_row(query_id="Q002")]
    source = write_csv(tmp_path / "in.csv", IMPORT_FIELDS, rows)
    code, data = do_import(root, source)
    assert code == 0
    assert data["written"] == 2
    assert data["skipped_duplicate_in_input"] == 1
    assert data["duplicates"][0]["line"] == 3
    assert data["duplicates"][0]["first_line"] == 2
    assert observations(root)[0]["answer_text"] == "可以考虑澜序 S7。"


def test_explicit_trace_id_is_respected_and_deduplicated(root: Path, tmp_path: Path) -> None:
    fields = ["trace_id", "observation_id", *IMPORT_FIELDS]
    rows = [
        import_row(trace_id="trace-A", observation_id="obs-A"),
        import_row(trace_id="trace-B", observation_id="obs-A", query_id="Q002"),
    ]
    source = write_csv(tmp_path / "in.csv", fields, rows)
    code, data = do_import(root, source)
    assert code == 1
    assert data["written"] == 1
    assert [(item["line"], item["column"]) for item in data["errors"]] == [(3, "observation_id")]
    assert observations(root)[0]["trace_id"] == "trace-A"

    rows[0]["status"] = "broken"  # 已导入过的行即使后来被改坏，也只是跳过
    write_csv(source, fields, rows[:1])
    code, data = do_import(root, source)
    assert code == 0
    assert data["skipped_existing"] == 1


# ------------------------------------------------------------------ 版本与快照


def test_prompt_set_hash_must_be_a_sealed_version(root: Path, tmp_path: Path) -> None:
    proj = read_json(root / "project.json")
    first = project.select_prompt_set(root)
    second = seal_prompts(
        root,
        proj,
        [*clean_prompts(), make_prompt("Q050", text="澜序 X5 的后备厢容积有多大？")],
        previous=first,
    )
    fields = ["prompt_set_hash", "prompt_text", *IMPORT_FIELDS]
    rows = [
        import_row(prompt_set_hash=first["content_hash"]),
        import_row(query_id="Q050"),
        import_row(query_id="Q050", attempt="2", prompt_set_hash=first["content_hash"]),
        import_row(query_id="Q002", prompt_set_hash="0" * 64),
        import_row(query_id="Q003", prompt_text="澜序 S7 的续航到底有多少？"),
    ]
    source = write_csv(tmp_path / "in.csv", fields, rows)
    code, data = do_import(root, source)
    assert code == 1
    assert {(item["line"], item["column"]) for item in data["errors"]} == {
        (4, "query_id"),
        (5, "prompt_set_hash"),
    }
    assert "第 1 版" in next(item["reason"] for item in data["errors"] if item["line"] == 4)
    records = observations(root)
    assert [record["prompt_set_hash"] for record in records] == [
        first["content_hash"],
        second["content_hash"],
        second["content_hash"],
    ]
    assert data["prompt_text_differs"] == 1
    assert records[2]["prompt_text"] == "澜序 S7 的续航到底有多少？", "实际提问原文按原样记录"


def test_snapshots_are_hashed_and_checked(root: Path, tmp_path: Path) -> None:
    shot = root / "snapshots" / "q1.html"
    shot.parent.mkdir()
    shot.write_bytes("<html>虚构快照</html>".encode())
    digest = hashlib.sha256(shot.read_bytes()).hexdigest()
    declared = "ab" * 32
    rows = [
        import_row(snapshots="snapshots/q1.html"),
        import_row(query_id="Q002", snapshots="snapshots/missing.png"),
        import_row(
            query_id="Q003",
            snapshots=json.dumps([{"path": "snapshots/q1.html", "sha256": declared}]),
        ),
        import_row(
            query_id="Q004",
            snapshots=json.dumps(
                [{"path": "snapshots/later.png", "sha256": declared, "media_type": "image/png"}]
            ),
        ),
        import_row(query_id="Q005", snapshots="../outside.txt"),
        import_row(query_id="Q006", snapshots="snapshots/q1.html; snapshots/q1.html"),
    ]
    source = write_csv(tmp_path / "in.csv", IMPORT_FIELDS, rows)
    code, data = do_import(root, source)
    assert code == 1
    assert {(item["line"], item["column"]) for item in data["errors"]} == {
        (3, "snapshots"),
        (4, "snapshots"),
        (6, "snapshots"),
    }
    records = {record["query_id"]: record for record in observations(root)}
    assert records["Q001"]["snapshots"] == [
        {"path": "snapshots/q1.html", "sha256": digest, "media_type": "text/html"}
    ]
    assert "snapshots" not in records["Q001"]["null_reasons"]
    assert records["Q004"]["snapshots"] == [
        {"path": "snapshots/later.png", "sha256": declared, "media_type": "image/png"}
    ]
    assert len(records["Q006"]["snapshots"]) == 2


# ------------------------------------------------------------------ JSONL


def test_jsonl_import_accepts_full_observations(root: Path, tmp_path: Path) -> None:
    proj = read_json(root / "project.json")
    sealed = project.select_prompt_set(root)
    full = [
        make_observation(proj, sealed, 1),
        make_observation(proj, sealed, 2, query_id="Q002", status="invalid"),
        make_observation(proj, sealed, 3, query_id="Q003", channel="manual", model_version=None),
    ]
    source = tmp_path / "in.jsonl"
    append_jsonl(source, full)
    code, data = do_import(root, source)
    assert code == 0
    assert data["written"] == 3
    assert data["format"] == "jsonl"
    assert observations(root) == full, "完整的观测记录原样进入，不被改写"

    code, data = do_import(root, source, "--resume")
    assert (data["written"], data["skipped_existing"]) == (0, 3)


def test_jsonl_row_errors(root: Path, tmp_path: Path) -> None:
    proj = read_json(root / "project.json")
    sealed = project.select_prompt_set(root)
    good = make_observation(proj, sealed, 1)
    wrong_project = dict(make_observation(proj, sealed, 2), project_id="other")
    unknown_key = dict(make_observation(proj, sealed, 3), mood="happy")
    bad_types = dict(make_observation(proj, sealed, 4), query_id=7, attempt="1", collector="x")
    lines = [
        json.dumps(good, ensure_ascii=False),
        "{not json",
        "",
        json.dumps(wrong_project, ensure_ascii=False),
        "[1, 2]",
        json.dumps(unknown_key, ensure_ascii=False),
        json.dumps(bad_types, ensure_ascii=False),
    ]
    source = tmp_path / "in.data"
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, data = do_import(root, source, "--format", "jsonl")
    assert code == 1
    assert data["rows"] == 6, "空行不计"
    assert data["written"] == 1
    located = {(item["line"], item["column"]) for item in data["errors"]}
    assert located == {
        (2, None),
        (4, "project_id"),
        (5, None),
        (6, "mood"),
        (7, "collector"),
    }
