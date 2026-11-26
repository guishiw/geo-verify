"""抽样复核清单：固定 seed 抽题，列出被抽中题目的全部有效观测 × 已有生效标注的实体。

抽样规则（第三方可据此复算）：
1. 取所选封存题库的 query_id，去重后排序；题数记为 N。
2. 抽取数量 k = ⌈fraction × N⌉。fraction 按其十进制写法精确计算（0.1 × 30 = 3，不会因浮点误差多抽一题）。
3. 对每道题计算 sha256("<seed>|<query_id>")（UTF-8），按十六进制摘要升序取前 k 道，结果按 query_id 排序。
   结果只取决于题库和 seed，与观测文件的行序无关。

“有效观测”：status = valid、prompt_set_hash 等于所选封存题库的 content_hash、题目在该题库中、
且未被审计事件剔除。复核项 =（观测，实体），只列出已有生效标注的实体；
out/disputes.json 中未解决的争议项并入清单（source = dispute）。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from geo_verify import project as project_module
from geo_verify.canon import sha256_bytes
from geo_verify.errors import InputError
from geo_verify.jsonl import read_json, write_json
from geo_verify.report import csvsafe

SCHEMA_VERSION = "1.0"
FIELDS: tuple[str, ...] = ("mentioned", "recommended", "rank")
SOURCES: tuple[str, ...] = ("sample", "dispute")
PLAN_PATH = "recheck/plan.json"
PLAN_CSV_PATH = "recheck/plan.csv"
# 复核表里原值为 null 时的写法（recheck-compare 认可 null、none、n/a、无）
NULL_CELL = "null"
RESULT_PATH = "recheck/result.json"
DISPUTES_PATH = "out/disputes.json"
SAMPLING_METHOD = "sha256_rank"
SAMPLING_RULE = (
    '对所选封存题库去重排序后的 query_id，逐题计算 sha256("<seed>|<query_id>")（UTF-8），'
    "按十六进制摘要升序取前 k 道，k = ⌈fraction × 题数⌉（fraction 按十进制写法精确计算）；"
    "结果按 query_id 排序，与观测文件的行序无关"
)
PLAN_CSV_HEADER: tuple[str, ...] = (
    "observation_id",
    "entity_id",
    "entity_name",
    "query_id",
    "platform",
    "source",
    "prompt_text",
    "answer_text",
    "original_mentioned",
    "original_recommended",
    "original_rank",
    "recheck_mentioned",
    "recheck_recommended",
    "recheck_rank",
    "recheck_note",
)


# ------------------------------------------------------------------ 抽样


def _exact_fraction(fraction: object) -> Fraction:
    if isinstance(fraction, bool) or not isinstance(fraction, int | float):
        raise InputError(f"抽样比例必须是 (0, 1] 之间的数：{fraction!r}")
    if not math.isfinite(fraction) or not 0 < fraction <= 1:
        raise InputError(f"抽样比例必须在 (0, 1] 之间：{fraction!r}")
    # 浮点数取其最短十进制写法（即登记或命令行里写的那个数），避免 0.1 × 30 = 3.0000000000000004
    exact = Fraction(repr(fraction)) if isinstance(fraction, float) else Fraction(fraction)
    return min(exact, Fraction(1))


def sample_size(total: int, fraction: float) -> int:
    """抽取的题数：⌈fraction × total⌉；fraction ∈ (0, 1]，total ≥ 1。"""
    exact = _exact_fraction(fraction)
    if isinstance(total, bool) or not isinstance(total, int) or total < 1:
        raise InputError("封存题库没有题目，无法抽样")
    return -((-exact.numerator * total) // exact.denominator)


def check_seed(seed: object) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise InputError(f"seed 必须是非负整数：{seed!r}")
    return seed


def sample_query_ids(query_ids: Iterable[str], fraction: float, seed: int) -> list[str]:
    """按固定 seed 抽题，返回排序后的 query_id 列表。"""
    check_seed(seed)
    unique = sorted(set(query_ids))
    size = sample_size(len(unique), fraction)
    ranked = sorted(
        unique, key=lambda qid: (hashlib.sha256(f"{seed}|{qid}".encode()).hexdigest(), qid)
    )
    return sorted(ranked[:size])


def _same(left: object, right: object) -> bool:
    """严格相等：false 与 null、true 与 1 都算不同。"""
    return left is right or (type(left) is type(right) and left == right)


def diff_fields(
    original: Mapping[str, Any], rechecked: Mapping[str, Any], fields: Iterable[str]
) -> list[dict[str, Any]]:
    """逐字段比对，按 FIELDS 的固定顺序返回不一致的字段。"""
    wanted = set(fields)
    return [
        {"field": name, "original": original[name], "rechecked": rechecked[name]}
        for name in FIELDS
        if name in wanted and not _same(original[name], rechecked[name])
    ]


def notice(code: str, severity: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, **details}


# ------------------------------------------------------------------ 项目数据


@dataclass
class ProjectData:
    """复核需要的项目数据：观测（同编号只取第一条）、实体、全部标注历史与生效标注。"""

    root: Path
    project: dict[str, Any]
    entities: dict[str, dict[str, Any]]
    observations: dict[str, dict[str, Any]]
    annotations_by_id: dict[str, dict[str, Any]]
    effective: dict[tuple[str, str], dict[str, Any]]
    excluded: dict[str, str]
    effective_by_observation: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for (observation_id, entity_id), annotation in self.effective.items():
            self.effective_by_observation.setdefault(observation_id, {})[entity_id] = annotation


def load_project_data(root: Path) -> ProjectData:
    project = project_module.load_project(root)
    entities = {item["entity_id"]: item for item in project_module.load_entities(root)}
    observations: dict[str, dict[str, Any]] = {}
    for item in project_module.load_observations(root):
        observations.setdefault(item["observation_id"], item)
    annotations = project_module.load_annotations(root)
    by_id: dict[str, dict[str, Any]] = {}
    for item in annotations:
        by_id.setdefault(item["annotation_id"], item)
    return ProjectData(
        root=root,
        project=project,
        entities=entities,
        observations=observations,
        annotations_by_id=by_id,
        effective=project_module.effective_annotations(annotations),
        excluded=project_module.exclusions(project_module.load_audit(root)),
    )


def usable_check(
    data: ProjectData, prompt_set: Mapping[str, Any]
) -> Callable[[Mapping[str, Any]], bool]:
    """返回判断“有效观测”的函数：有效、属于所选题库版本且题目在题库中、未被剔除。"""
    content_hash = prompt_set["content_hash"]
    bank = {item["query_id"] for item in prompt_set["prompts"]}

    def usable(observation: Mapping[str, Any]) -> bool:
        return (
            observation.get("status") == "valid"
            and observation.get("prompt_set_hash") == content_hash
            and observation.get("query_id") in bank
            and observation["observation_id"] not in data.excluded
        )

    return usable


# ------------------------------------------------------------------ 争议项


def load_disputes(root: Path) -> list[tuple[str, str, bool]] | None:
    """读取 out/disputes.json，返回 [(观测，实体，是否已裁决)]；文件不存在时返回 None。"""
    path = root / DISPUTES_PATH
    if not path.exists():
        return None
    data = read_json(path)
    if not isinstance(data, dict) or data.get("kind") != "disputes":
        raise InputError(f"{DISPUTES_PATH} 结构不对：应为 kind = disputes 的 JSON 对象")
    items = data.get("items")
    if not isinstance(items, list):
        raise InputError(f"{DISPUTES_PATH} 结构不对：缺少 items 列表")
    disputes: list[tuple[str, str, bool]] = []
    for position, item in enumerate(items, start=1):
        where = f"{DISPUTES_PATH} 的第 {position} 个争议项"
        if not isinstance(item, dict):
            raise InputError(f"{where}不是 JSON 对象")
        observation_id, entity_id = item.get("observation_id"), item.get("entity_id")
        if not isinstance(observation_id, str) or not isinstance(entity_id, str):
            raise InputError(f"{where}缺少字符串类型的 observation_id 或 entity_id")
        resolved = item.get("resolved", False)
        if not isinstance(resolved, bool):
            raise InputError(f"{where}的 resolved 必须为 true 或 false")
        disputes.append((observation_id, entity_id, resolved))
    return disputes


# ------------------------------------------------------------------ 复核清单


@dataclass
class Plan:
    document: dict[str, Any]
    csv_rows: list[list[Any]]
    registered: dict[str, Any]

    @property
    def items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.document["items"]
        return items


def _item(
    observation: Mapping[str, Any], entity_id: str, annotation: Mapping[str, Any], source: str
) -> dict[str, Any]:
    return {
        "observation_id": observation["observation_id"],
        "query_id": observation["query_id"],
        "entity_id": entity_id,
        "platform": observation.get("platform"),
        "source": source,
        "disputed": source == "dispute",
        "annotation_id": annotation["annotation_id"],
    }


def _item_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (item["query_id"], item["observation_id"], item["entity_id"])


def build_plan(
    root: Path,
    *,
    fraction: float | None = None,
    seed: int | None = None,
    prompt_set_version: int | None = None,
) -> Plan:
    """生成复核清单（不写文件）。fraction、seed 默认取预先登记值。"""
    data = load_project_data(root)
    project = data.project
    prompt_set = project_module.select_prompt_set(root, prompt_set_version)
    registered = project["preregistration"]["recheck"]
    if fraction is not None:
        _exact_fraction(fraction)
    if seed is not None:
        check_seed(seed)
    used_fraction = (
        registered["fraction"]
        if fraction is None or fraction == registered["fraction"]
        else fraction
    )
    used_seed = registered["seed"] if seed is None else seed
    query_ids = [item["query_id"] for item in prompt_set["prompts"]]
    sampled = sample_query_ids(query_ids, used_fraction, used_seed)
    sampled_set = set(sampled)

    notices: list[dict[str, Any]] = []
    prereg_changed = (
        project_module.preregistration_hash(project) != prompt_set["preregistration_hash"]
    )
    overridden = (used_fraction, used_seed) != (registered["fraction"], registered["seed"])
    if prereg_changed:
        notices.append(
            notice(
                "preregistration_changed",
                "warning",
                "预先登记内容在题库封存后被改动：本清单使用的抽样参数不能视为预先登记的口径。",
            )
        )
    if overridden:
        notices.append(
            notice(
                "not_preregistered",
                "warning",
                f"抽样参数被命令行覆盖（登记：比例 {registered['fraction']:g}、seed {registered['seed']}；"
                f"本次：比例 {used_fraction:g}、seed {used_seed}），本清单不是预先登记的抽样。",
                registered={"fraction": registered["fraction"], "seed": registered["seed"]},
                used={"fraction": used_fraction, "seed": used_seed},
            )
        )
    other_version = sum(
        1
        for item in data.observations.values()
        if item.get("prompt_set_hash") != prompt_set["content_hash"]
    )
    if other_version:
        notices.append(
            notice(
                "prompt_set_version_mismatch",
                "info",
                f"有 {other_version} 条观测不属于第 {prompt_set['version']} 版封存题库，不进入复核清单。",
                count=other_version,
            )
        )

    usable = usable_check(data, prompt_set)
    sample_items: list[dict[str, Any]] = []
    unannotated = 0
    for observation_id in sorted(data.observations):
        observation = data.observations[observation_id]
        if observation.get("query_id") not in sampled_set:
            continue
        if not usable(observation):
            continue
        annotated = data.effective_by_observation.get(observation_id, {})
        if not annotated:
            unannotated += 1
            continue
        for entity_id in sorted(annotated):
            sample_items.append(_item(observation, entity_id, annotated[entity_id], "sample"))
    sample_items.sort(key=_item_key)
    if unannotated:
        notices.append(
            notice(
                "unannotated_observations",
                "warning",
                f"被抽中的题目里有 {unannotated} 条有效观测还没有生效标注，未列入清单；请先完成标注。",
                count=unannotated,
            )
        )
    covered = {item["query_id"] for item in sample_items}
    missing = [qid for qid in sampled if qid not in covered]
    if missing:
        notices.append(
            notice(
                "sampled_queries_without_items",
                "warning",
                f"有 {len(missing)} 道被抽中的题目没有可复核的有效观测：{'、'.join(missing)}。",
                query_ids=missing,
            )
        )

    dispute_items, dispute_summary = _merge_disputes(data, usable, sample_items)
    if dispute_summary["skipped"]:
        notices.append(
            notice(
                "disputes_skipped",
                "warning",
                f"有 {len(dispute_summary['skipped'])} 个未解决的争议项无法列入清单"
                "（观测不存在、不是有效观测或没有生效标注），已在 disputes.skipped 中逐条列出。",
                count=len(dispute_summary["skipped"]),
            )
        )
    items = [*sample_items, *dispute_items]
    if not items:
        notices.append(
            notice(
                "empty_plan",
                "error",
                "复核清单为空：被抽中的题目没有带生效标注的有效观测，也没有可并入的争议项。",
            )
        )

    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "recheck_plan",
        "project_id": project["project_id"],
        "prompt_set": {
            "version": prompt_set["version"],
            "content_hash": prompt_set["content_hash"],
        },
        "fraction": used_fraction,
        "seed": used_seed,
        "preregistered": not overridden and not prereg_changed,
        "sampling": {"method": SAMPLING_METHOD, "rule": SAMPLING_RULE},
        "total_queries": len(set(query_ids)),
        "query_ids": sampled,
        "items": items,
        "counts": {
            "sampled_queries": len(sampled),
            "observations": len({item["observation_id"] for item in sample_items}),
            "sample_items": len(sample_items),
            "dispute_items": len(dispute_items),
            "items": len(items),
            "unannotated_observations": unannotated,
        },
        "disputes": dispute_summary,
        "notices": notices,
    }
    if project.get("synthetic") is True:
        document["synthetic"] = True
    return Plan(document, [_csv_row(data, item) for item in items], dict(registered))


def _merge_disputes(
    data: ProjectData,
    usable: Callable[[Mapping[str, Any]], bool],
    sample_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    disputes = load_disputes(data.root)
    summary: dict[str, Any] = {
        "present": disputes is not None,
        "unresolved": 0,
        "merged": 0,
        "already_in_sample": 0,
        "skipped": [],
    }
    if not disputes:
        return [], summary
    unresolved = sorted({(oid, eid) for oid, eid, resolved in disputes if not resolved})
    summary["unresolved"] = len(unresolved)
    in_sample = {(item["observation_id"], item["entity_id"]): item for item in sample_items}
    merged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for observation_id, entity_id in unresolved:
        existing = in_sample.get((observation_id, entity_id))
        if existing is not None:
            existing["disputed"] = True
            summary["already_in_sample"] += 1
            continue
        observation = data.observations.get(observation_id)
        if observation is None:
            reason = "unknown_observation"
        elif not usable(observation):
            reason = "not_valid_observation"
        elif (observation_id, entity_id) not in data.effective:
            reason = "no_effective_annotation"
        else:
            annotation = data.effective[(observation_id, entity_id)]
            merged.append(_item(observation, entity_id, annotation, "dispute"))
            continue
        skipped.append({"observation_id": observation_id, "entity_id": entity_id, "reason": reason})
    merged.sort(key=_item_key)
    summary["merged"] = len(merged)
    summary["skipped"] = skipped
    return merged, summary


def _csv_row(data: ProjectData, item: Mapping[str, Any]) -> list[Any]:
    observation = data.observations[item["observation_id"]]
    annotation = data.annotations_by_id[item["annotation_id"]]
    entity = data.entities.get(item["entity_id"], {})
    return [
        item["observation_id"],
        item["entity_id"],
        entity.get("name"),
        item["query_id"],
        item["platform"],
        item["source"],
        observation.get("prompt_text"),
        observation.get("answer_text"),
        annotation.get("mentioned"),
        _spelled(annotation.get("recommended")),
        _spelled(annotation.get("rank")),
        None,
        None,
        None,
        None,
    ]


def _spelled(value: Any) -> Any:
    """原标注里的 null（推荐未判断、无位次）写成 `null`，不留空单元格。

    复核列留空表示“未复核”；原值列若也留空，复核人照抄原值时就分不清“无位次”和“没填”，
    recheck-compare 会把整行判为不完整。`null` 是 recheck-compare 认可的写法之一。
    """
    return NULL_CELL if value is None else value


@dataclass(frozen=True)
class WrittenPlan:
    plan: Plan
    plan_hash: str
    previous_plan_hash: str | None

    @property
    def changed(self) -> bool:
        return self.plan_hash != self.previous_plan_hash


def write_plan(root: Path, plan: Plan) -> WrittenPlan:
    """写出 recheck/plan.json 与 recheck/plan.csv，返回新旧计划文件的哈希。"""
    target = root / PLAN_PATH
    previous = sha256_bytes(target.read_bytes()) if target.is_file() else None
    write_json(target, plan.document)
    csvsafe.write_csv(root / PLAN_CSV_PATH, PLAN_CSV_HEADER, plan.csv_rows)
    return WrittenPlan(plan, sha256_bytes(target.read_bytes()), previous)
