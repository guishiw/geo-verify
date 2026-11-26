"""校验证据包（evidence verify）。

依次检查：

1. 清单链：每份清单的契约、project_id、文件名序号与 sequence、manifest_hash；序号连续；
   相邻清单的 previous_manifest_hash 与前一份的 manifest_hash 相符。
2. 文件：以最新的可读清单为准，逐个核对哈希；JSONL 借助行索引定位到行
   （修改、删除、插入含末尾追加、重排）；覆盖范围内清单外的新文件记为新增。
3. 只追加约束：相邻两份清单之间，原始 JSONL 旧的行索引必须是新行索引的前缀，
   其他原始文件（封存题库、差异记录）和快照不得改动或消失。
4. 审计链（audit.verify_chain），以及审计日志中的 build_evidence 事件与清单文件是否一一对得上
   （可发现被删除的最新清单、没有生成记录的插入清单）。
5. 封存题库（project.load_sealed_prompt_sets）。

结果写入 out/evidence-verify.json；不含绝对路径和生成时间，复制到别的目录校验结果逐字节相同。
"""

from __future__ import annotations

import json
import os
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

from geo_verify import audit, project, spec
from geo_verify.errors import CheckFailed, InputError
from geo_verify.evidence import scope
from geo_verify.evidence.lines import (
    LineChange,
    first_divergence,
    locate_changes,
    scan_file,
    split_lines,
)
from geo_verify.evidence.manifest import (
    MANIFEST_NAME,
    PROOF_BOUNDARY,
    describe_divergence,
    index_as_of,
    manifest_hash,
    manifest_relpath,
)
from geo_verify.jsonl import write_json

SCHEMA_VERSION = "1.0"
RESULT_FILE = "evidence-verify.json"
KINDS: tuple[str, ...] = (
    "no_manifest",
    "manifest_invalid",
    "manifest_modified",
    "manifest_chain_broken",
    "file_missing",
    "file_modified",
    "file_added",
    "line_modified",
    "line_deleted",
    "line_inserted",
    "line_reordered",
    "append_only_violated",
    "audit_chain",
    "sealed_prompt_set",
)
KIND_LABELS: dict[str, str] = {
    "no_manifest": "没有清单",
    "manifest_invalid": "清单不合法",
    "manifest_modified": "清单被改动",
    "manifest_chain_broken": "清单链断裂",
    "file_missing": "文件缺失",
    "file_modified": "文件被改动",
    "file_added": "清单外新增文件",
    "line_modified": "行被改动",
    "line_deleted": "行被删除",
    "line_inserted": "行被插入",
    "line_reordered": "行序变化",
    "append_only_violated": "违反只追加",
    "audit_chain": "审计链问题",
    "sealed_prompt_set": "封存题库问题",
}
_SEALED_NAME = re.compile(r"v\d{4,}\.json")
_Chain = list[tuple[int, str, dict[str, Any] | None]]


def _problem(file: str, kind: str, line: int | None, message: str) -> dict[str, Any]:
    return {"file": file, "kind": kind, "line": line, "message": message}


def _sort_key(problem: dict[str, Any]) -> tuple[str, int, int, str]:
    return (
        problem["file"],
        problem["line"] or 0,
        KINDS.index(problem["kind"]),
        problem["message"],
    )


def _identity(project_file: Path) -> tuple[str | None, bool | None]:
    """宽松读取 project.json 的 project_id 与 synthetic；读不了时交给清单补足。"""
    try:
        data = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    project_id = data.get("project_id")
    return (project_id if isinstance(project_id, str) else None), data.get("synthetic") is True


def _load_manifest(
    path: Path, relpath: str, number: int, project_id: str | None, problems: list[dict[str, Any]]
) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        problems.append(_problem(relpath, "manifest_invalid", None, "不是合法的 UTF-8 JSON"))
        return None
    issues = spec.iter_issues("manifest", data)
    if issues:
        first = issues[0]
        problems.append(
            _problem(
                relpath,
                "manifest_invalid",
                None,
                f"不符合 manifest 契约（共 {len(issues)} 处，首处 {first.path or '/'}：{first.message}）",
            )
        )
        return None
    manifest: dict[str, Any] = data
    if project_id is not None and manifest["project_id"] != project_id:
        problems.append(
            _problem(
                relpath,
                "manifest_invalid",
                None,
                f"清单的 project_id（{manifest['project_id']}）与项目（{project_id}）不符",
            )
        )
        return None
    if manifest["sequence"] != number:
        problems.append(
            _problem(
                relpath,
                "manifest_chain_broken",
                None,
                f"文件名序号 {number} 与清单内 sequence {manifest['sequence']} 不符"
                "（清单可能被改名、插入或重排）",
            )
        )
    if manifest_hash(manifest) != manifest["manifest_hash"]:
        problems.append(
            _problem(
                relpath, "manifest_modified", None, "清单内容与其 manifest_hash 不符（清单被改动）"
            )
        )
    return manifest


