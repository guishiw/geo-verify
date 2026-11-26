"""报告用到的固定措辞与展示标签。

措辞红线：不得出现对第三方平台结果的保证、不得自称第三方验证、API 通道不得称作用户所见的界面结果，
不使用“官方”一类措辞。测试会对生成的报告全文做断言。
"""

from __future__ import annotations

NOTICE = "本报告为工具预检结论，不构成独立第三方验证"
WATERMARK = "合成数据，仅供演示，不得作为真实效果宣传"
FORMULA_DECLARATION = "此公式是本项目的定义"
DEFAULT_FORMULA_TEXT = "偏离度 = |A − B| / ((A + B) / 2)；A = B = 0 时为 0"
DEFAULT_FORMULA_NOTE = "此公式是本项目的定义（047 给出了 30% 的阈值，未给出公式）。"
POST_HOC_LABEL = "事后指标，不能用于验收结论"

STATUS_LABELS = {
    "pass": "通过",
    "fail": "不通过",
    "warning": "有保留",
    "insufficient": "数据不足",
    "not_run": "未执行",
}
VERDICT_LABELS = {
    "pass": "通过",
    "conditional_pass": "有条件通过",
    "fail": "不通过",
    "insufficient_data": "数据不足",
}
CHANNEL_LABELS = {
    "api": "API 通道",
    "manual": "人工采集通道",
    "share_import": "分享数据导入通道",
    "browser_assist": "浏览器辅助采集通道",
    "file_import": "文件导入通道",
}
METHOD_LABELS = {
    "wilson": "Wilson 区间",
    "cluster_bootstrap": "按题聚类 bootstrap",
    "paired_cluster_bootstrap": "按题配对的聚类 bootstrap",
}
ROLE_LABELS = {"acceptance": "验收指标", "descriptive": "描述性指标", "post_hoc": "事后指标"}
USE_LABELS = {
    "acceptance": "参与验收结论",
    "display_only": "分范围结果，仅展示",
    "descriptive": "描述性指标，不参与验收结论",
    "post_hoc": POST_HOC_LABEL,
}
CROSSCHECK_LABELS = {"pass": "通过", "fail": "不通过", "undetermined": "不可判定"}
COMPARE_LABELS = {"ok": "已比较", "refused": "拒绝比较", "insufficient": "样本不足"}
ALERT_LABELS = {"alert": "有告警", "clear": "无告警", "insufficient": "无法比较"}
EVIDENCE_LABELS = {"pass": "通过", "fail": "不通过"}

# 指标名称与 docs/cli.md 的“指标编号”表一致；表外的编号原样展示。
METRIC_TITLES = {
    "mention_rate": "品牌提及率",
    "intent_coverage": "意图覆盖率",
    "top1_rate": "首位推荐率",
    "top3_rate": "前三推荐率",
    "top5_rate": "前五推荐率",
    "recommendation_rate": "推荐达成率（整体）",
    "recommendation_rate_daily": "推荐达成率（按日）",
    "target_source_presence": "目标信源出现率",
    "target_source_share": "目标信源引用占比",
    "citation_validity": "引用溯源有效率",
    "authority_share": "权威信源占比",
    "accuracy_verifiable": "回答准确率（可核验口径）",
    "accuracy_strict": "回答准确率（严格口径）",
    "unverifiable_rate": "不可核验率",
    "core_fact_presence": "核心信息呈现率",
    "negative_miscitation_rate": "负面误引率",
    "sentiment_positive_share": "正面倾向占比",
    "sentiment_neutral_share": "中性倾向占比",
    "sentiment_negative_share": "负面倾向占比",
    "temporal_std": "跨时间稳定性（标准差）",
    "temporal_range": "跨时间稳定性（极差）",
    "temporal_cv": "跨时间稳定性（变异系数）",
    "cross_platform_core_facts": "跨平台一致性（核心事实项）",
    "cross_platform_sources": "跨平台一致性（来源指向）",
    "cross_platform_recommendation": "跨平台一致性（推荐逻辑）",
    "valid_observation_rate": "有效观测率",
    "environment_consistency_rate": "环境一致率",
    "raw_record_completeness": "原始记录完整率",
    "evidence_completeness": "证据完整率",
    "recheck_pass_rate": "抽样复核通过率",
    "risk_identification_rate": "风险识别率",
    "risk_closure_rate": "风险处置率",
    # 预先登记时可用的组名
    "accuracy": "回答准确率（按登记口径）",
    "sentiment_distribution": "倾向分布",
    "temporal_stability": "跨时间稳定性",
    "cross_platform_consistency": "跨平台一致性",
    "process_quality": "过程质量",
}

# 预先登记的组名 → 指标编号（metrics.json 没有给出 registered_metrics 时使用，与 docs/cli.md 一致）
REGISTERED_GROUPS = {
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


def metric_title(metric_id: str) -> str:
    return METRIC_TITLES.get(metric_id, metric_id)


def channel_label(channel: str | None) -> str | None:
    if channel is None:
        return None
    return CHANNEL_LABELS.get(channel, channel)


def channel_entry(channel: str) -> dict[str, str | None]:
    return {"channel": channel, "label": channel_label(channel)}
