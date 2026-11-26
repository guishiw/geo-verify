"""`geo-verify crosscheck` 测试：GOAL 5.4 偏离度黄金值、样本不足不可判定、合并行与平台行、
预先登记与命令行覆盖、区间配对规则、退出码与可复现性。

期望值来自 GOAL.md 5.4 的黄金值或注释中的手工推算，不取自程序输出。
"""

from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from factory import make_citation, read
from helpers_analytics import QUERIES, add_series, per_query
from helpers_metrics import Builder, copy_project
from hypothesis import given
from hypothesis import strategies as st

from geo_verify.crosscheck import (
    FORMULA_NOTE,
    FORMULA_TEXT,
    deviation_from_counts,
    passes_threshold,
    symmetric_relative_difference,
)
from geo_verify.stats import (
    paired_cluster_bootstrap_difference,
    unpaired_cluster_bootstrap_difference,
)

SEED = 20261001  # 样例项目预先登记的 seed
GOLDEN_TOLERANCE = 0.0001  # GOAL 5.4 的容差


def crosscheck(root: Path, *extra: str) -> tuple[int, dict[str, Any]]:
    code, data = run_cli_json("crosscheck", str(root), *extra)
    assert "error" not in data, data
    document: dict[str, Any] = read(root / "out" / "crosscheck.json")
    return code, document


def row(document: dict[str, Any], platform: str | None) -> dict[str, Any]:
    matches = [item for item in document["results"] if item["platform"] == platform]
    assert len(matches) == 1
    return matches[0]


def two_channels(
    root: Path, api: tuple[int, int], manual: tuple[int, int], **kwargs: Any
) -> Builder:
    """engine-a 上 api 与 manual 两个通道，各自“命中数 / 观测数”，3 个窗口、10 道题。"""
    b = Builder(root, queries=QUERIES)
    add_series(b, "engine-a", "api", *api, **kwargs)
    add_series(b, "engine-a", "manual", *manual, **kwargs)
    b.write()
    return b


# ---------------------------------------------------------------- 公式与黄金值


def test_deviation_formula_golden_values() -> None:
    # GOAL 5.4：60% 与 45% → 0.2857；10% 与 0% → 2.0；0% 与 0% → 0
    assert symmetric_relative_difference(0.60, 0.45) == pytest.approx(0.2857, abs=GOLDEN_TOLERANCE)
    assert symmetric_relative_difference(0.10, 0.0) == pytest.approx(2.0, abs=GOLDEN_TOLERANCE)
    assert symmetric_relative_difference(0.0, 0.0) == 0.0
    # 计数版本：18/30 与 27/60 → 2·|18·60 − 27·30| / (18·60 + 27·30) = 540 / 1890
    assert deviation_from_counts(18, 30, 27, 60) == Fraction(540, 1890)
    assert float(deviation_from_counts(18, 30, 27, 60)) == pytest.approx(0.2857, abs=1e-4)
    assert deviation_from_counts(3, 30, 0, 30) == 2
    assert deviation_from_counts(0, 30, 0, 45) == 0


def test_threshold_is_inclusive_and_exact() -> None:
    # 23/60 与 17/60：2·6/40 = 0.3 恰好等于阈值，按“≤ 阈值为通过”应通过；浮点计算会在边界上抖动
    exact = deviation_from_counts(23, 60, 17, 60)
    assert exact == Fraction(3, 10)
    assert passes_threshold(exact, 0.3)
    assert not passes_threshold(exact + Fraction(1, 10**9), 0.3)
    assert passes_threshold(Fraction(0), 0.3)


def test_formula_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="不能为负"):
        symmetric_relative_difference(-0.1, 0.2)
    with pytest.raises(ValueError, match="有限"):
        symmetric_relative_difference(math.nan, 0.2)
    with pytest.raises(ValueError, match="分母"):
        deviation_from_counts(1, 0, 1, 2)
    with pytest.raises(ValueError, match="分子"):
        deviation_from_counts(3, 2, 1, 2)


@given(
    st.floats(min_value=0, max_value=1, allow_nan=False),
    st.floats(min_value=0, max_value=1, allow_nan=False),
)
def test_deviation_is_symmetric_and_bounded(a: float, b: float) -> None:
    value = symmetric_relative_difference(a, b)
    assert value == symmetric_relative_difference(b, a)
    assert 0.0 <= value <= 2.0
    assert symmetric_relative_difference(a, a) == 0.0


