"""`geo-verify report`：生成预检报告（只读取项目文件与各命令的结果文件，不重新计算）。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from geo_verify.audit import resolve_now
from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.errors import EXIT_CHECK_FAILED, EXIT_INSUFFICIENT, EXIT_OK, InputError
from geo_verify.jsonl import write_json
from geo_verify.report import csvsafe
from geo_verify.report.document import build_report
from geo_verify.report.html import render_html
from geo_verify.report.inputs import Inputs, load_inputs
from geo_verify.report.observations import COLUMNS, observation_rows

FORMATS: tuple[str, ...] = ("json", "html", "csv")
FILE_NAMES = {"json": "report.json", "html": "report.html", "csv": "observations.csv"}
EXIT_CODES = {
    "pass": EXIT_OK,
    "conditional_pass": EXIT_OK,
    "fail": EXIT_CHECK_FAILED,
    "insufficient_data": EXIT_INSUFFICIENT,
}


def parse_formats(text: str) -> list[str]:
    names = [part.strip() for part in text.split(",")]
    if any(not name for name in names):
        raise InputError(f"--formats 不能有空项：{text!r}；可选 {','.join(FORMATS)}")
    unknown = [name for name in names if name not in FORMATS]
    if unknown:
        raise InputError(
            f"--formats 含不支持的格式：{'、'.join(unknown)}；可选 {','.join(FORMATS)}"
        )
    return [name for name in FORMATS if name in names]


def _text(report: dict[str, Any], outputs: dict[str, str]) -> str:
    verdict = report["verdict"]
    lines = [report["notice"] + "。"]
    if report.get("watermark"):
        lines.append(f"【{report['watermark']}】")
    lines.append(f"预检结论：{verdict['label']}")
    lines.append(verdict["summary"])
    lines.append("判定规则：")
    for item in report["rules"]:
        lines.append(
            f"  [{item['status_label']}] {item['rule_id']} {item['title']}：{item['basis']}"
        )
    if verdict["reservations"]:
        lines.append("保留意见：")
        lines.extend(f"  - {item['rule_id']}：{item['text']}" for item in verdict["reservations"])
    for name, path in outputs.items():
        lines.append(f"已写入 {name.upper()}：{path}")
    return "\n".join(lines)


def _write(name: str, target: Path, report: dict[str, Any], inputs: Inputs) -> None:
    if name == "json":
        write_json(target, report)
    elif name == "html":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_html(report), encoding="utf-8", newline="\n")
    else:
        csvsafe.write_csv(target, COLUMNS, observation_rows(inputs))


def run(args: argparse.Namespace) -> CommandResult:
    formats = parse_formats(args.formats)
    generated_at = None if args.now is None else resolve_now(args.now)
    root = Path(args.project)
    inputs = load_inputs(root)
    report = build_report(inputs, generated_at=generated_at)
    out_dir = Path(args.out_dir) if args.out_dir else root / "out"
    outputs: dict[str, str] = {}
    for name in formats:
        target = out_dir / FILE_NAMES[name]
        _write(name, target, report, inputs)
        outputs[name] = str(target) if args.out_dir else f"out/{FILE_NAMES[name]}"
    data = {**report, "outputs": outputs}
    exit_code = EXIT_CODES[report["verdict"]["code"]]
    return CommandResult(data=data, text=_text(report, outputs), exit_code=exit_code)


def register(subparsers: SubParsers) -> None:
    parser = add_command(
        subparsers,
        "report",
        run,
        help="生成预检报告（只是预检结论，不构成独立第三方验证）",
    )
    parser.add_argument("project", help="项目目录")
    parser.add_argument(
        "--formats", default=",".join(FORMATS), help=f"输出格式，逗号分隔，默认 {','.join(FORMATS)}"
    )
    parser.add_argument("--now", help="报告生成时间（UTC）；不传时报告不含生成时间")
    parser.add_argument("--out-dir", help="输出目录，默认 PROJECT/out")
