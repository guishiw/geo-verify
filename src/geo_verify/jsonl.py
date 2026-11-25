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


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """逐行产出 (行号, 记录)；行号从 1 开始。空行视为输入错误。"""
    if not path.exists():
        raise InputError(f"文件不存在：{path}")
    with path.open("r", encoding="utf-8") as handle:
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
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} 不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）") from exc
