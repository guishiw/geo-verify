"""选出要比较的两个对象：两个时间窗口，或项目时区下的两个自然日（“昨日与今日”）。

- 只看有效观测（取自指标索引：status 为 valid、属于所选封存题库版本、未被审计剔除）；
- 窗口按窗口内最早一条有效观测的时间排序（时间相同按窗口编号），默认比较最后两个；
- 只给一侧时取相邻的另一侧：只给当前窗口取它前一个窗口，只给基线窗口取它后一个窗口；
  只给当前日取前一天，只给基线日取后一天；
- 可比较的对象不足两个、或某一侧没有有效观测时“无法比较”（数据不足，退出码 3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from geo_verify.errors import InputError, InsufficientData
from geo_verify.metrics.index import BoolArray, ProjectIndex
from geo_verify.timeutil import parse_utc

MODE_WINDOW = "window"
MODE_DATE = "date"
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Period:
    """比较的一侧：mask 标出其中的有效观测（已按 --platform 过滤）。"""

    mode: str
    label: str
    mask: BoolArray
    first_observed_at: str | None
    last_observed_at: str | None

    @property
    def size(self) -> int:
        return int(np.count_nonzero(self.mask))

    def describe(self, side: str) -> str:
        """如“基线窗口 W1”“当前日 2026-10-10”。"""
        noun = "窗口" if self.mode == MODE_WINDOW else "日"
        return f"{side}{noun} {self.label}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.label if self.mode == MODE_WINDOW else None,
            "date": self.label if self.mode == MODE_DATE else None,
            "valid_observations": self.size,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
        }


@dataclass(frozen=True)
class Comparison:
    mode: str
    baseline: Period
    current: Period
    platform: str | None
    available_windows: list[str]


def _period(index: ProjectIndex, mode: str, label: str, mask: BoolArray) -> Period:
    # 同一时刻可能有不同写法（如带不带毫秒）：时间相同时按字符串排序，保证结果确定
    started = sorted(
        {str(item) for item in index.started[mask].tolist()},
        key=lambda text: (parse_utc(text), text),
    )
    return Period(
        mode,
        label,
        mask,
        started[0] if started else None,
        started[-1] if started else None,
    )


def ordered_windows(index: ProjectIndex, base: BoolArray) -> list[str]:
    """有有效观测的窗口，按窗口内最早观测时间排序。"""
    keys = []
    for code in np.unique(index.window[base]).tolist():
        rows = base & (index.window == code)
        earliest = min(parse_utc(str(item)) for item in set(index.started[rows].tolist()))
        keys.append((earliest, index.windows[int(code)]))
    return [label for _, label in sorted(keys)]


def _parse_date(text: str, option: str) -> date:
    if not _DATE_PATTERN.match(text):
        raise InputError(f"{option} 的日期格式应为 YYYY-MM-DD：{text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InputError(f"{option} 不是有效的日期：{text!r}") from exc


def _platform_mask(index: ProjectIndex, platform: str | None) -> BoolArray:
    if platform is None:
        return np.ones(index.size, dtype=np.bool_)
    registered = {item["platform_id"] for item in index.project["platforms"]}
    if platform not in registered and platform not in index.platforms:
        known = ", ".join(sorted(registered | set(index.platforms)))
        raise InputError(f"未知平台：{platform}；可选：{known}")
    return index.scope_mask(platform=platform)


def select_comparison(
    index: ProjectIndex,
    *,
    baseline_window: str | None = None,
    current_window: str | None = None,
    baseline_date: str | None = None,
    current_date: str | None = None,
    platform: str | None = None,
) -> Comparison:
    base: BoolArray = index.valid & _platform_mask(index, platform)
    windows = ordered_windows(index, base)
    uses_dates = baseline_date is not None or current_date is not None
    if uses_dates and (baseline_window is not None or current_window is not None):
        raise InputError(
            "按窗口比较（--baseline-window / --current-window）与按自然日比较不能同时使用"
        )
    if uses_dates:
        pair = _date_pair(index, base, baseline_date, current_date, windows)
    else:
        pair = _window_pair(index, base, baseline_window, current_window, windows)
    return Comparison(MODE_DATE if uses_dates else MODE_WINDOW, pair[0], pair[1], platform, windows)


def _window_pair(
    index: ProjectIndex,
    base: BoolArray,
    baseline: str | None,
    current: str | None,
    windows: list[str],
) -> tuple[Period, Period]:
    details = {"available_windows": windows}
    for option, value in (("--baseline-window", baseline), ("--current-window", current)):
        if value is not None and value not in index.windows:
            known = ", ".join(windows) or "无"
            raise InputError(f"{option}：没有窗口 {value}；有有效观测的窗口：{known}")
        if value is not None and value not in windows:
            raise InsufficientData(f"窗口 {value} 没有有效观测，无法比较", details=details)
    if baseline is not None and baseline == current:
        raise InputError("基线窗口与当前窗口不能相同")
    if current is not None and baseline is not None:
        labels = (baseline, current)
    elif current is not None:
        position = windows.index(current)
        if position == 0:
            raise InsufficientData(f"窗口 {current} 之前没有其他窗口，无法比较", details=details)
        labels = (windows[position - 1], current)
    elif baseline is not None:
        position = windows.index(baseline)
        if position == len(windows) - 1:
            raise InsufficientData(f"窗口 {baseline} 之后没有其他窗口，无法比较", details=details)
        labels = (baseline, windows[position + 1])
    elif len(windows) < 2:
        raise InsufficientData(
            f"有有效观测的窗口只有 {len(windows)} 个，无法比较；"
            "也可以用 --baseline-date / --current-date 按项目时区的自然日比较",
            details=details,
        )
    else:
        labels = (windows[-2], windows[-1])

    def period(label: str) -> Period:
        return _period(index, MODE_WINDOW, label, base & index.scope_mask(window_id=label))

    return period(labels[0]), period(labels[1])


def _date_pair(
    index: ProjectIndex,
    base: BoolArray,
    baseline: str | None,
    current: str | None,
    windows: list[str],
) -> tuple[Period, Period]:
    if current is not None:
        current_day = _parse_date(current, "--current-date")
        baseline_day = (
            _parse_date(baseline, "--baseline-date")
            if baseline is not None
            else current_day - timedelta(days=1)
        )
    else:
        baseline_day = _parse_date(str(baseline), "--baseline-date")
        current_day = baseline_day + timedelta(days=1)
    if baseline_day == current_day:
        raise InputError("基线日与当前日不能相同")
    periods = tuple(
        _period(index, MODE_DATE, day.isoformat(), base & index.scope_mask(date=day.isoformat()))
        for day in (baseline_day, current_day)
    )
    empty = [period.label for period in periods if period.size == 0]
    if empty:
        raise InsufficientData(
            f"{'、'.join(empty)} 没有有效观测（按项目时区 {index.project['timezone']} 的自然日），无法比较",
            details={
                "baseline_date": periods[0].label,
                "current_date": periods[1].label,
                "baseline_observations": periods[0].size,
                "current_observations": periods[1].size,
                "available_windows": windows,
            },
        )
    return periods[0], periods[1]
