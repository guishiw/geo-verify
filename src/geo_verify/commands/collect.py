"""`geo-verify collect`：按 `--adapter` 分发到各采集适配器。

每个适配器是 geo_verify.collect 下的一个模块，导出 `ADAPTER`（见 collect/adapter.py）。
新增适配器：实现模块 → 把它的 ADAPTER 加进 `_IMPLEMENTED` → 从 `PLANNED` 里去掉对应条目。
所有适配器共用的参数在这里注册，各自独有的参数由适配器的 add_arguments 注册。
"""

from __future__ import annotations

import argparse

from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.collect import manual
from geo_verify.collect.adapter import AdapterSpec
from geo_verify.errors import InputError

_IMPLEMENTED: tuple[AdapterSpec, ...] = (manual.ADAPTER,)
ADAPTERS: dict[str, AdapterSpec] = {spec.name: spec for spec in _IMPLEMENTED}
# 接口文档里已约定、尚未落地的适配器：选用时给出明确的输入错误，而不是“未知参数”。
PLANNED: dict[str, str] = {
    "openai": "通用 OpenAI 兼容接口",
    "share": "已保存的分享数据导入",
}


def run(args: argparse.Namespace) -> CommandResult:
    spec = ADAPTERS.get(args.adapter)
    if spec is None:
        raise InputError(
            f"适配器 {args.adapter}（{PLANNED[args.adapter]}）尚未实现；"
            f"当前可用：{'、'.join(ADAPTERS)}"
        )
    return spec.run(args)


def register(subparsers: SubParsers) -> None:
    names = [*ADAPTERS, *(name for name in PLANNED if name not in ADAPTERS)]
    parser = add_command(
        subparsers,
        "collect",
        run,
        help="采集观测：按 --adapter 选择适配器（manual=人工采集包）",
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
    common.add_argument("--now", help="审计事件时间（UTC），默认取当前时间")
    for spec in ADAPTERS.values():
        spec.add_arguments(parser)