@given(
    st.integers(min_value=1, max_value=500).flatmap(
        lambda den_a: st.tuples(st.integers(0, den_a), st.just(den_a))
    ),
    st.integers(min_value=1, max_value=500).flatmap(
        lambda den_b: st.tuples(st.integers(0, den_b), st.just(den_b))
    ),
)
def test_counts_version_matches_float_formula(a: tuple[int, int], b: tuple[int, int]) -> None:
    exact = deviation_from_counts(*a, *b)
    assert exact == deviation_from_counts(*b, *a)
    assert 0 <= exact <= 2
    assert float(exact) == pytest.approx(
        symmetric_relative_difference(a[0] / a[1], b[0] / b[1]), abs=1e-12
    )
    if Fraction(*a) == Fraction(*b):
        assert exact == 0


# ---------------------------------------------------------------- 黄金值：命令行端到端


def test_golden_60_vs_45_passes(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(36, 60), manual=(27, 60))
    code, document = crosscheck(b.root)
    assert code == 0
    assert document["status"] == "pass"
    assert document["status_label"] == "通过"
    combined = row(document, None)
    assert combined["a"]["channel"] == "api"
    assert (combined["a"]["numerator"], combined["a"]["denominator"]) == (36, 60)
    assert (combined["b"]["numerator"], combined["b"]["denominator"]) == (27, 60)
    assert combined["a"]["value"] == pytest.approx(0.60, abs=1e-12)
    assert combined["b"]["value"] == pytest.approx(0.45, abs=1e-12)
    assert combined["deviation"] == pytest.approx(0.2857, abs=GOLDEN_TOLERANCE)
    assert combined["difference"] == pytest.approx(0.15, abs=1e-12)
    assert combined["status"] == "pass"
    assert combined["a"]["sufficient"]
    assert combined["b"]["sufficient"]
    assert row(document, "engine-a")["deviation"] == pytest.approx(0.2857, abs=GOLDEN_TOLERANCE)


def test_golden_10_vs_0_fails(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(6, 60), manual=(0, 60))
    code, document = crosscheck(b.root)
    assert code == 1
    assert document["status"] == "fail"
    assert document["status_label"] == "不通过"
    combined = row(document, None)
    assert combined["deviation"] == pytest.approx(2.0, abs=GOLDEN_TOLERANCE)
    assert combined["difference"] == pytest.approx(0.10, abs=1e-12)
    assert combined["status"] == "fail"
    assert "超过阈值" in combined["reason"]


def test_golden_0_vs_0_passes_when_sufficient(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(0, 60), manual=(0, 60))
    code, document = crosscheck(b.root)
    assert code == 0
    combined = row(document, None)
    assert combined["deviation"] == 0
    assert combined["difference"] == 0
    assert combined["status"] == "pass"
    assert document["status"] == "pass"


@pytest.mark.parametrize(
    ("total", "windows", "why"),
    [
        (20, ("W1", "W2", "W3"), "有效观测不足 30 次"),  # 每个通道只有 20 次有效观测
        (60, ("W1", "W2"), "时间窗口不足 3 个"),  # 观测够了，但只有 2 个窗口
    ],
)
def test_golden_0_vs_0_is_undetermined_when_insufficient(
    tmp_path: Path, total: int, windows: tuple[str, ...], why: str
) -> None:
    b = two_channels(tmp_path / "proj", api=(0, total), manual=(0, total), windows=windows)
    code, document = crosscheck(b.root)
    assert code == 3
    assert document["status"] == "undetermined"
    assert document["status_label"] == "不可判定"
    combined = row(document, None)
    assert combined["deviation"] == 0  # 偏离度照样给出，但不得据此判通过
    assert combined["status"] == "undetermined"
    assert combined["status_label"] == "不可判定"
    assert not combined["a"]["sufficient"]
    assert any(why in item for item in combined["a"]["sufficiency_reasons"])
    assert why in combined["reason"]


def test_api_and_web_differ_strongly_fails(tmp_path: Path) -> None:
    # S05：API 通道提及率 75%，网页（人工采集）通道 30%：偏离度 2·0.45/1.05 ≈ 0.857
    b = two_channels(tmp_path / "proj", api=(45, 60), manual=(18, 60))
    code, document = crosscheck(b.root)
    assert code == 1
    assert row(document, None)["deviation"] == pytest.approx(0.9 / 1.05, abs=1e-9)


