"""可打印 HTML 报告与原始记录索引 CSV：自包含、全部转义、水印、防公式注入、逐字节可复现。"""

from __future__ import annotations

import codecs
import csv
import io
import re
from pathlib import Path
from typing import Any

import pytest
from helpers_report import (
    FORBIDDEN,
    NOW,
    PRECHECK_NOTICE,
    SYNTHETIC_WATERMARK,
    Case,
    alerts_doc,
    build_case,
    crosscheck_doc,
    evidence_doc,
    metric,
)

from geo_verify.jsonl import append_jsonl, read_jsonl

ATTACK = "<script>alert(1)</script>"
ESCAPED_ATTACK = "&lt;script&gt;alert(1)&lt;/script&gt;"
IMG_ATTACK = '<img src=x onerror="alert(2)">'
CSV_COLUMNS = [
    "line",
    "observation_id",
    "trace_id",
    "query_id",
    "platform",
    "channel",
    "channel_label",
    "window_id",
    "attempt",
    "started_at",
    "local_date",
    "model_version",
    "status",
    "invalid_reason",
    "in_selected_prompt_set",
    "excluded",
    "exclusion_rule",
    "duplicate",
    "snapshots",
    "citations",
    "answer_excerpt",
]


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return build_case(tmp_path / "proj")


def _html(case: Case) -> str:
    return (case.root / "out" / "report.html").read_text(encoding="utf-8")


def _csv(case: Case) -> tuple[bytes, list[dict[str, str]]]:
    raw = (case.root / "out" / "observations.csv").read_bytes()
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    assert rows[0] == CSV_COLUMNS
    return raw, [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]


def test_default_formats_write_all_three_files(case: Case) -> None:
    code, data = case.report()
    assert code == 0
    assert data["outputs"] == {
        "json": "out/report.json",
        "html": "out/report.html",
        "csv": "out/observations.csv",
    }
    for name in ("report.json", "report.html", "observations.csv"):
        assert (case.root / "out" / name).is_file()


def test_formats_option_selects_files(case: Case) -> None:
    code, data = case.report("--formats", "csv,html")
    assert code == 0
    assert set(data["outputs"]) == {"html", "csv"}
    assert not (case.root / "out" / "report.json").exists()


def test_html_is_self_contained_and_printable(case: Case) -> None:
    case.report()
    html = _html(case)
    assert html.startswith("<!DOCTYPE html>")
    assert '<meta charset="utf-8">' in html
    lowered = html.lower()
    for forbidden in (
        "<script",
        "<link",
        "src=",
        "@import",
        "url(",
        "<iframe",
        "<img",
        "<object",
        "http://",
        "https://",
    ):
        assert forbidden not in lowered, forbidden
    assert "@media print" in html


def test_html_shows_notice_verdict_rules_disclosure_and_signatures(case: Case) -> None:
    case.write("crosscheck", crosscheck_doc(case))
    case.report()
    html = _html(case)
    report = case.read_report()
    assert PRECHECK_NOTICE in html
    assert "预检结论：通过" in html
    for item in report["rules"]:
        assert item["rule_id"] in html
    assert "此公式是本项目的定义" in html
    assert "API 通道" in html
    assert "利益关系披露" in html
    assert "本样例数据全部为合成数据；工具操作方与服务方为同一主体（虚构）。" in html
    assert "工具操作方与服务方为同一主体（示例服务方（虚构））" in html
    assert "人工签字" in html
    for role in ("委托方", "服务方", "复核人"):
        assert role in html
    assert "哈希只能证明" in html
    assert "Q003" in html  # 抽样复核清单


def test_html_shows_reservations_and_failed_rules(case: Case) -> None:
    case.write("alerts", alerts_doc("alert"))
    case.report()
    html = _html(case)
    assert "预检结论：有条件通过" in html
    assert "保留意见" in html
    assert "提及率从 0.80 降至 0.40（测试构造）" in html
    case.write("evidence", evidence_doc("fail"))
    case.report()
    assert "预检结论：不通过" in _html(case)


def test_synthetic_watermark_is_visible_on_every_page(case: Case) -> None:
    case.report()
    html = _html(case)
    assert html.count(SYNTHETIC_WATERMARK) >= 3
    assert re.search(r"\.watermark\s*\{[^}]*position:\s*fixed", html)
    assert 'class="watermark"' in html


def test_real_project_html_has_no_watermark(tmp_path: Path) -> None:
    case = build_case(tmp_path / "proj", project_fields={"synthetic": False})
    case.report()
    html = _html(case)
    assert SYNTHETIC_WATERMARK not in html
    assert 'class="watermark"' not in html


