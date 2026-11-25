"""统计模块测试：Wilson 区间、聚类 bootstrap、ICC 与设计效应、配对比较、样本量、样本充分性。

期望值来源：GOAL.md 5.4 的黄金值（原样照抄，容差 ±0.0001）、手工推算（写在各测试的注释里）、
以及与被测代码不共用计算逻辑的模拟数据。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from geo_verify.errors import InputError
from geo_verify.stats import (
    CONCLUSION_DOWN,
    CONCLUSION_UNDETERMINED,
    CONCLUSION_UP,
    anova_icc,
    assess_sufficiency,
    cluster_bootstrap_mean,
    cluster_bootstrap_ratio,
    conclude,
    design_effect,
    effective_sample_size,
    model_version_changes,
    paired_cluster_bootstrap_difference,
    paired_comparison,
    summarize_clusters,
    two_proportion_sample_size,
    unpaired_cluster_bootstrap_difference,
    wilson_interval,
    zero_failure_sample_size,
)

TOLERANCE = 0.0001

# ---------------------------------------------------------------- 5.4 黄金值（不得改动）


@pytest.mark.parametrize(
    ("successes", "n", "low", "high"),
    [
        (0, 5, 0.0, 0.4345),
        (4, 5, 0.3755, 0.9638),
        (5, 5, 0.5655, 1.0),
        (24, 30, 0.6269, 0.9049),
        (120, 150, 0.7289, 0.8562),
    ],
)
def test_wilson_golden_values(successes: int, n: int, low: float, high: float) -> None:
    got_low, got_high = wilson_interval(successes, n, 0.95)
    assert abs(got_low - low) <= TOLERANCE
    assert abs(got_high - high) <= TOLERANCE


def test_wilson_bounds_are_exact_at_extremes() -> None:
    assert wilson_interval(0, 5)[0] == 0.0
    assert wilson_interval(5, 5)[1] == 1.0


@pytest.mark.parametrize(
    ("p1", "p2", "expected"),
    [(0.60, 0.75, 150), (0.60, 0.65, 1468), (0.30, 0.40, 354)],
)
def test_two_proportion_sample_size_golden(p1: float, p2: float, expected: int) -> None:
    assert two_proportion_sample_size(p1, p2, alpha=0.05, power=0.8) == expected
    # 两组对调不改变每组样本量
    assert two_proportion_sample_size(p2, p1) == expected


def test_zero_failure_sample_size_golden() -> None:
    assert zero_failure_sample_size(0.98, confidence=0.95) == 149


def test_design_effect_golden() -> None:
    deff = design_effect(cluster_size=10, icc=0.5)
    assert abs(deff - 5.5) <= TOLERANCE
    effective = effective_sample_size(300, deff)
    assert abs(effective - 300 / 5.5) <= TOLERANCE
    assert abs(effective - 54.5) < 0.05


# ---------------------------------------------------------------- Wilson


def test_wilson_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="n 必须为正"):
        wilson_interval(0, 0)
    with pytest.raises(ValueError, match="successes"):
        wilson_interval(6, 5)
    with pytest.raises(ValueError, match="successes"):
        wilson_interval(-1, 5)
    with pytest.raises(ValueError, match="置信水平"):
        wilson_interval(1, 5, 1.0)


def test_wilson_other_level_matches_hand_calculation() -> None:
    # 90% 水平：z = 1.6448536；x=8, n=10, p=0.8
    # 中心 = (0.8 + z²/20) / (1 + z²/10) = 0.935277/1.270554 = 0.736117
    # 半宽 = z·sqrt(0.016 + z²/400) / 1.270554 = 1.6448536·0.150880/1.270554 = 0.195329
    low, high = wilson_interval(8, 10, 0.90)
    assert abs(low - 0.5408) < 0.0005
    assert abs(high - 0.9314) < 0.0005


@given(n=st.integers(1, 400), data=st.data())
def test_wilson_properties(n: int, data: st.DataObject) -> None:
    x = data.draw(st.integers(0, n))
    low, high = wilson_interval(x, n)
    estimate = x / n
    assert 0.0 <= low <= estimate <= high <= 1.0
    # 对称性：x 次成功的区间与 n-x 次成功的区间互为镜像
    mirror_low, mirror_high = wilson_interval(n - x, n)
    assert math.isclose(low, 1 - mirror_high, abs_tol=1e-12)
    assert math.isclose(high, 1 - mirror_low, abs_tol=1e-12)
    # 单调性：成功数加一，上下界都不下降
    if x < n:
        next_low, next_high = wilson_interval(x + 1, n)
        assert next_low >= low
        assert next_high >= high


@given(n=st.integers(1, 200), factor=st.integers(2, 20), data=st.data())
def test_wilson_narrows_with_more_data(n: int, factor: int, data: st.DataObject) -> None:
    x = data.draw(st.integers(0, n))
    low, high = wilson_interval(x, n)
    big_low, big_high = wilson_interval(x * factor, n * factor)
    assert big_high - big_low < high - low


@given(n=st.integers(1, 200), data=st.data())
def test_wilson_higher_level_is_wider(n: int, data: st.DataObject) -> None:
    x = data.draw(st.integers(0, n))
    low90, high90 = wilson_interval(x, n, 0.90)
    low99, high99 = wilson_interval(x, n, 0.99)
    assert low99 <= low90
    assert high99 >= high90


# ---------------------------------------------------------------- 聚类 bootstrap


def test_cluster_bootstrap_two_clusters_by_enumeration() -> None:
    # 两题：(0/1) 与 (1/1)。重抽 2 题只有三种结果：0、0.5、1，概率 1/4、1/2、1/4，
    # 所以 2.5% 分位是 0，97.5% 分位是 1。
    low, high = cluster_bootstrap_ratio([0, 1], [1, 1], seed=7, iterations=2000)
    assert (low, high) == (0.0, 1.0)


def test_cluster_bootstrap_identical_clusters_gives_point_interval() -> None:
    low, high = cluster_bootstrap_ratio([3, 3, 3, 3], [5, 5, 5, 5], seed=1, iterations=500)
    assert low == high == 0.6


def test_cluster_bootstrap_resamples_clusters_not_observations() -> None:
    # 40 题、每题 50 次，一半题全中、一半题全不中：单次提问层面 n=2000，
    # 但信息量只有 40 题。按题重抽的区间半宽应接近 1.96·0.5/sqrt(40)=0.155，
    # 远大于把 2000 次当独立样本时的 0.022。
    numerators = [50] * 20 + [0] * 20
    denominators = [50] * 40
    low, high = cluster_bootstrap_ratio(numerators, denominators, seed=3, iterations=4000)
    half_width = (high - low) / 2
    assert 0.12 < half_width < 0.19
    naive_low, naive_high = wilson_interval(1000, 2000)
    assert half_width > 5 * (naive_high - naive_low) / 2


def test_cluster_bootstrap_matches_analytic_standard_error() -> None:
    # 等规模聚类时，比例的标准误 = 题均值的标准差 / sqrt(题数)。
    # 这里用与被测代码无关的方式（直接算题均值的标准差）得到期望半宽。
    rng = np.random.default_rng(99)
    sizes = [20] * 200
    successes = rng.binomial(20, rng.beta(4, 4, size=200)).tolist()
    means = [s / 20 for s in successes]
    grand = sum(means) / len(means)
    variance = sum((m - grand) ** 2 for m in means) / (len(means) - 1)
    expected_half = 1.959964 * math.sqrt(variance / len(means))
    low, high = cluster_bootstrap_ratio(successes, sizes, seed=5, iterations=4000)
    assert low < grand < high
    assert abs((high - low) / 2 - expected_half) / expected_half < 0.08


def test_cluster_bootstrap_is_deterministic_and_seed_sensitive() -> None:
    numerators = [1, 4, 2, 7, 0, 5, 3, 9]
    denominators = [10, 10, 5, 10, 3, 10, 10, 10]
    first = cluster_bootstrap_ratio(numerators, denominators, seed=11, iterations=1000)
    again = cluster_bootstrap_ratio(numerators, denominators, seed=11, iterations=1000)
    other = cluster_bootstrap_ratio(numerators, denominators, seed=12, iterations=1000)
    assert first == again
    assert repr(first) == repr(again)
    assert first != other


def test_cluster_bootstrap_ignores_empty_clusters_and_validates() -> None:
    with_empty = cluster_bootstrap_ratio([1, 0, 4, 2], [5, 0, 5, 5], seed=2, iterations=800)
    without = cluster_bootstrap_ratio([1, 4, 2], [5, 5, 5], seed=2, iterations=800)
    assert with_empty == without
    with pytest.raises(ValueError, match="长度"):
        cluster_bootstrap_ratio([1], [1, 2], seed=1)
    with pytest.raises(ValueError, match="没有分母大于 0 的聚类"):
        cluster_bootstrap_ratio([0, 0], [0, 0], seed=1)
    with pytest.raises(ValueError, match="iterations"):
        cluster_bootstrap_ratio([1], [2], seed=1, iterations=0)
    with pytest.raises(ValueError, match="负"):
        cluster_bootstrap_ratio([1, -1], [2, 2], seed=1)


def test_cluster_bootstrap_large_cluster_count_uses_chunks() -> None:
    # 题数很多时分块重抽：结果仍然确定，且区间落在点估计两侧。
    rng = np.random.default_rng(5)
    k = 6000
    denominators = [4] * k
    numerators = rng.binomial(4, 0.3, size=k).tolist()
    first = cluster_bootstrap_ratio(numerators, denominators, seed=8, iterations=1500)
    again = cluster_bootstrap_ratio(numerators, denominators, seed=8, iterations=1500)
    estimate = sum(numerators) / (4 * k)
    assert first == again
    assert first[0] < estimate < first[1]
    assert first[1] - first[0] < 0.03


ratio_clusters = st.lists(
    st.integers(1, 30).flatmap(lambda den: st.tuples(st.integers(0, den), st.just(den))),
    min_size=1,
    max_size=25,
)


@settings(max_examples=60, deadline=None)
@given(clusters=ratio_clusters, seed=st.integers(0, 2**32 - 1))
def test_cluster_bootstrap_properties(clusters: list[tuple[int, int]], seed: int) -> None:
    numerators = [item[0] for item in clusters]
    denominators = [item[1] for item in clusters]
    low, high = cluster_bootstrap_ratio(numerators, denominators, seed=seed, iterations=300)
    assert cluster_bootstrap_ratio(numerators, denominators, seed=seed, iterations=300) == (
        low,
        high,
    )
    ratios = [n / d for n, d in clusters]
    # 重抽后的汇总比例是各题比例的加权平均，不会超出各题比例的范围
    assert min(ratios) - 1e-12 <= low <= high <= max(ratios) + 1e-12


def test_cluster_bootstrap_mean() -> None:
    # 三题数值相同：均值恒为 0.2（0.2+0.2+0.2 的浮点和不恰为 0.6，故用容差比较）
    constant = cluster_bootstrap_mean([0.2, 0.2, 0.2], seed=1, iterations=300)
    assert math.isclose(constant[0], 0.2, abs_tol=1e-12)
    assert math.isclose(constant[1], 0.2, abs_tol=1e-12)
    # 两个值 -1 与 1：均值只可能是 -1、0、1，分位数落在两端
    assert cluster_bootstrap_mean([-1.0, 1.0], seed=4, iterations=2000) == (-1.0, 1.0)
    first = cluster_bootstrap_mean([0.1, -0.3, 0.25, 0.0, 0.4], seed=9, iterations=500)
    assert first == cluster_bootstrap_mean([0.1, -0.3, 0.25, 0.0, 0.4], seed=9, iterations=500)
    assert first[0] < 0.09 < first[1]
    with pytest.raises(ValueError, match="至少需要 1 个"):
        cluster_bootstrap_mean([], seed=1)


def test_paired_difference_bootstrap_uses_pairing() -> None:
    # 各题水平差异很大，但每题 B 都比 A 高 0.1：配对重抽时差值恒为 0.1。
    num_a = [1, 5, 9, 3]
    den = [10, 10, 10, 10]
    num_b = [2, 6, 10, 4]
    low, high = paired_cluster_bootstrap_difference(num_a, den, num_b, den, seed=1, iterations=500)
    assert math.isclose(low, 0.1, abs_tol=1e-12)
    assert math.isclose(high, 0.1, abs_tol=1e-12)
    # 不配对时题间差异进入区间，宽度明显更大
    wide_low, wide_high = unpaired_cluster_bootstrap_difference(
        num_a, den, num_b, den, seed=1, iterations=500
    )
    assert wide_low < 0 < wide_high
    assert wide_high - wide_low > 0.3


def test_paired_difference_bootstrap_validation_and_determinism() -> None:
    args = ([1, 2, 3], [4, 4, 4], [2, 2, 4], [4, 5, 4])
    first = paired_cluster_bootstrap_difference(*args, seed=3, iterations=400)
    assert first == paired_cluster_bootstrap_difference(*args, seed=3, iterations=400)
    assert first[0] <= first[1]
    with pytest.raises(ValueError, match="长度"):
        paired_cluster_bootstrap_difference([1], [1], [1, 2], [1, 2], seed=1)
    with pytest.raises(ValueError, match="分母"):
        paired_cluster_bootstrap_difference([1, 1], [1, 0], [1, 1], [1, 1], seed=1)
    unpaired = unpaired_cluster_bootstrap_difference(
        [1, 2], [4, 4], [3, 3, 1], [4, 4, 4], seed=3, iterations=400
    )
    assert unpaired == unpaired_cluster_bootstrap_difference(
        [1, 2], [4, 4], [3, 3, 1], [4, 4, 4], seed=3, iterations=400
    )


# ---------------------------------------------------------------- ICC 与设计效应


def test_anova_icc_hand_calculation_unequal_sizes() -> None:
    # 三题，次数 4、4、2，命中 4、0、1；N=10，k=3，总体比例 0.5。
    # MSB = [4·0.25 + 4·0.25 + 2·0] / 2 = 1
    # MSW = [4·0 + 0·1 + 1·0.5] / (10-3) = 1/14
    # n0  = (10 - 36/10) / 2 = 3.2
    # ICC = (1 - 1/14) / (1 + 2.2/14) = 65/81
    icc = anova_icc([4, 0, 1], [4, 4, 2])
    assert icc is not None
    assert math.isclose(icc, 65 / 81, rel_tol=1e-12)
    summary = summarize_clusters([4, 0, 1], [4, 4, 2])
    # 次数不等时 m = Σn²/N = 3.6；deff = 1 + 2.6·65/81
    assert summary.clusters == 3
    assert summary.n == 10
    assert summary.design_effect is not None
    assert summary.effective_n is not None
    assert math.isclose(summary.design_effect, 1 + 2.6 * 65 / 81, rel_tol=1e-12)
    assert math.isclose(summary.effective_n, 10 / (1 + 2.6 * 65 / 81), rel_tol=1e-12)


def test_anova_icc_extremes() -> None:
    # 题内完全一致、题间完全不同：ICC = 1
    assert anova_icc([2, 0], [2, 2]) == 1.0
    # 题间毫无差异：ANOVA 估计为负，截断为 0
    assert anova_icc([1, 1], [2, 2]) == 0.0
    # 无法估计的情形返回 None：只有一题、每题只有一次、全 0 或全 1
    assert anova_icc([3], [5]) is None
    assert anova_icc([1, 0, 1], [1, 1, 1]) is None
    assert anova_icc([5, 5], [5, 5]) is None
    assert anova_icc([0, 0], [5, 5]) is None
    summary = summarize_clusters([5, 5], [5, 5])
    assert (summary.icc, summary.design_effect, summary.effective_n) == (None, None, None)
    with pytest.raises(ValueError, match="长度"):
        anova_icc([1], [1, 2])


def test_anova_icc_skips_empty_clusters() -> None:
    assert anova_icc([4, 0, 0, 1], [4, 4, 0, 2]) == anova_icc([4, 0, 1], [4, 4, 2])
    assert summarize_clusters([4, 0, 0, 1], [4, 4, 0, 2]).clusters == 3


def test_anova_icc_recovers_simulated_value() -> None:
    # Beta(2, 2) 的题间差异对应 ICC = 1/(2+2+1) = 0.2；模拟数据不使用被测代码。
    rng = np.random.default_rng(123)
    probabilities = rng.beta(2, 2, size=10000)
    sizes = rng.integers(5, 16, size=10000)
    successes = rng.binomial(sizes, probabilities)
    icc = anova_icc(successes.tolist(), sizes.tolist())
    assert icc is not None
    assert abs(icc - 0.2) < 0.02


def test_design_effect_validation() -> None:
    assert design_effect(1, 0.9) == 1.0
    assert design_effect(10, 0.0) == 1.0
    with pytest.raises(InputError, match="每题次数"):
        design_effect(0.5, 0.2)
    with pytest.raises(InputError, match="ICC"):
        design_effect(10, 1.5)
    with pytest.raises(InputError, match="设计效应"):
        effective_sample_size(100, 0)
    with pytest.raises(InputError, match="样本量"):
        effective_sample_size(-1, 2)


# ---------------------------------------------------------------- 样本量


def test_sample_size_validation() -> None:
    with pytest.raises(InputError, match="不能相等"):
        two_proportion_sample_size(0.5, 0.5)
    with pytest.raises(InputError, match="0 到 1"):
        two_proportion_sample_size(1.2, 0.5)
    with pytest.raises(InputError, match="alpha"):
        two_proportion_sample_size(0.4, 0.5, alpha=0)
    with pytest.raises(InputError, match="功效"):
        two_proportion_sample_size(0.4, 0.5, power=1)
    with pytest.raises(InputError, match="目标"):
        zero_failure_sample_size(1.0)
    with pytest.raises(InputError, match="置信度"):
        zero_failure_sample_size(0.9, confidence=0)


def test_sample_size_hand_calculations() -> None:
    # 零失败：0.9^n <= 0.05 → n >= ln0.05/ln0.9 = 28.43 → 29
    assert zero_failure_sample_size(0.9, confidence=0.95) == 29
    # 0.5^n <= 0.5 恰好在 n=1 取等：不应因浮点误差多取 1
    assert zero_failure_sample_size(0.5, confidence=0.5) == 1
    # 功效 0.9、双侧 0.05：(1.959964+1.281552)²·(0.25+0.21)/0.04 = 120.84 → 121
    assert two_proportion_sample_size(0.5, 0.7, alpha=0.05, power=0.9) == 121


@given(
    p1=st.floats(0.05, 0.95),
    delta=st.floats(0.02, 0.4),
)
def test_sample_size_decreases_with_larger_effect(p1: float, delta: float) -> None:
    p2 = p1 + delta
    if p2 > 0.99:
        p2 = p1 - delta
    if not 0.01 <= p2 <= 0.99:
        return
    closer = (p1 + p2) / 2
    assert two_proportion_sample_size(p1, p2) <= two_proportion_sample_size(p1, closer)
    assert two_proportion_sample_size(p1, p2) >= 1


# ---------------------------------------------------------------- 配对比较与结论


def test_conclude_boundaries() -> None:
    assert conclude(0.01, 0.2) == CONCLUSION_UP == "提升"
    assert conclude(-0.2, -0.01) == CONCLUSION_DOWN == "下降"
    assert conclude(-0.1, 0.1) == CONCLUSION_UNDETERMINED == "不能判定"
    # 区间端点恰为 0 时不能判定
    assert conclude(0.0, 0.2) == CONCLUSION_UNDETERMINED
    assert conclude(-0.2, 0.0) == CONCLUSION_UNDETERMINED
    assert conclude(None, None) == CONCLUSION_UNDETERMINED


def test_paired_comparison_constant_improvement() -> None:
    before = {"Q1": (2, 10), "Q2": (5, 10), "Q3": (8, 10)}
    after = {"Q1": (4, 10), "Q2": (7, 10), "Q3": (10, 10)}
    result = paired_comparison(before, after, seed=1, iterations=500, level=0.95)
    assert result.pairs == 3
    assert math.isclose(result.difference, 0.2, abs_tol=1e-12)
    assert result.low is not None
    assert result.high is not None
    assert math.isclose(result.low, 0.2, abs_tol=1e-12)
    assert math.isclose(result.high, 0.2, abs_tol=1e-12)
    assert result.conclusion == CONCLUSION_UP
    reverse = paired_comparison(after, before, seed=1, iterations=500, level=0.95)
    assert reverse.conclusion == CONCLUSION_DOWN
    same = paired_comparison(before, before, seed=1, iterations=500, level=0.95)
    assert same.difference == 0.0
    assert same.conclusion == CONCLUSION_UNDETERMINED


def test_paired_comparison_weights_queries_equally_and_drops_unpaired() -> None:
    # Q1: 0/2 → 2/2（+1.0）；Q2: 50/100 → 50/100（0）；每题等权，差值 = 0.5。
    # Q3 只在基线出现、Q4 当前分母为 0：不参与配对。
    before = {"Q1": (0, 2), "Q2": (50, 100), "Q3": (1, 5), "Q4": (1, 5)}
    after = {"Q1": (2, 2), "Q2": (50, 100), "Q4": (0, 0), "Q5": (3, 3)}
    result = paired_comparison(before, after, seed=2, iterations=400, level=0.95)
    assert result.pairs == 2
    assert result.unpaired == 3
    assert math.isclose(result.difference, 0.5, abs_tol=1e-12)


def test_paired_comparison_with_too_few_pairs() -> None:
    single = paired_comparison({"Q1": (1, 4)}, {"Q1": (3, 4)}, seed=1, iterations=200, level=0.95)
    assert single.pairs == 1
    assert math.isclose(single.difference, 0.5)
    assert (single.low, single.high) == (None, None)
    assert single.conclusion == CONCLUSION_UNDETERMINED
    none = paired_comparison({"Q1": (1, 4)}, {"Q2": (3, 4)}, seed=1, iterations=200, level=0.95)
    assert none.pairs == 0
    assert none.difference is None
    assert none.conclusion == CONCLUSION_UNDETERMINED


def test_paired_comparison_is_deterministic() -> None:
    rng = np.random.default_rng(31)
    before = {f"Q{i:03d}": (int(rng.binomial(10, 0.4)), 10) for i in range(30)}
    after = {f"Q{i:03d}": (int(rng.binomial(10, 0.5)), 10) for i in range(30)}
    first = paired_comparison(before, after, seed=77, iterations=1000, level=0.95)
    shuffled = dict(reversed(list(after.items())))
    again = paired_comparison(before, shuffled, seed=77, iterations=1000, level=0.95)
    assert first == again


def test_model_version_changes() -> None:
    baseline = {"engine-a": {"a-1"}, "engine-b": {"b-1"}, "engine-c": {"c-1"}}
    current = {"engine-a": {"a-1"}, "engine-b": {"b-2"}, "engine-d": {"d-1"}}
    changes = model_version_changes(baseline, current)
    assert changes == [
        {"platform": "engine-b", "baseline": ["b-1"], "current": ["b-2"]},
    ]
    assert model_version_changes(baseline, baseline) == []
    multi = model_version_changes({"engine-a": {"a-1"}}, {"engine-a": {"a-1", "a-2"}})
    assert multi == [{"platform": "engine-a", "baseline": ["a-1"], "current": ["a-1", "a-2"]}]


# ---------------------------------------------------------------- 样本充分性


def test_assess_sufficiency_defaults_and_reasons() -> None:
    ok = assess_sufficiency(windows=3, valid_observations=30)
    assert ok == {"sufficient": True, "windows": 3, "valid_observations": 30, "reasons": []}
    few_windows = assess_sufficiency(windows=2, valid_observations=300)
    assert few_windows["sufficient"] is False
    assert few_windows["reasons"] == ["时间窗口不足 3 个（实际 2 个）"]
    few_observations = assess_sufficiency(windows=3, valid_observations=29)
    assert few_observations["sufficient"] is False
    assert few_observations["reasons"] == ["有效观测不足 30 次（实际 29 次）"]
    both = assess_sufficiency(windows=1, valid_observations=5)
    assert len(both["reasons"]) == 2
    custom = assess_sufficiency(
        windows=1, valid_observations=10, min_windows=1, min_valid_observations=10
    )
    assert custom["sufficient"] is True


# ---------------------------------------------------------------- 统计有效性（5.4）


def test_cluster_bootstrap_coverage_beats_independent_assumption() -> None:
    """40 题 × 10 次、题间有差异，模拟 1000 轮。

    每轮各题的真实概率从 Beta(2, 3) 重新抽取，真值取该分布的均值 0.4。
    数据由本测试直接用 numpy 生成，不调用被测代码。
    """
    rounds, queries, repeats = 1000, 40, 10
    alpha, beta = 2.0, 3.0
    truth = alpha / (alpha + beta)
    rng = np.random.default_rng(20261125)
    probabilities = rng.beta(alpha, beta, size=(rounds, queries))
    successes = rng.binomial(repeats, probabilities)
    sizes = [repeats] * queries
    covered_cluster = 0
    covered_independent = 0
    for index in range(rounds):
        row = successes[index].tolist()
        low, high = cluster_bootstrap_ratio(row, sizes, seed=index, iterations=1000, level=0.95)
        covered_cluster += low <= truth <= high
        naive_low, naive_high = wilson_interval(sum(row), queries * repeats, 0.95)
        covered_independent += naive_low <= truth <= naive_high
    cluster_coverage = covered_cluster / rounds
    independent_coverage = covered_independent / rounds
    print(
        f"聚类 bootstrap 覆盖率={cluster_coverage:.3f}，独立样本覆盖率={independent_coverage:.3f}"
    )
    assert 0.91 <= cluster_coverage <= 0.98
    # 把 400 次当独立样本时区间过窄，覆盖率必须明显更低
    assert independent_coverage < 0.88
    assert cluster_coverage - independent_coverage >= 0.10
