"""按模块检查行覆盖率与分支覆盖率门槛。

读取 pytest-cov 生成的 .coverage.json，对 pyproject.toml 中 [tool.geo.coverage]
列出的模块分别汇总；任一模块的行覆盖或分支覆盖低于门槛即退出 1。
模块没有任何可统计代码时同样判失败，避免“空模块自动达标”。
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    settings = config["tool"]["geo"]["coverage"]
    minimum = float(settings["minimum"])
    modules: list[str] = list(settings["modules"])
    report_path = ROOT / ".coverage.json"
    if not report_path.exists():
        print("缺少 .coverage.json，请先运行 pytest", file=sys.stderr)
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files: dict[str, dict[str, dict[str, int]]] = report["files"]
    failed = False
    for module in modules:
        lines = covered_lines = branches = covered_branches = 0
        for name, data in files.items():
            normalized = "/" + name.replace("\\", "/")
            if f"/{module}/" not in normalized and not normalized.endswith(f"/{module}.py"):
                continue
            summary = data["summary"]
            lines += summary["num_statements"]
            covered_lines += summary["covered_lines"]
            branches += summary["num_branches"]
            covered_branches += summary["covered_branches"]
        if lines == 0:
            print(f"[失败] 模块 {module}: 没有可统计的代码，门槛无法证明")
            failed = True
            continue
        line_pct = 100.0 * covered_lines / lines
        branch_pct = 100.0 * covered_branches / branches if branches else 100.0
        ok = line_pct >= minimum and branch_pct >= minimum
        failed = failed or not ok
        print(
            f"[{'通过' if ok else '失败'}] 模块 {module}: 行 {line_pct:.1f}% "
            f"({covered_lines}/{lines})，分支 {branch_pct:.1f}% ({covered_branches}/{branches})，"
            f"门槛 {minimum:.0f}%"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
