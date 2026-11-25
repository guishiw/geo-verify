"""题库版本差异：按 query_id 对齐，列出新增、删除和修改的题目。题目顺序不算变化。"""

from __future__ import annotations

from typing import Any

_MISSING = object()


def diff_prompts(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, Any]:
    old_by_id = {prompt["query_id"]: prompt for prompt in old}
    new_by_id = {prompt["query_id"]: prompt for prompt in new}
    added = [prompt for prompt in new if prompt["query_id"] not in old_by_id]
    removed = [prompt for prompt in old if prompt["query_id"] not in new_by_id]
    modified: list[dict[str, Any]] = []
    unchanged = 0
    for prompt in new:
        before = old_by_id.get(prompt["query_id"])
        if before is None:
            continue
        if before == prompt:
            unchanged += 1
            continue
        fields = sorted(
            key
            for key in before.keys() | prompt.keys()
            if before.get(key, _MISSING) != prompt.get(key, _MISSING)
        )
        modified.append(
            {
                "query_id": prompt["query_id"],
                "changed_fields": fields,
                "before": before,
                "after": prompt,
            }
        )
    return {"added": added, "removed": removed, "modified": modified, "unchanged": unchanged}


def is_empty(diff: dict[str, Any]) -> bool:
    return not (diff["added"] or diff["removed"] or diff["modified"])
