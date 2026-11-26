"""指标、交叉验证、告警、报告测试共用的项目构造器。

只按契约写文件（经由 tests/factory.py），不调用被测的指标或统计代码。
全部数据为虚构，带 synthetic: true。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from factory import (
    TARGET,
    make_annotation,
    make_entities,
    make_judgment,
    make_observation,
    make_project,
    make_prompt,
    read,
    seal_prompts,
    write_records,
)

from geo_verify import audit
from geo_verify.jsonl import write_json

MODEL_VERSIONS = {"engine-a": "engine-a-2026.10", "engine-b": "engine-b-2026.09"}
UNSET: Any = object()


class Builder:
    """逐条累积观测、标注、判定，最后一次写入项目目录。"""

    def __init__(
        self,
        root: Path,
        *,
        queries: tuple[str, ...] = ("Q001", "Q002", "Q003"),
        prompts: list[dict[str, Any]] | None = None,
        prereg: dict[str, Any] | None = None,
        entities_extra: list[dict[str, Any]] | None = None,
        **project_fields: Any,
    ) -> None:
        self.root = root
        if prereg is not None:
            project_fields["prereg"] = prereg
        self.project = make_project(root, **project_fields)
        self.entities = make_entities(root, extra=entities_extra)
        self.sealed = seal_prompts(
            root, self.project, prompts or [make_prompt(query) for query in queries]
        )
        self.observations: list[dict[str, Any]] = []
        self.annotations: list[dict[str, Any]] = []
        self.judgments: list[dict[str, Any]] = []
        self._written = [0, 0, 0]

    def obs(
        self,
        query: str = "Q001",
        platform: str = "engine-a",
        window: str = "W1",
        *,
        mentioned: bool | None = True,
        prompt_set: dict[str, Any] | None = None,
        model_version: Any = UNSET,
        ann: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """追加一条观测；mentioned 不为 None 时同时追加目标主体的规则标注。

        ann 里的键传给 make_annotation（如 rankable、rank、sentiment、risk、recommended）。
        """
        if model_version is UNSET:
            model_version = MODEL_VERSIONS.get(platform, f"{platform}-v1")
        number = len(self.observations) + 1
        observation = make_observation(
            self.project,
            prompt_set or self.sealed,
            number,
            query_id=query,
            platform=platform,
            window=window,
            model_version=model_version,
            **fields,
        )
        self.observations.append(observation)
        if mentioned is not None and observation["status"] == "valid":
            self.annotate(observation, mentioned=mentioned, **(ann or {}))
        return observation

    def annotate(
        self, observation: dict[str, Any], *, entity_id: str = TARGET, **fields: Any
    ) -> dict[str, Any]:
        annotation = make_annotation(
            observation, len(self.annotations) + 1, entity_id=entity_id, **fields
        )
        self.annotations.append(annotation)
        return annotation

    def judge(
        self,
        observation: dict[str, Any],
        label: str,
        *,
        direction: str | None = None,
        fact_id: str = "F-S7-RANGE-CLTC",
    ) -> dict[str, Any]:
        if direction is None:
            forward = label in ("supported", "contradicted", "unverifiable")
            direction = "answer_to_facts" if forward else "facts_to_answer"
        judgment = make_judgment(
            observation,
            len(self.judgments) + 1,
            direction=direction,
            label=label,
            fact_id=fact_id,
        )
        self.judgments.append(judgment)
        return judgment

    def write(self) -> Path:
        """把尚未写入的记录追加到项目文件（可多次调用）。"""
        pending = [
            self.observations[self._written[0] :],
            self.annotations[self._written[1] :],
            self.judgments[self._written[2] :],
        ]
        write_records(
            self.root, observations=pending[0], annotations=pending[1], judgments=pending[2]
        )
        self._written = [len(self.observations), len(self.annotations), len(self.judgments)]
        return self.root

    def exclude(
        self, observations: list[dict[str, Any]], rule_id: str = "EX-DUP"
    ) -> dict[str, Any]:
        ids = [item["observation_id"] for item in observations]
        return audit.append_event(
            self.root,
            actor="数据管理员（虚构）",
            action="exclude",
            target_type="observation",
            target_id="batch",
            reason="测试构造的剔除（虚构）",
            occurred_at="2026-10-09T03:00:00Z",
            details={"rule_id": rule_id, "count": len(ids), "observation_ids": ids},
            synthetic=True,
        )

    def update_project(self, **fields: Any) -> None:
        self.project.update(fields)
        write_json(self.root / "project.json", self.project)


def find(
    results: list[dict[str, Any]],
    metric_id: str,
    *,
    platform: str | None = None,
    channel: str | None = None,
    window_id: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """按指标编号和范围取出唯一的一条结果。"""
    matches = [
        item
        for item in results
        if item["metric_id"] == metric_id
        and item["scope"]["platform"] == platform
        and item["scope"]["channel"] == channel
        and item["scope"]["window_id"] == window_id
        and item["scope"]["date"] == date
    ]
    assert len(matches) == 1, f"{metric_id} 在该范围应恰有 1 条结果，实际 {len(matches)} 条"
    return matches[0]


def metrics_file(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = read(root / "out" / "metrics.json")
    return data


def copy_project(source: Path, target: Path) -> Path:
    """把项目的原始文件复制到另一个目录（不带 out/），模拟第三方独立复算。"""
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("out"))
    return target


def utc(day: int, hour: int = 2, minute: int = 0, second: int = 0, month: int = 10) -> str:
    return f"2026-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"
