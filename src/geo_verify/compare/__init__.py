"""基线对比：同题配对的差值、区间与结论；题库或模型版本变化时拒绝比较。"""

from geo_verify.compare.baseline import (
    CONCLUSION_CODES,
    DEFAULT_METRIC,
    EXIT_CODES,
    LIMITATIONS,
    METHOD,
    STATUS_LABELS,
    SUPPORTED_METRICS,
    Limits,
    compare_document,
    compare_pooled,
    refusal_for,
    resolve_metrics,
)

__all__ = [
    "CONCLUSION_CODES",
    "DEFAULT_METRIC",
    "EXIT_CODES",
    "LIMITATIONS",
    "METHOD",
    "STATUS_LABELS",
    "SUPPORTED_METRICS",
    "Limits",
    "compare_document",
    "compare_pooled",
    "refusal_for",
    "resolve_metrics",
]