def test_boundary_deviation_equal_to_threshold_passes(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(23, 60), manual=(17, 60))
    code, document = crosscheck(b.root)
    assert code == 0
    assert row(document, None)["deviation"] == pytest.approx(0.3, abs=1e-12)
    assert row(document, None)["status"] == "pass"


# ---------------------------------------------------------------- 文件结构与公式说明


def test_document_structure_follows_outputs_md(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(36, 60), manual=(27, 60))
    _, document = crosscheck(b.root)
    assert document["schema_version"] == "1.0"
    assert document["kind"] == "crosscheck"
    assert document["project_id"] == "auto-demo"
    assert document["synthetic"] is True
    assert document["prompt_set"] == {"version": 1, "content_hash": b.sealed["content_hash"]}
    assert document["formula"] == "symmetric_relative_difference"
    assert document["formula_text"] == FORMULA_TEXT
    assert "|A − B| / ((A + B) / 2)" in document["formula_text"]
    assert "A = B = 0 时为 0" in document["formula_text"]
    assert document["formula_note"] == FORMULA_NOTE
    assert "本项目的定义" in document["formula_note"]
    assert "30%" in document["formula_note"]
    assert document["threshold"] == 0.3
    assert document["preregistered"] is True
    assert (document["metric_id"], document["channel_a"], document["channel_b"]) == (
        "mention_rate",
        "api",
        "manual",
    )
    assert document["parameters"] == {"seed": SEED, "iterations": 1000, "confidence_level": 0.95}
    assert document["reason"]
    # 合并行 + 每个登记的平台一行（engine-b 没有数据）
    assert [item["platform"] for item in document["results"]] == [None, "engine-a", "engine-b"]
    for item in document["results"]:
        assert set(item) >= {
            "platform",
            "a",
            "b",
            "deviation",
            "difference",
            "interval",
            "paired_queries",
            "status",
            "status_label",
            "reason",
        }
        for side in (item["a"], item["b"]):
            assert set(side) >= {
                "channel",
                "value",
                "numerator",
                "denominator",
                "windows",
                "sufficient",
            }
    combined = row(document, None)
    assert combined["a"]["windows"] == 3
    assert combined["interval"]["method"] == "paired_cluster_bootstrap"
    assert combined["interval"]["level"] == 0.95
    assert combined["paired_queries"] == 10


def test_text_output_states_formula_is_project_definition(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(36, 60), manual=(27, 60))
    code, out, _ = run_cli("crosscheck", str(b.root))
    assert code == 0
    assert "本项目的定义" in out
    assert "通过" in out
    assert "0.2857" in out


def test_json_output_matches_file(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(36, 60), manual=(27, 60))
    code, data = run_cli_json("crosscheck", str(b.root))
    assert code == 0
    document = read(b.root / "out" / "crosscheck.json")
    assert data["output"].endswith("crosscheck.json")
    assert {key: value for key, value in data.items() if key != "output"} == document


def test_out_option_and_reproducible_bytes(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "one", api=(36, 60), manual=(27, 60))
    copy_project(b.root, tmp_path / "two")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert run_cli("crosscheck", str(b.root), "--out", str(first))[0] == 0
    assert run_cli("crosscheck", str(tmp_path / "two"), "--out", str(second))[0] == 0
    assert first.read_bytes() == second.read_bytes()
    assert str(tmp_path) not in first.read_text(encoding="utf-8")
    assert not (b.root / "out" / "crosscheck.json").exists()


# ---------------------------------------------------------------- 缺数据、配对规则