def _load_chain(
    evidence_dir: Path, project_id: str | None, problems: list[dict[str, Any]]
) -> _Chain:
    if not evidence_dir.is_dir():
        return []
    chain: _Chain = []
    seen: set[int] = set()
    for path in sorted(evidence_dir.glob("manifest*.json")):
        relpath = f"evidence/{path.name}"
        match = MANIFEST_NAME.match(path.name)
        if match is None:
            problems.append(
                _problem(relpath, "manifest_invalid", None, "文件名不符合 manifest-NNNN.json 约定")
            )
            continue
        number = int(match.group(1))
        if number in seen:
            problems.append(
                _problem(relpath, "manifest_chain_broken", None, f"清单序号 {number} 重复")
            )
            continue
        seen.add(number)
        chain.append((number, relpath, _load_manifest(path, relpath, number, project_id, problems)))
    chain.sort(key=lambda item: item[0])
    _check_links(chain, problems)
    return chain


def _check_links(chain: _Chain, problems: list[dict[str, Any]]) -> None:
    numbers = {number for number, _, _ in chain}
    for missing in sorted(set(range(1, max(numbers, default=0) + 1)) - numbers):
        problems.append(
            _problem(
                manifest_relpath(missing),
                "manifest_chain_broken",
                None,
                f"缺少第 {missing} 份清单（可能被删除）",
            )
        )
    for (n_prev, _, prev), (n_cur, relpath, cur) in pairwise(chain):
        if prev is None or cur is None or n_cur != n_prev + 1:
            continue
        if cur["previous_manifest_hash"] != prev["manifest_hash"]:
            problems.append(
                _problem(
                    relpath,
                    "manifest_chain_broken",
                    None,
                    f"previous_manifest_hash 与第 {n_prev} 份清单的 manifest_hash 不符"
                    "（前一份清单被改动后重算了哈希，或清单被替换）",
                )
            )


def _change_message(change: LineChange) -> str:
    if change.kind == "line_modified":
        moved = "" if change.old_line == change.line else f"（记录时为第 {change.old_line} 行）"
        return f"第 {change.line} 行内容与清单记录不符{moved}"
    if change.kind == "line_deleted":
        return f"清单记录的第 {change.line} 行已不存在（被删除）"
    if change.kind == "line_inserted":
        return f"第 {change.line} 行不在清单记录中（清单生成后插入或追加）"
    return f"第 {change.line} 行原为第 {change.old_line} 行（行的先后次序被改变）"


def _check_files(root: Path, manifest: dict[str, Any], problems: list[dict[str, Any]]) -> int:
    label = f"第 {manifest['sequence']} 份清单"
    recorded: set[str] = set()
    for entry in manifest["files"]:
        relpath = entry["path"]
        recorded.add(relpath)
        full = root / relpath
        if not full.is_file():
            problems.append(
                _problem(relpath, "file_missing", None, f"{label}登记的文件不存在（被删除或移动）")
            )
            continue
        index_path = entry["line_index"]
        scan = scan_file(full, lines=index_path is not None)
        old = index_as_of(root, manifest, index_path) if index_path is not None else None
        current = scan.line_hashes or []
        if scan.sha256 == entry["sha256"]:
            if index_path is not None and old is not None and old != current:
                problems.append(
                    _problem(
                        index_path,
                        "manifest_invalid",
                        None,
                        f"行索引与 {relpath} 的逐行哈希不一致（清单登记的索引不是由该文件生成的）",
                    )
                )
            continue
        modified = f"文件哈希与{label}的记录不符（文件被改动）"
        if index_path is None:
            problems.append(_problem(relpath, "file_modified", None, modified))
            continue
        if old is None:
            problems.append(
                _problem(
                    relpath,
                    "file_modified",
                    None,
                    f"{modified}；行索引不可用（缺失或被改动），无法定位到行",
                )
            )
            continue
        changes = locate_changes(old, current)
        if not changes:
            problems.append(
                _problem(
                    relpath,
                    "file_modified",
                    None,
                    f"{modified}；各行内容与行索引一致，差异只在换行符（如文件末尾的换行）",
                )
            )
            continue
        problems.append(
            _problem(
                relpath,
                "file_modified",
                None,
                f"{modified}；共 {len(changes)} 处行级差异，见同文件的逐行问题",
            )
        )
        problems.extend(
            _problem(relpath, change.kind, change.line, _change_message(change))
            for change in changes
        )
    for item in scope.discover(root):
        if item.path not in recorded:
            problems.append(
                _problem(item.path, "file_added", None, f"文件不在{label}中（清单生成后新增）")
            )
    return len(manifest["files"])


