"""`annotate rules` 的项目级流程：读项目 → 逐条观测按规则判断 → 校验 → 追加写入 → 审计。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from geo_verify import audit, project
from geo_verify.annotate.records import (
    SCHEMA_VERSION,
    annotated_entity_ids,
    annotation_id,
    append_annotations,
)
from geo_verify.annotate.rules import RULE_ANNOTATOR, RULES_VERSION, Judgment, RuleAnnotator
from geo_verify.canon import sha256_file


@dataclass
class RulesReport:
    entity_ids: list[str]
    observations_total: int = 0
    observations_eligible: int = 0
    skipped_invalid: int = 0
    skipped_no_answer: int = 0
    skipped_existing: int = 0
    written: int = 0
    audit_event_id: str | None = None
    summary: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "annotator": dict(RULE_ANNOTATOR),
            "rule_version": RULES_VERSION,
            "entity_ids": self.entity_ids,
            "observations_total": self.observations_total,
            "observations_eligible": self.observations_eligible,
            "skipped_invalid": self.skipped_invalid,
            "skipped_no_answer": self.skipped_no_answer,
            "skipped_existing": self.skipped_existing,
            "written": self.written,
            "audit_event_id": self.audit_event_id,
            "summary": self.summary,
        }


def rule_annotation_id(project_id: str, observation_id: str, entity_id: str) -> str:
    return annotation_id(
        {
            "project_id": project_id,
            "observation_id": observation_id,
            "entity_id": entity_id,
            "annotator": RULE_ANNOTATOR,
        }
    )


def build_annotation(
    observation: dict[str, Any],
    judgment: Judgment,
    *,
    created_at: str,
    synthetic: bool,
) -> dict[str, Any]:
    """把一个规则判断写成 annotation 契约对象。"""
    ext: dict[str, Any] = {"rule_codes": judgment.codes}
    if judgment.ambiguous:
        ext["ambiguous_spans"] = [{"start": start, "end": end} for start, end in judgment.ambiguous]
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "annotation_id": rule_annotation_id(
            observation["project_id"], observation["observation_id"], judgment.entity_id
        ),
        "observation_id": observation["observation_id"],
        "project_id": observation["project_id"],
        "entity_id": judgment.entity_id,
        "mentioned": judgment.mentioned,
        "recommended": judgment.recommended,
        "negated": judgment.negated,
        "rankable": judgment.rankable,
        "rank": judgment.rank,
        "rank_list_size": judgment.rank_list_size,
        "sentiment": judgment.sentiment,
        "risk": {
            "flags": [],
            "unfounded_negative": judgment.unfounded_negative,
            "identified": False,
            "confirmed": None,
            "closed": None,
        },
        "spans": [
            {"kind": kind, "start": start, "end": end} for kind, start, end in judgment.spans
        ],
        "annotator": dict(RULE_ANNOTATOR),
        "created_at": created_at,
        "supersedes": None,
        "ext": ext,
    }
    if judgment.note:
        record["note"] = judgment.note
    if synthetic:
        record["synthetic"] = True
    return record


def _count(summary: dict[str, dict[str, int]], judgment: Judgment) -> None:
    counts = summary.setdefault(
        judgment.entity_id,
        {"annotated": 0, "mentioned": 0, "recommended": 0, "negated": 0, "ranked": 0},
    )
    counts["annotated"] += 1
    counts["mentioned"] += int(judgment.mentioned)
    counts["recommended"] += int(judgment.recommended is True)
    counts["negated"] += int(judgment.negated)
    counts["ranked"] += int(judgment.rank is not None)


def annotate_project(root: str | Path, *, now: str) -> RulesReport:
    paths = project.paths(root)
    proj = project.load_project(root)
    entities = project.load_entities(root)
    entity_ids = annotated_entity_ids(proj, entities)
    annotator = RuleAnnotator(entities, entity_ids)
    existing = project.load_annotations(root)
    effective = project.effective_annotations(existing)
    known_ids = {item["annotation_id"] for item in existing}
    project_synthetic = proj.get("synthetic") is True
    report = RulesReport(entity_ids=entity_ids)
    records: list[dict[str, Any]] = []
    for observation in project.load_observations(root):
        report.observations_total += 1
        if observation.get("status") != "valid":
            report.skipped_invalid += 1
            continue
        text = observation.get("answer_text")
        if not isinstance(text, str) or not text.strip():
            report.skipped_no_answer += 1
            continue
        report.observations_eligible += 1
        observation_id = observation["observation_id"]
        pending = [e for e in entity_ids if (observation_id, e) not in effective]
        report.skipped_existing += len(entity_ids) - len(pending)
        if not pending:
            continue
        judgments = annotator.judge(text)
        synthetic = project_synthetic or observation.get("synthetic") is True
        for entity_id in pending:
            record = build_annotation(
                observation, judgments[entity_id], created_at=now, synthetic=synthetic
            )
            if record["annotation_id"] in known_ids:
                report.skipped_existing += 1
                continue
            known_ids.add(record["annotation_id"])
            records.append(record)
            _count(report.summary, judgments[entity_id])
    before = sha256_file(paths.annotations) if paths.annotations.exists() else None
    report.written = append_annotations(paths.annotations, records)
    if report.written:
        event = audit.append_event(
            root,
            actor=f"geo-verify annotate rules（{RULE_ANNOTATOR['id']} {RULES_VERSION}）",
            action="annotate",
            target_type="annotations",
            target_id=f"{RULE_ANNOTATOR['id']}-{RULES_VERSION}",
            reason=f"按规则 {RULE_ANNOTATOR['id']} {RULES_VERSION} 生成提及、推荐、否定与位次标注",
            occurred_at=now,
            before_hash=before,
            after_hash=sha256_file(paths.annotations),
            details={
                "annotator": dict(RULE_ANNOTATOR),
                "written": report.written,
                "skipped_existing": report.skipped_existing,
                "observations_eligible": report.observations_eligible,
                "entity_ids": entity_ids,
            },
            synthetic=True if project_synthetic else None,
        )
        report.audit_event_id = event["event_id"]
    return report
