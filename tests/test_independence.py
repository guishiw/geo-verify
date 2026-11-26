"""R10：geo-verify 不得依赖 geo-facts 或 geo-private 的代码（裁判与运动员分离）。

两个公开仓库只通过文件交换数据。这里用 ast 扫描本仓库全部 Python 文件的 import
（含 importlib.import_module / __import__ 的字符串参数），并检查依赖声明与锁文件。
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from conftest import REPO_ROOT

FORBIDDEN_MODULES = frozenset({"geo_facts", "geo_private"})
FORBIDDEN_DISTRIBUTIONS = ("geo-evidence-facts", "geo-private")
SKIPPED_DIRS = frozenset({".venv", ".git", "node_modules", "__pycache__", ".hypothesis"})


def _top(name: str) -> str:
    return name.split(".", 1)[0]


def forbidden_imports(path: Path, root: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(root).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _top(alias.name) in FORBIDDEN_MODULES:
                    found.append(f"{rel}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _top(node.module) in FORBIDDEN_MODULES:
                found.append(f"{rel}:{node.lineno} from {node.module} import ...")
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            first = node.args[0]
            if (
                name in {"import_module", "__import__"}
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and _top(first.value) in FORBIDDEN_MODULES
            ):
                found.append(f"{rel}:{node.lineno} {name}({first.value!r})")
    return found


def python_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if not SKIPPED_DIRS.intersection(path.relative_to(root).parts)
    )


def scan(root: Path) -> list[str]:
    return [hit for path in python_files(root) for hit in forbidden_imports(path, root)]


def test_no_python_file_imports_geo_facts_or_geo_private() -> None:
    files = python_files(REPO_ROOT)
    # 确认扫描范围确实覆盖了源码、测试和工具，而不是空跑
    scanned_dirs = {path.relative_to(REPO_ROOT).parts[0] for path in files}
    assert {"src", "tests", "tools"} <= scanned_dirs
    violations = scan(REPO_ROOT)
    assert not violations, "发现跨仓库依赖：\n" + "\n".join(violations)


def test_scanner_catches_every_import_form(tmp_path: Path) -> None:
    planted = tmp_path / "pkg" / "bad.py"
    planted.parent.mkdir()
    planted.write_text(
        "import geo_facts\n"
        "import geo_private.tools as t\n"
        "from geo_facts.judge import rules\n"
        "import importlib\n"
        "def later():\n"
        "    importlib.import_module('geo_facts.gate')\n"
        "    __import__('geo_private')\n"
        "from . import geo_facts_like\n"
        "import geo_factsx\n",
        encoding="utf-8",
    )
    hits = scan(tmp_path)
    assert [hit.split(" ", 1)[0] for hit in hits] == [
        "pkg/bad.py:1",
        "pkg/bad.py:2",
        "pkg/bad.py:3",
        "pkg/bad.py:6",
        "pkg/bad.py:7",
    ]


def test_declared_dependencies_and_lock_exclude_the_other_repositories() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    declared = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        declared += group
    for group in pyproject.get("dependency-groups", {}).values():
        declared += [item for item in group if isinstance(item, str)]
    assert declared, "pyproject.toml 没有读到任何依赖声明"
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    for dist in FORBIDDEN_DISTRIBUTIONS:
        assert not [item for item in declared if item.lower().startswith(dist)], dist
        assert f'name = "{dist}"' not in lock, dist
