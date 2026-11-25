"""采集与导入测试的辅助函数：拼 CSV、独立重算确定性编号。"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Any


def independent_digest(
    project_id: str,
    prompt_set_hash: str,
    platform: str,
    channel: str,
    window: str,
    query_id: str,
    attempt: int,
) -> str:
    """按约定独立重算任务摘要：sha256("项目|题库哈希|平台|通道|窗口|题目|次数") 的前 24 位。"""
    raw = f"{project_id}|{prompt_set_hash}|{platform}|{channel}|{window}|{query_id}|{attempt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def csv_text(fieldnames: list[str], rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in fieldnames})
    return buffer.getvalue()


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(csv_text(fieldnames, rows), encoding="utf-8", newline="")
    return path


IMPORT_FIELDS = [
    "query_id",
    "platform",
    "channel",
    "entry_form",
    "model_version",
    "entry_features",
    "window_id",
    "attempt",
    "started_at",
    "ended_at",
    "logged_in",
    "history_cleared",
    "web_search",
    "region",
    "device",
    "answer_text",
    "citations",
    "snapshots",
    "status",
    "invalid_reason",
    "operator",
]


def import_row(**overrides: Any) -> dict[str, Any]:
    """一行合法的导入数据（API 通道、有效观测）；用关键字参数改写个别列。"""
    row: dict[str, Any] = {
        "query_id": "Q001",
        "platform": "engine-a",
        "channel": "api",
        "entry_form": "api",
        "model_version": "engine-a-2026.10",
        "window_id": "W1",
        "attempt": "1",
        "started_at": "2026-10-08T02:00:00Z",
        "ended_at": "2026-10-08T02:00:09Z",
        "history_cleared": "true",
        "web_search": "true",
        "answer_text": "可以考虑澜序 S7。",
        "status": "valid",
        "operator": "采集员甲（虚构）",
    }
    row.update(overrides)
    return row
