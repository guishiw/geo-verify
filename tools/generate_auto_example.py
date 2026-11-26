"""生成虚构汽车品牌（澜序汽车）的完整合成样例，输出到 src/geo_verify/examples/auto/。

固定 seed（20261001），重复运行产出逐字节一致。全部数据为虚构，带 synthetic: true，
域名只用 .example，不包含任何真实品牌、客户信息或个人信息。
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path
from typing import Any

SEED = 20261001
PROJECT_ID = "auto-demo"
REGISTERED_AT = "2026-10-01T01:00:00Z"
OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "geo_verify" / "examples" / "auto"

PLATFORMS: tuple[tuple[str, str, str], ...] = (
    ("engine-a", "模拟平台 A", "engine-a-2026.10"),
    ("engine-b", "模拟平台 B", "engine-b-2026.10"),
)
CHANNELS = ("api", "manual")
WINDOWS: tuple[tuple[str, str], ...] = (
    ("W1", "2026-10-08"),
    ("W2", "2026-10-09"),
    ("W3", "2026-10-10"),
)
ATTEMPTS = (1, 2)

OBSERVATION_COLUMNS = (
    "observation_id",
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


def build_project() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "project_id": PROJECT_ID,
        "name": "澜序汽车 GEO 验收预检样例（虚构）",
        "industry": "auto",
        "client": {"id": "client-lanxu", "name": "澜序汽车（虚构委托方）"},
        "provider": {"id": "provider-demo", "name": "示例监测团队（虚构服务方）"},
        "timezone": "Asia/Shanghai",
        "platforms": [
            {"platform_id": pid, "display_name": name, "channels": list(CHANNELS)}
            for pid, name, _ in PLATFORMS
        ],
        "target_entity_id": "brand-lanxu",
        "competitor_entity_ids": ["brand-cenye", "brand-shuofeng"],
        "environment": {
            "logged_in": False,
            "history_cleared": True,
            "web_search": True,
            "region": "CN",
            "device": None,
        },
        "preregistration": {
            "registered_at": REGISTERED_AT,
            "registered_by": "甲乙双方共同登记（虚构）",
            "metrics": [
                {
                    "metric_id": "mention_rate",
                    "definition_version": "1.0",
                    "role": "acceptance",
                    "threshold": {"op": ">=", "value": 0.6},
                },
                {
                    "metric_id": "accuracy",
                    "definition_version": "1.0",
                    "role": "acceptance",
                    "threshold": {"op": ">=", "value": 0.85},
                },
                {
                    "metric_id": "core_fact_presence",
                    "definition_version": "1.0",
                    "role": "acceptance",
                    "threshold": {"op": ">=", "value": 0.7},
                },
                {
                    "metric_id": "intent_coverage",
                    "definition_version": "1.0",
                    "role": "descriptive",
                    "threshold": None,
                },
                {
                    "metric_id": "top3_rate",
                    "definition_version": "1.0",
                    "role": "descriptive",
                    "threshold": None,
                },
                {
                    "metric_id": "recommendation_rate",
                    "definition_version": "1.0",
                    "role": "descriptive",
                    "threshold": None,
                },
                {
                    "metric_id": "target_source_presence",
                    "definition_version": "1.0",
                    "role": "descriptive",
                    "threshold": None,
                },
                {
                    "metric_id": "authority_share",
                    "definition_version": "1.0",
                    "role": "descriptive",
                    "threshold": None,
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
                "seed": SEED,
            },
            "recheck": {"fraction": 0.2, "seed": SEED},
            "target_sources": ["lanxu-auto.example"],
            "authority": {
                "authoritative_tiers": ["A", "B"],
                "domain_tiers": {
                    "gov-data.example": "A",
                    "auto-review.example": "B",
                    "news-auto.example": "B",
                    "lanxu-auto.example": "C",
                    "cenye-auto.example": "C",
                    "shuofeng-auto.example": "C",
                    "ev-forum.example": "D",
                },
            },
            "exclusion_rules": [
                {"rule_id": "EX-DUP", "description": "同一采集任务重复导入的冗余观测"}
            ],
        },
        "disclosure": {
            "statement": "本样例全部为合成数据，仅用于演示 geo-verify 预检流程，不代表任何真实平台表现。",
            "relationships": [
                {
                    "party": "示例监测团队（虚构服务方）",
                    "role": "服务方",
                    "relationship": "受虚拟委托方委托执行演示预检",
                }
            ],
            "tool_operator": "示例监测团队（虚构服务方）",
            "operator_is_provider": True,
        },
        "synthetic": True,
        "ext": {
            "generator": "tools/generate_auto_example.py",
            "generator_seed": SEED,
        },
    }


def build_entities() -> list[dict[str, Any]]:
    def make(entity_id: str, kind: str, role: str, name: str, **extra: Any) -> dict[str, Any]:
        item: dict[str, Any] = {
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
        item.update(extra)
        return item

    return [
        make(
            "brand-lanxu",
            "brand",
            "target",
            "澜序",
            aliases=["澜序汽车", "LANXU", "Lan-Xu"],
            exclusions=["波澜序曲"],
            owned_domains=["lanxu-auto.example"],
        ),
        make(
            "product-lanxu-s7",
            "product",
            "target",
            "澜序 S7",
            aliases=["澜序S7", "LANXU S7", "Lan-Xu S7"],
            parent_entity_id="brand-lanxu",
        ),
        make(
            "product-lanxu-x5",
            "product",
            "target",
            "澜序 X5",
            aliases=["澜序X5"],
            parent_entity_id="brand-lanxu",
        ),
        make(
            "product-lanxu-e3",
            "product",
            "target",
            "澜序 E3",
            aliases=["澜序E3"],
            parent_entity_id="brand-lanxu",
        ),
        make(
            "product-lanxu-m9",
            "product",
            "target",
            "澜序 M9",
            aliases=["澜序M9"],
            parent_entity_id="brand-lanxu",
        ),
        make(
            "brand-cenye",
            "brand",
            "competitor",
            "岑野",
            aliases=["岑野汽车", "CENYE"],
            owned_domains=["cenye-auto.example"],
        ),
        make(
            "product-cenye-k5",
            "product",
            "competitor",
            "岑野 K5",
            aliases=["岑野K5"],
            parent_entity_id="brand-cenye",
        ),
        make(
            "product-cenye-s7",
            "product",
            "competitor",
            "岑野 S7",
            aliases=["岑野S7"],
            parent_entity_id="brand-cenye",
        ),
        make(
            "brand-shuofeng",
            "brand",
            "competitor",
            "朔风",
            aliases=["朔风汽车", "SHUOFENG"],
            owned_domains=["shuofeng-auto.example"],
        ),
        make(
            "product-shuofeng-t6",
            "product",
            "competitor",
            "朔风 T6",
            aliases=["朔风T6"],
            parent_entity_id="brand-shuofeng",
        ),
    ]


# (query_id, intent_type, business_level, priority, expects_recommendation, core_fact_ids, text)
PROMPT_SPECS: tuple[tuple[str, str, str, str, bool, list[str], str], ...] = (
    (
        "Q001",
        "brand",
        "L1",
        "P0",
        False,
        ["F-S7-LAUNCH"],
        "澜序汽车的定位和主要产品线是什么？",
    ),
    (
        "Q002",
        "brand",
        "L1",
        "P1",
        False,
        ["F-S7-WARRANTY"],
        "LANXU 品牌目前的市场口碑和用户反馈如何？",
    ),
    (
        "Q003",
        "product",
        "L1",
        "P0",
        False,
        ["F-S7-RANGE-CLTC", "F-S7-PRICE"],
        "澜序 S7 长续航版的续航里程和当前指导价是多少？",
    ),
    (
        "Q004",
        "product",
        "L1",
        "P0",
        False,
        ["F-S7-BATTERY", "F-S7-ACCEL"],
        "澜序 S7 的电池容量和零百加速表现怎么样？",
    ),
    (
        "Q005",
        "category",
        "L3",
        "P0",
        True,
        ["F-S7-RANGE-CLTC"],
        "20 万到 25 万元区间有哪些纯电中型 SUV 可以备选？",
    ),
    (
        "Q006",
        "category",
        "L3",
        "P1",
        True,
        ["F-E3-PRICE", "F-X5-PRICE"],
        "15 万元左右的纯电家用车一般看哪些车型？",
    ),
    (
        "Q007",
        "scenario",
        "L3",
        "P0",
        True,
        ["F-S7-RANGE-CLTC"],
        "经常周末跨城自驾的四口之家，选哪几款纯电 SUV 更合适？",
    ),
    (
        "Q008",
        "scenario",
        "L3",
        "P1",
        True,
        ["F-M9-RANGE-CLTC"],
        "日常通勤兼顾商务接待，30 万元级别纯电中大型车有哪些方案？",
    ),
    (
        "Q009",
        "problem",
        "L2",
        "P1",
        False,
        ["F-S7-RANGE-WLTC"],
        "冬季高速开暖风时，纯电 SUV 的实际续航一般会有多少衰减？",
    ),
    (
        "Q010",
        "problem",
        "L2",
        "P2",
        False,
        [],
        "没有固定私家充电桩的城市用户，日常使用纯电动车要注意什么？",
    ),
    (
        "Q011",
        "comparison",
        "L2",
        "P0",
        True,
        ["F-S7-RANGE-CLTC", "F-S7-PRICE"],
        "澜序 S7 和岑野 K5 在续航、空间和定价上各有什么特点？",
    ),
    (
        "Q012",
        "comparison",
        "L2",
        "P1",
        True,
        ["F-X5-RANGE-CLTC"],
        "澜序 X5 与朔风 T6 对比，日常代步选哪一台更均衡？",
    ),
    (
        "Q013",
        "decision",
        "L3",
        "P0",
        True,
        ["F-S7-PRICE"],
        "预算 22 万元左右买纯电 SUV，现阶段应该按什么顺序试驾？",
    ),
    (
        "Q014",
        "decision",
        "L3",
        "P1",
        True,
        ["F-E3-RANGE-CLTC"],
        "预算严格控制在 14 万元以内，纯电紧凑型车怎么选更稳妥？",
    ),
    (
        "Q015",
        "after_sales",
        "L1",
        "P0",
        False,
        ["F-S7-WARRANTY"],
        "澜序汽车的整车质保政策和三电保修周期怎么规定？",
    ),
    (
        "Q016",
        "after_sales",
        "L2",
        "P2",
        False,
        [],
        "纯电车型常规保养项目有哪些，大概的周期和花费是多少？",
    ),
    (
        "Q017",
        "risk",
        "L2",
        "P1",
        False,
        [],
        "购买新势力纯电 SUV 时，在电池衰减和车企售后方面需要留意哪些风险？",
    ),
    (
        "Q018",
        "risk",
        "L1",
        "P1",
        False,
        ["F-S7-PRICE"],
        "澜序 S7 近期是否调整过售价，早期车主和潜在买家需要注意什么？",
    ),
    (
        "Q019",
        "competitor",
        "L2",
        "P1",
        True,
        [],
        "岑野 S7 和岑野 K5 这两款车分别适合什么人群，同价位还有哪些备选？",
    ),
    (
        "Q020",
        "competitor",
        "L2",
        "P2",
        True,
        [],
        "朔风汽车主力纯电车型 T6 的综合表现如何，市场上有哪几个常见替代项？",
    ),
    (
        "Q021",
        "general",
        "L3",
        "P0",
        True,
        ["F-S7-RANGE-CLTC", "F-S7-PRICE"],
        "2026 年买一台兼顾续航、空间和质保的家用纯电 SUV，综合来看有哪些候选车型？",
    ),
    (
        "Q022",
        "general",
        "L2",
        "P1",
        True,
        [],
        "从续航标准、补能便利性和用车成本三个维度看，家用纯电 SUV 怎么评估？",
    ),
)


def build_prompts() -> list[dict[str, Any]]:
    prompts: list[dict[str, Any]] = []
    for qid, intent, level, priority, expects, fact_ids, text in PROMPT_SPECS:
        item: dict[str, Any] = {
            "query_id": qid,
            "text": text,
            "intent_type": intent,
            "business_level": level,
            "priority": priority,
            "source": "虚构汽车样例题库（合成）",
            "expects_recommendation": expects,
        }
        if fact_ids:
            item["core_fact_ids"] = fact_ids
        prompts.append(item)
    return prompts


# 每个回答场景的模板，返回 (answer_text, citations_text, fact_outcomes)
# fact_outcomes: list of (fact_id, claim_label, core_present_label, reason)
def _generate_answer(
    rng: random.Random,
    query_id: str,
    intent: str,
    core_fact_ids: list[str],
    platform: str,
    channel: str,
    window: str,
    attempt: int,
) -> tuple[str, str, list[tuple[str, str, str, str]]]:
    # 设定各通道稳定的提及概率（api ~0.82，manual ~0.76，跨窗口稳定、通道偏离小）
    base_prob = 0.82 if channel == "api" else 0.76
    if platform == "engine-b":
        base_prob -= 0.03
    if intent in ("brand", "product", "comparison"):
        mention_prob = min(0.95, base_prob + 0.12)
    elif intent in ("problem", "competitor"):
        mention_prob = max(0.45, base_prob - 0.18)
    else:
        mention_prob = base_prob

    mentioned = rng.random() < mention_prob
    alias = rng.choice(["澜序 S7", "澜序S7", "LANXU S7", "Lan-Xu S7"])
    brand_alias = rng.choice(["澜序", "澜序汽车", "LANXU", "Lan-Xu"])

    # 少数题出现同形异义词“波澜序曲”且未提及目标品牌，用于展示排除词效果
    if not mentioned:
        if query_id == "Q019":
            text = (
                "岑野 S7 定位偏运动，岑野 K5 空间更宽敞。同价位还可以对比朔风 T6，"
                "购车前建议结合实测续航与售后网点分布试驾[1]。"
            )
            cites = (
                "inline | https://cenye-auto.example/models | 岑野汽车车型总览（虚构）\n"
                "source_list | https://auto-review.example/suv-compare | 纯电 SUV 对比（虚构）"
            )
        elif query_id == "Q022" and attempt == 2:
            text = (
                "评测纯电 SUV 时，行业讨论常如波澜序曲般起伏，但核心还是看 CLTC 与 WLTC 差异、"
                "快充网络密度以及质保条款。车型方面可以先对比岑野 K5 和朔风 T6[1]。"
            )
            cites = "inline | https://auto-review.example/ev-guide | 家用纯电评估指南（虚构）"
        else:
            text = (
                "该需求下可以重点关注两款车型：\n"
                "1. 岑野 K5：后排腿部空间充裕，配置均衡[1]。\n"
                "2. 朔风 T6：底盘调校偏舒适，适合城市通勤。\n"
                "建议按自身充电条件预约试驾对比。"
            )
            cites = (
                "inline | https://auto-review.example/k5-t6 | 纯电家用横评（虚构）\n"
                "source_list | https://ev-forum.example/threads/ev-choice | 车主讨论（虚构）"
            )
        outcomes = [
            (fid, "unverifiable", "missing", "回答未提及目标车型，未覆盖该核心事实。")
            for fid in core_fact_ids
        ]
        return text, cites, outcomes

    # 决定是否偶尔出现“否定句”或“旧价/轻微误差”（保证准确率与呈现率仍远高于门槛）
    is_negated = query_id == "Q014" and attempt == 2
    price_wrong = query_id == "Q018" and window == "W1" and attempt == 1 and channel == "manual"
    range_wrong = query_id == "Q003" and window == "W2" and attempt == 1 and platform == "engine-b"

    if is_negated:
        text = (
            f"如果预算严格控制在 14 万元以内，不推荐 {alias}（售价 21.98 万元超出预算），"
            "可以优先考虑以下两款：\n"
            "1. 澜序 E3：CLTC 续航 560 公里，售价 13.58 万元[1]。\n"
            "2. 岑野 K5 入门版：城市代步够用。\n"
            "如果后续提高预算再看中型 SUV。"
        )
        cites = (
            "inline | https://lanxu-auto.example/e3/spec | 澜序 E3 参数页（虚构）\n"
            "source_list | https://news-auto.example/ev-14w | 14 万级纯电导购（虚构）"
        )
        outcomes = [
            (
                "F-E3-RANGE-CLTC",
                "supported",
                "present_correct",
                "CLTC 续航 560 km 与事实库一致。",
            )
        ]
        return text, cites, outcomes

    # 正常提及 + 推荐或客观介绍
    rank_style = rng.choice(["first", "first", "second"])
    s7_price = "23.98 万元" if price_wrong else "21.98 万元"
    s7_range = "680 公里" if range_wrong else "620 公里"

    if intent in ("category", "scenario", "comparison", "decision", "general", "competitor"):
        if rank_style == "first":
            text = (
                f"结合预算与续航需求，可以考虑以下三款车型：\n"
                f"1. {alias}：CLTC 续航 {s7_range}，当前售价 {s7_price}，82 kWh 电池兼顾长途与城市通勤[1]。\n"
                f"2. 岑野 K5：空间表现稳定，乘坐舒适[2]。\n"
                f"3. 朔风 T6：入门配置齐全。\n"
                f"{brand_alias}还提供澜序 X5（15.98 万元，CLTC 510 公里）与澜序 M9（32.98 万元，CLTC 650 公里）覆盖不同预算。"
            )
        else:
            text = (
                f"常见备选顺序如下：\n"
                f"1. 岑野 K5：保有量较稳，空间宽敞[2]。\n"
                f"2. {alias}：CLTC 续航 {s7_range}，售价 {s7_price}，整车质保 5 年或 15 万公里[1]。\n"
                f"3. 朔风 T6：城市代步能耗较低。\n"
                f"若预算在 15 万元左右也可对比澜序 X5（510 公里）或澜序 E3（13.58 万元）。"
            )
    else:
        text = (
            f"据公开参数资料，{brand_alias}主力纯电 SUV {alias} 于 2026-03-18 上市，"
            f"长续航版 CLTC 续航 {s7_range}、WLTC 续航 540 公里，电池容量 82 kWh，百公里加速 5.9 秒，"
            f"当前指导价 {s7_price}，整车质保 5 年或 15 万公里[1][2]。"
        )

    cites = (
        "inline | https://lanxu-auto.example/s7/spec | 澜序 S7 参数说明（虚构）\n"
        "inline | https://gov-data.example/ev/catalog-2026 | 新能源车型能耗目录（虚构）\n"
        "source_list | https://auto-review.example/2026-suv | 2026 纯电 SUV 评测（虚构）"
    )

    outcomes = []
    for fid in core_fact_ids:
        if fid == "F-S7-PRICE" and price_wrong:
            outcomes.append(
                (
                    fid,
                    "contradicted",
                    "present_wrong",
                    "回答仍使用已过期的旧价 23.98 万元，当前有效价为 21.98 万元。",
                )
            )
        elif fid == "F-S7-RANGE-CLTC" and range_wrong:
            outcomes.append(
                (
                    fid,
                    "contradicted",
                    "present_wrong",
                    "回答给出 CLTC 续航 680 km，与事实库 620 km 矛盾。",
                )
            )
        else:
            outcomes.append(
                (
                    fid,
                    "supported",
                    "present_correct",
                    f"回答陈述与事实库条目 {fid} 一致。",
                )
            )
    return text, cites, outcomes


def generate_dataset() -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, str]],
    list[dict[str, Any]],
]:
    rng = random.Random(SEED)
    proj = build_project()
    entities = build_entities()
    prompts = build_prompts()
    fact_map = {item["query_id"]: item.get("core_fact_ids", []) for item in prompts}
    intent_map = {item["query_id"]: item["intent_type"] for item in prompts}

    obs_rows: list[dict[str, str]] = []
    judgments: list[dict[str, Any]] = []
    obs_seq = 0
    jdg_seq = 0
    claim_seq = 0

    for window_id, date_str in WINDOWS:
        minute_offset = 0
        for platform_id, _, api_model in PLATFORMS:
            for channel in CHANNELS:
                for prompt in prompts:
                    qid = prompt["query_id"]
                    for attempt in ATTEMPTS:
                        obs_seq += 1
                        obs_id = f"obs-auto-{obs_seq:04d}"
                        hour = 2 + minute_offset // 60
                        minute = minute_offset % 60
                        minute_offset += 1
                        answer, cites, outcomes = _generate_answer(
                            rng,
                            qid,
                            intent_map[qid],
                            fact_map[qid],
                            platform_id,
                            channel,
                            window_id,
                            attempt,
                        )
                        is_api = channel == "api"
                        obs_rows.append(
                            {
                                "observation_id": obs_id,
                                "query_id": qid,
                                "platform": platform_id,
                                "channel": channel,
                                "entry_form": "api" if is_api else "web",
                                "model_version": api_model if is_api else "",
                                "entry_features": ""
                                if is_api
                                else f"{platform_id} 网页端默认模式，页面未展示模型名（虚构）",
                                "window_id": window_id,
                                "attempt": str(attempt),
                                "started_at": f"{date_str}T{hour:02d}:{minute:02d}:00Z",
                                "ended_at": f"{date_str}T{hour:02d}:{minute:02d}:18Z",
                                "logged_in": "false",
                                "history_cleared": "true",
                                "web_search": "true",
                                "region": "CN",
                                "device": "",
                                "answer_text": answer,
                                "citations": cites,
                                "status": "valid",
                                "invalid_reason": "",
                                "operator": "采集员甲（虚构）" if is_api else "采集员乙（虚构）",
                                "synthetic": "true",
                            }
                        )
                        # 只对每窗每次第 1 次尝试的带 core_fact_ids 观测写入判定样例，控制体积远小于 1 MB
                        if attempt != 1:
                            continue
                        for fid, a2f_label, f2a_label, reason in outcomes:
                            jdg_seq += 1
                            claim_seq += 1
                            judgments.append(
                                {
                                    "schema_version": "1.0",
                                    "judgment_id": f"jdg-auto-{jdg_seq:05d}",
                                    "project_id": PROJECT_ID,
                                    "direction": "answer_to_facts",
                                    "source": {"type": "observation", "id": obs_id},
                                    "query_id": qid,
                                    "claim_id": f"clm-auto-{claim_seq:05d}",
                                    "fact_id": None if a2f_label == "unverifiable" else fid,
                                    "fact_version": None if a2f_label == "unverifiable" else 1,
                                    "label": a2f_label,
                                    "claim_type": "numeric",
                                    "reason": reason,
                                    "as_of": date_str,
                                    "judge": {
                                        "type": "rule",
                                        "id": "geo-facts-judge",
                                        "version": "1.0",
                                    },
                                    "created_at": f"{date_str}T12:00:00Z",
                                    "synthetic": True,
                                    "ext": {"produced_by": "geo-facts"},
                                }
                            )
                            jdg_seq += 1
                            judgments.append(
                                {
                                    "schema_version": "1.0",
                                    "judgment_id": f"jdg-auto-{jdg_seq:05d}",
                                    "project_id": PROJECT_ID,
                                    "direction": "facts_to_answer",
                                    "source": {"type": "observation", "id": obs_id},
                                    "query_id": qid,
                                    "claim_id": None,
                                    "fact_id": fid,
                                    "fact_version": 1,
                                    "label": f2a_label,
                                    "claim_type": "numeric",
                                    "reason": reason,
                                    "as_of": date_str,
                                    "judge": {
                                        "type": "rule",
                                        "id": "geo-facts-judge",
                                        "version": "1.0",
                                    },
                                    "created_at": f"{date_str}T12:00:00Z",
                                    "synthetic": True,
                                    "ext": {"produced_by": "geo-facts"},
                                }
                            )

    # 追加 4 条无效观测（展示超时、限流、空回答、截断记为无效、不进指标分母）
    invalid_cases = (
        ("W1", "2026-10-08", "engine-a", "engine-a-2026.10", "Q001", "timeout", ""),
        ("W2", "2026-10-09", "engine-a", "engine-a-2026.10", "Q002", "rate_limited", ""),
        ("W2", "2026-10-09", "engine-b", "engine-b-2026.10", "Q003", "empty_answer", ""),
        (
            "W3",
            "2026-10-10",
            "engine-b",
            "engine-b-2026.10",
            "Q004",
            "truncated",
            "澜序 S7 采用 82 kWh 电池包，CLTC 续航……（回答截断）",
        ),
    )
    for idx, (wid, date_str, pid, model, qid, reason, partial) in enumerate(invalid_cases, start=1):
        obs_seq += 1
        obs_rows.append(
            {
                "observation_id": f"obs-auto-{obs_seq:04d}",
                "query_id": qid,
                "platform": pid,
                "channel": "api",
                "entry_form": "api",
                "model_version": model,
                "entry_features": "",
                "window_id": wid,
                "attempt": "3",
                "started_at": f"{date_str}T09:{idx:02d}:00Z",
                "ended_at": f"{date_str}T09:{idx:02d}:05Z",
                "logged_in": "false",
                "history_cleared": "true",
                "web_search": "true",
                "region": "CN",
                "device": "",
                "answer_text": partial,
                "citations": "",
                "status": "invalid",
                "invalid_reason": reason,
                "operator": "采集员甲（虚构）",
                "synthetic": "true",
            }
        )

    return proj, entities, prompts, obs_rows, judgments


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for record in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OBSERVATION_COLUMNS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    proj, entities, prompts, obs_rows, judgments = generate_dataset()
    _write_json(OUT_DIR / "project.json", proj)
    _write_jsonl(OUT_DIR / "entities.jsonl", entities)
    _write_jsonl(OUT_DIR / "prompts" / "draft.jsonl", prompts)
    _write_csv(OUT_DIR / "inbox" / "observations.csv", obs_rows)
    _write_jsonl(OUT_DIR / "inbox" / "judgments.jsonl", judgments)
    total = sum(p.stat().st_size for p in OUT_DIR.rglob("*") if p.is_file())
    print(
        f"已生成样例到 {OUT_DIR}：{len(entities)} 个实体、{len(prompts)} 道题、"
        f"{len(obs_rows)} 条观测、{len(judgments)} 条判定，总计 {total} 字节。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
