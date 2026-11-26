"""交叉验证、基线对比测试共用的数据构造：按“命中数 / 总数”批量追加观测。

只按契约写文件（经由 helpers_metrics.Builder），不调用被测的指标或统计代码。
命中位置用整数的均匀散布规则决定，测试可以据此手工推算每道题的分子、分母。
全部数据为虚构，带 synthetic: true。
"""

from __future__ import annotations

from typing import Any

from helpers_metrics import Builder, utc

WINDOW_DAYS = {"W1": 8, "W2": 9, "W3": 10, "W4": 11}
WINDOWS = ("W1", "W2", "W3")
QUERIES = tuple(f"Q{i:03d}" for i in range(1, 11))


def hit(position: int, hits: int, total: int) -> bool:
    """total 个位置中恰有 hits 个命中、均匀散开：第 position 个位置是否命中。"""
    return (position + 1) * hits // total > position * hits // total


def layout(
    position: int, queries: tuple[str, ...], windows: tuple[str, ...]
) -> tuple[str, str, int]:
    """第 position 条观测的（题目, 窗口, 第几次提问）：先轮题，再轮窗口，再加次数。"""
    query = queries[position % len(queries)]
    window = windows[(position // len(queries)) % len(windows)]
    attempt = position // (len(queries) * len(windows)) + 1
    return query, window, attempt


def add_series(
    b: Builder,
    platform: str,
    channel: str,
    hits: int,
    total: int,
    *,
    queries: tuple[str, ...] = QUERIES,
    windows: tuple[str, ...] = WINDOWS,
    **fields: Any,
) -> list[dict[str, Any]]:
    """追加 total 条有效观测（目标主体被提及的恰为 hits 条），返回这些观测。"""
    added = []
    for position in range(total):
        query, window, attempt = layout(position, queries, windows)
        fields.setdefault("model_version", None if channel == "manual" else f"{platform}-2026.10")
        added.append(
            b.obs(
                query,
                platform,
                window,
                mentioned=hit(position, hits, total),
                channel=channel,
                attempt=attempt,
                started_at=utc(WINDOW_DAYS[window], 2 + attempt % 20),
                **fields,
            )
        )
    return added


def per_query(
    hits: int,
    total: int,
    *,
    queries: tuple[str, ...] = QUERIES,
    windows: tuple[str, ...] = WINDOWS,
) -> dict[str, tuple[int, int]]:
    """按 add_series 的布局手工数出每道题的（命中数, 观测数）。"""
    counts: dict[str, list[int]] = {}
    for position in range(total):
        query, _, _ = layout(position, queries, windows)
        bucket = counts.setdefault(query, [0, 0])
        bucket[0] += hit(position, hits, total)
        bucket[1] += 1
    return {query: (counts[query][0], counts[query][1]) for query in sorted(counts)}


def add_query_counts(
    b: Builder,
    platform: str,
    channel: str,
    counts: dict[str, tuple[int, int]],
    *,
    windows: tuple[str, ...] = WINDOWS,
    flag: str = "mentioned",
    **fields: Any,
) -> list[dict[str, Any]]:
    """按题给定（命中数, 次数）追加有效观测：每题的各次提问依次落在各窗口。

    flag = "mentioned"：命中即“提及目标主体”；
    flag = "negative"：全部提及，命中即“含无依据的负面”（负面误引）。
    """
    added = []
    fields.setdefault("model_version", None if channel == "manual" else f"{platform}-2026.10")
    for query, (hits, total) in counts.items():
        for position in range(total):
            window = windows[position % len(windows)]
            attempt = position // len(windows) + 1
            flagged = hit(position, hits, total)
            if flag == "mentioned":
                options: dict[str, Any] = {"mentioned": flagged}
            else:
                options = {
                    "mentioned": True,
                    "ann": {
                        "risk": {"unfounded_negative": flagged},
                        "sentiment": "negative" if flagged else "neutral",
                    },
                }
            added.append(
                b.obs(
                    query,
                    platform,
                    window,
                    channel=channel,
                    attempt=attempt,
                    started_at=utc(WINDOW_DAYS[window], 2 + attempt % 20),
                    **options,
                    **fields,
                )
            )
    return added
