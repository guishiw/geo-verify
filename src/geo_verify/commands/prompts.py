"""`geo-verify prompts lint | seal | diff`：题库检查、封存与版本差异。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from geo_verify import audit, project
from geo_verify.cli import CommandResult, SubParsers, add_command, add_group
from geo_verify.errors import EXIT_CHECK_FAILED
from geo_verify.seal.lint import DEFAULT_NEAR_THRESHOLD, RULE_LABELS, lint_prompts
from geo_verify.seal.sealing import diff_versions, read_draft, seal_prompts

_SEVERITY_LABELS = {"error": "错误", "warning": "提醒"}


def _finding_lines(findings: list[dict[str, Any]]) -> list[str]:
    return [
        f"  [{_SEVERITY_LABELS[item['severity']]}] {RULE_LABELS[item['rule']]}：{item['message']}"
        for item in findings
    ]


def run_lint(args: argparse.Namespace) -> CommandResult:
    paths = project.paths(args.project)
    entities = project.load_entities(args.project)
    source = Path(args.file) if args.file else paths.draft
    prompts = read_draft(source)
    findings = [
        item.to_dict()
        for item in lint_prompts(prompts, entities, near_threshold=args.near_threshold)
    ]
    errors = sum(item["severity"] == "error" for item in findings)
    warnings = len(findings) - errors
    data = {
        "file": source.name,
        "prompts": len(prompts),
        "findings": findings,
        "errors": errors,
        "warnings": warnings,
    }
    if errors:
        head = f"检查未通过：{len(prompts)} 道题，{errors} 个错误，{warnings} 个提醒。"
    elif warnings:
        head = f"通过（有 {warnings} 个提醒）：{len(prompts)} 道题没有阻止封存的问题。"
    else:
        head = f"通过：{len(prompts)} 道题未发现问题。"
    return CommandResult(
        data=data,
        text="\n".join([head, *_finding_lines(findings)]),
        exit_code=EXIT_CHECK_FAILED if errors else 0,
    )


def run_seal(args: argparse.Namespace) -> CommandResult:
    data = seal_prompts(
        args.project,
        by=args.by,
        reason=args.reason,
        now=audit.resolve_now(args.now),
        near_threshold=args.near_threshold,
    )
    lines: list[str]
    if data["refused"]:
        lines = [f"拒绝封存：题库检查有 {data['errors']} 个错误，请修正后重试。"]
    elif data["sealed"]:
        lines = [
            f"已封存第 {data['version']} 版题库（{data['prompts']} 题），"
            f"内容哈希 {data['content_hash']}。"
        ]
        if "diff_file" in data:
            lines.append(f"差异记录：{data['diff_file']}")
    else:
        lines = [f"题库内容无变化，未产生新版本（当前第 {data['version']} 版）。"]
    if data["preregistration_changed"]:
        lines.append(
            "注意：project.json 的预先登记内容与最近一次封存时登记的哈希不一致，"
            "说明口径在封存后被改动过。"
        )
    lines.extend(_finding_lines(data["findings"]))
    return CommandResult(
        data=data,
        text="\n".join(lines),
        exit_code=EXIT_CHECK_FAILED if data["refused"] else 0,
    )


def run_diff(args: argparse.Namespace) -> CommandResult:
    data = diff_versions(args.project, args.from_version, args.to_version)
    lines = [
        f"第 {data['from_version']} 版 → 第 {data['to_version']} 版："
        f"新增 {len(data['added'])} 题，删除 {len(data['removed'])} 题，"
        f"修改 {len(data['modified'])} 题，未变 {data['unchanged']} 题。"
    ]
    lines.extend(f"  + {item['query_id']}：{item['text']}" for item in data["added"])
    lines.extend(f"  - {item['query_id']}：{item['text']}" for item in data["removed"])
    lines.extend(
        f"  ~ {item['query_id']}（变化字段：{'、'.join(item['changed_fields'])}）"
        for item in data["modified"]
    )
    return CommandResult(data=data, text="\n".join(lines))


def _add_threshold(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--near-threshold",
        type=float,
        default=DEFAULT_NEAR_THRESHOLD,
        help=f"近似重复的相似度阈值，默认 {DEFAULT_NEAR_THRESHOLD}",
    )


def register(subparsers: SubParsers) -> None:
    group = add_group(subparsers, "prompts", help="题库检查、封存与版本差异")

    lint = add_command(group, "lint", run_lint, help="检查草稿题库")
    lint.add_argument("project", help="项目目录")
    lint.add_argument("--file", help="待检查的题库文件，默认 prompts/draft.jsonl")
    _add_threshold(lint)

    seal = add_command(group, "seal", run_seal, help="封存草稿题库（先检查，有错误则拒绝）")
    seal.add_argument("project", help="项目目录")
    seal.add_argument("--by", required=True, help="封存人")
    seal.add_argument("--reason", help="变更理由；已有封存版本且内容有变化时必填")
    seal.add_argument("--now", help="封存时间（UTC，如 2026-10-02T03:00:00Z），默认取当前时间")
    _add_threshold(seal)

    diff = add_command(group, "diff", run_diff, help="比较两个封存版本")
    diff.add_argument("project", help="项目目录")
    diff.add_argument("--from", dest="from_version", type=int, help="起始版本号")
    diff.add_argument("--to", dest="to_version", type=int, help="目标版本号")
