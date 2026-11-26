"""代码版本与依赖锁：证据包要能说明“用哪一版工具、哪一组依赖”算出的结果。

- 代码版本：包版本；从源码目录运行且能取到 git commit 时写成 `0.1.0+git.<commit>`，
  工作区有未提交改动（或无法确认是否干净）时再加 `.dirty`。取不到 commit 不报错。
- 依赖锁：先找项目目录下的 `uv.lock`，再找安装元数据（`direct_url.json` 指向的本地源码目录）
  里的 `uv.lock`；都找不到时清单的 `lock_hash` 为 null，并在命令结果里说明。
"""

from __future__ import annotations

import functools
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import geo_verify

DIST_NAME = "geo-evidence-verify"
TOOL_NAME = "geo-verify"
_GIT_TIMEOUT_SECONDS = 10
_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def tool() -> dict[str, str]:
    return {"name": TOOL_NAME, "version": geo_verify.__version__}


@functools.cache
def code_version() -> str:
    """当前进程所用代码的版本描述（进程内缓存，同一进程内多次生成清单结果一致）。"""
    return describe_code_version(source_root())


def describe_code_version(root: Path | None) -> str:
    state = git_state(root) if root is not None else None
    if state is None:
        return geo_verify.__version__
    commit, dirty = state
    return f"{geo_verify.__version__}+git.{commit}{'.dirty' if dirty else ''}"


def source_root() -> Path | None:
    """从源码目录（src 布局）运行时返回仓库根目录；安装成普通包时返回 None。"""
    package_dir = Path(geo_verify.__file__).resolve().parent
    root = package_dir.parent.parent
    if package_dir.parent.name == "src" and (root / "pyproject.toml").is_file():
        return root
    return None


def git_state(root: Path) -> tuple[str, bool] | None:
    """(commit, 是否有未提交改动)。root 不是该 git 仓库的顶层目录时返回 None，避免误取外层仓库。"""
    git = shutil.which("git")
    if git is None:
        return None
    top = run_git(git, root, "rev-parse", "--show-toplevel")
    if top is None or Path(top).resolve() != root.resolve():
        return None
    commit = run_git(git, root, "rev-parse", "HEAD")
    if commit is None or not _COMMIT.match(commit):
        return None
    status = run_git(git, root, "status", "--porcelain")
    return commit, status is None or status != ""


def run_git(git: str, root: Path, *args: str) -> str | None:
    """运行一条只读 git 命令，失败时返回 None（不报错）。"""
    env = {key: value for key, value in os.environ.items() if key not in _GIT_ENV_DROP}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(
            [git, "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def installed_lock_path() -> Path | None:
    """从本包的安装元数据定位锁文件；定位不到返回 None。"""
    try:
        distribution = importlib.metadata.distribution(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None
    return lock_from_direct_url(distribution.read_text("direct_url.json"))


def lock_from_direct_url(text: str | None) -> Path | None:
    """PEP 610 的 direct_url.json 指向本地目录时，取该目录下的 uv.lock。"""
    if not text:
        return None
    try:
        info = json.loads(text)
    except json.JSONDecodeError:
        return None
    url = info.get("url") if isinstance(info, dict) else None
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    candidate = Path(url2pathname(parsed.path)) / "uv.lock"
    return candidate if candidate.is_file() else None
