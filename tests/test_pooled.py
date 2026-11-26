"""metrics.pooled：把若干（平台, 通道）范围的结果按题合并。期望值由注释中的手工计数推算。"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers_analytics import QUERIES, add_series, per_query
from helpers_metrics import Builder

from geo_verify.metrics import Engine, Scope, build_index, parameters_for
from geo_verify.metrics.pooled import (
    NO_CELL_REASON,
    known_channels,
    known_platforms,
    pool,
    pooled_interval,
)


def build(root: Path) -> Builder:
    b = Builder(root, queries=QUERIES)
    add_series(b, "engine-a", "api", 12, 30)  # 30 次，3 个窗口
    add_series(b, "engine-b", "api", 6, 20, windows=("W2", "W3"))  # 20 次，只有 W2、W3
    add_series(b, "engine-c", "manual", 5, 10)  # 未登记的平台
    b.write()
    return b


def engine_for(root: Path) -> Engine:
    index = build_index(root)
    return Engine(index, parameters_for(index.project, iterations=200))


def test_pool_sums_per_query_counts_and_unions_windows(tmp_path: Path) -> None:
    engine = engine_for(build(tmp_path / "proj").root)
    cells = [
        engine.compute("mention_rate", Scope(platform="engine-a", channel="api")),
        engine.compute("mention_rate", Scope(platform="engine-b", channel="api")),
        engine.compute("mention_rate", Scope(platform="engine-b", channel="manual")),  # 没有数据
    ]
    pooled = pool("mention_rate", cells)
    a = per_query(12, 30)
    b = per_query(6, 20, windows=("W2", "W3"))
    expected = {query: (a[query][0] + b[query][0], a[query][1] + b[query][1]) for query in QUERIES}
    assert pooled.clusters == expected
    assert list(pooled.clusters) == sorted(QUERIES)
    assert (pooled.numerator, pooled.denominator) == (18, 50)
    assert pooled.value == pytest.approx(18 / 50, abs=1e-12)
    assert pooled.windows == ("W1", "W2", "W3")
    assert pooled.observations == 50
    assert pooled.cells == (("engine-a", "api"), ("engine-b", "api"))
    assert pooled.null_reason is None
    assert pooled.queries == list(QUERIES)
    numerators, denominators = pooled.vectors()
    assert sum(numerators) == 18
    assert denominators == [expected[query][1] for query in QUERIES]
    assert pooled.sufficiency(min_windows=3, min_valid_observations=30)["sufficient"] is True
    short = pooled.sufficiency(min_windows=4, min_valid_observations=60)
    assert short["sufficient"] is False
    assert len(short["reasons"]) == 2
    # 只并入 engine-b（2 个窗口、20 次）
    alone = pool("mention_rate", cells[1:])
    assert alone.windows == ("W2", "W3")
    assert alone.sufficiency(min_windows=3, min_valid_observations=30)["sufficient"] is False


def test_pool_interval_follows_metrics_rules(tmp_path: Path) -> None:
    engine = engine_for(build(tmp_path / "proj").root)
    single = engine.compute("mention_rate", Scope(platform="engine-a", channel="api"))
    pooled = pool("mention_rate", [single])
    interval, note = pooled_interval(engine, pooled)
    assert interval == single.result["interval"]  # 与 metrics 在同一范围给出的区间一致
    assert note is None
    assert pooled_interval(engine, pool("mention_rate", [])) == (None, None)


def test_pool_without_data_explains_why(tmp_path: Path) -> None:
    engine = engine_for(build(tmp_path / "proj").root)
    empty = engine.compute("mention_rate", Scope(platform="engine-a", channel="manual"))
    pooled = pool("mention_rate", [empty, empty])
    assert pooled.value is None
    assert (pooled.numerator, pooled.denominator) == (0, 0)
    assert pooled.null_reason == empty.result["null_reason"]  # 相同的原因只写一次
    assert pooled.cells == ()
    assert pool("mention_rate", []).null_reason == NO_CELL_REASON
    with pytest.raises(ValueError, match="不能把"):
        pool("top1_rate", [empty])


def test_known_platforms_and_channels(tmp_path: Path) -> None:
    index = build_index(build(tmp_path / "proj").root)
    # 登记的 engine-a、engine-b，加上观测中出现的 engine-c；通道同理
    assert known_platforms(index) == ["engine-a", "engine-b", "engine-c"]
    assert known_channels(index) == ["api", "manual"]
