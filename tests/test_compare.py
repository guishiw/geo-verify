"""`geo-verify compare` 测试：同题配对的基线对比、拒绝比较（题库版本、模型版本）、样本不足、
S03 / S04 的多轮模拟、只合并两期共有的（平台, 通道）、退出码与可复现性。

期望值来自注释中的手工推算；模拟数据由 numpy 的二项分布在测试里直接生成，
不调用被测的统计或指标代码。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from conftest import run_cli, run_cli_json
from factory import make_prompt, read, seal_prompts
from helpers_analytics import WINDOWS, add_query_counts
from helpers_metrics import Builder, copy_project

from geo_verify.compare import Limits, compare_pooled
from geo_verify.metrics import Parameters
from geo_verify.metrics.pooled import Pooled
from geo_verify.stats import paired_comparison

SEED = 20261001  # 样例项目预先登记的 seed
QUERIES20 = tuple(f"Q{i:03d}" for i in range(1, 21))


def counts(hits: Callable[[int], int], total: int = 12) -> dict[str, tuple[int, int]]:
    return {query: (hits(i), total) for i, query in enumerate(QUERIES20)}


# 每题 12 次提问（3 个窗口 × 4 次），共 240 次。
BASE = counts(lambda i: 3 + i % 4)  # 每题 3～6 次命中：合计 60 + 30 = 90，即 0.375
UP = counts(lambda i: 6 + i % 4 + i % 3)  # 每题比基线多 3 + i%3 次：合计 169，约 0.704
SAME = counts(lambda i: 2 + i % 4 + i % 3)  # 每题比基线差 i%3 − 1 次（−1、0、+1 交替）
DOWN = counts(lambda i: 1 + i % 4 - i % 2)  # 每题比基线少 2 + i%2 次
# 逐题差值之和（单位：次）：UP 为 7·3 + 7·4 + 6·5 = 79；SAME 为 −7 + 0 + 6 = −1；DOWN 为 −(40 + 10) = −50


def make(
    root: Path,
    data: dict[str, tuple[int, int]],
    *,
    platform: str = "engine-a",
    queries: tuple[str, ...] = QUERIES20,
    **kwargs: Any,
) -> Builder:
    b = Builder(root, queries=queries)
    add_query_counts(b, platform, "api", data, **kwargs)
    b.write()
    return b


def compare(baseline: Builder, current: Builder, *extra: str) -> tuple[int, dict[str, Any]]:
    code, data = run_cli_json("compare", str(baseline.root), str(current.root), *extra)
    assert "error" not in data, data
    document: dict[str, Any] = read(current.root / "out" / "compare.json")
    return code, document


def result(
    document: dict[str, Any], metric_id: str = "mention_rate", platform: str | None = None
) -> dict[str, Any]:
    matches = [
        item
        for item in document["results"]
        if item["metric_id"] == metric_id and item["platform"] == platform
    ]
    assert len(matches) == 1
    return matches[0]


# ---------------------------------------------------------------- 结论


def test_clear_improvement_is_detected(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = make(tmp_path / "cur", UP)
    code, document = compare(baseline, current, "--iterations", "500")
    assert code == 0
    assert document["status"] == "ok"
    row = result(document)
    assert (row["baseline"]["numerator"], row["baseline"]["denominator"]) == (90, 240)
    assert (row["current"]["numerator"], row["current"]["denominator"]) == (169, 240)
    assert row["baseline"]["value"] == pytest.approx(0.375, abs=1e-12)
    assert row["difference"] == pytest.approx(79 / 240, abs=1e-12)
    assert row["baseline_paired_mean"] == pytest.approx(90 / 240, abs=1e-12)
    assert row["current_paired_mean"] == pytest.approx(169 / 240, abs=1e-12)
    assert row["paired_queries"] == 20
    assert row["interval"]["method"] == "paired_cluster_bootstrap"
    assert row["interval"]["level"] == 0.95
    expected = paired_comparison(BASE, UP, seed=SEED, iterations=500, level=0.95)
    assert (row["interval"]["low"], row["interval"]["high"]) == (expected.low, expected.high)
    assert row["interval"]["low"] > 0
    assert row["sufficient"] is True
    assert row["conclusion"] == "提升"
    assert row["conclusion_code"] == "improved"
    assert row["favorable"] is True
    assert row["higher_is_better"] is True


def test_unchanged_truth_is_not_improvement(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = make(tmp_path / "cur", SAME)
    code, document = compare(baseline, current)
    assert code == 0
    row = result(document)
    assert row["difference"] == pytest.approx(-1 / 240, abs=1e-12)
    assert row["interval"]["low"] < 0 < row["interval"]["high"]
    assert row["conclusion"] == "不能判定"
    assert row["conclusion_code"] == "inconclusive"
    assert row["favorable"] is None


def test_decline_and_favorable_direction(tmp_path: Path) -> None:
    code, document = compare(make(tmp_path / "b1", BASE), make(tmp_path / "c1", DOWN))
    row = result(document)
    assert row["difference"] == pytest.approx(-50 / 240, abs=1e-12)
    assert row["conclusion"] == "下降"
    assert row["conclusion_code"] == "declined"
    assert row["favorable"] is False
    # 负面误引率越低越好：数值下降 → conclusion 仍为“下降”，但 favorable 为 true
    baseline = make(tmp_path / "b2", BASE, flag="negative")
    current = make(tmp_path / "c2", DOWN, flag="negative")
    code, document = compare(
        baseline, current, "--metric", "negative_miscitation_rate", "--metric", "mention_rate"
    )
    assert code == 0
    assert document["metrics"] == ["mention_rate", "negative_miscitation_rate"]
    negative = result(document, "negative_miscitation_rate")
    assert negative["higher_is_better"] is False
    assert negative["conclusion"] == "下降"
    assert negative["favorable"] is True
    mention = result(document, "mention_rate")  # 两期全部提及：差值 0
    assert mention["difference"] == 0
    assert mention["conclusion"] == "不能判定"


def test_descriptive_metric_has_no_favorable_direction(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE, flag="negative")
    current = make(tmp_path / "cur", UP, flag="negative")
    _, document = compare(baseline, current, "--metric", "sentiment_negative_share")
    row = result(document, "sentiment_negative_share")
    assert row["higher_is_better"] is None
    assert row["conclusion"] == "提升"
    assert row["favorable"] is None


def simulate(shift: float, *, rounds: int, seed: int) -> Counter[str]:
    """每轮：30 道题的真值各不相同（0.2～0.6 均匀分布），两期各问 12 次；当前期真值整体加 shift。

    bootstrap 用 400 次重抽（够区分 S03 / S04 的门槛，也让 400 轮模拟保持在 1 秒内）。
    """
    rng = np.random.default_rng(seed)
    params = Parameters(
        seed=SEED, iterations=400, level=0.95, min_windows=3, min_valid_observations=30
    )
    limits = Limits(min_windows=3, min_valid_observations=30)
    queries = [f"Q{i:03d}" for i in range(1, 31)]

    def side(hits: Any) -> Pooled:
        return Pooled(
            metric_id="mention_rate",
            clusters={query: (int(k), 12) for query, k in zip(queries, hits, strict=True)},
            windows=WINDOWS,
            observations=12 * len(queries),
            cells=(("engine-a", "api"),),
            null_reason=None,
        )

    outcomes: Counter[str] = Counter()
    for _ in range(rounds):
        truth = rng.uniform(0.2, 0.6, size=len(queries))
        before = rng.binomial(12, truth)
        after = rng.binomial(12, np.clip(truth + shift, 0.0, 1.0))
        row = compare_pooled(
            "mention_rate",
            None,
            side(before),
            side(after),
            params=params,
            baseline_limits=limits,
            current_limits=limits,
        )
        outcomes[row["conclusion_code"]] += 1
    return outcomes


def test_simulated_no_change_false_positive_rate() -> None:
    # S03：前后真值相同，200 轮中判为“提升”的比例 ≤ 0.08
    outcomes = simulate(0.0, rounds=200, seed=303)
    assert outcomes["improved"] / 200 <= 0.08
    assert outcomes["declined"] / 200 <= 0.08


def test_simulated_clear_improvement_detection_rate() -> None:
    # S04：真值 40% → 70%，200 轮中判为“提升”的比例 ≥ 0.9
    outcomes = simulate(0.3, rounds=200, seed=404)
    assert outcomes["improved"] / 200 >= 0.9
    assert outcomes["declined"] == 0


def pooled(clusters: dict[str, tuple[int, int]]) -> Pooled:
    return Pooled(
        metric_id="mention_rate",
        clusters=clusters,
        windows=WINDOWS,
        observations=sum(total for _, total in clusters.values()),
        cells=(("engine-a", "api"),),
        null_reason=None,
    )


@pytest.mark.parametrize(
    ("baseline", "current", "pairs", "why"),
    [
        # 两期样本都充分，但题目完全不重合：无法配对
        ({"Q001": (10, 40)}, {"Q002": (30, 40)}, 0, "没有共同的题"),
        # 只有 1 道共同的题：无法按题重抽
        ({"Q001": (10, 40), "Q002": (5, 10)}, {"Q001": (30, 40)}, 1, "不足 2 道"),
    ],
)
def test_too_few_paired_queries_is_inconclusive(
    baseline: dict[str, tuple[int, int]],
    current: dict[str, tuple[int, int]],
    pairs: int,
    why: str,
) -> None:
    params = Parameters(
        seed=SEED, iterations=200, level=0.95, min_windows=3, min_valid_observations=30
    )
    limits = Limits(min_windows=3, min_valid_observations=30)
    row = compare_pooled(
        "mention_rate",
        None,
        pooled(baseline),
        pooled(current),
        params=params,
        baseline_limits=limits,
        current_limits=limits,
    )
    assert row["sufficient"] is True
    assert row["paired_queries"] == pairs
    assert row["interval"] is None
    assert row["conclusion"] == "不能判定"
    assert why in row["reason"]
    if pairs:
        assert row["difference"] == pytest.approx(30 / 40 - 10 / 40, abs=1e-12)
        assert row["baseline_paired_mean"] == pytest.approx(0.25, abs=1e-12)
    else:
        assert row["difference"] is None
        assert row["baseline_paired_mean"] is None


# ---------------------------------------------------------------- 拒绝比较


def test_prompt_set_changed_after_baseline_is_refused(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = Builder(tmp_path / "cur", queries=QUERIES20)
    amended = [make_prompt(query) for query in QUERIES20]
    amended[0]["text"] = "第 1 题（封存后改写）：20 万左右的纯电 SUV 哪款续航更长？（虚构）"
    second = seal_prompts(current.root, current.project, amended, previous=current.sealed)
    add_query_counts(current, "engine-a", "api", UP, prompt_set=second)
    current.write()
    code, document = compare(baseline, current)
    assert code == 1
    assert document["status"] == "refused"
    assert document["status_label"] == "拒绝比较"
    assert document["results"] == []
    refusal = document["refusal"]
    assert refusal["code"] == "prompt_set_mismatch"
    assert "禁止混算" in refusal["message"]
    assert refusal["details"]["baseline"] == {
        "version": 1,
        "content_hash": baseline.sealed["content_hash"],
    }
    assert refusal["details"]["current"] == {"version": 2, "content_hash": second["content_hash"]}
    assert document["current"]["prompt_set"]["version"] == 2


def test_model_version_change_requires_new_baseline(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = make(tmp_path / "cur", UP, model_version="engine-a-2026.11")
    code, document = compare(baseline, current)
    assert code == 1
    assert document["status"] == "refused"
    refusal = document["refusal"]
    assert refusal["code"] == "model_version_changed"
    assert "须重建基线" in refusal["message"]
    assert refusal["details"]["between"] == [
        {
            "platform": "engine-a",
            "channel": "api",
            "baseline": ["engine-a-2026.10"],
            "current": ["engine-a-2026.11"],
        }
    ]
    assert refusal["details"]["within"] == []
    _, text, _ = run_cli("compare", str(baseline.root), str(current.root))
    assert "须重建基线" in text
    # 不提供绕过开关：试图用参数放行版本变化属于用法错误
    code, _, err = run_cli(
        "compare", str(baseline.root), str(current.root), "--allow-version-change"
    )
    assert code == 2
    assert "输入错误" in err


def test_version_change_inside_one_project_is_refused(tmp_path: Path) -> None:
    # 两期都在观测期内从 2026.10 换到 2026.11：版本集合相同，但各自内部都有变化
    first_half = dict(list(BASE.items())[:10])
    second_half = dict(list(BASE.items())[10:])
    projects = []
    for name in ("base", "cur"):
        b = Builder(tmp_path / name, queries=QUERIES20)
        add_query_counts(b, "engine-a", "api", first_half)
        add_query_counts(b, "engine-a", "api", second_half, model_version="engine-a-2026.11")
        b.write()
        projects.append(b)
    code, document = compare(*projects)
    assert code == 1
    refusal = document["refusal"]
    assert refusal["code"] == "model_version_changed"
    assert refusal["details"]["between"] == []
    assert [item["side"] for item in refusal["details"]["within"]] == ["baseline", "current"]
    assert refusal["details"]["within"][0]["versions"] == ["engine-a-2026.10", "engine-a-2026.11"]


def test_platform_option_limits_version_check_and_rows(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    add_query_counts(baseline, "engine-b", "api", BASE)
    baseline.write()
    current = make(tmp_path / "cur", UP, model_version="engine-a-2026.11")
    add_query_counts(current, "engine-b", "api", UP)
    current.write()
    assert compare(baseline, current)[0] == 1  # engine-a 换了版本：整体拒绝
    code, document = compare(baseline, current, "--platform", "engine-b")
    assert code == 0
    assert document["platform"] == "engine-b"
    assert [item["platform"] for item in document["results"]] == ["engine-b"]
    assert result(document, platform="engine-b")["conclusion"] == "提升"
    assert document["baseline"]["valid_observations"] == 240  # 只统计 engine-b


# ---------------------------------------------------------------- 样本不足、部分重合、合并范围


def test_insufficient_sample_is_inconclusive_with_exit_3(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE, windows=("W1", "W2"))  # 只有 2 个窗口
    current = make(tmp_path / "cur", UP)
    code, document = compare(baseline, current)
    assert code == 3
    assert document["status"] == "insufficient"
    assert document["status_label"] == "样本不足"
    row = result(document)
    assert row["sufficient"] is False
    assert row["baseline"]["sufficient"] is False
    assert row["current"]["sufficient"] is True
    assert any("时间窗口不足 3 个" in item for item in row["baseline"]["sufficiency_reasons"])
    assert row["difference"] == pytest.approx(79 / 240, abs=1e-12)  # 差值照常给出
    assert row["interval"]["low"] > 0  # 区间也在 0 之上……
    assert row["conclusion"] == "不能判定"  # ……但样本不足时一律不能判定
    assert row["favorable"] is None
    assert "样本不足" in row["reason"]


def test_partially_overlapping_queries_pair_only_common(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = make(tmp_path / "cur", dict(list(UP.items())[:18]))  # 当前期 Q019、Q020 没有数据
    code, document = compare(baseline, current)
    assert code == 0
    row = result(document)
    assert row["paired_queries"] == 18
    assert row["baseline_only_queries"] == 2
    assert row["current_only_queries"] == 0
    # 前 18 题的逐题差值之和：6·3 + 6·4 + 6·5 = 72 次 → 72 / (18·12) = 1/3
    assert row["difference"] == pytest.approx(1 / 3, abs=1e-12)
    assert "18" in row["reason"]


def test_only_cells_with_data_in_both_periods_are_merged(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    add_query_counts(baseline, "engine-b", "api", UP)  # 当前期没有 engine-b
    baseline.write()
    current = make(tmp_path / "cur", UP)
    code, document = compare(baseline, current)
    assert code == 0
    overall = result(document)
    assert overall["cells"] == [{"platform": "engine-a", "channel": "api"}]
    assert overall["unmatched_cells"] == [
        {"platform": "engine-b", "channel": "api", "side": "baseline"}
    ]
    assert (overall["baseline"]["numerator"], overall["baseline"]["denominator"]) == (90, 240)
    platform_b = result(document, platform="engine-b")
    assert platform_b["cells"] == []
    assert platform_b["baseline"]["value"] is None
    assert platform_b["conclusion"] == "不能判定"
    assert "没有共同" in platform_b["reason"]
    assert document["status"] == "ok"  # 平台行不足不影响整体（整体行样本充分）


# ---------------------------------------------------------------- 指标选择与输入错误


def test_default_metrics_come_from_registered_acceptance_metrics(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = make(tmp_path / "cur", UP)
    _, document = compare(baseline, current)
    assert document["metrics"] == ["mention_rate"]  # 样例项目登记的验收指标
    row = result(document)
    assert row["preregistered"] is True
    assert row["role"] == "acceptance"
    assert [item["platform"] for item in document["results"]] == [None, "engine-a", "engine-b"]

    def registered(root: Path, items: list[dict[str, Any]]) -> Builder:
        b = Builder(root, queries=QUERIES20, prereg={"metrics": items})
        add_query_counts(b, "engine-a", "api", UP)
        b.write()
        return b

    threshold = {"op": ">=", "value": 0.5}
    mixed = registered(
        tmp_path / "mixed",
        [
            {
                "metric_id": "accuracy",
                "definition_version": "1.0",
                "role": "acceptance",
                "threshold": threshold,
            },
            {
                "metric_id": "top3_rate",
                "definition_version": "1.0",
                "role": "acceptance",
                "threshold": threshold,
            },
        ],
    )
    _, document = compare(baseline, mixed)
    assert document["metrics"] == ["top3_rate"]  # accuracy 按判定计，不能按观测配对，跳过
    descriptive = registered(
        tmp_path / "descriptive",
        [
            {
                "metric_id": "sentiment_distribution",
                "definition_version": "1.0",
                "role": "descriptive",
                "threshold": None,
            }
        ],
    )
    _, document = compare(baseline, descriptive)
    assert document["metrics"] == ["mention_rate"]
    assert "default_metric" in [item["code"] for item in document["notices"]]
    assert result(document)["preregistered"] is False
    assert result(document)["role"] == "post_hoc"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--metric", "intent_coverage"), "不支持"),
        (("--metric", "accuracy_verifiable"), "不支持"),
        (("--metric", "target_source_share"), "不支持"),
        (("--metric", "temporal_std"), "不支持"),
        (("--metric", "recheck_pass_rate"), "不支持"),
        (("--metric", "recommendation_rate_daily"), "不支持"),
        (("--metric", "process_quality"), "不支持"),
        (("--metric", "no_such_metric"), "未知指标"),
        (("--platform", "engine-z"), "未知平台"),
        (("--iterations", "0"), "iterations"),
    ],
)
def test_input_errors_exit_2(tmp_path: Path, args: tuple[str, ...], message: str) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = make(tmp_path / "cur", UP)
    code, data = run_cli_json("compare", str(baseline.root), str(current.root), *args)
    assert code == 2
    assert data["error"] == "input_error"
    assert message in data["message"]
    assert not (current.root / "out" / "compare.json").exists()


def test_metric_alias_expands_to_supported_metrics(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE, flag="negative")
    current = make(tmp_path / "cur", UP, flag="negative")
    _, document = compare(baseline, current, "--metric", "sentiment_distribution")
    assert document["metrics"] == [
        "sentiment_positive_share",
        "sentiment_neutral_share",
        "sentiment_negative_share",
    ]


# ---------------------------------------------------------------- 文件、输出与可复现性


def test_document_structure_follows_outputs_md(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = make(tmp_path / "cur", UP)
    _, document = compare(baseline, current)
    assert document["schema_version"] == "1.0"
    assert document["kind"] == "compare"
    assert document["project_id"] == "auto-demo"
    assert document["synthetic"] is True
    for side in ("baseline", "current"):
        assert document[side] == {
            "project_id": "auto-demo",
            "prompt_set": {"version": 1, "content_hash": baseline.sealed["content_hash"]},
            "windows": 3,
            "valid_observations": 240,
        }
    assert document["parameters"] == {"seed": SEED, "iterations": 1000, "confidence_level": 0.95}
    assert document["refusal"] is None
    assert document["status_label"] == "已比较"
    assert "数值方向" in document["method"]["conclusion"]
    assert "越低越好" in document["method"]["favorable"]
    # GOAL：版本变化默认禁止比较；本期不提供绕过开关，写入已知限制
    assert any("不提供人工例外开关" in item for item in document["limitations"])
    for item in document["results"]:
        assert set(item) >= {
            "metric_id",
            "platform",
            "baseline",
            "current",
            "difference",
            "interval",
            "paired_queries",
            "sufficient",
            "conclusion",
            "conclusion_code",
            "reason",
        }
        for side in (item["baseline"], item["current"]):
            assert set(side) >= {"value", "numerator", "denominator"}


def test_json_text_out_and_reproducible_bytes(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = make(tmp_path / "cur", UP)
    code, data = run_cli_json("compare", str(baseline.root), str(current.root))
    assert code == 0
    assert data["output"].endswith("compare.json")
    written = read(current.root / "out" / "compare.json")
    assert {key: value for key, value in data.items() if key != "output"} == written
    code, text, _ = run_cli("compare", str(baseline.root), str(current.root))
    assert code == 0
    assert "提升" in text
    assert "同题配对" in text
    # 两份项目复制到另一处独立计算，结果文件逐字节一致，且不含路径
    copy_project(baseline.root, tmp_path / "copy" / "base")
    copy_project(current.root, tmp_path / "copy" / "cur")
    out = tmp_path / "copy.json"
    code, _, _ = run_cli(
        "compare",
        str(tmp_path / "copy" / "base"),
        str(tmp_path / "copy" / "cur"),
        "--out",
        str(out),
    )
    assert code == 0
    assert out.read_bytes() == (current.root / "out" / "compare.json").read_bytes()
    assert str(tmp_path) not in out.read_text(encoding="utf-8")
    assert not (tmp_path / "copy" / "cur" / "out" / "compare.json").exists()


def test_parameter_override_is_noticed(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = make(tmp_path / "cur", UP)
    _, document = compare(baseline, current, "--seed", "7", "--iterations", "300")
    assert document["parameters"] == {"seed": 7, "iterations": 300, "confidence_level": 0.95}
    assert [item["code"] for item in document["notices"]] == ["parameters_overridden"]
    expected = paired_comparison(BASE, UP, seed=7, iterations=300, level=0.95)
    row = result(document)
    assert (row["interval"]["low"], row["interval"]["high"]) == (expected.low, expected.high)


def test_preregistration_change_is_noticed(tmp_path: Path) -> None:
    baseline = make(tmp_path / "base", BASE)
    current = make(tmp_path / "cur", UP)
    prereg = dict(current.project["preregistration"])
    prereg["sufficiency"] = {"min_windows": 1, "min_valid_observations": 1}  # 封存后放宽
    current.update_project(preregistration=prereg)
    _, document = compare(baseline, current)
    notice = next(item for item in document["notices"] if item["code"] == "preregistration_changed")
    assert notice["details"]["side"] == "current"
    assert result(document)["preregistered"] is False
