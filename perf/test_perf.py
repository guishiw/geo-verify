"""性能检查（GOAL 5.5）：10 万条观测计算全部指标 ≤ 60 秒、内存峰值 ≤ 2 GB（bootstrap 1000 次）。

数据现场生成（合成、虚构，不入库）：6 个平台 × 3 个窗口 × 400 道题 × 14 次 = 100 800 条观测，
每条观测有目标主体和竞品两条标注，另有约 4 万条判定、引用、无效观测、审计剔除和少量快照。
由 `make perf` 运行，不属于 `make check`。

测量口径：
- 耗时：在子进程中执行一次 `python -m geo_verify metrics PROJECT --json` 的墙钟时间，
  含解释器启动、读取并索引全部原始记录、计算全部指标与区间、剔除审计、写出 out/metrics.json；
  不含合成数据的生成时间，也不含模型判定（判定结果作为输入文件读入）。
- 内存：`resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`，即本进程已结束子进程的最大常驻内存（RSS）峰值。
  本测试只启动这一个子进程，所以它就是指标命令的内存峰值；macOS 以字节计，Linux 以 KB 计。
- bootstrap 次数取项目预先登记的 1000 次（结果文件中的 parameters.iterations 会被断言）。
"""

from __future__ import annotations

import hashlib
import json
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from factory import (
    COMPETITOR,
    TARGET,
    make_annotation,
    make_citation,
    make_entities,
    make_judgment,
    make_observation,
    make_project,
    make_prompt,
    seal_prompts,
)

from geo_verify import audit
from geo_verify.jsonl import dumps_line, write_json
from geo_verify.metrics import METRIC_IDS

PLATFORMS = [f"engine-{letter}" for letter in "abcdef"]
WINDOWS = ["W1", "W2", "W3"]
QUERIES = 400
ATTEMPTS = 14
MAX_SECONDS = 60.0
MAX_BYTES = 2 * 1024**3
DOMAINS = [
    "lanxu-auto.example",
    "gov-data.example",
    "other-media.example",
    "auto-review.example",
    "forum-ev.example",
]


