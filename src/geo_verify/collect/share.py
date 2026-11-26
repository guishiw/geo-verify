"""已保存的分享数据导入：`collect --adapter share`。

只解析使用者提供的**本地文件**，不联网、不访问文件里的任何链接、不执行脚本、不加载外部资源。
支持两种通用形式：

- JSON：`{"messages": [{"role": "user"|"assistant", "content": "…"}], "model": …,
  "citations"|"sources": […], "url": 分享页地址, "saved_at": …}`，字段名兼容少量常见别名
  （见 docs/collect.md）；
- HTML：使用者自己另存的分享页。用标准库 html.parser 提取可见正文与 `<a href>` 链接，
  按元素上的角色标记（如 `data-message-author-role`）切分提问与回答。这是尽力而为的解析。

取最后一组“用户提问 + 助手回答”写成一条观测（channel = share_import）。浏览器辅助插件导出的
文件（JSON，`"format": "geo-verify-capture/1"`，见 extension/FORMAT.md）按其固定格式读取，观测通道为
browser_assist，入口形态与环境字段取文件中的 environment。提问原文与封存题目
不一致时照样导入，prompt_text 记录文件中的实际提问原文，并给出明显的警告。
文件里拿不到的环境字段一律为 null 并写明原因；文件里没有可用的保存时间时，开始和结束时间
取导入时间（--now 或当前时间），并在 ext.share 注明“时间为导入时间，非采集时间”。
原文件复制进 snapshots/ 并记录 SHA-256；同一文件重复导入不会重复写入。
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from geo_verify import audit, project, spec
from geo_verify.canon import sha256_bytes, sha256_file
from geo_verify.cli import CommandResult
from geo_verify.collect.adapter import AdapterSpec, reject_options, require_options
from geo_verify.collect.base import (
    ENTRY_FORMS,
    PLACEMENTS,
    SCHEMA_VERSION,
    FieldError,
    collector,
    domain_of,
    snapshot_entry,
    to_utc,
)
from geo_verify.collect.openai_compat import is_garbled
from geo_verify.collect.tasks import plan_tasks
from geo_verify.errors import InputError
from geo_verify.jsonl import append_jsonl, read_bytes
from geo_verify.timeutil import format_utc, parse_utc

NAME = "share"
CHANNEL = "share_import"
BROWSER_CHANNEL = "browser_assist"
CAPTURE_FORMAT = "geo-verify-capture/1"
CAPTURE_PREFIX = "geo-verify-capture/"
CAPTURE_TIME_NOTE = "时间为用户点击保存的时间，非提问时间"
CAPTURE_IDS: tuple[tuple[str, str], ...] = (
    ("platform", "--platform"),
    ("window_id", "--window"),
    ("query_id", "--query"),
)
CAPTURE_TRI_STATE = ("logged_in", "history_cleared", "web_search")
CAPTURE_ENV_TEXT = ("entry_features", "region", "device")
ENVIRONMENT_FIELDS = ("logged_in", "history_cleared", "web_search", "region", "device")
DEFAULT_ENTRY_FORM = "web"
SHARE_ENTRY_FORMS: tuple[str, ...] = tuple(item for item in ENTRY_FORMS if item != "api")
SNAPSHOTS_DIR = "snapshots"
_DEFAULT_ACTOR = "geo-verify collect（未指明操作人）"

# JSON 字段名的别名（按先后顺序取第一个出现的）
MESSAGE_KEYS = ("messages", "conversation", "chat_messages")
ROLE_KEYS = ("role", "author", "sender")
TEXT_KEYS = ("content", "text")
MODEL_KEYS = ("model", "model_slug", "model_version")
CITATION_KEYS = ("citations", "sources", "references")
CITATION_URL_KEYS = ("url", "link", "href")
CITATION_TITLE_KEYS = ("title", "name")
SHARE_URL_KEYS = ("url", "share_url", "link")
SAVED_KEYS = ("saved_at", "exported_at", "created_at", "create_time")
ROLE_ALIASES = {
    "user": "user",
    "human": "user",
    "assistant": "assistant",
    "ai": "assistant",
    "bot": "assistant",
    "model": "assistant",
}

# HTML：角色标记、来源区标记、不可见元素
ROLE_ATTRIBUTES = ("data-message-author-role", "data-role", "data-author", "data-sender")
MODEL_ATTRIBUTES = ("data-model", "data-message-model-slug")
SOURCE_MARKERS = frozenset({"sources", "citations", "references"})
SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "head",
        "svg",
        "button",
        "select",
        "textarea",
        "iframe",
        "object",
        "canvas",
    }
)
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
SAVED_META_NAMES = frozenset({"saved_at", "saved-at"})
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([A-Za-z0-9_.:-]+)", re.IGNORECASE)

_NULL_REASONS = {
    "logged_in": "分享数据导入：文件不包含提问时是否登录",
    "history_cleared": "分享数据导入：文件不包含提问前是否清除历史",
    "web_search": "分享数据导入：文件不包含是否联网",
    "region": "分享数据导入：文件不包含地域",
    "device": "分享数据导入：文件不包含设备",
}
_CAPTURE_NULL_REASONS = {
    "logged_in": "浏览器辅助采集：采集人未填写是否登录",
    "history_cleared": "浏览器辅助采集：采集人未填写是否清除历史",
    "web_search": "浏览器辅助采集：采集人未填写是否联网",
    "region": "浏览器辅助采集：采集人未填写地域",
    "device": "浏览器辅助采集：采集人未填写设备",
}


@dataclass(frozen=True)
class RawCitation:
    position: int  # 在文件引用列表中的位置（从 1 起），用于匹配正文中的 [n]
    url: str
    title: str | None
    placement: str | None  # 文件明确给出的位置；None 表示按通用规则判断


@dataclass
class Message:
    role: str  # "user" | "assistant"
    text: str
    model: str | None = None
    citations: list[RawCitation] | None = None  # None 表示消息本身没有给引用
    citations_skipped: int = 0


@dataclass
class Parsed:
    format: str
    messages: list[Message]
    model: str | None = None
    share_url: str | None = None
    saved_field: str | None = None
    saved_value: Any = None
    citations: list[RawCitation] = field(default_factory=list)
    citations_skipped: int = 0


# ---------------------------------------------------------------- 读取文件


def detect_format(name: str, raw: bytes) -> str:
    if not raw.strip():
        raise InputError(f"{name} 是空文件")
    suffix = Path(name).suffix.lower()
    if suffix in (".mhtml", ".mht"):
        raise InputError(
            f"不支持 MHTML 文件（{name}）：请在浏览器里另存为“网页，仅 HTML”，或导出 JSON"
        )
    if suffix == ".json":
        return "json"
    if suffix in (".html", ".htm", ".xhtml"):
        return "html"
    head = raw.removeprefix(b"\xef\xbb\xbf").lstrip()[:1]
    if head in (b"{", b"["):
        return "json"
    if head == b"<":
        return "html"
    raise InputError(f"无法识别文件格式：{name}（支持 JSON 与 HTML；没有扩展名时按内容判断）")


def decode_html(raw: bytes, name: str) -> str:
    """优先按 UTF-8 解码；失败时按页面 <meta charset> 声明的编码解码。"""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    match = _META_CHARSET.search(raw[:4096])
    if match:
        try:
            return raw.decode(match.group(1).decode("ascii"))
        except (LookupError, UnicodeDecodeError):
            pass
    raise InputError(f"{name} 不是 UTF-8 编码，也没有可用的 <meta charset> 声明")


# ---------------------------------------------------------------- JSON


class _BadContent(Exception):
    pass


def _first_key(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    return next((key for key in keys if key in data), None)


def _text_value(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _role(item: dict[str, Any]) -> str | None:
    key = _first_key(item, ROLE_KEYS)
    value = item.get(key) if key else None
    if isinstance(value, dict):
        value = value.get("role")
    return ROLE_ALIASES.get(value.strip().lower()) if isinstance(value, str) else None


def _part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        text = part.get("text")
        return text if isinstance(text, str) else ""
    raise _BadContent


def _flatten(value: Any) -> str:
    """消息内容：文本、内容片段列表，或 {"parts": [...]}；其他写法无法识别。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_part_text(part) for part in value)
    if isinstance(value, dict) and isinstance(value.get("parts"), list):
        return "".join(_part_text(part) for part in value["parts"])
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        text: str = value["text"]
        return text
    raise _BadContent


