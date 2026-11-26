"""指标的边界与对抗输入：引用核验状态、无可核验陈述、跨平台多数规则、时区边界、
复核结果的未复核项、跨进程复现，以及不合契约的输入。期望值均为注释中的手工计数。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli_json
from factory import COMPETITOR, TARGET, make_citation
from helpers_metrics import Builder, copy_project, find, metrics_file, utc
from test_metrics_cli import build_rich

from geo_verify import metrics
from geo_verify.errors import InputError
from geo_verify.jsonl import write_json


def run_metrics(root: Path, *extra: str) -> dict[str, Any]:
    code, data = run_cli_json("metrics", str(root), *extra)
    assert code == 0, data
    return metrics_file(root)


def assert_ratio(result: dict[str, Any], numerator: int, denominator: int) -> None:
    assert (result["numerator"], result["denominator"]) == (numerator, denominator)
    assert result["value"] == pytest.approx(numerator / denominator, abs=1e-12)
    assert result["null_reason"] is None


# ---------------------------------------------------------------- 引用核验


def test_citation_check_states(tmp_path: Path) -> None:
    """一条观测的 7 条引用（第 7 条与第 6 条是同一个 URL）：

    #1 目标官网    核验：有效且相关            → 分子、分母
    #2 gov-data    核验：无效（打不开）        → 只进分母
    #3 other-media 核验项存在但都是 null       → 未核验
    #4 auto-review 核验：有效，相关性未评估    → 未核验
    #5 ev-forum    核验：有效但不相关          → 只进分母
    #6 news-auto   没有针对 #6 的核验
    #7 与 #6 同 URL，核验：有效且相关          → 记到去重后保留的 #6 上：分子、分母
    另有指向不存在的第 99 条引用的核验（忽略），以及竞品标注里的核验（不用于目标主体）。
    """
    b = Builder(tmp_path / "proj")
    domains = [
        "lanxu-auto.example",
        "gov-data.example",
        "other-media.example",
        "auto-review.example",
        "ev-forum.example",
        "news-auto.example",
    ]
    citations = [make_citation(order, domain) for order, domain in enumerate(domains, start=1)]
    repeated = make_citation(7, "news-auto.example")
    repeated["url"] = citations[5]["url"]
    observation = b.obs("Q001", mentioned=None, citations=[*citations, repeated])
    b.annotate(
        observation,
        citation_checks=[
            {"order": 1, "valid": True, "relevant": True},
            {"order": 2, "valid": False, "relevant": None},
            {"order": 3, "valid": None, "relevant": None},
            {"order": 4, "valid": True, "relevant": None},
            {"order": 5, "valid": True, "relevant": False},
            {"order": 7, "valid": True, "relevant": True},
            {"order": 99, "valid": True, "relevant": True},
        ],
    )
    b.annotate(
        observation,
        entity_id=COMPETITOR,
        mentioned=False,
        sentiment=None,
        citation_checks=[
            {"order": 3, "valid": True, "relevant": True},
            {"order": 6, "valid": False, "relevant": False},
        ],
    )
    b.write()
    validity = find(run_metrics(b.root)["results"], "citation_validity")
    assert_ratio(validity, 2, 4)
    assert validity["drilldown"]["numerator_ids"] == ["obs-000001#1", "obs-000001#6"]
    assert validity["drilldown"]["denominator_ids"] == [
        "obs-000001#1",
        "obs-000001#2",
        "obs-000001#5",
        "obs-000001#6",
    ]
    assert validity["components"]["identifiable_citations"] == 6
    assert validity["components"]["verified_citations"] == 4
    assert validity["components"]["unverified_citations"] == 2
    assert "尚未核验" in validity["components"]["note"]


# ---------------------------------------------------------------- 准确率


def test_accuracy_verifiable_is_null_when_nothing_is_verifiable(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    observation = b.obs("Q001")
    b.judge(observation, "unverifiable")
    b.judge(observation, "unverifiable")
    b.write()
    results = run_metrics(b.root)["results"]
    verifiable = find(results, "accuracy_verifiable")
    # 支持 0、矛盾 0：可核验口径的分母为 0，不能说成 0% 或 100%
    assert verifiable["value"] is None
    assert (verifiable["numerator"], verifiable["denominator"]) == (0, 0)
    assert "没有可核验的陈述" in verifiable["null_reason"]
    assert verifiable["interval"] is None
    assert verifiable["components"]["unverifiable"] == 2
    # 严格口径的分母是全部陈述（2 条）：这里的 0 是真实的 0，不是“无数据”
    strict = find(results, "accuracy_strict")
    assert (strict["numerator"], strict["denominator"], strict["value"]) == (0, 2, 0.0)
    assert strict["null_reason"] is None
    assert_ratio(find(results, "unverifiable_rate"), 2, 2)
    # 没有“事实库 → 回答”方向的判定：呈现率不能借用另一个方向的分母
    presence = find(results, "core_fact_presence")
    assert presence["value"] is None
    assert presence["denominator"] == 0


# ---------------------------------------------------------------- 跨平台多数规则


def test_cross_platform_majority_threshold(tmp_path: Path) -> None:
    """平台内比例恰为 0.5 记为“是”，低于 0.5 记为“否”。

    推荐逻辑：
      Q001：a 推荐 1/3（否），b 推荐 0/2（否）      → 一致
      Q002：a 推荐 1/2（是，恰为 0.5），b 推荐 2/2（是） → 一致
      Q003：a 推荐 1/3（否），b 推荐 2/3（是）      → 不一致
    核心事实：
      (Q001, F-A)：a 正确 1/3（否），b 正确 0/1（否） → 一致
      (Q002, F-A)：a 正确 1/2（是），b 正确 1/1（是） → 一致
      (Q003, F-A)：a 正确 1/3（否），b 正确 2/3（是） → 不一致
    """
    b = Builder(tmp_path / "proj")
    plan = {
        ("Q001", "engine-a"): [True, False, False],
        ("Q001", "engine-b"): [False, False],
        ("Q002", "engine-a"): [True, False],
        ("Q002", "engine-b"): [True, True],
        ("Q003", "engine-a"): [True, False, False],
        ("Q003", "engine-b"): [True, True, False],
    }
    facts = {
        ("Q001", "engine-a"): ["present_correct", "missing", "present_wrong"],
        ("Q001", "engine-b"): ["missing"],
        ("Q002", "engine-a"): ["present_correct", "missing"],
        ("Q002", "engine-b"): ["present_correct"],
        ("Q003", "engine-a"): ["present_correct", "missing", "missing"],
        ("Q003", "engine-b"): ["present_correct", "present_correct", "present_wrong"],
    }
    for (query, platform), flags in plan.items():
        labels = facts[(query, platform)]
        for position, flag in enumerate(flags):
            observation = b.obs(query, platform, mentioned=flag)
            if position < len(labels):
                b.judge(observation, labels[position], fact_id="F-A")
    b.write()
    results = run_metrics(b.root)["results"]
    recommendation = find(results, "cross_platform_recommendation")
    assert_ratio(recommendation, 2, 3)
    assert recommendation["drilldown"]["numerator_ids"] == ["Q001", "Q002"]
    core_facts = find(results, "cross_platform_core_facts")
    assert_ratio(core_facts, 2, 3)
    assert core_facts["drilldown"]["numerator_ids"] == ["Q001|F-A", "Q002|F-A"]


def test_cross_platform_sources_tie_and_majority(tmp_path: Path) -> None:
    """首要引用域名 = 平台内被最多观测引用的域名；并列时取字典序最小者。

    Q001：a 两条观测分别引用 gov-data、other-media（并列 → gov-data）；
          b 三条观测中 other-media 2 次、gov-data 1 次（→ other-media） → 不一致
    Q002：a 引用 other-media 1 次；
          b 有一条观测引用了 gov-data 的两个不同页面（按观测计，gov-data 只算 1 次），
          另两条观测各引用 other-media 一次（2 次）→ other-media → 一致。
          如果按引用条数而不是按观测计数，gov-data 与 other-media 会并列并误判为 gov-data。
    """
    b = Builder(tmp_path / "proj")
    gov = make_citation(1, "gov-data.example")
    gov_other_page = make_citation(2, "gov-data.example")
    other = make_citation(1, "other-media.example")
    b.obs("Q001", "engine-a", citations=[gov])
    b.obs("Q001", "engine-a", citations=[other])
    b.obs("Q001", "engine-b", citations=[other])
    b.obs("Q001", "engine-b", citations=[other])
    b.obs("Q001", "engine-b", citations=[gov])
    b.obs("Q002", "engine-a", citations=[other])
    b.obs("Q002", "engine-b", citations=[gov, gov_other_page])
    b.obs("Q002", "engine-b", citations=[other])
    b.obs("Q002", "engine-b", citations=[other])
    b.write()
    sources = find(run_metrics(b.root)["results"], "cross_platform_sources")
    assert_ratio(sources, 1, 2)
    assert sources["drilldown"]["numerator_ids"] == ["Q002"]
    assert sources["drilldown"]["denominator_ids"] == ["Q001", "Q002"]


# ---------------------------------------------------------------- 时区边界（按日对账）


@pytest.mark.parametrize(
    ("timezone", "before", "after", "day_before", "day_after"),
    [
        # 洛杉矶 10 月是夏令时（UTC−7）：06:59:59Z 还是前一天 23:59:59，07:00:00Z 进入当天
        ("America/Los_Angeles", utc(9, 6, 59, 59), utc(9, 7, 0, 0), "2026-10-08", "2026-10-09"),
        # 加尔各答 UTC+5:30：18:29:59Z 是当天 23:59:59，18:30:00Z 已是次日
        ("Asia/Kolkata", utc(8, 18, 29, 59), utc(8, 18, 30, 0), "2026-10-08", "2026-10-09"),
        # 洛杉矶 2026-11-01 结束夏令时（之后 UTC−8）：11-02 07:59:59Z 仍是 11-01，08:00:00Z 才是 11-02
        (
            "America/Los_Angeles",
            utc(2, 7, 59, 59, month=11),
            utc(2, 8, 0, 0, month=11),
            "2026-11-01",
            "2026-11-02",
        ),
        # 上海 UTC+8，跨月：10-31 15:59:59Z 是 10-31 23:59:59，16:00:00Z 是 11-01
        ("Asia/Shanghai", utc(31, 15, 59, 59), utc(31, 16, 0, 0), "2026-10-31", "2026-11-01"),
    ],
)
def test_daily_date_boundaries_follow_project_timezone(
    tmp_path: Path, timezone: str, before: str, after: str, day_before: str, day_after: str
) -> None:
    b = Builder(tmp_path / "proj", timezone=timezone)
    b.obs("Q001", started_at=before, mentioned=True)
    b.obs("Q001", started_at=after, mentioned=False)
    b.write()
    data = run_metrics(b.root)
    assert data["timezone"] == timezone
    first = find(data["results"], "recommendation_rate_daily", date=day_before)
    assert_ratio(first, 1, 1)
    assert first["drilldown"]["denominator_ids"] == ["obs-000001"]
    second = find(data["results"], "recommendation_rate_daily", date=day_after)
    assert (second["numerator"], second["denominator"], second["value"]) == (0, 1, 0.0)
    assert second["drilldown"]["denominator_ids"] == ["obs-000002"]
    assert second["drilldown"]["numerator_ids"] == []
    dates = {
        r["scope"]["date"] for r in data["results"] if r["metric_id"] == "recommendation_rate_daily"
    }
    assert dates == {day_before, day_after}
    # 下钻记录里的日期与结果范围一致
    code, drill = run_cli_json(
        "metrics", str(b.root), "--drilldown", "recommendation_rate_daily", "--date", day_before
    )
    assert code == 0
    assert [(r["id"], r["date"], r["started_at"]) for r in drill["records"]] == [
        ("obs-000001", day_before, before)
    ]


def test_daily_metric_requires_a_date(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001")
    b.write()
    code, data = run_cli_json("metrics", str(b.root), "--drilldown", "recommendation_rate_daily")
    assert code == 2
    assert "--date" in data["message"]
    # 无效观测也有日期：那一天的结果列出剔除数，分母为 0 时给 null
    invalid_only = Builder(tmp_path / "invalid")
    invalid_only.obs("Q001", status="invalid", invalid_reason="empty_answer", started_at=utc(8))
    invalid_only.write()
    day = find(
        run_metrics(invalid_only.root)["results"], "recommendation_rate_daily", date="2026-10-08"
    )
    assert day["value"] is None
    assert "分母为 0" in day["null_reason"]
    assert day["excluded"] == {
        "count": 1,
        "rules": [{"rule": "status=invalid:empty_answer", "count": 1}],
    }


# ---------------------------------------------------------------- 抽样复核结果


def recheck_file(root: Path, items: list[dict[str, Any]]) -> None:
    write_json(
        root / "recheck" / "result.json",
        {
            "schema_version": "1.0",
            "kind": "recheck_result",
            "project_id": "auto-demo",
            "items": items,
        },
    )


def test_recheck_items_not_yet_checked_are_not_counted_as_disagreement(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    observations = [b.obs("Q001"), b.obs("Q002"), b.obs("Q003")]
    b.write()
    recheck_file(
        b.root,
        [
            {
                "observation_id": observations[0]["observation_id"],
                "entity_id": TARGET,
                "agree": True,
            },
            # 计划内但尚未复核：agree 为 null
            {
                "observation_id": observations[1]["observation_id"],
                "entity_id": TARGET,
                "agree": None,
            },
            {
                "observation_id": observations[2]["observation_id"],
                "entity_id": TARGET,
                "agree": False,
            },
        ],
    )
    result = find(run_metrics(b.root)["results"], "recheck_pass_rate")
    # 已复核 2 项，其中一致 1 项；未复核的 1 项不进分母，单独列出
    assert_ratio(result, 1, 2)
    assert result["components"]["not_checked"] == 1
    assert result["drilldown"]["denominator_ids"] == [
        "obs-000001|brand-lanxu",
        "obs-000003|brand-lanxu",
    ]


def test_recheck_with_only_unchecked_items_is_null(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    observation = b.obs("Q001")
    b.write()
    recheck_file(
        b.root,
        [{"observation_id": observation["observation_id"], "entity_id": TARGET, "agree": None}],
    )
    result = find(run_metrics(b.root)["results"], "recheck_pass_rate")
    assert result["value"] is None
    assert result["denominator"] == 0
    assert "分母为 0" in result["null_reason"]
    assert result["components"]["not_checked"] == 1


@pytest.mark.parametrize(
    "item",
    [
        {"observation_id": "obs-000001", "entity_id": TARGET, "agree": "yes"},
        {"observation_id": "obs-000001", "entity_id": TARGET},
        {"entity_id": TARGET, "agree": True},
        "not-an-object",
    ],
)
def test_malformed_recheck_item_is_an_input_error(tmp_path: Path, item: Any) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001")
    b.write()
    recheck_file(b.root, [item])
    code, data = run_cli_json("metrics", str(b.root))
    assert code == 2
    assert "recheck/result.json" in data["message"]


# ---------------------------------------------------------------- 登记与参数


def test_unknown_registered_metric_is_noticed_and_not_computed(tmp_path: Path) -> None:
    registered = [
        {
            "metric_id": "mention_rate",
            "definition_version": "1.0",
            "role": "acceptance",
            "threshold": {"op": ">=", "value": 0.6},
        },
        {
            "metric_id": "brand_affinity_index",
            "definition_version": "1.0",
            "role": "descriptive",
            "threshold": None,
        },
    ]
    b = Builder(tmp_path / "proj", prereg={"metrics": registered})
    b.obs("Q001")
    b.write()
    data = run_metrics(b.root)
    notice = next(n for n in data["notices"] if n["code"] == "unknown_registered_metric")
    assert notice["details"]["metric_id"] == "brand_affinity_index"
    entries = {item["registered_id"]: item for item in data["registered_metrics"]}
    assert entries["brand_affinity_index"]["effective"] is False
    assert entries["brand_affinity_index"]["metric_ids"] == []
    assert entries["mention_rate"] == {
        "registered_id": "mention_rate",
        "metric_ids": ["mention_rate"],
        "role": "acceptance",
        "threshold": {"op": ">=", "value": 0.6},
        "definition_version": "1.0",
        "effective": True,
        "note": None,
    }
    assert not any(r["metric_id"] == "brand_affinity_index" for r in data["results"])
    mention = find(data["results"], "mention_rate")
    assert (mention["preregistered"], mention["role"]) == (True, "acceptance")


def test_negative_seed_is_rejected(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    b.obs("Q001")
    b.write()
    code, data = run_cli_json("metrics", str(b.root), "--seed", "-1")
    assert code == 2
    assert "--seed" in data["message"]


def test_scope_helpers() -> None:
    assert metrics.Scope().overall is True
    assert metrics.Scope(platform="engine-a").overall is False
    assert metrics.Scope(window_id="W1").to_dict("brand-lanxu") == {
        "platform": None,
        "channel": None,
        "window_id": "W1",
        "date": None,
        "entity_id": "brand-lanxu",
    }
    with pytest.raises(InputError):
        metrics.Scope(date="2026/10/08")


# ---------------------------------------------------------------- 不合契约的输入


def test_judgment_with_mismatched_direction_and_label_is_an_input_error(tmp_path: Path) -> None:
    b = Builder(tmp_path / "proj")
    observation = b.obs("Q001")
    b.judge(observation, "supported")
    b.judge(observation, "missing", direction="answer_to_facts")
    b.write()
    code, data = run_cli_json("metrics", str(b.root))
    assert code == 2
    assert "judgments.jsonl 第 2 行" in data["message"]
    assert "judgment" in data["message"]


def test_malformed_annotations_are_input_errors(tmp_path: Path) -> None:
    missing_field = Builder(tmp_path / "field")
    missing_field.obs("Q001")
    del missing_field.annotations[0]["mentioned"]
    missing_field.write()
    code, data = run_cli_json("metrics", str(missing_field.root))
    assert code == 2
    assert "ann-000001" in data["message"]
    assert "annotation 契约" in data["message"]

    # 同一（观测，实体）有两条标注时要比较标注者优先级：缺 annotator 的记录无法比较
    missing_annotator = Builder(tmp_path / "annotator")
    twice = missing_annotator.obs("Q001")
    missing_annotator.annotate(twice, mentioned=False, sentiment=None)
    del missing_annotator.annotations[0]["annotator"]
    missing_annotator.write()
    code, data = run_cli_json("metrics", str(missing_annotator.root))
    assert code == 2
    assert "annotations.jsonl" in data["message"]

    bad_checks = Builder(tmp_path / "checks")
    observation = bad_checks.obs("Q001", mentioned=None, citations=[make_citation(1, "x.example")])
    bad_checks.annotate(observation, citation_checks=[{"valid": True, "relevant": True}])
    bad_checks.write()
    code, data = run_cli_json("metrics", str(bad_checks.root))
    assert code == 2
    assert "ann-000001" in data["message"]


def test_identifier_that_cannot_be_encoded_is_an_input_error(tmp_path: Path) -> None:
    """编号里有孤立代理项（JSON 转义能写出来，但无法编码为 UTF-8）：按输入错误处理，而不是内部错误。"""
    b = Builder(tmp_path / "proj")
    observation = b.obs("Q001")
    observation["observation_id"] = "obs-" + chr(0xD800)
    b.annotations[0]["observation_id"] = observation["observation_id"]
    for name, records in (
        ("observations.jsonl", b.observations),
        ("annotations.jsonl", b.annotations),
    ):
        lines = [json.dumps(record, ensure_ascii=True, sort_keys=True) for record in records]
        (b.root / name).write_text("\n".join(lines) + "\n", encoding="ascii")
    code, data = run_cli_json("metrics", str(b.root))
    assert code == 2
    assert "编号" in data["message"]
    with pytest.raises(InputError):
        metrics.ids_hash(["ok", "bad-" + chr(0xDFFF)])


# ---------------------------------------------------------------- 跨进程复现（S18）


def test_separate_processes_with_different_hash_seeds_give_identical_bytes(tmp_path: Path) -> None:
    """两个独立目录、两个独立进程、不同的字符串哈希种子：结果文件逐字节相同。

    进程内的重复计算测不出依赖集合遍历顺序的输出，这里用不同的 PYTHONHASHSEED 各算一次。
    """
    first = build_rich(tmp_path / "party-a" / "proj").root
    second = copy_project(first, tmp_path / "party-b" / "copied-elsewhere")
    outputs: list[bytes] = []
    for root, hash_seed in ((first, "1"), (second, "4242")):
        completed = subprocess.run(
            [sys.executable, "-m", "geo_verify", "metrics", str(root)],
            cwd=root.parent,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        outputs.append((root / "out" / "metrics.json").read_bytes())
    assert outputs[0] == outputs[1]
    document = json.loads(outputs[0])
    assert document["exclusion_audit"]["excluded_observations"] == 2
    assert len(document["results"]) > 100
