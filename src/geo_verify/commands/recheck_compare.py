"""`geo-verify recheck-compare`：把人工复核结果与原标注比对，输出一致率。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from geo_verify import project as project_module
from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.errors import EXIT_CHECK_FAILED, EXIT_INSUFFICIENT, EXIT_OK
from geo_verify.report.recheck import FIELDS, RESULT_PATH
from geo_verify.report.recheck_compare import FORMATS, compare, parse_fields, write_result

MAX_LISTED = 50


def _difference_text(item: dict[str, Any]) -> str:
    parts = [
        f"{diff['field']} {_show(diff['original'])} → {_show(diff['rechecked'])}"
        for diff in item["differences"]
    ]
    return f"  {item['observation_id']} / {item['entity_id']}：{'；'.join(parts)}"


def _show(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _text(result: dict[str, Any]) -> str:
    lines = [
        f"抽样复核比对（字段：{'、'.join(result['fields'])}）：计划 {result['planned']} 项，"
        f"已复核 {result['checked']} 项（一致 {result['agreed']} 项，不一致 {result['disagreed']} 项），"
        f"未复核 {result['not_checked']} 项。"
    ]
    if result["pass_rate"] is None:
        lines.append(f"一致率无法计算：{result['null_reason']}。")
    else:
        lines.append(
            f"一致率 {result['pass_rate']:.4f}（{result['agreed']}/{result['checked']}）。"
            "本命令只计算一致率，不判定是否达标（门槛属于项目登记的口径）。"
        )
    if result["not_checked"]:
        lines.append(
            f"注意：计划内还有 {result['not_checked']} 项没有复核，一致率只覆盖已复核的项。"
        )
    disagreed = [item for item in result["items"] if not item["agree"]]
    if disagreed:
        lines.append("不一致的项：")
        lines.extend(_difference_text(item) for item in disagreed[:MAX_LISTED])
    if result["unplanned"]:
        lines.append(f"另有 {result['unplanned']} 项不在计划内，未计入一致率。")
    if result["plan_hash"] is None:
        lines.append("提示：没有复核计划文件，文件中每个合法的复核行都计入。")
    errors = result["errors"]
    if errors:
        lines.append(f"复核文件有 {result['input']['invalid_rows']} 行不合法（未计入）：")
        for item in errors[:MAX_LISTED]:
            column = f" {item['column']}" if item["column"] else ""
            lines.append(f"  第 {item['line']} 行{column}：{item['message']}")
        if len(errors) > MAX_LISTED:
            lines.append(f"  ……仅列出前 {MAX_LISTED} 条，完整列表见 {RESULT_PATH}。")
    lines.append(f"结果已写入 {RESULT_PATH}。")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> CommandResult:
    fields = parse_fields(args.fields)
    root = Path(args.project)
    project_module.paths(root)
    result = compare(root, Path(args.input), fields, args.format)
    write_result(root, result)
    if result["errors"]:
        exit_code = EXIT_CHECK_FAILED
    elif result["checked"] == 0:
        exit_code = EXIT_INSUFFICIENT
    else:
        exit_code = EXIT_OK
    data = {**result, "outputs": {"result": RESULT_PATH}}
    return CommandResult(data=data, text=_text(result), exit_code=exit_code)


def register(subparsers: SubParsers) -> None:
    parser = add_command(
        subparsers,
        "recheck-compare",
        run,
        help="把人工复核结果与原标注比对，输出一致率（recheck/result.json）",
    )
    parser.add_argument("project", help="项目目录")
    parser.add_argument("--input", required=True, help="人工复核结果（CSV 或 JSONL）")
    parser.add_argument(
        "--fields",
        default=",".join(FIELDS),
        help=f"比对的字段，逗号分隔，默认 {','.join(FIELDS)}",
    )
    parser.add_argument("--format", choices=FORMATS, help="输入格式，默认按扩展名判断")
