"""`geo-verify collect`：按 `--adapter` 分发到各采集适配器。

每个适配器是 geo_verify.collect 下的一个模块，导出 `ADAPTER`（见 collect/adapter.py）。
新增适配器：实现模块 → 把它的 ADAPTER 加进 `_IMPLEMENTED` → 从 `PLANNED` 里去掉对应条目。
所有适配器共用的参数在这里注册，各自独有的参数由适配器的 add_arguments 注册；
选用某个适配器时，给出了别的适配器独有的参数会被拒绝（输入错误），而不是被悄悄忽略。
"""

from __future__ import annotations

import argparse

from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.collect import manual, openai_compat
from geo_verify.collect.adapter import AdapterSpec, reject_options
from geo_verify.errors import InputError

_IMPLEMENTED: tuple[AdapterSpec, ...] = (manual.ADAPTER, openai_compat.ADAPTER)
ADAPTERS: dict[str, AdapterSpec] = {spec.name: spec for spec in _IMPLEMENTED}
# 接口文档里已约定、尚未落地的适配器：选用时给出明确的输入错误，而不是“未知参数”。
PLANNED: dict[str, str] = {
    "share": "已保存的分享数据导入",
}
# 各适配器自己注册的参数（如 manual 的 --action），注册解析器时记录
OWN_OPTIONS: dict[str, tuple[str, ...]] = {}


def run(args: argparse.Namespace) -> CommandResult:
    spec = ADAPTERS.get(args.adapter)
    if spec is None:
        raise InputError(
            f"适配器 {args.adapter}（{PLANNED[args.adapter]}）尚未实现；"
            f"当前可用：{'、'.join(ADAPTERS)}"
        )
    own = set(OWN_OPTIONS.get(spec.name, ()))
    foreign = [
        flag
        for name, flags in OWN_OPTIONS.items()
        if name != spec.name
        for flag in flags
        if flag not in own
    ]
    reject_options(args, f"--adapter {spec.name}", *foreign, hint="（它们属于其他适配器）")
    return spec.run(args)


def register(subparsers: SubParsers) -> None:
    names = [*ADAPTERS, *(name for name in PLANNED if name not in ADAPTERS)]
    parser = add_command(
        subparsers,
        "collect",
        run,
        help="采集观测：按 --adapter 选择适配器（manual=人工采集包，openai=OpenAI 兼容接口）",
    )
    parser.add_argument("project", help="项目目录")
    parser.add_argument(
        "--adapter",
        required=True,
        choices=names,
        help="；".join(f"{spec.name}={spec.summary}" for spec in ADAPTERS.values()),
    )
    common = parser.add_argument_group("各适配器共用的参数（是否需要取决于适配器）")
    common.add_argument("--platform", help="平台编号，须在 project.json 的 platforms 中登记")
    common.add_argument("--window", help="时间窗口编号，如 W1")
    common.add_argument("--repeats", type=int, help="每题提问次数，默认 1")
    common.add_argument("--queries", help="只采集这些题目，逗号分隔；默认全部题目")
    common.add_argument("--input", help="待导入的本地文件")
    common.add_argument("--operator", help="采集人；数据里没有写明采集人时使用")
    common.add_argument(
        "--now",
        help="命令时间（UTC），默认取当前时间；manual 用作审计事件时间，"
        "openai 用它推出各任务的开始时间（第 i 个任务为该时间加 i 秒）",
    )
    for spec in ADAPTERS.values():
        before = len(parser._actions)
        spec.add_arguments(parser)
        OWN_OPTIONS[spec.name] = tuple(
            flag
            for action in parser._actions[before:]
            for flag in action.option_strings
            if flag.startswith("--")
        )
