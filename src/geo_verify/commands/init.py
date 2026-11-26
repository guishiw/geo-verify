"""`geo-verify init`：新建项目骨架或写入虚构汽车品牌完整样例。"""

from __future__ import annotations

import argparse
import json
import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from geo_verify import audit, spec
from geo_verify.cli import CommandResult, SubParsers, add_command
from geo_verify.errors import InputError
from geo_verify.jsonl import dumps_line, write_json

DEFAULT_SEED = 20261001
DEFAULT_SKELETON_ID = "geo-project"
EXAMPLES = ("auto",)
PROJECT_SUBDIRS = (
    "prompts/sealed",
    "prompts/diffs",
    "snapshots",
    "recheck",
    "evidence",
    "out",
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _validate_project_id(value: str) -> str:
    if not _ID_RE.match(value):
        raise InputError(
            f"非法的 --project-id：{value!r}（须以字母或数字开头，仅含字母、数字及 . _ : -，最长 128 字符）"
        )
    return value


def _default_project_id(root: Path) -> str:
    name = root.resolve().name
    return name if _ID_RE.match(name) else DEFAULT_SKELETON_ID


def _prepare_target(root: Path) -> None:
    if root.exists():
        if not root.is_dir():
            raise InputError(f"目标路径已存在且不是目录：{root}")
        if any(root.iterdir()):
            raise InputError(f"目标目录非空，拒绝初始化：{root}")
    else:
        root.mkdir(parents=True, exist_ok=True)
    for subdir in PROJECT_SUBDIRS:
        (root / subdir).mkdir(parents=True, exist_ok=True)


def _skeleton_project(project_id: str, registered_at: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "project_id": project_id,
        "name": "请填写项目名称（如：某品牌 2026Q4 监测预检）",
        "industry": "请填写行业（如：auto / consumer）",
        "client": {"id": "replace-client-id", "name": "请填写委托方名称"},
        "provider": {"id": "replace-provider-id", "name": "请填写服务方名称"},
        "timezone": "Asia/Shanghai",
        "platforms": [
            {
                "platform_id": "replace-platform-a",
                "display_name": "请填写平台名称",
                "channels": ["api", "manual"],
            }
        ],
        "target_entity_id": "brand-target",
        "competitor_entity_ids": ["brand-competitor"],
        "environment": {
            "logged_in": False,
            "history_cleared": True,
            "web_search": True,
            "region": "CN",
            "device": None,
        },
        "preregistration": {
            "registered_at": registered_at,
            "registered_by": "请填写登记人（建议甲乙双方共同确认）",
            "metrics": [
                {
                    "metric_id": "mention_rate",
                    "definition_version": "1.0",
                    "role": "acceptance",
                    "threshold": {"op": ">=", "value": 0.6},
                },
                {
                    "metric_id": "accuracy",
                    "definition_version": "1.0",
                    "role": "acceptance",
                    "threshold": {"op": ">=", "value": 0.9},
                },
                {
                    "metric_id": "sentiment_distribution",
                    "definition_version": "1.0",
                    "role": "descriptive",
                    "threshold": None,
                },
            ],
            "accuracy_basis": "verifiable",
            "recommendation_success": {"criterion": "recommended", "top_n": None},
            "crosscheck": {
                "formula": "symmetric_relative_difference",
                "threshold": 0.3,
                "metric_id": "mention_rate",
                "channel_a": "api",
                "channel_b": "manual",
            },
            "sufficiency": {"min_windows": 3, "min_valid_observations": 30},
            "statistics": {
                "confidence_level": 0.95,
                "bootstrap_iterations": 1000,
                "seed": DEFAULT_SEED,
            },
            "recheck": {"fraction": 0.2, "seed": DEFAULT_SEED},
            "target_sources": ["brand.example"],
            "authority": {
                "authoritative_tiers": ["A", "B"],
                "domain_tiers": {"gov-data.example": "A", "brand.example": "C"},
            },
            "exclusion_rules": [
                {"rule_id": "EX-DUP", "description": "重复导入或重复采集的冗余记录"}
            ],
        },
        "disclosure": {
            "statement": "请如实填写利益关系声明；本工具只给出预检结论，不构成独立第三方验证。",
            "relationships": [
                {
                    "party": "请填写服务方或相关方名称",
                    "role": "服务方",
                    "relationship": "请说明委托关系或利益关联",
                }
            ],
            "tool_operator": "请填写工具操作方名称",
            "operator_is_provider": True,
        },
        "ext": {
            "template_notes": {
                "placeholders": "所有带“请填写”或 replace- 的字段都须在封存题库（prompts seal）前替换为项目实际信息。",
                "preregistration": "预先登记内容会在封存题库时计算哈希；封存后修改会导致验收指标降为事后指标、预检不通过。",
                "sufficiency": "默认要求至少 3 个时间窗口、单一关键指标至少 30 次有效观测（047 §8.3）。",
                "crosscheck": "偏离度 = |A - B| / ((A + B) / 2)，此公式为本项目定义，默认阈值 30%。",
                "recheck": "默认按固定 seed 抽取 20% 题目做人工复核。",
                "live_notice": "默认不联网；真实平台调用须显式 --live，且平台配置项须人工核实。",
            }
        },
    }


def _skeleton_prompts() -> list[dict[str, Any]]:
    return [
        {
            "query_id": "Q001",
            "text": "请替换为实际评测题目：例如“20 万元左右的纯电 SUV 有哪些值得关注？”",
            "intent_type": "category",
            "business_level": "L3",
            "priority": "P0",
            "source": "模板示例（封存前请替换）",
            "expects_recommendation": True,
        },
        {
            "query_id": "Q002",
            "text": "请替换为实际评测题目：例如“某品牌主力车型的续航和质保政策如何？”",
            "intent_type": "product",
            "business_level": "L1",
            "priority": "P1",
            "source": "模板示例（封存前请替换）",
            "expects_recommendation": False,
        },
    ]


def _write_jsonl_lines(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(dumps_line(record) + "\n" for record in records)
    path.write_text(text, encoding="utf-8", newline="\n")


def _init_skeleton(root: Path, project_id: str, now: str) -> tuple[list[str], bool]:
    doc = _skeleton_project(project_id, now)
    spec.require_valid("project", doc, where="project.json 模板")
    write_json(root / "project.json", doc)
    (root / "entities.jsonl").write_text("", encoding="utf-8", newline="\n")
    _write_jsonl_lines(root / "prompts" / "draft.jsonl", _skeleton_prompts())
    return ["project.json", "entities.jsonl", "prompts/draft.jsonl"], False


def _example_root(name: str) -> Traversable:
    base = resources.files("geo_verify").joinpath("examples", name)
    if not base.is_dir():
        raise InputError(f"找不到内置样例目录：{name}，安装可能不完整")
    return base


def _rewrite_jsonl_project_id(raw_text: str, project_id: str) -> str:
    lines: list[str] = []
    for line in raw_text.splitlines():
        record = json.loads(line)
        record["project_id"] = project_id
        lines.append(dumps_line(record))
    return "\n".join(lines) + "\n"


def _init_example(
    root: Path,
    name: str,
    project_id_override: str | None,
    now_override: str | None,
) -> tuple[str, list[str], bool]:
    source_dir = _example_root(name)
    created: list[str] = []

    project_doc: dict[str, Any] = json.loads(
        source_dir.joinpath("project.json").read_text(encoding="utf-8")
    )
    if project_id_override is not None:
        project_doc["project_id"] = project_id_override
    if now_override is not None:
        project_doc["preregistration"]["registered_at"] = now_override
    spec.require_valid("project", project_doc, where="样例 project.json")
    write_json(root / "project.json", project_doc)
    created.append("project.json")

    project_id: str = project_doc["project_id"]
    copy_specs = (
        ("entities.jsonl", True),
        ("prompts/draft.jsonl", False),
        ("inbox/observations.csv", False),
        ("inbox/judgments.jsonl", True),
    )
    for relpath, rewrite_id in copy_specs:
        src = source_dir
        for part in relpath.split("/"):
            src = src.joinpath(part)
        dest = root / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        if rewrite_id and project_id_override is not None:
            rewritten = _rewrite_jsonl_project_id(src.read_text(encoding="utf-8"), project_id)
            dest.write_text(rewritten, encoding="utf-8", newline="\n")
        else:
            dest.write_bytes(src.read_bytes())
        created.append(relpath)

    return project_id, created, bool(project_doc.get("synthetic"))


def run(args: argparse.Namespace) -> CommandResult:
    root = Path(args.project)
    project_id_override = (
        _validate_project_id(args.project_id) if args.project_id is not None else None
    )
    now_override = audit.resolve_now(args.now) if args.now is not None else None
    _prepare_target(root)

    if args.example is None:
        project_id = project_id_override or _default_project_id(root)
        now = now_override or audit.resolve_now(None)
        created, synthetic = _init_skeleton(root, project_id, now)
        lines = [
            f"已初始化项目骨架：{root}（project_id = {project_id}）。",
            "已创建文件：" + "、".join(created),
            "下一步：填写 project.json（替换占位项）、补充 entities.jsonl 与 prompts/draft.jsonl，再运行 prompts lint 与 prompts seal。",
        ]
    else:
        project_id, created, synthetic = _init_example(
            root, args.example, project_id_override, now_override
        )
        lines = [
            f"已写入合成样例 {args.example} 到 {root}（project_id = {project_id}，synthetic = true）。",
            "已创建文件：" + "、".join(created),
            "注意：样例数据全部为虚构（品牌、产品、域名均为合成），不代表任何真实平台表现。",
            "下一步：依次运行 prompts lint、prompts seal、import --input inbox/observations.csv、annotate rules，",
            "或直接执行 examples/auto/run.sh 走完整个预检流程。",
        ]

    data: dict[str, Any] = {
        "project_dir": root.name,
        "project_id": project_id,
        "example": args.example,
        "synthetic": synthetic,
        "created_files": created,
        "directories": list(PROJECT_SUBDIRS),
    }
    return CommandResult(data=data, text="\n".join(lines))


def register(subparsers: SubParsers) -> None:
    parser = add_command(
        subparsers,
        "init",
        run,
        help="新建项目骨架，或写入内置虚构样例（--example auto）",
    )
    parser.add_argument("project", help="待初始化的项目目录（须不存在或为空）")
    parser.add_argument(
        "--example",
        choices=EXAMPLES,
        default=None,
        help="写入内置虚构样例（可选：auto）",
    )
    parser.add_argument("--project-id", default=None, help="项目编号；默认取目录名或样例默认编号")
    parser.add_argument(
        "--now",
        default=None,
        help="预先登记时间（UTC ISO 8601，如 2026-10-01T01:00:00Z）",
    )
