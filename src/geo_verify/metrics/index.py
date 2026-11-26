"""项目索引：对原始记录各遍历一次，建成按列存放的紧凑索引。

所有指标、交叉验证、基线对比和告警都从这份索引取数，保证口径一致：

- “有效观测”= status 为 valid、属于所选封存题库版本、题目在题库内、且未被审计事件剔除；
- 每条观测只落入一个状态（见 rules），非有效观测按状态如实计数，不静默丢弃；
- 观测按 observation_id 升序存放，引用、判定等子表按各自编号升序存放，
  因此任何范围内取出的编号列表天然有序，可直接计算下钻哈希。

为了能处理 10 万条以上的观测，这里不对每条记录做 JSON Schema 校验
（写入时已校验；复核可用 `geo-verify validate`），只做指标需要的字段检查。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from geo_verify import project as project_module
from geo_verify import spec
from geo_verify.canon import sha256_file
from geo_verify.errors import InputError
from geo_verify.jsonl import iter_jsonl, read_json
from geo_verify.timeutil import local_date

BoolArray = npt.NDArray[np.bool_]
IntArray = npt.NDArray[np.int64]
ObjArray = npt.NDArray[np.object_]

RULE_VALID = "valid"
RULE_VERSION_MISMATCH = "prompt_set_version_mismatch"
RULE_DUPLICATE = "duplicate_observation_id"
RULE_UNKNOWN_QUERY = "unknown_query_id"
RULE_UNANNOTATED = "unannotated"
AUDIT_PREFIX = "audit:"
UNKNOWN_QUERY_LABEL = "(题库外)"

SENTIMENTS = {"positive": 1, "neutral": 2, "negative": 3}
LABELS = {
    "supported": 0,
    "contradicted": 1,
    "unverifiable": 2,
    "present_correct": 3,
    "present_wrong": 4,
    "missing": 5,
}
DIRECTIONS = {"answer_to_facts": 0, "facts_to_answer": 1}
SNAPSHOT_OK, SNAPSHOT_NONE, SNAPSHOT_MISSING, SNAPSHOT_MISMATCH = 0, 1, 2, 3
CHECK_UNVERIFIED, CHECK_VALID_RELEVANT, CHECK_NOT_RELEVANT, CHECK_INVALID = 0, 1, 2, 3

_STRUCTURAL_FIELDS = (
    "observation_id",
    "prompt_set_hash",
    "query_id",
    "platform",
    "channel",
    "window_id",
    "started_at",
    "status",
)
_NULLABLE_FIELDS = (
    "model_version",
    "logged_in",
    "history_cleared",
    "web_search",
    "region",
    "device",
    "answer_text",
    "operator",
)
_ENVIRONMENT_FLAGS = ("logged_in", "history_cleared", "web_search", "device")


@dataclass(frozen=True)
class Table:
    """一张以编号升序排列的表；obs 给出每行所属观测在观测表中的位置。"""

    unit: str
    ids: ObjArray
    obs: IntArray

    def __len__(self) -> int:
        return int(self.ids.size)


@dataclass(frozen=True)
class EntityColumns:
    """某个实体的生效标注，按观测表的行对齐。"""

    present: BoolArray
    mentioned: BoolArray
    recommended: BoolArray
    rankable: BoolArray
    rank: IntArray
    sentiment: IntArray
    negative_miscitation: BoolArray
    negative_unassessed: BoolArray
    risk_identified: BoolArray
    risk_confirmed: IntArray
    risk_closed: BoolArray


@dataclass(frozen=True)
class CitationTable:
    table: Table
    domain: IntArray
    domains: list[str]
    is_target: BoolArray
    is_authority: BoolArray
    tiered: BoolArray
    check: IntArray


@dataclass(frozen=True)
class JudgmentTable:
    table: Table
    direction: IntArray
    label: IntArray
    fact: IntArray
    facts: list[str]


@dataclass(frozen=True)
class RecheckTable:
    """抽样复核项。checked 为 False 的是“计划内但尚未复核”的项，不进通过率的分母。"""

    table: Table
    agree: BoolArray
    checked: BoolArray
    unmatched: int


@dataclass
class ProjectIndex:
    project: dict[str, Any]
    prompt_set: dict[str, Any]
    sealed: list[dict[str, Any]]
    entities: dict[str, dict[str, Any]]
    target_id: str
    queries: list[str]
    expects: BoolArray
    observations: Table
    query: IntArray
    platform: IntArray
    platforms: list[str]
    channel: IntArray
    channels: list[str]
    window: IntArray
    windows: list[str]
    date: IntArray
    dates: list[str]
    started: ObjArray
    state: IntArray
    rules: list[str]
    status_valid: BoolArray
    complete: BoolArray
    documented_nulls: BoolArray
    env_ok: BoolArray
    env_unknown: BoolArray
    snapshot: IntArray
    unidentified_citations: IntArray
    duplicate_citations: IntArray
    annotations: dict[str, EntityColumns]
    citations: CitationTable
    judgments: JudgmentTable
    recheck: RecheckTable | None
    target_sources: list[str]
    model_versions: dict[tuple[str, str], dict[str, list[Any]]]
    version_counts: Counter[str]
    excluded_by_audit: dict[str, str]
    counters: Counter[str] = field(default_factory=Counter)
    _mask_cache: dict[tuple[str | None, ...], BoolArray] = field(default_factory=dict)

    # ------------------------------------------------------------ 派生量

    @property
    def size(self) -> int:
        return len(self.observations)

    @property
    def valid(self) -> BoolArray:
        result: BoolArray = self.state == 0
        return result

    @property
    def universe(self) -> BoolArray:
        """“全部观测”：属于所选题库版本的观测（重复编号只算第一条）。"""
        outside = [
            code
            for code, rule in enumerate(self.rules)
            if rule in (RULE_VERSION_MISMATCH, RULE_DUPLICATE)
        ]
        result: BoolArray = ~np.isin(self.state, outside)
        return result

    def entity(self, entity_id: str) -> EntityColumns:
        columns = self.annotations.get(entity_id)
        if columns is None:
            columns = _empty_entity_columns(self.size)
            self.annotations[entity_id] = columns
        return columns

    def scope_mask(
        self,
        platform: str | None = None,
        channel: str | None = None,
        window_id: str | None = None,
        date: str | None = None,
    ) -> BoolArray:
        """范围内的全部观测（含无效观测），用于剔除计数和各指标的范围过滤。"""
        key = (platform, channel, window_id, date)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached
        mask = np.ones(self.size, dtype=np.bool_)
        for value, labels, codes in (
            (platform, self.platforms, self.platform),
            (channel, self.channels, self.channel),
            (window_id, self.windows, self.window),
            (date, self.dates, self.date),
        ):
            if value is None:
                continue
            if value in labels:
                mask &= codes == labels.index(value)
            else:
                mask[:] = False
        if len(self._mask_cache) > 256:
            self._mask_cache.clear()
        self._mask_cache[key] = mask
        return mask


def _empty_entity_columns(size: int) -> EntityColumns:
    def flags() -> BoolArray:
        return np.zeros(size, dtype=np.bool_)

    def numbers(fill: int = 0) -> IntArray:
        return np.full(size, fill, dtype=np.int64)

    return EntityColumns(
        present=flags(),
        mentioned=flags(),
        recommended=flags(),
        rankable=flags(),
        rank=numbers(),
        sentiment=numbers(),
        negative_miscitation=flags(),
        negative_unassessed=flags(),
        risk_identified=flags(),
        risk_confirmed=numbers(-1),
        risk_closed=flags(),
    )


def domain_matches(domain: str, candidates: Iterable[str]) -> bool:
    """域名与候选域名相同，或是其子域名（按点分段比较，后缀相同但不是子域名的不算）。"""
    return any(domain == item or domain.endswith("." + item) for item in candidates)


def _encode(labels: list[str]) -> tuple[IntArray, list[str]]:
    """把标签列表编码为升序标签表上的下标。"""
    ordered = sorted(set(labels))
    position = {label: index for index, label in enumerate(ordered)}
    return np.fromiter(
        (position[label] for label in labels), dtype=np.int64, count=len(labels)
    ), ordered


def _sorted_table(unit: str, ids: list[str], obs: list[int]) -> tuple[Table, IntArray]:
    """按编号升序建表，返回 (表, 排序用的下标)。"""
    order = np.asarray(sorted(range(len(ids)), key=ids.__getitem__), dtype=np.int64)
    id_array = np.empty(len(ids), dtype=np.object_)
    id_array[:] = ids
    obs_array = np.asarray(obs, dtype=np.int64)
    return Table(unit, id_array[order], obs_array[order]), order


class _ObservationReader:
    """逐条读取观测并累积各列（文件顺序），最后统一按 observation_id 排序。"""

    def __init__(
        self,
        root: Path,
        project: dict[str, Any],
        prompt_set: dict[str, Any],
        excluded: dict[str, str],
        target_sources: list[str],
    ) -> None:
        self.root = root
        self.resolved_root = root.resolve()
        self.timezone: str = project["timezone"]
        self.selected_hash: str = prompt_set["content_hash"]
        self.query_set = {item["query_id"] for item in prompt_set["prompts"]}
        self.excluded = excluded
        self.target_sources = target_sources
        authority = project["preregistration"]["authority"]
        self.domain_tiers: dict[str, str] = authority["domain_tiers"]
        self.authoritative_tiers = set(authority["authoritative_tiers"])
        self.environment = {
            key: value for key, value in project["environment"].items() if value is not None
        }
        self.required = frozenset(spec.load_schema("observation")["required"])
        self.seen: set[str] = set()
        self.ids: list[str] = []
        self.queries: list[str] = []
        self.platforms: list[str] = []
        self.channels: list[str] = []
        self.windows: list[str] = []
        self.dates: list[str] = []
        self.started: list[str] = []
        self.rules: list[str] = []
        self.status_valid: list[bool] = []
        self.complete: list[bool] = []
        self.documented_nulls: list[bool] = []
        self.env_ok: list[bool] = []
        self.env_unknown: list[bool] = []
        self.snapshot: list[int] = []
        self.unidentified: list[int] = []
        self.duplicates: list[int] = []
        self.citation_ids: list[str] = []
        self.citation_obs: list[int] = []
        self.citation_domains: list[str] = []
        self.citation_lookup: dict[tuple[int, int], int] = {}
        self.model_versions: dict[tuple[str, str], dict[str, list[Any]]] = {}
        self.version_counts: Counter[str] = Counter()
        self._hash_cache: dict[str, str | None] = {}
        self._domain_cache: dict[str, tuple[bool, bool, bool]] = {}

    def add(self, record: dict[str, Any]) -> None:
        for name in _STRUCTURAL_FIELDS:
            if not isinstance(record.get(name), str):
                raise KeyError(name)
        observation_id: str = record["observation_id"]
        position = len(self.ids)
        rule = self._rule(record, observation_id)
        self.seen.add(observation_id)
        self.ids.append(observation_id)
        self.queries.append(
            record["query_id"] if record["query_id"] in self.query_set else UNKNOWN_QUERY_LABEL
        )
        self.platforms.append(record["platform"])
        self.channels.append(record["channel"])
        self.windows.append(record["window_id"])
        self.dates.append(local_date(record["started_at"], self.timezone).isoformat())
        self.started.append(record["started_at"])
        self.rules.append(rule)
        self.status_valid.append(record["status"] == "valid")
        complete, documented = self._completeness(record)
        self.complete.append(complete)
        self.documented_nulls.append(documented)
        consistent, unknown = self._environment(record)
        self.env_ok.append(consistent)
        self.env_unknown.append(unknown)
        self.snapshot.append(self._snapshots(record.get("snapshots") or []))
        self._citations(position, observation_id, record.get("citations") or [])
        if rule != RULE_DUPLICATE:
            self.version_counts[record["prompt_set_hash"]] += 1
        if rule == RULE_VALID and isinstance(record.get("model_version"), str):
            self._model_version(record)

    def _rule(self, record: dict[str, Any], observation_id: str) -> str:
        if observation_id in self.seen:
            return RULE_DUPLICATE
        if record["prompt_set_hash"] != self.selected_hash:
            return RULE_VERSION_MISMATCH
        if observation_id in self.excluded:
            return AUDIT_PREFIX + self.excluded[observation_id]
        if record["query_id"] not in self.query_set:
            return RULE_UNKNOWN_QUERY
        if record["status"] != "valid":
            reason = record.get("invalid_reason") or "unspecified"
            return f"status={record['status']}:{reason}"
        return RULE_VALID

    def _completeness(self, record: dict[str, Any]) -> tuple[bool, bool]:
        """(必备字段是否齐全, 是否含已说明原因的空字段)。"""
        reasons = record.get("null_reasons")
        if not isinstance(reasons, dict):
            reasons = {}
        complete = self.required <= record.keys()
        documented = False
        for name in _NULLABLE_FIELDS:
            if record.get(name) is None:
                if name in reasons:
                    documented = True
                else:
                    complete = False
        if not record.get("snapshots"):
            if "snapshots" in reasons:
                documented = True
            else:
                complete = False
        if record.get("model_version") is None and not isinstance(
            record.get("entry_features"), str
        ):
            complete = False
        if record["status"] == "valid" and not record.get("answer_text"):
            complete = False
        return complete, documented

    def _environment(self, record: dict[str, Any]) -> tuple[bool, bool]:
        """(与登记环境一致, 有登记项无法确认)。"""
        consistent = True
        unknown = False
        for name, expected in self.environment.items():
            actual = record.get(name)
            if actual is None:
                unknown = True
                consistent = False
            elif name == "region":
                if not (actual == expected or str(actual).startswith(f"{expected}-")):
                    consistent = False
            elif actual != expected:
                consistent = False
        return consistent, unknown

    def _snapshots(self, snapshots: list[dict[str, Any]]) -> int:
        if not snapshots:
            return SNAPSHOT_NONE
        state = SNAPSHOT_OK
        for item in snapshots:
            actual = self._file_hash(item["path"])
            if actual is None:
                return SNAPSHOT_MISSING
            if actual != item["sha256"]:
                state = SNAPSHOT_MISMATCH
        return state

    def _file_hash(self, relative: str) -> str | None:
        if relative not in self._hash_cache:
            path = (self.root / relative).resolve()
            inside = path.is_relative_to(self.resolved_root)
            self._hash_cache[relative] = sha256_file(path) if inside and path.is_file() else None
        return self._hash_cache[relative]

    def _citations(
        self, position: int, observation_id: str, citations: list[dict[str, Any]]
    ) -> None:
        first_by_url: dict[str, int | None] = {}
        unidentified = duplicates = 0
        for citation in citations:
            url: str = citation["url"]
            order: int = citation["order"]
            domain = citation["domain"]
            if url in first_by_url:
                duplicates += 1
                row = first_by_url[url]
                if row is not None:
                    self.citation_lookup.setdefault((position, order), row)
                continue
            if domain is None:
                unidentified += 1
                first_by_url[url] = None
                continue
            row = len(self.citation_ids)
            first_by_url[url] = row
            self.citation_ids.append(f"{observation_id}#{order}")
            self.citation_obs.append(position)
            self.citation_domains.append(str(domain).lower())
            self.citation_lookup.setdefault((position, order), row)
        self.unidentified.append(unidentified)
        self.duplicates.append(duplicates)

    def _model_version(self, record: dict[str, Any]) -> None:
        versions = self.model_versions.setdefault((record["platform"], record["channel"]), {})
        started: str = record["started_at"]
        entry = versions.setdefault(record["model_version"], [0, started, started])
        entry[0] += 1
        entry[1] = min(entry[1], started)
        entry[2] = max(entry[2], started)

    def domain_flags(self, domain: str) -> tuple[bool, bool, bool]:
        """(是目标信源, 是权威信源, 有登记等级)。"""
        cached = self._domain_cache.get(domain)
        if cached is None:
            tier = next(
                (
                    value
                    for key, value in sorted(
                        self.domain_tiers.items(), key=lambda item: -len(item[0])
                    )
                    if domain_matches(domain, [key])
                ),
                None,
            )
            cached = (
                domain_matches(domain, self.target_sources),
                tier in self.authoritative_tiers,
                tier is not None,
            )
            self._domain_cache[domain] = cached
        return cached


def _read_stream(path: Path, contract: str, handle: Callable[[dict[str, Any]], None]) -> None:
    """逐行处理 JSONL；记录结构不合契约时报出文件和行号，而不是内部错误。"""
    if not path.exists():
        return
    for number, record in iter_jsonl(path):
        try:
            handle(record)
        except InputError as exc:
            raise InputError(f"{path.name} 第 {number} 行：{exc.message}") from exc
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise InputError(
                f"{path.name} 第 {number} 行不符合 {contract} 契约"
                f"（{type(exc).__name__}: {exc}）；请先运行 geo-verify validate 检查"
            ) from exc


def build_index(root: str | Path, version: int | None = None) -> ProjectIndex:
    """读取项目目录并建立索引。version 为封存题库版本号，默认取最新版本。"""
    paths = project_module.paths(root)
    project = project_module.load_project(root)
    entities = {item["entity_id"]: item for item in project_module.load_entities(root)}
    target_id: str = project["target_entity_id"]
    if target_id not in entities:
        raise InputError(f"目标主体 {target_id} 不在 entities.jsonl 中")
    sealed = project_module.load_sealed_prompt_sets(root)
    prompt_set = project_module.select_prompt_set(root, version)
    excluded = project_module.exclusions(project_module.load_audit(root))
    target_sources = sorted(
        set(project["preregistration"]["target_sources"])
        | set(entities[target_id]["owned_domains"])
    )

    reader = _ObservationReader(paths.root, project, prompt_set, excluded, target_sources)
    _read_stream(paths.observations, "observation", reader.add)
    # order：排序后的位置 → 文件内行号；sorted_position：文件内行号 → 排序后的位置
    observations, order = _sorted_table("observation", reader.ids, list(range(len(reader.ids))))
    size = len(observations)
    sorted_position = np.empty(size, dtype=np.int64)
    sorted_position[order] = np.arange(size, dtype=np.int64)
    first_position: dict[str, int] = {}
    for position, observation_id in enumerate(observations.ids.tolist()):
        first_position.setdefault(observation_id, position)

    def column(values: list[Any], dtype: Any) -> Any:
        return np.asarray(values, dtype=dtype)[order]

    queries = sorted(reader.query_set)
    query_position = {query: position for position, query in enumerate(queries)}
    query_position[UNKNOWN_QUERY_LABEL] = len(queries)
    expects = np.zeros(len(queries) + 1, dtype=np.bool_)
    for item in prompt_set["prompts"]:
        expects[query_position[item["query_id"]]] = item.get("expects_recommendation") is True
    platform, platforms = _encode(reader.platforms)
    channel, channels = _encode(reader.channels)
    window, windows = _encode(reader.windows)
    date, dates = _encode(reader.dates)
    rule_labels = [RULE_VALID, *sorted(set(reader.rules) - {RULE_VALID})]
    rule_position = {rule: position for position, rule in enumerate(rule_labels)}
    citations, citation_row = _citation_table(reader, sorted_position)

    index = ProjectIndex(
        project=project,
        prompt_set=prompt_set,
        sealed=sealed,
        entities=entities,
        target_id=target_id,
        queries=queries,
        expects=expects,
        observations=Table("observation", observations.ids, np.arange(size, dtype=np.int64)),
        query=column([query_position[item] for item in reader.queries], np.int64),
        platform=platform[order],
        platforms=platforms,
        channel=channel[order],
        channels=channels,
        window=window[order],
        windows=windows,
        date=date[order],
        dates=dates,
        started=column(reader.started, np.object_),
        state=column([rule_position[rule] for rule in reader.rules], np.int64),
        rules=rule_labels,
        status_valid=column(reader.status_valid, np.bool_),
        complete=column(reader.complete, np.bool_),
        documented_nulls=column(reader.documented_nulls, np.bool_),
        env_ok=column(reader.env_ok, np.bool_),
        env_unknown=column(reader.env_unknown, np.bool_),
        snapshot=column(reader.snapshot, np.int64),
        unidentified_citations=column(reader.unidentified, np.int64),
        duplicate_citations=column(reader.duplicates, np.int64),
        annotations={},
        citations=citations,
        judgments=_empty_judgments(),
        recheck=None,
        target_sources=target_sources,
        model_versions=reader.model_versions,
        version_counts=reader.version_counts,
        excluded_by_audit=excluded,
    )
    index.counters["duplicate_observation_ids"] = reader.rules.count(RULE_DUPLICATE)
    _load_annotations(
        index, paths.root, first_position, order, reader.citation_lookup, citation_row
    )
    index.judgments = _load_judgments(index, paths.judgments, first_position)
    index.recheck = _load_recheck(paths.recheck_dir / "result.json", first_position)
    return index


def _citation_table(
    reader: _ObservationReader, sorted_position: IntArray
) -> tuple[CitationTable, IntArray]:
    """返回 (引用表, 建表时的行号 → 排序后的行号)。"""
    obs = [int(sorted_position[item]) for item in reader.citation_obs]
    table, order = _sorted_table("citation", reader.citation_ids, obs)
    row_of = np.empty(len(table), dtype=np.int64)
    row_of[order] = np.arange(len(table), dtype=np.int64)
    domain, domains = _encode(reader.citation_domains)
    flags = [reader.domain_flags(item) for item in domains]

    def lookup(position: int) -> BoolArray:
        per_domain = np.asarray([item[position] for item in flags], dtype=np.bool_)
        result: BoolArray = per_domain[domain[order]]
        return result

    citations = CitationTable(
        table=table,
        domain=domain[order],
        domains=domains,
        is_target=lookup(0),
        is_authority=lookup(1),
        tiered=lookup(2),
        check=np.zeros(len(table), dtype=np.int64),
    )
    return citations, row_of


def _load_annotations(
    index: ProjectIndex,
    root: Path,
    first_position: dict[str, int],
    order: IntArray,
    citation_lookup: dict[tuple[int, int], int],
    citation_row: IntArray,
) -> None:
    """取每个（观测，实体）当前生效的标注填入各列；目标主体标注里的引用核验写入引用表。"""
    annotations = project_module.load_annotations(root)
    try:
        effective = project_module.effective_annotations(annotations)
    except (KeyError, TypeError) as exc:
        raise InputError(
            f"annotations.jsonl 中有不符合 annotation 契约的记录（{type(exc).__name__}: {exc}）；"
            "请先运行 geo-verify validate 检查"
        ) from exc
    for (observation_id, entity_id), annotation in effective.items():
        position = first_position.get(observation_id)
        if position is None:
            index.counters["annotations_without_observation"] += 1
            continue
        try:
            _fill_annotation(index.entity(entity_id), position, annotation)
            checks = annotation.get("citation_checks") or []
            if entity_id != index.target_id or not checks:
                continue
            file_row = int(order[position])
            for check in checks:
                created = citation_lookup.get((file_row, check["order"]))
                if created is None:
                    continue
                row = int(citation_row[created])
                if index.citations.check[row] == CHECK_UNVERIFIED:
                    index.citations.check[row] = _check_state(check)
        except (KeyError, TypeError, AttributeError) as exc:
            raise InputError(
                f"标注 {annotation.get('annotation_id')} 不符合 annotation 契约"
                f"（{type(exc).__name__}: {exc}）；请先运行 geo-verify validate 检查"
            ) from exc
    target = index.entity(index.target_id)
    index.counters["unannotated_observations"] = int((index.valid & ~target.present).sum())


def _check_state(check: dict[str, Any]) -> int:
    if check.get("valid") is False:
        return CHECK_INVALID
    if check.get("valid") is True and check.get("relevant") is True:
        return CHECK_VALID_RELEVANT
    if check.get("valid") is True and check.get("relevant") is False:
        return CHECK_NOT_RELEVANT
    return CHECK_UNVERIFIED


def _fill_annotation(columns: EntityColumns, position: int, annotation: dict[str, Any]) -> None:
    risk = annotation.get("risk") or {}
    flags = risk.get("flags") or []
    sentiment = annotation.get("sentiment")
    unfounded = risk.get("unfounded_negative")
    columns.present[position] = True
    columns.mentioned[position] = annotation["mentioned"] is True
    columns.recommended[position] = annotation.get("recommended") is True
    columns.rankable[position] = annotation.get("rankable") is True
    columns.rank[position] = annotation.get("rank") or 0
    columns.sentiment[position] = SENTIMENTS.get(sentiment, 0) if sentiment else 0
    columns.negative_miscitation[position] = (
        unfounded is True
        or "unfounded_negative" in flags
        or ("wrong_fact" in flags and sentiment == "negative")
    )
    columns.negative_unassessed[position] = unfounded is None and not flags
    columns.risk_identified[position] = risk.get("identified") is True
    confirmed = risk.get("confirmed")
    columns.risk_confirmed[position] = -1 if confirmed is None else int(confirmed is True)
    columns.risk_closed[position] = risk.get("closed") is True


def _empty_judgments() -> JudgmentTable:
    empty = np.zeros(0, dtype=np.int64)
    ids = np.empty(0, dtype=np.object_)
    return JudgmentTable(Table("judgment", ids, empty), empty, empty, empty, [])


def _load_judgments(
    index: ProjectIndex, path: Path, first_position: dict[str, int]
) -> JudgmentTable:
    ids: list[str] = []
    obs: list[int] = []
    directions: list[int] = []
    labels: list[int] = []
    facts: list[str] = []
    seen: set[str] = set()

    def handle(record: dict[str, Any]) -> None:
        judgment_id: str = record["judgment_id"]
        source = record["source"]
        if source["type"] != "observation":
            return
        if judgment_id in seen:
            index.counters["duplicate_judgment_ids"] += 1
            return
        seen.add(judgment_id)
        position = first_position.get(source["id"])
        if position is None:
            index.counters["judgments_without_observation"] += 1
            return
        direction = DIRECTIONS[record["direction"]]
        label = LABELS[record["label"]]
        if (label >= 3) != (direction == 1):
            raise ValueError(f"判定 {judgment_id} 的方向与标签不匹配")
        ids.append(judgment_id)
        obs.append(position)
        directions.append(direction)
        labels.append(label)
        facts.append(record.get("fact_id") or "")

    _read_stream(path, "judgment", handle)
    table, order = _sorted_table("judgment", ids, obs)
    fact, fact_labels = _encode(facts)
    return JudgmentTable(
        table=table,
        direction=np.asarray(directions, dtype=np.int64)[order],
        label=np.asarray(labels, dtype=np.int64)[order],
        fact=fact[order],
        facts=fact_labels,
    )


def _load_recheck(path: Path, first_position: dict[str, int]) -> RecheckTable | None:
    """读取抽样复核比对结果；文件不存在时返回 None。"""
    if not path.exists():
        return None
    data = read_json(path)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise InputError(
            "recheck/result.json 缺少 items 列表，请重新运行 geo-verify recheck-compare"
        )
    ids: list[str] = []
    obs: list[int] = []
    agree: list[bool] = []
    checked: list[bool] = []
    unmatched = 0
    seen: set[str] = set()
    for item in items:
        try:
            item_id = f"{item['observation_id']}|{item['entity_id']}"
            agreed = item["agree"]
            position = first_position.get(item["observation_id"])
        except (KeyError, TypeError) as exc:
            raise InputError(
                "recheck/result.json 的 items 须含 observation_id、entity_id、agree"
            ) from exc
        if agreed is not None and not isinstance(agreed, bool):
            raise InputError(
                f"recheck/result.json 中 {item_id} 的 agree 须为 true、false 或 null（尚未复核）"
            )
        if position is None or item_id in seen:
            unmatched += 1
            continue
        seen.add(item_id)
        ids.append(item_id)
        obs.append(position)
        agree.append(agreed is True)
        checked.append(agreed is not None)
    table, order = _sorted_table("observation", ids, obs)
    return RecheckTable(
        table,
        np.asarray(agree, dtype=np.bool_)[order],
        np.asarray(checked, dtype=np.bool_)[order],
        unmatched,
    )
