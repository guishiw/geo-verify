"""`geo-verify alerts` 命令测试：窗口与自然日选择、阈值覆盖、引用域名变化与线索、
S17、措辞红线、--json、退出码、逐字节可复现。期望值由测试构造的数据手工推得。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from factory import TARGET, make_citation, read
from helpers_alerts import add, alerts_text, all_strings, banned_in, run_alerts, select
from helpers_metrics import Builder, copy_project

DEFAULT_THRESHOLDS = {
    "mention_drop": {
        "min_observations": 20,
        "min_abs_drop": 0.15,
        "min_rel_drop": 0.2,
        "alpha": 0.05,
        "critical_abs_drop": 0.3,
        "min_rankable": 10,
        "min_top3_abs_drop": 0.2,
        "min_top3_rel_drop": 0.2,
        "min_ranked": 10,
        "min_mean_rank_increase": 1.0,
        "critical_mean_rank_increase": 2.0,
    },
    "negative_spread": {
        "min_observations": 20,
        "min_abs_rise": 0.05,
        "min_rel_rise": 0.5,
        "min_current_count": 3,
        "alpha": 0.05,
        "critical_abs_rise": 0.15,
        "min_platform_count": 3,
        "min_platform_increase": 1,
        "critical_platform_increase": 2,
    },
    "citation_failure": {
        "min_verified": 20,
        "invalid_share_threshold": 0.3,
        "min_invalid_count": 5,
        "min_abs_rise": 0.15,
        "min_rel_rise": 0.5,
        "alpha": 0.05,
        "critical_share": 0.5,
    },
    "competitor_surge": {
        "min_observations": 20,
        "min_abs_rise": 0.15,
        "min_rel_rise": 0.25,
        "alpha": 0.05,
        "critical_abs_rise": 0.3,
    },
    "platform_conflict": {"min_each": 3},
    "leads": {"max_domains": 10},
}


def drop_project(root: Path, **project: Any) -> Builder:
    """单平台 engine-a：W1（10 月 8 日）24/30 提及，W2（10 月 9 日）12/30 提及 → 提及率骤降。"""
    b = Builder(root, **project)
    add(b, "W1", 30, day=8, mentioned=lambda i: i < 24)
    add(b, "W2", 30, day=9, mentioned=lambda i: i < 12)
    b.write()
    return b


def quiet_project(root: Path) -> Builder:
    """两窗提及率相同（各 25/30），没有任何告警。"""
    b = Builder(root)
    add(b, "W1", 30, day=8, mentioned=lambda i: i < 25)
    add(b, "W2", 30, day=9, mentioned=lambda i: i < 25)
    b.write()
    return b


# ---------------------------------------------------------------- 输出结构与退出码


def test_output_structure_and_exit_code(tmp_path: Path) -> None:
    drop_project(tmp_path)
    code, data = run_cli_json("alerts", str(tmp_path))
    assert code == 1
    doc = read(tmp_path / "out" / "alerts.json")
    assert data["output"] == str(tmp_path / "out" / "alerts.json")
    assert {key: value for key, value in data.items() if key != "output"} == doc
    assert doc["schema_version"] == "1.0"
    assert doc["kind"] == "alerts"
    assert doc["project_id"] == "auto-demo"
    assert doc["synthetic"] is True
    assert doc["target_entity_id"] == TARGET
    assert doc["mode"] == "window"
    assert (doc["baseline_date"], doc["current_date"]) == (None, None)
    assert doc["thresholds"] == DEFAULT_THRESHOLDS
    assert doc["threshold_overrides"] == []
    assert doc["status_label"] == "有告警"
    assert doc["baseline"]["valid_observations"] == 30
    assert doc["baseline"]["first_observed_at"] == "2026-10-08T02:00:00Z"
    assert doc["baseline"]["last_observed_at"] == "2026-10-08T02:29:00Z"
    assert doc["prompt_set"]["version"] == 1
    for alert in doc["alerts"]:
        assert set(alert) >= {
            "alert_id",
            "rule_id",
            "severity",
            "platform",
            "entity_id",
            "message",
            "evidence",
            "observation_ids",
        }
    assert [item["rule_id"] for item in doc["rule_summary"]] == [
        "mention_drop",
        "negative_spread",
        "citation_failure",
        "competitor_surge",
        "platform_conflict",
    ]
    assert set(doc["citation_changes"]) >= {"added", "removed", "changed", "note"}


def test_clear_exit_zero_and_text(tmp_path: Path) -> None:
    quiet_project(tmp_path)
    code, out, _ = alerts_text(tmp_path)
    assert code == 0
    assert "未发现告警" in out
    assert "W1" in out
    assert "W2" in out
    doc = read(tmp_path / "out" / "alerts.json")
    assert doc["status"] == "clear"
    assert doc["status_label"] == "无告警"


def test_text_output_lists_alerts(tmp_path: Path) -> None:
    drop_project(tmp_path)
    code, out, _ = alerts_text(tmp_path)
    assert code == 1
    assert "mention_drop" in out
    assert "critical" in out
    assert "alerts.json" in out


def test_custom_out_path(tmp_path: Path) -> None:
    root = tmp_path / "project"
    drop_project(root)
    target = tmp_path / "elsewhere" / "a.json"
    code, _data = run_cli_json("alerts", str(root), "--out", str(target))
    assert code == 1
    assert read(target)["status"] == "alert"
    assert not (root / "out" / "alerts.json").exists()


def test_byte_reproducible_across_runs_and_directories(tmp_path: Path) -> None:
    first = tmp_path / "first"
    b = drop_project(first)
    add(
        b,
        "W2",
        3,
        day=9,
        citations=lambda i: [make_citation(1, "ev-forum.example")],
        started=lambda i: f"2026-10-09T08:0{i}:00Z",
    )
    b.write()
    run_alerts(first)
    content = (first / "out" / "alerts.json").read_bytes()
    run_alerts(first)
    assert (first / "out" / "alerts.json").read_bytes() == content
    second = copy_project(first, tmp_path / "second")
    run_alerts(second)
    assert (second / "out" / "alerts.json").read_bytes() == content
    assert str(tmp_path) not in content.decode("utf-8")


# ---------------------------------------------------------------- 窗口选择


def three_windows(root: Path) -> Builder:
    """窗口编号的字母序与时间顺序不同：B 最早（10 月 8 日），C 其次（9 日），A 最晚（10 日）。"""
    b = Builder(root)
    for window, day in (("A", 10), ("B", 8), ("C", 9)):
        add(b, window, 20, day=day)
    b.write()
    return b


def test_default_compares_latest_two_windows_by_time(tmp_path: Path) -> None:
    three_windows(tmp_path)
    _, doc = run_alerts(tmp_path)
    assert (doc["baseline_window"], doc["current_window"]) == ("C", "A")
    assert doc["available_windows"] == ["B", "C", "A"]


def test_explicit_windows(tmp_path: Path) -> None:
    three_windows(tmp_path)
    _, doc = run_alerts(tmp_path, "--baseline-window", "B", "--current-window", "A")
    assert (doc["baseline_window"], doc["current_window"]) == ("B", "A")
    _, doc = run_alerts(tmp_path, "--current-window", "C")
    assert (doc["baseline_window"], doc["current_window"]) == ("B", "C")
    _, doc = run_alerts(tmp_path, "--baseline-window", "B")
    assert (doc["baseline_window"], doc["current_window"]) == ("B", "C")


def test_window_errors(tmp_path: Path) -> None:
    three_windows(tmp_path)
    code, data = run_cli_json("alerts", str(tmp_path), "--current-window", "W9")
    assert code == 2
    assert data["error"] == "input_error"
    code, _ = run_cli_json(
        "alerts", str(tmp_path), "--baseline-window", "A", "--current-window", "A"
    )
    assert code == 2
    # 最晚的窗口之后没有窗口；最早的窗口之前没有窗口 → 无法比较
    code, data = run_cli_json("alerts", str(tmp_path), "--baseline-window", "A")
    assert code == 3
    assert data["error"] == "insufficient_data"
    code, _ = run_cli_json("alerts", str(tmp_path), "--current-window", "B")
    assert code == 3


def test_single_window_cannot_compare(tmp_path: Path) -> None:
    b = Builder(tmp_path)
    add(b, "W1", 30, day=8)
    # 另一个窗口只有无效观测：不算可比较的窗口
    b.obs("Q001", "engine-a", "W2", status="invalid", invalid_reason="timeout")
    b.write()
    code, data = run_cli_json("alerts", str(tmp_path))
    assert code == 3
    assert data["error"] == "insufficient_data"
    assert data["details"]["available_windows"] == ["W1"]
    assert not (tmp_path / "out" / "alerts.json").exists()
    code, _, err = run_cli("alerts", str(tmp_path))
    assert code == 3
    assert "无法比较" in err
    code, _ = run_cli_json("alerts", str(tmp_path), "--current-window", "W2")
    assert code == 3


def test_no_observations_cannot_compare(tmp_path: Path) -> None:
    Builder(tmp_path).write()
    code, data = run_cli_json("alerts", str(tmp_path))
    assert code == 3
    assert data["details"]["available_windows"] == []


def test_observation_times_compare_as_instants(tmp_path: Path) -> None:
    # “02:00:00.5Z”按字符串比较排在“02:00:00Z”之前，按时间比较则在其后；
    # “02:00:00.000Z”与“02:00:00Z”是同一时刻，按字符串排序决定先后，结果确定。
    b = Builder(tmp_path)
    times = ["2026-10-08T02:00:00.5Z", "2026-10-08T02:00:00Z", "2026-10-08T02:00:00.000Z"]
    add(b, "W1", 3, started=lambda i: times[i])
    add(b, "W2", 3, day=9)
    b.write()
    _, doc = run_alerts(tmp_path)
    assert doc["baseline"]["first_observed_at"] == "2026-10-08T02:00:00.000Z"
    assert doc["baseline"]["last_observed_at"] == "2026-10-08T02:00:00.5Z"


def test_platform_filter(tmp_path: Path) -> None:
    # engine-a 提及率骤降；engine-b 不变。只看 engine-b → 无告警；只看 engine-a → 有告警。
    b = Builder(tmp_path)
    add(b, "W1", 30, day=8, mentioned=lambda i: i < 24)
    add(b, "W2", 30, day=9, mentioned=lambda i: i < 9)
    add(b, "W1", 30, day=8, platform="engine-b", mentioned=lambda i: i < 24)
    add(b, "W2", 30, day=9, platform="engine-b", mentioned=lambda i: i < 24)
    b.write()
    code, doc = run_alerts(tmp_path, "--platform", "engine-b")
    assert code == 0
    assert doc["platform"] == "engine-b"
    assert doc["baseline"]["valid_observations"] == 30
    code, doc = run_alerts(tmp_path, "--platform", "engine-a")
    assert code == 1
    assert {item["platform"] for item in doc["alerts"]} == {"engine-a"}
    code, _data = run_cli_json("alerts", str(tmp_path), "--platform", "engine-z")
    assert code == 2


# ---------------------------------------------------------------- 按自然日（昨日与今日）


def daily_project(root: Path) -> Builder:
    """项目时区 Asia/Shanghai（UTC+8），所有观测都在同一个窗口 W1。

    本地 10 月 9 日（UTC 10-08T16:00 ～ 10-09T15:59:59）：25 条，全部提及；
    本地 10 月 10 日（UTC 10-09T16:00 起）：25 条，前 5 条提及；
    本地 10 月 8 日（UTC 10-08T15:30）：3 条，不属于比较的两天。
    按 UTC 日期分组会得到不同的计数，用来检出时区处理错误。
    """
    b = Builder(root)
    yesterday = [
        "2026-10-08T16:00:00Z",
        "2026-10-09T15:59:59Z",
        *[f"2026-10-09T0{h}:{m:02d}:00Z" for h in range(3) for m in range(0, 50, 7)][:23],
    ]
    today = ["2026-10-09T16:00:00Z"] + [f"2026-10-10T01:{m:02d}:00Z" for m in range(24)]
    assert len(yesterday) == 25
    assert len(today) == 25
    add(b, "W1", 3, started=lambda i: f"2026-10-08T15:3{i}:00Z")
    add(b, "W1", 25, started=lambda i: yesterday[i])
    add(b, "W1", 25, started=lambda i: today[i], mentioned=lambda i: i < 5)
    b.write()
    return b


def test_daily_comparison_uses_project_timezone(tmp_path: Path) -> None:
    daily_project(tmp_path)
    code, _ = run_cli_json("alerts", str(tmp_path))
    assert code == 3  # 只有一个窗口
    code, doc = run_alerts(tmp_path, "--current-date", "2026-10-10")
    assert code == 1
    assert doc["mode"] == "date"
    assert (doc["baseline_date"], doc["current_date"]) == ("2026-10-09", "2026-10-10")
    assert (doc["baseline_window"], doc["current_window"]) == (None, None)
    assert doc["baseline"]["valid_observations"] == 25
    assert doc["current"]["valid_observations"] == 25
    assert doc["baseline"]["first_observed_at"] == "2026-10-08T16:00:00Z"
    assert doc["baseline"]["last_observed_at"] == "2026-10-09T15:59:59Z"
    (alert,) = select(doc, "mention_drop", metric="mention_rate")
    assert alert["evidence"]["baseline"] == {"numerator": 25, "denominator": 25, "value": 1.0}
    assert alert["evidence"]["current"] == {"numerator": 5, "denominator": 25, "value": 0.2}
    assert "2026-10-09" in alert["message"]
    assert "2026-10-10" in alert["message"]
    _, same = run_alerts(tmp_path, "--baseline-date", "2026-10-09", "--current-date", "2026-10-10")
    assert same == doc
    _, forward = run_alerts(tmp_path, "--baseline-date", "2026-10-09")
    assert forward["current_date"] == "2026-10-10"


def test_daily_errors(tmp_path: Path) -> None:
    daily_project(tmp_path)
    code, _ = run_cli_json("alerts", str(tmp_path), "--current-date", "2026/10/10")
    assert code == 2
    code, _ = run_cli_json("alerts", str(tmp_path), "--current-date", "2026-02-30")
    assert code == 2
    code, _ = run_cli_json(
        "alerts", str(tmp_path), "--current-date", "2026-10-10", "--current-window", "W1"
    )
    assert code == 2
    code, _ = run_cli_json(
        "alerts", str(tmp_path), "--baseline-date", "2026-10-10", "--current-date", "2026-10-10"
    )
    assert code == 2
    # 10 月 12 日没有观测，前一天也没有 → 无法比较
    code, data = run_cli_json("alerts", str(tmp_path), "--current-date", "2026-10-12")
    assert code == 3
    assert data["error"] == "insufficient_data"


# ---------------------------------------------------------------- 阈值覆盖


def test_threshold_override_from_cli(tmp_path: Path) -> None:
    drop_project(tmp_path)
    code, doc = run_alerts(tmp_path, "--threshold", "mention_drop.min_abs_drop=0.5")
    assert code == 0  # 降 0.4 < 0.5
    assert doc["thresholds"]["mention_drop"]["min_abs_drop"] == 0.5
    assert doc["threshold_overrides"] == [
        {"key": "mention_drop.min_abs_drop", "value": 0.5, "source": "cli"}
    ]


def test_threshold_override_from_project_ext_and_precedence(tmp_path: Path) -> None:
    ext = {"alerts": {"thresholds": {"mention_drop": {"min_observations": 40}}}}
    drop_project(tmp_path, ext=ext)
    code, doc = run_alerts(tmp_path)
    assert code == 0  # 每窗 30 条 < 40：样本不足，不评估
    assert doc["thresholds"]["mention_drop"]["min_observations"] == 40
    assert doc["threshold_overrides"] == [
        {"key": "mention_drop.min_observations", "value": 40, "source": "project"}
    ]
    code, doc = run_alerts(tmp_path, "--threshold", "mention_drop.min_observations=30")
    assert code == 1  # 命令行优先于 project.json
    assert doc["threshold_overrides"] == [
        {"key": "mention_drop.min_observations", "value": 30, "source": "cli"}
    ]


@pytest.mark.parametrize(
    "item",
    [
        "mention_drop.unknown=1",
        "nothing.alpha=0.1",
        "mention_drop.alpha",
        "mention_drop.alpha=abc",
        "mention_drop.alpha=0",
        "mention_drop.alpha=1.5",
        "mention_drop.min_abs_drop=-0.1",
        "mention_drop.min_observations=2.5",
        "mention_drop.min_observations=0",
        "mention_drop.min_mean_rank_increase=0",
        "mention_drop.alpha=true",
        "mention_drop.alpha=nan",
    ],
)
def test_invalid_threshold_override(tmp_path: Path, item: str) -> None:
    drop_project(tmp_path)
    code, data = run_cli_json("alerts", str(tmp_path), "--threshold", item)
    assert code == 2, data
    assert data["error"] == "input_error"


@pytest.mark.parametrize(
    "ext",
    [
        {"alerts": []},
        {"alerts": {"thresholds": []}},
        {"alerts": {"thresholds": {"mention_drop": 0.1}}},
        {"alerts": {"thresholds": {"mention_drop": {"alpha": "0.1"}}}},
        {"alerts": {"thresholds": {"bogus": {"alpha": 0.1}}}},
    ],
)
def test_invalid_project_thresholds(tmp_path: Path, ext: dict[str, Any]) -> None:
    drop_project(tmp_path, ext=ext)
    code, data = run_cli_json("alerts", str(tmp_path))
    assert code == 2, data


def test_integer_threshold_given_as_float_in_project(tmp_path: Path) -> None:
    drop_project(tmp_path, ext={"alerts": {"thresholds": {"platform_conflict": {"min_each": 4.0}}}})
    _, doc = run_alerts(tmp_path)
    assert doc["thresholds"]["platform_conflict"]["min_each"] == 4
    assert isinstance(doc["thresholds"]["platform_conflict"]["min_each"], int)


# ---------------------------------------------------------------- 引用域名变化与线索


def test_s17_rank_drop_with_citation_domain_change(tmp_path: Path) -> None:
    """S17：品牌掉位且引用域名变化 → 告警触发，线索列出变化的域名，措辞为“相关线索”。

    单平台 engine-a，两窗各 30 条，目标主体都被提及且可排序：
      W1：27 条位次 1、3 条位次 4 → 前三 27/30 = 0.9；每条引用 lanxu-auto.example 与 auto-review.example；
      W2：9 条位次 2、21 条位次 5 → 前三 9/30 = 0.3；每条引用 ev-forum.example，前 10 条另引 auto-review.example。
    引用计数：lanxu-auto 30 → 0（消失），ev-forum 0 → 30（新增），auto-review 30 → 10（变化 −20）。
    """
    b = Builder(tmp_path)
    add(
        b,
        "W1",
        30,
        day=8,
        ann=lambda i: {"rankable": True, "rank": 1 if i < 27 else 4},
        citations=lambda i: [
            make_citation(1, "lanxu-auto.example"),
            make_citation(2, "auto-review.example"),
        ],
    )
    add(
        b,
        "W2",
        30,
        day=9,
        ann=lambda i: {"rankable": True, "rank": 2 if i < 9 else 5},
        citations=lambda i: (
            [make_citation(1, "ev-forum.example")]
            + ([make_citation(2, "auto-review.example")] if i < 10 else [])
        ),
    )
    b.write()
    code, doc = run_alerts(tmp_path)
    assert code == 1
    assert select(doc, "mention_drop", metric="mention_rate") == []
    (top3,) = select(doc, "mention_drop", metric="top3_rate")
    assert top3["evidence"]["baseline"]["value"] == 0.9
    assert top3["evidence"]["current"]["value"] == pytest.approx(0.3)
    changes = doc["citation_changes"]
    assert changes["added"] == [{"domain": "ev-forum.example", "current": 30}]
    assert changes["removed"] == [{"domain": "lanxu-auto.example", "baseline": 30}]
    assert changes["changed"] == [
        {"domain": "auto-review.example", "baseline": 30, "current": 10, "delta": -20}
    ]
    assert (changes["baseline_citations"], changes["current_citations"]) == (60, 40)
    assert "相关" in changes["note"]
    assert "待核查" in changes["note"]
    leads = {lead["alert_id"]: lead for lead in doc["leads"]}
    lead = leads[top3["alert_id"]]
    assert lead["alert_rule_id"] == "mention_drop"
    assert lead["platform"] == "engine-a"
    # 按变化次数从大到小，再按域名排序
    assert lead["domains"] == ["ev-forum.example", "lanxu-auto.example", "auto-review.example"]
    assert "相关线索" in lead["text"]
    assert "待核查" in lead["text"]
    for domain in lead["domains"]:
        assert domain in lead["text"]
    assert banned_in(all_strings(doc)) == []


def test_citation_counts_deduplicate_urls_within_observation(tmp_path: Path) -> None:
    # W1：每条观测把同一 URL 引用两次（只算 1 次）→ a-media.example 30 次；
    # W2：每条观测引用 a-media.example 的两个不同 URL → 60 次。
    b = Builder(tmp_path)
    same = {
        "order": 2,
        "url": "https://a-media.example/page-1",
        "domain": "a-media.example",
        "title": None,
        "placement": "source_list",
    }
    add(b, "W1", 30, day=8, citations=lambda i: [make_citation(1, "a-media.example"), same])
    add(
        b,
        "W2",
        30,
        day=9,
        citations=lambda i: [
            make_citation(1, "a-media.example"),
            make_citation(2, "a-media.example"),
        ],
    )
    b.write()
    _, doc = run_alerts(tmp_path)
    assert doc["citation_changes"]["changed"] == [
        {"domain": "a-media.example", "baseline": 30, "current": 60, "delta": 30}
    ]


def test_no_citation_data(tmp_path: Path) -> None:
    # 没有任何引用：citation_changes 为空，照常输出（告警仍然给出，线索为空）。
    drop_project(tmp_path)
    code, doc = run_alerts(tmp_path)
    assert code == 1
    changes = doc["citation_changes"]
    assert (changes["added"], changes["removed"], changes["changed"]) == ([], [], [])
    assert (changes["baseline_citations"], changes["current_citations"]) == (0, 0)
    assert changes["by_platform"] == []
    assert doc["leads"] == []
    assert changes["note"]


def test_leads_follow_alert_platform_and_cap(tmp_path: Path) -> None:
    # engine-a 提及率骤降；engine-a 当前窗口新增 12 个域名（各 1 次），engine-b 新增 b-only.example。
    # engine-a 告警的线索只列 engine-a 的变化，最多 10 个（另有 2 个未列出）。
    b = Builder(tmp_path)
    add(b, "W1", 30, day=8, mentioned=lambda i: i < 24)
    add(
        b,
        "W2",
        30,
        day=9,
        mentioned=lambda i: i < 9,
        citations=lambda i: [make_citation(1, f"m{i:02d}.example")] if i < 12 else None,
    )
    add(b, "W1", 30, day=8, platform="engine-b")
    add(
        b,
        "W2",
        30,
        day=9,
        platform="engine-b",
        citations=lambda i: [make_citation(1, "b-only.example")] if i == 0 else None,
    )
    b.write()
    _, doc = run_alerts(tmp_path, "--threshold", "leads.max_domains=10")
    (alert,) = select(doc, "mention_drop", platform="engine-a", metric="mention_rate")
    lead = next(item for item in doc["leads"] if item["alert_id"] == alert["alert_id"])
    assert lead["domains"] == [f"m{i:02d}.example" for i in range(10)]
    assert "b-only.example" not in lead["text"]
    assert "另有 2 个" in lead["text"]
    by_platform = {item["platform"]: item for item in doc["citation_changes"]["by_platform"]}
    assert by_platform["engine-b"]["added"] == [{"domain": "b-only.example", "current": 1}]
    assert len(by_platform["engine-a"]["added"]) == 12
    (overall,) = select(doc, "mention_drop", platform=None, metric="mention_rate")
    overall_lead = next(item for item in doc["leads"] if item["alert_id"] == overall["alert_id"])
    assert "b-only.example" in overall_lead["domains"]


# ---------------------------------------------------------------- 措辞红线


def every_alert_project(root: Path) -> Builder:
    """同时触发五类规则的项目（三个平台、两个窗口），并带引用域名变化。"""
    b = Builder(root)
    competitor = "brand-cenye"

    def after(window: str, platform: str) -> Any:
        def write(observation: dict[str, Any], i: int) -> None:
            surge = window == "W2" and platform == "engine-a"
            b.annotate(
                observation,
                entity_id=competitor,
                mentioned=(i < 20) if surge else (i < 4),
                recommended=False,
                sentiment=None,
            )
            if window == "W2" and platform == "engine-a" and i < 8:
                b.judge(
                    observation,
                    "present_correct" if i < 4 else "present_wrong",
                    fact_id="F-S7-PRICE",
                )

        return write

    for platform in ("engine-a", "engine-b", "engine-c"):
        for window, day in (("W1", 8), ("W2", 9)):
            drop = window == "W2" and platform == "engine-a"
            neg = window == "W2" and platform in ("engine-b", "engine-c")
            add(
                b,
                window,
                30,
                day=day,
                platform=platform,
                mentioned=lambda i, d=drop: i < (8 if d else 27),
                ann=lambda i, n=neg, w=window: {
                    **({"risk": {"unfounded_negative": True}} if n and i < 6 else {}),
                    "citation_checks": [
                        {"order": 1, "valid": not (w == "W2" and i < 12), "relevant": True}
                    ],
                },
                citations=lambda i, w=window: [
                    make_citation(1, "lanxu-auto.example" if w == "W1" else "ev-forum.example")
                ],
                after=after(window, platform),
            )
    b.write()
    return b


def test_wording_red_lines_across_inputs(tmp_path: Path) -> None:
    texts: list[str] = []
    every = every_alert_project(tmp_path / "every")
    del every
    code, doc = run_alerts(tmp_path / "every")
    assert code == 1
    assert {item["rule_id"] for item in doc["alerts"]} == {
        "mention_drop",
        "negative_spread",
        "citation_failure",
        "competitor_surge",
        "platform_conflict",
    }
    assert doc["leads"]
    texts += all_strings(doc)
    texts.append(alerts_text(tmp_path / "every")[1])
    texts.append(alerts_text(tmp_path / "every", "--platform", "engine-a")[1])
    quiet_project(tmp_path / "quiet")
    texts.append(alerts_text(tmp_path / "quiet")[1])
    texts += all_strings(run_alerts(tmp_path / "quiet")[1])
    daily_project(tmp_path / "daily")
    texts.append(alerts_text(tmp_path / "daily", "--current-date", "2026-10-10")[1])
    texts += all_strings(run_alerts(tmp_path / "daily", "--current-date", "2026-10-10")[1])
    # 无法比较、输入错误时的提示也在检查范围内
    for argv in (
        ("--current-date", "2026-10-12"),
        (),
        ("--current-window", "W9"),
        ("--threshold", "x.y=1"),
    ):
        code, out, err = alerts_text(tmp_path / "daily", *argv)
        assert code in (2, 3)
        texts += [out, err]
        code, data = run_cli_json("alerts", str(tmp_path / "daily"), *argv)
        texts += all_strings(data)
    assert texts
    assert banned_in(texts) == []


def test_every_alert_message_names_scope(tmp_path: Path) -> None:
    every_alert_project(tmp_path)
    _, doc = run_alerts(tmp_path)
    for alert in doc["alerts"]:
        assert alert["message"]
        if alert["platform"] is not None:
            assert alert["platform"] in alert["message"]
        assert alert["observation_ids"] == sorted(set(alert["observation_ids"]))
    alert_ids = [item["alert_id"] for item in doc["alerts"]]
    assert len(alert_ids) == len(set(alert_ids))
    assert {lead["alert_id"] for lead in doc["leads"]} <= set(alert_ids)


def test_prompt_set_version_option(tmp_path: Path) -> None:
    drop_project(tmp_path)
    code, _doc = run_alerts(tmp_path, "--prompt-set-version", "1")
    assert code == 1
    code, _ = run_cli_json("alerts", str(tmp_path), "--prompt-set-version", "2")
    assert code == 2


def test_json_flag_prints_machine_readable_document(tmp_path: Path) -> None:
    quiet_project(tmp_path)
    code, out, _ = run_cli("alerts", str(tmp_path), "--json")
    assert code == 0
    data = json.loads(out)
    assert data["kind"] == "alerts"
    assert data["status"] == "clear"
    # 结果文件可以删掉重算，重算结果与 --json 输出一致
    shutil.rmtree(tmp_path / "out")
    _, doc = run_alerts(tmp_path)
    assert doc == {key: value for key, value in data.items() if key != "output"}
