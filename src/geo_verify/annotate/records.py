"""标注记录：选择要标注的实体、确定性编号、契约校验后追加写入。

annotations.jsonl 只追加：本模块只提供追加写入，不改动、不删除已有行。
"""

from __future__ import annotations

import re
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

_SCHEMA_VERSION_RE = re.compile(r"1\.[0-9]+")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")
_NON_BLANK_RE = re.compile(r"\S")
_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "annotation_id",
        "observation_id",
        "project_id",
        "entity_id",
        "mentioned",
        "recommended",
        "negated",
        "rankable",
        "rank",
        "sentiment",
        "risk",
        "annotator",
        "created_at",
        "supersedes",
    }
)
_FAST_ALLOWED_KEYS = _REQUIRED_KEYS | frozenset(
    {"rank_list_size", "spans", "note", "synthetic", "ext"}
)
_RISK_KEYS = frozenset({"flags", "unfounded_negative", "identified", "confirmed", "closed"})
_ACTOR_KEYS = frozenset({"type", "id", "version"})
_SPAN_KEYS = frozenset({"kind", "start", "end"})
_SENTIMENTS = frozenset({"positive", "neutral", "negative"})
_ACTOR_TYPES = frozenset({"rule", "model", "human"})
_SPAN_KINDS = frozenset({"mention", "recommendation", "negation", "rank"})


def _opt_bool(value: Any) -> bool:
    return value is None or type(value) is bool


def _opt_pos_int(value: Any) -> bool:
    return value is None or (type(value) is int and value >= 1)


def _is_id(value: Any) -> bool:
    return type(value) is str and _ID_RE.fullmatch(value) is not None


def _fast_valid_annotation(record: dict[str, Any]) -> bool:
    """常见 annotation 记录的快速等价校验；任何不满足或非常规字段都回退到完整 JSON Schema 校验。"""
    keys = record.keys()
    if not (_REQUIRED_KEYS <= keys <= _FAST_ALLOWED_KEYS):
        return False
    sv = record["schema_version"]
    if type(sv) is not str or _SCHEMA_VERSION_RE.fullmatch(sv) is None:
        return False
    if not (
        _is_id(record["annotation_id"])
        and _is_id(record["observation_id"])
        and _is_id(record["project_id"])
        and _is_id(record["entity_id"])
    ):
        return False
    mentioned = record["mentioned"]
    recommended = record["recommended"]
    negated = record["negated"]
    rankable = record["rankable"]
    rank = record["rank"]
    if not (
        type(mentioned) is bool
        and _opt_bool(recommended)
        and type(negated) is bool
        and type(rankable) is bool
        and _opt_pos_int(rank)
    ):
        return False
    if not mentioned and (recommended is True or rank is not None or negated):
        return False
    if not rankable and rank is not None:
        return False
    if negated and recommended is not False:
        return False
    if "rank_list_size" in record and not _opt_pos_int(record["rank_list_size"]):
        return False
    sentiment = record["sentiment"]
    if sentiment is not None and (type(sentiment) is not str or sentiment not in _SENTIMENTS):
        return False
    created_at = record["created_at"]
    if type(created_at) is not str or _UTC_RE.fullmatch(created_at) is None:
        return False
    supersedes = record["supersedes"]
    if supersedes is not None and not _is_id(supersedes):
        return False
    risk = record["risk"]
    if not (
        type(risk) is dict
        and risk.keys() == _RISK_KEYS
        and risk["flags"] == []
        and _opt_bool(risk["unfounded_negative"])
        and type(risk["identified"]) is bool
        and _opt_bool(risk["confirmed"])
        and _opt_bool(risk["closed"])
    ):
        return False
    annotator = record["annotator"]
    if not (
        type(annotator) is dict
        and annotator.keys() == _ACTOR_KEYS
        and type(annotator["type"]) is str
        and annotator["type"] in _ACTOR_TYPES
        and type(annotator["id"]) is str
        and _NON_BLANK_RE.search(annotator["id"]) is not None
        and type(annotator["version"]) is str
        and _NON_BLANK_RE.search(annotator["version"]) is not None
    ):
        return False
    if "spans" in record:
        spans = record["spans"]
        if type(spans) is not list:
            return False
        for item in spans:
            if not (
                type(item) is dict
                and item.keys() == _SPAN_KEYS
                and type(item["kind"]) is str
                and item["kind"] in _SPAN_KINDS
                and type(item["start"]) is int
                and item["start"] >= 0
                and type(item["end"]) is int
                and item["end"] >= 1
            ):
                return False
    if "note" in record and type(record["note"]) is not str:
        return False
    if "synthetic" in record and type(record["synthetic"]) is not bool:
        return False
    return not ("ext" in record and type(record["ext"]) is not dict)


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
        if not _fast_valid_annotation(record):
            spec.require_valid("annotation", record, where=f"标注 {record.get('annotation_id')}")
    return append_jsonl(path, records) if records else 0
