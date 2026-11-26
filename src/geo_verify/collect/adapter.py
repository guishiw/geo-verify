"""采集适配器与 `collect` 命令之间的接口。

每个适配器是 collect 包下的一个模块，导出一个 `ADAPTER = AdapterSpec(...)`：
`add_arguments` 往 `collect` 的解析器上注册自己独有的参数，`run` 执行并返回结果。
commands/collect.py 只负责按 `--adapter` 分发，新增适配器时把它的 ADAPTER 加进注册表即可。

`collect` 是一个叶子命令，所有适配器的参数都注册在同一个解析器上，argparse 无法表达
“某个适配器的某个动作才需要的参数”，因此由适配器在执行前用这里的函数自行检查，
统一报输入错误（退出码 2）。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass

from geo_verify.cli import CommandResult
from geo_verify.errors import InputError


@dataclass(frozen=True)
class AdapterSpec:
    name: str
    summary: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], CommandResult]


def option_given(args: argparse.Namespace, flag: str) -> bool:
    """命令行上是否给出了某个参数（如 "--snapshots-dir"）。"""
    value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
    return value is not None and value is not False


def require_options(args: argparse.Namespace, context: str, *flags: str) -> None:
    missing = [flag for flag in flags if not option_given(args, flag)]
    if missing:
        raise InputError(f"{context} 缺少参数：{'、'.join(missing)}")


def reject_options(args: argparse.Namespace, context: str, *flags: str, hint: str = "") -> None:
    """拒绝对当前适配器或动作不起作用的参数，避免使用者误以为它生效了。"""
    extra = [flag for flag in flags if option_given(args, flag)]
    if extra:
        raise InputError(f"{context} 不使用参数：{'、'.join(extra)}{hint}")
