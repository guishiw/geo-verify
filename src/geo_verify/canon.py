"""规范化 JSON（RFC 8785）与 SHA-256。

所有内容哈希都经由这里计算，保证甲方、乙方、第三方对同一份数据得到同一个哈希。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import rfc8785

from geo_verify.errors import InputError


def canonical_bytes(value: Any) -> bytes:
    """返回 RFC 8785 规范化 JSON 字节串。"""
    try:
        return rfc8785.dumps(value)
    except rfc8785.CanonicalizationError as exc:
        raise InputError(f"无法规范化为 JSON：{exc}") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_obj(value: Any) -> str:
    """对象的内容哈希：规范化 JSON 后取 SHA-256（小写十六进制）。"""
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def hash_without(value: dict[str, Any], *fields: str) -> str:
    """去掉指定顶层字段后计算内容哈希，用于“自带哈希字段”的对象。"""
    return hash_obj({key: item for key, item in value.items() if key not in fields})
