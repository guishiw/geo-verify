"""全流程（tests/test_pipeline.py）用的手工构造数据。

只写“人会准备的输入”：project.json、entities.jsonl、prompts/draft.jsonl、待导入的观测 CSV、
两名标注者的标注表、填好的复核表。之后的封存、导入、标注、指标、报告全部经 CLI 完成。

数据规模：1 个平台（engine-a）、2 个通道（api、manual）、6 道题、3 个窗口、每题每窗每通道 2 次，
即每个通道 36 条有效观测，另有 1 条 api 超时的无效观测。回答只有两种写法：

    MENTION  “推荐以下两款：\n1. 澜序 S7：……\n2. 岑野 K5：……”  提及并推荐目标品牌，可排序，位次 1
    OTHER    “可以看看岑野 K5，空间表现不错。”           不提及目标品牌，不可排序

所以提及率、偏离度都能按下面的计划手工数出来（见各 PLAN 的注释）。全部数据为虚构，带 synthetic。
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROJECT_ID = "pipe-demo"
PLATFORM = "engine-a"
CHANNELS = ("api", "manual")
WINDOWS = ("W1", "W2", "W3")
WINDOW_DATES = {"W1": "2026-10-08", "W2": "2026-10-09", "W3": "2026-10-10"}
BASELINE_DATES = {"W1": "2026-09-08", "W2": "2026-09-09", "W3": "2026-09-10"}
QUERIES = ("Q001", "Q002", "Q003", "Q004", "Q005", "Q006")
ATTEMPTS = (1, 2)
API_MODEL = "engine-a-2026.10"
MANUAL_FEATURES = "网页端默认模式，页面未展示模型名（虚构）"

MENTION = "推荐以下两款：\n1. 澜序 S7：续航长，值得考虑。\n2. 岑野 K5：空间大。\n澜序 S7 的 CLTC 续航为 620 公里[1]。"
MENTION_CITATIONS = (
    "inline | https://lanxu-auto.example/s7/spec | 澜序 S7 参数（虚构）\n"
    "source_list | https://auto-review.example/ev-suv | 纯电 SUV 横评（虚构）"
)
OTHER = "可以看看岑野 K5，空间表现不错[1]。"
OTHER_CITATIONS = "inline | https://auto-review.example/k5 | 岑野 K5 评测（虚构）"

# 计划：给定（通道, 题目, 次数）返回是否提及目标品牌；每个窗口都一样（窗口间没有变化，不应告警）。
Plan = Callable[[str, str, int], bool]


def plan_pass(channel: str, query_id: str, attempt: int) -> bool:
    """api：只有 Q006 第 2 次不提及 → 每窗 11/12，合计 33/36。
    manual：Q005、Q006 的第 2 次不提及 → 每窗 10/12，合计 30/36。
    偏离度 = |33/36 − 30/36| / ((33/36 + 30/36) / 2) = (3/36) / (63/72) = 2/21 ≈ 0.0952 ≤ 0.3 → 通过。
    合并提及率 = 63/72 = 0.875 ≥ 0.6。"""
    if attempt == 2 and query_id == "Q006":
        return False
    return not (channel == "manual" and attempt == 2 and query_id == "Q005")


def plan_crosscheck_fail(channel: str, query_id: str, attempt: int) -> bool:
    """api：全部提及 → 36/36 = 1。manual：只有 Q001、Q002 提及 → 12/36 = 1/3。
    偏离度 = |1 − 1/3| / ((1 + 1/3) / 2) = (2/3) / (2/3) = 1.0 > 0.3 → 不通过。"""
    return channel == "api" or query_id in {"Q001", "Q002"}


def plan_baseline(channel: str, query_id: str, attempt: int) -> bool:
    """基线期：每题第 1 次提及、第 2 次不提及 → 每个通道 18/36 = 0.5。"""
    return attempt == 1


def project_document() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "project_id": PROJECT_ID,
        "name": "澜序汽车全流程联调（虚构）",
        "industry": "auto",
        "client": {"id": "client-lanxu", "name": "澜序汽车（虚构委托方）"},
        "provider": {"id": "provider-demo", "name": "示例服务方（虚构）"},
        "timezone": "Asia/Shanghai",
        "platforms": [
            {"platform_id": PLATFORM, "display_name": "模拟平台 A", "channels": list(CHANNELS)}
        ],
        "target_entity_id": "brand-lanxu",
        "competitor_entity_ids": ["brand-cenye"],
        "environment": {
            "logged_in": False,
            "history_cleared": True,
            "web_search": True,
            "region": "CN",
            "device": None,
        },
        "preregistration": {
            "registered_at": "2026-10-01T01:00:00Z",
            "registered_by": "甲乙双方共同登记（虚构）",
            "metrics": [
                {
                    "metric_id": "mention_rate",
                    "definition_version": "1.0",
                    "role": "acceptance",
                    "threshold": {"op": ">=", "value": 0.6},
                },
                {
                    "metric_id": "sentiment_distribution",
                    "definition_version": "1.0",
                    "role": "descriptive",
                    "threshold": None,
                },
            ],
            "accuracy_basis": "verifiable",
            "recommendation_success": {"criterion": "recommended", "top_n": None},
            "crosscheck": {
                "formula": "symmetric_relative_difference",
                "threshold": 0.3,
                "metric_id": "mention_rate",
                "channel_a": "api",
                "channel_b": "manual",
            },
            "sufficiency": {"min_windows": 3, "min_valid_observations": 30},
            "statistics": {
                "confidence_level": 0.95,
                "bootstrap_iterations": 1000,
                "seed": 20261001,
            },
            "recheck": {"fraction": 0.2, "seed": 20261001},
            "target_sources": ["lanxu-auto.example"],
            "authority": {
                "authoritative_tiers": ["A", "B"],
                "domain_tiers": {"gov-data.example": "A", "lanxu-auto.example": "C"},
            },
            "exclusion_rules": [
                {"rule_id": "EX-DUP", "description": "同一 trace_id 重复导入的记录"}
            ],
        },
        "disclosure": {
            "statement": "本项目数据全部为合成数据；工具操作方与服务方为同一主体（虚构）。",
            "relationships": [
                {
                    "party": "示例服务方（虚构）",
                    "role": "服务方",
                    "relationship": "受委托方付费提供优化服务",
                }
            ],
            "tool_operator": "示例服务方（虚构）",
            "operator_is_provider": True,
        },
        "synthetic": True,
    }


def entity_records() -> list[dict[str, Any]]:
    def entity(entity_id: str, kind: str, role: str, name: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": "1.0",
            "entity_id": entity_id,
            "project_id": PROJECT_ID,
            "kind": kind,
            "role": role,
            "name": name,
            "aliases": [],
            "exclusions": [],
            "owned_domains": [],
            "parent_entity_id": None,
            "synthetic": True,
        }
        record.update(fields)
        return record

    return [
        entity(
            "brand-lanxu",
            "brand",
            "target",
            "澜序",
            aliases=["澜序汽车", "LANXU", "Lan-Xu"],
            exclusions=["波澜序曲"],
            owned_domains=["lanxu-auto.example"],
        ),
        entity(
            "product-lanxu-s7",
            "product",
            "target",
            "澜序 S7",
            aliases=["澜序S7"],
            parent_entity_id="brand-lanxu",
        ),
        entity(
            "brand-cenye",
            "brand",
            "competitor",
            "岑野",
            aliases=["岑野汽车", "CENYE"],
            owned_domains=["cenye-auto.example"],
        ),
        entity(
            "product-cenye-k5",
            "product",
            "competitor",
            "岑野 K5",
            aliases=["岑野K5"],
            parent_entity_id="brand-cenye",
        ),
    ]


PROMPTS: tuple[tuple[str, str, str, bool], ...] = (
    ("Q001", "category", "20 万左右的纯电 SUV 有哪些值得考虑？", True),
    ("Q002", "scenario", "经常跑长途的家庭，适合买哪款纯电 SUV？", True),
    ("Q003", "comparison", "澜序 S7 和岑野 K5 相比各有什么优缺点？", True),
    ("Q004", "decision", "预算 22 万买纯电 SUV，应该怎么选？", True),
    ("Q005", "brand", "澜序汽车这个品牌怎么样？", True),
    ("Q006", "after_sales", "纯电 SUV 的整车质保一般是多久？", False),
)


def prompt_records() -> list[dict[str, Any]]:
    return [
        {
            "query_id": query_id,
            "text": text,
            "intent_type": intent,
            "business_level": None,
            "priority": "P1",
            "source": "联调样例（虚构）",
            "expects_recommendation": expects,
        }
        for query_id, intent, text, expects in PROMPTS
    ]


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_inputs(root: Path) -> None:
    """写出项目目录的三份手工输入：project.json、entities.jsonl、prompts/draft.jsonl。"""
    root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(project_document(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    (root / "project.json").write_text(text, encoding="utf-8")
    _write_jsonl(root / "entities.jsonl", entity_records())
    _write_jsonl(root / "prompts" / "draft.jsonl", prompt_records())


OBSERVATION_COLUMNS = (
    "query_id",
    "platform",
    "channel",
    "entry_form",
    "model_version",
    "entry_features",
    "window_id",
    "attempt",
    "started_at",
    "ended_at",
    "logged_in",
    "history_cleared",
    "web_search",
    "region",
    "device",
    "answer_text",
    "citations",
    "status",
    "invalid_reason",
    "operator",
    "synthetic",
)


def observation_rows(plan: Plan, dates: dict[str, str] = WINDOW_DATES) -> list[dict[str, str]]:
    """按计划生成观测行：每个窗口内按通道、题目、次数排列，时间逐条递增 1 分钟（UTC）。

    另在 W1 末尾追加一条 api 超时的无效观测（Q001 第 3 次），它不进任何指标分母。
    """
    rows: list[dict[str, str]] = []
    for window in WINDOWS:
        minute = 0
        for channel in CHANNELS:
            for query_id in QUERIES:
                for attempt in ATTEMPTS:
                    mentioned = plan(channel, query_id, attempt)
                    rows.append(
                        _row(
                            window,
                            dates[window],
                            minute,
                            channel,
                            query_id,
                            attempt,
                            answer=MENTION if mentioned else OTHER,
                            citations=MENTION_CITATIONS if mentioned else OTHER_CITATIONS,
                        )
                    )
                    minute += 1
        if window == "W1":
            invalid = _row(window, dates[window], minute, "api", "Q001", 3, answer="", citations="")
            invalid.update(status="invalid", invalid_reason="timeout")
            rows.append(invalid)
    return rows


def _row(
    window: str,
    date: str,
    minute: int,
    channel: str,
    query_id: str,
    attempt: int,
    *,
    answer: str,
    citations: str,
) -> dict[str, str]:
    api = channel == "api"
    return {
        "query_id": query_id,
        "platform": PLATFORM,
        "channel": channel,
        "entry_form": "api" if api else "web",
        "model_version": API_MODEL if api else "",
        "entry_features": "" if api else MANUAL_FEATURES,
        "window_id": window,
        "attempt": str(attempt),
        "started_at": f"{date}T02:{minute:02d}:00Z",
        "ended_at": f"{date}T02:{minute:02d}:30Z",
        "logged_in": "false",
        "history_cleared": "true",
        "web_search": "true",
        "region": "CN",
        "device": "",
        "answer_text": answer,
        "citations": citations,
        "status": "valid",
        "invalid_reason": "",
        "operator": "采集员甲（虚构）" if api else "采集员乙（虚构）",
        "synthetic": "true",
    }


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


ANNOTATION_COLUMNS = (
    "observation_id",
    "entity_id",
    "mentioned",
    "recommended",
    "negated",
    "rankable",
    "rank",
    "rank_list_size",
    "sentiment",
    "note",
)


def human_rows(
    mentioned_ids: list[str], other_ids: list[str], *, flip: str | None = None
) -> list[dict[str, str]]:
    """一名标注者对目标品牌的人工标注。提及的回答：推荐、位次 1（序列 2 项）、正面；未提及的：全部为否。

    flip 指定一条“提及”观测，把 recommended 标成 false（制造一处分歧）。
    """
    rows = []
    for observation_id in mentioned_ids:
        rows.append(
            {
                "observation_id": observation_id,
                "entity_id": "brand-lanxu",
                "mentioned": "true",
                "recommended": "false" if observation_id == flip else "true",
                "negated": "false",
                "rankable": "true",
                "rank": "1",
                "rank_list_size": "2",
                "sentiment": "positive",
                "note": "人工标注（虚构）",
            }
        )
    for observation_id in other_ids:
        rows.append(
            {
                "observation_id": observation_id,
                "entity_id": "brand-lanxu",
                "mentioned": "false",
                "recommended": "false",
                "negated": "false",
                "rankable": "false",
                "rank": "",
                "rank_list_size": "",
                "sentiment": "",
                "note": "人工标注（虚构）",
            }
        )
    return rows


def fill_recheck(plan_csv: Path, out_csv: Path) -> int:
    """复核人逐项填写复核表：结论与原标注一致（照抄 original_* 到 recheck_*）。返回行数。"""
    with plan_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = tuple(reader.fieldnames or ())
        rows = list(reader)
    for row in rows:
        for field in ("mentioned", "recommended", "rank"):
            row[f"recheck_{field}"] = row[f"original_{field}"]
        row["recheck_note"] = "复核一致（虚构）"
    write_csv(out_csv, columns, rows)
    return len(rows)


def kappa_sample(observations_file: Path, size: int = 4) -> tuple[list[str], list[str]]:
    """双人标注的样本：按观测文件顺序取前 size 条“提及”回答和前 size 条“未提及”回答的编号。"""
    mentioned: list[str] = []
    other: list[str] = []
    for line in observations_file.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["status"] != "valid":
            continue
        bucket = mentioned if record["answer_text"] == MENTION else other
        if len(bucket) < size:
            bucket.append(record["observation_id"])
    return mentioned, other
