"""浏览器辅助插件导出文件（`geo-verify-capture/1`）经 `collect --adapter share` 导入。

格式见 extension/FORMAT.md。期望值按 extension/examples/capture.example.json 的内容手工写出：
通道为 browser_assist；入口形态与环境字段取文件中的 environment；保存时间如实记为开始、结束时间，
并在 ext 中注明“时间为用户点击保存的时间，非提问时间”；文件中的平台 / 窗口 / 题目编号与命令行
不一致时以命令行为准并给出警告；未填写的环境字段为 null 并写明原因。全部数据为虚构。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, run_cli_json
from factory import make_prompt
from helpers_basic import sealed_project
from helpers_collect import independent_digest

from geo_verify import project, spec
from geo_verify.canon import sha256_file

NOW = "2026-10-20T08:00:00Z"
EXAMPLE = REPO_ROOT / "extension" / "examples" / "capture.example.json"
QUESTION = "澜序 S7 长续航版的 CLTC 续航是多少？"
ANSWER = "澜序 S7 长续航版的 CLTC 续航为 620 km，WLTC 续航为 540 km。\n参考：评测文章、权威数据。"
TIME_NOTE = "时间为用户点击保存的时间，非提问时间"
PLATFORMS = [
    {
        "platform_id": "engine-a",
        "display_name": "模拟平台 A",
        "channels": ["api", "share_import", "browser_assist"],
    },
    {"platform_id": "engine-b", "display_name": "模拟平台 B", "channels": ["api", "share_import"]},
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    prompts = [
        make_prompt("Q001", text=QUESTION, intent_type="product"),
        make_prompt("Q002", text="澜序 S7 的整车质保政策是怎样的？", intent_type="after_sales"),
    ]
    sealed_project(path, prompts=prompts, platforms=PLATFORMS)
    return path


def capture(root: Path, source: Path, *extra: str, query: str = "Q001") -> tuple[int, Any]:
    return run_cli_json(
        "collect", str(root), "--adapter", "share", "--input", str(source),
        "--platform", "engine-a", "--window", "W1", "--query", query, "--now", NOW, *extra,
    )  # fmt: skip


def example() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return data


def write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def only(root: Path) -> dict[str, Any]:
    records = project.load_observations(root)
    assert len(records) == 1
    assert spec.iter_issues("observation", records[0]) == []
    return records[0]


def test_example_export_is_imported_as_browser_assist(root: Path) -> None:
    code, data = capture(root, EXAMPLE)

    assert code == 0, data
    assert data["channel"] == "browser_assist"
    assert data["format"] == "geo-verify-capture/1"
    assert data["written"] == 1
    assert data["prompt_text_differs"] is False
    assert data["time_source"] == "saved_at"
    assert data["page_url"] == "https://chat.engine-a.example/share/abc"
    assert data["warnings"] == []
    sealed = project.select_prompt_set(root)
    digest = independent_digest(
        "auto-demo", sealed["content_hash"], "engine-a", "browser_assist", "W1", "Q001", 1
    )
    record = only(root)
    assert record["trace_id"] == f"t-{digest}"
    assert record["observation_id"] == f"o-{digest}"
    assert record["channel"] == "browser_assist"
    assert record["entry_form"] == "web"
    assert record["prompt_text"] == QUESTION
    assert record["answer_text"] == ANSWER
    assert record["status"] == "valid"
    # 保存时间如实作为开始、结束时间
    assert record["started_at"] == record["ended_at"] == "2026-10-08T02:00:00Z"
    # 环境字段取自文件；未填写的为 null 并写明原因
    assert record["entry_features"] == "网页端默认模式"
    assert record["model_version"] is None
    assert record["logged_in"] is False
    assert record["history_cleared"] is True
    assert record["web_search"] is None
    assert record["region"] == "上海"
    assert record["device"] == "桌面浏览器"
    assert record["operator"] == "采集人甲"
    assert set(record["null_reasons"]) == {"model_version", "web_search"}
    assert "未填写" in record["null_reasons"]["web_search"]
    assert "未填写" in record["null_reasons"]["model_version"]
    # 链接：正文里没有 [n] 也没有链接地址，按通用规则记为仅出现在来源列表
    assert record["citations"] == [
        {
            "order": 1,
            "url": "https://auto-review.example/a",
            "domain": "auto-review.example",
            "title": "评测文章",
            "placement": "source_list",
        },
        {
            "order": 2,
            "url": "https://gov-data.example/b",
            "domain": "gov-data.example",
            "title": "权威数据",
            "placement": "source_list",
        },
    ]
    snapshot = record["snapshots"][0]
    assert snapshot["path"] == f"snapshots/o-{digest}/capture.example.json"
    assert snapshot["sha256"] == sha256_file(EXAMPLE)
    share = record["ext"]["share"]
    assert share["format"] == "geo-verify-capture/1"
    assert share["time_source"] == "saved_at"
    assert share["time_field"] == "saved_at"
    assert share["time_note"] == TIME_NOTE
    assert share["share_url"] is None
    assert share["capture"] == {
        "page_url": "https://chat.engine-a.example/share/abc",
        "page_title": "澜序 S7 续航问答（虚构示例）",
        "file_ids": {"platform": "engine-a", "window_id": "W1", "query_id": "Q001"},
        "mismatched": [],
    }
    (event,) = [item for item in project.load_audit(root) if item["action"] == "import"]
    assert event["details"]["channel"] == "browser_assist"
    assert event["details"]["format"] == "geo-verify-capture/1"
    assert event["actor"] == "采集人甲"

    # 同一文件再导入一次：跳过，不重复计数
    code, again = capture(root, EXAMPLE)
    assert (code, again["written"], again["skipped_existing"]) == (0, 0, 1)
    assert len(project.load_observations(root)) == 1


def test_command_line_ids_win_over_the_file_with_warnings(root: Path, tmp_path: Path) -> None:
    payload = example()
    payload.update(
        platform="engine-b", window_id="W9", query_id="Q002", model="engine-a-web-2026.10"
    )
    source = write(tmp_path / "mismatch.json", payload)

    code, data = capture(root, source)

    assert code == 0, data
    record = only(root)
    assert record["model_version"] == "engine-a-web-2026.10"
    assert "model_version" not in record["null_reasons"]
    assert (record["platform"], record["window_id"], record["query_id"]) == (
        "engine-a",
        "W1",
        "Q001",
    )
    text = "\n".join(data["warnings"])
    for fragment in ("engine-b", "--platform", "W9", "--window", "Q002", "--query", "以命令行为准"):
        assert fragment in text
    capture_ext = record["ext"]["share"]["capture"]
    assert capture_ext["mismatched"] == ["platform", "window_id", "query_id"]
    assert capture_ext["file_ids"] == {
        "platform": "engine-b",
        "window_id": "W9",
        "query_id": "Q002",
    }
    code, again = capture(root, source, "--attempt", "2")
    assert code == 2  # 同一文件已导入为别的任务，不能重复计数
    assert "重复计数" in again["message"]


def test_unfilled_ids_and_environment_stay_null_with_reasons(root: Path, tmp_path: Path) -> None:
    payload = example()
    payload.update(platform=None, window_id=None, query_id=None, operator=None, model=None)
    payload["environment"] = {
        "entry_form": "web",
        "entry_features": None,
        "logged_in": None,
        "history_cleared": None,
        "web_search": None,
        "region": None,
        "device": None,
    }
    source = write(tmp_path / "blank.json", payload)

    code, data = capture(root, source)

    assert code == 0, data
    assert data["warnings"] == []  # 文件里没填编号不是不一致
    record = only(root)
    for key in ("logged_in", "history_cleared", "web_search", "region", "device", "operator"):
        assert record[key] is None
        assert "未填写" in record["null_reasons"][key], key
    assert record["model_version"] is None
    assert "model_version" in record["null_reasons"]
    assert isinstance(record["entry_features"], str)
    assert "未填写入口特征" in record["entry_features"]
    assert record["ext"]["share"]["capture"]["file_ids"] == {
        "platform": None,
        "window_id": None,
        "query_id": None,
    }


def test_operator_and_entry_form_options_override_the_file(root: Path, tmp_path: Path) -> None:
    code, data = capture(root, EXAMPLE, "--operator", "复核员乙", "--entry-form", "app")

    assert code == 0, data
    record = only(root)
    assert record["operator"] == "复核员乙"
    assert record["entry_form"] == "app"
    text = "\n".join(data["warnings"])
    for fragment in ("采集人甲", "--operator", "web", "--entry-form", "以命令行为准"):
        assert fragment in text


def test_unusable_saved_time_falls_back_to_import_time(root: Path, tmp_path: Path) -> None:
    payload = example()
    payload["saved_at"] = "2026-10-08 10:00"  # 没有时区
    source = write(tmp_path / "notz.json", payload)

    code, data = capture(root, source)

    assert code == 0, data
    record = only(root)
    assert record["started_at"] == record["ended_at"] == NOW
    assert record["ext"]["share"]["time_source"] == "import_time"
    assert "导入时间" in record["ext"]["share"]["time_note"]
    assert any("保存时间" in item for item in data["warnings"])


def test_platform_without_browser_assist_channel_is_rejected(root: Path) -> None:
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "share", "--input", str(EXAMPLE),
        "--platform", "engine-b", "--window", "W1", "--query", "Q001", "--now", NOW,
    )  # fmt: skip
    assert code == 2
    assert "browser_assist" in data["message"]
    assert project.load_observations(root) == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"format": "geo-verify-capture/2"}, "geo-verify-capture/2"),
        ({"capture": "share"}, "capture 应为 browser_assist"),
        ({"environment": "web"}, "environment"),
        ({"environment": {"entry_form": "api"}}, "entry_form"),
        ({"messages": [{"role": "user", "content": QUESTION}]}, "messages"),
        ({"platform": 7}, "platform"),
        ({"operator": ["甲"]}, "operator"),
    ],
)
def test_malformed_capture_file_is_an_input_error(
    root: Path, tmp_path: Path, change: dict[str, Any], fragment: str
) -> None:
    payload = example()
    payload.update(copy.deepcopy(change))
    source = write(tmp_path / "bad.json", payload)

    code, data = capture(root, source)

    assert code == 2
    assert fragment in data["message"]
    assert project.load_observations(root) == []


def test_tri_state_fields_must_be_true_false_or_null(root: Path, tmp_path: Path) -> None:
    payload = example()
    payload["environment"]["logged_in"] = "是"
    source = write(tmp_path / "tri.json", payload)

    code, data = capture(root, source)

    assert code == 2
    assert "logged_in" in data["message"]


def test_plain_share_json_keeps_share_import_channel(root: Path, tmp_path: Path) -> None:
    payload = {
        "messages": [
            {"role": "user", "content": QUESTION},
            {"role": "assistant", "content": "（虚构回答）"},
        ],
        "format": "chat-export",  # 不是插件格式的 format 字段不影响识别
    }
    source = write(tmp_path / "plain.json", payload)

    code, data = capture(root, source)

    assert code == 0, data
    assert data["channel"] == "share_import"
    assert only(root)["channel"] == "share_import"
