"""逐行哈希索引与行级差异定位。

JSONL 原始记录按行切分（只按 `\\n` 切分，行内容不含结尾的 `\\n`；文件末尾没有换行时最后一段也算一行），
每行内容的原始字节取 SHA-256，按行写成索引文件：每行一个 64 位小写十六进制哈希，以 `\\n` 结尾。

定位算法（只依赖两份行哈希序列，结果确定）：

1. 先去掉两份序列相同的前缀和后缀，它们原样未动；
2. 中间段按哈希配对（相同内容的第 k 次出现配第 k 次出现）；
3. 配上对的行里，若某行在“共同行”中的先后名次变了，记为 `line_reordered`；
4. 没配上的行，按其前面有几行共同行归到同一个“空隙”里：同一空隙内旧行和新行依次两两配成
   `line_modified`，多出来的旧行记 `line_deleted`，多出来的新行记 `line_inserted`。

行号一律从 1 起：`line_deleted` 用清单记录时的行号，其余用当前文件的行号。
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_HASH_LINE = re.compile(rb"^[0-9a-f]{64}$")
_CHUNK = 1 << 20


@dataclass(frozen=True)
class FileScan:
    """一次读文件的结果：整文件 SHA-256、字节数，以及（需要时）逐行哈希。"""

    sha256: str
    size: int
    line_hashes: list[str] | None


@dataclass(frozen=True)
class LineChange:
    """一处行级差异。line 为报告用的行号；old_line / new_line 分别是记录时与当前的行号。"""

    kind: str
    line: int
    old_line: int | None
    new_line: int | None


def split_lines(data: bytes) -> list[bytes]:
    """按 `\\n` 切分；行内容不含 `\\n`。空字节串没有行。"""
    if not data:
        return []
    parts = data.split(b"\n")
    if parts[-1] == b"":
        parts.pop()
    return parts


def line_hash(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def scan_file(path: Path, *, lines: bool) -> FileScan:
    """流式读取文件，计算整文件哈希；lines=True 时同时计算逐行哈希。"""
    digest = hashlib.sha256()
    size = 0
    if not lines:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
        return FileScan(digest.hexdigest(), size, None)
    hashes: list[str] = []
    with path.open("rb") as handle:
        for raw in handle:
            digest.update(raw)
            size += len(raw)
            hashes.append(line_hash(raw[:-1] if raw.endswith(b"\n") else raw))
    return FileScan(digest.hexdigest(), size, hashes)


def render_index(hashes: Sequence[str]) -> bytes:
    """行索引文件内容：每行一个哈希，以 `\\n` 结尾。"""
    return "".join(f"{value}\n" for value in hashes).encode("ascii")


def parse_index(data: bytes) -> list[str]:
    """解析行索引文件；格式不对时抛出 ValueError。"""
    if data and not data.endswith(b"\n"):
        raise ValueError("行索引文件末尾缺少换行")
    values: list[str] = []
    for number, line in enumerate(split_lines(data), start=1):
        if not _HASH_LINE.match(line):
            raise ValueError(f"行索引第 {number} 行不是 SHA-256 十六进制串")
        values.append(line.decode("ascii"))
    return values


def first_divergence(old: Sequence[str], new: Sequence[str]) -> int | None:
    """只追加约束：old 是 new 的前缀时返回 None，否则返回第一处不符的位置（0 起）。

    new 比 old 短且前面都相同（末尾被截断）时，返回 len(new)。
    """
    for position, value in enumerate(old):
        if position >= len(new) or new[position] != value:
            return position
    return None


def locate_changes(old: Sequence[str], new: Sequence[str]) -> list[LineChange]:
    """比较记录时（old）与当前（new）的行哈希序列，返回按行号排序的差异列表。"""
    n_old, n_new = len(old), len(new)
    limit = min(n_old, n_new)
    prefix = 0
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[n_old - 1 - suffix] == new[n_new - 1 - suffix]:
        suffix += 1
    middle_old = range(prefix, n_old - suffix)
    middle_new = range(prefix, n_new - suffix)

    waiting: dict[str, deque[int]] = defaultdict(deque)
    for j in middle_new:
        waiting[new[j]].append(j)
    pairs: list[tuple[int, int]] = []
    for i in middle_old:
        queue = waiting.get(old[i])
        if queue:
            pairs.append((i, queue.popleft()))
    matched_old = {i for i, _ in pairs}
    matched_new = {j for _, j in pairs}

    changes: list[LineChange] = []
    rank_new = {pair: rank for rank, pair in enumerate(sorted(pairs, key=lambda p: p[1]))}
    for rank, (i, j) in enumerate(pairs):
        if rank_new[(i, j)] != rank:
            changes.append(LineChange("line_reordered", j + 1, old_line=i + 1, new_line=j + 1))

    gaps_old = _gaps(middle_old, matched_old, prefix)
    gaps_new = _gaps(middle_new, matched_new, prefix)
    for gap in sorted(set(gaps_old) | set(gaps_new)):
        olds = gaps_old.get(gap, [])
        news = gaps_new.get(gap, [])
        for i, j in zip(olds, news, strict=False):
            changes.append(LineChange("line_modified", j + 1, old_line=i + 1, new_line=j + 1))
        for i in olds[len(news) :]:
            changes.append(LineChange("line_deleted", i + 1, old_line=i + 1, new_line=None))
        for j in news[len(olds) :]:
            changes.append(LineChange("line_inserted", j + 1, old_line=None, new_line=j + 1))
    return sorted(changes, key=lambda item: (item.line, item.kind))


def _gaps(indices: range, matched: set[int], prefix: int) -> dict[int, list[int]]:
    """未配对的行按“前面有几行共同行”分组。"""
    groups: dict[int, list[int]] = defaultdict(list)
    seen = prefix
    for index in indices:
        if index in matched:
            seen += 1
        else:
            groups[seen].append(index)
    return groups
