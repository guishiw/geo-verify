"""`geo-verify compare`：同题配对的基线对比，版本变化时拒绝比较。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.compare import EXIT_CODES, compare_document, resolve_metrics
from geo_verify.jsonl import write_json
from geo_verify.metrics import METRICS, build_index, parameters_for
from geo_verify.project import paths


def _side_text(side: dict[str, Any]) -> str:
    if side["value"] is None:
        return "无数据"
    return f"{side['value']:.4f}（{side['numerator']}/{side['denominator']}）"


def _row_text(item: dict[str, Any]) -> str:
    name = "整体" if item["platform"] is None else item["platform"]
    parts = [
        f"  {item['metric_id']}（{METRICS[item['metric_id']].title}，{name}）："
        f"基线 {_side_text(item['baseline'])}，当前 {_side_text(item['current'])}"
    ]
    if item["difference"] is not None:
        parts.append(f"，同题配对差值 {item['difference']:+.4f}（{item['paired_queries']} 道题）")
    interval = item["interval"]
    if interval is not None:
        parts.append(
            f"，{interval['level']:.0%} 区间 [{interval['low']:+.4f}, {interval['high']:+.4f}]"
        )
    favorable = {True: "，对目标主体有利", False: "，对目标主体不利", None: ""}[item["favorable"]]
    parts.append(f"：{item['conclusion']}{favorable}")
    return "".join(parts)


def _summary(document: dict[str, Any], out_path: Path) -> str:
    baseline, current = document["baseline"], document["current"]
    lines = [
        f"基线对比（同题配对）：基线 {baseline['project_id']}（题库第 {baseline['prompt_set']['version']} 版，"
        f"有效观测 {baseline['valid_observations']} 条）→ 当前 {current['project_id']}"
        f"（题库第 {current['prompt_set']['version']} 版，有效观测 {current['valid_observations']} 条）："
        f"{document['status_label']}",
        f"理由：{document['reason']}",
        *(_row_text(item) for item in document["results"]),
    ]
    if document["results"]:
        lines.append(
            f"说明：{document['method']['conclusion']}；{document['method']['favorable']}。"
        )
    lines.extend(f"[{item['severity']}] {item['message']}" for item in document["notices"])
    lines.append(f"已写入 {out_path}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> CommandResult:
    baseline = build_index(args.baseline_project)
    current = build_index(args.current_project)
    params = parameters_for(current.project, seed=args.seed, iterations=args.iterations)
    metric_ids, fallback = resolve_metrics(current.project, args.metric)
    document = compare_document(
        baseline, current, params, metric_ids, platform=args.platform, fallback=fallback
    )
    out_path = Path(args.out) if args.out else paths(args.current_project).out_dir / "compare.json"
    write_json(out_path, document)
    return CommandResult(
        data={**document, "output": str(out_path)},
        text=_summary(document, out_path),
        exit_code=EXIT_CODES[document["status"]],
    )


def register(subparsers: SubParsers) -> None:
    parser = add_command(
        subparsers,
        "compare",
        run,
        help="同题配对的基线对比；题库或模型版本变化时拒绝比较，结果写入当前项目的 out/compare.json",
    )
    parser.add_argument("baseline_project", help="基线项目目录")
    parser.add_argument("current_project", help="当前项目目录")
    parser.add_argument(
        "--metric",
        action="append",
        help="比较的指标（可重复）；默认取当前项目预先登记的验收指标，没有则为 mention_rate",
    )
    parser.add_argument("--platform", help="只比较该平台")
    parser.add_argument("--seed", type=int, help="bootstrap 的 seed，默认取当前项目预先登记值")
    parser.add_argument("--iterations", type=int, help="bootstrap 重抽次数，默认取预先登记值")
    parser.add_argument("--out", help="结果文件路径，默认 CURRENT_PROJECT/out/compare.json")
