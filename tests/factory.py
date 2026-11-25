"""测试用的合成项目构造器。

直接按契约写文件，不调用被测的封存、采集、标注实现，
这样指标、统计等模块的测试输入不依赖其他被测模块的行为。
全部数据为虚构，带 synthetic: true。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from conftest import valid_example

from geo_verify.canon import hash_obj
from geo_verify.jsonl import append_jsonl, write_json

PROMPT_HASH_FIELDS = ("prompt_set_id", "project_id", "version", "previous_hash", "prompts")
TARGET = "brand-lanxu"
COMPETITOR = "brand-cenye"
INTENTS = [
    "brand",
    "product",
    "category",
    "scenario",
    "problem",
    "comparison",
    "decision",
    "after_sales",
    "risk",
    "competitor",
    "general",
]


def make_project(root: Path, **overrides: Any) -> dict[str, Any]:
    """写出 project.json。overrides 可覆盖顶层字段；prereg=dict 可局部覆盖预先登记内容。"""
    project = valid_example("project")
    prereg = overrides.pop("prereg", None)
    project.update(overrides)
    if prereg:
        project["preregistration"].update(prereg)
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "project.json", project)
    return project


def make_entities(root: Path, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    target = valid_example("entity", 0)
    competitor = copy.deepcopy(target)
    competitor.update(
        entity_id=COMPETITOR,
        role="competitor",
        name="岑野",
        aliases=["岑野汽车", "CENYE"],
        exclusions=[],
        owned_domains=["cenye-auto.example"],
    )
    entities = [target, competitor, *(extra or [])]
    path = root / "entities.jsonl"
    if path.exists():
        path.unlink()
    append_jsonl(path, entities)
    return entities


def make_prompt(query_id: str, **fields: Any) -> dict[str, Any]:
    index = int("".join(ch for ch in query_id if ch.isdigit()) or 0)
    prompt = {
        "query_id": query_id,
        "text": f"第 {index} 题：20 万左右的纯电 SUV 怎么选？（虚构题目 {query_id}）",
        "intent_type": INTENTS[index % len(INTENTS)],
        "business_level": None,
        "priority": "P1",
        "source": "测试构造（虚构）",
        "expects_recommendation": True,
    }
    prompt.update(fields)
    return prompt


def seal_prompts(
    root: Path,
    project: dict[str, Any],
    prompts: list[dict[str, Any]],
    *,
    previous: dict[str, Any] | None = None,
    sealed_at: str = "2026-10-02T03:00:00Z",
) -> dict[str, Any]:
    """按契约直接写出一版封存题库（不经过被测的 seal 实现）。"""
    version = previous["version"] + 1 if previous else 1
    prompt_set: dict[str, Any] = {
        "schema_version": "1.0",
        "prompt_set_id": f"{project['project_id']}-prompts",
        "project_id": project["project_id"],
        "version": version,
        "previous_hash": previous["content_hash"] if previous else None,
        "prompts": prompts,
        "preregistration_hash": hash_obj(project["preregistration"]),
        "sealed_by": "甲方项目负责人（虚构）",
        "sealed_at": sealed_at,
        "synthetic": True,
    }
    prompt_set["content_hash"] = hash_obj({key: prompt_set[key] for key in PROMPT_HASH_FIELDS})
    write_json(root / "prompts" / "sealed" / f"v{version:04d}.json", prompt_set)
    return prompt_set


def make_observation(
    project: dict[str, Any],
    prompt_set: dict[str, Any],
    number: int,
    *,
    query_id: str = "Q001",
    platform: str = "engine-a",
    window: str = "W1",
    attempt: int = 1,
    channel: str = "api",
    answer: str | None = "可以考虑澜序 S7。",
    status: str = "valid",
    invalid_reason: str | None = None,
    started_at: str = "2026-10-08T02:00:00Z",
    citations: list[dict[str, Any]] | None = None,
    model_version: str | None = "engine-a-2026.10",
    **fields: Any,
) -> dict[str, Any]:
    prompt_text = next(
        (item["text"] for item in prompt_set["prompts"] if item["query_id"] == query_id),
        f"题库外的问题 {query_id}",
    )
    api = channel == "api"
    observation: dict[str, Any] = {
        "schema_version": "1.0",
        "observation_id": f"obs-{number:06d}",
        "project_id": project["project_id"],
        "trace_id": f"trace-{number:06d}",
        "prompt_set_hash": prompt_set["content_hash"],
        "query_id": query_id,
        "prompt_text": prompt_text,
        "platform": platform,
        "entry_form": "api" if api else "web",
        "model_version": model_version,
        "entry_features": None if model_version else "网页端默认模式，未展示模型名",
        "channel": channel,
        "started_at": started_at,
        "ended_at": started_at,
        "logged_in": False,
        "history_cleared": True,
        "web_search": True,
        "region": "CN",
        "device": None,
        "answer_text": answer if status == "valid" else None,
        "citations": citations or [],
        "snapshots": [],
        "collector": {"tool": "test-factory", "version": "0"},
        "operator": "测试构造（虚构）",
        "window_id": window,
        "attempt": attempt,
        "status": status,
        "invalid_reason": invalid_reason if status == "invalid" else None,
        "null_reasons": {"device": "测试构造未记录设备", "snapshots": "测试构造不生成快照"},
        "synthetic": True,
    }
    if status == "invalid":
        observation["invalid_reason"] = invalid_reason or "timeout"
        observation["null_reasons"]["answer_text"] = "无效观测没有回答"
    if model_version is None:
        observation["null_reasons"]["model_version"] = "页面未展示模型版本"
    observation.update(fields)
    return observation


def make_citation(order: int, domain: str, *, placement: str = "inline") -> dict[str, Any]:
    return {
        "order": order,
        "url": f"https://{domain}/page-{order}",
        "domain": domain,
        "title": None,
        "placement": placement,
    }


def make_annotation(
    observation: dict[str, Any],
    number: int,
    *,
    entity_id: str = TARGET,
    mentioned: bool = True,
    recommended: bool | None = None,
    negated: bool = False,
    rankable: bool = False,
    rank: int | None = None,
    sentiment: str | None = "neutral",
    annotator_type: str = "rule",
    annotator_id: str = "geo-verify-rules",
    created_at: str = "2026-10-08T03:00:00Z",
    supersedes: str | None = None,
    risk: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    annotation: dict[str, Any] = {
        "schema_version": "1.0",
        "annotation_id": f"ann-{number:06d}",
        "observation_id": observation["observation_id"],
        "project_id": observation["project_id"],
        "entity_id": entity_id,
        "mentioned": mentioned,
        "recommended": recommended if recommended is not None else (mentioned and not negated),
        "negated": negated,
        "rankable": rankable,
        "rank": rank,
        "sentiment": sentiment,
        "risk": {
            "flags": [],
            "unfounded_negative": False,
            "identified": False,
            "confirmed": None,
            "closed": None,
            **(risk or {}),
        },
        "annotator": {"type": annotator_type, "id": annotator_id, "version": "1.0"},
        "created_at": created_at,
        "supersedes": supersedes,
        "synthetic": True,
    }
    annotation.update(fields)
    return annotation


def make_judgment(
    observation: dict[str, Any],
    number: int,
    *,
    direction: str = "answer_to_facts",
    label: str = "supported",
    fact_id: str | None = "F-S7-RANGE-CLTC",
) -> dict[str, Any]:
    forward = direction == "answer_to_facts"
    needs_fact = label != "unverifiable"
    return {
        "schema_version": "1.0",
        "judgment_id": f"jdg-{number:06d}",
        "project_id": observation["project_id"],
        "direction": direction,
        "source": {"type": "observation", "id": observation["observation_id"]},
        "query_id": observation["query_id"],
        "claim_id": f"clm-{number:06d}" if forward else None,
        "fact_id": fact_id if needs_fact else None,
        "fact_version": 1 if needs_fact else None,
        "label": label,
        "reason": "测试构造的判定（虚构）",
        "as_of": "2026-10-08",
        "judge": {"type": "rule", "id": "test-factory", "version": "0"},
        "created_at": "2026-10-08T05:00:00Z",
        "synthetic": True,
    }


def write_records(
    root: Path,
    *,
    observations: list[dict[str, Any]] | None = None,
    annotations: list[dict[str, Any]] | None = None,
    judgments: list[dict[str, Any]] | None = None,
) -> None:
    for name, records in (
        ("observations.jsonl", observations),
        ("annotations.jsonl", annotations),
        ("judgments.jsonl", judgments),
    ):
        if records:
            append_jsonl(root / name, records)


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
