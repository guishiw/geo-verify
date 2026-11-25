"""geo-verify 命令行入口。

子命令分布在 geo_verify.commands 下的各个模块里，每个模块提供
`register(subparsers)`。所有命令支持 `--json`。
退出码：0 成功，1 检查未通过，2 输入错误，3 数据不足，4 内部错误。
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from geo_verify import __version__
from geo_verify.errors import EXIT_INPUT_ERROR, EXIT_INTERNAL, EXIT_OK, GeoError

PRECHECK_NOTICE = "本工具只给出预检结论，不构成独立第三方验证。"


@dataclass
class CommandResult:
    """命令的统一返回：退出码、机器可读数据、给人看的中文文本。"""

    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    exit_code: int = EXIT_OK


Handler = Callable[[argparse.Namespace], CommandResult]
SubParsers = Any  # argparse 的 _SubParsersAction 是私有类型，这里只作标注占位


class _Parser(argparse.ArgumentParser):
    """用法错误统一按“输入错误”退出（退出码 2），提示用中文。"""

    def error(self, message: str) -> Any:
        self.print_usage(sys.stderr)
        self.exit(EXIT_INPUT_ERROR, f"输入错误：{message}\n")


def add_command(
    subparsers: SubParsers,
    name: str,
    handler: Handler,
    *,
    help: str,
    description: str | None = None,
) -> argparse.ArgumentParser:
    """注册一个叶子命令，并统一加上 --json。"""
    parser: argparse.ArgumentParser = subparsers.add_parser(
        name, help=help, description=description or help
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.set_defaults(handler=handler)
    return parser


def add_group(subparsers: SubParsers, name: str, *, help: str) -> SubParsers:
    """注册一个命令组（如 prompts、evidence），返回其子命令容器。"""
    parser: argparse.ArgumentParser = subparsers.add_parser(name, help=help, description=help)
    group = parser.add_subparsers(dest=f"{name}_command", metavar="子命令", parser_class=_Parser)
    group.required = True
    return group


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="geo-verify",
        description=f"GEO 验收预检工具。{PRECHECK_NOTICE}",
    )
    parser.add_argument("--version", action="version", version=f"geo-verify {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="命令", parser_class=_Parser)
    subparsers.required = True
    package = importlib.import_module("geo_verify.commands")
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"geo_verify.commands.{info.name}")
        module.register(subparsers)
    return parser


def _emit(result: CommandResult, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.data, ensure_ascii=False, sort_keys=True, indent=2))
    elif result.text:
        print(result.text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        result: CommandResult = args.handler(args)
    except GeoError as exc:
        if as_json:
            print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(f"错误：{exc.message}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:
        return EXIT_OK
    except Exception as exc:  # 内部错误必须如实暴露，不能伪装成成功
        if as_json:
            payload = {"error": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        traceback.print_exc(file=sys.stderr)
        return EXIT_INTERNAL
    _emit(result, as_json=as_json)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
