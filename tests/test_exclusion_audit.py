"""剔除审计测试：数量与规则暴露、未登记规则、选择性剔除的识别。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli_json
from helpers_metrics import Builder, find, metrics_file

from geo_verify.metrics import hypergeometric_tail


def run_metrics(root: Path) -> dict[str, Any]:
    code, data = run_cli_json("metrics", str(root))
    assert code == 0, data
    return metrics_file(root)


def build_half_mentioned(root: Path, **kwargs: Any) -> Builder:
    """40 条有效观测，4 道题各 10 条：每题前 5 条提及、后 5 条未提及（共 20 提及、20 未提及）。"""
    b = Builder(root, queries=("Q001", "Q002", "Q003", "Q004"), **kwargs)
    for query in ("Q001", "Q002", "Q003", "Q004"):
        for index in range(10):
            b.obs(query, window=f"W{index % 3 + 1}", mentioned=index < 5)
    b.write()
    return b


def not_mentioned(b: Builder, count: int) -> list[dict[str, Any]]:
    mentioned = {a["observation_id"]: a["mentioned"] for a in b.annotations}
    return [o for o in b.observations if not mentioned[o["observation_id"]]][:count]


def test_hypergeometric_tail_hand_values() -> None:
    # 10 个样本中 5 个命中，取 5 个全不中：C(5,0)·C(5,5)/C(10,5) = 1/252
    assert hypergeometric_tail(10, 5, 5, 0, lower=True) == pytest.approx(1 / 252, rel=1e-9)
    assert hypergeometric_tail(10, 5, 5, 5, lower=False) == pytest.approx(1 / 252, rel=1e-9)
    # 4 个样本中 2 个命中，取 2 个：P(X ≤ 1) = 1 − 1/6
    assert hypergeometric_tail(4, 2, 2, 1, lower=True) == pytest.approx(5 / 6, rel=1e-9)
    assert hypergeometric_tail(4, 2, 2, 0, lower=False) == pytest.approx(1.0, rel=1e-9)
    # 取出的数量超过未命中总数时，命中数有下限：6 个里 5 个命中，取 3 个至少命中 2 个
    assert hypergeometric_tail(6, 5, 3, 1, lower=True) == 0.0
    assert hypergeometric_tail(6, 5, 3, 2, lower=True) == pytest.approx(0.5, rel=1e-9)


def test_no_exclusions(tmp_path: Path) -> None:
    b = build_half_mentioned(tmp_path / "proj")
    audit = run_metrics(b.root)["exclusion_audit"]
    assert audit["rules"] == []
    assert audit["unregistered_rules"] == []
    assert audit["excluded_observations"] == 0
    assert audit["comparisons"] == []
    assert audit["selective"] is False
    assert audit["registered_rules"] == ["EX-DUP"]
    assert audit["thresholds"] == {"alpha": 0.05, "min_excluded": 5, "min_difference": 0.1}


def test_selective_exclusion_is_exposed(tmp_path: Path) -> None:
    b = build_half_mentioned(tmp_path / "proj")
    b.exclude(not_mentioned(b, 8), "EX-DUP")
    data = run_metrics(b.root)
    overall = find(data["results"], "mention_rate")
    # 剔除 8 条未提及的观测后：20/32；剔除的数量和规则必须如实列出
    assert (overall["numerator"], overall["denominator"]) == (20, 32)
    assert overall["excluded"] == {"count": 8, "rules": [{"rule": "audit:EX-DUP", "count": 8}]}
    audit = data["exclusion_audit"]
    assert audit["rules"] == [
        {
            "rule_id": "EX-DUP",
            "registered": True,
            "listed_observations": 8,
            "excluded_observations": 8,
        }
    ]
    assert audit["excluded_observations"] == 8
    comparison = audit["comparisons"][0]
    assert comparison["metric_id"] == "mention_rate"
    assert comparison["direction"] == "higher_is_better"
    assert comparison["retained"] == {"numerator": 20, "denominator": 32, "value": 0.625}
    assert comparison["excluded"] == {"numerator": 0, "denominator": 8, "value": 0.0}
    assert comparison["value_if_included"] == 0.5
    # 单侧超几何检验：C(20,8)/C(40,8) = 125970/76904685
    assert comparison["p_value"] == pytest.approx(125970 / 76904685, rel=1e-9)
    assert comparison["selective"] is True
    assert audit["selective"] is True
    notice = next(n for n in data["notices"] if n["code"] == "selective_exclusion")
    assert notice["severity"] == "error"
    assert notice["details"]["metrics"] == ["mention_rate"]


def test_balanced_exclusion_is_not_selective(tmp_path: Path) -> None:
    b = build_half_mentioned(tmp_path / "proj")
    # 每题剔除 1 条提及、1 条未提及：被剔除样本的提及率与保留样本相同
    chosen = [b.observations[q * 10 + offset] for q in range(4) for offset in (0, 9)]
    b.exclude(chosen, "EX-DUP")
    data = run_metrics(b.root)
    comparison = data["exclusion_audit"]["comparisons"][0]
    assert comparison["retained"] == {"numerator": 16, "denominator": 32, "value": 0.5}
    assert comparison["excluded"] == {"numerator": 4, "denominator": 8, "value": 0.5}
    assert comparison["selective"] is False
    assert data["exclusion_audit"]["selective"] is False
    assert not any(n["code"] == "selective_exclusion" for n in data["notices"])


def test_few_exclusions_are_not_flagged(tmp_path: Path) -> None:
    b = build_half_mentioned(tmp_path / "proj")
    b.exclude(not_mentioned(b, 4), "EX-DUP")
    comparison = run_metrics(b.root)["exclusion_audit"]["comparisons"][0]
    # 只剔除 4 条（少于 5 条）：即使全部是不利样本也不足以认定为选择性剔除，但数字照样列出
    assert comparison["excluded"] == {"numerator": 0, "denominator": 4, "value": 0.0}
    assert comparison["selective"] is False


def test_unregistered_rule_is_exposed(tmp_path: Path) -> None:
    b = build_half_mentioned(tmp_path / "proj")
    b.exclude([b.observations[0]], "EX-MADE-UP")
    b.exclude([b.observations[1]], "EX-DUP")
    data = run_metrics(b.root)
    audit = data["exclusion_audit"]
    assert audit["unregistered_rules"] == ["EX-MADE-UP"]
    assert [(r["rule_id"], r["registered"]) for r in audit["rules"]] == [
        ("EX-DUP", True),
        ("EX-MADE-UP", False),
    ]
    notice = next(n for n in data["notices"] if n["code"] == "unregistered_exclusion_rules")
    assert notice["severity"] == "error"
    assert notice["details"]["rules"] == ["EX-MADE-UP"]


def test_excluded_observations_without_annotation_cannot_be_compared(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    hidden = [b.obs("Q001", mentioned=None) for _ in range(6)]
    for _ in range(6):
        b.obs("Q002", mentioned=True)
    b.write()
    b.exclude(hidden, "EX-DUP")
    comparison = run_metrics(b.root)["exclusion_audit"]["comparisons"][0]
    assert comparison["excluded"] == {"numerator": 0, "denominator": 0, "value": None}
    assert comparison["p_value"] is None
    assert comparison["selective"] is False
    assert "无法判断" in comparison["note"]


def test_listed_ids_that_do_not_exist_are_visible(tmp_path: Path) -> None:
    b = build_half_mentioned(tmp_path / "proj")
    ghost = dict(b.observations[0])
    ghost["observation_id"] = "obs-ghost"
    b.exclude([ghost, b.observations[1]], "EX-DUP")
    rule = run_metrics(b.root)["exclusion_audit"]["rules"][0]
    assert rule["listed_observations"] == 2
    assert rule["excluded_observations"] == 1


def test_lower_is_better_metric_uses_opposite_direction(tmp_path: Path) -> None:
    metrics = [
        {
            "metric_id": "negative_miscitation_rate",
            "definition_version": "1.0",
            "role": "acceptance",
            "threshold": {"op": "<=", "value": 0.1},
        }
    ]
    b = Builder(tmp_path / "proj", prereg={"metrics": metrics})
    negatives = [
        b.obs("Q001", ann={"risk": {"unfounded_negative": True}, "sentiment": "negative"})
        for _ in range(10)
    ]
    for index in range(30):
        b.obs(f"Q00{index % 3 + 1}")
    b.write()
    b.exclude(negatives[:8], "EX-DUP")
    data = run_metrics(b.root)
    comparisons = {c["metric_id"]: c for c in data["exclusion_audit"]["comparisons"]}
    negative = comparisons["negative_miscitation_rate"]
    assert negative["direction"] == "lower_is_better"
    # 被剔除的 8 条全是负面误引；保留样本 32 条中只有 2 条
    assert negative["excluded"] == {"numerator": 8, "denominator": 8, "value": 1.0}
    assert negative["retained"] == {"numerator": 2, "denominator": 32, "value": 0.0625}
    assert negative["value_if_included"] == 0.25
    assert negative["selective"] is True
    # 提及率上，被剔除样本并不更差（全部提及），不构成选择性剔除
    assert comparisons["mention_rate"]["selective"] is False
    assert data["exclusion_audit"]["selective"] is True
