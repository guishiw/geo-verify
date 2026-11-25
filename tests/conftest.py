"""测试公共设施。

所有测试都不得访问网络（红线 R2）：这里把出站连接和域名解析直接换成报错，
任何一次真实联网尝试都会让测试失败，而不是悄悄成功。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_DIR = REPO_ROOT / "spec" / "v1"


class NetworkAccessError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> None:
        if self.family == socket.AF_UNIX:
            return original_connect(self, address, *args, **kwargs)
        raise NetworkAccessError(f"测试不得联网：尝试连接 {address!r}")

    def refuse_resolve(*args: Any, **kwargs: Any) -> None:
        raise NetworkAccessError(f"测试不得联网：尝试解析 {args[:1]!r}")

    original_connect = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_connect)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_resolve)
    monkeypatch.setattr(socket, "create_connection", refuse_resolve)


def load_examples(name: str) -> dict[str, Any]:
    path = SPEC_DIR / "examples" / f"{name}.examples.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def valid_example(name: str, index: int = 0) -> dict[str, Any]:
    """返回某契约合法样例的深拷贝，供测试改造。"""
    example: dict[str, Any] = json.loads(json.dumps(load_examples(name)["valid"][index]))
    return example


def run_cli(*argv: str) -> tuple[int, str, str]:
    """进程内运行 CLI，返回 (退出码, 标准输出, 标准错误)。"""
    import contextlib
    import io

    from geo_verify.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def run_cli_json(*argv: str) -> tuple[int, Any]:
    code, out, err = run_cli(*argv, "--json")
    try:
        return code, json.loads(out)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"输出不是 JSON：{out!r}；stderr={err!r}") from exc
