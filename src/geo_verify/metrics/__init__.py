"""指标：索引、定义表、计算引擎和结果文档。"""

from geo_verify.metrics.definitions import (
    ALIASES,
    DEFINITION_VERSION,
    METRIC_IDS,
    METRICS,
    ROLE_LABELS,
    MetricDefinition,
)
from geo_verify.metrics.document import compute_document, default_scopes
from geo_verify.metrics.engine import (
    INLINE_ID_LIMIT,
    Computation,
    Engine,
    Parameters,
    Scope,
    compute,
    ids_hash,
    parameters_for,
)
from geo_verify.metrics.exclusion import exclusion_audit, hypergeometric_tail
from geo_verify.metrics.index import ProjectIndex, build_index, domain_matches

__all__ = [
    "ALIASES",
    "DEFINITION_VERSION",
    "INLINE_ID_LIMIT",
    "METRICS",
    "METRIC_IDS",
    "ROLE_LABELS",
    "Computation",
    "Engine",
    "MetricDefinition",
    "Parameters",
    "ProjectIndex",
    "Scope",
    "build_index",
    "compute",
    "compute_document",
    "default_scopes",
    "domain_matches",
    "exclusion_audit",
    "hypergeometric_tail",
    "ids_hash",
    "parameters_for",
]