def _json_citations(data: dict[str, Any]) -> tuple[list[RawCitation] | None, int]:
    key = _first_key(data, CITATION_KEYS)
    if key is None:
        return None, 0
    value = data[key]
    if not isinstance(value, list):
        return [], 1
    citations: list[RawCitation] = []
    skipped = 0
    for position, item in enumerate(value, start=1):
        url: Any = None
        title: str | None = None
        placement: str | None = None
        if isinstance(item, str):
            url = item
        elif isinstance(item, dict):
            url = next((item[k] for k in CITATION_URL_KEYS if isinstance(item.get(k), str)), None)
            title = _text_value(item, CITATION_TITLE_KEYS)
            placement = item.get("placement") if item.get("placement") in PLACEMENTS else None
        if not isinstance(url, str) or not url.strip():
            skipped += 1
            continue
        citations.append(RawCitation(position, url.strip(), title, placement))
    return citations, skipped


def load_json(text: str, name: str = "文件") -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"{name} 不是合法的 JSON：{exc.msg}（第 {exc.lineno} 行）") from None


def parse_json_data(data: Any, name: str = "文件") -> Parsed:
    top: dict[str, Any]
    if isinstance(data, list):
        top, items = {}, data
    elif isinstance(data, dict):
        top = data
        key = _first_key(data, MESSAGE_KEYS)
        if key is None:
            raise InputError(f"{name} 缺少 messages（也接受 {'、'.join(MESSAGE_KEYS[1:])}）")
        items = data[key]
        if not isinstance(items, list):
            raise InputError(f"{name} 的 {key} 应为消息列表")
    else:
        raise InputError(f"{name} 的 JSON 顶层应为对象（或消息列表）")
    messages: list[Message] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise InputError(f"{name} 第 {index} 条消息应为对象")
        role = _role(item)
        if role is None:
            continue  # system、tool 等其他角色不参与配对
        key = _first_key(item, TEXT_KEYS)
        try:
            content = _flatten(item[key]) if key else ""
        except _BadContent:
            raise InputError(f"{name} 第 {index} 条消息的内容无法识别") from None
        citations, skipped = _json_citations(item)
        messages.append(Message(role, content, _text_value(item, MODEL_KEYS), citations, skipped))
    citations, skipped = _json_citations(top)
    saved_field = _first_key(top, SAVED_KEYS)
    return Parsed(
        format="json",
        messages=messages,
        model=_text_value(top, MODEL_KEYS),
        share_url=_text_value(top, SHARE_URL_KEYS),
        saved_field=saved_field,
        saved_value=top.get(saved_field) if saved_field else None,
        citations=citations or [],
        citations_skipped=skipped,
    )


