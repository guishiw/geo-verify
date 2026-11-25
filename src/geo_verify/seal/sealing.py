"""题库封存：计算内容哈希、生成版本链和差异记录，并写审计事件。

封存后的文件只增不改：内容有任何增、删、改都产生新版本；旧版本原样保留。
题目顺序不算内容变化（指标按 query_id 计算，与顺序无关）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from geo_verify import audit, project, spec
from geo_verify.errors import InputError
from geo_verify.jsonl import read_jsonl, write_json
from geo_verify.seal.diff import diff_prompts, is_empty
from geo_verify.seal.lint import DEFAULT_NEAR_THRESHOLD, Finding, has_errors, lint_prompts

SCHEMA_VERSION = "1.0"


def read_draft(path: Path) -> list[dict[str, Any]]:
    prompts = read_jsonl(path)
    if not prompts:
        raise InputError(f"草稿题库为空：{path}")
    return prompts


def sealed_relpath(version: int) -> str:
    return f"prompts/sealed/v{version:04d}.json"


def diff_relpath(from_version: int, to_version: int) -> str:
    return f"prompts/diffs/v{from_version:04d}-v{to_version:04d}.json"


def _summary(findings: list[Finding]) -> dict[str, Any]:
    return {
        "findings": [item.to_dict() for item in findings],
        "errors": sum(item.severity == "error" for item in findings),
        "warnings": sum(item.severity == "warning" for item in findings),
    }


def seal_prompts(
    root: str | Path,
    *,
    by: str,
    reason: str | None,
    now: str,
    near_threshold: float = DEFAULT_NEAR_THRESHOLD,
) -> dict[str, Any]:
    """封存草稿题库，返回结果说明。lint 有 error 时不封存（sealed=False, refused=True）。"""
    if not by or not by.strip():
        raise InputError("必须写明封存人（--by）")
    paths = project.paths(root)
    proj = project.load_project(root)
    entities = project.load_entities(root)
    sealed = project.load_sealed_prompt_sets(root)
    prompts = read_draft(paths.draft)
    findings = lint_prompts(prompts, entities, near_threshold=near_threshold)
    latest = sealed[-1] if sealed else None
    registered_hash = project.preregistration_hash(proj)
    result: dict[str, Any] = {
        "sealed": False,
        "refused": False,
        "changed": None,
        "prompts": len(prompts),
        "version": latest["version"] if latest else None,
        "content_hash": latest["content_hash"] if latest else None,
        "preregistration_hash": registered_hash,
        "preregistration_changed": (
            latest is not None and latest["preregistration_hash"] != registered_hash
        ),
        **_summary(findings),
    }
    if has_errors(findings):
        result["refused"] = True
        return result

    difference = diff_prompts(latest["prompts"], prompts) if latest else None
    if difference is not None and is_empty(difference):
        result["changed"] = False
        return result
    if latest is not None and not (reason and reason.strip()):
        raise InputError("题库内容有变化，必须用 --reason 写明变更理由")

    version = latest["version"] + 1 if latest else 1
    prompt_set: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "prompt_set_id": f"{proj['project_id']}-prompts",
        "project_id": proj["project_id"],
        "version": version,
        "previous_hash": latest["content_hash"] if latest else None,
        "prompts": prompts,
        "preregistration_hash": registered_hash,
        "sealed_by": by,
        "sealed_at": now,
    }
    if proj.get("synthetic") is True:
        prompt_set["synthetic"] = True
    prompt_set["content_hash"] = project.prompt_content_hash(prompt_set)
    spec.require_valid("prompt_set", prompt_set, where="待封存题库")
    target = paths.sealed_file(version)
    if target.exists():
        raise InputError(f"封存文件已存在，拒绝覆盖：{sealed_relpath(version)}")
    write_json(target, prompt_set)

    result.update(
        sealed=True,
        changed=True,
        version=version,
        content_hash=prompt_set["content_hash"],
        previous_hash=prompt_set["previous_hash"],
        file=sealed_relpath(version),
        preregistration_changed=False,
    )
    target_id = f"{prompt_set['prompt_set_id']}@{version}"
    if latest is None or difference is None:
        event = audit.append_event(
            root,
            actor=by,
            action="seal",
            target_type="prompt_set",
            target_id=target_id,
            reason=reason.strip() if reason and reason.strip() else "首次封存题库",
            occurred_at=now,
            after_hash=prompt_set["content_hash"],
            details={"version": version, "prompts": len(prompts), "file": sealed_relpath(version)},
            synthetic=prompt_set.get("synthetic"),
        )
    else:
        assert reason is not None
        relpath = diff_relpath(latest["version"], version)
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "project_id": proj["project_id"],
            "prompt_set_id": prompt_set["prompt_set_id"],
            "from_version": latest["version"],
            "to_version": version,
            "from_hash": latest["content_hash"],
            "to_hash": prompt_set["content_hash"],
            "reason": reason,
            "created_by": by,
            "created_at": now,
            **difference,
        }
        if prompt_set.get("synthetic") is True:
            record["synthetic"] = True
        write_json(paths.root / relpath, record)
        result["diff_file"] = relpath
        event = audit.append_event(
            root,
            actor=by,
            action="amend_prompts",
            target_type="prompt_set",
            target_id=target_id,
            reason=reason,
            occurred_at=now,
            before_hash=latest["content_hash"],
            after_hash=prompt_set["content_hash"],
            details={
                "from_version": latest["version"],
                "to_version": version,
                "added": len(difference["added"]),
                "removed": len(difference["removed"]),
                "modified": len(difference["modified"]),
                "file": sealed_relpath(version),
                "diff_file": relpath,
            },
            synthetic=prompt_set.get("synthetic"),
        )
    result["audit_event_id"] = event["event_id"]
    return result


def diff_versions(
    root: str | Path, from_version: int | None, to_version: int | None
) -> dict[str, Any]:
    """比较两个封存版本；默认比较最近两个版本。"""
    sealed = project.load_sealed_prompt_sets(root)
    if len(sealed) < 2:
        raise InputError(f"需要至少两个封存版本才能比较（现有 {len(sealed)} 个）")
    start = from_version if from_version is not None else len(sealed) - 1
    stop = to_version if to_version is not None else len(sealed)
    for version in (start, stop):
        if not 1 <= version <= len(sealed):
            raise InputError(f"没有第 {version} 版封存题库（现有 1～{len(sealed)} 版）")
    if start == stop:
        raise InputError("--from 与 --to 不能是同一个版本")
    old, new = sealed[start - 1], sealed[stop - 1]
    return {
        "from_version": start,
        "to_version": stop,
        "from_hash": old["content_hash"],
        "to_hash": new["content_hash"],
        **diff_prompts(old["prompts"], new["prompts"]),
    }
