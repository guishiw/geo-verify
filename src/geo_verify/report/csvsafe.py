"""CSV 安全导出：UTF-8 BOM，并防公式注入。

表格软件会把以 `=`、`+`、`-`、`@`、制表符、回车开头的单元格当作公式执行。
题目、回答原文等内容来自外部，导出时一律在这类单元格前加一个单引号 `'`。
换行用 CRLF，含换行、回车、逗号、引号的单元格由 csv 模块加引号。
"""

from __future__ import annotations

import codecs
import csv
import io
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

FORMULA_TRIGGERS: tuple[str, ...] = ("=", "+", "-", "@", chr(9), chr(13))
GUARD = "'"


def safe_cell(value: Any) -> str:
    """把一个值转成可安全写入 CSV 的文本：None 为空，布尔为 true / false。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text.startswith(FORMULA_TRIGGERS):
        return GUARD + text
    return text


def render_csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    """生成带 BOM 的 CSV 字节串；同样的输入得到逐字节相同的结果。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([safe_cell(name) for name in header])
    for row in rows:
        if len(row) != len(header):
            raise ValueError(f"CSV 行的列数（{len(row)}）与表头的列数（{len(header)}）不一致")
        writer.writerow([safe_cell(cell) for cell in row])
    return codecs.BOM_UTF8 + buffer.getvalue().encode("utf-8")


def write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_csv(header, rows))
