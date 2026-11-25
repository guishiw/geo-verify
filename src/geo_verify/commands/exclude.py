"""`geo-verify exclude`：剔除观测。只写审计事件，不改动 observations.jsonl。"""

from __future__ import annotations

import argparse
from pathlib import Path

from geo_verify import audit, project
from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.errors import InputError


def _requested_ids(args: argparse.Namespace) -> list[str]:
    if args.ids_file:
        path = Path(args.ids_file)
        if not path.is_file():
            raise InputError(f"文件不存在：{path}")
        raw = path.read_text(encoding="utf-8").replace("\n", ",")
    else:
        raw = args.ids
    ids: list[str] = []
    for piece in raw.split(","):
        item = piece.strip()
        if item and item not in ids:
            ids.append(item)
    if not ids:
        raise InputError("没有给出要剔除的观测编号")
    return ids


def run(args: argparse.Namespace) -> CommandResult:
    proj = project.load_project(args.project)
    requested = _requested_ids(args)
    known = {item["observation_id"] for item in project.load_observations(args.project)}
    unknown = [item for item in requested if item not in known]
    if unknown:
        raise InputError(
            f"观测编号不存在：{'、'.join(unknown)}", details={"unknown_observation_ids": unknown}
        )
    excluded = project.exclusions(project.load_audit(args.project))
    already = [item for item in requested if item in excluded]
    fresh = [item for item in requested if item not in excluded]
    registered = args.rule_id in {
        rule["rule_id"] for rule in proj["preregistration"]["exclusion_rules"]
    }
    data = {
        "rule_id": args.rule_id,
        "rule_registered": registered,
        "count": len(fresh),
        "observation_ids": fresh,
        "already_excluded": already,
        "audit_event_id": None,
    }
    lines: list[str] = []
    if fresh:
        event = audit.append_event(
            args.project,
            actor=args.actor,
            action="exclude",
            target_type="observation",
            target_id="batch",
            reason=args.reason,
            occurred_at=audit.resolve_now(args.now),
            details={
                "rule_id": args.rule_id,
                "count": len(fresh),
                "observation_ids": fresh,
                "rule_registered": registered,
            },
            synthetic=True if proj.get("synthetic") is True else None,
        )
        data["audit_event_id"] = event["event_id"]
        lines.append(
            f"已按规则 {args.rule_id} 剔除 {len(fresh)} 条观测；"
            "剔除只记入审计事件，原始观测文件未改动。"
        )
    else:
        lines.append("没有新剔除的观测，未写入审计事件。")
    if already:
        lines.append(f"{len(already)} 条观测此前已被剔除，不重复计数：{'、'.join(already)}")
    if not registered:
        lines.append(
            f"注意：规则 {args.rule_id} 未预先登记在 project.json 的 exclusion_rules 中，"
            "报告的预检结论会因此不通过。"
        )
    return CommandResult(data=data, text="\n".join(lines))


def register(subparsers: SubParsers) -> None:
    parser = add_command(subparsers, "exclude", run, help="剔除观测（只写审计事件，不改原始记录）")
    parser.add_argument("project", help="项目目录")
    parser.add_argument("--rule-id", required=True, help="剔除规则编号")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--ids", help="观测编号，逗号分隔")
    source.add_argument("--ids-file", help="每行一个观测编号的文本文件")
    parser.add_argument("--actor", required=True, help="操作人")
    parser.add_argument("--reason", required=True, help="剔除理由")
    parser.add_argument("--now", help="操作时间（UTC），默认取当前时间")
