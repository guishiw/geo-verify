"""`geo-verify evidence build | verify`：证据清单链、逐行索引与定位校验。"""

from __future__ import annotations

import argparse
from typing import Any

from geo_verify import audit
from geo_verify.cli import CommandResult, SubParsers, add_command, add_group
from geo_verify.errors import EXIT_CHECK_FAILED, EXIT_OK
from geo_verify.evidence.manifest import DEFAULT_ACTOR, PROOF_BOUNDARY, build_evidence
from geo_verify.evidence.verify import KIND_LABELS, RESULT_FILE, verify_evidence

ROLE_LABELS = {
    "raw": "原始",
    "snapshot": "快照",
    "config": "口径配置",
    "derived": "派生",
    "lock": "依赖锁",
    "index": "行索引",
}


def _where(item: dict[str, Any]) -> str:
    return f"{item['file']} 第 {item['line']} 行" if item["line"] is not None else item["file"]


def run_build(args: argparse.Namespace) -> CommandResult:
    data = build_evidence(
        args.project, now=audit.resolve_now(args.now), actor=args.actor or DEFAULT_ACTOR
    )
    lines: list[str] = []
    if data["unchanged"]:
        lines.append(
            f"原始数据与第 {data['sequence']} 份清单（{data['manifest']}）相比无变化，未生成新清单。"
        )
    else:
        roles = "、".join(
            f"{ROLE_LABELS[role]} {count}" for role, count in data["files_by_role"].items()
        )
        lines.append(
            f"已生成第 {data['sequence']} 份证据清单 {data['manifest']}："
            f"{data['files']} 个文件（{roles}）。"
        )
        lines.append(f"清单哈希：{data['manifest_hash']}")
        if data["previous_manifest_hash"]:
            lines.append(f"前一清单哈希：{data['previous_manifest_hash']}")
    lines.append(f"代码版本：{data['code_version']}；预先登记的 seed：{data['seed']}")
    lock = data["lock"]
    if lock["lock_hash"]:
        lines.append(f"依赖锁：{lock['path']}（SHA-256 {lock['lock_hash']}）")
    if lock["note"]:
        lines.append(f"说明：{lock['note']}")
    violations = data["append_only_violations"]
    if violations:
        lines.append(f"发现 {len(violations)} 处违反只追加约束（清单已生成，校验时会持续报告）：")
        lines.extend(f"  [违反只追加] {_where(item)}：{item['message']}" for item in violations)
    lines.append(PROOF_BOUNDARY)
    return CommandResult(
        data=data,
        text="\n".join(lines),
        exit_code=EXIT_CHECK_FAILED if violations else EXIT_OK,
    )


def run_verify(args: argparse.Namespace) -> CommandResult:
    data = verify_evidence(args.project)
    problems = data["problems"]
    if not problems:
        latest = data["latest_manifest"]
        head = (
            f"证据校验通过：以第 {latest['sequence']} 份清单为准，清单链共 {data['manifests']} 份，"
            f"已校验 {data['checked_files']} 个文件，未发现问题。"
        )
    else:
        head = f"证据校验未通过：发现 {len(problems)} 个问题。"
    lines = [head]
    lines.extend(
        f"  [{KIND_LABELS[item['kind']]}] {_where(item)}：{item['message']}" for item in problems
    )
    lines.append(PROOF_BOUNDARY)
    lines.append(f"结果已写入 out/{RESULT_FILE}。")
    return CommandResult(
        data=data,
        text="\n".join(lines),
        exit_code=EXIT_CHECK_FAILED if problems else EXIT_OK,
    )


def register(subparsers: SubParsers) -> None:
    group = add_group(subparsers, "evidence", help="证据清单链：生成与校验")

    build = add_command(group, "build", run_build, help="生成证据清单（原始数据无变化时不生成）")
    build.add_argument("project", help="项目目录")
    build.add_argument("--now", help="清单时间（UTC，如 2026-10-09T01:00:00Z），默认取当前时间")
    build.add_argument("--actor", help="操作人，写入审计事件 build_evidence")

    verify = add_command(group, "verify", run_verify, help="校验清单链和全部文件，定位到文件和行")
    verify.add_argument("project", help="项目目录")
