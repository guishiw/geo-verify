"""异常识别与引用域名变化（V8）。

比较两个窗口（或项目时区下的两个自然日），识别提及率骤降与掉位、错误负面扩散、
引用大面积失效、竞品异常提升、同平台口径冲突；引用域名变化只作为待核查的相关线索。
"""

from geo_verify.alerts.change import (
    Change,
    Count,
    mean_shift,
    mean_shift_p_value,
    proportion_change,
    two_proportion_p_value,
)
from geo_verify.alerts.document import compute_alerts
from geo_verify.alerts.rules import RULES
from geo_verify.alerts.thresholds import DEFAULT_THRESHOLDS, resolve_thresholds
from geo_verify.alerts.wording import FORBIDDEN_TERMS, ensure_clean, find_forbidden

__all__ = [
    "DEFAULT_THRESHOLDS",
    "FORBIDDEN_TERMS",
    "RULES",
    "Change",
    "Count",
    "compute_alerts",
    "ensure_clean",
    "find_forbidden",
    "mean_shift",
    "mean_shift_p_value",
    "proportion_change",
    "resolve_thresholds",
    "two_proportion_p_value",
]
