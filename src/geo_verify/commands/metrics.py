"""`geo-verify metrics`：计算全部指标，或下钻某个指标的分子、分母编号列表。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.errors import InputError
from geo_verify.jsonl import write_json
from geo_verify.metrics import (
    METRICS,
    ROLE_LABELS,
    Engine,
    Scope,
    build_index,
    compute_document,
    parameters_for,
)
from geo_verify.project import paths

METHOD_LABELS = {
    "wilson": "Wilson 区间",
    "cluster_bootstrap": "按题聚类 bootstrap",
    "paired_cluster_bootstrap": "同题配对聚类 bootstrap",
}
TEXT_ID_LIMIT = 200
_SCOPE_OPTIONS = ("platform", "channel", "window", "date")


def format_value(result: dict[str, Any]) -> str:
    """一条结果的简短中文描述：值、分子分母、区间、样本是否充分。"""
    if result["value"] is None:
        return f"无结果（{result['null_reason']}）"
    parts = [f"{result['value']:.4f}"]
    if result["numerator"] is not None:
        parts.append(f"（{result['numerator']:g}/{result['denominator']:g}）")
    interval = result["interval"]
    if interval is not None:
        parts.append(
            f"，{interval['level']:.0%} 区间 [{interval['low']:.4f}, {interval['high']:.4f}]"
            f"（{METHOD_LABELS[interval['method']]}）"
        )
    if not result["sufficiency"]["sufficient"]:
        parts.append("，样本不足")
    return "".join(parts)


def _summary(document: dict[str, Any], out_path: Path) -> str:
    counts = document["observations"]
    lines = [
        f"项目 {document['project_id']}：题库第 {document['prompt_set']['version']} 版，"
        f"观测 {counts['total']} 条，其中有效 {counts['valid']} 条。",
        "整体范围的结果：",
    ]
    for result in document["results"]:
        scope = result["scope"]
        if any(scope[key] is not None for key in ("platform", "channel", "window_id", "date")):
            continue
        definition = METRICS[result["metric_id"]]
        lines.append(
            f"  {result['metric_id']}（{definition.title}）：{format_value(result)}"
            f"；{ROLE_LABELS[result['role']]}"
        )
    if document["notices"]:
        lines.append("提示：")
        lines.extend(f"  [{item['severity']}] {item['message']}" for item in document["notices"])
    lines.append(f"共 {len(document['results'])} 条结果，已写入 {out_path}")
    return "\n".join(lines)


def _drilldown(engine: Engine, args: argparse.Namespace) -> CommandResult:
    scope = Scope(
        platform=args.platform, channel=args.channel, window_id=args.window, date=args.date
    )
    computation = engine.compute(args.drilldown, scope)
    result = computation.result
    data = {
        "metric_id": args.drilldown,
        "scope": result["scope"],
        "unit": result["drilldown"]["unit"],
        "numerator_ids": computation.numerator_ids,
        "denominator_ids": computation.denominator_ids,
        "numerator_ids_hash": result["drilldown"]["numerator_ids_hash"],
        "denominator_ids_hash": result["drilldown"]["denominator_ids_hash"],
        "records": engine.records(computation),
        "result": result,
    }
    numerator_ids = computation.numerator_ids or []
    denominator_ids = computation.denominator_ids or []
    in_numerator = set(numerator_ids)
    lines = [
        f"{args.drilldown}（{METRICS[args.drilldown].title}）：{format_value(result)}",
        f"分子 {len(numerator_ids)} 条，分母 {len(denominator_ids)} 条"
        f"（单位：{result['drilldown']['unit']}；“*” 表示在分子中）",
        f"分子编号列表哈希：{result['drilldown']['numerator_ids_hash']}",
        f"分母编号列表哈希：{result['drilldown']['denominator_ids_hash']}",
    ]
    lines.extend(
        f"  {'*' if item in in_numerator else ' '} {item}"
        for item in denominator_ids[:TEXT_ID_LIMIT]
    )
    if len(denominator_ids) > TEXT_ID_LIMIT:
        lines.append(f"  ……仅列出前 {TEXT_ID_LIMIT} 条，完整列表请加 --json。")
    return CommandResult(data=data, text="\n".join(lines))


def run(args: argparse.Namespace) -> CommandResult:
    scoped = [name for name in _SCOPE_OPTIONS if getattr(args, name) is not None]
    if args.drilldown is None and scoped:
        raise InputError(
            "--platform、--channel、--window、--date 只能与 --drilldown 一起使用；"
            "不加 --drilldown 时会计算全部范围"
        )
    index = build_index(args.project, args.prompt_set_version)
    params = parameters_for(index.project, seed=args.seed, iterations=args.iterations)
    if args.drilldown is not None:
        return _drilldown(Engine(index, params), args)
    document = compute_document(index, params)
    out_path = Path(args.out) if args.out else paths(args.project).out_dir / "metrics.json"
    write_json(out_path, document)
    return CommandResult(
        data={**document, "output": str(out_path)}, text=_summary(document, out_path)
    )


def register(subparsers: SubParsers) -> None:
    parser = add_command(
        subparsers,
        "metrics",
        run,
        help="计算全部指标并写入 out/metrics.json；或用 --drilldown 列出分子、分母编号",
    )
    parser.add_argument("project", help="项目目录")
    parser.add_argument("--prompt-set-version", type=int, help="封存题库版本号，默认最新版本")
    parser.add_argument("--seed", type=int, help="bootstrap 的 seed，默认取预先登记值")
    parser.add_argument("--iterations", type=int, help="bootstrap 重抽次数，默认取预先登记值")
    parser.add_argument("--out", help="结果文件路径，默认 PROJECT/out/metrics.json")
    parser.add_argument("--drilldown", metavar="METRIC", help="列出该指标的分子、分母编号列表")
    parser.add_argument("--platform", help="下钻范围：平台")
    parser.add_argument("--channel", help="下钻范围：采集通道")
    parser.add_argument("--window", help="下钻范围：时间窗口")
    parser.add_argument("--date", help="下钻范围：项目时区下的自然日（YYYY-MM-DD）")
