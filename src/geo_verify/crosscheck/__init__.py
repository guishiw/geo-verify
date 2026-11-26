"""双通道交叉验证：偏离度公式（本项目的定义）、样本充分性与通过判定。"""

from geo_verify.crosscheck.check import (
    EXIT_CODES,
    STATUS_LABELS,
    SUPPORTED_METRICS,
    Config,
    channel_names,
    crosscheck_document,
    resolve_config,
)
from geo_verify.crosscheck.formula import (
    FORMULA,
    FORMULA_NOTE,
    FORMULA_TEXT,
    deviation_from_counts,
    passes_threshold,
    symmetric_relative_difference,
)

__all__ = [
    "EXIT_CODES",
    "FORMULA",
    "FORMULA_NOTE",
    "FORMULA_TEXT",
    "STATUS_LABELS",
    "SUPPORTED_METRICS",
    "Config",
    "channel_names",
    "crosscheck_document",
    "deviation_from_counts",
    "passes_threshold",
    "resolve_config",
    "symmetric_relative_difference",
]