def _generate(root: Path) -> dict[str, Any]:
    """生成项目目录，返回生成时记下的真值（提及次数等），供结果核对。"""
    project = make_project(
        root,
        platforms=[
            {"platform_id": platform, "display_name": f"模拟平台 {platform}", "channels": ["api"]}
            for platform in PLATFORMS
        ],
    )
    make_entities(root)
    sealed = seal_prompts(
        root, project, [make_prompt(f"Q{index:04d}") for index in range(1, QUERIES + 1)]
    )
    snapshot_dir = root / "snapshots"
    snapshot_dir.mkdir()
    truth = {
        "observations": 0,
        "valid": 0,
        "mentioned": 0,
        "judgments": 0,
        "checked_citations": 0,
        "confirmed_risks": 0,
    }
    excluded: list[str] = []
    recheck_items: list[dict[str, Any]] = []
    number = annotation_number = judgment_number = 0
    with (
        (root / "observations.jsonl").open("w", encoding="utf-8") as observations,
        (root / "annotations.jsonl").open("w", encoding="utf-8") as annotations,
        (root / "judgments.jsonl").open("w", encoding="utf-8") as judgments,
    ):
        for query_index in range(QUERIES):
            # 各题的提及倾向不同（题间差异），用确定的算术规则代替随机数
            threshold = 10 + (query_index * 37) % 81
            for platform_index, platform in enumerate(PLATFORMS):
                for window_index, window in enumerate(WINDOWS):
                    for attempt in range(ATTEMPTS):
                        number += 1
                        invalid = number % 33 == 0
                        citations = [
                            make_citation(order, DOMAINS[(number + order * 3) % len(DOMAINS)])
                            for order in range(1, 1 + number % 4)
                        ]
                        fields: dict[str, Any] = {}
                        if number % 500 == 0:
                            content = f"snapshot {number}".encode()
                            name = f"snapshots/obs-{number:06d}.txt"
                            (root / name).write_bytes(content)
                            fields["snapshots"] = [
                                {
                                    "path": name,
                                    "sha256": hashlib.sha256(content).hexdigest(),
                                    "media_type": "text/plain",
                                }
                            ]
                        observation = make_observation(
                            project,
                            sealed,
                            number,
                            query_id=f"Q{query_index + 1:04d}",
                            platform=platform,
                            window=window,
                            attempt=attempt + 1,
                            status="invalid" if invalid else "valid",
                            invalid_reason="timeout",
                            started_at=f"2026-10-{8 + window_index * 7 + attempt % 5:02d}T"
                            f"{(attempt * 5 + platform_index) % 24:02d}:00:00Z",
                            citations=citations,
                            model_version=f"{platform}-2026.10",
                            answer="可以考虑澜序 S7，也可以看看岑野 K5。" * 8,
                            **fields,
                        )
                        observations.write(dumps_line(observation) + "\n")
                        truth["observations"] += 1
                        if invalid:
                            continue
                        dropped = number % 997 == 0
                        if dropped:
                            excluded.append(observation["observation_id"])
                        score = (number * 7919 + platform_index * 13 + window_index * 5) % 100
                        mentioned = score < threshold
                        if not dropped:
                            truth["valid"] += 1
                            truth["mentioned"] += mentioned
                        rankable = mentioned and number % 3 == 0
                        target_extra: dict[str, Any] = {}
                        if number % 4 == 1 and citations:
                            # 每条引用的 URL 都不同且域名可识别：每条核验对应一条已核验引用
                            target_extra["citation_checks"] = [
                                {
                                    "order": item["order"],
                                    "valid": (number + item["order"]) % 5 != 0,
                                    "relevant": (number + item["order"]) % 3 != 0,
                                }
                                for item in citations
                            ]
                            if not dropped:
                                truth["checked_citations"] += len(citations)
                        if mentioned and number % 11 == 0:
                            target_extra["risk"] = {
                                "flags": ["unfounded_negative"],
                                "unfounded_negative": True,
                                "identified": number % 22 == 0,
                                "confirmed": True,
                                "closed": number % 44 == 0,
                            }
                            if not dropped:
                                truth["confirmed_risks"] += 1
                        if number % 50 == 1 and not dropped:
                            recheck_items.append(
                                {
                                    "observation_id": observation["observation_id"],
                                    "entity_id": TARGET,
                                    "agree": number % 100 == 1,
                                    "differences": [],
                                }
                            )
                        for entity_id, flag in ((None, mentioned), (COMPETITOR, number % 2 == 0)):
                            annotation_number += 1
                            extra = {"entity_id": entity_id} if entity_id else target_extra
                            annotation = make_annotation(
                                observation,
                                annotation_number,
                                mentioned=flag,
                                rankable=rankable and entity_id is None,
                                rank=1 + number % 6 if rankable and entity_id is None else None,
                                sentiment=("positive", "neutral", "negative")[number % 3]
                                if flag
                                else None,
                                **extra,
                            )
                            annotations.write(dumps_line(annotation) + "\n")
                        if number % 5 == 0:
                            for label in (
                                ("supported", "contradicted", "unverifiable")[number % 3],
                                ("present_correct", "present_wrong", "missing")[number % 3],
                            ):
                                judgment_number += 1
                                direction = (
                                    "answer_to_facts"
                                    if label in ("supported", "contradicted", "unverifiable")
                                    else "facts_to_answer"
                                )
                                judgment = make_judgment(
                                    observation, judgment_number, direction=direction, label=label
                                )
                                judgments.write(dumps_line(judgment) + "\n")
                                if not dropped:
                                    truth["judgments"] += 1
    audit.append_event(
        root,
        actor="数据管理员（虚构）",
        action="exclude",
        target_type="observation",
        target_id="batch",
        reason="性能检查构造的剔除（虚构）",
        occurred_at="2026-11-01T00:00:00Z",
        details={"rule_id": "EX-DUP", "count": len(excluded), "observation_ids": excluded},
        synthetic=True,
    )
    agreed = sum(1 for item in recheck_items if item["agree"])
    write_json(
        root / "recheck" / "result.json",
        {
            "schema_version": "1.0",
            "kind": "recheck_result",
            "project_id": project["project_id"],
            "synthetic": True,
            "plan_hash": None,
            "fields": ["mentioned", "recommended", "rank"],
            "planned": len(recheck_items),
            "checked": len(recheck_items),
            "agreed": agreed,
            "disagreed": len(recheck_items) - agreed,
            "not_checked": 0,
            "pass_rate": agreed / len(recheck_items),
            "null_reason": None,
            "items": recheck_items,
        },
    )
    truth["recheck_items"] = len(recheck_items)
    truth["recheck_agreed"] = agreed
    truth["excluded"] = len(excluded)
    truth["annotations"] = annotation_number
    truth["judgment_records"] = judgment_number
    return truth


