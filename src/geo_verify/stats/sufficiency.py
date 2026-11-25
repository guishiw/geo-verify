"""样本充分性。

默认要求：时间窗口 ≥ 3 个，且该指标的有效观测 ≥ 30 次（阈值取自预先登记内容）。
不满足时不能给出肯定结论。
"""

from __future__ import annotations

from typing import Any

DEFAULT_MIN_WINDOWS = 3
DEFAULT_MIN_VALID_OBSERVATIONS = 30


def assess_sufficiency(
    *,
    windows: int,
    valid_observations: int,
    min_windows: int = DEFAULT_MIN_WINDOWS,
    min_valid_observations: int = DEFAULT_MIN_VALID_OBSERVATIONS,
) -> dict[str, Any]:
    """返回 metric_result 契约中的 sufficiency 对象。"""
    reasons: list[str] = []
    if windows < min_windows:
        reasons.append(f"时间窗口不足 {min_windows} 个（实际 {windows} 个）")
    if valid_observations < min_valid_observations:
        reasons.append(f"有效观测不足 {min_valid_observations} 次（实际 {valid_observations} 次）")
    return {
        "sufficient": not reasons,
        "windows": windows,
        "valid_observations": valid_observations,
        "reasons": reasons,
    }
