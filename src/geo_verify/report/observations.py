"""原始记录索引：observations.jsonl 的每一行对应一行（含重复编号与无效观测），便于逐条核对。

导出为 CSV 时用 UTF-8 BOM，并对以 `= + - @`、制表符、回车开头的单元格加 `'` 前缀（见 csvsafe）。
"""

from __future__ import annotations

from typing import Any

from geo_verify.errors import InputError
from geo_verify.report.inputs import Inputs
from geo_verify.report.labels import channel_label
from geo_verify.timeutil import local_date

EXCERPT_LENGTH = 80
COLUMNS: tuple[str, ...] = (
    "line",
    "observation_id",
    "trace_id",
    "query_id",
    "platform",
    "channel",
    "channel_label",
    "window_id",
    "attempt",
    "started_at",
    "local_date",
    "model_version",
    "status",
    "invalid_reason",
    "in_selected_prompt_set",
    "excluded",
    "exclusion_rule",
    "duplicate",
    "snapshots",
    "citations",
    "answer_excerpt",
)


def excerpt(text: Any) -> str | None:
    """回答摘要：空白折叠为单个空格，超过 EXCERPT_LENGTH 个字符时截断并加省略号。"""
    if not isinstance(text, str):
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) <= EXCERPT_LENGTH:
        return collapsed
    return collapsed[:EXCERPT_LENGTH] + "…"


def _date(value: Any, timezone: str) -> str | None:
    try:
        return local_date(value, timezone).isoformat()
    except InputError:
        return None


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def observation_rows(inputs: Inputs) -> list[list[Any]]:
    selected = inputs.prompt_set["content_hash"] if inputs.prompt_set else None
    timezone = inputs.project["timezone"]
    seen: set[Any] = set()
    rows: list[list[Any]] = []
    for line, item in enumerate(inputs.observations, start=1):
        observation_id = item.get("observation_id")
        duplicate = observation_id in seen
        seen.add(observation_id)
        channel = item.get("channel")
        rule = inputs.excluded.get(observation_id) if isinstance(observation_id, str) else None
        rows.append(
            [
                line,
                observation_id,
                item.get("trace_id"),
                item.get("query_id"),
                item.get("platform"),
                channel,
                channel_label(channel) if isinstance(channel, str) else None,
                item.get("window_id"),
                item.get("attempt"),
                item.get("started_at"),
                _date(item.get("started_at"), timezone),
                item.get("model_version"),
                item.get("status"),
                item.get("invalid_reason"),
                None if selected is None else item.get("prompt_set_hash") == selected,
                rule is not None,
                rule,
                duplicate,
                _count(item.get("snapshots")),
                _count(item.get("citations")),
                excerpt(item.get("answer_text")),
            ]
        )
    return rows
