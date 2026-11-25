"""封存、采集、标注、证据等测试共用的合成数据构造器。

全部为虚构数据；品牌、车型、域名都不存在。只按契约拼装输入，不调用被测实现。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from factory import make_project, make_prompt, seal_prompts

from geo_verify.jsonl import append_jsonl

PROJECT_ID = "auto-demo"


def entity(
    entity_id: str,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    exclusions: tuple[str, ...] = (),
    kind: str = "product",
    role: str = "target",
    parent: str | None = None,
    domains: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "entity_id": entity_id,
        "project_id": PROJECT_ID,
        "kind": kind,
        "role": role,
        "name": name,
        "aliases": list(aliases),
        "exclusions": list(exclusions),
        "owned_domains": list(domains),
        "parent_entity_id": parent,
        "synthetic": True,
    }


def auto_entities() -> list[dict[str, Any]]:
    """虚构汽车品牌的实体表：目标品牌澜序，竞品岑野（含与目标同名的车型 S7）和朔风。"""
    return [
        entity(
            "brand-lanxu",
            "澜序",
            aliases=("澜序汽车", "LANXU", "Lan-Xu"),
            exclusions=("波澜序曲",),
            kind="brand",
            domains=("lanxu-auto.example",),
        ),
        entity("product-lanxu-s7", "澜序 S7", aliases=("S7",), parent="brand-lanxu"),
        entity("product-lanxu-x5", "澜序 X5", aliases=("X5",), parent="brand-lanxu"),
        entity(
            "brand-cenye",
            "岑野",
            aliases=("岑野汽车", "CENYE"),
            kind="brand",
            role="competitor",
            domains=("cenye-auto.example",),
        ),
        entity(
            "product-cenye-k5",
            "岑野 K5",
            aliases=("K5",),
            role="competitor",
            parent="brand-cenye",
        ),
        entity(
            "product-cenye-s7",
            "岑野 S7",
            aliases=("CENYE S7",),
            role="competitor",
            parent="brand-cenye",
        ),
        entity("brand-shuofeng", "朔风", kind="brand", role="competitor"),
        entity(
            "product-shuofeng-t6",
            "朔风 T6",
            aliases=("T6",),
            role="competitor",
            parent="brand-shuofeng",
        ),
    ]


def write_entities(
    root: Path, entities: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    records = entities if entities is not None else auto_entities()
    path = root / "entities.jsonl"
    if path.exists():
        path.unlink()
    append_jsonl(path, records)
    return records


def write_draft(root: Path, prompts: list[dict[str, Any]]) -> Path:
    path = root / "prompts" / "draft.jsonl"
    if path.exists():
        path.unlink()
    append_jsonl(path, prompts)
    return path


INTENT_TEXTS: dict[str, str] = {
    "brand": "澜序这个品牌怎么样？",
    "product": "澜序 S7 的续航是多少公里？",
    "category": "20 万左右的纯电 SUV 有哪些可以考虑？",
    "scenario": "一家五口周末自驾出游，适合开什么车？",
    "problem": "冬天电动车续航缩水明显，应该怎么办？",
    "comparison": "澜序 S7 和岑野 K5 的空间表现有什么区别？",
    "decision": "预算 25 万，第一次买电动车要注意哪些方面？",
    "after_sales": "澜序的电池质保政策是怎样的？",
    "risk": "电动车自燃的常见诱因有哪些？",
    "competitor": "岑野 K5 的智能驾驶辅助水平如何？",
    "general": "新能源车的保值率一般受哪些因素影响？",
}


def clean_prompts() -> list[dict[str, Any]]:
    """11 类意图各一题、互不相似、不含诱导和个人信息的题库。"""
    return [
        make_prompt(f"Q{index:03d}", text=text, intent_type=intent)
        for index, (intent, text) in enumerate(INTENT_TEXTS.items(), start=1)
    ]


def sealed_project(
    root: Path, prompts: list[dict[str, Any]] | None = None, **project_overrides: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """直接按契约写出项目、实体和一版封存题库（不经过被测的 seal 实现）。"""
    proj = make_project(root, **project_overrides)
    write_entities(root)
    sealed = seal_prompts(root, proj, prompts if prompts is not None else clean_prompts())
    return proj, sealed
