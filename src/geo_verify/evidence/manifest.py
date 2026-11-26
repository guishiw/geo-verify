"""生成证据清单（evidence build）。

步骤（顺序是为了不让清单因为自己写的审计事件而立刻失效）：

1. 读取最新清单并核对其契约、序号和 manifest_hash；对不上就拒绝续链（退出 1）。
2. 准备依赖锁，按覆盖范围计算候选文件清单（只在内存中计算，此时不写任何文件）。
3. 候选内容与最新清单相同（文件、代码版本、依赖锁、seed、口径版本都没变）：不生成新清单、
   不写审计事件，提示“无变化”。
4. 否则先追加审计事件 `build_evidence`，再重新计算文件清单——这次的 audit.jsonl 已含该事件——
   然后写行索引、写清单。清单覆盖含本次事件的审计日志，生成后立即校验仍然通过。

原始记录与快照只增不改：封存题库、差异记录、快照一旦列入清单，之后不得改动或消失；
原始 JSONL 只能在末尾追加。行索引只延长、不改写：原始 JSONL 只是在末尾追加时，沿用并延长原来的索引文件；
发现历史行被改写或删除时，保留旧索引不动，另起一代 `evidence/index/<文件名>.<序号>.lines`，
并把这一处只追加违规报告出来（清单照常生成，`evidence verify` 会持续报告这处违规）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geo_verify import audit, project, spec
from geo_verify.canon import hash_without, sha256_bytes, sha256_file
from geo_verify.errors import CheckFailed, InputError
from geo_verify.evidence import environment, scope
from geo_verify.evidence.lines import (
    first_divergence,
    parse_index,
    render_index,
    scan_file,
    split_lines,
)
from geo_verify.evidence.timestamp import NullTimestampProvider, TimestampProvider
from geo_verify.jsonl import read_json, write_json

SCHEMA_VERSION = "1.0"
MANIFEST_NAME = re.compile(r"^manifest-(\d{4,})\.json$")
DEFAULT_ACTOR = "geo-verify evidence build（未指明操作人）"
PROOF_BOUNDARY = (
    "哈希只能证明内容自清单生成以来未被改动，不能证明采集时间真实；"
    "可信时间戳仅提供接口，未接入任何时间戳服务。"
)
# 判断“有无变化”时比较的清单字段（不含序号、时间、链接和哈希）
CONTENT_FIELDS = (
    "project_id",
    "files",
    "tool",
    "code_version",
    "lock_hash",
    "seed",
    "definitions",
    "synthetic",
)


def manifest_relpath(sequence: int) -> str:
    return f"evidence/manifest-{sequence:04d}.json"


def manifest_hash(manifest: dict[str, Any]) -> str:
    """清单哈希：除 manifest_hash 外的全部字段（含时间戳令牌）的规范化 JSON 的 SHA-256。"""
    return hash_without(manifest, "manifest_hash")


def content_digest(manifest: dict[str, Any]) -> str:
    """交给时间戳服务签发的摘要：去掉 timestamp_token 和 manifest_hash。"""
    return hash_without(manifest, "manifest_hash", "timestamp_token")


def numbered_manifests(evidence_dir: Path) -> list[tuple[int, Path]]:
    """evidence/ 下文件名合乎 manifest-NNNN.json 的清单，按序号排序。"""
    if not evidence_dir.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in evidence_dir.glob("manifest-*.json"):
        match = MANIFEST_NAME.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    return sorted(found)


def index_as_of(root: Path, manifest: dict[str, Any], relpath: str) -> list[str] | None:
    """某份清单生成时的行索引：取该清单登记的字节数前缀并核对哈希。取不到或对不上返回 None。"""
    entry = next(
        (item for item in manifest["files"] if item["path"] == relpath and item["role"] == "index"),
        None,
    )
    path = root / relpath
    if entry is None or not path.is_file():
        return None
    with path.open("rb") as handle:
        data = handle.read(entry["bytes"])
    if len(data) != entry["bytes"] or sha256_bytes(data) != entry["sha256"]:
        return None
    try:
        values = parse_index(data)
    except ValueError:
        return None
    if entry["records"] is not None and len(values) != entry["records"]:
        return None
    return values


def describe_divergence(old_count: int, new_count: int, position: int) -> str:
    if position >= new_count:
        return (
            f"第 {position + 1} 行起的历史记录被删除（行数由 {old_count} 减为 {new_count}）；"
            "原始记录只能在末尾追加"
        )
    return f"第 {position + 1} 行被改写；原始记录只能在末尾追加"


@dataclass(frozen=True)
class _Plan:
    entries: list[dict[str, Any]]
    index_contents: dict[str, bytes]
    violations: list[dict[str, Any]]


def _entry(
    path: str, sha: str, size: int, role: str, records: int | None, line_index: str | None
) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": sha,
        "bytes": size,
        "role": role,
        "records": records,
        "line_index": line_index,
    }


def _violation(file: str, line: int | None, message: str) -> dict[str, Any]:
    return {"file": file, "line": line, "message": message}


def _choose_index(
    root: Path,
    latest: dict[str, Any] | None,
    before: dict[str, Any] | None,
    source: str,
    hashes: list[str],
    sequence: int,
) -> tuple[str, dict[str, Any] | None]:
    """决定本次行索引写到哪里：能延长旧索引就延长，否则另起一代（不覆盖任何已有索引）。"""
    if latest is not None and before is not None and before["line_index"] is not None:
        old = index_as_of(root, latest, before["line_index"])
        if old is None:
            return scope.index_relpath(source, sequence), _violation(
                source,
                None,
                f"上一份清单登记的行索引 {before['line_index']} 缺失或被改动，无法核对只追加约束",
            )
        position = first_divergence(old, hashes)
        if position is None:
            return before["line_index"], None
        return scope.index_relpath(source, sequence), _violation(
            source, position + 1, describe_divergence(len(old), len(hashes), position)
        )
    base = scope.index_relpath(source)
    if (root / base).exists():
        return scope.index_relpath(source, sequence), None
    return base, None


def _plan(root: Path, latest: dict[str, Any] | None, sequence: int) -> _Plan:
    """在内存中计算本次清单的文件条目与要写的行索引，不写任何文件。"""
    covered = scope.discover(root)
    previous = {item["path"]: item for item in latest["files"]} if latest else {}
    entries: list[dict[str, Any]] = []
    index_contents: dict[str, bytes] = {}
    violations: list[dict[str, Any]] = []
    present: set[str] = set()
    for item in covered:
        if item.role == "index":
            continue
        present.add(item.path)
        scan = scan_file(root / item.path, lines=item.indexed)
        before = previous.get(item.path)
        if scan.line_hashes is None:
            entries.append(_entry(item.path, scan.sha256, scan.size, item.role, None, None))
            if (
                item.role in scope.APPEND_ONLY_ROLES
                and before is not None
                and before["sha256"] != scan.sha256
            ):
                noun = scope.ROLE_NOUNS[item.role]
                violations.append(
                    _violation(item.path, None, f"只增不改的{noun}与上一份清单记录时不同")
                )
            continue
        target, violation = _choose_index(
            root, latest, before, item.path, scan.line_hashes, sequence
        )
        index_contents[target] = render_index(scan.line_hashes)
        entries.append(
            _entry(item.path, scan.sha256, scan.size, item.role, len(scan.line_hashes), target)
        )
        if violation is not None:
            violations.append(violation)
    for path, before in previous.items():
        if before["role"] in scope.APPEND_ONLY_ROLES and path not in present:
            noun = scope.ROLE_NOUNS[before["role"]]
            violations.append(_violation(path, None, f"上一份清单中的{noun}已不存在（被删除）"))
    existing = {item.path for item in covered if item.role == "index"}
    for relpath in sorted(existing | set(index_contents)):
        content = index_contents.get(relpath)
        if content is None:
            content = (root / relpath).read_bytes()
        entries.append(
            _entry(
                relpath,
                sha256_bytes(content),
                len(content),
                "index",
                len(split_lines(content)),
                None,
            )
        )
    entries.sort(key=lambda entry: entry["path"])
    violations.sort(key=lambda item: (item["file"], item["line"] or 0))
    return _Plan(entries, index_contents, violations)


def _prepare_lock(root: Path) -> dict[str, Any]:
    """依赖锁：项目目录下的 uv.lock 优先；否则把安装元数据里定位到的锁复制进证据包。"""
    project_lock = root / "uv.lock"
    if project_lock.is_file():
        return {
            "origin": "project",
            "path": "uv.lock",
            "lock_hash": sha256_file(project_lock),
            "note": None,
        }
    installed = environment.installed_lock_path()
    if installed is None:
        return {
            "origin": None,
            "path": None,
            "lock_hash": None,
            "note": (
                "未找到依赖锁：项目目录下没有 uv.lock，安装元数据中也定位不到锁文件，"
                "清单的 lock_hash 记为 null"
            ),
        }
    content = installed.read_bytes()
    target = root / scope.LOCK_COPY
    if not target.is_file() or target.read_bytes() != content:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return {
        "origin": "installed",
        "path": scope.LOCK_COPY,
        "lock_hash": sha256_bytes(content),
        "note": (
            f"项目目录下没有 uv.lock；已按安装元数据定位到工具的锁文件，并复制到 {scope.LOCK_COPY}"
        ),
    }


def _body(
    proj: dict[str, Any], entries: list[dict[str, Any]], lock: dict[str, Any]
) -> dict[str, Any]:
    prereg = proj["preregistration"]
    body: dict[str, Any] = {
        "project_id": proj["project_id"],
        "files": entries,
        "tool": environment.tool(),
        "code_version": environment.code_version(),
        "lock_hash": lock["lock_hash"],
        "seed": prereg["statistics"]["seed"],
        "definitions": {
            item["metric_id"]: item["definition_version"] for item in prereg["metrics"]
        },
    }
    if proj.get("synthetic") is True:
        body["synthetic"] = True
    return body


def _latest_for_build(evidence_dir: Path, project_id: str) -> dict[str, Any] | None:
    numbered = numbered_manifests(evidence_dir)
    if not numbered:
        return None
    number, path = numbered[-1]
    where = f"evidence/{path.name}"
    advice = "拒绝在其上续链；请先运行 geo-verify evidence verify 查明原因"
    try:
        manifest = read_json(path)
    except (InputError, UnicodeDecodeError) as exc:
        raise CheckFailed(f"最新清单 {where} 不是合法 JSON，{advice}") from exc
    if not spec.is_valid("manifest", manifest):
        raise CheckFailed(f"最新清单 {where} 不符合 manifest 契约，{advice}")
    if manifest["sequence"] != number:
        raise CheckFailed(
            f"最新清单 {where} 的 sequence 为 {manifest['sequence']}，与文件名序号不符，{advice}"
        )
    if manifest["project_id"] != project_id:
        raise CheckFailed(f"最新清单 {where} 属于另一个项目（{manifest['project_id']}），{advice}")
    if manifest_hash(manifest) != manifest["manifest_hash"]:
        raise CheckFailed(f"最新清单 {where} 的内容与其 manifest_hash 不符（清单被改动），{advice}")
    result: dict[str, Any] = manifest
    return result


def _assemble(
    proj: dict[str, Any],
    sequence: int,
    now: str,
    previous_hash: str | None,
    body: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": f"{proj['project_id'][:100]}-manifest-{sequence:04d}",
        "sequence": sequence,
        "created_at": now,
        "previous_manifest_hash": previous_hash,
        **body,
        "timestamp_token": None,
        "manifest_hash": "0" * 64,
    }


def _summary(
    manifest: dict[str, Any], lock: dict[str, Any], provider: TimestampProvider
) -> dict[str, Any]:
    roles = Counter(item["role"] for item in manifest["files"])
    return {
        "manifest": manifest_relpath(manifest["sequence"]),
        "sequence": manifest["sequence"],
        "manifest_hash": manifest["manifest_hash"],
        "previous_manifest_hash": manifest["previous_manifest_hash"],
        "files": len(manifest["files"]),
        "files_by_role": dict(sorted(roles.items())),
        "code_version": manifest["code_version"],
        "seed": manifest["seed"],
        "lock": lock,
        "timestamp": {
            "provider": provider.name,
            "stamped": manifest["timestamp_token"] is not None,
        },
        "notice": PROOF_BOUNDARY,
    }


def build_evidence(
    root: str | Path,
    *,
    now: str,
    actor: str = DEFAULT_ACTOR,
    provider: TimestampProvider | None = None,
) -> dict[str, Any]:
    """生成下一份证据清单；内容无变化时不生成。返回结果说明（命令据此给退出码）。"""
    stamper: TimestampProvider = provider if provider is not None else NullTimestampProvider()
    paths = project.paths(root)
    proj = project.load_project(root)
    latest = _latest_for_build(paths.evidence_dir, proj["project_id"])
    sequence = latest["sequence"] + 1 if latest else 1
    lock = _prepare_lock(paths.root)
    plan = _plan(paths.root, latest, sequence)
    body = _body(proj, plan.entries, lock)
    if latest is not None and all(body.get(key) == latest.get(key) for key in CONTENT_FIELDS):
        return {
            "created": False,
            "unchanged": True,
            **_summary(latest, lock, stamper),
            "append_only_violations": [],
            "audit_event_id": None,
        }

    previous_hash = latest["manifest_hash"] if latest else None
    relpath = manifest_relpath(sequence)
    spec.require_valid(
        "manifest", _assemble(proj, sequence, now, previous_hash, body), where=relpath
    )
    event = audit.append_event(
        paths.root,
        actor=actor,
        action="build_evidence",
        target_type="manifest",
        target_id=f"{proj['project_id'][:100]}-manifest-{sequence:04d}",
        reason=f"生成第 {sequence} 份证据清单（{relpath}）",
        occurred_at=now,
        before_hash=previous_hash,
        details={
            "sequence": sequence,
            "manifest": relpath,
            "previous_manifest_hash": previous_hash,
        },
        synthetic=True if proj.get("synthetic") is True else None,
    )
    plan = _plan(paths.root, latest, sequence)
    for index_path, content in plan.index_contents.items():
        target = paths.root / index_path
        if not target.is_file() or target.read_bytes() != content:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    manifest = _assemble(proj, sequence, now, previous_hash, _body(proj, plan.entries, lock))
    manifest["timestamp_token"] = stamper.stamp(content_digest(manifest))
    manifest["manifest_hash"] = manifest_hash(manifest)
    spec.require_valid("manifest", manifest, where=relpath)
    write_json(paths.root / relpath, manifest)
    return {
        "created": True,
        "unchanged": False,
        **_summary(manifest, lock, stamper),
        "append_only_violations": plan.violations,
        "audit_event_id": event["event_id"],
    }
