"""`geo-verify validate`：按数据契约校验 JSON / JSONL 文件。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from geo_verify import spec
from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.errors import EXIT_CHECK_FAILED, InputError
from geo_verify.jsonl import iter_jsonl, read_json

MAX_REPORTED = 50


def _records(path: Path) -> list[tuple[str, Any]]:
    if path.suffix == ".jsonl":
        return [(f"第 {number} 行", record) for number, record in iter_jsonl(path)]
    value = read_json(path)
    if isinstance(value, list):
        return [(f"第 {index + 1} 项", item) for index, item in enumerate(value)]
    return [("文件", value)]


def run(args: argparse.Namespace) -> CommandResult:
    path = Path(args.path)
    contract: str = args.type
    if contract not in spec.CONTRACTS:
        raise InputError(f"未知契约：{contract}；可选：{', '.join(spec.CONTRACTS)}")
    records = _records(path)
    problems: list[dict[str, Any]] = []
    invalid = 0
    for where, record in records:
        issues = spec.iter_issues(contract, record)
        if not issues:
            continue
        invalid += 1
        if len(problems) < MAX_REPORTED:
            problems.append({"where": where, "issues": [issue.to_dict() for issue in issues]})
    data = {
        "contract": contract,
        "path": str(path),
        "records": len(records),
        "invalid": invalid,
        "problems": problems,
        "problems_truncated": invalid > len(problems),
    }
    if invalid == 0:
        return CommandResult(
            data=data, text=f"通过：{len(records)} 条记录全部符合契约 {contract}。"
        )
    lines = [f"未通过：{len(records)} 条记录中有 {invalid} 条不符合契约 {contract}。"]
    for problem in problems:
        for issue in problem["issues"]:
            lines.append(f"  {problem['where']} {issue['path'] or '/'}：{issue['message']}")
    if invalid > len(problems):
        lines.append(f"  ……仅列出前 {MAX_REPORTED} 条。")
    return CommandResult(data=data, text="\n".join(lines), exit_code=EXIT_CHECK_FAILED)


def register(subparsers: SubParsers) -> None:
    parser = add_command(subparsers, "validate", run, help="按数据契约校验 JSON 或 JSONL 文件")
    parser.add_argument("path", help="待校验文件（.json 或 .jsonl）")
    parser.add_argument("--type", required=True, help="契约名，如 observation、project")