def test_channel_without_data_is_undetermined(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj", queries=QUERIES)
    add_series(b, "engine-a", "api", 36, 60)
    b.write()
    code, document = crosscheck(b.root)
    assert code == 3
    assert document["status"] == "undetermined"
    platform_row = row(document, "engine-a")
    assert platform_row["deviation"] is None
    assert platform_row["difference"] is None
    assert platform_row["interval"] is None
    assert platform_row["b"]["value"] is None
    assert platform_row["b"]["denominator"] == 0
    assert platform_row["b"]["null_reason"]
    assert "manual" in platform_row["reason"]
    combined = row(document, None)
    # 没有任何平台同时有两个通道的数据：合并行无数据、不可判定
    assert combined["merged_platforms"] == []
    assert combined["a"]["denominator"] == 0
    assert combined["status"] == "undetermined"
    assert "两个通道都有数据的平台" in combined["reason"]


def test_identical_queries_use_paired_bootstrap(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(36, 60), manual=(27, 60))
    _, document = crosscheck(b.root, "--iterations", "400")
    combined = row(document, None)
    # 手工按布局数出每题的（命中, 次数）；差值方向为 A − B（api − manual）
    api, manual = per_query(36, 60), per_query(27, 60)
    expected = paired_cluster_bootstrap_difference(
        [manual[q][0] for q in QUERIES],
        [manual[q][1] for q in QUERIES],
        [api[q][0] for q in QUERIES],
        [api[q][1] for q in QUERIES],
        seed=SEED,
        iterations=400,
        level=0.95,
    )
    assert combined["interval"]["method"] == "paired_cluster_bootstrap"
    assert (combined["interval"]["low"], combined["interval"]["high"]) == expected
    assert combined["interval"]["low"] <= combined["difference"] <= combined["interval"]["high"]
    assert combined["paired_queries"] == 10
    assert document["parameters"]["iterations"] == 400


def test_different_queries_are_not_paired(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj", queries=QUERIES)
    add_series(b, "engine-a", "api", 36, 60)
    add_series(b, "engine-a", "manual", 24, 48, queries=QUERIES[:8])  # 网页通道只问了 8 道题
    b.write()
    code, document = crosscheck(b.root, "--iterations", "300")
    combined = row(document, None)
    assert code == 0  # 偏离度 2·0.1/1.1 ≈ 0.18，样本充分
    assert combined["paired_queries"] == 8
    assert combined["interval"]["method"] == "unpaired_cluster_bootstrap"
    assert "无法同题配对" in combined["interval_note"]
    api, manual = per_query(36, 60), per_query(24, 48, queries=QUERIES[:8])
    expected = unpaired_cluster_bootstrap_difference(
        [manual[q][0] for q in sorted(manual)],
        [manual[q][1] for q in sorted(manual)],
        [api[q][0] for q in sorted(api)],
        [api[q][1] for q in sorted(api)],
        seed=SEED,
        iterations=300,
        level=0.95,
    )
    assert (combined["interval"]["low"], combined["interval"]["high"]) == expected
    # 各通道各自的区间照常给出
    assert combined["a"]["interval"]["method"] == "cluster_bootstrap"
    assert combined["b"]["interval"]["method"] == "cluster_bootstrap"


def test_single_query_gives_no_difference_interval(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj", queries=("Q001",))
    add_series(b, "engine-a", "api", 18, 30, queries=("Q001",))
    add_series(b, "engine-a", "manual", 15, 30, queries=("Q001",))
    b.write()
    code, document = crosscheck(b.root)
    combined = row(document, None)
    assert code == 0  # 偏离度 2·0.1/1.1 ≈ 0.18
    assert combined["interval"] is None
    assert "不足 2 道" in combined["interval_note"]
    assert combined["a"]["interval"]["method"] == "wilson"  # 单题范围用 Wilson


def test_unpaired_side_with_single_query_gives_no_difference_interval(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj", queries=QUERIES)
    add_series(b, "engine-a", "api", 36, 60)
    add_series(b, "engine-a", "manual", 15, 30, queries=("Q001",))  # 网页通道只问了 1 道题
    b.write()
    code, document = crosscheck(b.root)
    combined = row(document, None)
    assert code == 0  # 0.6 与 0.5：偏离度 ≈ 0.18
    assert combined["paired_queries"] == 1
    assert combined["interval"] is None
    assert "无法同题配对" in combined["interval_note"]
    assert "不足 2 道题" in combined["interval_note"]


# ---------------------------------------------------------------- 合并行、平台行与整体结论


def test_combined_row_only_merges_platforms_with_both_channels(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj", queries=QUERIES)
    add_series(b, "engine-a", "api", 36, 60)
    add_series(b, "engine-a", "manual", 30, 60)
    add_series(b, "engine-b", "api", 6, 60)  # engine-b 只有 api：不进合并行
    b.write()
    code, document = crosscheck(b.root)
    combined = row(document, None)
    assert combined["merged_platforms"] == ["engine-a"]
    assert (combined["a"]["numerator"], combined["a"]["denominator"]) == (36, 60)
    assert combined["deviation"] == pytest.approx(2 * 0.1 / 1.1, abs=1e-12)
    assert row(document, "engine-b")["status"] == "undetermined"
    assert code == 0  # 合并行通过；engine-b 行不可判定但不是“样本充分的不通过”


def test_sufficient_platform_failure_fails_overall(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj", queries=QUERIES)
    # engine-a：30/60 对 30/60（偏离度 0）；engine-b：36/60 对 24/60（偏离度 0.4）
    # 合并：66/120 对 54/120 → 偏离度 2·0.1/1.0 = 0.2，合并行通过
    add_series(b, "engine-a", "api", 30, 60)
    add_series(b, "engine-a", "manual", 30, 60)
    add_series(b, "engine-b", "api", 36, 60)
    add_series(b, "engine-b", "manual", 24, 60)
    b.write()
    code, document = crosscheck(b.root)
    combined = row(document, None)
    assert combined["merged_platforms"] == ["engine-a", "engine-b"]
    assert combined["deviation"] == pytest.approx(0.2, abs=1e-12)
    assert combined["status"] == "pass"
    assert row(document, "engine-a")["status"] == "pass"
    assert row(document, "engine-b")["deviation"] == pytest.approx(0.4, abs=1e-12)
    assert row(document, "engine-b")["status"] == "fail"
    assert document["status"] == "fail"
    assert code == 1
    assert "engine-b" in document["reason"]


def test_insufficient_platform_row_does_not_fail_overall(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj", queries=QUERIES)
    add_series(b, "engine-a", "api", 30, 60)
    add_series(b, "engine-a", "manual", 30, 60)
    add_series(b, "engine-b", "api", 9, 10)  # engine-b 只有 10 次：偏离度很大但样本不足
    add_series(b, "engine-b", "manual", 1, 10)
    b.write()
    code, document = crosscheck(b.root)
    platform_row = row(document, "engine-b")
    assert platform_row["deviation"] == pytest.approx(1.6, abs=1e-12)
    assert platform_row["status"] == "undetermined"
    # 合并：39/70 对 31/70 → 2·8/70 / (70/70) ≈ 0.2286，通过
    assert row(document, None)["deviation"] == pytest.approx(16 / 70, abs=1e-12)
    assert document["status"] == "pass"
    assert code == 0


def test_platform_option_only_computes_that_platform(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj", queries=QUERIES)
    add_series(b, "engine-a", "api", 30, 60)
    add_series(b, "engine-a", "manual", 30, 60)
    add_series(b, "engine-b", "api", 36, 60)
    add_series(b, "engine-b", "manual", 24, 60)
    b.write()
    code, document = crosscheck(b.root, "--platform", "engine-a")
    assert code == 0
    assert document["platform"] == "engine-a"
    assert [item["platform"] for item in document["results"]] == ["engine-a"]
    code, document = crosscheck(b.root, "--platform", "engine-b")
    assert code == 1
    assert document["status"] == "fail"


# ---------------------------------------------------------------- 预先登记与覆盖


def test_threshold_override_is_not_preregistered(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(36, 60), manual=(27, 60))
    code, document = crosscheck(b.root, "--threshold", "0.25")
    assert code == 1  # 0.2857 > 0.25
    assert document["threshold"] == 0.25
    assert document["preregistered"] is False
    assert document["overridden"] == ["threshold"]
    assert document["registered"]["threshold"] == 0.3
    # 与登记值相同的参数不算覆盖
    code, document = crosscheck(b.root, "--threshold", "0.3", "--metric", "mention_rate")
    assert code == 0
    assert document["preregistered"] is True
    assert document["overridden"] == []


def test_channel_and_metric_overrides(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(36, 60), manual=(27, 60))
    code, document = crosscheck(b.root, "--channel-a", "manual", "--channel-b", "api")
    assert code == 0
    assert document["overridden"] == ["channel_a", "channel_b"]
    assert document["preregistered"] is False
    combined = row(document, None)
    assert combined["a"]["channel"] == "manual"
    assert combined["difference"] == pytest.approx(-0.15, abs=1e-12)  # A − B 方向随之翻转
    assert combined["deviation"] == pytest.approx(0.2857, abs=GOLDEN_TOLERANCE)  # 偏离度对称
    code, document = crosscheck(b.root, "--metric", "valid_observation_rate")
    assert document["overridden"] == ["metric_id"]
    assert document["metric_id"] == "valid_observation_rate"
    assert row(document, None)["deviation"] == 0  # 两通道全部有效：1 对 1


def test_registered_metric_alias_is_resolved(tmp_path: Path) -> None:
    b = Builder(
        tmp_path / "proj",
        queries=QUERIES,
        prereg={
            "crosscheck": {
                "formula": "symmetric_relative_difference",
                "threshold": 0.5,
                "metric_id": "accuracy",
                "channel_a": "api",
                "channel_b": "manual",
            }
        },
    )
    for channel, labels in (("api", ("supported", "contradicted")), ("manual", ("supported",))):
        for observation in add_series(b, "engine-a", channel, 30, 30):
            for label in labels:
                b.judge(observation, label)
    b.write()
    code, document = crosscheck(b.root)
    # 登记的 accuracy 按 accuracy_basis = verifiable 展开：api 30/60 = 0.5，manual 30/30 = 1
    assert document["metric_id"] == "accuracy_verifiable"
    assert document["preregistered"] is True
    combined = row(document, None)
    assert (combined["a"]["numerator"], combined["a"]["denominator"]) == (30, 60)
    assert combined["deviation"] == pytest.approx(2 * 0.5 / 1.5, abs=1e-12)
    assert code == 1


def test_citation_level_metric_is_supported(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj", queries=QUERIES)
    for channel, own in (("api", 2), ("manual", 1)):
        for position, observation in enumerate(add_series(b, "engine-a", channel, 30, 60)):
            domain = "lanxu-auto.example" if position % 3 < own else "other-media.example"
            observation["citations"] = [make_citation(1, domain)]
    b.write()
    code, document = crosscheck(b.root, "--metric", "target_source_share")
    combined = row(document, None)
    # api：每 3 条引用里 2 条是目标信源 → 40/60；manual：20/60
    assert (combined["a"]["numerator"], combined["a"]["denominator"]) == (40, 60)
    assert (combined["b"]["numerator"], combined["b"]["denominator"]) == (20, 60)
    assert combined["deviation"] == pytest.approx(2 / 3, abs=1e-12)
    assert code == 1


def test_preregistration_changed_after_sealing(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(36, 60), manual=(27, 60))
    prereg = dict(b.project["preregistration"])
    prereg["crosscheck"] = {**prereg["crosscheck"], "threshold": 0.5}  # 封存后放宽阈值
    b.update_project(preregistration=prereg)
    code, document = crosscheck(b.root)
    assert code == 0
    assert document["threshold"] == 0.5
    assert document["preregistered"] is False
    assert document["overridden"] == []
    notice = next(item for item in document["notices"] if item["code"] == "preregistration_changed")
    assert notice["severity"] == "error"
    assert "封存后" in notice["message"]


def test_parameter_override_is_noticed_but_keeps_preregistration(tmp_path: Path) -> None:
    b = two_channels(tmp_path / "proj", api=(36, 60), manual=(27, 60))
    _, document = crosscheck(b.root, "--seed", "7", "--iterations", "200")
    assert document["preregistered"] is True
    assert document["parameters"] == {"seed": 7, "iterations": 200, "confidence_level": 0.95}
    codes = [item["code"] for item in document["notices"]]
    assert codes == ["parameters_overridden"]


# ---------------------------------------------------------------- 输入错误


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--channel-a", "manual"), "不能相同"),
        (("--channel-a", "web"), "未知通道"),
        (("--metric", "no_such_metric"), "未知指标"),
        (("--metric", "temporal_std"), "不支持"),
        (("--metric", "recommendation_rate_daily"), "不支持"),
        (("--threshold", "0"), "阈值"),
        (("--threshold", "2.5"), "阈值"),
        (("--threshold", "nan"), "阈值"),
        (("--platform", "engine-z"), "未知平台"),
        (("--iterations", "0"), "iterations"),
    ],
)
def test_input_errors_exit_2(tmp_path: Path, args: tuple[str, ...], message: str) -> None:
    b = two_channels(tmp_path / "proj", api=(36, 60), manual=(27, 60))
    code, data = run_cli_json("crosscheck", str(b.root), *args)
    assert code == 2
    assert data["error"] == "input_error"
    assert message in data["message"]
    assert not (b.root / "out" / "crosscheck.json").exists()
