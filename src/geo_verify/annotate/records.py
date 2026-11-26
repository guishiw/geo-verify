"""标注记录：选择要标注的实体、确定性编号、契约校验后追加写入。

annotations.jsonl 只追加：本模块只提供追加写入，不改动、不删除已有行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from geo_verify import spec
from geo_verify.annotate.matching import EntityMatcher
from geo_verify.canon import hash_obj
from geo_verify.errors import InputError
from geo_verify.jsonl import append_jsonl

SCHEMA_VERSION = "1.0"
ID_PREFIX = "a-"
ID_HEX_LENGTH = 24


def annotation_id(identity: dict[str, Any]) -> str:
    """标注编号：`a-` + 身份信息规范化 JSON 的 SHA-256 前 24 位。同样的身份信息总得到同一个编号。"""
    return ID_PREFIX + hash_obj(identity)[:ID_HEX_LENGTH]


def annotated_entity_ids(project: dict[str, Any], entities: list[dict[str, Any]]) -> list[str]:
    """要标注的实体：目标主体、登记的竞品，以及 parent 链能追到它们的实体。

    顺序：目标主体在前、竞品按登记顺序；每个主体之后紧跟其下级实体（按 entities.jsonl 的顺序）。
    """
    known = {entity["entity_id"] for entity in entities}
    roots = [project["target_entity_id"], *project["competitor_entity_ids"]]
    missing = [entity_id for entity_id in roots if entity_id not in known]
    if missing:
        raise InputError(
            f"entities.jsonl 中没有这些实体：{'、'.join(missing)}（project.json 的目标主体或竞品）",
            details={"missing_entity_ids": missing},
        )
    matcher = EntityMatcher(entities)
    selected: list[str] = []
    for root in roots:
        if root not in selected:
            selected.append(root)
        for entity in entities:
            entity_id = entity["entity_id"]
            if entity_id not in selected and root in matcher.ancestors(entity_id):
                selected.append(entity_id)
    return selected


def append_annotations(path: Path, records: list[dict[str, Any]]) -> int:
    """逐条按 annotation 契约校验，全部合法后才追加写入；返回写入条数。"""
    for record in records:
        spec.require_valid("annotation", record, where=f"标注 {record.get('annotation_id')}")
    return append_jsonl(path, records) if records else 0
