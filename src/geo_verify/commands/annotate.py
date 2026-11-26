"""`geo-verify annotate`：规则标注（rules）。"""

from __future__ import annotations

import argparse

from geo_verify import audit
from geo_verify.annotate import run_rules
from geo_verify.cli import CommandResult, SubParsers, add_command, add_group


def _rules(args: argparse.Namespace) -> CommandResult:
    report = run_rules.annotate_project(args.project, now=audit.resolve_now(args.now))
    data = report.to_dict()
    lines = [
        f"规则标注（{data['annotator']['id']} {data['rule_version']}）："
        f"观测 {report.observations_total} 条，其中可标注 {report.observations_eligible} 条"
        f"（跳过无效 {report.skipped_invalid} 条、无回答 {report.skipped_no_answer} 条）；"
        f"实体 {len(report.entity_ids)} 个：{'、'.join(report.entity_ids)}。",
        f"新写入 {report.written} 条标注，已有生效标注而跳过 {report.skipped_existing} 个（观测，实体）。",
    ]
    for entity_id, counts in report.summary.items():
        lines.append(
            f"  {entity_id}：新标注 {counts['annotated']}，提及 {counts['mentioned']}，"
            f"推荐 {counts['recommended']}，否定 {counts['negated']}，有位次 {counts['ranked']}"
        )
    if not report.written:
        lines.append("没有新标注，未写入审计事件。")
    return CommandResult(data=data, text="\n".join(lines))


def register(subparsers: SubParsers) -> None:
    group = add_group(subparsers, "annotate", help="标注：规则判断提及、推荐、否定与位次")
    rules = add_command(
        group,
        "rules",
        _rules,
        help="按规则为每条有效观测 ×（目标主体、竞品及其产品）生成标注；已有生效标注的不重复生成",
    )
    rules.add_argument("project", help="项目目录")
    rules.add_argument("--now", help="标注时间（UTC），默认取当前时间")
