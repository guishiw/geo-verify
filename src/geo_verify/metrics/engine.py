"""指标计算引擎：给定索引、指标编号和范围，产出一条 metric_result 及其下钻列表。

约定：
- 分母为 0 时 value 为 null 并给出 null_reason，绝不返回 0 或 100%；
- 区间：范围内只有一道题、或每道题只有一个计数单元时用 Wilson；
  跨题且同题有多个单元时用按 query_id 聚类的 bootstrap；
- 每个结果都对分子、分母的编号列表作出哈希承诺，可用 `metrics --drilldown` 复核。
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from geo_verify import project as project_module
from geo_verify.errors import InputError
from geo_verify.metrics.definitions import (
    DEFINITION_VERSION,
    KIND_CROSS,
    KIND_QUERY,
    KIND_TEMPORAL,
    METRICS,
    MetricDefinition,
    resolve_registered_id,
)
from geo_verify.metrics.index import (
    CHECK_UNVERIFIED,
    CHECK_VALID_RELEVANT,
    RULE_DUPLICATE,
    RULE_UNANNOTATED,
    RULE_VERSION_MISMATCH,
    SENTIMENTS,
    SNAPSHOT_MISMATCH,
    SNAPSHOT_MISSING,
    SNAPSHOT_NONE,
    SNAPSHOT_OK,
    UNKNOWN_QUERY_LABEL,
    BoolArray,
    IntArray,
    ProjectIndex,
    Table,
)
from geo_verify.stats import (
    assess_sufficiency,
    cluster_bootstrap_ratio,
    summarize_clusters,
    wilson_interval,
)

SCHEMA_VERSION = "1.0"
INLINE_ID_LIMIT = 200
MAJORITY_NOTE = "平台内比例 ≥ 0.5 记为“是”"
DEGENERATE_NOTE = (
    "全部命中或全部未命中时，按题重抽的区间退化为零宽；"
    "此处改用以题数为 n 的 Wilson 区间（相当于按题内完全相关处理，偏保守）"
)

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LABEL_NAMES = (
    "supported",
    "contradicted",
    "unverifiable",
    "present_correct",
    "present_wrong",
    "missing",
)


@dataclass(frozen=True)
class Scope:
    platform: str | None = None
    channel: str | None = None
    window_id: str | None = None
    date: str | None = None

    def __post_init__(self) -> None:
        if self.date is not None and not _DATE_PATTERN.match(self.date):
            raise InputError(f"日期格式应为 YYYY-MM-DD：{self.date!r}")

    def as_tuple(self) -> tuple[str | None, str | None, str | None, str | None]:
        return (self.platform, self.channel, self.window_id, self.date)

    @property
    def overall(self) -> bool:
        return self.as_tuple() == (None, None, None, None)

    def to_dict(self, entity_id: str | None) -> dict[str, str | None]:
        return {
            "platform": self.platform,
            "channel": self.channel,
            "window_id": self.window_id,
            "date": self.date,
            "entity_id": entity_id,
        }


@dataclass(frozen=True)
class Parameters:
    seed: int
    iterations: int
    level: float
    min_windows: int
    min_valid_observations: int


def parameters_for(
    project: dict[str, Any], *, seed: int | None = None, iterations: int | None = None
) -> Parameters:
    """统计参数默认取预先登记值；seed 与重抽次数可由命令行覆盖。"""
    prereg = project["preregistration"]
    statistics_config = prereg["statistics"]
    if iterations is not None and iterations < 1:
        raise InputError("--iterations 必须为正整数")
    if seed is not None and seed < 0:
        raise InputError("--seed 不能为负")
    return Parameters(
        seed=statistics_config["seed"] if seed is None else seed,
        iterations=statistics_config["bootstrap_iterations"] if iterations is None else iterations,
        level=statistics_config["confidence_level"],
        min_windows=prereg["sufficiency"]["min_windows"],
        min_valid_observations=prereg["sufficiency"]["min_valid_observations"],
    )


@dataclass(frozen=True)
class Registration:
    preregistered: bool
    role: str
    threshold: dict[str, Any] | None = None


POST_HOC = Registration(False, "post_hoc")


def resolve_registrations(
    project: dict[str, Any], prompt_set: dict[str, Any]
) -> tuple[dict[str, Registration], list[dict[str, Any]], list[dict[str, Any]]]:
    """解析预先登记的指标。

    返回 (指标编号 → 登记信息, 登记清单（供报告使用）, 提示)。
    封存后登记内容被改动时，全部指标都只能作为事后指标。
    """
    prereg = project["preregistration"]
    notices: list[dict[str, Any]] = []
    current_hash = project_module.preregistration_hash(project)
    changed = current_hash != prompt_set["preregistration_hash"]
    if changed:
        notices.append(
            _notice(
                "preregistration_changed",
                "error",
                "预先登记内容在题库封存后被改动：当前口径与封存时登记的口径不一致，"
                "全部指标只能作为事后指标展示，不能用于验收结论。",
                sealed_hash=prompt_set["preregistration_hash"],
                current_hash=current_hash,
            )
        )
    registrations: dict[str, Registration] = {}
    registered: list[dict[str, Any]] = []
    for item in prereg["metrics"]:
        metric_ids = resolve_registered_id(item["metric_id"], prereg["accuracy_basis"])
        entry = {
            "registered_id": item["metric_id"],
            "metric_ids": list(metric_ids),
            "role": item["role"],
            "threshold": item["threshold"],
            "definition_version": item["definition_version"],
            "effective": True,
            "note": None,
        }
        if not metric_ids:
            entry.update(effective=False, note="本工具的指标表中没有这个编号，未计算")
            notices.append(
                _notice(
                    "unknown_registered_metric",
                    "warning",
                    f"预先登记的指标 {item['metric_id']} 不在本工具的指标表中，未计算。",
                    metric_id=item["metric_id"],
                )
            )
        elif item["definition_version"] != DEFINITION_VERSION:
            entry.update(
                effective=False,
                note=f"登记的口径版本 {item['definition_version']} 与工具实现的 {DEFINITION_VERSION} 不同",
            )
            notices.append(
                _notice(
                    "definition_version_mismatch",
                    "warning",
                    f"指标 {item['metric_id']} 登记的口径版本为 {item['definition_version']}，"
                    f"本工具实现的是 {DEFINITION_VERSION}：计算结果只能作为事后指标展示。",
                    metric_id=item["metric_id"],
                    registered_version=item["definition_version"],
                    implemented_version=DEFINITION_VERSION,
                )
            )
        elif changed:
            entry.update(effective=False, note="预先登记内容在封存后被改动")
        else:
            for metric_id in metric_ids:
                registrations[metric_id] = Registration(True, item["role"], item["threshold"])
        registered.append(entry)
    return registrations, registered, notices


def _notice(code: str, severity: str, message: str, **details: Any) -> dict[str, Any]:
    notice: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if details:
        notice["details"] = details
    return notice


def ids_hash(ids: list[str]) -> str:
    """已排序编号列表的哈希：规范化 JSON（RFC 8785）+ SHA-256。

    对字符串数组而言，RFC 8785 的序列化与“紧凑分隔符、不转义非 ASCII”的 JSON 相同，
    这里直接用标准库生成同样的字节串，以便处理十万级的列表（测试中与 canon.hash_obj 比对）。
    """
    text = json.dumps(ids, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Computation:
    """一次计算的完整结果：契约对象 + 下钻编号列表 + 各题的（分子, 分母）。"""

    result: dict[str, Any]
    numerator_ids: list[str] | None
    denominator_ids: list[str] | None
    clusters: dict[str, tuple[int, int]] = field(default_factory=dict)
    table: Table | None = None
    denominator_rows: IntArray | None = None


@dataclass(frozen=True)
class _Ratio:
    """比例型指标在整张表上的分子、分母掩码（与范围无关，可缓存）。"""

    table: Table
    numerator: BoolArray
    denominator: BoolArray
    zero_reason: Callable[[BoolArray], str]
    components: Callable[[BoolArray, BoolArray], dict[str, Any]] | None = None
    universe_based: bool = False
    needs_annotation: bool = False
    not_applicable: str | None = None


def _count(mask: BoolArray) -> int:
    return int(np.count_nonzero(mask))


def _reason(text: str) -> Callable[[BoolArray], str]:
    return lambda _rows: text


class Engine:
    def __init__(self, index: ProjectIndex, params: Parameters) -> None:
        self.index = index
        self.params = params
        self.registrations, self.registered, self.registration_notices = resolve_registrations(
            index.project, index.prompt_set
        )
        self._ratios: dict[tuple[str, str], _Ratio] = {}
        self._excluded: dict[tuple[tuple[str | None, ...], bool], list[tuple[str, int]]] = {}

    # ------------------------------------------------------------ 入口

    def compute(
        self, metric_id: str, scope: Scope | None = None, entity_id: str | None = None
    ) -> Computation:
        definition = METRICS.get(metric_id)
        if definition is None:
            raise InputError(f"未知指标：{metric_id}；可选：{', '.join(METRICS)}")
        scope = scope or Scope()
        entity = entity_id or self.index.target_id
        if metric_id == "recommendation_rate_daily" and scope.date is None:
            raise InputError("recommendation_rate_daily 是按日指标，需要指定日期（--date）")
        if definition.kind == KIND_CROSS:
            if scope.platform is not None:
                raise InputError(f"{metric_id} 是跨平台指标，不适用于单一平台范围")
            return self._cross_platform(definition, scope, entity)
        if definition.kind == KIND_QUERY:
            return self._coverage(definition, scope, entity)
        if definition.kind == KIND_TEMPORAL:
            return self._temporal(definition, scope, entity)
        return self._ratio(definition, scope, entity)

    # ------------------------------------------------------------ 公共部件

    def _base_result(
        self, definition: MetricDefinition, scope: Scope, entity: str
    ) -> dict[str, Any]:
        registration = self.registrations.get(definition.metric_id, POST_HOC)
        if entity != self.index.target_id:
            registration = POST_HOC
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "metric_id": definition.metric_id,
            "project_id": self.index.project["project_id"],
            "scope": scope.to_dict(entity if definition.entity_scoped else None),
            "definition_version": DEFINITION_VERSION,
            "preregistered": registration.preregistered,
            "role": registration.role,
        }
        if self.index.project.get("synthetic") is True:
            result["synthetic"] = True
        return result

    def _excluded_rules(
        self, scope: Scope, *, universe_based: bool, unannotated: int = 0
    ) -> dict[str, Any]:
        """范围内未进入该指标观测基数的观测：数量与规则。"""
        key = (scope.as_tuple(), universe_based)
        rules = self._excluded.get(key)
        if rules is None:
            index = self.index
            mask = index.scope_mask(*scope.as_tuple())
            counts = np.bincount(index.state[mask], minlength=len(index.rules))
            rules = [
                (rule, int(counts[code]))
                for code, rule in enumerate(index.rules)
                if code > 0
                and counts[code] > 0
                and (not universe_based or rule in (RULE_VERSION_MISMATCH, RULE_DUPLICATE))
            ]
            self._excluded[key] = rules
        listed = list(rules)
        if unannotated:
            listed.append((RULE_UNANNOTATED, unannotated))
        listed.sort()
        return {
            "count": sum(count for _, count in listed),
            "rules": [{"rule": rule, "count": count} for rule, count in listed],
        }

    def _sufficiency(self, distinct: IntArray) -> dict[str, Any]:
        """由进入分母的观测（去重后的位置数组）评估样本充分性。"""
        return assess_sufficiency(
            windows=int(np.unique(self.index.window[distinct]).size),
            valid_observations=int(distinct.size),
            min_windows=self.params.min_windows,
            min_valid_observations=self.params.min_valid_observations,
        )

    def _coverage_info(self, distinct: IntArray) -> dict[str, Any]:
        """结果所依据的观测（去重后的位置数组）覆盖了哪些题、日期、窗口、平台和通道。"""
        index = self.index
        if distinct.size == 0:
            return {
                "observations": 0,
                "queries": 0,
                "date_from": None,
                "date_to": None,
                "windows": [],
                "platforms": [],
                "channels": {},
            }
        dates = index.date[distinct]
        channel_counts = np.bincount(index.channel[distinct], minlength=len(index.channels))
        return {
            "observations": int(distinct.size),
            "queries": int(np.unique(index.query[distinct]).size),
            "date_from": index.dates[int(dates.min())],
            "date_to": index.dates[int(dates.max())],
            "windows": [index.windows[int(code)] for code in np.unique(index.window[distinct])],
            "platforms": [
                index.platforms[int(code)] for code in np.unique(index.platform[distinct])
            ],
            "channels": {
                index.channels[code]: int(count)
                for code, count in enumerate(channel_counts)
                if count > 0
            },
        }

    def _interval(
        self, numerators: list[int], denominators: list[int]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """由各题的（分子, 分母）给出区间；返回 (区间, 说明)。"""
        level = self.params.level
        clusters = len(denominators)
        numerator, denominator = sum(numerators), sum(denominators)
        if denominator == 0:
            return None, None
        note: str | None = None
        if clusters == 1 or max(denominators) == 1:
            low, high = wilson_interval(numerator, denominator, level)
            method = "wilson"
        elif numerator in (0, denominator):
            low, high = wilson_interval(clusters if numerator else 0, clusters, level)
            method, note = "wilson", DEGENERATE_NOTE
        else:
            low, high = cluster_bootstrap_ratio(
                numerators,
                denominators,
                seed=self.params.seed,
                iterations=self.params.iterations,
                level=level,
            )
            method = "cluster_bootstrap"
        return {"method": method, "level": level, "low": low, "high": high}, note

    def _drilldown(
        self, unit: str, numerator_ids: list[str] | None, denominator_ids: list[str] | None
    ) -> dict[str, Any]:
        drilldown: dict[str, Any] = {
            "unit": unit,
            "numerator_ids_hash": None if numerator_ids is None else ids_hash(numerator_ids),
            "denominator_ids_hash": None if denominator_ids is None else ids_hash(denominator_ids),
        }
        for name, ids in (("numerator_ids", numerator_ids), ("denominator_ids", denominator_ids)):
            if ids is not None and len(ids) <= INLINE_ID_LIMIT:
                drilldown[name] = ids
        return drilldown

    def _annotated(self, entity: str) -> BoolArray:
        result: BoolArray = self.index.valid & self.index.entity(entity).present
        return result

    def _unannotated(self, scope: Scope, entity: str) -> int:
        index = self.index
        mask = index.valid & ~index.entity(entity).present & index.scope_mask(*scope.as_tuple())
        return _count(mask)

    def _success(self, entity: str) -> BoolArray:
        """按预先登记的“推荐成功”定义，逐观测判断是否成功。"""
        columns = self.index.entity(entity)
        rule = self.index.project["preregistration"]["recommendation_success"]
        result: BoolArray
        if rule["criterion"] == "top_n":
            result = (columns.rank > 0) & (columns.rank <= rule["top_n"])
        elif rule["criterion"] == "mentioned":
            result = columns.mentioned
        else:
            result = columns.recommended
        return result

    # ------------------------------------------------------------ 比例型指标

    def _ratio(self, definition: MetricDefinition, scope: Scope, entity: str) -> Computation:
        index = self.index
        key = (definition.metric_id, entity)
        spec = self._ratios.get(key)
        if spec is None:
            spec = self._ratio_spec(definition.metric_id, entity)
            self._ratios[key] = spec
        obs_scope = index.scope_mask(*scope.as_tuple())
        table = spec.table
        row_scope = obs_scope if table is index.observations else obs_scope[table.obs]
        den_rows = spec.denominator & row_scope
        num_rows = spec.numerator & den_rows
        result = self._base_result(definition, scope, entity)
        unannotated = self._unannotated(scope, entity) if spec.needs_annotation else 0
        result["excluded"] = self._excluded_rules(
            scope, universe_based=spec.universe_based, unannotated=unannotated
        )
        components = spec.components(row_scope, obs_scope) if spec.components else {}
        if spec.not_applicable is not None:
            empty = np.zeros(0, dtype=np.int64)
            result.update(
                value=None,
                null_reason=spec.not_applicable,
                numerator=None,
                denominator=None,
                interval=None,
                clustering=None,
                sufficiency=self._sufficiency(empty),
                drilldown=self._drilldown(definition.unit, None, None),
                ext={"coverage": self._coverage_info(empty)},
            )
            if components:
                result["components"] = components
            return Computation(result, None, None)

        den_obs = table.obs[den_rows]
        distinct = den_obs if table is index.observations else np.unique(den_obs)
        size = len(index.queries) + 1
        den_by_query = np.bincount(index.query[den_obs], minlength=size)
        num_by_query = np.bincount(index.query[table.obs[num_rows]], minlength=size)
        present = np.flatnonzero(den_by_query)
        labels = [*index.queries, UNKNOWN_QUERY_LABEL]
        numerators = [int(num_by_query[code]) for code in present]
        denominators = [int(den_by_query[code]) for code in present]
        clusters = {
            labels[int(code)]: (numerators[position], denominators[position])
            for position, code in enumerate(present)
        }
        numerator, denominator = sum(numerators), sum(denominators)
        interval, note = self._interval(numerators, denominators)
        if note:
            components["interval_note"] = note
        numerator_ids: list[str] = table.ids[num_rows].tolist()
        denominator_ids: list[str] = table.ids[den_rows].tolist()
        result.update(
            value=numerator / denominator if denominator else None,
            null_reason=None if denominator else f"{spec.zero_reason(row_scope)}，分母为 0",
            numerator=numerator,
            denominator=denominator,
            interval=interval,
            clustering=summarize_clusters(numerators, denominators).to_dict()
            if denominator
            else None,
            sufficiency=self._sufficiency(distinct),
            drilldown=self._drilldown(definition.unit, numerator_ids, denominator_ids),
            ext={"coverage": self._coverage_info(distinct)},
        )
        if components:
            result["components"] = components
        return Computation(
            result, numerator_ids, denominator_ids, clusters, table, np.flatnonzero(den_rows)
        )

    def _ratio_spec(self, metric_id: str, entity: str) -> _Ratio:
        index = self.index
        obs = index.observations
        if metric_id in ("mention_rate", "top1_rate", "top3_rate", "top5_rate"):
            return self._spec_mention_or_rank(metric_id, entity)
        if metric_id in ("recommendation_rate", "recommendation_rate_daily"):
            return self._spec_recommendation(entity)
        if metric_id == "negative_miscitation_rate":
            base = self._annotated(entity)
            columns = index.entity(entity)
            return _Ratio(
                obs,
                base & columns.negative_miscitation,
                base,
                _reason("没有带标注的有效观测"),
                lambda rows, _obs: {
                    "unassessed": _count(base & columns.negative_unassessed & rows)
                },
                needs_annotation=True,
            )
        if metric_id.startswith("sentiment_"):
            columns = index.entity(entity)
            base = self._annotated(entity) & columns.mentioned & (columns.sentiment > 0)
            code = SENTIMENTS[metric_id.split("_")[1]]
            return _Ratio(
                obs,
                base & (columns.sentiment == code),
                base,
                _reason("没有带倾向标注的提及观测"),
                needs_annotation=True,
            )
        if metric_id in ("risk_identification_rate", "risk_closure_rate"):
            return self._spec_risk(metric_id, entity)
        if metric_id in ("target_source_presence", "target_source_share", "authority_share"):
            return self._spec_sources(metric_id)
        if metric_id == "citation_validity":
            return self._spec_citation_validity()
        if metric_id in (
            "accuracy_verifiable",
            "accuracy_strict",
            "unverifiable_rate",
            "core_fact_presence",
        ):
            return self._spec_judgments(metric_id)
        if metric_id == "recheck_pass_rate":
            return self._spec_recheck()
        return self._spec_process(metric_id)

    def _spec_mention_or_rank(self, metric_id: str, entity: str) -> _Ratio:
        columns = self.index.entity(entity)
        base = self._annotated(entity)
        if metric_id == "mention_rate":
            return _Ratio(
                self.index.observations,
                base & columns.mentioned,
                base,
                _reason("没有带标注的有效观测"),
                needs_annotation=True,
            )
        top_n = int(metric_id[3])
        rankable = base & columns.rankable
        return _Ratio(
            self.index.observations,
            rankable & (columns.rank > 0) & (columns.rank <= top_n),
            rankable,
            _reason("没有可排序的观测"),
            needs_annotation=True,
        )

    def _spec_recommendation(self, entity: str) -> _Ratio:
        index = self.index
        base = self._annotated(entity)
        expected: BoolArray = index.expects[index.query]
        rule = index.project["preregistration"]["recommendation_success"]

        def components(rows: BoolArray, _obs: BoolArray) -> dict[str, Any]:
            return {
                "criterion": rule["criterion"],
                "top_n": rule["top_n"],
                "not_expected_observations": _count(base & ~expected & rows),
            }

        return _Ratio(
            index.observations,
            base & expected & self._success(entity),
            base & expected,
            _reason("没有应推荐的有效观测"),
            components,
            needs_annotation=True,
        )

    def _spec_risk(self, metric_id: str, entity: str) -> _Ratio:
        columns = self.index.entity(entity)
        base = self._annotated(entity)
        if metric_id == "risk_identification_rate":
            confirmed = base & (columns.risk_confirmed == 1)
            return _Ratio(
                self.index.observations,
                confirmed & columns.risk_identified,
                confirmed,
                _reason("没有经复核确认的风险"),
                lambda rows, _obs: {
                    "unreviewed": _count(base & (columns.risk_confirmed == -1) & rows)
                },
                needs_annotation=True,
            )
        identified = base & columns.risk_identified
        return _Ratio(
            self.index.observations,
            identified & columns.risk_closed,
            identified,
            _reason("没有已识别的风险"),
            lambda rows, _obs: {
                "identified_not_confirmed": _count(
                    identified & (columns.risk_confirmed == 0) & rows
                )
            },
            needs_annotation=True,
        )

    def _spec_sources(self, metric_id: str) -> _Ratio:
        index = self.index
        citations = index.citations
        if metric_id == "target_source_presence":
            cited = np.zeros(index.size, dtype=np.bool_)
            cited[citations.table.obs[citations.is_target]] = True
            return _Ratio(
                index.observations,
                index.valid & cited,
                index.valid,
                _reason("没有有效观测"),
                lambda _rows, _obs: {"target_sources": index.target_sources},
            )
        row_valid: BoolArray = index.valid[citations.table.obs]
        authority = index.project["preregistration"]["authority"]

        def components(rows: BoolArray, obs_scope: BoolArray) -> dict[str, Any]:
            in_scope = index.valid & obs_scope
            info: dict[str, Any] = {
                "unidentified_citations": int(index.unidentified_citations[in_scope].sum()),
                "duplicate_citations": int(index.duplicate_citations[in_scope].sum()),
            }
            if metric_id == "target_source_share":
                info["target_sources"] = index.target_sources
            else:
                info["authoritative_tiers"] = sorted(authority["authoritative_tiers"])
                info["untiered_citations"] = _count(row_valid & ~citations.tiered & rows)
            return info

        flag = citations.is_target if metric_id == "target_source_share" else citations.is_authority
        return _Ratio(
            citations.table, row_valid & flag, row_valid, _reason("没有可识别的引用"), components
        )

    def _spec_citation_validity(self) -> _Ratio:
        index = self.index
        citations = index.citations
        row_valid: BoolArray = index.valid[citations.table.obs]
        verified = row_valid & (citations.check != CHECK_UNVERIFIED)

        def components(rows: BoolArray, _obs: BoolArray) -> dict[str, Any]:
            identifiable = _count(row_valid & rows)
            checked = _count(verified & rows)
            info: dict[str, Any] = {
                "identifiable_citations": identifiable,
                "verified_citations": checked,
                "unverified_citations": identifiable - checked,
            }
            if identifiable - checked:
                info["note"] = f"有 {identifiable - checked} 条可识别引用尚未核验，未计入分母"
            return info

        def zero_reason(rows: BoolArray) -> str:
            if _count(row_valid & rows) == 0:
                return "没有可识别的引用"
            return "没有已核验的引用（标注中未提供 citation_checks）"

        return _Ratio(
            citations.table,
            row_valid & (citations.check == CHECK_VALID_RELEVANT),
            verified,
            zero_reason,
            components,
        )

    def _spec_judgments(self, metric_id: str) -> _Ratio:
        index = self.index
        judgments = index.judgments
        row_valid: BoolArray = index.valid[judgments.table.obs]
        forward = row_valid & (judgments.direction == 0)
        backward = row_valid & (judgments.direction == 1)
        basis: str = index.project["preregistration"]["accuracy_basis"]

        def counts(rows: BoolArray, codes: tuple[int, ...]) -> dict[str, Any]:
            info: dict[str, Any] = {
                _LABEL_NAMES[code]: _count(row_valid & (judgments.label == code) & rows)
                for code in codes
            }
            if metric_id.startswith("accuracy_"):
                info["accuracy_basis"] = basis
                info["is_registered_basis"] = metric_id == f"accuracy_{basis}"
            info["judgments_from_non_valid_observations"] = _count(~row_valid & rows)
            return info

        if metric_id == "core_fact_presence":
            return _Ratio(
                judgments.table,
                backward & (judgments.label == 3),
                backward,
                _reason("没有可用的判定（事实库 → 回答方向）"),
                lambda rows, _obs: counts(rows, (3, 4, 5)),
            )

        def zero_reason(rows: BoolArray) -> str:
            if _count(forward & rows):
                return "没有可核验的陈述（支持 + 矛盾为 0）"
            return "没有可用的判定（回答 → 事实库方向）"

        if metric_id == "accuracy_verifiable":
            denominator = forward & (judgments.label <= 1)
            numerator = forward & (judgments.label == 0)
        else:
            denominator = forward
            numerator = forward & (judgments.label == (0 if metric_id == "accuracy_strict" else 2))
        return _Ratio(
            judgments.table,
            numerator,
            denominator,
            zero_reason,
            lambda rows, _obs: counts(rows, (0, 1, 2)),
        )

    def _spec_recheck(self) -> _Ratio:
        index = self.index
        recheck = index.recheck
        if recheck is None:
            empty = np.zeros(0, dtype=np.bool_)
            table = Table("observation", np.empty(0, dtype=np.object_), np.zeros(0, dtype=np.int64))
            return _Ratio(
                table, empty, empty, _reason("尚未执行抽样复核（缺少 recheck/result.json）")
            )
        row_valid: BoolArray = index.valid[recheck.table.obs]
        unmatched = recheck.unmatched

        def components(rows: BoolArray, obs_scope: BoolArray) -> dict[str, Any]:
            whole_project = bool(obs_scope.all())
            return {
                "items_without_valid_observation": _count(~row_valid & rows)
                + (unmatched if whole_project else 0)
            }

        return _Ratio(
            recheck.table,
            row_valid & recheck.agree,
            row_valid,
            _reason("复核结果中没有对应有效观测的复核项"),
            components,
        )

    def _spec_process(self, metric_id: str) -> _Ratio:
        index = self.index
        obs = index.observations
        valid = index.valid
        universe = index.universe
        if metric_id == "valid_observation_rate":

            def not_valid(rows: BoolArray, _obs: BoolArray) -> dict[str, Any]:
                counts = np.bincount(index.state[universe & rows], minlength=len(index.rules))
                return {
                    "not_valid": [
                        {"rule": rule, "count": int(counts[code])}
                        for code, rule in enumerate(index.rules)
                        if code > 0 and counts[code] > 0
                    ]
                }

            return _Ratio(obs, valid, universe, _reason("没有观测"), not_valid, universe_based=True)
        if metric_id == "raw_record_completeness":
            complete = universe & index.complete
            return _Ratio(
                obs,
                complete,
                universe,
                _reason("没有观测"),
                lambda rows, _obs: {
                    "with_documented_nulls": _count(complete & index.documented_nulls & rows)
                },
                universe_based=True,
            )
        if metric_id == "evidence_completeness":

            def snapshots(rows: BoolArray, _obs: BoolArray) -> dict[str, Any]:
                states = index.snapshot[valid & rows]
                return {
                    "no_snapshot": _count(states == SNAPSHOT_NONE),
                    "missing_file": _count(states == SNAPSHOT_MISSING),
                    "hash_mismatch": _count(states == SNAPSHOT_MISMATCH),
                }

            return _Ratio(
                obs,
                valid & (index.snapshot == SNAPSHOT_OK),
                valid,
                _reason("没有有效观测"),
                snapshots,
            )
        requirements = {
            name: value for name, value in index.project["environment"].items() if value is not None
        }

        def environment(rows: BoolArray, _obs: BoolArray) -> dict[str, Any]:
            return {
                "registered_environment": requirements,
                "unknown_environment": _count(valid & index.env_unknown & rows),
            }

        return _Ratio(
            obs,
            valid & index.env_ok,
            valid,
            _reason("没有有效观测"),
            environment,
            not_applicable=None if requirements else "项目没有登记环境要求，无法评估环境一致率",
        )

    # ------------------------------------------------------------ 意图覆盖率

    def _coverage(self, definition: MetricDefinition, scope: Scope, entity: str) -> Computation:
        index = self.index
        base = self._annotated(entity) & index.scope_mask(*scope.as_tuple())
        observed = set(np.unique(index.query[base]).tolist())
        covered_codes = set(np.unique(index.query[base & index.entity(entity).mentioned]).tolist())
        denominator_ids = list(index.queries)
        numerator_ids = [query for code, query in enumerate(index.queries) if code in covered_codes]
        numerator, denominator = len(numerator_ids), len(denominator_ids)
        by_intent: dict[str, dict[str, int]] = {}
        position = {query: code for code, query in enumerate(index.queries)}
        for prompt in index.prompt_set["prompts"]:
            bucket = by_intent.setdefault(prompt["intent_type"], {"covered": 0, "total": 0})
            bucket["total"] += 1
            bucket["covered"] += position[prompt["query_id"]] in covered_codes
        has_data = bool(base.any())
        result = self._base_result(definition, scope, entity)
        interval, _ = self._interval(
            [int(code in covered_codes) for code in range(denominator)], [1] * denominator
        )
        result.update(
            value=numerator / denominator if has_data else None,
            null_reason=None if has_data else "范围内没有有效观测，无法评估覆盖率",
            numerator=numerator,
            denominator=denominator,
            excluded=self._excluded_rules(
                scope, universe_based=False, unannotated=self._unannotated(scope, entity)
            ),
            interval=interval if has_data else None,
            clustering=None,
            sufficiency=self._sufficiency(np.flatnonzero(base)),
            components={
                "queries_without_valid_observations": denominator - len(observed),
                "by_intent_type": dict(sorted(by_intent.items())),
            },
            drilldown=self._drilldown(definition.unit, numerator_ids, denominator_ids),
            ext={"coverage": self._coverage_info(np.flatnonzero(base))},
        )
        clusters = {
            query: (int(code in covered_codes), 1)
            for code, query in enumerate(index.queries)
            if code in observed
        }
        return Computation(result, numerator_ids, denominator_ids, clusters)

    # ------------------------------------------------------------ 跨时间稳定性

    def _temporal(self, definition: MetricDefinition, scope: Scope, entity: str) -> Computation:
        index = self.index
        base = self._annotated(entity) & index.scope_mask(*scope.as_tuple())
        size = len(index.windows)
        den = np.bincount(index.window[base], minlength=size)
        num = np.bincount(index.window[base & index.entity(entity).mentioned], minlength=size)
        windows: list[dict[str, Any]] = [
            {
                "window_id": index.windows[code],
                "numerator": int(num[code]),
                "denominator": int(den[code]),
                "value": int(num[code]) / int(den[code]),
            }
            for code in range(size)
            if den[code] > 0
        ]
        values: list[float] = [item["value"] for item in windows]
        value: float | None = None
        reason: str | None = None
        components: dict[str, Any] = {"windows": windows}
        if len(values) < 2:
            reason = f"至少需要 2 个有数据的时间窗口（实际 {len(values)} 个）"
        else:
            mean = statistics.fmean(values)
            deviation = statistics.stdev(values)
            components["mean"] = mean
            if definition.metric_id == "temporal_std":
                value = deviation
            elif definition.metric_id == "temporal_range":
                value = max(values) - min(values)
            elif mean == 0:
                reason = "各窗口提及率的均值为 0，变异系数无定义"
            else:
                value = deviation / mean
        window_ids: list[str] = [item["window_id"] for item in windows]
        result = self._base_result(definition, scope, entity)
        result.update(
            value=value,
            null_reason=reason,
            numerator=None,
            denominator=None,
            excluded=self._excluded_rules(
                scope, universe_based=False, unannotated=self._unannotated(scope, entity)
            ),
            interval=None,
            clustering=None,
            sufficiency=self._sufficiency(np.flatnonzero(base)),
            components=components,
            drilldown=self._drilldown(definition.unit, None, window_ids),
            ext={"coverage": self._coverage_info(np.flatnonzero(base))},
        )
        return Computation(result, None, window_ids)

    # ------------------------------------------------------------ 跨平台一致性

    def _cross_platform(
        self, definition: MetricDefinition, scope: Scope, entity: str
    ) -> Computation:
        index = self.index
        obs_scope = index.scope_mask(*scope.as_tuple())
        unannotated = 0
        if definition.metric_id == "cross_platform_recommendation":
            base = self._annotated(entity) & obs_scope
            states = self._platform_states(
                index.query[base], index.platform[base], self._success(entity)[base]
            )
            label_of: Callable[[int], str] = index.queries.__getitem__
            unannotated = self._unannotated(scope, entity)
            used = np.flatnonzero(base)
        elif definition.metric_id == "cross_platform_sources":
            states, used = self._source_states(obs_scope)
            label_of = index.queries.__getitem__
        else:
            states, fact_labels, used = self._fact_states(obs_scope)
            label_of = fact_labels.__getitem__
        items = {item: by_platform for item, by_platform in states.items() if len(by_platform) >= 2}
        denominator_ids = sorted(label_of(item) for item in items)
        numerator_ids = sorted(
            label_of(item)
            for item, by_platform in items.items()
            if len(set(by_platform.values())) == 1
        )
        numerator, denominator = len(numerator_ids), len(denominator_ids)
        per_query: dict[str, list[int]] = {}
        consistent = set(numerator_ids)
        for item_id in denominator_ids:
            bucket = per_query.setdefault(item_id.split("|")[0], [0, 0])
            bucket[0] += item_id in consistent
            bucket[1] += 1
        ordered = sorted(per_query)
        interval, note = self._interval(
            [per_query[key][0] for key in ordered], [per_query[key][1] for key in ordered]
        )
        components: dict[str, Any] = {
            "platforms_with_data": sorted(
                {index.platforms[int(code)] for code in np.unique(index.platform[used])}
            ),
            "rule": definition.limitation,
        }
        if note:
            components["interval_note"] = note
        result = self._base_result(definition, scope, entity)
        result.update(
            value=numerator / denominator if denominator else None,
            null_reason=None
            if denominator
            else "没有同时在至少两个平台有数据的条目，无法评估跨平台一致性，分母为 0",
            numerator=numerator,
            denominator=denominator,
            excluded=self._excluded_rules(scope, universe_based=False, unannotated=unannotated),
            interval=interval,
            clustering=None,
            sufficiency=self._sufficiency(used),
            components=components,
            drilldown=self._drilldown(definition.unit, numerator_ids, denominator_ids),
            ext={"coverage": self._coverage_info(used)},
        )
        clusters = {key: (per_query[key][0], per_query[key][1]) for key in ordered}
        return Computation(result, numerator_ids, denominator_ids, clusters)

    def _platform_states(
        self, items: IntArray, platforms: IntArray, success: BoolArray
    ) -> dict[int, dict[int, Any]]:
        """每个条目在每个平台上的多数结论：成功比例 ≥ 0.5 记为 True（用整数比较，避免浮点）。"""
        width = len(self.index.platforms)
        keys, inverse = np.unique(items * width + platforms, return_inverse=True)
        totals = np.bincount(inverse, minlength=keys.size)
        hits = np.bincount(inverse[success], minlength=keys.size)
        states: dict[int, dict[int, Any]] = {}
        for key, total, hit in zip(keys.tolist(), totals.tolist(), hits.tolist(), strict=True):
            states.setdefault(key // width, {})[key % width] = 2 * hit >= total
        return states

    def _source_states(self, obs_scope: BoolArray) -> tuple[dict[int, dict[int, Any]], IntArray]:
        """每道题在每个平台上的首要引用域名（被最多观测引用；并列取域名字典序最小者）。"""
        index = self.index
        citations = index.citations
        rows = (index.valid & obs_scope)[citations.table.obs]
        obs = citations.table.obs[rows]
        domains = len(citations.domains) or 1
        pairs = np.unique(obs * domains + citations.domain[rows])
        pair_obs, pair_domain = pairs // domains, pairs % domains
        width = len(index.platforms)
        group = index.query[pair_obs] * width + index.platform[pair_obs]
        keys, counts = np.unique(group * domains + pair_domain, return_counts=True)
        best: dict[int, tuple[int, int]] = {}
        for key, count in zip(keys.tolist(), counts.tolist(), strict=True):
            cell, domain = key // domains, key % domains
            current = best.get(cell)
            if current is None or count > current[0]:
                best[cell] = (count, domain)
        states: dict[int, dict[int, Any]] = {}
        for cell, (_, domain) in best.items():
            states.setdefault(cell // width, {})[cell % width] = domain
        return states, np.unique(pair_obs)

    def _fact_states(
        self, obs_scope: BoolArray
    ) -> tuple[dict[int, dict[int, Any]], dict[int, str], IntArray]:
        """每个（题目，核心事实）在每个平台上是否“正确呈现”。"""
        index = self.index
        judgments = index.judgments
        rows = (index.valid & obs_scope)[judgments.table.obs] & (judgments.direction == 1)
        obs = judgments.table.obs[rows]
        facts = len(judgments.facts) or 1
        item = index.query[obs] * facts + judgments.fact[rows]
        states = self._platform_states(item, index.platform[obs], judgments.label[rows] == 3)
        queries = [*index.queries, UNKNOWN_QUERY_LABEL]
        labels = {key: f"{queries[key // facts]}|{judgments.facts[key % facts]}" for key in states}
        return states, labels, np.unique(obs)

    # ------------------------------------------------------------ 下钻记录

    def records(self, computation: Computation) -> list[dict[str, Any]]:
        """下钻用的逐条记录：编号、所属观测、题目、平台、通道、窗口、日期、是否在分子中。"""
        ids = computation.denominator_ids or []
        in_numerator = set(computation.numerator_ids or [])
        table, rows = computation.table, computation.denominator_rows
        if table is None or rows is None:
            return [{"id": item, "in_numerator": item in in_numerator} for item in ids]
        index = self.index
        queries = [*index.queries, UNKNOWN_QUERY_LABEL]
        records: list[dict[str, Any]] = []
        for item, position in zip(ids, table.obs[rows].tolist(), strict=True):
            records.append(
                {
                    "id": item,
                    "observation_id": index.observations.ids[position],
                    "query_id": queries[int(index.query[position])],
                    "platform": index.platforms[int(index.platform[position])],
                    "channel": index.channels[int(index.channel[position])],
                    "window_id": index.windows[int(index.window[position])],
                    "date": index.dates[int(index.date[position])],
                    "started_at": index.started[position],
                    "in_numerator": item in in_numerator,
                }
            )
        return records


def compute(
    index: ProjectIndex,
    metric_id: str,
    scope: Scope | None = None,
    params: Parameters | None = None,
    entity_id: str | None = None,
) -> Computation:
    """单次计算的便捷入口。"""
    engine = Engine(index, params or parameters_for(index.project))
    return engine.compute(metric_id, scope, entity_id)
