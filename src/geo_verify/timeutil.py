"""时间处理：存储一律 UTC ISO 8601；按日统计使用项目时区。"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from geo_verify.errors import InputError

DEFAULT_TIMEZONE = "Asia/Shanghai"

_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


def parse_utc(text: str) -> datetime:
    """解析契约规定的 UTC 时间（必须以 Z 结尾）。"""
    if not isinstance(text, str) or not _UTC_PATTERN.match(text):
        raise InputError(f"时间必须是以 Z 结尾的 UTC ISO 8601：{text!r}")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"非法时间：{text!r}") from exc


def format_utc(moment: datetime) -> str:
    """输出契约规定的 UTC 时间字符串（秒级，带 Z）。"""
    if moment.tzinfo is None:
        raise InputError("时间缺少时区信息，拒绝猜测")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InputError(f"未知时区：{name!r}") from exc


def local_date(text: str, tz_name: str = DEFAULT_TIMEZONE) -> date:
    """UTC 时间在项目时区下所属的自然日。"""
    return parse_utc(text).astimezone(zone(tz_name)).date()