@dataclass(frozen=True)
class Capture:
    """浏览器辅助插件导出文件（geo-verify-capture/1）中 parse_json_data 之外的字段。"""

    page_url: str | None
    page_title: str | None
    ids: dict[str, str | None]  # 文件里手工填写的 platform、window_id、query_id
    environment: dict[str, Any]  # entry_form 与 CAPTURE_TRI_STATE、CAPTURE_ENV_TEXT 各项
    operator: str | None


def _optional_text(data: dict[str, Any], key: str, label: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InputError(f"{label} 应为文本或 null：{value!r}")
    return value.strip() or None


def read_capture(data: Any, name: str) -> Capture | None:
    """识别插件导出文件；不是插件格式时返回 None。字段不合 FORMAT.md 的约定时为输入错误。"""
    if not isinstance(data, dict):
        return None
    declared = data.get("format")
    if not isinstance(declared, str) or not declared.startswith(CAPTURE_PREFIX):
        return None
    if declared != CAPTURE_FORMAT:
        raise InputError(f"{name} 的格式 {declared} 不受支持：本版本只能导入 {CAPTURE_FORMAT}")
    where = f"{name}（{CAPTURE_FORMAT}）"
    if data.get("capture") != BROWSER_CHANNEL:
        raise InputError(f"{where} 的 capture 应为 {BROWSER_CHANNEL}：{data.get('capture')!r}")
    messages = data.get("messages")
    roles = (
        [item.get("role") if isinstance(item, dict) else None for item in messages]
        if isinstance(messages, list)
        else None
    )
    if roles != ["user", "assistant"]:
        raise InputError(f"{where} 的 messages 应恰好两条：先 user，后 assistant")
    environment = data.get("environment")
    if not isinstance(environment, dict):
        raise InputError(f"{where} 的 environment 应为对象")
    entry_form = environment.get("entry_form")
    if entry_form not in SHARE_ENTRY_FORMS:
        raise InputError(
            f"{where} 的 environment.entry_form 应为 {' / '.join(SHARE_ENTRY_FORMS)} 之一："
            f"{entry_form!r}"
        )
    values: dict[str, Any] = {"entry_form": entry_form}
    for key in CAPTURE_TRI_STATE:
        value = environment.get(key)
        if value is not None and not isinstance(value, bool):
            raise InputError(
                f"{where} 的 environment.{key} 只能是 true、false 或 null（未知）：{value!r}"
            )
        values[key] = value
    for key in CAPTURE_ENV_TEXT:
        values[key] = _optional_text(environment, key, f"{where} 的 environment.{key}")
    _optional_text(data, "model", f"{where} 的 model")
    return Capture(
        page_url=_optional_text(data, "url", f"{where} 的 url"),
        page_title=_optional_text(data, "title", f"{where} 的 title"),
        ids={key: _optional_text(data, key, f"{where} 的 {key}") for key, _ in CAPTURE_IDS},
        environment=values,
        operator=_optional_text(data, "operator", f"{where} 的 operator"),
    )


# ---------------------------------------------------------------- HTML


@dataclass
class _Link:
    href: str
    title_attr: str | None
    placement: str
    parts: list[str] = field(default_factory=list)


@dataclass
class _Open:
    tag: str
    hidden: bool = False
    sources: bool = False
    message: _HtmlMessage | None = None
    link: _Link | None = None


@dataclass
class _HtmlMessage:
    role: str
    model: str | None
    parts: list[str] = field(default_factory=list)
    links: list[tuple[str, str | None, str]] = field(default_factory=list)
    skipped: int = 0


def _visible_text(parts: list[str]) -> str:
    lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)


