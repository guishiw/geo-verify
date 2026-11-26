"""指标口径测试：每个期望值都由注释中的手工计数推算，不取自程序输出。"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli_json
from factory import COMPETITOR, TARGET, make_citation, make_prompt, seal_prompts
from helpers_metrics import Builder, find, metrics_file, utc

from geo_verify import spec
from geo_verify.jsonl import write_json
from geo_verify.stats import cluster_bootstrap_ratio, wilson_interval

SEED = 20261001  # 样例项目预先登记的 seed


def run_metrics(root: Path, *extra: str) -> dict[str, Any]:
    code, data = run_cli_json("metrics", str(root), *extra)
    assert code == 0, data
    return metrics_file(root)


def build_basic(root: Path) -> Builder:
    """基础场景（engine-a、engine-b 两个平台，三个窗口，四道题）。

    有效观测 9 条：
      o1 Q001 a W1 提及，位次 1，正面        o2 Q001 a W2 提及，位次 2，正面
      o3 Q001 a W3 未提及                    o4 Q002 a W1 提及，位次 4，中性
      o5 Q002 a W2 未提及                    o6 Q002 a W3 未提及（有推荐序列但不含目标）
      o7 Q001 b W1 提及，负面                o8 Q001 b W2 否定提及，负面
      o9 Q003 a W1 未提及
    无效观测 3 条：engine-a 超时 2 条、engine-b 限流 1 条。Q004 没有任何观测。
    """
    b = Builder(root, queries=("Q001", "Q002", "Q003", "Q004"))
    day = {"W1": 8, "W2": 9, "W3": 10}

    def add(query: str, platform: str, window: str, mentioned: bool, **ann: Any) -> None:
        if not mentioned:
            ann.setdefault("sentiment", None)
        b.obs(query, platform, window, mentioned=mentioned, ann=ann, started_at=utc(day[window]))

    add("Q001", "engine-a", "W1", True, rankable=True, rank=1, sentiment="positive")
    add("Q001", "engine-a", "W2", True, rankable=True, rank=2, sentiment="positive")
    add("Q001", "engine-a", "W3", False)
    add("Q002", "engine-a", "W1", True, rankable=True, rank=4, sentiment="neutral")
    add("Q002", "engine-a", "W2", False)
    add("Q002", "engine-a", "W3", False, rankable=True)
    add("Q001", "engine-b", "W1", True, sentiment="negative")
    add("Q001", "engine-b", "W2", True, negated=True, sentiment="negative")
    add("Q003", "engine-a", "W1", False)
    b.obs("Q001", "engine-a", "W1", status="invalid", invalid_reason="timeout")
    b.obs("Q001", "engine-a", "W1", status="invalid", invalid_reason="timeout")
    b.obs("Q002", "engine-b", "W2", status="invalid", invalid_reason="rate_limited")
    b.write()
    return b


@pytest.fixture
def basic(tmp_path: Path) -> list[dict[str, Any]]:
    build_basic(tmp_path / "proj")
    results: list[dict[str, Any]] = run_metrics(tmp_path / "proj")["results"]
    return results


def assert_ratio(result: dict[str, Any], numerator: int, denominator: int) -> None:
    assert (result["numerator"], result["denominator"]) == (numerator, denominator)
    assert result["value"] == pytest.approx(numerator / denominator, abs=1e-12)
    assert result["null_reason"] is None


def assert_null(result: dict[str, Any], reason: str) -> None:
    assert result["value"] is None
    assert reason in result["null_reason"]
    assert result["interval"] is None


# ---------------------------------------------------------------- 提及率与范围


def test_mention_rate_overall_and_platforms(basic: list[dict[str, Any]]) -> None:
    overall = find(basic, "mention_rate")
    assert_ratio(overall, 5, 9)
    assert overall["scope"]["entity_id"] == TARGET
    assert overall["excluded"] == {
        "count": 3,
        "rules": [
            {"rule": "status=invalid:rate_limited", "count": 1},
            {"rule": "status=invalid:timeout", "count": 2},
        ],
    }
    engine_a = find(basic, "mention_rate", platform="engine-a")
    assert_ratio(engine_a, 3, 7)
    assert engine_a["excluded"] == {
        "count": 2,
        "rules": [{"rule": "status=invalid:timeout", "count": 2}],
    }
    engine_b = find(basic, "mention_rate", platform="engine-b")
    assert_ratio(engine_b, 2, 2)
    assert engine_b["excluded"]["count"] == 1


def test_mention_rate_by_window_and_channel(basic: list[dict[str, Any]]) -> None:
    assert_ratio(find(basic, "mention_rate", window_id="W1"), 3, 4)
    assert_ratio(find(basic, "mention_rate", window_id="W2"), 2, 3)
    assert_ratio(find(basic, "mention_rate", window_id="W3"), 0, 2)
    assert_ratio(find(basic, "mention_rate", channel="api"), 5, 9)
    # engine-a 登记了 manual 通道但没有观测：给出空结果而不是省略
    assert_null(find(basic, "mention_rate", channel="manual"), "分母为 0")


def test_cross_query_interval_is_cluster_bootstrap(basic: list[dict[str, Any]]) -> None:
    overall = find(basic, "mention_rate")
    # 各题（分子, 分母）：Q001 (4,5)、Q002 (1,3)、Q003 (0,1)，按题目编号升序
    low, high = cluster_bootstrap_ratio([4, 1, 0], [5, 3, 1], seed=SEED, iterations=1000)
    assert overall["interval"] == {
        "method": "cluster_bootstrap",
        "level": 0.95,
        "low": low,
        "high": high,
    }
    # 手工推算：MSB=0.377778，MSW=0.244444，n0=2.555556 → ICC=0.175896；
    # m=35/9 → deff=1.508144；有效样本量=9/1.508144=5.9676
    clustering = overall["clustering"]
    assert clustering["clusters"] == 3
    assert clustering["icc"] == pytest.approx(0.175896, abs=1e-5)
    assert clustering["design_effect"] == pytest.approx(1.508144, abs=1e-5)
    assert clustering["effective_n"] == pytest.approx(5.9676, abs=1e-3)


def test_single_query_scope_uses_wilson(basic: list[dict[str, Any]]) -> None:
    engine_b = find(basic, "mention_rate", platform="engine-b")
    low, high = wilson_interval(2, 2, 0.95)
    assert engine_b["interval"] == {"method": "wilson", "level": 0.95, "low": low, "high": high}
    assert engine_b["clustering"] == {
        "clusters": 1,
        "icc": None,
        "design_effect": None,
        "effective_n": None,
    }


def test_sufficiency_uses_registered_thresholds(tmp_path: Path) -> None:
    b = build_basic(tmp_path / "proj")
    results = run_metrics(b.root)["results"]
    overall = find(results, "mention_rate")["sufficiency"]
    assert overall == {
        "sufficient": False,
        "windows": 3,
        "valid_observations": 9,
        "reasons": ["有效观测不足 30 次（实际 9 次）"],
    }
    window = find(results, "mention_rate", window_id="W1")["sufficiency"]
    assert window["windows"] == 1
    assert window["sufficient"] is False
    assert len(window["reasons"]) == 2

    relaxed = Builder(
        tmp_path / "relaxed",
        prereg={"sufficiency": {"min_windows": 2, "min_valid_observations": 3}},
    )
    for window_id in ("W1", "W2"):
        relaxed.obs("Q001", window=window_id)
        relaxed.obs("Q002", window=window_id)
    relaxed.write()
    sufficiency = find(run_metrics(relaxed.root)["results"], "mention_rate")["sufficiency"]
    assert sufficiency["sufficient"] is True
    assert sufficiency["reasons"] == []


# ---------------------------------------------------------------- 覆盖率、位次、推荐


def test_intent_coverage(basic: list[dict[str, Any]]) -> None:
    overall = find(basic, "intent_coverage")
    # 封存 4 题；Q001、Q002 出现过目标主体，Q003 未出现，Q004 没有观测
    assert_ratio(overall, 2, 4)
    assert overall["drilldown"]["unit"] == "query"
    assert overall["drilldown"]["numerator_ids"] == ["Q001", "Q002"]
    assert overall["drilldown"]["denominator_ids"] == ["Q001", "Q002", "Q003", "Q004"]
    low, high = wilson_interval(2, 4, 0.95)
    assert overall["interval"]["method"] == "wilson"
    assert (overall["interval"]["low"], overall["interval"]["high"]) == (low, high)
    assert overall["components"]["queries_without_valid_observations"] == 1
    assert_ratio(find(basic, "intent_coverage", platform="engine-b"), 1, 4)


def test_rank_rates_use_rankable_denominator(basic: list[dict[str, Any]]) -> None:
    # 可排序观测 4 条：位次 1、2、4、无（o6）；不可排序的观测不进分母
    assert_ratio(find(basic, "top1_rate"), 1, 4)
    assert_ratio(find(basic, "top3_rate"), 2, 4)
    assert_ratio(find(basic, "top5_rate"), 3, 4)
    engine_b = find(basic, "top1_rate", platform="engine-b")
    assert_null(engine_b, "没有可排序的观测")
    assert (engine_b["numerator"], engine_b["denominator"]) == (0, 0)


def test_recommendation_rate_default_criterion(basic: list[dict[str, Any]]) -> None:
    # 推荐成功 = recommended：o1、o2、o4、o7；o8 是否定提及，不算推荐
    overall = find(basic, "recommendation_rate")
    assert_ratio(overall, 4, 9)
    assert overall["components"]["criterion"] == "recommended"


def build_recommendation(root: Path, criterion: dict[str, Any], timezone: str) -> Builder:
    """Q001、Q002 应推荐；Q003 不要求推荐。

    r1 Q001 15:59:59Z 提及且推荐，位次 2      r2 Q001 16:00:00Z 提及未推荐（否定）
    r3 Q002 次日 03:00Z 推荐，位次 5           r4 Q002 次日 04:00Z 未提及
    r5 Q003 次日 05:00Z 推荐（不在“应推荐”范围）
    """
    prompts = [
        make_prompt("Q001"),
        make_prompt("Q002"),
        make_prompt("Q003", expects_recommendation=False),
    ]
    b = Builder(
        root, prompts=prompts, prereg={"recommendation_success": criterion}, timezone=timezone
    )
    b.obs("Q001", started_at=utc(8, 15, 59, 59), ann={"rankable": True, "rank": 2})
    b.obs("Q001", started_at=utc(8, 16, 0, 0), ann={"negated": True})
    b.obs("Q002", started_at=utc(9, 3), ann={"rankable": True, "rank": 5})
    b.obs("Q002", started_at=utc(9, 4), mentioned=False)
    b.obs("Q003", started_at=utc(9, 5))
    b.write()
    return b


@pytest.mark.parametrize(
    ("criterion", "numerator"),
    [
        ({"criterion": "recommended", "top_n": None}, 2),  # r1、r3
        ({"criterion": "top_n", "top_n": 3}, 1),  # 只有 r1 的位次 ≤ 3
        ({"criterion": "mentioned", "top_n": None}, 3),  # r1、r2、r3
    ],
)
def test_recommendation_criteria_are_registered(
    tmp_path: Path, criterion: dict[str, Any], numerator: int
) -> None:
    b = build_recommendation(tmp_path / "proj", criterion, "Asia/Shanghai")
    overall = find(run_metrics(b.root)["results"], "recommendation_rate")
    # 应推荐次数 = Q001、Q002 的 4 条有效观测；Q003 不要求推荐
    assert_ratio(overall, numerator, 4)
    assert overall["components"]["criterion"] == criterion["criterion"]
    assert overall["components"]["top_n"] == criterion["top_n"]
    assert overall["components"]["not_expected_observations"] == 1


def test_daily_recommendation_uses_project_timezone(tmp_path: Path) -> None:
    criterion = {"criterion": "recommended", "top_n": None}
    shanghai = build_recommendation(tmp_path / "sh", criterion, "Asia/Shanghai")
    results = run_metrics(shanghai.root)["results"]
    # 上海时区：15:59:59Z 是 10-08 23:59:59；16:00:00Z 已是 10-09 00:00:00
    assert_ratio(find(results, "recommendation_rate_daily", date="2026-10-08"), 1, 1)
    assert_ratio(find(results, "recommendation_rate_daily", date="2026-10-09"), 1, 3)
    day8 = find(results, "recommendation_rate_daily", date="2026-10-08")
    assert day8["drilldown"]["denominator_ids"] == ["obs-000001"]
    day9 = find(results, "recommendation_rate_daily", date="2026-10-09")
    assert day9["drilldown"]["denominator_ids"] == ["obs-000002", "obs-000003", "obs-000004"]
    assert day9["drilldown"]["numerator_ids"] == ["obs-000003"]
    assert_ratio(
        find(results, "recommendation_rate_daily", date="2026-10-09", platform="engine-a"), 1, 3
    )
    dates = {r["scope"]["date"] for r in results if r["metric_id"] == "recommendation_rate_daily"}
    assert dates == {"2026-10-08", "2026-10-09"}

    utc_project = build_recommendation(tmp_path / "utc", criterion, "UTC")
    results = run_metrics(utc_project.root)["results"]
    # UTC：r1、r2 都在 10-08；r3、r4 在 10-09
    assert_ratio(find(results, "recommendation_rate_daily", date="2026-10-08"), 1, 2)
    assert_ratio(find(results, "recommendation_rate_daily", date="2026-10-09"), 1, 2)


# ---------------------------------------------------------------- 倾向、负面、风险


def test_sentiment_distribution(basic: list[dict[str, Any]]) -> None:
    # 提及且有倾向标注的观测 5 条：正 2（o1、o2）、中 1（o4）、负 2（o7、o8）
    assert_ratio(find(basic, "sentiment_positive_share"), 2, 5)
    assert_ratio(find(basic, "sentiment_neutral_share"), 1, 5)
    assert_ratio(find(basic, "sentiment_negative_share"), 2, 5)


def build_risk(root: Path) -> Builder:
    """6 条有效观测的风险标注：

    k1 无依据负面，已识别，复核确认，已闭环
    k2 无依据负面（仅 flags），已识别，复核确认，未闭环
    k3 错误事实且倾向为负，未识别，复核确认（漏识别）
    k4 已识别，复核否认（误报），未闭环
    k5 错误事实但倾向中性，未识别，未复核
    k6 无风险；unfounded_negative 为 null（未评估）
    """
    b = Builder(root)
    risks: list[tuple[dict[str, Any], str]] = [
        (
            {"unfounded_negative": True, "identified": True, "confirmed": True, "closed": True},
            "negative",
        ),
        (
            {
                "flags": ["unfounded_negative"],
                "identified": True,
                "confirmed": True,
                "closed": False,
            },
            "negative",
        ),
        ({"flags": ["wrong_fact"], "identified": False, "confirmed": True}, "negative"),
        ({"identified": True, "confirmed": False, "closed": None}, "neutral"),
        ({"flags": ["wrong_fact"]}, "neutral"),
        ({"unfounded_negative": None}, "neutral"),
    ]
    for index, (risk, sentiment) in enumerate(risks):
        b.obs(f"Q00{index % 3 + 1}", ann={"risk": risk, "sentiment": sentiment})
    b.write()
    return b


def test_negative_miscitation_and_risk_rates(tmp_path: Path) -> None:
    results = run_metrics(build_risk(tmp_path / "proj").root)["results"]
    negative = find(results, "negative_miscitation_rate")
    # 含无依据或错误负面：k1、k2、k3（错误事实 + 负面倾向）
    assert_ratio(negative, 3, 6)
    assert negative["components"]["unassessed"] == 1
    assert negative["drilldown"]["numerator_ids"] == ["obs-000001", "obs-000002", "obs-000003"]
    identification = find(results, "risk_identification_rate")
    # 复核确认应识别 3 条（k1、k2、k3），其中已识别 2 条
    assert_ratio(identification, 2, 3)
    assert identification["components"]["unreviewed"] == 2
    closure = find(results, "risk_closure_rate")
    # 已识别 3 条（k1、k2、k4），已闭环 1 条
    assert_ratio(closure, 1, 3)
    assert closure["components"]["identified_not_confirmed"] == 1


# ---------------------------------------------------------------- 引用类指标


def build_citations(root: Path) -> Builder:
    """三条有效观测：

    c1 引用：目标官网 /page-1、同一 URL 重复一次、gov-data /page-3、域名无法识别的 1 条
       标注核验：第 1 条有效且相关；第 3 条有效但不相关
    c2 引用：other-media /page-1（未核验）
    c3 没有引用
    """
    b = Builder(root)
    duplicate = make_citation(2, "lanxu-auto.example")
    duplicate["url"] = "https://lanxu-auto.example/page-1"
    unknown = make_citation(4, "x.example")
    unknown.update(domain=None, url="unresolvable-reference")
    c1 = b.obs(
        "Q001",
        mentioned=None,
        citations=[
            make_citation(1, "lanxu-auto.example"),
            duplicate,
            make_citation(3, "gov-data.example", placement="source_list"),
            unknown,
        ],
    )
    b.annotate(
        c1,
        citation_checks=[
            {"order": 1, "valid": True, "relevant": True},
            {"order": 3, "valid": True, "relevant": False},
        ],
    )
    b.obs("Q002", citations=[make_citation(1, "other-media.example")])
    b.obs("Q003")
    b.write()
    return b


def test_citation_metrics(tmp_path: Path) -> None:
    results = run_metrics(build_citations(tmp_path / "proj").root)["results"]
    presence = find(results, "target_source_presence")
    assert_ratio(presence, 1, 3)
    assert presence["components"]["target_sources"] == ["lanxu-auto.example"]
    share = find(results, "target_source_share")
    # 去重后可识别引用 3 条：目标官网 1、gov-data 1、other-media 1
    assert_ratio(share, 1, 3)
    assert share["drilldown"]["unit"] == "citation"
    assert share["drilldown"]["numerator_ids"] == ["obs-000001#1"]
    assert share["drilldown"]["denominator_ids"] == [
        "obs-000001#1",
        "obs-000001#3",
        "obs-000002#1",
    ]
    assert share["components"]["unidentified_citations"] == 1
    assert share["components"]["duplicate_citations"] == 1
    authority = find(results, "authority_share")
    # 登记的权威等级为 A、B：只有 gov-data（A）算权威；目标官网为 C
    assert_ratio(authority, 1, 3)
    assert authority["components"]["authoritative_tiers"] == ["A", "B"]
    assert authority["components"]["untiered_citations"] == 1
    validity = find(results, "citation_validity")
    # 已核验 2 条，其中有效且相关 1 条；未核验 1 条
    assert_ratio(validity, 1, 2)
    assert validity["components"] == {
        "identifiable_citations": 3,
        "verified_citations": 2,
        "unverified_citations": 1,
        "note": "有 1 条可识别引用尚未核验，未计入分母",
    }


def test_citation_metrics_without_any_citation_or_check(tmp_path: Path) -> None:
    b = Builder(tmp_path / "none")
    b.obs("Q001")
    b.obs("Q002", citations=[make_citation(1, "other-media.example")])
    b.write()
    results = run_metrics(b.root)["results"]
    assert_null(find(results, "citation_validity"), "没有已核验的引用")
    assert find(results, "citation_validity")["components"]["unverified_citations"] == 1
    assert_ratio(find(results, "target_source_presence"), 0, 2)
    assert_ratio(find(results, "target_source_share"), 0, 1)

    empty = Builder(tmp_path / "empty")
    empty.obs("Q001")
    empty.write()
    results = run_metrics(empty.root)["results"]
    assert_null(find(results, "target_source_share"), "没有可识别的引用")
    assert_null(find(results, "authority_share"), "没有可识别的引用")
    assert_null(find(results, "citation_validity"), "没有可识别的引用")


def test_target_sources_include_owned_and_subdomains(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj", prereg={"target_sources": ["partner-news.example"]})
    b.obs("Q001", citations=[make_citation(1, "www.lanxu-auto.example")])
    b.obs("Q002", citations=[make_citation(1, "partner-news.example")])
    b.obs("Q003", citations=[make_citation(1, "notlanxu-auto.example")])
    b.write()
    results = run_metrics(b.root)["results"]
    presence = find(results, "target_source_presence")
    # 登记信源 partner-news + 目标主体自有域名 lanxu-auto（含其子域名）；
    # notlanxu-auto.example 只是后缀相同，不是子域名
    assert_ratio(presence, 2, 3)
    assert presence["components"]["target_sources"] == [
        "lanxu-auto.example",
        "partner-news.example",
    ]


# ---------------------------------------------------------------- 准确率与呈现率


def build_judgments(root: Path) -> Builder:
    """回答 → 事实库：支持 6、矛盾 2、不可核验 2；事实库 → 回答：正确 3、错误 1、缺失 1。

    另有来自无效观测、被剔除观测的判定各 1 条，不得计入。
    """
    b = Builder(root)
    first = b.obs("Q001")
    second = b.obs("Q002", platform="engine-b")
    for label in ("supported", "supported", "supported", "contradicted", "unverifiable"):
        b.judge(first, label)
    for label in ("supported", "supported", "supported", "contradicted", "unverifiable"):
        b.judge(second, label)
    for label in ("present_correct", "present_correct", "present_wrong"):
        b.judge(first, label)
    for label in ("present_correct", "missing"):
        b.judge(second, label)
    invalid = b.obs("Q003", status="invalid", invalid_reason="truncated")
    b.judge(invalid, "contradicted")
    removed = b.obs("Q003")
    b.judge(removed, "contradicted")
    b.judge(removed, "missing")
    b.write()
    b.exclude([removed])
    return b


def test_accuracy_and_presence_use_separate_denominators(tmp_path: Path) -> None:
    results = run_metrics(build_judgments(tmp_path / "proj").root)["results"]
    verifiable = find(results, "accuracy_verifiable")
    assert_ratio(verifiable, 6, 8)
    assert verifiable["drilldown"]["unit"] == "judgment"
    assert verifiable["components"] == {
        "supported": 6,
        "contradicted": 2,
        "unverifiable": 2,
        "accuracy_basis": "verifiable",
        "is_registered_basis": True,
        "judgments_from_non_valid_observations": 3,
    }
    strict = find(results, "accuracy_strict")
    assert_ratio(strict, 6, 10)
    assert strict["components"]["is_registered_basis"] is False
    assert_ratio(find(results, "unverifiable_rate"), 2, 10)
    presence = find(results, "core_fact_presence")
    assert_ratio(presence, 3, 5)
    assert presence["components"] == {
        "present_correct": 3,
        "present_wrong": 1,
        "missing": 1,
        "judgments_from_non_valid_observations": 3,
    }
    # 按平台：engine-a 只有 first（支持 3、矛盾 1、不可核验 1）
    assert_ratio(find(results, "accuracy_verifiable", platform="engine-a"), 3, 4)
    assert_ratio(find(results, "accuracy_strict", platform="engine-b"), 3, 5)
    # 两题 → 聚类 bootstrap，按观测所属题目聚类
    assert verifiable["interval"]["method"] == "cluster_bootstrap"
    assert verifiable["clustering"]["clusters"] == 2
    assert verifiable["sufficiency"]["valid_observations"] == 2


def test_no_judgments_gives_null_with_reason(basic: list[dict[str, Any]]) -> None:
    for metric_id in ("accuracy_verifiable", "accuracy_strict", "unverifiable_rate"):
        assert_null(find(basic, metric_id), "没有可用的判定")
    assert_null(find(basic, "core_fact_presence"), "没有可用的判定")


# ---------------------------------------------------------------- 跨时间、跨平台


def test_temporal_stability(basic: list[dict[str, Any]]) -> None:
    # 各窗口提及率：W1=3/4、W2=2/3、W3=0；均值 17/36
    # 样本标准差 = sqrt((100+49+289)/1296/2) = sqrt(219)/36；极差 0.75；变异系数 = sqrt(219)/17
    std = find(basic, "temporal_std")
    assert std["value"] == pytest.approx(math.sqrt(219) / 36, abs=1e-12)
    assert (std["numerator"], std["denominator"]) == (None, None)
    assert std["interval"] is None
    assert std["drilldown"]["unit"] == "window"
    assert std["drilldown"]["denominator_ids"] == ["W1", "W2", "W3"]
    assert std["components"]["windows"] == [
        {"window_id": "W1", "numerator": 3, "denominator": 4, "value": 0.75},
        {"window_id": "W2", "numerator": 2, "denominator": 3, "value": 2 / 3},
        {"window_id": "W3", "numerator": 0, "denominator": 2, "value": 0.0},
    ]
    assert find(basic, "temporal_range")["value"] == pytest.approx(0.75, abs=1e-12)
    assert find(basic, "temporal_cv")["value"] == pytest.approx(math.sqrt(219) / 17, abs=1e-12)
    # engine-b 只有 W1、W2 两个窗口且提及率都是 1：标准差 0、极差 0、变异系数 0
    assert find(basic, "temporal_std", platform="engine-b")["value"] == 0.0
    assert find(basic, "temporal_cv", platform="engine-b")["value"] == 0.0


def test_temporal_stability_needs_two_windows_and_nonzero_mean(tmp_path: Path) -> None:
    b = Builder(tmp_path / "one")
    b.obs("Q001")
    b.write()
    results = run_metrics(b.root)["results"]
    for metric_id in ("temporal_std", "temporal_range", "temporal_cv"):
        assert_null(find(results, metric_id), "至少需要 2 个")
    zero = Builder(tmp_path / "zero")
    zero.obs("Q001", window="W1", mentioned=False)
    zero.obs("Q001", window="W2", mentioned=False)
    zero.write()
    results = run_metrics(zero.root)["results"]
    assert find(results, "temporal_std")["value"] == 0.0
    assert_null(find(results, "temporal_cv"), "均值为 0")


def build_cross_platform(root: Path) -> Builder:
    """三道题在 engine-a、engine-b 上的表现：

    推荐逻辑（按登记的“推荐成功”定义，平台内成功比例 ≥ 0.5 记为“推荐”）
      Q001：a 推荐 2/2，b 推荐 1/2 → 一致
      Q002：a 推荐 0/2，b 推荐 2/2 → 不一致
      Q003：只有 a 有观测 → 不进分母
    来源指向（平台内被最多观测引用的域名）
      Q001：a 为 lanxu-auto（2 次），b 为 lanxu-auto → 一致
      Q002：a 为 other-media，b 为 gov-data → 不一致
    核心事实（平台内正确呈现比例 ≥ 0.5 记为“正确呈现”）
      (Q001, F-A)：a 正确 2/2，b 正确 1/1 → 一致
      (Q001, F-B)：a 正确 0/1，b 正确 1/1 → 不一致
      (Q002, F-A)：a 缺失，b 错误 → 都不是“正确呈现”→ 一致
    """
    b = Builder(root)
    lanxu = [make_citation(1, "lanxu-auto.example")]
    other = [make_citation(1, "other-media.example")]
    gov = [make_citation(1, "gov-data.example")]
    a1 = b.obs("Q001", "engine-a", citations=lanxu)
    a2 = b.obs("Q001", "engine-a", citations=[*lanxu, make_citation(2, "other-media.example")])
    b1 = b.obs("Q001", "engine-b", citations=lanxu)
    b.obs("Q001", "engine-b", mentioned=False)
    a3 = b.obs("Q002", "engine-a", mentioned=False, citations=other)
    b.obs("Q002", "engine-a", mentioned=False)
    b2 = b.obs("Q002", "engine-b", citations=gov)
    b.obs("Q002", "engine-b")
    b.obs("Q003", "engine-a")
    b.judge(a1, "present_correct", fact_id="F-A")
    b.judge(a2, "present_correct", fact_id="F-A")
    b.judge(b1, "present_correct", fact_id="F-A")
    b.judge(a1, "missing", fact_id="F-B")
    b.judge(b1, "present_correct", fact_id="F-B")
    b.judge(a3, "missing", fact_id="F-A")
    b.judge(b2, "present_wrong", fact_id="F-A")
    b.write()
    return b


def test_cross_platform_consistency(tmp_path: Path) -> None:
    results = run_metrics(build_cross_platform(tmp_path / "proj").root)["results"]
    recommendation = find(results, "cross_platform_recommendation")
    assert_ratio(recommendation, 1, 2)
    assert recommendation["drilldown"]["unit"] == "query"
    assert recommendation["drilldown"]["numerator_ids"] == ["Q001"]
    assert recommendation["drilldown"]["denominator_ids"] == ["Q001", "Q002"]
    assert recommendation["scope"]["platform"] is None
    sources = find(results, "cross_platform_sources")
    assert_ratio(sources, 1, 2)
    assert sources["drilldown"]["numerator_ids"] == ["Q001"]
    facts = find(results, "cross_platform_core_facts")
    assert_ratio(facts, 2, 3)
    assert facts["drilldown"]["unit"] == "fact"
    assert facts["drilldown"]["denominator_ids"] == ["Q001|F-A", "Q001|F-B", "Q002|F-A"]
    assert facts["drilldown"]["numerator_ids"] == ["Q001|F-A", "Q002|F-A"]
    # 跨平台一致性只在整体范围给出
    for metric_id in (
        "cross_platform_core_facts",
        "cross_platform_sources",
        "cross_platform_recommendation",
    ):
        assert [r for r in results if r["metric_id"] == metric_id] == [find(results, metric_id)]


def test_cross_platform_needs_two_platforms(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001")
    b.write()
    results = run_metrics(b.root)["results"]
    assert_null(find(results, "cross_platform_recommendation"), "至少两个平台")
    assert_null(find(results, "cross_platform_sources"), "至少两个平台")
    assert_null(find(results, "cross_platform_core_facts"), "至少两个平台")


# ---------------------------------------------------------------- 过程质量


def test_valid_observation_rate(basic: list[dict[str, Any]]) -> None:
    overall = find(basic, "valid_observation_rate")
    assert_ratio(overall, 9, 12)
    assert overall["scope"]["entity_id"] is None
    # 分母是全部观测：无效观测在分母里，不属于“被剔除”
    assert overall["excluded"] == {"count": 0, "rules": []}
    assert overall["components"]["not_valid"] == [
        {"rule": "status=invalid:rate_limited", "count": 1},
        {"rule": "status=invalid:timeout", "count": 2},
    ]
    assert_ratio(find(basic, "valid_observation_rate", platform="engine-a"), 7, 9)
    assert_ratio(find(basic, "valid_observation_rate", platform="engine-b"), 2, 3)


def test_environment_consistency(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    # 登记环境：未登录、清除历史、联网、地域 CN、设备不作要求
    b.obs("Q001")  # 一致
    b.obs("Q001", region="CN-SH")  # 地域是 CN 的下级 → 一致
    b.obs("Q002", logged_in=True)  # 登录态不符
    b.obs("Q002", web_search=None, null_reasons={"web_search": "未知"})  # 无法确认
    b.obs("Q003", region="US")  # 地域不符
    b.obs("Q003", device="mobile")  # 设备不作要求 → 一致
    b.obs("Q003", logged_in=True, status="invalid", invalid_reason="error")  # 无效，不进分母
    b.write()
    result = find(run_metrics(b.root)["results"], "environment_consistency_rate")
    assert_ratio(result, 3, 6)
    assert result["components"]["unknown_environment"] == 1
    assert result["components"]["registered_environment"] == {
        "logged_in": False,
        "history_cleared": True,
        "web_search": True,
        "region": "CN",
    }
    assert result["drilldown"]["numerator_ids"] == ["obs-000001", "obs-000002", "obs-000006"]


def test_environment_without_requirements_is_null(tmp_path: Path) -> None:
    nothing = dict.fromkeys(
        ("logged_in", "history_cleared", "web_search", "region", "device"), None
    )
    b = Builder(tmp_path / "proj", environment=nothing)
    b.obs("Q001")
    b.write()
    result = find(run_metrics(b.root)["results"], "environment_consistency_rate")
    assert_null(result, "没有登记环境要求")


def test_raw_record_completeness(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001")  # 完整（设备、快照为空但都写明了原因）
    missing_field = b.obs("Q001")
    del missing_field["collector"]  # 缺必备字段
    undocumented = b.obs("Q002")
    undocumented["region"] = None  # 为空但未说明原因
    no_entry = b.obs("Q002")
    no_entry["model_version"] = None  # 版本未知且未写入口特征
    no_entry["null_reasons"]["model_version"] = "未展示"
    b.obs("Q003", status="invalid", invalid_reason="timeout")  # 无效但记录完整
    empty_answer = b.obs("Q003")
    empty_answer["answer_text"] = ""  # 有效观测却没有回答
    b.write()
    result = find(run_metrics(b.root)["results"], "raw_record_completeness")
    assert_ratio(result, 2, 6)
    assert result["drilldown"]["numerator_ids"] == ["obs-000001", "obs-000005"]
    assert result["components"]["with_documented_nulls"] == 2


def test_evidence_completeness(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    snapshots = b.root / "snapshots"
    snapshots.mkdir(parents=True)

    def snapshot(name: str, content: bytes, *, digest: str | None = None) -> list[dict[str, Any]]:
        if content:
            (snapshots / name).write_bytes(content)
        return [
            {
                "path": f"snapshots/{name}",
                "sha256": digest or hashlib.sha256(content).hexdigest(),
                "media_type": "text/html",
            }
        ]

    b.obs("Q001", snapshots=snapshot("ok.html", b"<p>ok</p>"))  # 完整
    b.obs("Q001", snapshots=snapshot("gone.html", b""))  # 文件不存在
    b.obs("Q002", snapshots=snapshot("bad.html", b"<p>bad</p>", digest="0" * 64))  # 哈希不符
    b.obs("Q002")  # 没有快照
    b.obs(
        "Q003",
        snapshots=snapshot("ok.html", b"<p>ok</p>") + snapshot("gone.html", b""),
    )  # 两个快照中有一个缺失
    b.write()
    result = find(run_metrics(b.root)["results"], "evidence_completeness")
    assert_ratio(result, 1, 5)
    assert result["components"] == {
        "no_snapshot": 1,
        "missing_file": 2,
        "hash_mismatch": 1,
    }


def test_recheck_pass_rate_reads_result_file(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    observations = [b.obs("Q001"), b.obs("Q001"), b.obs("Q002", platform="engine-b")]
    b.write()
    results = run_metrics(b.root)["results"]
    assert_null(find(results, "recheck_pass_rate"), "尚未执行抽样复核")
    write_json(
        b.root / "recheck" / "result.json",
        {
            "schema_version": "1.0",
            "kind": "recheck_result",
            "items": [
                {
                    "observation_id": observations[0]["observation_id"],
                    "entity_id": TARGET,
                    "agree": True,
                },
                {
                    "observation_id": observations[1]["observation_id"],
                    "entity_id": TARGET,
                    "agree": False,
                },
                {
                    "observation_id": observations[2]["observation_id"],
                    "entity_id": TARGET,
                    "agree": True,
                },
                {
                    "observation_id": observations[2]["observation_id"],
                    "entity_id": COMPETITOR,
                    "agree": True,
                },
                {"observation_id": "obs-999999", "entity_id": TARGET, "agree": True},
            ],
        },
    )
    results = run_metrics(b.root)["results"]
    overall = find(results, "recheck_pass_rate")
    # 4 个复核项对应有效观测，其中 3 项一致；1 项的观测不存在，不计入
    assert_ratio(overall, 3, 4)
    assert overall["components"]["items_without_valid_observation"] == 1
    assert overall["drilldown"]["denominator_ids"] == [
        "obs-000001|brand-lanxu",
        "obs-000002|brand-lanxu",
        "obs-000003|brand-cenye",
        "obs-000003|brand-lanxu",
    ]
    assert_ratio(find(results, "recheck_pass_rate", platform="engine-b"), 2, 2)


# ---------------------------------------------------------------- 剔除、版本、登记


def test_audit_exclusions_are_counted_and_not_mixed(tmp_path: Path) -> None:
    b = build_basic(tmp_path / "proj")
    # 剔除 o3（未提及）与一条本来就无效的观测
    b.exclude([b.observations[2], b.observations[9]], "EX-DUP")
    b.exclude([b.observations[4]], "EX-OTHER")
    results = run_metrics(b.root)["results"]
    overall = find(results, "mention_rate")
    # 有效观测 9 → 7（o3、o5 被剔除）；提及仍为 5
    assert_ratio(overall, 5, 7)
    assert overall["excluded"] == {
        "count": 5,
        "rules": [
            {"rule": "audit:EX-DUP", "count": 2},
            {"rule": "audit:EX-OTHER", "count": 1},
            {"rule": "status=invalid:rate_limited", "count": 1},
            {"rule": "status=invalid:timeout", "count": 1},
        ],
    }
    assert "obs-000003" not in overall["drilldown"]["denominator_ids"]
    # 有效观测率的分母仍是全部 12 条
    assert_ratio(find(results, "valid_observation_rate"), 7, 12)


def test_other_prompt_set_versions_are_not_mixed(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    old = b.sealed
    for _ in range(3):
        b.obs("Q001", mentioned=True)
    new = seal_prompts(
        b.root,
        b.project,
        [make_prompt("Q001"), make_prompt("Q002"), make_prompt("Q009")],
        previous=old,
        sealed_at="2026-10-05T03:00:00Z",
    )
    b.obs("Q001", prompt_set=new, mentioned=False)
    b.obs("Q009", prompt_set=new, mentioned=True)
    unknown = dict(new)
    unknown["content_hash"] = "ab" * 32
    b.obs("Q001", prompt_set=unknown, mentioned=True)
    b.write()

    latest = run_metrics(b.root)
    assert latest["prompt_set"] == {"version": 2, "content_hash": new["content_hash"]}
    overall = find(latest["results"], "mention_rate")
    # 默认取最新版（v2）：只有 2 条观测属于它，旧版 3 条和未知版本 1 条都不参与
    assert_ratio(overall, 1, 2)
    assert overall["excluded"] == {
        "count": 4,
        "rules": [{"rule": "prompt_set_version_mismatch", "count": 4}],
    }
    assert_ratio(find(latest["results"], "valid_observation_rate"), 2, 2)
    assert find(latest["results"], "valid_observation_rate")["excluded"]["count"] == 4
    notice = next(n for n in latest["notices"] if n["code"] == "prompt_set_version_mismatch")
    assert notice["severity"] == "warning"
    assert "禁止混算" in notice["message"]
    assert notice["details"]["selected_version"] == 2
    assert notice["details"]["versions"] == [
        {"version": 1, "content_hash": old["content_hash"], "observations": 3},
        {"version": 2, "content_hash": new["content_hash"], "observations": 2},
        {"version": None, "content_hash": "ab" * 32, "observations": 1},
    ]

    first = run_metrics(b.root, "--prompt-set-version", "1")
    assert first["prompt_set"]["version"] == 1
    assert_ratio(find(first["results"], "mention_rate"), 3, 3)
    assert_ratio(find(first["results"], "intent_coverage"), 1, 3)


def test_observation_with_unknown_query_is_excluded(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001")
    b.obs("Q777")
    b.write()
    data = run_metrics(b.root)
    overall = find(data["results"], "mention_rate")
    assert_ratio(overall, 1, 1)
    assert overall["excluded"]["rules"] == [{"rule": "unknown_query_id", "count": 1}]
    assert any(n["code"] == "unknown_query_ids" for n in data["notices"])


def test_duplicate_observation_ids_are_counted_once(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    first = b.obs("Q001")
    b.obs("Q002")
    b.observations.append(dict(first))
    b.write()
    data = run_metrics(b.root)
    overall = find(data["results"], "mention_rate")
    assert_ratio(overall, 2, 2)
    assert overall["excluded"]["rules"] == [{"rule": "duplicate_observation_id", "count": 1}]
    assert_ratio(find(data["results"], "valid_observation_rate"), 2, 2)
    assert any(n["code"] == "duplicate_observation_ids" for n in data["notices"])


def test_unannotated_observations_are_reported_not_assumed(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001", mentioned=True)
    b.obs("Q001", mentioned=None)  # 没有标注
    b.obs("Q002", mentioned=False)
    b.write()
    data = run_metrics(b.root)
    overall = find(data["results"], "mention_rate")
    # 没有标注的观测既不算提及也不算未提及：不进分母，并如实列出
    assert_ratio(overall, 1, 2)
    assert overall["excluded"] == {"count": 1, "rules": [{"rule": "unannotated", "count": 1}]}
    notice = next(n for n in data["notices"] if n["code"] == "unannotated_observations")
    assert notice["details"]["count"] == 1
    # 不依赖标注的指标不受影响
    assert_ratio(find(data["results"], "target_source_presence"), 0, 3)
    assert find(data["results"], "target_source_presence")["excluded"]["count"] == 0


def test_human_override_takes_effect(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    observation = b.obs("Q001", mentioned=True)
    rule_annotation = b.annotations[-1]
    b.annotate(
        observation,
        mentioned=False,
        sentiment=None,
        annotator_type="human",
        annotator_id="复核员甲（虚构）",
        created_at="2026-10-09T03:00:00Z",
        supersedes=rule_annotation["annotation_id"],
    )
    b.write()
    assert_ratio(find(run_metrics(b.root)["results"], "mention_rate"), 0, 1)


def test_preregistered_roles_and_post_hoc(basic: list[dict[str, Any]]) -> None:
    mention = find(basic, "mention_rate")
    assert (mention["preregistered"], mention["role"]) == (True, "acceptance")
    assert mention["definition_version"] == "1.0"
    # 样例登记了 sentiment_distribution（描述性），覆盖三个倾向占比
    for metric_id in (
        "sentiment_positive_share",
        "sentiment_neutral_share",
        "sentiment_negative_share",
    ):
        result = find(basic, metric_id)
        assert (result["preregistered"], result["role"]) == (True, "descriptive")
    # 未登记的指标是事后指标
    for metric_id in ("top1_rate", "intent_coverage", "valid_observation_rate"):
        result = find(basic, metric_id)
        assert (result["preregistered"], result["role"]) == (False, "post_hoc")
    by_window = find(basic, "mention_rate", window_id="W1")
    assert (by_window["preregistered"], by_window["role"]) == (True, "acceptance")


def test_accuracy_alias_follows_registered_basis(tmp_path: Path) -> None:
    metrics = [
        {
            "metric_id": "accuracy",
            "definition_version": "1.0",
            "role": "acceptance",
            "threshold": {"op": ">=", "value": 0.9},
        },
        {
            "metric_id": "top3_rate",
            "definition_version": "2.0",
            "role": "descriptive",
            "threshold": None,
        },
    ]
    b = Builder(tmp_path / "proj", prereg={"metrics": metrics, "accuracy_basis": "strict"})
    b.judge(b.obs("Q001"), "supported")
    b.write()
    data = run_metrics(b.root)
    strict = find(data["results"], "accuracy_strict")
    assert (strict["preregistered"], strict["role"]) == (True, "acceptance")
    verifiable = find(data["results"], "accuracy_verifiable")
    assert (verifiable["preregistered"], verifiable["role"]) == (False, "post_hoc")
    # 登记的口径版本与工具实现的版本不同：不能当作已登记口径
    top3 = find(data["results"], "top3_rate")
    assert (top3["preregistered"], top3["role"]) == (False, "post_hoc")
    notice = next(n for n in data["notices"] if n["code"] == "definition_version_mismatch")
    assert notice["details"]["metric_id"] == "top3_rate"


def test_preregistration_changed_after_sealing_marks_everything_post_hoc(tmp_path: Path) -> None:
    b = build_basic(tmp_path / "proj")
    prereg = dict(b.project["preregistration"])
    prereg["metrics"] = [
        {
            "metric_id": "mention_rate",
            "definition_version": "1.0",
            "role": "acceptance",
            "threshold": {"op": ">=", "value": 0.5},  # 封存后把阈值从 0.6 改成 0.5
        }
    ]
    b.update_project(preregistration=prereg)
    data = run_metrics(b.root)
    assert all(not r["preregistered"] and r["role"] == "post_hoc" for r in data["results"])
    notice = next(n for n in data["notices"] if n["code"] == "preregistration_changed")
    assert notice["severity"] == "error"
    assert "封存后" in notice["message"]
    assert notice["details"]["sealed_hash"] == b.sealed["preregistration_hash"]
    assert notice["details"]["current_hash"] != b.sealed["preregistration_hash"]


def test_model_version_change_is_noticed(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001", "engine-a", "W1", started_at=utc(8))
    b.obs("Q001", "engine-a", "W2", started_at=utc(9), model_version="engine-a-2026.11")
    b.obs("Q001", "engine-a", "W2", started_at=utc(10), model_version="engine-a-2026.11")
    b.obs("Q001", "engine-b", "W1")
    b.obs("Q001", "engine-b", "W2", model_version=None, channel="manual")
    b.write()
    data = run_metrics(b.root)
    notices = [n for n in data["notices"] if n["code"] == "model_version_changed"]
    assert len(notices) == 1
    notice = notices[0]
    assert notice["severity"] == "warning"
    assert "须重建基线" in notice["message"]
    assert "不得合并比较" in notice["message"]
    assert notice["details"] == {
        "platform": "engine-a",
        "channel": "api",
        "versions": [
            {
                "model_version": "engine-a-2026.10",
                "observations": 1,
                "first_seen": utc(8),
                "last_seen": utc(8),
            },
            {
                "model_version": "engine-a-2026.11",
                "observations": 2,
                "first_seen": utc(9),
                "last_seen": utc(10),
            },
        ],
    }
    assert run_metrics(build_basic(tmp_path / "ok").root)["notices"] == []


# ---------------------------------------------------------------- 空数据与契约


def test_project_without_observations_produces_null_results(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    data = run_metrics(b.root)
    assert data["results"]
    for result in data["results"]:
        assert result["value"] is None, result["metric_id"]
        assert isinstance(result["null_reason"], str)
        assert result["null_reason"].strip()
        assert result["interval"] is None
        assert spec.iter_issues("metric_result", result) == []
    overall = find(data["results"], "mention_rate")
    assert (overall["numerator"], overall["denominator"]) == (0, 0)
    assert overall["sufficiency"]["sufficient"] is False
    coverage = find(data["results"], "intent_coverage")
    assert_null(coverage, "没有有效观测")
    assert (coverage["numerator"], coverage["denominator"]) == (0, 3)


def test_every_result_satisfies_contract_and_has_no_fake_zero(tmp_path: Path) -> None:
    for name, builder in (
        ("basic", build_basic),
        ("citations", build_citations),
        ("judgments", build_judgments),
        ("cross", build_cross_platform),
        ("risk", build_risk),
    ):
        data = run_metrics(builder(tmp_path / name).root)
        assert len({(r["metric_id"], str(r["scope"])) for r in data["results"]}) == len(
            data["results"]
        )
        for result in data["results"]:
            assert spec.iter_issues("metric_result", result) == [], result["metric_id"]
            assert result["synthetic"] is True
            if result["denominator"] == 0:
                assert result["value"] is None
                assert result["null_reason"]
            if result["value"] is not None and result["interval"] is not None:
                assert result["interval"]["low"] <= result["value"] <= result["interval"]["high"]


def test_all_or_nothing_rates_do_not_get_zero_width_interval(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    for query in ("Q001", "Q002", "Q003"):
        for _ in range(4):
            b.obs(query, mentioned=True)
    b.write()
    overall = find(run_metrics(b.root)["results"], "mention_rate")
    assert_ratio(overall, 12, 12)
    # 全部命中时按题重抽的区间退化为零宽；改用以题数（3）为 n 的 Wilson 区间
    low, high = wilson_interval(3, 3, 0.95)
    assert overall["interval"] == {"method": "wilson", "level": 0.95, "low": low, "high": high}
    assert "退化" in overall["components"]["interval_note"]
