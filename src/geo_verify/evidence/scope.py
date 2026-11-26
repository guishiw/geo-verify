"""证据包覆盖哪些文件，以及每个文件在清单中的角色。

覆盖范围（存在的才列入清单）：

    project.json                     config  口径配置（预先登记的指标、阈值、seed）
    entities.jsonl                   raw     （以下 5 个 JSONL 都生成逐行索引，且只能追加）
    observations.jsonl               raw
    annotations.jsonl                raw
    judgments.jsonl                  raw
    audit.jsonl                      raw
    prompts/sealed/*  prompts/diffs/*  raw   封存题库与差异记录，只增不改
    snapshots/**                     snapshot  快照（递归），只增不改
    recheck/*                        derived   抽样复核计划与结果
    uv.lock  evidence/lock/*         lock      依赖锁
    evidence/index/*                 index     逐行哈希索引（含因改写历史而另起的各代索引）

不覆盖：out/（生成物，可由原始数据复算）、prompts/draft.jsonl（草稿）、evidence/manifest-*.json
（清单本身，由清单链校验）、系统生成的元数据文件（.DS_Store、Thumbs.db、desktop.ini、._*）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LINE_INDEXED: tuple[str, ...] = (
    "entities.jsonl",
    "observations.jsonl",
    "annotations.jsonl",
    "judgments.jsonl",
    "audit.jsonl",
)
TOP_LEVEL: dict[str, str] = {
    "project.json": "config",
    **dict.fromkeys(LINE_INDEXED, "raw"),
    "uv.lock": "lock",
}
INDEX_DIR = "evidence/index"
LOCK_COPY = "evidence/lock/uv.lock"
# (目录, 角色, 是否递归)
DIRECTORIES: tuple[tuple[str, str, bool], ...] = (
    ("prompts/sealed", "raw", False),
    ("prompts/diffs", "raw", False),
    ("snapshots", "snapshot", True),
    ("recheck", "derived", False),
    (INDEX_DIR, "index", False),
    ("evidence/lock", "lock", False),
)
IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
# 只增不改的角色：一旦列入清单，之后的清单里必须原样保留（原始 JSONL 只能在末尾追加）
APPEND_ONLY_ROLES = frozenset({"raw", "snapshot"})
ROLE_NOUNS = {"raw": "原始文件", "snapshot": "快照文件"}


@dataclass(frozen=True)
class Covered:
    """证据包中的一个文件：相对路径（POSIX 分隔符）、角色、是否维护逐行索引。"""

    path: str
    role: str
    indexed: bool


def is_ignored(name: str) -> bool:
    return name in IGNORED_NAMES or name.startswith("._")


def index_relpath(source: str, generation: int | None = None) -> str:
    """JSONL 的行索引路径。改写历史后另起一代索引时，文件名带上新清单的序号。"""
    if generation is None:
        return f"{INDEX_DIR}/{source}.lines"
    return f"{INDEX_DIR}/{source}.{generation:04d}.lines"


def discover(root: Path) -> list[Covered]:
    """列出项目目录中属于证据包的全部文件，按路径排序。"""
    found: list[Covered] = []
    for name, role in TOP_LEVEL.items():
        if (root / name).is_file():
            found.append(Covered(name, role, name in LINE_INDEXED))
    for directory, role, recursive in DIRECTORIES:
        base = root / directory
        if not base.is_dir():
            continue
        candidates = base.rglob("*") if recursive else base.iterdir()
        for path in candidates:
            relative = path.relative_to(root)
            if path.is_file() and not any(is_ignored(part) for part in relative.parts):
                found.append(Covered(relative.as_posix(), role, False))
    return sorted(found, key=lambda item: item.path)
