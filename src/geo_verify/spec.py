"""数据契约加载与校验。契约唯一源是仓库根目录的 spec/。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from geo_verify.errors import InputError

SPEC_VERSION = "v1"

CONTRACTS: tuple[str, ...] = (
    "project",
    "prompt_set",
    "entity",
    "observation",
    "annotation",
    "fact",
    "claim",
    "judgment",
    "metric_result",
    "manifest",
    "audit_event",
)


@dataclass(frozen=True)
class SchemaIssue:
    """一条校验问题：JSON 指针位置、触发的关键字、可读说明。"""

    path: str
    keyword: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "keyword": self.keyword, "message": self.message}


def spec_root() -> Path:
    """定位契约目录：安装后在包内 _spec，开发态在仓库根目录 spec/。"""
    packaged = Path(__file__).resolve().parent / "_spec"
    if packaged.is_dir():
        return packaged
    source = Path(__file__).resolve().parents[2] / "spec"
    if source.is_dir():
        return source
    raise InputError("找不到契约目录 spec/，安装可能不完整")


def schema_path(name: str) -> Path:
    if name not in CONTRACTS:
        raise InputError(f"未知契约：{name}；可选：{', '.join(CONTRACTS)}")
    return spec_root() / SPEC_VERSION / f"{name}.schema.json"


@cache
def load_schema(name: str) -> dict[str, Any]:
    path = schema_path(name)
    if not path.exists():
        raise InputError(f"契约文件缺失：{path}")
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


@cache
def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name))


def _pointer(parts: Any) -> str:
    return "/" + "/".join(str(part) for part in parts) if parts else ""


def iter_issues(name: str, instance: Any) -> list[SchemaIssue]:
    """返回全部校验问题（按位置排序，结果确定）。空列表表示合法。"""
    issues = [
        SchemaIssue(
            path=_pointer(list(error.absolute_path)),
            keyword=str(error.validator),
            message=error.message,
        )
        for error in _validator(name).iter_errors(instance)
    ]
    return sorted(issues, key=lambda item: (item.path, item.keyword, item.message))


def is_valid(name: str, instance: Any) -> bool:
    return _validator(name).is_valid(instance)


def require_valid(name: str, instance: Any, *, where: str = "") -> None:
    """不合契约时抛出 InputError，附全部问题。"""
    issues = iter_issues(name, instance)
    if issues:
        prefix = f"{where}：" if where else ""
        first = issues[0]
        raise InputError(
            f"{prefix}不符合契约 {name}（共 {len(issues)} 处，首处 {first.path or '/'}：{first.message}）",
            details=[issue.to_dict() for issue in issues],
        )
