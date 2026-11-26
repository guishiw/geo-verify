"""JSONL 读写。原始记录只追加：本模块不提供覆盖或删除单行的接口。"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from geo_verify.errors import InputError


def dumps_line(record: dict[str, Any]) -> str:
    """单行 JSON：键排序、紧凑分隔符、保留中文，保证逐字节可复现。"""
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unreadable(path: Path, exc: OSError) -> InputError:
    """把“读不了文件”的系统错误转成输入错误（退出码 2），说明是哪个文件、为什么。"""
    if isinstance(exc, FileNotFoundError):
        return InputError(f"文件不存在：{path}")
    if isinstance(exc, IsADirectoryError):
        return InputError(f"{path} 是目录，不是文件")
    return InputError(f"无法读取文件 {path}：{exc.strerror or type(exc).__name__}")


def read_bytes(path: Path) -> bytes:
    """读取输入文件的全部字节。读不了（不存在、是目录、没有权限等）一律是输入错误。"""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _unreadable(path, exc) from exc


def read_text(path: Path, *, encoding: str = "utf-8") -> str:
    """读取 UTF-8 文本文件（encoding 可为 utf-8-sig 以去掉 BOM）；不是 UTF-8 是输入错误。"""
    try:
        return read_bytes(path).decode(encoding)
    except UnicodeDecodeError as exc:
        raise InputError(f"{path} 不是 UTF-8 编码") from exc


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """逐行产出 (行号, 记录)；行号从 1 开始。空行视为输入错误。"""
    if not path.exists():
        raise InputError(f"文件不存在：{path}")
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise _unreadable(path, exc) from exc
    with handle:
        try:
            for number, raw in enumerate(handle, start=1):
                line = raw.rstrip("\n")
                if not line.strip():
                    raise InputError(f"{path} 第 {number} 行为空行")
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise InputError(f"{path} 第 {number} 行不是合法 JSON：{exc.msg}") from exc
                if not isinstance(value, dict):
                    raise InputError(f"{path} 第 {number} 行不是 JSON 对象")
                yield number, value
        except UnicodeDecodeError as exc:
            raise InputError(f"{path} 不是 UTF-8 编码") from exc


def read_jsonl(path: Path, *, missing_ok: bool = False) -> list[dict[str, Any]]:
    if missing_ok and not path.exists():
        return []
    return [record for _, record in iter_jsonl(path)]


def append_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    """以追加方式写入记录，返回写入条数。不会改动已有内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(dumps_line(record) + "\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def write_json(path: Path, value: Any) -> None:
    """写出给人和机器看的 JSON 文件：键排序、两格缩进、结尾换行，逐字节可复现。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def read_json(path: Path) -> Any:
    if not path.exists():
        raise InputError(f"文件不存在：{path}")
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} 不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）") from exc