def _check_append_only(
    root: Path, valid: list[tuple[int, dict[str, Any]]], problems: list[dict[str, Any]]
) -> None:
    for (n_prev, prev), (n_cur, cur) in pairwise(valid):
        later = {item["path"]: item for item in cur["files"]}
        span = f"第 {n_prev} 份到第 {n_cur} 份清单之间"
        for entry in prev["files"]:
            if entry["role"] not in scope.APPEND_ONLY_ROLES:
                continue
            relpath = entry["path"]
            noun = scope.ROLE_NOUNS[entry["role"]]
            after = later.get(relpath)
            if after is None:
                problems.append(
                    _problem(
                        relpath,
                        "append_only_violated",
                        None,
                        f"{span}，{noun}从清单中消失（被删除）",
                    )
                )
                continue
            if entry["line_index"] is None:
                if after["sha256"] != entry["sha256"]:
                    problems.append(
                        _problem(
                            relpath,
                            "append_only_violated",
                            None,
                            f"{span}，只增不改的{noun}被改动",
                        )
                    )
                continue
            old = index_as_of(root, prev, entry["line_index"])
            new = index_as_of(root, cur, after["line_index"]) if after["line_index"] else None
            if old is None or new is None:
                problems.append(
                    _problem(
                        relpath,
                        "append_only_violated",
                        None,
                        f"{span}的行索引取不到（缺失或被改动），无法核对只追加约束",
                    )
                )
                continue
            position = first_divergence(old, new)
            if position is not None:
                problems.append(
                    _problem(
                        relpath,
                        "append_only_violated",
                        position + 1,
                        f"{span}，{describe_divergence(len(old), len(new), position)}",
                    )
                )


def _read_audit(path: Path) -> list[dict[str, Any]]:
    """宽松读取审计日志：读不了的行用空对象占位，保证事件序号与行号一致。"""
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for raw in split_lines(path.read_bytes()):
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        events.append(value if isinstance(value, dict) else {})
    return events


def _check_audit(
    events: list[dict[str, Any]], chain: _Chain, problems: list[dict[str, Any]]
) -> None:
    for item in audit.verify_chain(events):
        problems.append(
            _problem(
                "audit.jsonl",
                "audit_chain",
                item["index"],
                f"第 {item['index']} 条审计事件：{item['message']}",
            )
        )
    recorded: set[int] = set()
    for event in events:
        details = event.get("details")
        sequence = details.get("sequence") if isinstance(details, dict) else None
        if event.get("action") == "build_evidence" and type(sequence) is int:
            recorded.add(sequence)
    top = max((number for number, _, _ in chain), default=0)
    for sequence in sorted(recorded):
        if sequence > top:
            problems.append(
                _problem(
                    manifest_relpath(sequence),
                    "manifest_chain_broken",
                    None,
                    f"审计日志记录了第 {sequence} 份清单的生成，但清单文件不存在（可能被删除）",
                )
            )
    for number, relpath, _ in chain:
        if number not in recorded:
            problems.append(
                _problem(
                    relpath,
                    "manifest_chain_broken",
                    None,
                    "审计日志中没有对应的 build_evidence 事件（可能是插入或伪造的清单）",
                )
            )


def _relative(message: str, root: Path) -> str:
    for prefix in {f"{root}{os.sep}", f"{root.resolve()}{os.sep}"}:
        message = message.replace(prefix, "")
    return message


def _check_sealed(root: Path, problems: list[dict[str, Any]]) -> None:
    try:
        project.load_sealed_prompt_sets(root)
    except (CheckFailed, InputError) as exc:
        message = _relative(exc.message, root)
        details = exc.details if isinstance(exc.details, dict) else {}
        name = details.get("file")
        if not isinstance(name, str):
            match = _SEALED_NAME.search(message)
            name = match.group(0) if match else None
        problems.append(
            _problem(
                f"prompts/sealed/{name}" if name else "prompts/sealed",
                "sealed_prompt_set",
                None,
                message,
            )
        )


def verify_evidence(root: str | Path) -> dict[str, Any]:
    """校验证据包并写出 out/evidence-verify.json，返回结果。"""
    paths = project.paths(root)
    problems: list[dict[str, Any]] = []
    project_id, synthetic = _identity(paths.project_file)
    chain = _load_chain(paths.evidence_dir, project_id, problems)
    valid = [(number, manifest) for number, _, manifest in chain if manifest is not None]
    reference = valid[-1][1] if valid else None
    checked = 0
    if not chain:
        problems.append(
            _problem(
                "evidence",
                "no_manifest",
                None,
                "没有任何证据清单：请先运行 geo-verify evidence build",
            )
        )
    if reference is not None:
        checked = _check_files(paths.root, reference, problems)
        project_id = project_id or reference["project_id"]
        synthetic = synthetic if synthetic is not None else reference.get("synthetic") is True
    _check_append_only(paths.root, valid, problems)
    _check_audit(_read_audit(paths.audit), chain, problems)
    _check_sealed(paths.root, problems)
    problems.sort(key=_sort_key)
    passed = not problems
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "evidence_verify",
        "project_id": project_id,
        "status": "pass" if passed else "fail",
        "status_label": "通过" if passed else "不通过",
        "manifests": len(chain),
        "latest_manifest": (
            {"sequence": reference["sequence"], "manifest_hash": reference["manifest_hash"]}
            if reference is not None
            else None
        ),
        "checked_files": checked,
        "problems": problems,
        "notice": PROOF_BOUNDARY,
    }
    if synthetic:
        result["synthetic"] = True
    write_json(paths.out_dir / RESULT_FILE, result)
    return result
