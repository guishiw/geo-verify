"""平台配置项：OpenAI 兼容接口的接入参数。

随包分发的 platforms.toml 只列出 DeepSeek、Kimi、元宝、豆包、文心、千问六个占位条目，
接口地址、模型名、联网参数全部留空并标为未核实：这些取值不得凭记忆写死，须由人工查阅
该平台文档后填写，并记录来源链接与核对日期。使用者可复制该文件修改后用 --platforms-file 指定。

未核实的条目不能用于真实调用（require_verified）；未核实条目里的取值一律不被使用。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from geo_verify.errors import InputError
from geo_verify.jsonl import read_bytes

BUILTIN_FILE = "platforms.toml"
STATUS_VERIFIED = "已核实"
STATUS_UNVERIFIED = "未核实"
REQUIRED_KEYS: tuple[str, ...] = (
    "display_name",
    "base_url",
    "model",
    "web_search_params",
    "verified",
    "status",
    "source_url",
    "checked_at",
)
OPTIONAL_KEYS: tuple[str, ...] = ("note",)
TEXT_KEYS: tuple[str, ...] = (
    "display_name",
    "base_url",
    "model",
    "status",
    "source_url",
    "checked_at",
    "note",
)
# 联网参数并入请求体时不得覆盖这些字段
RESERVED_BODY_KEYS = frozenset({"model", "messages"})
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


@dataclass(frozen=True)
class PlatformConfig:
    name: str
    display_name: str
    base_url: str | None
    model: str | None
    web_search_params: dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    status: str = STATUS_UNVERIFIED
    source_url: str | None = None
    checked_at: str | None = None
    note: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verified": self.verified,
            "status": self.status,
            "source_url": self.source_url,
            "checked_at": self.checked_at,
        }


def builtin_text() -> str:
    """随包分发的 platforms.toml 原文。"""
    return resources.files("geo_verify.collect").joinpath(BUILTIN_FILE).read_text(encoding="utf-8")


def _http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _real_date(value: str) -> bool:
    if not _DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _entry(label: str, name: str, value: Any) -> PlatformConfig:
    if not _NAME.match(name):
        raise InputError(f"{label}：平台名称 {name!r} 不合法（只能用字母、数字和 . _ -）")
    where = f"{label} 中的平台 {name}"
    if not isinstance(value, dict):
        raise InputError(f"{where} 应写成表 [platforms.{name}]")
    unknown = sorted(set(value) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS))
    if unknown:
        raise InputError(f"{where} 含未知字段：{'、'.join(unknown)}")
    missing = [key for key in REQUIRED_KEYS if key not in value]
    if missing:
        raise InputError(f"{where} 缺少字段：{'、'.join(missing)}")
    wrong = [key for key in TEXT_KEYS if key in value and not isinstance(value[key], str)]
    if wrong:
        raise InputError(f"{where} 的 {'、'.join(wrong)} 应为字符串（未知请写空字符串）")
    if not isinstance(value["verified"], bool):
        raise InputError(f"{where} 的 verified 应为 true 或 false")
    params = value["web_search_params"]
    if not isinstance(params, dict):
        raise InputError(f"{where} 的 web_search_params 应为内联表，如 {{}} 或 {{ 字段 = 取值 }}")
    clash = sorted(RESERVED_BODY_KEYS & set(params))
    if clash:
        raise InputError(
            f"{where} 的 web_search_params 不能包含 {'、'.join(clash)}（会覆盖请求的模型或题目）"
        )
    status = value["status"]
    if status not in (STATUS_VERIFIED, STATUS_UNVERIFIED):
        raise InputError(f"{where} 的 status 只能是“未核实”或“已核实”：{status!r}")
    if status != (STATUS_VERIFIED if value["verified"] else STATUS_UNVERIFIED):
        raise InputError(f"{where} 的 status（{status}）与 verified（{value['verified']}）不一致")

    def text(key: str) -> str | None:
        return str(value.get(key, "")).strip() or None

    config = PlatformConfig(
        name=name,
        display_name=text("display_name") or name,
        base_url=text("base_url"),
        model=text("model"),
        web_search_params=dict(params),
        verified=value["verified"],
        status=status,
        source_url=text("source_url"),
        checked_at=text("checked_at"),
        note=text("note"),
    )
    if config.verified:
        problems = []
        if config.base_url is None or not _http_url(config.base_url):
            problems.append("base_url 须为 http(s) 接口地址")
        if config.model is None:
            problems.append("model 不能为空")
        if config.source_url is None or not _http_url(config.source_url):
            problems.append("source_url 须为核实所依据的文档链接")
        if config.checked_at is None or not _real_date(config.checked_at):
            problems.append("checked_at 须为核对日期 YYYY-MM-DD")
        if problems:
            raise InputError(f"{where} 标为已核实，但{'；'.join(problems)}")
    return config


def load_platform_configs(path: Path | None = None) -> dict[str, PlatformConfig]:
    """读取平台配置：path 为 None 时读随包分发的 platforms.toml。按文件中的顺序返回。"""
    if path is None:
        label, text = BUILTIN_FILE, builtin_text()
    else:
        label = path.name
        if not path.is_file():
            raise InputError(f"平台配置文件不存在：{path}")
        try:
            text = read_bytes(path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InputError(f"平台配置文件 {label} 不是 UTF-8 编码") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InputError(f"平台配置文件 {label} 不是合法的 TOML：{exc}") from exc
    table = data.get("platforms")
    if not isinstance(table, dict):
        raise InputError(f"平台配置文件 {label} 缺少 [platforms.<名称>] 条目")
    return {name: _entry(label, name, value) for name, value in table.items()}


def platform_config(name: str, path: Path | None = None) -> PlatformConfig:
    configs = load_platform_configs(path)
    if name not in configs:
        source = BUILTIN_FILE if path is None else path.name
        raise InputError(f"平台配置 {name} 不存在（{source} 中有：{'、'.join(configs) or '无'}）")
    return configs[name]


def require_verified(config: PlatformConfig) -> None:
    """真实调用前的检查：未核实的平台配置一律拒绝。"""
    if not config.verified:
        raise InputError(
            f"平台配置 {config.name}（{config.display_name}）未核实，不能用于 --live。"
            "请先由人工查阅该平台文档，填写 base_url、model（需要时还有 web_search_params），"
            "记录 source_url（来源链接）与 checked_at（核对日期），再把 verified 改为 true、"
            "status 改为“已核实”；建议复制配置文件修改后用 --platforms-file 指定"
        )
