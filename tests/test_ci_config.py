"""CI 配置检查：插件的 JS 测试需要 Node，CI 必须在 make check 之前装好 Node 20 或更新版本。

不引入 YAML 解析库，按行检查 .github/workflows/ci.yml 的步骤顺序。
"""

from __future__ import annotations

import re

from conftest import REPO_ROOT

CI_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _steps() -> list[list[str]]:
    """把 steps 列表切成若干步，每步是它的行（去掉首尾空白）。"""
    steps: list[list[str]] = []
    for raw in CI_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("- uses:") or line.startswith("- run:"):
            steps.append([line])
        elif steps and line and not line.startswith("#"):
            steps[-1].append(line)
    return steps


def test_ci_sets_up_node_20_or_newer_before_make_check() -> None:
    steps = _steps()
    heads = [step[0] for step in steps]
    node = next(index for index, head in enumerate(heads) if "actions/setup-node@" in head)
    check = heads.index("- run: make check")
    assert node < check
    version = next(line for line in steps[node] if line.startswith("node-version:"))
    match = re.search(r"(\d+)", version)
    assert match is not None
    assert int(match.group(1)) >= 20
