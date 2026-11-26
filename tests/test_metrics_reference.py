"""指标口径的独立对照（随机输入）。

对照实现只读原始记录（观测、标注、判定、剔除清单），用最直白的逐条循环重算每个范围的
分子、分母和剔除数，再与索引 + 引擎的向量化结果逐项比对。两边不共用任何计算代码：
这里不调用 geo_verify.metrics 的任何函数来得到期望值，时区换算也自己做（上海时间 = UTC+8）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from factory import make_citation, make_prompt, seal_prompts
from helpers_metrics import Builder, find
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from geo_verify import metrics

QUERIES = ("Q001", "Q002", "Q003", "Q004")
EXPECTS = {"Q001": True, "Q002": True, "Q003": False, "Q004": True}
PLATFORMS = ("engine-a", "engine-b")
WINDOWS = ("W1", "W2", "W3")
CHANNELS = ("api", "manual")
TARGET_DOMAIN = "lanxu-auto.example"
DOMAINS = (TARGET_DOMAIN, "www.lanxu-auto.example", "gov-data.example", "other-media.example")
AUTHORITY = {"gov-data.example"}  # 样例项目登记：gov-data 为 A 级（权威），目标官网为 C 级
FORWARD = ("supported", "contradicted", "unverifiable")
BACKWARD = ("present_correct", "present_wrong", "missing")

annotation_strategy = st.one_of(
    st.none(),
    st.fixed_dictionaries(
        {
            "mentioned": st.booleans(),
            "negated": st.booleans(),
            "recommended": st.booleans(),
            "rankable": st.booleans(),
            "rank": st.one_of(st.none(), st.integers(1, 7)),
            "sentiment": st.sampled_from(["positive", "neutral", "negative", None]),
            "unfounded": st.booleans(),
        }
    ),
)
observation_strategy = st.fixed_dictionaries(
    {
        "query": st.sampled_from((*QUERIES, "Q001", "Q002", "Q900")),
        "platform": st.sampled_from(PLATFORMS),
        "window": st.sampled_from(WINDOWS),
        "channel": st.sampled_from(CHANNELS),
        "valid": st.sampled_from((True, True, True, False)),
        "day": st.integers(8, 10),
        "hour": st.integers(0, 23),
        "old_version": st.sampled_from((False, False, False, False, True)),
        "excluded": st.sampled_from((False, False, False, False, True)),
        "annotation": annotation_strategy,
        # 引用：(域名下标或 None, 页码)；同一域名同一页码就是同一个 URL（重复引用）
        "citations": st.lists(
            st.tuples(st.one_of(st.none(), st.integers(0, len(DOMAINS) - 1)), st.integers(1, 2)),
            max_size=4,
        ),
        "judgments": st.lists(st.sampled_from(FORWARD + BACKWARD), max_size=4),
    }
)


def _build(root: Path, drawn: list[dict[str, Any]]) -> dict[str, Any]:
    """把抽到的规格写成项目目录，同时返回对照实现要用的原始记录。"""
    b = Builder(root, queries=QUERIES[:3])
    current = seal_prompts(
        b.root,
        b.project,
        [make_prompt(query, expects_recommendation=EXPECTS[query]) for query in QUERIES],
        previous=b.sealed,
        sealed_at="2026-10-05T03:00:00Z",
    )
    excluded: list[dict[str, Any]] = []
    for item in drawn:
        citations = []
        for order, (domain_index, page) in enumerate(item["citations"], start=1):
            citation = make_citation(order, "placeholder.example")
            if domain_index is None:
                citation.update(domain=None, url=f"unresolvable-reference-{page}")
            else:
                domain = DOMAINS[domain_index]
                citation.update(domain=domain, url=f"https://{domain}/page-{page}")
            citations.append(citation)
        observation = b.obs(
            item["query"],
            item["platform"],
            item["window"],
            mentioned=None,
            prompt_set=b.sealed if item["old_version"] else current,
            channel=item["channel"],
            status="valid" if item["valid"] else "invalid",
            invalid_reason="timeout",
            started_at=f"2026-10-{item['day']:02d}T{item['hour']:02d}:30:00Z",
            citations=citations,
        )
        spec = item["annotation"]
        if spec is not None:
            mentioned = spec["mentioned"]
            negated = mentioned and spec["negated"]
            rankable = spec["rankable"]
            b.annotate(
                observation,
                mentioned=mentioned,
                negated=negated,
                recommended=mentioned and not negated and spec["recommended"],
                rankable=rankable,
                rank=spec["rank"] if mentioned and rankable else None,
                sentiment=spec["sentiment"],
                risk={"unfounded_negative": spec["unfounded"]},
            )
        for label in item["judgments"]:
            b.judge(observation, label)
        if item["excluded"]:
            excluded.append(observation)
    b.write()
    if excluded:
        b.exclude(excluded)
    return {
        "root": b.root,
        "observations": b.observations,
        "annotations": {a["observation_id"]: a for a in b.annotations},
        "judgments": b.judgments,
        "excluded": {o["observation_id"] for o in excluded},
        "selected_hash": current["content_hash"],
    }


def _local_date(started_at: str) -> str:
    moment = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=8)
    return moment.date().isoformat()


def _is_target(domain: str) -> bool:
    return domain == TARGET_DOMAIN or domain.endswith("." + TARGET_DOMAIN)


class Reference:
    """逐条循环的对照实现。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.annotations: dict[str, dict[str, Any]] = data["annotations"]

    def in_scope(self, observation: dict[str, Any], scope: dict[str, Any]) -> bool:
        return (
            scope["platform"] in (None, observation["platform"])
            and scope["channel"] in (None, observation["channel"])
            and scope["window_id"] in (None, observation["window_id"])
            and scope["date"] in (None, _local_date(observation["started_at"]))
        )

    def in_universe(self, observation: dict[str, Any]) -> bool:
        return bool(observation["prompt_set_hash"] == self.data["selected_hash"])

    def is_valid(self, observation: dict[str, Any]) -> bool:
        return (
            self.in_universe(observation)
            and observation["observation_id"] not in self.data["excluded"]
            and observation["query_id"] in QUERIES
            and observation["status"] == "valid"
        )

    def identifiable_citations(self, observation: dict[str, Any]) -> list[str]:
        """按 URL 去重后、域名可识别的引用的域名列表。"""
        seen: set[str] = set()
        domains: list[str] = []
        for citation in observation["citations"]:
            if citation["url"] in seen:
                continue
            seen.add(citation["url"])
            if citation["domain"] is not None:
                domains.append(citation["domain"])
        return domains

    def counts(self, metric_id: str, scope: dict[str, Any]) -> tuple[int, int, int]:
        """返回 (分子, 分母, 剔除数)。"""
        scoped = [o for o in self.data["observations"] if self.in_scope(o, scope)]
        valid = [o for o in scoped if self.is_valid(o)]
        annotated = [o for o in valid if o["observation_id"] in self.annotations]
        not_valid = len(scoped) - len(valid)
        with_unannotated = not_valid + len(valid) - len(annotated)

        def ann(observation: dict[str, Any]) -> dict[str, Any]:
            return self.annotations[observation["observation_id"]]

        if metric_id == "mention_rate":
            return sum(ann(o)["mentioned"] for o in annotated), len(annotated), with_unannotated
        if metric_id in ("top1_rate", "top3_rate", "top5_rate"):
            limit = int(metric_id[3])
            rankable = [o for o in annotated if ann(o)["rankable"]]
            hits = [o for o in rankable if ann(o)["rank"] is not None and ann(o)["rank"] <= limit]
            return len(hits), len(rankable), with_unannotated
        if metric_id in ("recommendation_rate", "recommendation_rate_daily"):
            expected = [o for o in annotated if EXPECTS[o["query_id"]]]
            hits = [o for o in expected if ann(o)["recommended"] is True]
            return len(hits), len(expected), with_unannotated
        if metric_id.startswith("sentiment_"):
            wanted = metric_id.split("_")[1]
            base = [o for o in annotated if ann(o)["mentioned"] and ann(o)["sentiment"] is not None]
            return sum(ann(o)["sentiment"] == wanted for o in base), len(base), with_unannotated
        if metric_id == "negative_miscitation_rate":
            hits = [o for o in annotated if ann(o)["risk"]["unfounded_negative"] is True]
            return len(hits), len(annotated), with_unannotated
        if metric_id == "target_source_presence":
            hits = [o for o in valid if any(_is_target(d) for d in self.identifiable_citations(o))]
            return len(hits), len(valid), not_valid
        if metric_id in ("target_source_share", "authority_share"):
            domains = [d for o in valid for d in self.identifiable_citations(o)]
            if metric_id == "target_source_share":
                return sum(_is_target(d) for d in domains), len(domains), not_valid
            return sum(d in AUTHORITY for d in domains), len(domains), not_valid
        if metric_id == "valid_observation_rate":
            universe = [o for o in scoped if self.in_universe(o)]
            return len(valid), len(universe), len(scoped) - len(universe)
        valid_ids = {o["observation_id"] for o in valid}
        labels = [j["label"] for j in self.data["judgments"] if j["source"]["id"] in valid_ids]
        supported = labels.count("supported")
        contradicted = labels.count("contradicted")
        unverifiable = labels.count("unverifiable")
        if metric_id == "accuracy_verifiable":
            return supported, supported + contradicted, not_valid
        if metric_id == "accuracy_strict":
            return supported, supported + contradicted + unverifiable, not_valid
        if metric_id == "unverifiable_rate":
            return unverifiable, supported + contradicted + unverifiable, not_valid
        assert metric_id == "core_fact_presence"
        backward = sum(labels.count(label) for label in BACKWARD)
        return labels.count("present_correct"), backward, not_valid

    def per_query(self, scope: dict[str, Any]) -> dict[str, tuple[int, int]]:
        """提及率在各题上的（分子, 分母）。"""
        clusters: dict[str, list[int]] = {}
        for observation in self.data["observations"]:
            annotation = self.annotations.get(observation["observation_id"])
            if annotation is None or not self.is_valid(observation):
                continue
            if not self.in_scope(observation, scope):
                continue
            bucket = clusters.setdefault(observation["query_id"], [0, 0])
            bucket[0] += annotation["mentioned"]
            bucket[1] += 1
        return {query: (bucket[0], bucket[1]) for query, bucket in clusters.items()}


