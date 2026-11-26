"""告警测试共用的场景构造器。

只按契约写文件（经由 helpers_metrics.Builder / tests/factory.py），不调用被测的告警代码。
每个场景在调用处用注释写明手工计数，期望值由这些计数推得。全部数据为虚构，带 synthetic: true。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from conftest import run_cli, run_cli_json
from factory import read
from helpers_metrics import Builder

QUERIES = ("Q001", "Q002", "Q003")
# 告警输出里不得出现的措辞：因果表述（GOAL V8）、压制负面与结果保证（R3、R4）、R9 禁用词。
# 这份清单独立写在测试里，不取自被测代码。
CAUSAL_TERMS = (
    "原因",
    "导致",
    "因为",
    "由于",
    "造成",
    "引起",
    "致使",
    "归因",
    "起因",
    "源于",
    "所以",
    "因此",
    "因而",
    "影响",
    "使得",
)
RED_LINE_TERMS = ("压制", "挤出", "删除负面", "保证", "必然", "首推", "官方", "认证", "对标")


def stamp(day: int, i: int, *, month: int = 10) -> str:
    """第 i 条观测的 UTC 时间：当天 02:00 起每条错开一分钟（i < 600）。"""
    hour, minute = divmod(i, 60)
    return f"2026-{month:02d}-{day:02d}T{2 + hour:02d}:{minute:02d}:00Z"


def add(
    b: Builder,
    window: str,
    count: int,
    *,
    platform: str = "engine-a",
    day: int = 8,
    mentioned: Callable[[int], bool | None] = lambda _i: True,
    ann: Callable[[int], dict[str, Any]] = lambda _i: {},
    citations: Callable[[int], list[dict[str, Any]] | None] = lambda _i: None,
    started: Callable[[int], str] | None = None,
    after: Callable[[dict[str, Any], int], None] | None = None,
    **fields: Any,
) -> list[dict[str, Any]]:
    """追加 count 条有效观测（题目在 Q001～Q003 间轮换），返回这些观测。

    mentioned(i)：目标主体是否被提及（None 表示不写目标主体的标注）；
    ann(i)：传给目标主体标注的其他字段；citations(i)：该观测的引用；
    after(obs, i)：追加观测后的回调（如再写竞品标注、判定）。
    """
    made = []
    for i in range(count):
        extra = dict(ann(i))
        flag = mentioned(i)
        if flag is False:
            extra.setdefault("sentiment", None)
        observation = b.obs(
            QUERIES[i % len(QUERIES)],
            platform,
            window,
            mentioned=flag,
            ann=extra,
            started_at=started(i) if started else stamp(day, i),
            citations=citations(i),
            **fields,
        )
        if after is not None:
            after(observation, i)
        made.append(observation)
    return made


def ids(observations: list[dict[str, Any]]) -> list[str]:
    return sorted(item["observation_id"] for item in observations)


def upper_tail(z: float) -> float:
    """标准正态上尾概率 P(Z > z)，用 erfc 独立计算（不依赖被测代码）。"""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def two_proportion_p(x1: int, n1: int, x2: int, n2: int, *, increase: bool) -> float:
    """合并方差的两比例 z 检验单侧 p 值（手工公式）。"""
    p1, p2 = x1 / n1, x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    z = (p2 - p1) / math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    return upper_tail(z if increase else -z)


def welch_p(before: list[int], after: list[int]) -> float:
    """均值后移（after 均值更大）的单侧 Welch z 检验 p 值（手工公式）。"""

    def mean_var(values: list[int]) -> tuple[float, float]:
        mean = sum(values) / len(values)
        return mean, sum((v - mean) ** 2 for v in values) / (len(values) - 1)

    m1, v1 = mean_var(before)
    m2, v2 = mean_var(after)
    return upper_tail((m2 - m1) / math.sqrt(v1 / len(before) + v2 / len(after)))


def run_alerts(root: Path, *extra: str) -> tuple[int, dict[str, Any]]:
    """运行 alerts --json，返回 (退出码, 写出的 out/alerts.json 内容)。"""
    code, data = run_cli_json("alerts", str(root), *extra)
    assert code in (0, 1), data
    return code, read(root / "out" / "alerts.json")


def alerts_text(root: Path, *extra: str) -> tuple[int, str, str]:
    return run_cli("alerts", str(root), *extra)


def select(
    document: dict[str, Any],
    rule_id: str,
    *,
    platform: str | Any | None = ...,
    metric: str | None = None,
    entity_id: str | Any | None = ...,
) -> list[dict[str, Any]]:
    """按规则（及可选的平台、指标、实体）筛选告警。"""
    found = []
    for alert in document["alerts"]:
        if alert["rule_id"] != rule_id:
            continue
        if platform is not ... and alert["platform"] != platform:
            continue
        if entity_id is not ... and alert["entity_id"] != entity_id:
            continue
        if metric is not None and alert["evidence"]["metric"] != metric:
            continue
        found.append(alert)
    return found


def all_strings(value: Any) -> list[str]:
    """递归取出 JSON 值里的全部字符串（含键名）。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (key, *all_strings(item))]
    if isinstance(value, list):
        return [text for item in value for text in all_strings(item)]
    return []


def banned_in(texts: list[str]) -> list[tuple[str, str]]:
    return [
        (term, text) for text in texts for term in (*CAUSAL_TERMS, *RED_LINE_TERMS) if term in text
    ]
