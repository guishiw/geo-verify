"""采集任务规划：把（题目 × 第几次）展开成带确定性编号的任务列表。

人工采集包和接口类适配器共用。同一组（项目、题库版本、平台、通道、窗口、题目、次数）
无论何时重算都得到同一个 trace_id，因此采集可以续跑，且不会重复计数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from geo_verify import project, spec
from geo_verify.collect.base import observation_id_for, scope_problem, trace_id_for
from geo_verify.errors import InputError


@dataclass(frozen=True)
class Task:
    query_id: str
    attempt: int
    prompt_text: str
    trace_id: str
    observation_id: str


@dataclass(frozen=True)
class TaskPlan:
    project_id: str
    prompt_set_hash: str
    prompt_set_version: int
    platform: str
    channel: str
    window_id: str
    repeats: int
    query_ids: tuple[str, ...]
    tasks: tuple[Task, ...]


def split_ids(value: str | None) -> list[str] | None:
    """解析逗号分隔的编号列表（如 `--queries Q001,Q002`）；没有给出时返回 None。"""
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def plan_tasks(
    root: str | Path,
    *,
    platform: str,
    channel: str,
    window: str,
    repeats: int = 1,
    queries: list[str] | None = None,
) -> TaskPlan:
    """按最新封存的题库展开任务。题目按封存顺序排列，每题从第 1 次到第 repeats 次。

    平台与通道必须已在 project.json 登记；queries 为 None 表示全部题目。
    """
    proj = project.load_project(root)
    sealed = project.select_prompt_set(root)
    scope = scope_problem(proj, platform, channel)
    if scope:
        raise InputError(scope[1])
    # 窗口编号会原样写进观测的 window_id，格式以 observation 契约为准
    id_pattern: str = spec.load_schema("observation")["$defs"]["id"]["pattern"]
    if re.search(id_pattern, window) is None:
        raise InputError(
            f"窗口编号 {window!r} 不合法：须以字母或数字开头，只能包含字母、数字和 . _ : -"
        )
    if repeats < 1:
        raise InputError(f"每题提问次数（--repeats）应为不小于 1 的整数：{repeats}")
    texts: dict[str, str] = {item["query_id"]: item["text"] for item in sealed["prompts"]}
    if queries is not None:
        if not queries:
            raise InputError("--queries 没有给出任何题目编号")
        unknown = [item for item in dict.fromkeys(queries) if item not in texts]
        if unknown:
            raise InputError(
                f"题目 {'、'.join(unknown)} 不在第 {sealed['version']} 版封存题库中",
                details={"unknown_query_ids": unknown},
            )
    wanted = set(texts if queries is None else queries)
    query_ids = tuple(query_id for query_id in texts if query_id in wanted)
    project_id: str = proj["project_id"]
    content_hash: str = sealed["content_hash"]
    tasks: list[Task] = []
    for query_id in query_ids:
        for attempt in range(1, repeats + 1):
            parts = (project_id, content_hash, platform, channel, window, query_id, attempt)
            tasks.append(
                Task(
                    query_id=query_id,
                    attempt=attempt,
                    prompt_text=texts[query_id],
                    trace_id=trace_id_for(*parts),
                    observation_id=observation_id_for(*parts),
                )
            )
    return TaskPlan(
        project_id=project_id,
        prompt_set_hash=content_hash,
        prompt_set_version=sealed["version"],
        platform=platform,
        channel=channel,
        window_id=window,
        repeats=repeats,
        query_ids=query_ids,
        tasks=tuple(tasks),
    )
