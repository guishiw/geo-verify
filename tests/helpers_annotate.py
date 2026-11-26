"""标注测试的辅助函数：虚构实体表上的规则判断、全角变换、拼装标注项目。"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

from factory import make_observation, write_records
from helpers_basic import auto_entities, sealed_project

from geo_verify.annotate.rules import Judgment, RuleAnnotator

FULLWIDTH_OFFSET = 0xFEE0
NOW = "2026-10-08T03:00:00Z"


def fullwidth(text: str) -> str:
    """把 ASCII 可见字符换成全角形式（不动空格和换行）。"""
    return "".join(chr(ord(ch) + FULLWIDTH_OFFSET) if "!" <= ch <= "~" else ch for ch in text)


@cache
def auto_annotator() -> RuleAnnotator:
    entities = auto_entities()
    return RuleAnnotator(entities, [entity["entity_id"] for entity in entities])


def judge(text: str) -> dict[str, Judgment]:
    return auto_annotator().judge(text)


def verdict(text: str, entity_id: str) -> tuple[bool, bool | None, bool]:
    """(mentioned, recommended, negated)。"""
    result = judge(text)[entity_id]
    return result.mentioned, result.recommended, result.negated


def ranking(text: str, entity_id: str) -> tuple[bool, int | None, int | None]:
    """(rankable, rank, rank_list_size)。"""
    result = judge(text)[entity_id]
    return result.rankable, result.rank, result.rank_list_size


def span_text(text: str, result: Judgment, kind: str) -> list[str]:
    return [text[start:end] for span_kind, start, end in result.spans if span_kind == kind]


def annotation_project(
    root: Path, answers: list[str | None], **project_overrides: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """项目 + 实体 + 封存题库 + 观测；answers 中的 None 表示一条超时的无效观测。"""
    proj, sealed = sealed_project(root, **project_overrides)
    observations = [
        make_observation(proj, sealed, number, answer=answer)
        if answer is not None
        else make_observation(proj, sealed, number, status="invalid", invalid_reason="timeout")
        for number, answer in enumerate(answers, start=1)
    ]
    write_records(root, observations=observations)
    return proj, observations
