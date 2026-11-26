"""文档、红线措辞、标准条款白名单与 CLI 文档一致性检查。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from geo_verify.cli import build_parser
from geo_verify.report.rules import evaluate

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    ".github/workflows/ci.yml",
    "docs/quickstart.md",
    "docs/methodology.md",
    "docs/clause-map.md",
    "docs/cli.md",
    "docs/outputs.md",
    "docs/collect.md",
    "docs/evidence.md",
    "docs/recheck.md",
    "examples/auto/README.md",
    "examples/auto/run.sh",
)

# 红线 R4 / R9 禁止措辞：不使用“官方”“认证”“对标”及对第三方平台结果的保证性表述。
# 注：“对标”排除“核对标注 / 比对标注”中跨词偶然拼接的子串。
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("官方", re.compile("官方")),
    ("认证", re.compile("认证")),
    ("对标", re.compile(r"(?<!核)(?<!比)对标(?!注)")),
    ("保证首推", re.compile("保证首推")),
    ("必然收录", re.compile("必然收录")),
    ("保证排名", re.compile("保证排名")),
    ("保证推荐", re.compile("保证推荐")),
    ("确保首推", re.compile("确保首推")),
    ("必然推荐", re.compile("必然推荐")),
    ("百分百收录", re.compile("百分百收录")),
    ("100%收录", re.compile(r"100%\s*收录")),
    ("包收录", re.compile("包收录")),
    ("稳居第一", re.compile("稳居第一")),
)

# src 中仅豁免负责拦截禁用词本身的红线词表与显式说明“不使用该词”的注释。
SRC_EXEMPT_FILES = frozenset({"src/geo_verify/alerts/wording.py"})
SRC_META_COMMENT_RE = re.compile(r"不使用[“\"']官方[”\"']")

# GOAL.md 允许引用的全部标准条款号白名单（其余一律写“待人工核对”，不得编造）
ALLOWED_SECTIONS: frozenset[str] = frozenset(
    {
        "§7.1.4",
        "§8.3.5",
        "§8.4.4",
        "§8.3",
        "§9.3",
        "§10.1",
        "§13.2",
        "§14",
        "§15.2",
        "§6.2",
        "§7.3",
    }
)
ALLOWED_QUALIFIED_CLAUSES: frozenset[tuple[str, str]] = frozenset(
    {
        ("046", "§7.1.4"),
        ("046", "§8.3.5"),
        ("046", "§8.4.4"),
        ("047", "§8.3"),
        ("047", "§9.3"),
        ("047", "§10.1"),
        ("047", "§13.2"),
        ("047", "§14"),
        ("047", "§15.2"),
        ("026", "§6.2"),
        ("026", "§7.3"),
    }
)
SECTION_RE = re.compile(r"§\d+(?:\.\d+)*")
QUALIFIED_CLAUSE_RE = re.compile(r"(046|047|048|026)\s*(§\d+(?:\.\d+)*)")


def _iter_repo_text_files() -> list[Path]:
    targets: list[Path] = []
    for rel in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md"):
        targets.append(REPO_ROOT / rel)
    for folder in ("docs", "examples", "src"):
        base = REPO_ROOT / folder
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            targets.append(path)
    return targets


def _cli_leaf_commands(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> set[str]:
    leaves: set[str] = set()
    has_sub = False
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            has_sub = True
            for name, sub in action.choices.items():
                leaves.update(_cli_leaf_commands(sub, (*prefix, name)))
    if not has_sub and prefix:
        leaves.add(" ".join(prefix))
    return leaves


def _cli_doc_commands(markdown: str) -> set[str]:
    commands: set[str] = set()
    heading_re = re.compile(r"^###\s+`([^`]+)`\s*$", re.MULTILINE)
    for match in heading_re.finditer(markdown):
        tokens = match.group(1).split()
        # 第一词为顶层命令；若第二词是小写子命令（非大写占位符、非 -- 选项），则组成二级命令
        if (
            len(tokens) >= 2
            and not tokens[1].startswith("-")
            and tokens[1].islower()
            and tokens[1].isascii()
        ):
            commands.add(f"{tokens[0]} {tokens[1]}")
        elif tokens:
            commands.add(tokens[0])
    return commands


def test_required_repository_files_exist_and_nonempty() -> None:
    for rel in REQUIRED_FILES:
        path = REPO_ROOT / rel
        assert path.is_file(), f"缺少必需文件：{rel}"
        assert path.stat().st_size > 0, f"文件为空：{rel}"

    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text

    ci_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "make check" in ci_text


def test_no_forbidden_wording_in_docs_examples_and_src() -> None:
    hits: list[str] = []
    for path in _iter_repo_text_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in SRC_EXEMPT_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if rel.startswith("src/") and SRC_META_COMMENT_RE.search(line):
                continue
            for label, pattern in FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    hits.append(f"{rel}:{lineno}: 命中禁用词[{label}] -> {line.strip()}")
    assert hits == [], "发现禁用措辞：\n" + "\n".join(hits)


def test_clause_map_disclaimer_and_whitelist() -> None:
    clause_map = (REPO_ROOT / "docs" / "clause-map.md").read_text(encoding="utf-8")
    assert "本表为开发者自行整理的对照，未经标准制定方确认，须由人工复核" in clause_map
    assert "待人工核对" in clause_map

    # 校验条款对照表里的每一个条款号和（标准号，条款号）组合都在白名单内
    found_sections = set(SECTION_RE.findall(clause_map))
    assert found_sections, "docs/clause-map.md 中未找到任何条款号"
    unexpected_sections = found_sections - ALLOWED_SECTIONS
    assert not unexpected_sections, f"条款对照表含白名单之外的条款号：{sorted(unexpected_sections)}"

    found_pairs = set(QUALIFIED_CLAUSE_RE.findall(clause_map))
    unexpected_pairs = found_pairs - ALLOWED_QUALIFIED_CLAUSES
    assert not unexpected_pairs, (
        f"条款对照表含白名单之外的（标准, 条款）：{sorted(unexpected_pairs)}"
    )

    # 全仓库文档中所有 § 条款号均不得超出 GOAL.md 白名单
    for rel_dir in ("docs", "examples"):
        for path in sorted((REPO_ROOT / rel_dir).rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            bad = set(SECTION_RE.findall(text)) - ALLOWED_SECTIONS
            assert not bad, f"{path.relative_to(REPO_ROOT)} 含白名单外条款号：{sorted(bad)}"


def test_cli_doc_matches_parser_command_tree() -> None:
    parser_commands = _cli_leaf_commands(build_parser())
    cli_md = (REPO_ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    doc_commands = _cli_doc_commands(cli_md)

    assert doc_commands, "未能从 docs/cli.md 解析出命令列表"
    unknown_in_doc = doc_commands - parser_commands
    assert not unknown_in_doc, f"docs/cli.md 列出了 CLI 中不存在的命令：{sorted(unknown_in_doc)}"

    missing_from_doc = parser_commands - doc_commands
    assert not missing_from_doc, f"CLI 存在但 docs/cli.md 未记录的命令：{sorted(missing_from_doc)}"


def test_methodology_covers_required_declarations_and_rules() -> None:
    methodology = (REPO_ROOT / "docs" / "methodology.md").read_text(encoding="utf-8")
    assert "此公式是本项目的定义" in methodology
    assert "预检结论" in methodology
    assert "不构成独立第三方验证" in methodology

    # 确认方法学文档覆盖报告模块定义的全部预检规则编号
    from geo_verify.report import rules as rules_mod

    expected_rules = set(re.findall(r'"(R-[A-Z-]+)"', Path(rules_mod.__file__).read_text("utf-8")))
    assert evaluate is not None
    missing_rules = {rid for rid in expected_rules if rid not in methodology}
    assert not missing_rules, f"docs/methodology.md 缺少预检规则说明：{sorted(missing_rules)}"
