"""采集适配器的公共部分。

所有适配器都产出符合 observation 契约的记录：写入前逐条校验，再以追加方式写入
observations.jsonl。无法获得的字段一律填 null 并在 null_reasons 写明原因，不编造默认值。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

from geo_verify import __version__, project, spec
from geo_verify.canon import sha256_file
from geo_verify.jsonl import append_jsonl

SCHEMA_VERSION = "1.0"
TOOL_NAME = "geo-verify"
PLACEMENTS = ("inline", "source_list")
CHANNELS = ("api", "manual", "share_import", "browser_assist", "file_import")
ENTRY_FORMS = ("api", "web", "app", "mini_program", "plugin", "other")
INVALID_REASONS = (
    "timeout",
    "rate_limited",
    "empty_answer",
    "truncated",
    "garbled",
    "error",
    "other",
)
NULLABLE_FIELDS = (
    "model_version",
    "logged_in",
    "history_cleared",
    "web_search",
    "region",
    "device",
    "answer_text",
    "operator",
)
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRUE = frozenset({"true", "1", "yes", "y", "是", "已", "已登录", "已清除", "开", "开启"})
_FALSE = frozenset({"false", "0", "no", "n", "否", "未", "未登录", "未清除", "关", "关闭"})


class FieldError(ValueError):
    """某一列（字段）的取值不合法。"""

    def __init__(self, column: str | None, reason: str) -> None:
        super().__init__(reason)
        self.column = column
        self.reason = reason


def collector(tool: str | None = None, version: str | None = None) -> dict[str, str]:
    return {"tool": tool or TOOL_NAME, "version": version or __version__}


def task_digest(
    project_id: str,
    prompt_set_hash: str,
    platform: str,
    channel: str,
    window: str,
    query_id: str,
    attempt: int,
) -> str:
    """一次采集任务的确定性摘要：同一任务无论何时重跑都得到同一个编号。"""
    raw = "|".join([project_id, prompt_set_hash, platform, channel, window, query_id, str(attempt)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def trace_id_for(*parts: Any) -> str:
    return "t-" + task_digest(*parts)


def observation_id_for(*parts: Any) -> str:
    return "o-" + task_digest(*parts)


def domain_of(url: str) -> str | None:
    """从 URL 解析域名（小写）；解析不出或不是合法主机名时返回 None。"""
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return host if _HOSTNAME.match(host) else None


def parse_bool(value: Any, column: str) -> bool | None:
    """解析“是否”类取值；空白返回 None（未知），无法识别时报错。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if not token:
            return None
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
    raise FieldError(column, f"无法识别的是否取值 {value!r}（可用 true/false、是/否，未知请留空）")


def to_utc(value: Any, column: str) -> tuple[str, datetime]:
    """把带时区的 ISO 8601 时间换算为 UTC（Z 结尾）。没有时区的时间拒绝猜测。"""
    if not isinstance(value, str) or not value.strip():
        raise FieldError(column, "必填，格式如 2026-10-08T02:00:00Z 或 2026-10-08T10:00:00+08:00")
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise FieldError(column, f"不是合法的 ISO 8601 时间：{text!r}") from None
    if moment.tzinfo is None:
        raise FieldError(
            column, f"时间缺少时区，拒绝猜测：{text!r}（请写成 …Z 或带 +08:00 这样的偏移）"
        )
    moment = moment.astimezone(UTC)
    fraction = f".{moment.microsecond:06d}".rstrip("0") if moment.microsecond else ""
    return f"{moment:%Y-%m-%dT%H:%M:%S}{fraction}Z", moment