def _peak_bytes() -> int:
    """已结束子进程的最大常驻内存。macOS 以字节计，Linux 以 KB 计。"""
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def test_metrics_on_100k_observations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "perf-project"
    started = time.perf_counter()
    truth = _generate(root)
    generated = time.perf_counter() - started
    assert truth["observations"] >= 100_000
    # 生成数据不启动子进程：此前的子进程内存峰值应远小于上限，否则测到的就不是指标命令
    assert _peak_bytes() < MAX_BYTES // 8

    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "geo_verify", "metrics", str(root), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    peak = _peak_bytes()
    assert completed.returncode == 0, completed.stderr[-2000:]

    document = json.loads((root / "out" / "metrics.json").read_text(encoding="utf-8"))
    assert document["parameters"]["iterations"] == 1000
    # “全部指标”：指标表里的每个编号都有结果，每个平台都有一条提及率
    assert {item["metric_id"] for item in document["results"]} == set(METRIC_IDS)
    assert {
        item["scope"]["platform"]
        for item in document["results"]
        if item["metric_id"] == "mention_rate" and item["scope"]["platform"] is not None
    } == set(PLATFORMS)
    assert document["observations"]["total"] == truth["observations"]
    assert document["observations"]["valid"] == truth["valid"]
    overall = next(
        item
        for item in document["results"]
        if item["metric_id"] == "mention_rate"
        and all(value is None for key, value in item["scope"].items() if key != "entity_id")
    )
    assert overall["numerator"] == truth["mentioned"]
    assert overall["denominator"] == truth["valid"]
    assert overall["interval"]["method"] == "cluster_bootstrap"
    assert overall["clustering"]["clusters"] == QUERIES
    assert overall["excluded"]["count"] == truth["observations"] - truth["valid"]
    strict = next(
        item
        for item in document["results"]
        if item["metric_id"] == "accuracy_strict" and item["scope"]["platform"] is None
    )
    presence = next(
        item
        for item in document["results"]
        if item["metric_id"] == "core_fact_presence" and item["scope"]["platform"] is None
    )
    assert strict["denominator"] + presence["denominator"] == truth["judgments"]
    evidence = next(
        item
        for item in document["results"]
        if item["metric_id"] == "evidence_completeness" and item["scope"]["platform"] is None
    )
    assert evidence["numerator"] > 0
    overall_results = {
        item["metric_id"]: item
        for item in document["results"]
        if all(value is None for key, value in item["scope"].items() if key != "entity_id")
    }
    assert overall_results["citation_validity"]["denominator"] == truth["checked_citations"]
    assert overall_results["risk_identification_rate"]["denominator"] == truth["confirmed_risks"]
    recheck = overall_results["recheck_pass_rate"]
    assert (recheck["numerator"], recheck["denominator"]) == (
        truth["recheck_agreed"],
        truth["recheck_items"],
    )
    # 这份数据让整体范围的每个指标都有值（没有任何一个因为缺输入而被跳过）
    assert [key for key, item in overall_results.items() if item["value"] is None] == []

    with capsys.disabled():  # 不加 -s 也把实测数值打印出来，便于写入验证记录
        print(
            f"\n性能：观测 {truth['observations']} 条（有效 {truth['valid']}），"
            f"标注 {truth['annotations']} 条，判定 {truth['judgment_records']} 条，"
            f"结果 {len(document['results'])} 条；生成数据 {generated:.1f} 秒；"
            f"计算全部指标 {elapsed:.1f} 秒（上限 {MAX_SECONDS:.0f} 秒）；"
            f"内存峰值 {peak / 1024**2:.0f} MB（上限 {MAX_BYTES / 1024**2:.0f} MB）"
        )
    assert elapsed <= MAX_SECONDS
    assert peak <= MAX_BYTES
