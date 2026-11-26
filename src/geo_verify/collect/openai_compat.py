"""通用 OpenAI 兼容 API 适配器：`collect --adapter openai`。

默认只连接进程内的模拟服务：`--asgi-app <模块或文件>:<对象>` 指定的 ASGI 应用经
httpx.ASGITransport 调用，不占端口、不联网。没有 `--asgi-app` 又没有 `--live` 时拒绝运行。
真实调用必须显式 `--live`，并同时给出 `--base-url` 和 `--api-key-env`；密钥只从该环境变量
读取，只出现在请求头里，不写入观测、快照、审计事件和任何输出（响应里回显的密钥也会被抹去）。

每个任务（题目 × 第几次）发一次 `POST {base_url}/chat/completions`，请求体为
`{"model": …, "messages": [{"role": "user", "content": 题目原文}]}`，请求头
`X-Request-Id: <trace_id>`。观测的 channel 与 entry_form 固定为 api：这是 API 通道的数据，
只反映接口返回的内容。

故障一律写成无效观测（status = invalid + invalid_reason），供计数，不进入指标分母：
超时（客户端超时、HTTP 408 / 504）→ timeout；HTTP 429 有限次重试后仍为 429 → rate_limited；
空回答 → empty_answer；finish_reason 为 length → truncated；乱码 → garbled；
其他 HTTP 错误、非法 JSON、缺少 choices 等 → error。

trace_id 由（项目、题库哈希、平台、通道、窗口、题目、次数）确定生成。重跑同一命令时，
已有记录的任务（无论有效还是无效）直接跳过、不再请求，因此可以续跑且不重复计数。
无效观测是原始记录，不能被覆盖或“重试成有效”；需要重问时，以新的次数序号采集
（增大 --repeats）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from geo_verify import __version__, audit, project, spec
from geo_verify.canon import sha256_bytes, sha256_file
from geo_verify.cli import CommandResult
from geo_verify.collect.adapter import AdapterSpec, reject_options, require_options
from geo_verify.collect.base import (
    SCHEMA_VERSION,
    ObservationSink,
    collector,
    domain_of,
)
from geo_verify.collect.platforms import PlatformConfig, platform_config, require_verified
from geo_verify.collect.tasks import Task, TaskPlan, plan_tasks, split_ids
from geo_verify.errors import InputError
from geo_verify.timeutil import format_utc, parse_utc

NAME = "openai"
CHANNEL = "api"
ENTRY_FORM = "api"
DEFAULT_BASE_URL = "http://mock.invalid/v1"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_WAIT = 5.0
# 乱码判定：去掉空白字符后，U+FFFD（替换字符）与控制字符（Unicode 类别 Cc、Cs）占全部字符的
# 比例达到 20% 即判为乱码。正常回答里几乎不会出现这类字符；偶发一两个替换字符（个别字节解码
# 失败）不至于让整段回答作废，而编码错误或二进制内容会让它们成片出现。
GARBLED_RATIO = 0.2
# 响应体上限：超过即放弃读取，记为 error，不保存半截内容
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REDACTED = "[REDACTED]"
TIMEOUT_STATUSES = frozenset({408, 504})
RATE_LIMITED_STATUS = 429
SNAPSHOTS_DIR = "snapshots"
MAX_DETAIL = 200
_REPLACEMENT = chr(0xFFFD)
_BAD_CATEGORIES = frozenset({"Cc", "Cs"})
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")

_FIXED_NULL_REASONS = {
    "logged_in": "API 通道：接口调用不经过网页或应用登录，没有登录状态可记录",
    "history_cleared": (
        "API 通道：每次请求只发送本题一条用户消息，不存在网页端的对话历史，无法按“是否清除历史”记录"
    ),
    "region": "API 通道：无法确定服务端或网络出口所在地域",
    "device": "API 通道：不经过终端设备上的网页或应用界面",
}
_WEB_SEARCH_REASON = "未用 --web-search 声明是否联网，接口响应也不包含这一信息"
_OPERATOR_REASON = "未用 --operator 指明采集人"
_DEFAULT_ACTOR = "geo-verify collect（未指明操作人）"


# ---------------------------------------------------------------- 可替换的连接方式


def asgi_transport(app: Any) -> httpx.AsyncBaseTransport:
    """进程内 ASGI 传输：请求直接交给应用对象处理，不经过任何网络。"""
    return httpx.ASGITransport(app=app, raise_app_exceptions=False)


def live_transport() -> httpx.AsyncBaseTransport:
    """真实网络传输，只在 --live 时使用。不做传输层重试，也不读取代理等环境配置。"""
    return httpx.AsyncHTTPTransport(retries=0, trust_env=False)


async def retry_sleep(seconds: float) -> None:
    """HTTP 429 重试前的等待。"""
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------- 参数


class Secret:
    """只放进请求头的密钥。repr / str 都不暴露内容，避免误写进日志或报错信息。"""

    __slots__ = ("_tokens", "_value")

    def __init__(self, value: str) -> None:
        self._value = value
        # 响应里可能以原文或 JSON 转义后的形式回显密钥，几种写法都要抹去
        forms = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
        self._tokens = tuple(form.encode("utf-8") for form in sorted(forms))

    def __repr__(self) -> str:
        return "Secret(已隐藏)"

    __str__ = __repr__

    def header(self) -> str:
        return f"Bearer {self._value}"

    def redact(self, data: bytes) -> tuple[bytes, bool]:
        found = False
        for token in self._tokens:
            if token in data:
                data = data.replace(token, REDACTED.encode("ascii"))
                found = True
        return data, found


@dataclass(frozen=True)
class Settings:
    platform: str
    window: str
    repeats: int
    queries: list[str] | None
    transport: str  # "asgi" | "live"
    app_label: str | None
    base_url: str
    model: str | None
    web_search: bool | None
    extra_body: dict[str, Any]
    timeout: float
    max_retries: int
    retry_wait: float
    operator: str | None
    now: str | None
    resume: bool
    api_key_env: str | None
    platform_config: PlatformConfig | None
    warnings: tuple[str, ...] = ()


def load_asgi_app(ref: str) -> tuple[Any, str]:
    """按 `<模块>:<对象>` 或 `<文件.py>:<对象>` 载入 ASGI 应用，返回 (应用, 记录用的标签)。

    标签只含模块名或文件名，不含目录，避免把本机路径写进结果文件。
    同一个文件已作为模块载入时直接复用，不重复执行。
    """
    target, sep, attr = ref.strip().rpartition(":")
    if not sep or not target or not attr:
        raise InputError(
            "--asgi-app 应写成 <模块或文件>:<对象>，如 mock_engines.app:app 或 path/to/app.py:app"
        )
    if target.endswith(".py") or "/" in target or os.sep in target:
        path = Path(target)
        label = f"{path.name}:{attr}"
        module = _load_file(path, label)
    else:
        label = f"{target}:{attr}"
        try:
            module = importlib.import_module(target)
        except Exception as exc:
            raise InputError(
                f"--asgi-app 无法导入模块 {target}：{type(exc).__name__}: {exc}"
            ) from exc
    obj: Any = module
    for part in attr.split("."):
        if not hasattr(obj, part):
            raise InputError(f"--asgi-app：{label} 中没有对象 {attr}")
        obj = getattr(obj, part)
    if not callable(obj):
        raise InputError(f"--asgi-app：{label} 不是可调用的 ASGI 应用")
    return obj, label


def _load_file(path: Path, label: str) -> Any:
    if not path.is_file():
        raise InputError(f"--asgi-app 指定的文件不存在：{path.name}")
    resolved = path.resolve()
    for module in list(sys.modules.values()):
        location = getattr(module, "__file__", None)
        if (
            isinstance(location, str)
            and Path(location).name == resolved.name
            and Path(location).resolve() == resolved
        ):
            return module
    name = "_geo_verify_asgi_" + hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    module_spec = importlib.util.spec_from_file_location(name, resolved)
    if module_spec is None or module_spec.loader is None:
        raise InputError(f"--asgi-app 无法把 {path.name} 当作 Python 文件载入")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[name]
        raise InputError(f"--asgi-app 无法载入 {label}：{type(exc).__name__}: {exc}") from exc
    return module


def check_base_url(value: str) -> str:
    """接口地址只接受 http(s)，不得带用户名密码、查询参数或片段；去掉末尾的斜杠。"""
    text = value.strip()
    problem = (
        "--base-url 不合法：须为 http(s) 地址（写到 /v1 一级），不能包含用户名密码、查询参数或片段"
    )
    try:
        parts = urlsplit(text)
        _ = parts.port  # 端口不合法时在这里报错，而不是等到发请求时
    except ValueError:
        raise InputError(problem) from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
    ):
        raise InputError(problem)
    return text.rstrip("/")


def _positive(value: float | None, default: float, flag: str, *, allow_zero: bool) -> float:
    number = default if value is None else value
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        bound = "不小于 0" if allow_zero else "大于 0"
        raise InputError(f"{flag} 应为{bound}的秒数：{number}")
    return number


def _secret(args: argparse.Namespace) -> Secret | None:
    name = args.api_key_env
    if name is None:
        return None
    if not _ENV_NAME.match(name):
        raise InputError(f"--api-key-env 应为环境变量名（字母、数字、下划线）：{name!r}")
    value = os.environ.get(name, "").strip()
    if not value:
        raise InputError(f"环境变量 {name} 未设置或为空：密钥只从 --api-key-env 指定的环境变量读取")
    return Secret(value)


def resolve_settings(args: argparse.Namespace) -> tuple[Settings, Any, Secret | None]:
    """检查参数并解析出 (采集设置, ASGI 应用或 None, 密钥或 None)。任何请求发出之前完成。"""
    context = f"--adapter {NAME}"
    require_options(args, context, "--platform", "--window")
    reject_options(
        args, context, "--input", hint="（openai 适配器按封存题库逐题提问，不读取本地文件）"
    )
    if args.now is not None:
        parse_utc(args.now)
    timeout = _positive(args.timeout, DEFAULT_TIMEOUT, "--timeout", allow_zero=False)
    retry_wait = _positive(args.retry_wait, DEFAULT_RETRY_WAIT, "--retry-wait", allow_zero=True)
    max_retries = DEFAULT_MAX_RETRIES if args.max_retries is None else args.max_retries
    if max_retries < 0:
        raise InputError(f"--max-retries 应为不小于 0 的整数：{max_retries}")
    model: str | None = None
    if args.model is not None:
        model = args.model.strip()
        if not model:
            raise InputError("--model 不能为空")

    config: PlatformConfig | None = None
    if args.platforms_file is not None and args.platform_config is None:
        raise InputError("--platforms-file 需要与 --platform-config 一起使用")
    if args.platform_config is not None:
        source = Path(args.platforms_file) if args.platforms_file else None
        config = platform_config(args.platform_config, source)

    if args.live:
        if args.asgi_app is not None:
            raise InputError(
                "--live 与 --asgi-app 不能同时使用：--live 访问真实地址，--asgi-app 只连接进程内应用"
            )
        if args.now is not None:
            raise InputError(
                "--live 不能与 --now 同时使用：真实调用的开始、结束时间和审计时间只取本机时钟，"
                "不得指定（--now 只用于连接进程内模拟服务的可复现运行）"
            )
        require_options(args, "--live", "--base-url", "--api-key-env")
        if config is not None:
            require_verified(config)
    elif args.asgi_app is None:
        raise InputError(
            "默认只连接进程内模拟服务：请用 --asgi-app <模块或文件>:<对象> 指定；"
            "真实调用须显式加 --live，并同时给出 --base-url 与 --api-key-env"
        )
    base_url = check_base_url(args.base_url or DEFAULT_BASE_URL)

    warnings: list[str] = []
    usable = config if config is not None and config.verified else None
    if config is not None and usable is None:
        warnings.append(f"平台配置 {config.name} 未核实：其中的取值一律不使用")
    if model is None and usable is not None:
        model = usable.model
    if args.live and model is None:
        raise InputError("--live 需要 --model（或已核实平台配置中的 model）：真实接口要求指明模型")
    web_search = None if args.web_search is None else args.web_search == "true"
    extra_body: dict[str, Any] = {}
    if usable is not None:
        if args.live and usable.base_url != base_url:
            warnings.append(
                f"--base-url 与已核实平台配置 {usable.name} 的 base_url 不一致，按 --base-url 调用"
            )
        if web_search:
            extra_body = dict(usable.web_search_params)
            if not extra_body:
                warnings.append(
                    f"平台配置 {usable.name} 没有登记联网参数：--web-search true 只作为声明记录，"
                    "请求体不变"
                )

    secret = _secret(args)
    app: Any = None
    label: str | None = None
    if not args.live:
        app, label = load_asgi_app(str(args.asgi_app))
    operator = args.operator.strip() if args.operator and args.operator.strip() else None
    settings = Settings(
        platform=args.platform,
        window=args.window,
        repeats=1 if args.repeats is None else args.repeats,
        queries=split_ids(args.queries),
        transport="live" if args.live else "asgi",
        app_label=label,
        base_url=base_url,
        model=model,
        web_search=web_search,
        extra_body=extra_body,
        timeout=timeout,
        max_retries=max_retries,
        retry_wait=retry_wait,
        operator=operator,
        now=args.now,
        resume=bool(args.resume),
        api_key_env=args.api_key_env,
        platform_config=config,
        warnings=tuple(warnings),
    )
    return settings, app, secret


# ---------------------------------------------------------------- 一次请求


class _TooLarge(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


@dataclass
class Exchange:
    """一次任务的往返结果（含 429 重试）。body 已抹去密钥。"""

    http_status: int | None = None
    body: bytes | None = None
    content_type: str | None = None
    retries: int = 0
    failure: tuple[str, str, str] | None = None  # (invalid_reason, 说明, 快照为空的原因)
    redacted: bool = False


async def _send(
    client: httpx.AsyncClient, url: str, content: bytes, headers: dict[str, str]
) -> tuple[int, str | None, bytes]:
    async with client.stream("POST", url, content=content, headers=headers) as response:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise _TooLarge(response.status_code)
            chunks.append(chunk)
        return response.status_code, response.headers.get("content-type"), b"".join(chunks)


async def exchange(
    client: httpx.AsyncClient,
    url: str,
    content: bytes,
    headers: dict[str, str],
    settings: Settings,
    secret: Secret | None,
) -> Exchange:
    """发出请求；HTTP 429 时按 --max-retries / --retry-wait 做有限次普通重试。"""
    result = Exchange()
    while True:
        try:
            async with asyncio.timeout(settings.timeout):
                status, content_type, body = await _send(client, url, content, headers)
        except (TimeoutError, httpx.TimeoutException):
            text = f"请求超过 {settings.timeout:g} 秒未完成"
            result.failure = ("timeout", text, "请求超时，未收到响应体")
            return result
        except _TooLarge as exc:
            result.http_status = exc.status
            text = f"响应体超过上限 {MAX_RESPONSE_BYTES} 字节，已放弃读取"
            result.failure = ("error", text, "响应体超过上限，未保存")
            return result
        except httpx.HTTPError as exc:
            name = type(exc).__name__
            result.failure = ("error", f"请求失败：{name}", f"请求失败（{name}），未收到响应体")
            return result
        if status == RATE_LIMITED_STATUS and result.retries < settings.max_retries:
            result.retries += 1
            await retry_sleep(settings.retry_wait)
            continue
        if secret is not None:
            body, result.redacted = secret.redact(body)
        result.http_status, result.content_type, result.body = status, content_type, body
        return result


# ---------------------------------------------------------------- 解析响应


class _Malformed(Exception):
    pass


def garbled_ratio(text: str) -> float:
    """去掉空白后，替换字符与控制字符所占的比例。"""
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return 0.0
    bad = sum(
        1 for char in chars if char == _REPLACEMENT or unicodedata.category(char) in _BAD_CATEGORIES
    )
    return bad / len(chars)


def is_garbled(text: str) -> bool:
    return garbled_ratio(text) >= GARBLED_RATIO


def _first_message(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _Malformed("响应缺少 choices[0]")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise _Malformed("choices[0].message 不是对象")
    return message


def _content_text(content: Any) -> str | None:
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(part for part in parts if isinstance(part, str))
    raise _Malformed("choices[0].message.content 既不是文本也不是内容片段列表")


def _title(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def extract_citations(
    payload: dict[str, Any], content: str | None
) -> tuple[list[dict[str, Any]], int]:
    """提取引用，返回 (引用列表, 无法解析而跳过的条目数)。重复引用原样保留。

    - `choices[0].message.annotations[]` 中 type 为 url_citation 的条目：正文引用（inline）。
    - 顶层 `citations`（URL 字符串或 {url, title} 对象的列表）：正文里出现第 n 条对应的
      `[n]` 标记、或直接出现该链接时为 inline，否则为 source_list（仅在来源列表）。
    """
    found: list[tuple[str, str | None, str]] = []
    skipped = 0
    try:
        annotations = _first_message(payload).get("annotations")
    except _Malformed:
        annotations = None
    if annotations is not None and not isinstance(annotations, list):
        annotations, skipped = [], skipped + 1
    for item in annotations or []:
        is_url = isinstance(item, dict) and item.get("type") == "url_citation"
        cited: Any = item.get("url_citation") if is_url else None
        url = cited.get("url") if isinstance(cited, dict) else None
        if not isinstance(url, str) or not url.strip():
            skipped += 1
            continue
        found.append((url.strip(), _title(cited.get("title")), "inline"))
    listed = payload.get("citations")
    if listed is not None and not isinstance(listed, list):
        listed, skipped = [], skipped + 1
    text = content or ""
    for position, item in enumerate(listed or [], start=1):
        if isinstance(item, str):
            url, title = item, None
        elif isinstance(item, dict):
            url, title = item.get("url"), _title(item.get("title"))
        else:
            skipped += 1
            continue
        if not isinstance(url, str) or not url.strip():
            skipped += 1
            continue
        url = url.strip()
        inline = f"[{position}]" in text or url in text
        found.append((url, title, "inline" if inline else "source_list"))
    citations = [
        {"order": order, "url": url, "domain": domain_of(url), "title": title, "placement": where}
        for order, (url, title, where) in enumerate(found, start=1)
    ]
    return citations, skipped


@dataclass
class Reading:
    """对一次往返结果的解读。"""

    status: str = "valid"
    invalid_reason: str | None = None
    detail: str | None = None
    answer: str | None = None
    answer_reason: str | None = None
    model: str | None = None
    has_payload: bool = False
    finish_reason: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    citations_skipped: int = 0
    snapshot_kind: str | None = None  # "json" | "raw" | None
    snapshot_reason: str | None = None

    def fail(self, reason: str, detail: str) -> Reading:
        self.status, self.invalid_reason, self.detail = "invalid", reason, detail
        return self


def _error_message(payload: Any) -> str | None:
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message.strip()[:MAX_DETAIL] if isinstance(message, str) and message.strip() else None


def interpret(result: Exchange) -> Reading:
    reading = Reading()
    if result.failure is not None:
        reason, detail, snapshot_reason = result.failure
        reading.snapshot_reason = snapshot_reason
        reading.answer_reason = f"没有得到回答：{detail}"
        return reading.fail(reason, detail)
    status = result.http_status
    body = result.body or b""
    payload: Any = None
    parsed = False
    if body:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace").lstrip(chr(0xFEFF)))
            parsed = True
        except json.JSONDecodeError:
            payload = None
        reading.snapshot_kind = "json" if parsed else "raw"
    else:
        reading.snapshot_reason = f"HTTP {status} 响应没有响应体"
    reading.answer_reason = "没有得到回答"
    if status == RATE_LIMITED_STATUS:
        return reading.fail("rate_limited", f"HTTP 429：重试 {result.retries} 次后仍被限流")
    if status in TIMEOUT_STATUSES:
        return reading.fail("timeout", f"HTTP {status}：平台响应超时")
    if status is None or not 200 <= status < 300:
        server_message = _error_message(payload)
        suffix = f"：{server_message}" if server_message else ""
        return reading.fail("error", f"HTTP {status}{suffix}")
    if not body:
        return reading.fail("error", f"HTTP {status} 没有响应体")
    if not parsed:
        return reading.fail("error", "响应不是合法 JSON")
    if not isinstance(payload, dict):
        return reading.fail("error", "响应 JSON 不是对象")
    reading.has_payload = True
    model = payload.get("model")
    reading.model = model.strip() if isinstance(model, str) and model.strip() else None
    try:
        message = _first_message(payload)
        text = _content_text(message.get("content"))
    except _Malformed as exc:
        return reading.fail("error", str(exc))
    finish = payload["choices"][0].get("finish_reason")
    reading.finish_reason = finish if isinstance(finish, str) else None
    reading.citations, reading.citations_skipped = extract_citations(payload, text)
    has_text = text is not None and bool(text.strip())
    reading.answer = text if has_text else None
    if reading.finish_reason == "length":
        reading.answer_reason = "回答被截断且没有内容"
        return reading.fail("truncated", "finish_reason 为 length：回答被截断")
    if text is None or not has_text:
        reading.answer_reason = "接口返回空回答"
        return reading.fail("empty_answer", "接口返回空回答")
    ratio = garbled_ratio(text)
    if ratio >= GARBLED_RATIO:
        return reading.fail(
            "garbled",
            f"回答中替换字符与控制字符占 {ratio:.0%}，达到乱码阈值 {GARBLED_RATIO:.0%}",
        )
    return reading


# ---------------------------------------------------------------- 观测


def _media_type(content_type: str | None) -> str:
    value = (content_type or "").split(";")[0].strip().lower()
    return value if _MEDIA_TYPE.match(value) else "application/octet-stream"


def store_snapshot(
    root: Path, observation_id: str, kind: str, body: bytes, content_type: str | None
) -> tuple[dict[str, str], bool]:
    """把原始响应体写入 snapshots/，返回 (快照引用, 是否新写入)。任何已有文件都不覆盖。

    同名文件内容相同（上一次运行中断前已写入）时直接复用；内容不同时换用带序号的文件名。
    """
    suffix = ".json" if kind == "json" else ".raw"
    media_type = "application/json" if kind == "json" else _media_type(content_type)
    digest = sha256_bytes(body)
    number = 1
    while True:
        stem = observation_id if number == 1 else f"{observation_id}.{number}"
        relpath = f"{SNAPSHOTS_DIR}/{stem}{suffix}"
        target = root / relpath
        entry = {"path": relpath, "sha256": digest, "media_type": media_type}
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(body)
            return entry, True
        if target.is_file() and sha256_file(target) == digest:
            return entry, False
        number += 1


def build_observation(
    plan: TaskPlan,
    task: Task,
    settings: Settings,
    result: Exchange,
    reading: Reading,
    *,
    started_at: str,
    ended_at: str,
    snapshots: list[dict[str, str]],
    synthetic: bool,
) -> dict[str, Any]:
    requested = settings.model or "未指定"
    features = f"API 通道：OpenAI 兼容 chat/completions 接口；请求模型：{requested}"
    reasons: dict[str, str] = dict(_FIXED_NULL_REASONS)
    if reading.model is None:
        if reading.has_payload:
            features += "；响应未返回 model 字段"
            reasons["model_version"] = "接口响应未返回 model 字段"
        else:
            features += "；未得到可解析的响应"
            reasons["model_version"] = "未得到可解析的响应，无法读取模型版本"
    if settings.web_search is None:
        reasons["web_search"] = _WEB_SEARCH_REASON
    if settings.operator is None:
        reasons["operator"] = _OPERATOR_REASON
    if reading.answer is None:
        reasons["answer_text"] = reading.answer_reason or "没有得到回答"
    if not snapshots:
        reasons["snapshots"] = reading.snapshot_reason or "没有响应体"
    api: dict[str, Any] = {
        "transport": settings.transport,
        "endpoint": f"{settings.base_url}/chat/completions",
        "request_model": settings.model,
        "http_status": result.http_status,
        "finish_reason": reading.finish_reason,
        "rate_limit_retries": result.retries,
    }
    if reading.detail is not None:
        api["detail"] = reading.detail
    if reading.citations_skipped:
        api["citations_skipped"] = reading.citations_skipped
    if result.redacted:
        api["secret_redacted"] = True
    if settings.platform_config is not None:
        api["platform_config"] = settings.platform_config.name
    observation: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observation_id": task.observation_id,
        "project_id": plan.project_id,
        "trace_id": task.trace_id,
        "prompt_set_hash": plan.prompt_set_hash,
        "query_id": task.query_id,
        "prompt_text": task.prompt_text,
        "platform": plan.platform,
        "entry_form": ENTRY_FORM,
        "model_version": reading.model,
        "entry_features": features,
        "channel": CHANNEL,
        "started_at": started_at,
        "ended_at": ended_at,
        "logged_in": None,
        "history_cleared": None,
        "web_search": settings.web_search,
        "region": None,
        "device": None,
        "answer_text": reading.answer,
        "citations": reading.citations,
        "snapshots": snapshots,
        "collector": collector(),
        "operator": settings.operator,
        "window_id": plan.window_id,
        "attempt": task.attempt,
        "status": reading.status,
        "invalid_reason": reading.invalid_reason,
        "null_reasons": reasons,
        "ext": {"api": api},
    }
    if synthetic:
        observation["synthetic"] = True
    spec.require_valid("observation", observation, where=f"观测 {task.observation_id}")
    return observation


# ---------------------------------------------------------------- 采集


@dataclass
class CollectReport:
    plan: TaskPlan
    skipped_existing: int = 0
    snapshots_written: int = 0
    interrupted: bool = False
    counts: dict[str, Any] = field(default_factory=dict)
    audit_event_id: str | None = None


def _start_time(settings: Settings, index: int) -> str:
    if settings.now is None:
        return format_utc(datetime.now(UTC))
    return format_utc(parse_utc(settings.now) + timedelta(seconds=index))


def _request_body(settings: Settings, task: Task) -> bytes:
    body: dict[str, Any] = {}
    if settings.model is not None:
        body["model"] = settings.model
    body["messages"] = [{"role": "user", "content": task.prompt_text}]
    body.update(settings.extra_body)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


async def _collect_tasks(
    root: Path,
    plan: TaskPlan,
    settings: Settings,
    app: Any,
    secret: Secret | None,
    sink: ObservationSink,
    report: CollectReport,
    synthetic: bool,
) -> None:
    transport = live_transport() if settings.transport == "live" else asgi_transport(app)
    url = f"{settings.base_url}/chat/completions"
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(settings.timeout),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        for index, task in enumerate(plan.tasks):
            if sink.has_trace(task.trace_id):
                report.skipped_existing += 1
                continue
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"geo-verify/{__version__}",
                "X-Request-Id": task.trace_id,
            }
            if secret is not None:
                headers["Authorization"] = secret.header()
            started_at = _start_time(settings, index)
            result = await exchange(
                client, url, _request_body(settings, task), headers, settings, secret
            )
            ended_at = started_at if settings.now is not None else _start_time(settings, index)
            reading = interpret(result)
            snapshots: list[dict[str, str]] = []
            if reading.snapshot_kind is not None and result.body:
                entry, written = store_snapshot(
                    root,
                    task.observation_id,
                    reading.snapshot_kind,
                    result.body,
                    result.content_type,
                )
                snapshots.append(entry)
                report.snapshots_written += written
            sink.add(
                build_observation(
                    plan,
                    task,
                    settings,
                    result,
                    reading,
                    started_at=started_at,
                    ended_at=ended_at,
                    snapshots=snapshots,
                    synthetic=synthetic,
                )
            )


def _audit(
    root: Path, settings: Settings, report: CollectReport, before: str | None, synthetic: bool
) -> str:
    plan = report.plan
    if settings.now is None:
        occurred_at = format_utc(datetime.now(UTC))
    else:
        occurred_at = format_utc(parse_utc(settings.now) + timedelta(seconds=len(plan.tasks)))
    observations_path = project.paths(root).observations
    event = audit.append_event(
        root,
        actor=settings.operator or _DEFAULT_ACTOR,
        action="collect",
        target_type="observations",
        target_id=f"{CHANNEL}:{plan.platform}:{plan.window_id}",
        reason=f"API 通道采集（OpenAI 兼容接口）：平台 {plan.platform}，窗口 {plan.window_id}",
        occurred_at=occurred_at,
        before_hash=before,
        after_hash=sha256_file(observations_path),
        details={**_summary(settings, report), "interrupted": report.interrupted},
        synthetic=True if synthetic else None,
    )
    event_id: str = event["event_id"]
    return event_id


def _summary(settings: Settings, report: CollectReport) -> dict[str, Any]:
    plan = report.plan
    return {
        "adapter": NAME,
        "channel": CHANNEL,
        "platform": plan.platform,
        "window_id": plan.window_id,
        "prompt_set_version": plan.prompt_set_version,
        "prompt_set_hash": plan.prompt_set_hash,
        "transport": settings.transport,
        "app": settings.app_label,
        "base_url": settings.base_url,
        "model": settings.model,
        "web_search": settings.web_search,
        "platform_config": (
            settings.platform_config.name if settings.platform_config is not None else None
        ),
        "api_key_env": settings.api_key_env,
        "planned": len(plan.tasks),
        "skipped_existing": report.skipped_existing,
        **report.counts,
        "snapshots_written": report.snapshots_written,
    }


def collect(
    root: str | Path, settings: Settings, *, app: Any = None, secret: Secret | None = None
) -> CollectReport:
    """按计划逐题请求并写入观测。中途被打断时，已写入的观测保留，并照样写审计事件。"""
    plan = plan_tasks(
        root,
        platform=settings.platform,
        channel=CHANNEL,
        window=settings.window,
        repeats=settings.repeats,
        queries=settings.queries,
    )
    proj = project.load_project(root)
    synthetic = proj.get("synthetic") is True
    base = project.paths(root)
    before = sha256_file(base.observations) if base.observations.exists() else None
    report = CollectReport(plan=plan)
    with ObservationSink(root, batch_size=1) as sink:
        conflicts = [
            task.observation_id
            for task in plan.tasks
            if not sink.has_trace(task.trace_id)
            and sink.id_conflict(task.observation_id, task.trace_id)
        ]
        if conflicts:
            raise InputError(
                f"观测编号 {'、'.join(conflicts[:5])} 已被其他记录（不同 trace_id）占用，拒绝采集",
                details={"conflicting_observation_ids": conflicts},
            )
        try:
            asyncio.run(
                _collect_tasks(base.root, plan, settings, app, secret, sink, report, synthetic)
            )
        except BaseException:
            report.interrupted = True
            raise
        finally:
            sink.flush()
            report.counts = sink.counts()
            if sink.written:
                report.audit_event_id = _audit(base.root, settings, report, before, synthetic)
    return report


# ---------------------------------------------------------------- 命令行


def _text(settings: Settings, report: CollectReport) -> str:
    plan, counts = report.plan, report.counts
    where = (
        f"进程内模拟服务 {settings.app_label}"
        if settings.transport == "asgi"
        else f"真实接口 {settings.base_url}"
    )
    reasons = "、".join(f"{key} {value}" for key, value in counts["invalid_by_reason"].items())
    lines = [
        f"API 通道采集（OpenAI 兼容接口，{where}）：平台 {plan.platform}，窗口 {plan.window_id}，"
        f"题库第 {plan.prompt_set_version} 版。",
        f"计划 {len(plan.tasks)} 个任务：跳过已存在 {report.skipped_existing} 个，"
        f"写入 {counts['written']} 条（有效 {counts['valid']}，无效 {counts['invalid']}"
        f"{'：' + reasons if reasons else ''}）。",
    ]
    if counts["invalid"]:
        lines.append(
            "无效观测已如实写入，不进入指标分母，也不会被覆盖；如需重问，请增大 --repeats，"
            "以新的次数序号采集。"
        )
    if counts["written"] and not counts["valid"]:
        lines.append("注意：本次写入的观测全部无效，请检查 --base-url、--model 和平台状态。")
    lines += [f"警告：{item}" for item in settings.warnings]
    lines.append("API 通道数据只反映接口返回的内容，与网页端、应用端看到的可能不同。")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> CommandResult:
    settings, app, secret = resolve_settings(args)
    report = collect(args.project, settings, app=app, secret=secret)
    plan = report.plan
    data: dict[str, Any] = {
        **_summary(settings, report),
        "queries": len(plan.query_ids),
        "repeats": plan.repeats,
        "resume": settings.resume,
        "platform_config": (
            settings.platform_config.summary() if settings.platform_config is not None else None
        ),
        "audit_event_id": report.audit_event_id,
        "warnings": list(settings.warnings),
    }
    return CommandResult(data=data, text=_text(settings, report))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(f"OpenAI 兼容接口（--adapter {NAME}）")
    group.add_argument(
        "--asgi-app",
        help="进程内 ASGI 应用：<模块>:<对象> 或 <文件.py>:<对象>，如 mock_engines.app:app；"
        "默认只连接它，不联网",
    )
    group.add_argument(
        "--base-url",
        help=f"接口地址（写到 /v1 一级），默认 {DEFAULT_BASE_URL}；连接 ASGI 应用时只用到其路径",
    )
    group.add_argument(
        "--model",
        help="请求体里的 model；不给则不发送（--live 时必须给出，或由已核实的平台配置提供）",
    )
    group.add_argument(
        "--web-search", choices=("true", "false"), help="声明本次采集是否开启联网；不给则记为 null"
    )
    group.add_argument(
        "--timeout", type=float, help=f"单次请求的超时秒数，默认 {DEFAULT_TIMEOUT:g}"
    )
    group.add_argument(
        "--max-retries",
        type=int,
        help=f"遇到 HTTP 429 时最多重试几次，默认 {DEFAULT_MAX_RETRIES}；不做其他任何重试",
    )
    group.add_argument(
        "--retry-wait", type=float, help=f"每次 429 重试前等待的秒数，默认 {DEFAULT_RETRY_WAIT:g}"
    )
    group.add_argument(
        "--resume",
        action="store_true",
        help="续跑：跳过已有记录的任务（不加也会跳过，加上只是显式声明）",
    )
    group.add_argument(
        "--live",
        action="store_true",
        help="真实调用 --base-url 指向的地址；须同时给出 --base-url 与 --api-key-env",
    )
    group.add_argument(
        "--api-key-env", help="存放密钥的环境变量名；密钥只从环境变量读取，不会写入任何文件"
    )
    group.add_argument(
        "--platform-config", help="使用平台配置项（如 deepseek）；未核实的配置不能用于 --live"
    )
    group.add_argument(
        "--platforms-file", help="自备的平台配置文件，格式同随包分发的 platforms.toml"
    )


ADAPTER = AdapterSpec(
    name=NAME,
    summary="通用 OpenAI 兼容接口（API 通道），默认只连进程内模拟服务",
    add_arguments=add_arguments,
    run=run,
)
