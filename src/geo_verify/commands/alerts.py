"""`geo-verify alerts`：比较两个窗口（或两个自然日），识别异常并列出引用域名变化。

有告警退出 1，无告警 0，无法比较（可比较的窗口或日期不足）退出 3；无法比较时也写出结果文件，
status = "insufficient"，避免报告读到旧结论。
引用域名变化只作为待核查的相关线索输出，不表示因果。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from geo_verify.alerts import RULES, compute_alerts
from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.errors import EXIT_CHECK_FAILED, EXIT_OK, InsufficientData
from geo_verify.jsonl import write_json
from geo_verify.metrics import build_index
from geo_verify.project import paths

TEXT_LEAD_LIMIT = 20


def _side(document: dict[str, Any], side: str, label: str) -> str:
    info = document[side]
    if document["mode"] == "window":
        name = f"{label}窗口 {info['window_id']}"
    else:
        name = f"{label}日 {info['date']}"
    return f"{name}（有效观测 {info['valid_observations']} 条）"


def _summary(document: dict[str, Any], out_path: Path) -> str:
    scope = f"，只看平台 {document['platform']}" if document["platform"] else ""
    lines = [
        f"项目 {document['project_id']}：比较{_side(document, 'baseline', '基线')}"
        f"与{_side(document, 'current', '当前')}{scope}。"
    ]
    alerts = document["alerts"]
    if alerts:
        lines.append(f"发现 {len(alerts)} 条告警：")
        lines.extend(
            f"  [{item['severity']}] {item['rule_id']}（{RULES[item['rule_id']]}）{item['message']}"
            for item in alerts
        )
    else:
        lines.append("未发现告警。")
    changes = document["citation_changes"]
    lines.append(
        f"引用域名变化：新增 {len(changes['added'])} 个、消失 {len(changes['removed'])} 个、"
        f"次数变化 {len(changes['changed'])} 个。{changes['note']}"
    )
    leads = document["leads"]
    if leads:
        lines.append("待核查的相关线索：")
        lines.extend(f"  - {item['text']}" for item in leads[:TEXT_LEAD_LIMIT])
        if len(leads) > TEXT_LEAD_LIMIT:
            lines.append(f"  ……仅列出前 {TEXT_LEAD_LIMIT} 条，完整内容见结果文件。")
    lines.append(document["notice"])
    lines.append(f"结果已写入 {out_path}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> CommandResult:
    index = build_index(args.project, args.prompt_set_version)
    document = compute_alerts(
        index,
        baseline_window=args.baseline_window,
        current_window=args.current_window,
        baseline_date=args.baseline_date,
        current_date=args.current_date,
        platform=args.platform,
        threshold_items=args.threshold or [],
    )
    out_path = Path(args.out) if args.out else paths(args.project).out_dir / "alerts.json"
    write_json(out_path, document)
    if document["status"] == "insufficient":
        # 结果文件照样写出（status = insufficient），命令仍按“数据不足”退出 3
        raise InsufficientData(
            f"{document['reason']}；结果已写入 {out_path}（status = insufficient）",
            details={**document["details"], "output": str(out_path)},
        )
    return CommandResult(
        data={**document, "output": str(out_path)},
        text=_summary(document, out_path),
        exit_code=EXIT_CHECK_FAILED if document["status"] == "alert" else EXIT_OK,
    )


def register(subparsers: SubParsers) -> None:
    parser = add_command(
        subparsers,
        "alerts",
        run,
        help="识别异常并输出引用域名变化（待核查的相关线索），写入 out/alerts.json",
        description=(
            "比较两个时间窗口（默认最近两个）或项目时区下的两个自然日，识别提及率骤降与掉位、"
            "错误负面扩散、引用大面积失效、竞品异常提升、同平台口径冲突。"
            "有告警退出 1，无告警 0，无法比较 3。"
        ),
    )
    parser.add_argument("project", help="项目目录")
    parser.add_argument("--baseline-window", help="基线窗口；只给当前窗口时取它前一个窗口")
    parser.add_argument("--current-window", help="当前窗口；只给基线窗口时取它后一个窗口")
    parser.add_argument(
        "--baseline-date", help="基线日（项目时区，YYYY-MM-DD）；只给当前日时取前一天"
    )
    parser.add_argument(
        "--current-date", help="当前日（项目时区，YYYY-MM-DD）；只给基线日时取后一天"
    )
    parser.add_argument("--platform", help="只比较该平台")
    parser.add_argument("--prompt-set-version", type=int, help="封存题库版本号，默认最新版本")
    parser.add_argument(
        "--threshold",
        action="append",
        metavar="规则.键=值",
        help="覆盖阈值，可重复，如 mention_drop.alpha=0.1（优先于 project.json 的 ext.alerts.thresholds）",
    )
    parser.add_argument("--out", help="结果文件路径，默认 PROJECT/out/alerts.json")
