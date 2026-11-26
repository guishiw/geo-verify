"""双人标注一致性：Cohen's kappa 与争议项（out/disputes.json）。

规则：

- 两名标注者按 annotator.id 区分（任意类型，如规则标注 geo-verify-rules 也可与人工比较）；
  每人对每个（观测，实体）取各自最新的一条（created_at 最新，同一时间取编号较大者）。
- 只比较两人都标注过的（观测，实体）；没有共同项 → 数据不足。
- 每个字段的取值作为类别计算 Cohen's kappa：κ = (p_o − p_e) / (1 − p_e)，
  p_o 为一致比例，p_e = Σ_k p_A(k) · p_B(k)。recommended 的 null（无法判断）是单独的一个类别。
  用整数计算：κ = (n · 一致数 − Σ n_A(k) n_B(k)) / (n² − Σ n_A(k) n_B(k))，结果保留 6 位小数。
  p_e = 1（两人全部给出同一个类别）时 κ 无定义：kappa = null 并写明 null_reason。
- 两人取值不同的（观测，实体，字段）是争议项。若该（观测，实体）在两条分歧标注之后
  （annotations.jsonl 中位于两者之后的行）还有一条人工标注、且审计日志中有指向它的 override 事件，
  则该争议项已裁决（resolved = true）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geo_verify import project
from geo_verify.errors import InputError, InsufficientData
from geo_verify.jsonl import dumps_line, write_json

FIELDS = ("mentioned", "recommended")
DISPUTES_RELPATH = "out/disputes.json"
SCHEMA_VERSION = "1.0"
DIGITS = 6
UNDEFINED_REASON = "两名标注者在该字段上全部给出同一类别，期望一致率为 1，kappa 无定义"
EMPTY_REASON = "没有可比较的项"


@dataclass(frozen=True)
class KappaResult:
    compared: int
    agreements: int
    disagreements: int
    kappa: float | None
    null_reason: str | None


def cohen_kappa(pairs: list[tuple[Any, Any]]) -> KappaResult:
    """一组 (A 的取值, B 的取值) 的 Cohen's kappa。"""
    count = len(pairs)
    agreements = sum(1 for first, second in pairs if first == second)
    if count == 0:
        return KappaResult(0, 0, 0, None, EMPTY_REASON)
    first_counts = Counter(dumps_line({"v": first}) for first, _ in pairs)
    second_counts = Counter(dumps_line({"v": second}) for _, second in pairs)
    chance = sum(first_counts[key] * second_counts[key] for key in first_counts)
    denominator = count * count - chance
    if denominator == 0:
        return KappaResult(count, agreements, count - agreements, None, UNDEFINED_REASON)
    kappa = round((count * agreements - chance) / denominator, DIGITS)
    return KappaResult(count, agreements, count - agreements, kappa, None)


def _latest(
    annotations: list[dict[str, Any]], annotator_id: str
) -> dict[tuple[str, str], tuple[int, dict[str, Any]]]:
    latest: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    for index, item in enumerate(annotations):
        if item["annotator"]["id"] != annotator_id:
            continue
        key = (item["observation_id"], item["entity_id"])
        current = latest.get(key)
        if current is None or (item["created_at"], item["annotation_id"]) >= (
            current[1]["created_at"],
            current[1]["annotation_id"],
        ):
            latest[key] = (index, item)
    return latest


def compute(root: str | Path, first: str, second: str, fields: tuple[str, ...]) -> dict[str, Any]:
    """计算 kappa 与争议项，写入 out/disputes.json，返回文件内容。"""
    if first == second:
        raise InputError("--a 与 --b 必须是两名不同的标注者")
    proj = project.load_project(root)
    annotations = project.load_annotations(root)
    labels_a = _latest(annotations, first)
    labels_b = _latest(annotations, second)
    common = sorted(set(labels_a) & set(labels_b))
    if not common:
        raise InsufficientData(
            f"标注者 {first} 与 {second} 没有共同标注的（观测，实体），无法计算一致性",
            details={"annotator_a_items": len(labels_a), "annotator_b_items": len(labels_b)},
        )
    override_targets = {
        event["target"]["id"]
        for event in project.load_audit(root)
        if event.get("action") == "override" and event.get("target", {}).get("type") == "annotation"
    }
    adjudications: dict[tuple[str, str], list[int]] = {}
    for index, item in enumerate(annotations):
        if item["annotation_id"] in override_targets and item["annotator"]["type"] == "human":
            adjudications.setdefault((item["observation_id"], item["entity_id"]), []).append(index)

    summaries: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for name in fields:
        pairs = [(labels_a[key][1][name], labels_b[key][1][name]) for key in common]
        result = cohen_kappa(pairs)
        summaries.append(
            {
                "field": name,
                "compared": result.compared,
                "agreements": result.agreements,
                "disagreements": result.disagreements,
                "kappa": result.kappa,
                "null_reason": result.null_reason,
            }
        )
        for key, (value_a, value_b) in zip(common, pairs, strict=True):
            if value_a == value_b:
                continue
            last = max(labels_a[key][0], labels_b[key][0])
            resolved = any(index > last for index in adjudications.get(key, []))
            items.append(
                {
                    "observation_id": key[0],
                    "entity_id": key[1],
                    "field": name,
                    "a": value_a,
                    "b": value_b,
                    "resolved": resolved,
                }
            )
    items.sort(key=lambda item: (item["observation_id"], item["entity_id"], item["field"]))
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "disputes",
        "project_id": proj["project_id"],
        "annotator_a": first,
        "annotator_b": second,
        "fields": summaries,
        "items": items,
        "unresolved": sum(1 for item in items if not item["resolved"]),
    }
    if proj.get("synthetic") is True:
        document["synthetic"] = True
    write_json(project.paths(root).root / DISPUTES_RELPATH, document)
    return document