class _ShareParser(HTMLParser):
    """只收集可见文本和链接：不执行脚本，不加载任何外部资源。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Open] = []
        self.messages: list[_HtmlMessage] = []
        self.hidden_depth = 0
        self.sources_depth = 0
        self.message: _HtmlMessage | None = None
        self.link: _Link | None = None
        self.share_url: str | None = None
        self.og_url: str | None = None
        self.saved_at: str | None = None

    def _visible_in_message(self) -> bool:
        return self.message is not None and self.hidden_depth == 0

    def _newline(self) -> None:
        if self.message is not None and self._visible_in_message():
            self.message.parts.append("\n")

    def _meta(self, attrs: dict[str, str]) -> None:
        name = attrs.get("name", "").strip().lower()
        content = attrs.get("content", "").strip()
        if name in SAVED_META_NAMES and content:
            self.saved_at = content
        if attrs.get("property", "").strip().lower() == "og:url" and content:
            self.og_url = content

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "meta":
            self._meta(values)
        elif tag == "link":
            rels = values.get("rel", "").lower().split()
            if "canonical" in rels and values.get("href", "").strip():
                self.share_url = values["href"].strip()
        if tag in VOID_TAGS:
            if tag in ("br", "hr"):
                self._newline()
            return
        entry = _Open(tag)
        entry.hidden = (
            tag in SKIP_TAGS
            or "hidden" in values
            or values.get("aria-hidden", "").strip().lower() == "true"
            or bool(_HIDDEN_STYLE.search(values.get("style", "")))
        )
        marker = (values.get("data-role", "") or values.get("data-section", "")).strip().lower()
        entry.sources = marker in SOURCE_MARKERS or tag == "aside"
        if tag in BLOCK_TAGS:
            self._newline()
        if entry.hidden:
            self.hidden_depth += 1
        if entry.sources:
            self.sources_depth += 1
        role = next(
            (
                ROLE_ALIASES[value]
                for value in (values.get(name, "").strip().lower() for name in ROLE_ATTRIBUTES)
                if value in ROLE_ALIASES
            ),
            None,
        )
        if role is not None and self.message is None and self.hidden_depth == 0:
            model = next(
                (values[name].strip() for name in MODEL_ATTRIBUTES if values.get(name, "").strip()),
                None,
            )
            entry.message = _HtmlMessage(role, model)
            self.messages.append(entry.message)
            self.message = entry.message
        if (
            tag == "a"
            and self.link is None
            and self.message is not None
            and self.message.role == "assistant"
            and self.hidden_depth == 0
        ):
            entry.link = _Link(
                href=values.get("href", "").strip(),
                title_attr=values.get("title", "").strip() or None,
                placement="source_list" if self.sources_depth else "inline",
            )
            self.link = entry.link
        self.stack.append(entry)

    def _close(self, entry: _Open) -> None:
        if entry.link is not None and self.message is not None:
            link = entry.link
            title = " ".join("".join(link.parts).split()) or link.title_attr
            if link.href.lower().startswith(("http://", "https://")):
                self.message.links.append((link.href, title, link.placement))
            else:
                self.message.skipped += 1
            self.link = None
        if entry.message is not None:
            self.message = None
        if entry.hidden:
            self.hidden_depth -= 1
        if entry.sources:
            self.sources_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        index = next(
            (
                position
                for position in range(len(self.stack) - 1, -1, -1)
                if self.stack[position].tag == tag
            ),
            None,
        )
        if index is None:
            return  # 多余的结束标签
        for entry in reversed(self.stack[index:]):
            self._close(entry)
        del self.stack[index:]
        if tag in BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self.message is None or self.hidden_depth:
            return
        if self.link is not None:
            self.link.parts.append(data)
        if not self.sources_depth:
            self.message.parts.append(data)

    def finish(self) -> None:
        self.close()
        for entry in reversed(self.stack):
            self._close(entry)
        self.stack.clear()


def parse_html(text: str) -> Parsed:
    parser = _ShareParser()
    parser.feed(text)
    parser.finish()
    messages = [
        Message(
            role=item.role,
            text=_visible_text(item.parts),
            model=item.model,
            citations=[
                RawCitation(position, url, title, placement)
                for position, (url, title, placement) in enumerate(item.links, start=1)
            ],
            citations_skipped=item.skipped,
        )
        for item in parser.messages
    ]
    return Parsed(
        format="html",
        messages=messages,
        share_url=parser.share_url or parser.og_url,
        saved_field="meta:saved_at" if parser.saved_at else None,
        saved_value=parser.saved_at,
    )


# ---------------------------------------------------------------- 配对与转换


def last_pair(messages: list[Message]) -> tuple[Message, Message, int]:
    """最后一组“用户提问 + 助手回答”，以及提问之前还有几条对话消息。"""
    answer_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].role == "assistant"
        ),
        None,
    )
    question_index = (
        None
        if answer_index is None
        else next(
            (index for index in range(answer_index - 1, -1, -1) if messages[index].role == "user"),
            None,
        )
    )
    if answer_index is None or question_index is None:
        raise InputError(
            "文件中没有找到“用户提问 + 助手回答”"
            "（JSON 需要 role 为 user 与 assistant 的消息；HTML 需要带角色标记的元素）"
        )
    question = messages[question_index]
    if not question.text.strip():
        raise InputError("文件中最后一组对话的提问为空")
    return question, messages[answer_index], question_index


def resolve_citations(raw: list[RawCitation], answer: str) -> list[dict[str, Any]]:
    """确定每条引用的位置：文件明确给出的为准；否则正文出现 [n] 或该链接为 inline。"""
    citations = []
    for order, item in enumerate(raw, start=1):
        placement = item.placement or (
            "inline" if f"[{item.position}]" in answer or item.url in answer else "source_list"
        )
        citations.append(
            {
                "order": order,
                "url": item.url,
                "domain": domain_of(item.url),
                "title": item.title,
                "placement": placement,
            }
        )
    return citations


def saved_time(parsed: Parsed) -> tuple[str | None, str | None]:
    """文件里的保存时间（UTC）。返回 (时间或 None, 无法使用时的警告或 None)。"""
    if parsed.saved_field is None:
        return None, None
    value = parsed.saved_value
    problem = (
        f"文件中的保存时间（{parsed.saved_field} = {value!r}）无法使用"
        "（须为带时区的 ISO 8601 时间或 Unix 秒），已改用导入时间"
    )
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None, problem
    if isinstance(value, str):
        try:
            return to_utc(value, parsed.saved_field)[0], None
        except FieldError:
            return None, problem
    try:
        return format_utc(datetime.fromtimestamp(value, UTC)), None
    except (OverflowError, OSError, ValueError):
        return None, problem


def _same_prompt(actual: str, sealed: str) -> bool:
    return " ".join(actual.split()) == " ".join(sealed.split())


def _source_sha(observation: dict[str, Any]) -> str | None:
    ext = observation.get("ext")
    share = ext.get("share") if isinstance(ext, dict) else None
    value = share.get("source_sha256") if isinstance(share, dict) else None
    return value if isinstance(value, str) else None


def _store_copy(root: Path, observation_id: str, name: str, content: bytes) -> tuple[str, bool]:
    """把原文件复制到 snapshots/<观测编号>/<文件名>。任何已有文件都不覆盖。"""
    safe = name.replace("\\", "_")
    stem, suffix = Path(safe).stem, Path(safe).suffix
    digest = sha256_bytes(content)
    number = 1
    while True:
        filename = safe if number == 1 else f"{stem}.{number}{suffix}"
        relpath = f"{SNAPSHOTS_DIR}/{observation_id}/{filename}"
        target = root / relpath
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(content)
            return relpath, True
        if target.is_file() and sha256_file(target) == digest:
            return relpath, False
        number += 1


# ---------------------------------------------------------------- 命令行


def run(args: argparse.Namespace) -> CommandResult:
    context = f"--adapter {NAME}"
    require_options(args, context, "--input", "--platform", "--window", "--query")
    reject_options(
        args,
        context,
        "--repeats",
        "--queries",
        hint="（一次导入一个文件：用 --query 指明题目、--attempt 指明第几次）",
    )
    attempt = 1 if args.attempt is None else args.attempt
    if attempt < 1:
        raise InputError(f"--attempt 应为不小于 1 的整数：{attempt}")
    if args.now is not None:
        parse_utc(args.now)
    project.paths(args.project)
    source = Path(args.input)
    if not source.is_file():
        raise InputError(f"文件不存在：{source}")
    raw = read_bytes(source)
    name = source.name
    fmt = detect_format(name, raw)
    captured: Capture | None = None
    if fmt == "json":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise InputError(f"{name} 不是 UTF-8 编码（JSON 文件须为 UTF-8）") from None
        loaded = load_json(text, name)
        captured = read_capture(loaded, name)
        parsed = parse_json_data(loaded, name)
    else:
        parsed = parse_html(decode_html(raw, name))
    # 通道决定任务的 trace_id，所以先读文件、再按通道生成任务
    channel = BROWSER_CHANNEL if captured is not None else CHANNEL
    file_format = CAPTURE_FORMAT if captured is not None else fmt
    plan = plan_tasks(
        args.project,
        platform=args.platform,
        channel=channel,
        window=args.window,
        repeats=attempt,
        queries=[args.query],
    )
    task = plan.tasks[-1]
    question, answer, prior = last_pair(parsed.messages)
    digest = sha256_bytes(raw)

    root = project.paths(args.project).root
    proj = project.load_project(root)
    existing = project.load_observations(root)
    by_trace = {item["trace_id"]: item for item in existing}
    previous = by_trace.get(task.trace_id)
    base: dict[str, Any] = {
        "adapter": NAME,
        "channel": channel,
        "input": name,
        "format": file_format,
        "platform": plan.platform,
        "window_id": plan.window_id,
        "query_id": task.query_id,
        "attempt": task.attempt,
        "prompt_set_version": plan.prompt_set_version,
        "prompt_set_hash": plan.prompt_set_hash,
        "trace_id": task.trace_id,
        "observation_id": task.observation_id,
    }
    if previous is not None:
        if _source_sha(previous) != digest:
            raise InputError(
                f"任务（题目 {task.query_id}，第 {task.attempt} 次）已有分享导入记录 "
                f"{previous['observation_id']}，来自另一个文件；观测写入后不可覆盖，"
                "如要导入这个文件，请用新的 --attempt"
            )
        data = {
            **base,
            "written": 0,
            "skipped_existing": 1,
            "status": previous["status"],
            "invalid_reason": previous["invalid_reason"],
            "audit_event_id": None,
            "warnings": [],
        }
        text_out = f"跳过：{name} 已导入为观测 {previous['observation_id']}，不重复写入。"
        return CommandResult(data=data, text=text_out)
    same_file = next(
        (
            item
            for item in existing
            if item.get("channel") == channel and _source_sha(item) == digest
        ),
        None,
    )
    if same_file is not None:
        raise InputError(
            f"这个文件已导入为观测 {same_file['observation_id']}（平台 {same_file['platform']}，"
            f"窗口 {same_file['window_id']}，题目 {same_file['query_id']}，"
            f"第 {same_file['attempt']} 次），再次导入会重复计数"
        )
    taken = next((item for item in existing if item["observation_id"] == task.observation_id), None)
    if taken is not None:
        raise InputError(
            f"观测编号 {task.observation_id} 已被另一条记录（trace_id {taken['trace_id']}）占用"
        )

    sealed_text = task.prompt_text
    differs = not _same_prompt(question.text, sealed_text)
    warnings: list[str] = []
    if differs:
        warnings.append(
            f"文件中的提问与封存题目 {task.query_id} 的原文不一致：已按文件中的实际提问原文记录 "
            "prompt_text，请核对 --query 是否选对"
        )
    mismatched: list[str] = []
    if captured is not None:
        given = {"platform": plan.platform, "window_id": plan.window_id, "query_id": task.query_id}
        for key, option in CAPTURE_IDS:
            value = captured.ids[key]
            if value is not None and value != given[key]:
                mismatched.append(key)
                warnings.append(
                    f"文件中的 {key}（{value}）与命令行 {option}（{given[key]}）不一致：以命令行为准"
                )
    started_at, time_problem = saved_time(parsed)
    time_source = "saved_at" if started_at is not None else "import_time"
    if time_problem:
        warnings.append(time_problem)
    if started_at is None:
        started_at = audit.resolve_now(args.now)
        time_note = "时间为导入时间，非采集时间：文件中没有可用的保存时间"
        if not time_problem:
            warnings.append("文件中没有保存时间：开始和结束时间记为导入时间（非采集时间）")
    elif captured is not None:
        time_note = CAPTURE_TIME_NOTE
    else:
        time_note = (
            f"开始和结束时间均取文件中的保存时间（{parsed.saved_field}），不是实际提问的时间"
        )
    if prior:
        warnings.append(f"文件中这组提问之前还有 {prior} 条对话消息，回答可能受前文影响")

    entry_form = args.entry_form or DEFAULT_ENTRY_FORM
    if captured is not None:
        file_entry = captured.environment["entry_form"]
        if args.entry_form and args.entry_form != file_entry:
            warnings.append(
                f"文件中的 entry_form（{file_entry}）与命令行 --entry-form（{args.entry_form}）"
                "不一致：以命令行为准"
            )
        entry_form = args.entry_form or file_entry
    model = answer.model or parsed.model
    if answer.citations is not None:
        raw_citations, skipped = answer.citations, answer.citations_skipped
    else:
        raw_citations, skipped = (
            parsed.citations,
            parsed.citations_skipped + answer.citations_skipped,
        )
    if not answer.text.strip():
        status, reason, answer_text = "invalid", "empty_answer", None
    elif is_garbled(answer.text):
        status, reason, answer_text = "invalid", "garbled", answer.text
    else:
        status, reason, answer_text = "valid", None, answer.text
    citations = resolve_citations(raw_citations, answer.text)

    operator = args.operator.strip() if args.operator and args.operator.strip() else None
    if captured is not None:
        environment = {key: captured.environment[key] for key in ENVIRONMENT_FIELDS}
        reasons = {
            key: text for key, text in _CAPTURE_NULL_REASONS.items() if environment[key] is None
        }
        features = captured.environment["entry_features"] or "浏览器辅助采集：采集人未填写入口特征"
        if model is None:
            if captured.environment["entry_features"] is None:
                features += "；文件未提供模型版本"
            reasons["model_version"] = "浏览器辅助采集：采集人未填写模型或产品版本"
        if operator and captured.operator and operator != captured.operator:
            warnings.append(
                f"文件中的 operator（{captured.operator}）与命令行 --operator（{operator}）"
                "不一致：以命令行为准"
            )
        operator = operator or captured.operator
        if operator is None:
            reasons["operator"] = "浏览器辅助采集：采集人未填写，也未用 --operator 指明"
    else:
        environment = dict.fromkeys(ENVIRONMENT_FIELDS)
        features = f"分享数据导入：使用者另存的 {fmt.upper()} 文件，入口形态按 {entry_form} 记录"
        reasons = dict(_NULL_REASONS)
        if prior:
            reasons["history_cleared"] += f"；文件中该提问之前还有 {prior} 条对话消息"
        if model is None:
            features += "；文件未提供模型版本"
            reasons["model_version"] = "分享文件未提供模型或产品版本"
        if operator is None:
            reasons["operator"] = "未用 --operator 指明采集人"
    if answer_text is None:
        reasons["answer_text"] = "分享文件中的回答为空"
    share_ext: dict[str, Any] = {
        "format": file_format,
        "source_file": name,
        "source_sha256": digest,
        "share_url": None if captured is not None else parsed.share_url,
        "time_source": time_source,
        "time_field": parsed.saved_field if time_source == "saved_at" else None,
        "time_note": time_note,
        "prompt_matches_sealed": not differs,
        "prior_messages": prior,
    }
    if skipped:
        share_ext["citations_skipped"] = skipped
    if captured is not None:
        share_ext["capture"] = {
            "page_url": captured.page_url,
            "page_title": captured.page_title,
            "file_ids": captured.ids,
            "mismatched": mismatched,
        }

    relpath, _ = _store_copy(root, task.observation_id, name, raw)
    observation: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observation_id": task.observation_id,
        "project_id": plan.project_id,
        "trace_id": task.trace_id,
        "prompt_set_hash": plan.prompt_set_hash,
        "query_id": task.query_id,
        "prompt_text": question.text,
        "platform": plan.platform,
        "entry_form": entry_form,
        "model_version": model,
        "entry_features": features,
        "channel": channel,
        "started_at": started_at,
        "ended_at": started_at,
        **environment,
        "answer_text": answer_text,
        "citations": citations,
        "snapshots": [snapshot_entry(root, relpath, sha256=digest)],
        "collector": collector(),
        "operator": operator,
        "window_id": plan.window_id,
        "attempt": task.attempt,
        "status": status,
        "invalid_reason": reason,
        "null_reasons": reasons,
        "ext": {"share": share_ext},
    }
    synthetic = proj.get("synthetic") is True
    if synthetic:
        observation["synthetic"] = True
    spec.require_valid("observation", observation, where=f"观测 {task.observation_id}")
    observations_path = project.paths(root).observations
    before = sha256_file(observations_path) if observations_path.exists() else None
    append_jsonl(observations_path, [observation])
    event = audit.append_event(
        root,
        actor=operator or _DEFAULT_ACTOR,
        action="import",
        target_type="observations",
        target_id=name,
        reason=f"导入已保存的分享数据：{name}",
        occurred_at=audit.resolve_now(args.now),
        before_hash=before,
        after_hash=sha256_file(observations_path),
        details={
            "adapter": NAME,
            "channel": channel,
            "source_file": name,
            "source_sha256": digest,
            "format": file_format,
            "platform": plan.platform,
            "window_id": plan.window_id,
            "query_id": task.query_id,
            "attempt": task.attempt,
            "observation_id": task.observation_id,
            "status": status,
            "prompt_text_differs": differs,
            "time_source": time_source,
        },
        synthetic=True if synthetic else None,
    )
    if captured is not None:
        base["page_url"] = captured.page_url
    data = {
        **base,
        "entry_form": entry_form,
        "written": 1,
        "skipped_existing": 0,
        "status": status,
        "invalid_reason": reason,
        "prompt_text_differs": differs,
        "time_source": time_source,
        "citations": len(citations),
        "share_url": share_ext["share_url"],
        "prior_messages": prior,
        "snapshot": relpath,
        "audit_event_id": event["event_id"],
        "warnings": warnings,
    }
    state = "有效" if status == "valid" else f"无效（{reason}）"
    lines = [f"警告：{item}" for item in warnings[:1] if differs]
    kind = "浏览器辅助采集文件" if captured is not None else "分享数据"
    label = CAPTURE_FORMAT if captured is not None else fmt.upper()
    lines.append(
        f"已导入{kind} {name}（{label}）→ 观测 {task.observation_id}："
        f"平台 {plan.platform}，窗口 {plan.window_id}，题目 {task.query_id} 第 {task.attempt} 次，"
        f"{state}，引用 {len(citations)} 条。"
    )
    lines += [f"警告：{item}" for item in warnings[1 if differs else 0 :]]
    lines.append("只解析了本地文件，没有访问文件中的任何链接。")
    return CommandResult(data=data, text="\n".join(lines))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(f"已保存的分享数据导入（--adapter {NAME}）")
    group.add_argument("--query", help="文件对应的封存题目编号，如 Q001")
    group.add_argument("--attempt", type=int, help="同一窗口内第几次提问，默认 1")
    group.add_argument(
        "--entry-form",
        choices=SHARE_ENTRY_FORMS,
        help=f"入口形态，默认 {DEFAULT_ENTRY_FORM}（分享数据不是接口调用，不能为 api）",
    )


ADAPTER = AdapterSpec(
    name=NAME,
    summary="已保存的分享数据导入：只解析本地 JSON / HTML 文件，不联网",
    add_arguments=add_arguments,
    run=run,
)