def text_or_none(value: Any, column: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FieldError(column, f"应为文本，实际为 {type(value).__name__}")
    return value if value.strip() else None


def parse_citations(value: Any, column: str = "citations") -> list[dict[str, Any]]:
    """把引用列解析为契约规定的列表。

    接受三种写法：已是列表；JSON 数组字符串；或每行一条的简写
    `placement | url | 标题`（标题可省）。每条引用都必须写明 placement：
    inline=正文引用，source_list=仅出现在来源列表。重复引用原样保留。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    items: Any = value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                items = json.loads(text)
            except json.JSONDecodeError as exc:
                raise FieldError(column, f"不是合法的 JSON 数组：{exc.msg}") from None
        else:
            items = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                parts = [part.strip() for part in line.split("|", 2)]
                if len(parts) < 2:
                    raise FieldError(column, f"简写格式应为“placement | url | 标题”：{line!r}")
                items.append(
                    {
                        "placement": parts[0],
                        "url": parts[1],
                        "title": parts[2] if len(parts) > 2 else None,
                    }
                )
    if not isinstance(items, list):
        raise FieldError(column, "应为引用列表")
    citations: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise FieldError(column, f"第 {index} 条引用应为对象（含 url 和 placement）")
        unknown = sorted(set(item) - {"order", "url", "domain", "title", "placement"})
        if unknown:
            raise FieldError(column, f"第 {index} 条引用含未知字段：{', '.join(unknown)}")
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            raise FieldError(column, f"第 {index} 条引用缺少 url")
        placement = item.get("placement")
        if placement not in PLACEMENTS:
            raise FieldError(
                column,
                f"第 {index} 条引用必须写明 placement（inline=正文引用，source_list=仅在来源列表）",
            )
        title = item.get("title")
        if isinstance(title, str) and not title.strip():
            title = None
        citations.append(
            {
                "order": item.get("order", index),
                "url": url.strip(),
                "domain": item["domain"] if "domain" in item else domain_of(url),
                "title": title,
                "placement": placement,
            }
        )
    return citations


def snapshot_entry(
    root: Path, relpath: str, *, sha256: str | None = None, media_type: str | None = None
) -> dict[str, str]:
    """生成一条快照引用。文件存在时计算 SHA-256 并与声明值核对。"""
    clean = relpath.strip().replace("\\", "/")
    if not clean or clean.startswith("/") or ".." in clean.split("/"):
        raise FieldError("snapshots", f"快照路径必须是项目目录内的相对路径：{relpath!r}")
    if sha256 is not None and not _SHA256.match(sha256):
        raise FieldError("snapshots", f"快照 {clean} 的 sha256 不是 64 位小写十六进制")
    target = root / clean
    if target.is_file():
        actual = sha256_file(target)
        if sha256 is not None and sha256 != actual:
            raise FieldError("snapshots", f"快照 {clean} 的 sha256 与文件实际内容不符")
        sha256 = actual
    elif sha256 is None:
        raise FieldError("snapshots", f"快照文件不存在，且未提供 sha256：{clean}")
    guessed = mimetypes.guess_type(clean)[0]
    return {
        "path": clean,
        "sha256": sha256,
        "media_type": media_type or guessed or "application/octet-stream",
    }


def fill_null_reasons(observation: dict[str, Any], defaults: dict[str, str]) -> None:
    """为取值为 null 的字段（以及空的快照列表）补上原因；已有原因的不覆盖。"""
    reasons: dict[str, str] = dict(observation.get("null_reasons") or {})
    for name in NULLABLE_FIELDS:
        if observation.get(name) is None and name not in reasons and name in defaults:
            reasons[name] = defaults[name]
    if not observation.get("snapshots") and "snapshots" not in reasons and "snapshots" in defaults:
        reasons["snapshots"] = defaults["snapshots"]
    if reasons:
        observation["null_reasons"] = reasons


def scope_problem(proj: dict[str, Any], platform: str, channel: str) -> tuple[str, str] | None:
    """平台和通道是否在 project.json 登记的范围内；不在则返回 (字段名, 说明)。"""
    registered = {item["platform_id"]: item["channels"] for item in proj["platforms"]}
    if platform not in registered:
        return (
            "platform",
            f"平台 {platform} 未在 project.json 的 platforms 中登记（已登记：{'、'.join(registered)}）",
        )
    if channel not in registered[platform]:
        return (
            "channel",
            f"通道 {channel} 未在 project.json 中为平台 {platform} 登记"
            f"（已登记：{'、'.join(registered[platform])}）",
        )
    return None


def observation_issues(observation: dict[str, Any]) -> list[spec.SchemaIssue]:
    return spec.iter_issues("observation", observation)


class ObservationSink:
    """只追加的观测写入器：按 trace_id 去重、分批落盘、统计有效与无效条数。"""

    def __init__(self, root: str | Path, *, batch_size: int = 200) -> None:
        self._path = project.paths(root).observations
        existing = project.load_observations(root)
        self._traces: set[str] = {item["trace_id"] for item in existing}
        self._ids: dict[str, str] = {item["observation_id"]: item["trace_id"] for item in existing}
        self._buffer: list[dict[str, Any]] = []
        self._batch_size = batch_size
        self.written = 0
        self.valid = 0
        self.invalid_by_reason: Counter[str] = Counter()

    def has_trace(self, trace_id: str) -> bool:
        return trace_id in self._traces

    def id_conflict(self, observation_id: str, trace_id: str) -> bool:
        """同一个 observation_id 是否已被另一个 trace_id 占用。"""
        return self._ids.get(observation_id, trace_id) != trace_id

    def add(self, observation: dict[str, Any]) -> None:
        """加入一条已通过契约校验的观测。"""
        self._traces.add(observation["trace_id"])
        self._ids[observation["observation_id"]] = observation["trace_id"]
        self._buffer.append(observation)
        self.written += 1
        if observation["status"] == "valid":
            self.valid += 1
        else:
            self.invalid_by_reason[observation["invalid_reason"]] += 1
        if len(self._buffer) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if self._buffer:
            append_jsonl(self._path, self._buffer)
            self._buffer = []

    def counts(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "valid": self.valid,
            "invalid": sum(self.invalid_by_reason.values()),
            "invalid_by_reason": dict(sorted(self.invalid_by_reason.items())),
        }

    def __enter__(self) -> ObservationSink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.flush()
