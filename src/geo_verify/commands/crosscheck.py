"""`geo-verify crosscheck`：双通道交叉验证，样本不足时不可判定。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.crosscheck import (
    EXIT_CODES,
    FORMULA_NOTE,
    FORMULA_TEXT,
    crosscheck_document,
    resolve_config,
)
from geo_verify.jsonl import write_json
from geo_verify.metrics import build_index, parameters_for
from geo_verify.project import paths

METHOD_LABELS = {
    "paired_cluster_bootstrap": "同题配对聚类 bootstrap",
    "unpaired_cluster_bootstrap": "两通道各自按题重抽",
}


def _side_text(side: dict[str, Any]) -> str:
    if side["value"] is None:
        return f"{side['channel']} 无数据"
    return f"{side['channel']} {side['value']:.4f}（{side['numerator']}/{side['denominator']}）"


def _row_text(item: dict[str, Any]) -> str:
    name = (
        f"合并（{'、'.join(item['merged_platforms']) or '无'}）"
        if item["platform"] is None
        else item["platform"]
    )
    parts = [f"  {name}：{_side_text(item['a'])}，{_side_text(item['b'])}"]
    if item["deviation"] is not None:
        parts.append(f"，偏离度 {item['deviation']:.4f}，差值 A − B = {item['difference']:+.4f}")
    interval = item["interval"]
    if interval is not None:
        parts.append(
            f"，{interval['level']:.0%} 区间 [{interval['low']:+.4f}, {interval['high']:+.4f}]"
            f"（{METHOD_LABELS[interval['method']]}）"
        )
    parts.append(f"：{item['status_label']}（{item['reason']}）")
    return "".join(parts)


def _summary(document: dict[str, Any], out_path: Path) -> str:
    lines = [
        f"双通道交叉验证：指标 {document['metric_id']}，通道 A = {document['channel_a']}，"
        f"B = {document['channel_b']}，阈值 {document['threshold']:g}"
        f"{'' if document['preregistered'] else '（非预先登记口径）'}：{document['status_label']}",
        f"公式：{FORMULA_TEXT}。{FORMULA_NOTE}",
        *(_row_text(item) for item in document["results"]),
        f"结论理由：{document['reason']}",
    ]
    lines.extend(f"[{item['severity']}] {item['message']}" for item in document["notices"])
    lines.append(f"已写入 {out_path}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> CommandResult:
    index = build_index(args.project)
    config = resolve_config(
        index.project,
        index.prompt_set,
        metric_id=args.metric,
        channel_a=args.channel_a,
        channel_b=args.channel_b,
        threshold=args.threshold,
    )
    params = parameters_for(index.project, seed=args.seed, iterations=args.iterations)
    document = crosscheck_document(index, params, config, platform=args.platform)
    out_path = Path(args.out) if args.out else paths(args.project).out_dir / "crosscheck.json"
    write_json(out_path, document)
    return CommandResult(
        data={**document, "output": str(out_path)},
        text=_summary(document, out_path),
        exit_code=EXIT_CODES[document["status"]],
    )


def register(subparsers: SubParsers) -> None:
    parser = add_command(
        subparsers,
        "crosscheck",
        run,
        help="双通道交叉验证（偏离度公式是本项目的定义），结果写入 out/crosscheck.json",
    )
    parser.add_argument("project", help="项目目录")
    parser.add_argument("--metric", help="比较的指标，默认取预先登记值")
    parser.add_argument("--channel-a", help="通道 A，默认取预先登记值")
    parser.add_argument("--channel-b", help="通道 B，默认取预先登记值")
    parser.add_argument("--platform", help="只计算该平台")
    parser.add_argument("--threshold", type=float, help="偏离度阈值，默认取预先登记值")
    parser.add_argument("--seed", type=int, help="bootstrap 的 seed，默认取预先登记值")
    parser.add_argument("--iterations", type=int, help="bootstrap 重抽次数，默认取预先登记值")
    parser.add_argument("--out", help="结果文件路径，默认 PROJECT/out/crosscheck.json")
