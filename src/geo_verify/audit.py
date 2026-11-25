"""审计事件：只追加的哈希链。

每个事件记录变更人、时间、理由、变更前后哈希，并用 previous_event_hash 指向上一个事件。
任何对历史事件的修改、删除或重排都会让链校验失败。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geo_verify import spec
from geo_verify.canon import hash_without
from geo_verify.errors import InputError
from geo_verify.jsonl import append_jsonl, read_jsonl
from geo_verify.project import paths
from geo_verify.timeutil import format_utc, parse_utc

SCHEMA_VERSION = "1.0"


def resolve_now(value: str | None) -> str:
    """命令的时间戳：传入 --now 时使用它（便于复算），否则取当前 UTC 时间。"""
    if value is None:
        return format_utc(datetime.now(UTC))
    parse_utc(value)
    return value


def event_hash(event: dict[str, Any]) -> str:
    return hash_without(event, "event_hash")


def append_event(
    root: str | Path,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
    occurred_at: str,
    before_hash: str | None = None,
    after_hash: str | None = None,
    details: dict[str, Any] | None = None,
    synthetic: bool | None = None,
) -> dict[str, Any]:
    """追加一个审计事件并返回它。理由不能为空。"""
    if not reason or not reason.strip():
        raise InputError("审计事件必须写明理由（--reason）")
    if not actor or not actor.strip():
        raise InputError("审计事件必须写明变更人")
    project_paths = paths(root)
    existing = read_jsonl(project_paths.audit, missing_ok=True)
    project_id = _project_id(project_paths.project_file)
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"evt-{len(existing) + 1:06d}",
        "project_id": project_id,
        "actor": actor,
        "occurred_at": occurred_at,
        "action": action,
        "target": {"type": target_type, "id": target_id},
        "reason": reason,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "previous_event_hash": existing[-1]["event_hash"] if existing else None,
    }
    if details is not None:
        event["details"] = details
    if synthetic is not None:
        event["synthetic"] = synthetic
    event["event_hash"] = event_hash(event)
    spec.require_valid("audit_event", event, where="审计事件")
    append_jsonl(project_paths.audit, [event])
    return event


def _project_id(project_file: Path) -> str:
    from geo_verify.jsonl import read_json

    project = read_json(project_file)
    project_id = project.get("project_id") if isinstance(project, dict) else None
    if not isinstance(project_id, str):
        raise InputError(f"{project_file} 缺少 project_id")
    return project_id


def verify_chain(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验审计链，返回问题列表（空列表表示完整）。每个问题带事件序号，便于定位。"""
    problems: list[dict[str, Any]] = []
    previous: str | None = None
    for index, event in enumerate(events, start=1):
        if not spec.is_valid("audit_event", event):
            problems.append(
                {"index": index, "kind": "invalid", "message": "不符合 audit_event 契约"}
            )
            previous = event.get("event_hash") if isinstance(event.get("event_hash"), str) else None
            continue
        if event_hash(event) != event["event_hash"]:
            problems.append(
                {
                    "index": index,
                    "kind": "modified",
                    "message": f"事件 {event['event_id']} 内容被改动",
                }
            )
        if event["previous_event_hash"] != previous:
            problems.append(
                {
                    "index": index,
                    "kind": "chain_broken",
                    "message": f"事件 {event['event_id']} 与前一事件的链接断裂（可能有删除、插入或重排）",
                }
            )
        if event["event_id"] != f"evt-{index:06d}":
            problems.append(
                {
                    "index": index,
                    "kind": "sequence",
                    "message": f"事件编号 {event['event_id']} 与位置 {index} 不符",
                }
            )
        previous = event["event_hash"]
    return problems
