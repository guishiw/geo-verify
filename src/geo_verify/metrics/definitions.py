"""指标定义表：编号、名称、分子分母口径、单位和已知限制。

编号与顺序以 docs/cli.md 的“指标编号”表为准。本工具实现的口径版本统一为 1.0；
预先登记的口径版本与此不同时，该指标只能作为事后指标展示。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFINITION_VERSION = "1.0"

KIND_RATIO = "ratio"
KIND_QUERY = "query"
KIND_TEMPORAL = "temporal"
KIND_CROSS = "cross_platform"


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    title: str
    formula: str
    kind: str
    unit: str
    entity_scoped: bool
    higher_is_better: bool | None
    limitation: str


def _d(
    metric_id: str,
    title: str,
    formula: str,
    *,
    kind: str = KIND_RATIO,
    unit: str = "observation",
    entity: bool = False,
    better: bool | None = True,
    limitation: str = "",
) -> MetricDefinition:
    return MetricDefinition(metric_id, title, formula, kind, unit, entity, better, limitation)


_ANNOTATION_LIMIT = "依赖生效标注；没有标注的有效观测不进分母并单独计数。"
_CITATION_LIMIT = "引用按观测内 URL 去重；域名无法识别的引用不进分母。"
_JUDGMENT_LIMIT = "判定由事实核验工具给出，本工具不评价判定器自身的准确度。"

_DEFINITIONS = (
    _d(
        "mention_rate",
        "品牌提及率",
        "提及目标主体的有效观测 / 有效观测",
        entity=True,
        limitation=_ANNOTATION_LIMIT,
    ),
    _d(
        "intent_coverage",
        "意图覆盖率",
        "出现目标主体的题 / 封存题总数",
        kind=KIND_QUERY,
        unit="query",
        entity=True,
        limitation="每题提问次数越多越容易“出现过”，不同采集量之间不宜直接比较。",
    ),
    _d(
        "top1_rate",
        "首位推荐率",
        "位次为 1 的可排序观测 / 可排序观测",
        entity=True,
        limitation="只统计能明确识别推荐序列的回答，不以出现先后代替位次。",
    ),
    _d(
        "top3_rate",
        "前三推荐率",
        "位次 ≤ 3 的可排序观测 / 可排序观测",
        entity=True,
        limitation="只统计能明确识别推荐序列的回答，不以出现先后代替位次。",
    ),
    _d(
        "top5_rate",
        "前五推荐率",
        "位次 ≤ 5 的可排序观测 / 可排序观测",
        entity=True,
        limitation="只统计能明确识别推荐序列的回答，不以出现先后代替位次。",
    ),
    _d(
        "recommendation_rate",
        "推荐达成率（整体）",
        "推荐成功次数 / 应推荐次数",
        entity=True,
        limitation="“推荐成功”按预先登记的定义判断；“应推荐”取题目的 expects_recommendation。",
    ),
    _d(
        "recommendation_rate_daily",
        "推荐达成率（按日）",
        "推荐成功次数 / 应推荐次数（按项目时区的自然日）",
        entity=True,
        limitation="单日样本通常较少，仅用于逐日对账；验收判定请登记整体的推荐达成率。",
    ),
    _d(
        "target_source_presence",
        "目标信源出现率",
        "出现目标信源的有效观测 / 有效观测",
        limitation="目标信源 = 预先登记的信源 ∪ 目标主体自有域名（含子域名）。",
    ),
    _d(
        "target_source_share",
        "目标信源引用占比",
        "目标信源引用 / 全部可识别引用",
        unit="citation",
        limitation=_CITATION_LIMIT,
    ),
    _d(
        "citation_validity",
        "引用溯源有效率",
        "有效且相关的引用 / 已核验的可识别引用",
        unit="citation",
        limitation="只统计标注中已核验的引用；未核验数量单独列出，核验比例低时结论不能外推。",
    ),
    _d(
        "authority_share",
        "权威信源占比",
        "权威信源引用 / 全部可识别引用",
        unit="citation",
        limitation="权威等级按预先登记的域名分级规则判断；未登记等级的域名不算权威。",
    ),
    _d(
        "accuracy_verifiable",
        "回答准确率（可核验口径）",
        "支持 /（支持 + 矛盾）",
        unit="judgment",
        limitation=_JUDGMENT_LIMIT,
    ),
    _d(
        "accuracy_strict",
        "回答准确率（严格口径）",
        "支持 / 全部陈述",
        unit="judgment",
        limitation=_JUDGMENT_LIMIT,
    ),
    _d(
        "unverifiable_rate",
        "不可核验率",
        "不可核验 / 全部陈述",
        unit="judgment",
        better=False,
        limitation=_JUDGMENT_LIMIT,
    ),
    _d(
        "core_fact_presence",
        "核心信息呈现率",
        "正确呈现的核心事实 / 应呈现核心事实",
        unit="judgment",
        limitation="只包含配置了核心事实清单的题；" + _JUDGMENT_LIMIT,
    ),
    _d(
        "negative_miscitation_rate",
        "负面误引率",
        "含无依据或错误负面的观测 / 有效观测",
        entity=True,
        better=False,
        limitation="依赖标注中的风险字段；只识别无依据或错误的负面，不涉及真实负面。",
    ),
    _d(
        "sentiment_positive_share",
        "倾向分布：正面占比",
        "正面倾向的观测 / 有倾向标注的提及观测",
        entity=True,
        better=None,
        limitation="仅描述，不用于验收结论。",
    ),
    _d(
        "sentiment_neutral_share",
        "倾向分布：中性占比",
        "中性倾向的观测 / 有倾向标注的提及观测",
        entity=True,
        better=None,
        limitation="仅描述，不用于验收结论。",
    ),
    _d(
        "sentiment_negative_share",
        "倾向分布：负面占比",
        "负面倾向的观测 / 有倾向标注的提及观测",
        entity=True,
        better=None,
        limitation="仅描述，不用于验收结论。",
    ),
    _d(
        "temporal_std",
        "跨时间稳定性：标准差",
        "各时间窗口提及率的样本标准差",
        kind=KIND_TEMPORAL,
        unit="window",
        entity=True,
        better=False,
        limitation="窗口数很少时估计不稳定；不给区间。",
    ),
    _d(
        "temporal_range",
        "跨时间稳定性：极差",
        "各时间窗口提及率的最大值 − 最小值",
        kind=KIND_TEMPORAL,
        unit="window",
        entity=True,
        better=False,
        limitation="窗口数很少时估计不稳定；不给区间。",
    ),
    _d(
        "temporal_cv",
        "跨时间稳定性：变异系数",
        "各时间窗口提及率的样本标准差 / 均值",
        kind=KIND_TEMPORAL,
        unit="window",
        entity=True,
        better=False,
        limitation="均值为 0 时无定义；窗口数很少时估计不稳定；不给区间。",
    ),
    _d(
        "cross_platform_core_facts",
        "跨平台一致性：核心事实项",
        "各平台呈现状态一致的（题目，核心事实）/ 至少两个平台有判定的（题目，核心事实）",
        kind=KIND_CROSS,
        unit="fact",
        better=None,
        limitation="平台内正确呈现比例 ≥ 0.5 记为“正确呈现”；这是本项目的定义。",
    ),
    _d(
        "cross_platform_sources",
        "跨平台一致性：来源指向",
        "各平台首要引用域名相同的题 / 至少两个平台有可识别引用的题",
        kind=KIND_CROSS,
        unit="query",
        better=None,
        limitation="首要引用域名 = 平台内被最多观测引用的域名，并列时取字典序最小者；这是本项目的定义。",
    ),
    _d(
        "cross_platform_recommendation",
        "跨平台一致性：推荐逻辑",
        "各平台是否推荐目标主体结论一致的题 / 至少两个平台有标注观测的题",
        kind=KIND_CROSS,
        unit="query",
        entity=True,
        better=None,
        limitation="平台内推荐成功比例 ≥ 0.5 记为“推荐”；这是本项目的定义。",
    ),
    _d(
        "valid_observation_rate",
        "有效观测率",
        "有效观测 / 全部观测",
        limitation="“全部观测”指属于所选题库版本的观测，其他版本的观测单独计数。",
    ),
    _d(
        "environment_consistency_rate",
        "环境一致率",
        "与登记环境一致的有效观测 / 有效观测",
        limitation="登记为 null 的环境项不作要求；观测中未知的环境项按不一致计并单独计数。",
    ),
    _d(
        "raw_record_completeness",
        "原始记录完整率",
        "必备字段齐全的观测 / 全部观测",
        limitation="字段为空但写明原因视为齐全；只检查字段是否齐备，不检查内容真伪。",
    ),
    _d(
        "evidence_completeness",
        "证据完整率",
        "快照存在且哈希相符的有效观测 / 有效观测",
        limitation="哈希只能说明快照自记录后未改动，不能说明采集时间真实。",
    ),
    _d(
        "recheck_pass_rate",
        "抽样复核通过率",
        "复核一致的项 / 已复核的项",
        limitation="取自 recheck/result.json；复核项 =（观测，实体）。",
    ),
    _d(
        "risk_identification_rate",
        "风险识别率",
        "已识别且复核确认的风险 / 复核确认应识别的风险",
        entity=True,
        limitation="分母只含人工复核确认的风险；未复核的标注单独计数。",
    ),
    _d(
        "risk_closure_rate",
        "风险处置率",
        "已闭环的风险 / 已识别的风险",
        entity=True,
        limitation="“已识别”含复核否认的误报，误报数量单独列出。",
    ),
)

METRICS: dict[str, MetricDefinition] = {item.metric_id: item for item in _DEFINITIONS}
METRIC_IDS: tuple[str, ...] = tuple(METRICS)

# 预先登记时可以用一个组名覆盖一组指标。
ALIASES: dict[str, tuple[str, ...]] = {
    "sentiment_distribution": (
        "sentiment_positive_share",
        "sentiment_neutral_share",
        "sentiment_negative_share",
    ),
    "temporal_stability": ("temporal_std", "temporal_range", "temporal_cv"),
    "cross_platform_consistency": (
        "cross_platform_core_facts",
        "cross_platform_sources",
        "cross_platform_recommendation",
    ),
    "process_quality": (
        "valid_observation_rate",
        "environment_consistency_rate",
        "raw_record_completeness",
        "evidence_completeness",
        "recheck_pass_rate",
    ),
}
ACCURACY_ALIAS = "accuracy"

ROLE_LABELS = {
    "acceptance": "验收指标",
    "descriptive": "描述性指标",
    "post_hoc": "事后指标",
}


def resolve_registered_id(metric_id: str, accuracy_basis: str) -> tuple[str, ...]:
    """把登记用的编号（可能是组名）展开为本工具的指标编号；无法识别时返回空元组。"""
    if metric_id == ACCURACY_ALIAS:
        return (f"accuracy_{accuracy_basis}",)
    if metric_id in ALIASES:
        return ALIASES[metric_id]
    if metric_id in METRICS:
        return (metric_id,)
    return ()
