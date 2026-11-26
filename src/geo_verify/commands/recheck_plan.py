"""`geo-verify recheck-plan`：按固定 seed 抽题，生成抽样复核清单。"""

from __future__ import annotations

import argparse
from pathlib import Path

from geo_verify import project as project_module
from geo_verify.audit import resolve_now
from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.errors import EXIT_INSUFFICIENT, EXIT_OK
from geo_verify.report.recheck import PLAN_CSV_PATH, PLAN_PATH, WrittenPlan, build_plan, write_plan


def _text(written: WrittenPlan) -> str:
    plan = written.plan.document
    registered = written.plan.registered
    counts = plan["counts"]
    lines = [
        f"抽样复核清单：第 {plan['prompt_set']['version']} 版封存题库共 {plan['total_queries']} 道题，"
        f"按比例 {plan['fraction']:g}、seed {plan['seed']} 抽取 {counts['sampled_queries']} 道"
        f"（⌈比例 × 题数⌉，按 sha256(seed|query_id) 排序取前若干道）。",
    ]
    if plan["preregistered"]:
        lines.append("抽样参数取自预先登记的口径。")
    else:
        lines.append(
            f"注意：本清单未采用预先登记的抽样参数（登记：比例 {registered['fraction']:g}、"
            f"seed {registered['seed']}），不能作为预先登记的抽样复核。"
        )
    lines.append(
        f"复核项共 {counts['items']} 项：抽样 {counts['sample_items']} 项"
        f"（{counts['observations']} 条有效观测），争议 {counts['dispute_items']} 项。"
    )
    if plan["query_ids"]:
        lines.append(f"抽中的题目：{'、'.join(plan['query_ids'])}")
    for item in plan["notices"]:
        lines.append(f"提示（{item['code']}）：{item['message']}")
    lines.append(
        f"已写入 {PLAN_PATH} 和 {PLAN_CSV_PATH}；在 CSV 的 recheck_* 列填写复核结论后，"
        "用 geo-verify recheck-compare 比对。"
    )
    if written.previous_plan_hash is None:
        pass
    elif written.changed:
        lines.append(f"原有的复核计划（SHA-256 {written.previous_plan_hash[:12]}…）已被替换。")
    else:
        lines.append("复核计划与上次相同，文件内容未变化。")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> CommandResult:
    if args.now is not None:
        resolve_now(args.now)  # 只校验格式：复核计划不含时间戳，同输入同 seed 逐字节一致
    root = Path(args.project)
    project_module.paths(root)
    plan = build_plan(
        root, fraction=args.fraction, seed=args.seed, prompt_set_version=args.prompt_set_version
    )
    written = write_plan(root, plan)
    data = {
        **plan.document,
        "outputs": {"plan": PLAN_PATH, "csv": PLAN_CSV_PATH},
        "plan_hash": written.plan_hash,
        "previous_plan_hash": written.previous_plan_hash,
        "changed": written.changed,
    }
    exit_code = EXIT_OK if plan.items else EXIT_INSUFFICIENT
    return CommandResult(data=data, text=_text(written), exit_code=exit_code)


def register(subparsers: SubParsers) -> None:
    parser = add_command(
        subparsers,
        "recheck-plan",
        run,
        help="按固定 seed 抽取题目，生成抽样复核清单（recheck/plan.json 与 plan.csv）",
    )
    parser.add_argument("project", help="项目目录")
    parser.add_argument(
        "--fraction", type=float, help="抽样比例 (0, 1]，默认取预先登记值；覆盖后不再视为预先登记"
    )
    parser.add_argument(
        "--seed", type=int, help="抽样 seed，默认取预先登记值；覆盖后不再视为预先登记"
    )
    parser.add_argument("--prompt-set-version", type=int, help="封存题库版本，默认最新版本")
    parser.add_argument("--now", help="UTC 时间（只校验格式；复核计划不含时间戳）")
