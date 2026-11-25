"""项目目录约定与基础读取。

一个项目就是一个目录，各模块只通过这里约定的文件交换数据：

    project.json            project 契约
    entities.jsonl          entity 契约
    prompts/draft.jsonl     草稿题库（每行一题）
    prompts/sealed/vNNNN.json  已封存题库（prompt_set 契约），只增不改
    prompts/diffs/          版本差异记录
    observations.jsonl      observation 契约，只追加
    snapshots/              快照文件
    annotations.jsonl       annotation 契约，只追加
    judgments.jsonl         judgment 契约（由事实核验工具产出）
    audit.jsonl             audit_event 契约，哈希链，只追加
    recheck/                抽样复核计划与结果
    evidence/               证据清单与行索引
    out/                    指标、交叉验证、告警、报告等生成物
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geo_verify import spec
from geo_verify.canon import hash_obj
from geo_verify.errors import CheckFailed, InputError
from geo_verify.jsonl import read_json, read_jsonl

PROMPT_HASH_FIELDS = ("prompt_set_id", "project_id", "version", "previous_hash", "prompts")
ANNOTATOR_PRIORITY = {"human": 3, "model": 2, "rule": 1}


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def project_file(self) -> Path:
        return self.root / "project.json"

    @property
    def entities(self) -> Path:
        return self.root / "entities.jsonl"

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    @property
    def draft(self) -> Path:
        return self.prompts_dir / "draft.jsonl"

    @property
    def sealed_dir(self) -> Path:
        return self.prompts_dir / "sealed"

    @property
    def diffs_dir(self) -> Path:
        return self.prompts_dir / "diffs"

    @property
    def observations(self) -> Path:
        return self.root / "observations.jsonl"

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def annotations(self) -> Path:
        return self.root / "annotations.jsonl"

    @property
    def judgments(self) -> Path:
        return self.root / "judgments.jsonl"

    @property
    def audit(self) -> Path:
        return self.root / "audit.jsonl"

    @property
    def recheck_dir(self) -> Path:
        return self.root / "recheck"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    def sealed_file(self, version: int) -> Path:
        return self.sealed_dir / f"v{version:04d}.json"


def paths(root: str | Path) -> ProjectPaths:
    path = Path(root)
    if not path.is_dir():
        raise InputError(f"项目目录不存在：{path}")
    return ProjectPaths(path)


def load_project(root: str | Path) -> dict[str, Any]:
    project_paths = paths(root)
    project: dict[str, Any] = read_json(project_paths.project_file)
    spec.require_valid("project", project, where=str(project_paths.project_file))
    return project


def preregistration_hash(project: dict[str, Any]) -> str:
    """预先登记内容的哈希。封存题库时写入 prompt_set，用于识别封存后的口径改动。"""
    return hash_obj(project["preregistration"])


def load_entities(root: str | Path) -> list[dict[str, Any]]:
    project_paths = paths(root)
    entities = read_jsonl(project_paths.entities)
    seen: set[str] = set()
    for index, entity in enumerate(entities, start=1):
        spec.require_valid("entity", entity, where=f"{project_paths.entities} 第 {index} 行")
        if entity["entity_id"] in seen:
            raise InputError(f"实体编号重复：{entity['entity_id']}")
        seen.add(entity["entity_id"])
    return entities


def prompt_content_hash(prompt_set: dict[str, Any]) -> str:
    """题库内容哈希：只覆盖题目本身和版本链，不含封存人和封存时间。"""
    return hash_obj({key: prompt_set[key] for key in PROMPT_HASH_FIELDS})


def load_sealed_prompt_sets(root: str | Path) -> list[dict[str, Any]]:
    """读取全部已封存题库并校验：契约、内容哈希、版本连续、版本链。

    任何一处对不上都说明封存后的文件被改动过，抛出 CheckFailed。
    """
    project_paths = paths(root)
    if not project_paths.sealed_dir.is_dir():
        return []
    sealed: list[dict[str, Any]] = []
    for path in sorted(project_paths.sealed_dir.glob("v*.json")):
        prompt_set: dict[str, Any] = read_json(path)
        spec.require_valid("prompt_set", prompt_set, where=str(path))
        expected_version = len(sealed) + 1
        if (
            prompt_set["version"] != expected_version
            or path.name != f"v{expected_version:04d}.json"
        ):
            raise CheckFailed(
                f"封存题库版本不连续：{path.name} 的版本号为 {prompt_set['version']}，期望 {expected_version}"
            )
        if prompt_content_hash(prompt_set) != prompt_set["content_hash"]:
            raise CheckFailed(
                f"封存题库被改动：{path.name} 的内容与封存时的哈希不一致",
                details={"file": path.name, "sealed_hash": prompt_set["content_hash"]},
            )
        previous = sealed[-1]["content_hash"] if sealed else None
        if prompt_set["previous_hash"] != previous:
            raise CheckFailed(f"封存题库版本链断裂：{path.name} 的 previous_hash 与上一版本不符")
        sealed.append(prompt_set)
    return sealed


def select_prompt_set(root: str | Path, version: int | None = None) -> dict[str, Any]:
    """取指定版本的封存题库，默认取最新版本。"""
    sealed = load_sealed_prompt_sets(root)
    if not sealed:
        raise InputError("题库尚未封存：请先执行 geo-verify prompts seal")
    if version is None:
        return sealed[-1]
    if not 1 <= version <= len(sealed):
        raise InputError(f"没有第 {version} 版封存题库（现有 1～{len(sealed)} 版）")
    return sealed[version - 1]


def load_observations(root: str | Path) -> list[dict[str, Any]]:
    """读取观测记录。这里不逐条做契约校验（写入时已校验；复核可用 validate 命令）。"""
    return read_jsonl(paths(root).observations, missing_ok=True)


def load_annotations(root: str | Path) -> list[dict[str, Any]]:
    return read_jsonl(paths(root).annotations, missing_ok=True)


def load_judgments(root: str | Path) -> list[dict[str, Any]]:
    return read_jsonl(paths(root).judgments, missing_ok=True)


def load_audit(root: str | Path) -> list[dict[str, Any]]:
    return read_jsonl(paths(root).audit, missing_ok=True)


def effective_annotations(
    annotations: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """从全部历史标注中解析出每个（观测，实体）当前生效的一条。

    规则（全部历史保留在文件里，这里只做选择）：
    1. 被其他标注 supersedes 指向的标注不再生效；
    2. 剩余标注中，人工优先于模型，模型优先于规则；
    3. 同级多条时取 created_at 最新的，再按 annotation_id 排序保证结果确定。
    """
    superseded = {item["supersedes"] for item in annotations if item.get("supersedes")}
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for item in annotations:
        if item["annotation_id"] in superseded:
            continue
        key = (item["observation_id"], item["entity_id"])
        current = chosen.get(key)
        if current is None or _annotation_rank(item) > _annotation_rank(current):
            chosen[key] = item
    return chosen


def _annotation_rank(annotation: dict[str, Any]) -> tuple[int, str, str]:
    return (
        ANNOTATOR_PRIORITY[annotation["annotator"]["type"]],
        annotation["created_at"],
        annotation["annotation_id"],
    )


def exclusions(audit_events: list[dict[str, Any]]) -> dict[str, str]:
    """从审计事件中取出被剔除的观测：{observation_id: rule_id}。

    剔除只能通过审计事件表达，原始观测文件不会被改动；同一观测多次被剔除时保留最早的规则。
    """
    excluded: dict[str, str] = {}
    for event in audit_events:
        if event.get("action") != "exclude":
            continue
        details = event.get("details", {})
        for observation_id in details.get("observation_ids", []):
            excluded.setdefault(observation_id, details["rule_id"])
    return excluded
