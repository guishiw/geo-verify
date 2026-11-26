"""抽样复核测试的辅助函数：合成项目构造、抽样的独立参考实现、复核表的读写。

全部数据为虚构（synthetic: true）。这里不调用被测的 report.recheck 实现。
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import heapq
import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import factory

from geo_verify.jsonl import append_jsonl, write_json

REGISTERED_SEED = 20261001  # spec 合法样例里预先登记的 recheck.seed
REGISTERED_FRACTION = (1, 5)  # 预先登记的 fraction = 0.2，用整数比表示以便精确计算

AnnotationFields = Callable[[dict[str, Any], str], dict[str, Any] | None]


def reference_sample(
    query_ids: Iterable[str], numerator: int, denominator: int, seed: int
) -> list[str]:
    """抽样的独立参考实现。

    规则：对每个题目计算 sha256("<seed>|<query_id>")，取摘要最小的
    ⌈numerator / denominator × 题数⌉ 个。这里用整数算术求题数、用摘要字节加 heapq 取最小值，
    写法与被测实现不同。
    """
    unique = sorted(set(query_ids))
    size = -(-numerator * len(unique) // denominator)
    keyed = [(hashlib.sha256(f"{seed}|{qid}".encode()).digest(), qid) for qid in unique]
    return sorted(qid for _, qid in heapq.nsmallest(size, keyed))


def default_fields(observation: dict[str, Any], entity_id: str) -> dict[str, Any] | None:
    """默认标注：目标品牌被推荐且排第 1；竞品被提及、未被推荐、排第 2。"""
    if entity_id == factory.TARGET:
        return {"mentioned": True, "recommended": True, "rankable": True, "rank": 1}
    return {"mentioned": True, "recommended": False, "rankable": True, "rank": 2}


@dataclass
class Built:
    root: Path
    project: dict[str, Any]
    prompt_set: dict[str, Any]
    observations: list[dict[str, Any]]
    annotations: list[dict[str, Any]]

    @property
    def query_ids(self) -> list[str]:
        return [item["query_id"] for item in self.prompt_set["prompts"]]

    def sampled(
        self, numerator: int = 1, denominator: int = 5, seed: int = REGISTERED_SEED
    ) -> list[str]:
        return reference_sample(self.query_ids, numerator, denominator, seed)

    def observations_of(self, query_ids: Iterable[str]) -> list[dict[str, Any]]:
        wanted = set(query_ids)
        return [item for item in self.observations if item["query_id"] in wanted]

    def observation_ids_of(self, query_ids: Iterable[str]) -> list[str]:
        return sorted(item["observation_id"] for item in self.observations_of(query_ids))


def build_project(
    root: Path,
    *,
    queries: int = 10,
    per_query: int = 3,
    entities: tuple[str, ...] = (factory.TARGET, factory.COMPETITOR),
    fields: AnnotationFields = default_fields,
    prereg: dict[str, Any] | None = None,
    write: bool = True,
) -> Built:
    """构造一个已封存、已标注的合成项目：queries 道题，每题 per_query 条有效观测。"""
    project = factory.make_project(root, prereg=prereg)
    factory.make_entities(root)
    prompts = [factory.make_prompt(f"Q{index:03d}") for index in range(1, queries + 1)]
    prompt_set = factory.seal_prompts(root, project, prompts)
    observations: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    for index in range(1, queries + 1):
        for attempt in range(1, per_query + 1):
            observation = factory.make_observation(
                project,
                prompt_set,
                (index - 1) * per_query + attempt,
                query_id=f"Q{index:03d}",
                attempt=attempt,
            )
            observations.append(observation)
            for entity_id in entities:
                values = fields(observation, entity_id)
                if values is None:
                    continue
                annotations.append(
                    factory.make_annotation(
                        observation, len(annotations) + 1, entity_id=entity_id, **values
                    )
                )
    if write:
        factory.write_records(root, observations=observations, annotations=annotations)
    return Built(root, project, prompt_set, observations, annotations)


def write_disputes(root: Path, items: list[dict[str, Any]]) -> None:
    """按 outputs.md 的结构写出 out/disputes.json（不经过被测的 kappa 实现）。"""
    write_json(
        root / "out" / "disputes.json",
        {
            "schema_version": "1.0",
            "kind": "disputes",
            "project_id": "auto-demo",
            "synthetic": True,
            "annotator_a": "标注员甲（虚构）",
            "annotator_b": "标注员乙（虚构）",
            "fields": [],
            "items": items,
            "unresolved": sum(1 for item in items if not item.get("resolved")),
        },
    )


def dispute(observation_id: str, entity_id: str, *, resolved: bool = False) -> dict[str, Any]:
    return {
        "observation_id": observation_id,
        "entity_id": entity_id,
        "field": "mentioned",
        "a": True,
        "b": False,
        "resolved": resolved,
    }


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """读取带 BOM 的 CSV，返回（表头，行字典列表）。"""
    text = path.read_bytes().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    header = rows[0]
    return header, [dict(zip(header, cells, strict=True)) for cells in rows[1:]]


def write_csv(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    """模拟表格软件另存为“CSV UTF-8”：带 BOM、CRLF 换行。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(name, "") for name in header])
    path.write_bytes(codecs.BOM_UTF8 + buffer.getvalue().encode("utf-8"))


def fill(
    rows: list[dict[str, str]], answers: dict[tuple[str, str], tuple[str, str, str]]
) -> list[dict[str, str]]:
    """把人工复核结论填进复核表：answers 的键是（观测，实体），值是（提及，推荐，位次）。"""
    filled: list[dict[str, str]] = []
    for row in rows:
        row = dict(row)
        key = (row["observation_id"], row["entity_id"])
        if key in answers:
            mentioned, recommended, rank = answers[key]
            row["recheck_mentioned"] = mentioned
            row["recheck_recommended"] = recommended
            row["recheck_rank"] = rank
        filled.append(row)
    return filled


def write_jsonl_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    append_jsonl(path, rows)