def test_generation_time_only_from_now(case: Case) -> None:
    case.report()
    assert NOW in _html(case)
    case.report(now=None)
    html = _html(case)
    assert "生成时间" not in html
    assert NOW not in html


def _hostile_case(root: Path) -> Case:
    def mutate(observations: list[dict[str, Any]]) -> None:
        observations[0]["answer_text"] = f"推荐澜序 S7 {ATTACK} 结束"
        observations[2]["answer_text"] = IMG_ATTACK

    case = build_case(
        root,
        prompt_fields={"Q003": {"text": f"题目里有脚本 {ATTACK}"}},
        mutate_observations=mutate,
        project_fields={"name": f'恶意项目名 "{ATTACK}" & 其他'},
    )
    entities = read_jsonl(root / "entities.jsonl")
    entities[0]["name"] = f"澜序{ATTACK}"
    (root / "entities.jsonl").unlink()
    append_jsonl(root / "entities.jsonl", entities)
    alerts = alerts_doc("alert")
    alerts["alerts"][0]["message"] = f"告警 {ATTACK}"
    alerts["leads"][0]["text"] = f"线索 {IMG_ATTACK}，待核查"
    case.write("alerts", alerts)
    return case


def test_html_escapes_answers_prompts_and_entity_names(tmp_path: Path) -> None:
    case = _hostile_case(tmp_path / "proj")
    code, _ = case.report()
    assert code == 0
    html = _html(case)
    lowered = html.lower()
    assert "<script" not in lowered
    assert "<img" not in lowered
    assert "onerror=" not in lowered.replace("onerror=&quot;", "")
    assert html.count(ESCAPED_ATTACK) >= 5  # 项目名、实体名、题目、回答摘要、告警
    assert "澜序" + ESCAPED_ATTACK in html
    assert "题目里有脚本 " + ESCAPED_ATTACK in html
    assert "推荐澜序 S7 " + ESCAPED_ATTACK + " 结束" in html
    assert "&lt;img src=x onerror=&quot;alert(2)&quot;&gt;" in html
    assert "恶意项目名 &quot;" + ESCAPED_ATTACK + "&quot; &amp; 其他" in html
    # JSON 里保留原文，交给机器复核
    report = case.read_report()
    assert report["project"]["target"]["name"] == f"澜序{ATTACK}"


def test_csv_index_has_bom_and_one_row_per_observation_line(case: Case) -> None:
    case.report()
    raw, rows = _csv(case)
    assert raw.startswith(codecs.BOM_UTF8)
    assert b"\r\n" in raw
    assert len(rows) == 12
    assert [row["line"] for row in rows] == [str(index) for index in range(1, 13)]
    by_id = {row["observation_id"]: row for row in rows}
    assert by_id["obs-000002"]["status"] == "invalid"
    assert by_id["obs-000002"]["invalid_reason"] == "timeout"
    assert by_id["obs-000002"]["answer_excerpt"] == ""
    assert by_id["obs-000004"]["in_selected_prompt_set"] == "false"
    assert by_id["obs-000001"]["in_selected_prompt_set"] == "true"
    assert (by_id["obs-000005"]["excluded"], by_id["obs-000005"]["exclusion_rule"]) == (
        "true",
        "EX-DUP",
    )
    assert (by_id["obs-000001"]["excluded"], by_id["obs-000001"]["exclusion_rule"]) == ("false", "")
    assert by_id["obs-000001"]["citations"] == "1"
    assert by_id["obs-000001"]["snapshots"] == "0"
    assert by_id["obs-000001"]["channel_label"] == "API 通道"
    assert by_id["obs-000002"]["channel_label"] == "人工采集通道"
    # 上海时间：W2 的 2026-10-09T17:30Z 属于 10 月 10 日
    assert by_id["obs-000007"]["window_id"] == "W2"
    assert by_id["obs-000007"]["local_date"] == "2026-10-10"
    assert by_id["obs-000001"]["local_date"] == "2026-10-08"
    assert by_id["obs-000001"]["answer_excerpt"] == "可以考虑澜序 S7。"
    assert {row["duplicate"] for row in rows} == {"false"}


