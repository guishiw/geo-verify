"""告警规则测试：五类规则各有触发例与不触发例（含“小样本大波动不触发”）。

期望值全部由注释里的手工计数推得；p 值用 helpers_alerts 里独立写的公式（erfc）计算。
默认阈值见 test_alerts_cli.DEFAULT_THRESHOLDS。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from factory import COMPETITOR, TARGET, make_citation
from helpers_alerts import add, ids, run_alerts, select, two_proportion_p, welch_p
from helpers_metrics import Builder

from geo_verify.metrics import Scope, build_index, compute

# ---------------------------------------------------------------- mention_drop：提及率


def build_mention_drop(root: Path, *, base: tuple[int, int], cur: tuple[int, int]) -> Builder:
    """单平台 engine-a：W1（10 月 8 日）base=(提及数, 观测数)，W2（10 月 9 日）cur 同理。"""
    b = Builder(root)
    add(b, "W1", base[1], day=8, mentioned=lambda i: i < base[0])
    add(b, "W2", cur[1], day=9, mentioned=lambda i: i < cur[0])
    b.write()
    return b


def test_mention_drop_triggers_with_hand_computed_evidence(tmp_path: Path) -> None:
    # W1 24/30 = 0.8，W2 12/30 = 0.4：降 0.4 ≥ 0.15，相对降 50% ≥ 20%，两窗各 30 ≥ 20，
    # p = 单侧两比例 z 检验 ≈ 0.0008 ≤ 0.05 → 触发；降幅 0.4 ≥ 0.3 → critical。
    b = build_mention_drop(tmp_path, base=(24, 30), cur=(12, 30))
    code, doc = run_alerts(tmp_path)
    assert code == 1
    assert doc["status"] == "alert"
    assert doc["baseline_window"] == "W1"
    assert doc["current_window"] == "W2"
    # 只有一个平台：只按平台评估，不重复输出“全部平台合并”
    alerts = select(doc, "mention_drop", metric="mention_rate")
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["platform"] == "engine-a"
    assert alert["entity_id"] == TARGET
    assert alert["severity"] == "critical"
    evidence = alert["evidence"]
    assert evidence["baseline"] == {"numerator": 24, "denominator": 30, "value": 0.8}
    assert evidence["current"] == {"numerator": 12, "denominator": 30, "value": 0.4}
    assert evidence["difference"] == pytest.approx(-0.4)
    assert evidence["relative_change"] == pytest.approx(-0.5)
    assert evidence["p_value"] == pytest.approx(
        two_proportion_p(24, 30, 12, 30, increase=False), abs=1e-6
    )
    # 下钻：当前窗口中未提及目标主体的有效观测（第 13～30 条）
    w2 = [item for item in b.observations if item["window_id"] == "W2"]
    assert alert["observation_ids"] == ids(w2[12:])
    assert [item["rule_id"] for item in doc["alerts"]] == ["mention_drop"]


def test_mention_drop_small_sample_large_swing_does_not_trigger(tmp_path: Path) -> None:
    # W1 5/5 = 1.0，W2 1/5 = 0.2：降 0.8，但两窗各只有 5 条 < 20 → 不触发，记为样本不足。
    build_mention_drop(tmp_path, base=(5, 5), cur=(1, 5))
    code, doc = run_alerts(tmp_path)
    assert code == 0
    assert doc["status"] == "clear"
    assert doc["alerts"] == []
    summary = {item["rule_id"]: item for item in doc["rule_summary"]}
    skipped = summary["mention_drop"]["skipped"]
    assert {"platform": "engine-a", "entity_id": TARGET, "metric": "mention_rate"} in [
        {key: item[key] for key in ("platform", "entity_id", "metric")} for item in skipped
    ]
    assert summary["mention_drop"]["triggered"] == 0


def test_mention_drop_small_drop_on_large_sample_does_not_trigger(tmp_path: Path) -> None:
    # W1 80/100 = 0.8，W2 70/100 = 0.7：降 0.1 < 0.15 → 不触发。
    build_mention_drop(tmp_path, base=(80, 100), cur=(70, 100))
    code, doc = run_alerts(tmp_path)
    assert code == 0
    assert select(doc, "mention_drop") == []


def test_mention_drop_requires_significance(tmp_path: Path) -> None:
    # W1 10/20 = 0.5，W2 6/20 = 0.3：降 0.2、相对降 40%，但 p ≈ 0.098 > 0.05 → 不触发；
    # 把 alpha 放宽到 0.1 后触发（降 0.2 < 0.3 → warning）。
    build_mention_drop(tmp_path, base=(10, 20), cur=(6, 20))
    assert two_proportion_p(10, 20, 6, 20, increase=False) == pytest.approx(0.0984, abs=1e-3)
    code, doc = run_alerts(tmp_path)
    assert code == 0
    code, doc = run_alerts(tmp_path, "--threshold", "mention_drop.alpha=0.1")
    assert code == 1
    (alert,) = select(doc, "mention_drop", metric="mention_rate")
    assert alert["severity"] == "warning"


def test_mention_drop_requires_relative_drop(tmp_path: Path) -> None:
    # W1 95/100，W2 78/100：降 0.17 ≥ 0.15，p ≈ 0.0002，但相对降 0.17/0.95 ≈ 17.9% < 20% → 不触发。
    build_mention_drop(tmp_path, base=(95, 100), cur=(78, 100))
    code, doc = run_alerts(tmp_path)
    assert code == 0
    code, doc = run_alerts(tmp_path, "--threshold", "mention_drop.min_rel_drop=0.15")
    assert code == 1
    assert len(select(doc, "mention_drop", metric="mention_rate")) == 1


def test_mention_drop_counts_only_valid_observations(tmp_path: Path) -> None:
    # 当前窗口另有 10 条无效观测（超时）和 5 条被审计剔除的提及观测：都不进分子分母，
    # 当前仍是 12/30。
    b = Builder(tmp_path)
    add(b, "W1", 30, day=8, mentioned=lambda i: i < 24)
    add(b, "W2", 30, day=9, mentioned=lambda i: i < 12)
    for i in range(10):
        b.obs(
            "Q001",
            "engine-a",
            "W2",
            status="invalid",
            invalid_reason="timeout",
            started_at=f"2026-10-09T05:{i:02d}:00Z",
        )
    excluded = add(
        b, "W2", 5, day=9, mentioned=lambda _i: True, started=lambda i: f"2026-10-09T06:{i:02d}:00Z"
    )
    b.write()
    b.exclude(excluded)
    _, doc = run_alerts(tmp_path)
    (alert,) = select(doc, "mention_drop", metric="mention_rate")
    assert alert["evidence"]["current"] == {"numerator": 12, "denominator": 30, "value": 0.4}
    assert doc["current"]["valid_observations"] == 30


def test_mention_counts_match_metrics_engine(tmp_path: Path) -> None:
    # 告警与指标模块对同一范围取到的分子、分母一致（共用索引与口径）。
    build_mention_drop(tmp_path, base=(24, 30), cur=(12, 30))
    _, doc = run_alerts(tmp_path)
    (alert,) = select(doc, "mention_drop", metric="mention_rate")
    index = build_index(tmp_path)
    for side, window in (("baseline", "W1"), ("current", "W2")):
        result = compute(index, "mention_rate", Scope(platform="engine-a", window_id=window)).result
        assert alert["evidence"][side]["numerator"] == result["numerator"]
        assert alert["evidence"][side]["denominator"] == result["denominator"]


def test_mention_drop_overall_and_per_platform(tmp_path: Path) -> None:
    # engine-a：W1 24/30 → W2 9/30（降 0.5，critical）；engine-b：W1 24/30 → W2 24/30（不变）。
    # 合并：W1 48/60 = 0.8 → W2 33/60 = 0.55，降 0.25、相对降 31%，p ≈ 0.0017 → 触发（warning）。
    b = Builder(tmp_path)
    add(b, "W1", 30, day=8, mentioned=lambda i: i < 24)
    add(b, "W1", 30, day=8, platform="engine-b", mentioned=lambda i: i < 24)
    add(b, "W2", 30, day=9, mentioned=lambda i: i < 9)
    add(b, "W2", 30, day=9, platform="engine-b", mentioned=lambda i: i < 24)
    b.write()
    code, doc = run_alerts(tmp_path)
    assert code == 1
    (overall,) = select(doc, "mention_drop", platform=None, metric="mention_rate")
    assert overall["evidence"]["baseline"]["numerator"] == 48
    assert overall["evidence"]["current"] == {"numerator": 33, "denominator": 60, "value": 0.55}
    assert overall["severity"] == "warning"
    assert overall["evidence"]["p_value"] == pytest.approx(
        two_proportion_p(48, 60, 33, 60, increase=False), abs=1e-6
    )
    (platform_a,) = select(doc, "mention_drop", platform="engine-a", metric="mention_rate")
    assert platform_a["severity"] == "critical"
    assert select(doc, "mention_drop", platform="engine-b") == []


# ---------------------------------------------------------------- mention_drop：掉位


def build_rank_drop(root: Path, *, n: int, base: list[int], cur: list[int]) -> Builder:
    """单平台；每条观测都提及目标且可排序，位次分别取 base[i]、cur[i]。"""
    b = Builder(root)
    add(b, "W1", n, day=8, ann=lambda i: {"rankable": True, "rank": base[i]})
    add(b, "W2", n, day=9, ann=lambda i: {"rankable": True, "rank": cur[i]})
    b.write()
    return b


def test_rank_drop_triggers_top3_and_mean_rank(tmp_path: Path) -> None:
    # W1：18 条位次 2、2 条位次 5 → 前三 18/20 = 0.9，平均位次 2.3；
    # W2：8 条位次 2、12 条位次 6 → 前三 8/20 = 0.4，平均位次 4.4。提及率都是 1.0，不触发提及告警。
    # 前三降 0.5 ≥ 0.2、相对降 56% → 触发（critical）；平均位次后移 2.1 ≥ 1（≥ 2 → critical）。
    base = [2] * 18 + [5] * 2
    cur = [2] * 8 + [6] * 12
    b = build_rank_drop(tmp_path, n=20, base=base, cur=cur)
    code, doc = run_alerts(tmp_path)
    assert code == 1
    assert select(doc, "mention_drop", metric="mention_rate") == []
    (top3,) = select(doc, "mention_drop", metric="top3_rate")
    assert top3["evidence"]["baseline"] == {"numerator": 18, "denominator": 20, "value": 0.9}
    assert top3["evidence"]["current"] == {"numerator": 8, "denominator": 20, "value": 0.4}
    assert top3["evidence"]["p_value"] == pytest.approx(
        two_proportion_p(18, 20, 8, 20, increase=False), abs=1e-6
    )
    assert top3["severity"] == "critical"
    w2 = [item for item in b.observations if item["window_id"] == "W2"]
    assert top3["observation_ids"] == ids(w2[8:])
    (mean_rank,) = select(doc, "mention_drop", metric="mean_rank")
    evidence = mean_rank["evidence"]
    assert evidence["baseline"] == {"mean": pytest.approx(2.3), "count": 20}
    assert evidence["current"] == {"mean": pytest.approx(4.4), "count": 20}
    assert evidence["difference"] == pytest.approx(2.1)
    assert evidence["p_value"] == pytest.approx(welch_p(base, cur), abs=1e-6)
    assert mean_rank["severity"] == "critical"
    assert mean_rank["observation_ids"] == ids(w2)


def test_rank_drop_small_sample_does_not_trigger(tmp_path: Path) -> None:
    # 两窗各 5 条可排序观测（< 10）：位次从 1 全部变成 6，也不触发。
    build_rank_drop(tmp_path, n=5, base=[1] * 5, cur=[6] * 5)
    code, doc = run_alerts(tmp_path)
    assert code == 0
    assert doc["alerts"] == []


def test_rank_shift_below_threshold_does_not_trigger(tmp_path: Path) -> None:
    # 前三都是 20/20；平均位次 1.0 → 1.5（后移 0.5 < 1）→ 不触发。
    build_rank_drop(tmp_path, n=20, base=[1] * 20, cur=[1] * 10 + [2] * 10)
    code, doc = run_alerts(tmp_path)
    assert code == 0
    assert doc["alerts"] == []


# ---------------------------------------------------------------- negative_spread


def negative(flag: bool) -> dict[str, Any]:
    return {"risk": {"unfounded_negative": True}, "sentiment": "negative"} if flag else {}


def test_negative_rate_rise_triggers(tmp_path: Path) -> None:
    # 单平台：W1 0/30，W2 6/30 含无依据负面：升 0.2 ≥ 0.05；基线为 0，相对升幅视为满足；
    # 当前 6 条 ≥ 3；p ≈ 0.005 → 触发；升 0.2 ≥ 0.15 → critical。只有一个平台，不评估平台数。
    b = Builder(tmp_path)
    add(b, "W1", 30, day=8)
    current = add(b, "W2", 30, day=9, ann=lambda i: negative(i < 6))
    b.write()
    code, doc = run_alerts(tmp_path)
    assert code == 1
    (alert,) = select(doc, "negative_spread")
    assert alert["platform"] == "engine-a"
    assert alert["entity_id"] == TARGET
    assert alert["severity"] == "critical"
    evidence = alert["evidence"]
    assert evidence["metric"] == "negative_miscitation_rate"
    assert evidence["baseline"] == {"numerator": 0, "denominator": 30, "value": 0.0}
    assert evidence["current"] == {"numerator": 6, "denominator": 30, "value": 0.2}
    assert evidence["relative_change"] is None
    assert evidence["p_value"] == pytest.approx(
        two_proportion_p(0, 30, 6, 30, increase=True), abs=1e-6
    )
    assert alert["observation_ids"] == ids(current[:6])


def test_negative_rate_few_cases_do_not_trigger(tmp_path: Path) -> None:
    # W1 0/30，W2 2/30：当前只有 2 条 < 3 → 不触发。
    b = Builder(tmp_path)
    add(b, "W1", 30, day=8)
    add(b, "W2", 30, day=9, ann=lambda i: negative(i < 2))
    b.write()
    code, doc = run_alerts(tmp_path)
    assert code == 0
    assert doc["alerts"] == []


def test_negative_small_sample_does_not_trigger(tmp_path: Path) -> None:
    # W1 0/6，W2 4/6：比例升 0.67，但每窗只有 6 条 < 20 → 不触发。
    b = Builder(tmp_path)
    add(b, "W1", 6, day=8)
    add(b, "W2", 6, day=9, ann=lambda i: negative(i < 4))
    b.write()
    code, doc = run_alerts(tmp_path)
    assert code == 0
    assert doc["alerts"] == []


def build_spread(root: Path, *, current_cases: int) -> Builder:
    """三个平台各 30 条/窗。基线只有 engine-a 有 3 条无依据负面；
    当前 engine-a 没有，engine-b、engine-c 各有 current_cases 条。"""
    b = Builder(root)
    for platform in ("engine-a", "engine-b", "engine-c"):
        base_cases = 3 if platform == "engine-a" else 0
        cur_cases = 0 if platform == "engine-a" else current_cases
        add(b, "W1", 30, day=8, platform=platform, ann=lambda i, k=base_cases: negative(i < k))
        add(b, "W2", 30, day=9, platform=platform, ann=lambda i, k=cur_cases: negative(i < k))
    b.write()
    return b


def test_negative_platform_spread_triggers(tmp_path: Path) -> None:
    # 每个平台至少 3 条才算“出现”：基线 1 个平台（engine-a），当前 2 个（engine-b、engine-c）
    # → 平台数增加 1 ≥ 1 → 触发；增加 < 2 → warning。
    b = build_spread(tmp_path, current_cases=3)
    code, doc = run_alerts(tmp_path)
    assert code == 1
    (alert,) = select(doc, "negative_spread", metric="platform_count")
    assert alert["platform"] is None
    assert alert["severity"] == "warning"
    evidence = alert["evidence"]
    assert evidence["baseline"] == {"count": 1, "platforms": ["engine-a"]}
    assert evidence["current"] == {"count": 2, "platforms": ["engine-b", "engine-c"]}
    assert evidence["new_platforms"] == ["engine-b", "engine-c"]
    expected = [
        item
        for item in b.observations
        if item["window_id"] == "W2"
        and item["platform"] in ("engine-b", "engine-c")
        and int(item["started_at"][14:16]) < 3
    ]
    assert len(expected) == 6
    assert alert["observation_ids"] == ids(expected)


def test_negative_platform_spread_needs_minimum_cases(tmp_path: Path) -> None:
    # 当前 engine-b、engine-c 各只有 2 条 < 3：当前“出现”的平台数为 0 → 不触发平台数告警。
    build_spread(tmp_path, current_cases=2)
    _, doc = run_alerts(tmp_path)
    assert select(doc, "negative_spread", metric="platform_count") == []


# ---------------------------------------------------------------- citation_failure


def checked(valid: bool) -> dict[str, Any]:
    return {"citation_checks": [{"order": 1, "valid": valid, "relevant": True if valid else None}]}


def cite(_i: int) -> list[dict[str, Any]]:
    return [make_citation(1, "auto-review.example")]


def test_citation_failure_share_above_threshold(tmp_path: Path) -> None:
    # 两窗各 24 条已核验引用，各 8 条无效：当前 8/24 ≈ 0.333 ≥ 0.3，无效 8 ≥ 5，已核验 24 ≥ 20
    # → 触发（达到阈值）；较基线没有上升；0.333 < 0.5 → warning。
    b = Builder(tmp_path)
    add(b, "W1", 24, day=8, citations=cite, ann=lambda i: checked(i >= 8))
    current = add(b, "W2", 24, day=9, citations=cite, ann=lambda i: checked(i >= 8))
    b.write()
    code, doc = run_alerts(tmp_path)
    assert code == 1
    (alert,) = select(doc, "citation_failure")
    assert alert["platform"] == "engine-a"
    assert alert["entity_id"] is None
    assert alert["severity"] == "warning"
    evidence = alert["evidence"]
    assert evidence["metric"] == "invalid_citation_share"
    assert evidence["triggers"] == ["share_above_threshold"]
    assert evidence["current"] == {"numerator": 8, "denominator": 24, "value": 8 / 24}
    assert evidence["baseline"] == {"numerator": 8, "denominator": 24, "value": 8 / 24}
    assert alert["observation_ids"] == ids(current[:8])


def test_citation_failure_rise_from_baseline(tmp_path: Path) -> None:
    # W1 1/30 无效，W2 8/30 无效（0.267 < 0.3，未达到占比阈值）；升 0.233 ≥ 0.15、相对升 700%、
    # p ≈ 0.006 → 触发（较基线上升）。另有 10 条当前观测的引用未核验：不进分母。
    b = Builder(tmp_path)
    add(b, "W1", 30, day=8, citations=cite, ann=lambda i: checked(i >= 1))
    add(b, "W2", 30, day=9, citations=cite, ann=lambda i: checked(i >= 8))
    add(b, "W2", 10, day=9, citations=cite, started=lambda i: f"2026-10-09T07:{i:02d}:00Z")
    b.write()
    code, doc = run_alerts(tmp_path)
    assert code == 1
    (alert,) = select(doc, "citation_failure")
    evidence = alert["evidence"]
    assert evidence["triggers"] == ["rise_from_baseline"]
    assert evidence["current"]["denominator"] == 30
    assert evidence["current"]["numerator"] == 8
    assert evidence["p_value"] == pytest.approx(
        two_proportion_p(1, 30, 8, 30, increase=True), abs=1e-6
    )


def test_citation_failure_small_sample_does_not_trigger(tmp_path: Path) -> None:
    # 当前只有 10 条已核验引用（< 20），其中 5 条无效（50%）→ 不触发。
    b = Builder(tmp_path)
    add(b, "W1", 24, day=8, citations=cite, ann=lambda _i: checked(True))
    add(b, "W2", 10, day=9, citations=cite, ann=lambda i: checked(i >= 5))
    b.write()
    code, doc = run_alerts(tmp_path)
    assert code == 0
    assert select(doc, "citation_failure") == []


# ---------------------------------------------------------------- competitor_surge


def build_competitor(root: Path, *, n: int, base: int, cur: int, recommended: bool) -> Builder:
    """单平台；目标主体始终被提及。竞品 brand-cenye：W1 前 base 条、W2 前 cur 条被提及。"""
    b = Builder(root)

    def competitor(limit: int) -> Any:
        def write(observation: dict[str, Any], i: int) -> None:
            flag = i < limit
            b.annotate(
                observation,
                entity_id=COMPETITOR,
                mentioned=flag,
                recommended=flag and recommended,
                sentiment=None,
            )

        return write

    add(b, "W1", n, day=8, after=competitor(base))
    add(b, "W2", n, day=9, after=competitor(cur))
    b.write()
    return b


def test_competitor_mention_surge_triggers(tmp_path: Path) -> None:
    # 竞品提及 W1 6/30 = 0.2 → W2 18/30 = 0.6：升 0.4、相对升 200%、p ≈ 0.0008 → 触发（critical）。
    # 推荐率两窗都是 0/30 → 不触发。
    b = build_competitor(tmp_path, n=30, base=6, cur=18, recommended=False)
    code, doc = run_alerts(tmp_path)
    assert code == 1
    alerts = select(doc, "competitor_surge")
    assert [item["evidence"]["metric"] for item in alerts] == ["mention_rate"]
    alert = alerts[0]
    assert alert["entity_id"] == COMPETITOR
    assert alert["severity"] == "critical"
    assert alert["evidence"]["baseline"] == {"numerator": 6, "denominator": 30, "value": 0.2}
    assert alert["evidence"]["current"] == {"numerator": 18, "denominator": 30, "value": 0.6}
    assert alert["evidence"]["p_value"] == pytest.approx(
        two_proportion_p(6, 30, 18, 30, increase=True), abs=1e-6
    )
    w2 = [item for item in b.observations if item["window_id"] == "W2"]
    assert alert["observation_ids"] == ids(w2[:18])


def test_competitor_recommendation_surge_triggers(tmp_path: Path) -> None:
    # 被提及即被推荐：提及率、推荐率都从 0.2 升到 0.6 → 两条告警。
    build_competitor(tmp_path, n=30, base=6, cur=18, recommended=True)
    _, doc = run_alerts(tmp_path)
    metrics = sorted(item["evidence"]["metric"] for item in select(doc, "competitor_surge"))
    assert metrics == ["mention_rate", "recommended_rate"]


def test_competitor_small_sample_does_not_trigger(tmp_path: Path) -> None:
    # 每窗 5 条：竞品提及 1/5 → 5/5，升 0.8，但样本 < 20 → 不触发。
    build_competitor(tmp_path, n=5, base=1, cur=5, recommended=True)
    code, doc = run_alerts(tmp_path)
    assert code == 0
    assert doc["alerts"] == []


# ---------------------------------------------------------------- platform_conflict


def test_platform_conflict(tmp_path: Path) -> None:
    # 两个平台、两个窗口，各 10 条观测（样本少，提及类规则都不评估）。
    # 当前窗口 W2：engine-a 对 F-S7-PRICE 正确呈现 4 条、错误呈现 3 条 → 两者都 ≥ 3 → 冲突；
    #             engine-a 对 F-S7-RANGE-CLTC 只有错误呈现 5 条 → 不是冲突；
    #             engine-b 对 F-S7-PRICE 正确 5 条、错误 2 条 → 错误 < 3，不触发。
    # 基线 W1：engine-a 对 F-S7-PRICE 正确 2 条、错误 1 条（只作对照，不参与判断）。
    b = Builder(tmp_path)
    plan = {
        ("W1", "engine-a"): {"F-S7-PRICE": (2, 1)},
        ("W2", "engine-a"): {"F-S7-PRICE": (4, 3), "F-S7-RANGE-CLTC": (0, 5)},
        ("W2", "engine-b"): {"F-S7-PRICE": (5, 2)},
    }
    conflicted: list[dict[str, Any]] = []

    def judge(window: str, platform: str) -> Any:
        def write(observation: dict[str, Any], i: int) -> None:
            for fact, (correct, wrong) in plan.get((window, platform), {}).items():
                if i < correct:
                    label = "present_correct"
                elif i < correct + wrong:
                    label = "present_wrong"
                else:
                    continue
                b.judge(observation, label, fact_id=fact)
                if (window, platform, fact) == ("W2", "engine-a", "F-S7-PRICE"):
                    conflicted.append(observation)

        return write

    for window, day in (("W1", 8), ("W2", 9)):
        for platform in ("engine-a", "engine-b"):
            add(b, window, 10, day=day, platform=platform, after=judge(window, platform))
    b.write()
    code, doc = run_alerts(tmp_path)
    assert code == 1
    (alert,) = select(doc, "platform_conflict")
    assert alert["platform"] == "engine-a"
    assert alert["entity_id"] is None
    assert alert["severity"] == "warning"
    evidence = alert["evidence"]
    assert evidence["fact_id"] == "F-S7-PRICE"
    assert evidence["current"] == {"present_correct": 4, "present_wrong": 3}
    assert evidence["baseline"] == {"present_correct": 2, "present_wrong": 1}
    assert alert["observation_ids"] == ids(conflicted)
    assert "F-S7-PRICE" in alert["message"]
    assert [item["rule_id"] for item in doc["alerts"]] == ["platform_conflict"]


def test_platform_conflict_wrong_majority_is_critical(tmp_path: Path) -> None:
    # 当前窗口：正确 3 条、错误 4 条（错误不少于正确）→ critical。
    b = Builder(tmp_path)

    def judge(observation: dict[str, Any], i: int) -> None:
        if i < 7:
            b.judge(
                observation, "present_correct" if i < 3 else "present_wrong", fact_id="F-S7-PRICE"
            )

    add(b, "W1", 10, day=8)
    add(b, "W2", 10, day=9, after=judge)
    b.write()
    _, doc = run_alerts(tmp_path)
    (alert,) = select(doc, "platform_conflict")
    assert alert["severity"] == "critical"
    assert alert["evidence"]["baseline"] == {"present_correct": 0, "present_wrong": 0}