COMPARED = (
    "mention_rate",
    "top1_rate",
    "top3_rate",
    "top5_rate",
    "recommendation_rate",
    "recommendation_rate_daily",
    "target_source_presence",
    "target_source_share",
    "authority_share",
    "accuracy_verifiable",
    "accuracy_strict",
    "unverifiable_rate",
    "core_fact_presence",
    "negative_miscitation_rate",
    "sentiment_positive_share",
    "sentiment_neutral_share",
    "sentiment_negative_share",
    "valid_observation_rate",
)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(drawn=st.lists(observation_strategy, max_size=24))
def test_vectorized_counts_match_naive_reference(
    tmp_path_factory: pytest.TempPathFactory, drawn: list[dict[str, Any]]
) -> None:
    data = _build(tmp_path_factory.mktemp("reference") / "proj", drawn)
    reference = Reference(data)
    index = metrics.build_index(data["root"])
    params = metrics.parameters_for(index.project, iterations=100)
    document = metrics.compute_document(index, params)

    observations = data["observations"]
    assert document["observations"] == {
        "total": len(observations),
        "in_selected_version": sum(reference.in_universe(o) for o in observations),
        "valid": sum(reference.is_valid(o) for o in observations),
    }
    compared = 0
    for result in document["results"]:
        if result["metric_id"] not in COMPARED:
            continue
        scope = result["scope"]
        numerator, denominator, excluded = reference.counts(result["metric_id"], scope)
        where = (result["metric_id"], scope)
        assert (result["numerator"], result["denominator"]) == (numerator, denominator), where
        assert result["excluded"]["count"] == excluded, where
        assert sum(rule["count"] for rule in result["excluded"]["rules"]) == excluded, where
        if denominator == 0:
            assert result["value"] is None, where
            assert result["null_reason"], where
            assert result["interval"] is None, where
        else:
            assert result["value"] == pytest.approx(numerator / denominator, abs=1e-12), where
            assert result["interval"]["low"] <= result["interval"]["high"], where
        compared += 1
    # 除按日指标外，每个指标至少比对了整体和两个登记平台三个范围
    assert compared >= (len(COMPARED) - 1) * 3

    # 按题聚合的分子分母（区间与配对比较的输入）
    engine = metrics.Engine(index, params)
    for scope in (
        metrics.Scope(),
        metrics.Scope(platform="engine-a"),
        metrics.Scope(channel="api"),
    ):
        computation = engine.compute("mention_rate", scope)
        assert computation.clusters == reference.per_query(scope.to_dict(None))

    # 按日的结果恰好覆盖所选版本内出现过的每个自然日，分母加起来等于整体
    dates = {_local_date(o["started_at"]) for o in observations if reference.in_universe(o)}
    daily = [
        r
        for r in document["results"]
        if r["metric_id"] == "recommendation_rate_daily" and r["scope"]["platform"] is None
    ]
    assert {r["scope"]["date"] for r in daily} == dates
    overall = find(document["results"], "recommendation_rate")
    assert sum(r["denominator"] for r in daily) == overall["denominator"]
    assert sum(r["numerator"] for r in daily) == overall["numerator"]
