"""`geo-verify annotate`：规则标注（rules）、人工覆盖（override）、批量导入（import）。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from geo_verify import audit
from geo_verify.annotate import human, run_rules
from geo_verify.cli import CommandResult, SubParsers, add_command, add_group
from geo_verify.errors import EXIT_CHECK_FAILED, EXIT_OK, InputError

MAX_TEXT_ERRORS = 50


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


def _show(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "|".join(str(item) for item in value) or "（空）"
    return str(value)


def _override(args: argparse.Namespace) -> CommandResult:
    data = human.override(
        args.project,
        observation_id=args.observation,
        entity_id=args.entity,
        assignments=args.set,
        annotator=args.annotator,
        version=args.annotator_version,
        reason=args.reason,
        now=audit.resolve_now(args.now),
    )
    lines = [
        f"已追加人工覆盖标注 {data['annotation_id']}（{data['observation_id']}，{data['entity_id']}），"
        f"覆盖 {data['supersedes']}；原有标注行保持不变，审计事件 {data['audit_event_id']}。"
    ]
    lines.extend(
        f"  {key}：{_show(change['from'])} → {_show(change['to'])}"
        for key, change in data["changes"].items()
    )
    return CommandResult(data=data, text="\n".join(lines))


def _template(args: argparse.Namespace) -> CommandResult:
    content = human.template(args.template)
    data: dict[str, Any] = {
        "format": args.template,
        "columns": human.column_help(),
        "content": content,
        "out": None,
    }
    if not args.out:
        return CommandResult(data=data, text=content.rstrip("\n"))
    target = Path(args.out)
    if target.exists():
        raise InputError(f"文件已存在，拒绝覆盖：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    encoding = "utf-8-sig" if args.template == "csv" else "utf-8"
    target.write_text(content, encoding=encoding, newline="")
    data["out"] = target.name
    return CommandResult(data=data, text=f"模板已写入 {target}")


def _import(args: argparse.Namespace) -> CommandResult:
    if args.template:
        return _template(args)
    if not args.project or not args.input:
        raise InputError("需要给出项目目录和 --input 文件；只想要模板时用 --template csv|jsonl")
    if not args.annotator:
        raise InputError("必须写明标注者（--annotator）")
    report = human.import_file(
        args.project,
        Path(args.input),
        annotator=args.annotator,
        version=args.annotator_version,
        fmt=args.format,
        now=audit.resolve_now(args.now),
    )
    data = report.to_dict()
    lines = [
        f"导入标注者 {report.annotator['id']} 的人工标注（{report.source}）：共 {report.rows} 行，"
        f"写入 {report.written} 条，内容与已有标注相同而跳过 {report.skipped_identical} 条，"
        f"文件内重复 {report.skipped_duplicate_in_input} 条，非法 {report.rejected_rows} 行。"
        "批量导入不覆盖其他标注者的标注。"
    ]
    for item in report.errors[:MAX_TEXT_ERRORS]:
        column = f" [{item.column}]" if item.column else ""
        lines.append(f"  第 {item.line} 行{column}：{item.reason}")
    if len(report.errors) > MAX_TEXT_ERRORS:
        lines.append(f"  ……共 {len(report.errors)} 处错误，完整清单见 --json 输出")
    return CommandResult(
        data=data,
        text="\n".join(lines),
        exit_code=EXIT_CHECK_FAILED if report.rejected_rows else EXIT_OK,
    )


def register(subparsers: SubParsers) -> None:
    group = add_group(subparsers, "annotate", help="标注：规则判断、人工覆盖、批量导入人工标注")
    rules = add_command(
        group,
        "rules",
        _rules,
        help="按规则为每条有效观测 ×（目标主体、竞品及其产品）生成标注；已有生效标注的不重复生成",
    )
    rules.add_argument("project", help="项目目录")
    rules.add_argument("--now", help="标注时间（UTC），默认取当前时间")

    override = add_command(
        group,
        "override",
        _override,
        help="人工覆盖：追加一条人工标注并 supersede 当前生效标注，写审计事件；历史全部保留",
    )
    override.add_argument("project", help="项目目录")
    override.add_argument("--observation", required=True, help="观测编号")
    override.add_argument("--entity", required=True, help="实体编号")
    override.add_argument(
        "--set",
        required=True,
        help="要改的字段，如 recommended=false,rank=null,risk.confirmed=true；未指定的沿用原值",
    )
    override.add_argument("--annotator", required=True, help="标注者（人工）")
    override.add_argument(
        "--annotator-version", default=human.DEFAULT_VERSION, help="标注规范版本，默认 manual"
    )
    override.add_argument("--reason", required=True, help="覆盖理由（写入审计事件）")
    override.add_argument("--now", help="覆盖时间（UTC），默认取当前时间")

    importer = add_command(
        group,
        "import",
        _import,
        help="批量导入一名标注者的人工标注（CSV / JSONL），逐行报错；或输出模板",
    )
    importer.add_argument("project", nargs="?", help="项目目录")
    importer.add_argument("--input", help="待导入的 CSV 或 JSONL 文件")
    importer.add_argument("--format", choices=["csv", "jsonl"], help="文件格式，默认按扩展名判断")
    importer.add_argument("--annotator", help="标注者（人工）")
    importer.add_argument(
        "--annotator-version", default=human.DEFAULT_VERSION, help="标注规范版本，默认 manual"
    )
    importer.add_argument("--now", help="标注时间（UTC），默认取当前时间")
    importer.add_argument("--template", choices=["csv", "jsonl"], help="输出导入模板后退出")
    importer.add_argument("--out", help="模板输出文件，默认打印到标准输出")