def test_csv_guards_formula_triggers(tmp_path: Path) -> None:
    tab, carriage_return = chr(9), chr(13)

    def mutate(observations: list[dict[str, Any]]) -> None:
        observations[0]["answer_text"] = '=HYPERLINK("http://evil.example","点我")'
        observations[2]["answer_text"] = "+1 澜序 S7"
        observations[4]["answer_text"] = "-1 岑野 K5"
        observations[5]["answer_text"] = "@SUM(A1:A9)"
        observations[6]["model_version"] = tab + "=cmd"
        observations[7]["trace_id"] = carriage_return + "=1+1"
        observations[8]["answer_text"] = "第一行" + chr(10) + "第二行" + tab + "第三段"

    case = build_case(tmp_path / "proj", mutate_observations=mutate)
    case.report("--formats", "csv")
    _, rows = _csv(case)
    by_id = {row["observation_id"]: row for row in rows}
    assert by_id["obs-000001"]["answer_excerpt"] == '\'=HYPERLINK("http://evil.example","点我")'
    assert by_id["obs-000003"]["answer_excerpt"] == "'+1 澜序 S7"
    assert by_id["obs-000005"]["answer_excerpt"] == "'-1 岑野 K5"
    assert by_id["obs-000006"]["answer_excerpt"] == "'@SUM(A1:A9)"
    assert by_id["obs-000007"]["model_version"] == "'" + tab + "=cmd"
    assert by_id["obs-000008"]["trace_id"] == "'" + carriage_return + "=1+1"
    # 摘要把换行和制表符折叠为空格
    assert by_id["obs-000009"]["answer_excerpt"] == "第一行 第二行 第三段"


def test_long_answers_are_truncated_in_the_excerpt(tmp_path: Path) -> None:
    long_answer = "澜序" * 50  # 100 个字符

    def mutate(observations: list[dict[str, Any]]) -> None:
        observations[0]["answer_text"] = long_answer

    case = build_case(tmp_path / "proj", mutate_observations=mutate)
    case.report("--formats", "csv")
    _, rows = _csv(case)
    assert rows[0]["answer_excerpt"] == long_answer[:80] + "…"


def test_duplicate_observation_lines_are_flagged(tmp_path: Path) -> None:
    def mutate(observations: list[dict[str, Any]]) -> None:
        observations.append(dict(observations[0]))

    case = build_case(tmp_path / "proj", mutate_observations=mutate)
    case.report("--formats", "csv,json")
    _, rows = _csv(case)
    assert len(rows) == 13
    assert [row["duplicate"] for row in rows].count("true") == 1
    assert rows[-1]["observation_id"] == "obs-000001"
    assert rows[-1]["duplicate"] == "true"
    report = case.read_report()
    assert report["observation_index"]["total"] == 13
    assert report["project"]["period"]["observations"]["total"] == 12


def test_all_three_formats_are_byte_identical_across_directories(tmp_path: Path) -> None:
    first = build_case(tmp_path / "a" / "proj")
    second = build_case(tmp_path / "b" / "elsewhere" / "proj")
    for current in (first, second):
        assert current.report()[0] == 0
    for name in ("report.json", "report.html", "observations.csv"):
        one = (first.root / "out" / name).read_bytes()
        assert one == (second.root / "out" / name).read_bytes(), name
        assert str(tmp_path).encode() not in one


SCENARIOS = {
    "pass": lambda case: None,
    "fail_evidence": lambda case: case.write("evidence", evidence_doc("fail")),
    "fail_crosscheck": lambda case: case.write("crosscheck", crosscheck_doc(case, "fail")),
    "undetermined": lambda case: case.write("crosscheck", crosscheck_doc(case, "undetermined")),
    "alerts": lambda case: case.write("alerts", alerts_doc("alert")),
    "no_metrics": lambda case: case.remove("metrics"),
    "null_value": lambda case: case.metrics([metric("mention_rate", None, 0, 0, sufficient=False)]),
    "nothing_but_project": lambda case: [
        case.remove(name)
        for name in (
            "metrics",
            "crosscheck",
            "compare",
            "alerts",
            "evidence",
            "disputes",
            "plan",
            "result",
        )
    ],
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_html_and_csv_have_no_forbidden_wording(case: Case, scenario: str) -> None:
    SCENARIOS[scenario](case)
    case.report()
    texts = [_html(case), (case.root / "out" / "observations.csv").read_text(encoding="utf-8-sig")]
    for text in texts:
        for phrase in FORBIDDEN:
            assert phrase not in text, phrase


def test_text_output_summarises_the_verdict(case: Case) -> None:
    case.write("alerts", alerts_doc("alert"))
    code, out = case.report_text()
    assert code == 0
    assert PRECHECK_NOTICE in out
    assert SYNTHETIC_WATERMARK in out
    assert "预检结论：有条件通过" in out
    assert "[有保留] R-ALERTS" in out
    assert "保留意见" in out
    assert "out/report.html" in out
