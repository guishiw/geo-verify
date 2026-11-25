"""`geo-verify import`：从 CSV / JSONL 导入观测，或输出导入模板。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from geo_verify import audit, project
from geo_verify.canon import sha256_file
from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.collect import fileimport
from geo_verify.errors import EXIT_CHECK_FAILED, InputError

MAX_JSON_ERRORS = 1000
MAX_TEXT_ERRORS = 50


def _template(args: argparse.Namespace) -> CommandResult:
    content = fileimport.template(args.template)
    data: dict[str, Any] = {
        "format": args.template,
        "columns": fileimport.column_help(),
        "content": content,
        "out": None,
    }
    if not args.out:
        return CommandResult(data=data, text=content.rstrip("\n"))
    target = Path(args.out)
    if target.exists():
        raise InputError(f"文件已存在，拒绝覆盖：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # CSV 带 BOM，便于表格软件正确识别中文；导入时会自动去掉
    encoding = "utf-8-sig" if args.template == "csv" else "utf-8"
    target.write_text(content, encoding=encoding, newline="")
    data["out"] = target.name
    return CommandResult(data=data, text=f"模板已写入 {target}")


def run(args: argparse.Namespace) -> CommandResult:
    if args.template:
        return _template(args)
    if not args.project or not args.input:
        raise InputError("需要给出项目目录和 --input 文件；只想要模板时用 --template csv|jsonl")
    paths = project.paths(args.project)
    proj = project.load_project(args.project)
    source = Path(args.input)
    before = sha256_file(paths.observations) if paths.observations.exists() else None
    report, sink = fileimport.import_file(
        args.project, source, fmt=args.format, operator=args.operator
    )
    counts = sink.counts()
    errors = [item.to_dict() for item in report.errors]
    data: dict[str, Any] = {
        "input": report.source,
        "format": report.fmt,
        "resume": bool(args.resume),
        "rows": report.rows,
        **counts,
        "skipped_existing": report.skipped_existing,
        "skipped_duplicate_in_input": len(report.duplicates),
        "duplicates": report.duplicates,
        "rejected_rows": report.rejected_rows,
        "errors": errors[:MAX_JSON_ERRORS],
        "errors_total": len(errors),
        "errors_truncated": len(errors) > MAX_JSON_ERRORS,
        "errors_file": fileimport.ERRORS_RELPATH,
        "prompt_text_differs": report.prompt_text_differs,
        "audit_event_id": None,
    }
    if counts["written"]:
        operator = args.operator.strip() if args.operator and args.operator.strip() else None
        event = audit.append_event(
            args.project,
            actor=operator or "geo-verify import（未指明操作人）",
            action="import",
            target_type="observations",
            target_id=report.source,
            reason=f"从 {report.source} 导入观测",
            occurred_at=audit.resolve_now(args.now),
            before_hash=before,
            after_hash=sha256_file(paths.observations),
            details={
                "source_file": report.source,
                "source_sha256": sha256_file(source),
                "format": report.fmt,
                "rows": report.rows,
                **counts,
                "skipped_existing": report.skipped_existing,
                "skipped_duplicate_in_input": len(report.duplicates),
                "rejected_rows": report.rejected_rows,
            },
            synthetic=True if proj.get("synthetic") is True else None,
        )
        data["audit_event_id"] = event["event_id"]

    lines = [
        f"导入 {report.source}：共 {report.rows} 行，写入 {counts['written']} 条"
        f"（有效 {counts['valid']}，无效 {counts['invalid']}），"
        f"跳过已存在 {report.skipped_existing} 条，文件内重复 {len(report.duplicates)} 条，"
        f"非法 {report.rejected_rows} 行。"
    ]
    if report.prompt_text_differs:
        lines.append(
            f"注意：{report.prompt_text_differs} 条观测的提问原文与封存题目不一致（已按原样记录）。"
        )
    for item in errors[:MAX_TEXT_ERRORS]:
        column = f" [{item['column']}]" if item["column"] else ""
        lines.append(f"  第 {item['line']} 行{column}：{item['reason']}")
    if len(errors) > MAX_TEXT_ERRORS:
        lines.append(f"  ……共 {len(errors)} 处错误，完整清单见 {fileimport.ERRORS_RELPATH}")
    return CommandResult(
        data=data,
        text="\n".join(lines),
        exit_code=EXIT_CHECK_FAILED if report.rejected_rows else 0,
    )


def register(subparsers: SubParsers) -> None:
    parser = add_command(
        subparsers,
        "import",
        run,
        help="从 CSV / JSONL 导入观测（逐行报错、按 trace_id 去重、可续导），或输出模板",
    )
    parser.add_argument("project", nargs="?", help="项目目录")
    parser.add_argument("--input", help="待导入的 CSV 或 JSONL 文件")
    parser.add_argument("--format", choices=["csv", "jsonl"], help="文件格式，默认按扩展名判断")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="续导：已存在的 trace_id 被跳过并计数（重复执行时默认也是如此，此开关用于明确表意）",
    )
    parser.add_argument("--operator", help="采集人；文件中 operator 列为空时使用")
    parser.add_argument("--now", help="审计事件时间（UTC），默认取当前时间")
    parser.add_argument("--template", choices=["csv", "jsonl"], help="输出导入模板后退出")
    parser.add_argument("--out", help="模板输出文件，默认打印到标准输出")
