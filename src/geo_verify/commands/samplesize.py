"""`geo-verify samplesize`：两比例样本量、零失败样本量、设计效应与有效样本量。"""

from __future__ import annotations

import argparse

from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.errors import InputError
from geo_verify.stats import (
    DESIGN_EFFECT_BASIS,
    TWO_PROPORTION_BASIS,
    ZERO_FAILURE_BASIS,
    design_effect,
    effective_sample_size,
    two_proportion_sample_size,
    zero_failure_sample_size,
)

_MODE_OPTIONS = {
    "two_proportion": ("p1", "p2"),
    "zero_failure": ("target",),
    "design_effect": ("cluster_size", "icc", "n"),
}


def _reject_foreign_options(args: argparse.Namespace, mode: str) -> None:
    for other, names in _MODE_OPTIONS.items():
        if other == mode:
            continue
        used = [name for name in names if getattr(args, name) is not None]
        if used:
            options = "、".join("--" + name.replace("_", "-") for name in used)
            raise InputError(f"参数 {options} 不属于当前计算模式，请分开计算")


def _two_proportion(args: argparse.Namespace) -> CommandResult:
    _reject_foreign_options(args, "two_proportion")
    if args.p1 is None or args.p2 is None:
        raise InputError(
            "两比例样本量需要同时给出 --p1 和 --p2；其他模式请用 --zero-failure 或 --design-effect"
        )
    alpha = 0.05 if args.alpha is None else args.alpha
    power = 0.8 if args.power is None else args.power
    n = two_proportion_sample_size(args.p1, args.p2, alpha=alpha, power=power)
    data = {
        "mode": "two_proportion",
        "p1": args.p1,
        "p2": args.p2,
        "alpha": alpha,
        "power": power,
        "n_per_group": n,
        "n_total": 2 * n,
        "basis": TWO_PROPORTION_BASIS,
    }
    text = (
        f"检出 {args.p1:g} 与 {args.p2:g} 的差异：每组 {n} 次，两组合计 {2 * n} 次"
        f"（α={alpha:g}，功效={power:g}）。\n口径：{TWO_PROPORTION_BASIS}"
    )
    return CommandResult(data=data, text=text)


def _zero_failure(args: argparse.Namespace) -> CommandResult:
    _reject_foreign_options(args, "zero_failure")
    if args.target is None:
        raise InputError("零失败样本量需要给出 --target（如 0.98）")
    confidence = 0.95 if args.confidence is None else args.confidence
    n = zero_failure_sample_size(args.target, confidence=confidence)
    data = {
        "mode": "zero_failure",
        "target": args.target,
        "confidence": confidence,
        "n": n,
        "basis": ZERO_FAILURE_BASIS,
    }
    text = (
        f"说明合格率不低于 {args.target:g}（单侧置信度 {confidence:g}）：需要连续 {n} 次无失败。\n"
        f"口径：{ZERO_FAILURE_BASIS}"
    )
    return CommandResult(data=data, text=text)


def _design_effect(args: argparse.Namespace) -> CommandResult:
    _reject_foreign_options(args, "design_effect")
    if args.cluster_size is None or args.icc is None:
        raise InputError("设计效应需要同时给出 --cluster-size 和 --icc")
    deff = design_effect(args.cluster_size, args.icc)
    effective = None if args.n is None else effective_sample_size(args.n, deff)
    data = {
        "mode": "design_effect",
        "cluster_size": args.cluster_size,
        "icc": args.icc,
        "design_effect": deff,
        "n": args.n,
        "effective_n": effective,
        "basis": DESIGN_EFFECT_BASIS,
    }
    lines = [f"每题 {args.cluster_size:g} 次、ICC={args.icc:g}：设计效应 {deff:.4g}。"]
    if effective is not None:
        lines.append(f"{args.n:g} 次观测的有效样本量约为 {effective:.1f} 次。")
    lines.append(f"口径：{DESIGN_EFFECT_BASIS}")
    return CommandResult(data=data, text="\n".join(lines))


def run(args: argparse.Namespace) -> CommandResult:
    if args.zero_failure and args.design_effect:
        raise InputError("--zero-failure 与 --design-effect 不能同时使用")
    if args.zero_failure:
        return _zero_failure(args)
    if args.design_effect:
        return _design_effect(args)
    return _two_proportion(args)


def register(subparsers: SubParsers) -> None:
    parser = add_command(
        subparsers,
        "samplesize",
        run,
        help="样本量计算：两比例比较、零失败样本量、设计效应与有效样本量",
    )
    parser.add_argument("--p1", type=float, help="基线比例，如 0.6")
    parser.add_argument("--p2", type=float, help="目标比例，如 0.75")
    parser.add_argument("--alpha", type=float, help="双侧显著性水平，默认 0.05")
    parser.add_argument("--power", type=float, help="功效，默认 0.8")
    parser.add_argument("--zero-failure", action="store_true", help="零失败样本量模式")
    parser.add_argument("--target", type=float, help="零失败模式：要说明的合格率下限，如 0.98")
    parser.add_argument("--confidence", type=float, help="零失败模式：单侧置信度，默认 0.95")
    parser.add_argument("--design-effect", action="store_true", help="设计效应模式")
    parser.add_argument("--cluster-size", type=float, help="设计效应模式：每题提问次数")
    parser.add_argument("--icc", type=float, help="设计效应模式：题内相关系数")
    parser.add_argument("--n", type=float, help="设计效应模式：观测总次数，用于换算有效样本量")
