"""组装 out/metrics.json：全部指标 × 默认范围，加上提示和剔除审计。

文档不含生成时间和路径：同一份证据在任何目录计算，文件逐字节相同。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from geo_verify.metrics.definitions import KIND_CROSS, METRIC_IDS, METRICS
from geo_verify.metrics.engine import SCHEMA_VERSION, Engine, Parameters, Scope, _notice
from geo_verify.metrics.exclusion import exclusion_audit
from geo_verify.metrics.index import RULE_VERSION_MISMATCH, ProjectIndex


def _labels_in_universe(index: ProjectIndex, codes: Any, labels: list[str]) -> list[str]:
    return [labels[int(code)] for code in np.unique(codes[index.universe])]


def default_scopes(index: ProjectIndex, metric_id: str) -> list[Scope]:
    """每个指标默认输出的范围：整体 + 每个平台；提及率另按窗口、按通道；推荐达成率按日。"""
    project = index.project
    platforms = sorted(
        {item["platform_id"] for item in project["platforms"]}
        | set(_labels_in_universe(index, index.platform, index.platforms))
    )
    if METRICS[metric_id].kind == KIND_CROSS:
        return [Scope()]
    if metric_id == "recommendation_rate_daily":
        dates = _labels_in_universe(index, index.date, index.dates)
        return [
            Scope(platform=platform, date=date) for date in dates for platform in (None, *platforms)
        ]
    scopes = [Scope(), *(Scope(platform=platform) for platform in platforms)]
    if metric_id == "mention_rate":
        windows = _labels_in_universe(index, index.window, index.windows)
        channels = sorted(
            {channel for item in project["platforms"] for channel in item["channels"]}
            | set(_labels_in_universe(index, index.channel, index.channels))
        )
        scopes += [Scope(window_id=window) for window in windows]
        scopes += [Scope(channel=channel) for channel in channels]
    return scopes


def index_notices(index: ProjectIndex, params: Parameters) -> list[dict[str, Any]]:
    """与具体指标无关、但会影响结果解读的提示。"""
    notices: list[dict[str, Any]] = []
    selected = index.prompt_set
    by_hash = {item["content_hash"]: item["version"] for item in index.sealed}
    others = {
        content_hash: count
        for content_hash, count in index.version_counts.items()
        if content_hash != selected["content_hash"]
    }
    if others:
        versions = [
            {
                "version": by_hash.get(content_hash),
                "content_hash": content_hash,
                "observations": count,
            }
            for content_hash, count in index.version_counts.items()
        ]
        versions.sort(
            key=lambda item: (item["version"] is None, item["version"] or 0, item["content_hash"])
        )
        notices.append(
            _notice(
                RULE_VERSION_MISMATCH,
                "warning",
                f"有 {sum(others.values())} 条观测不属于所选的第 {selected['version']} 版封存题库："
                "不同版本的题库禁止混算，这些观测只计数、不参与任何指标。",
                selected_version=selected["version"],
                versions=versions,
            )
        )
    for (platform, channel), versions_seen in sorted(index.model_versions.items()):
        if len(versions_seen) < 2:
            continue
        listed = sorted(
            (
                {
                    "model_version": version,
                    "observations": stats[0],
                    "first_seen": stats[1],
                    "last_seen": stats[2],
                }
                for version, stats in versions_seen.items()
            ),
            key=lambda item: (item["first_seen"], item["model_version"]),
        )
        notices.append(
            _notice(
                "model_version_changed",
                "warning",
                f"平台 {platform}（通道 {channel}）在观测期内出现 {len(listed)} 个模型或产品版本："
                "版本变化前后的数据不得合并比较，须重建基线。",
                platform=platform,
                channel=channel,
                versions=listed,
            )
        )
    registered = index.project["preregistration"]["statistics"]
    if (params.seed, params.iterations) != (registered["seed"], registered["bootstrap_iterations"]):
        notices.append(
            _notice(
                "parameters_overridden",
                "warning",
                "本次计算的 seed 或重抽次数与预先登记值不同，区间仅供参考。",
                registered={
                    "seed": registered["seed"],
                    "iterations": registered["bootstrap_iterations"],
                },
                used={"seed": params.seed, "iterations": params.iterations},
            )
        )
    counters = index.counters
    unknown_queries = int(np.count_nonzero(index.universe & (index.query == len(index.queries))))
    registered_platforms = {item["platform_id"] for item in index.project["platforms"]}
    unregistered = sorted(
        set(_labels_in_universe(index, index.platform, index.platforms)) - registered_platforms
    )
    simple: list[tuple[str, int, str]] = [
        (
            "unannotated_observations",
            counters["unannotated_observations"],
            "条有效观测缺少目标主体的标注：依赖标注的指标不把它们计入分母，请先完成标注。",
        ),
        (
            "duplicate_observation_ids",
            counters["duplicate_observation_ids"],
            "条观测的 observation_id 与更早的记录重复：只保留第一条，其余只计数。",
        ),
        (
            "unknown_query_ids",
            unknown_queries,
            "条观测的题目不在所选封存题库中：只计数，不参与任何指标。",
        ),
        (
            "judgments_without_observation",
            counters["judgments_without_observation"],
            "条判定指向不存在的观测：未计入准确率和呈现率。",
        ),
        (
            "duplicate_judgment_ids",
            counters["duplicate_judgment_ids"],
            "条判定的 judgment_id 重复：只保留第一条。",
        ),
        (
            "annotations_without_observation",
            counters["annotations_without_observation"],
            "条生效标注指向不存在的观测：已忽略。",
        ),
    ]
    for code, count, text in simple:
        if count:
            notices.append(_notice(code, "warning", f"有 {count} {text}", count=count))
    if unregistered:
        notices.append(
            _notice(
                "unregistered_platforms",
                "warning",
                f"观测中出现了项目未登记的平台：{'、'.join(unregistered)}。",
                platforms=unregistered,
            )
        )
    return notices


def _audit_notices(audit: dict[str, Any]) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    if audit["unregistered_rules"]:
        notices.append(
            _notice(
                "unregistered_exclusion_rules",
                "error",
                "使用了未预先登记的剔除规则："
                + "、".join(audit["unregistered_rules"])
                + "。剔除规则必须在封存前登记。",
                rules=audit["unregistered_rules"],
            )
        )
    selective = [item["metric_id"] for item in audit["comparisons"] if item["selective"]]
    if selective:
        notices.append(
            _notice(
                "selective_exclusion",
                "error",
                "被剔除的样本明显偏向不利样本（指标："
                + "、".join(selective)
                + "）：剔除抬高了结果，详见 exclusion_audit。",
                metrics=selective,
            )
        )
    return notices


def compute_document(index: ProjectIndex, params: Parameters) -> dict[str, Any]:
    """计算全部指标，返回 out/metrics.json 的内容。"""
    engine = Engine(index, params)
    results = [
        engine.compute(metric_id, scope).result
        for metric_id in METRIC_IDS
        for scope in default_scopes(index, metric_id)
    ]
    audit = exclusion_audit(index, engine, params)
    project = index.project
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "metrics",
        "project_id": project["project_id"],
        "target_entity_id": index.target_id,
        "timezone": project["timezone"],
        "prompt_set": {
            "version": index.prompt_set["version"],
            "content_hash": index.prompt_set["content_hash"],
        },
        "parameters": {
            "seed": params.seed,
            "iterations": params.iterations,
            "confidence_level": params.level,
        },
        "sufficiency_thresholds": {
            "min_windows": params.min_windows,
            "min_valid_observations": params.min_valid_observations,
        },
        "observations": {
            "total": index.size,
            "in_selected_version": int(np.count_nonzero(index.universe)),
            "valid": int(np.count_nonzero(index.valid)),
        },
        "registered_metrics": engine.registered,
        "notices": [
            *engine.registration_notices,
            *index_notices(index, params),
            *_audit_notices(audit),
        ],
        "exclusion_audit": audit,
        "results": results,
    }
    if project.get("synthetic") is True:
        document["synthetic"] = True
    return document
