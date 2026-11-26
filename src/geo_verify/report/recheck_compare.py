"""抽样复核结果比对：把人工复核结论与生成计划时生效的标注逐项比对。

输入是 CSV（UTF-8，可带 BOM，如在 recheck/plan.csv 上填写后另存）或 JSONL，每行一个复核项：
observation_id、entity_id，以及各复核字段。复核列优先取 `recheck_<字段>`，没有时取 `<字段>`。

取值：
- 提及 / 推荐：true、yes、y、1、是 表示“是”；false、no、n、0、否 表示“否”（不区分大小写）；
  推荐另可填 null、none、n/a、无 表示“未判断”。提及不能为空值。
- 位次：≥ 1 的整数；null、none、n/a、无 表示没有位次。
- 一行的复核列全部留空表示“未复核”；只填了一部分为不完整（逐行报错）。

比对对象：计划项带 annotation_id 时，用这条标注（生成计划时生效的那条），
事后改标注不会让一致率变好；计划外或没有计划时，用当前生效的标注。
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from geo_verify.canon import sha256_bytes
from geo_verify.errors import InputError
from geo_verify.jsonl import read_json, write_json
from geo_verify.report.recheck import (
    FIELDS,
    PLAN_PATH,
    RESULT_PATH,
    SCHEMA_VERSION,
    SOURCES,
    ProjectData,
    diff_fields,
    load_project_data,
    notice,
)

FORMATS = ("csv", "jsonl")
TRUE_WORDS = frozenset({"true", "yes", "y", "1", "是"})
FALSE_WORDS = frozenset({"false", "no", "n", "0", "否"})
NULL_WORDS = frozenset({"null", "none", "n/a", "无"})
NOTE_COLUMN = "recheck_note"
_INTEGER = re.compile(r"[0-9]+")
_MISSING = object()


def parse_fields(text: str) -> list[str]:
    """解析 --fields：逗号分隔，输出按固定顺序。"""
    names = [part.strip() for part in text.split(",")]
    if not names or any(not name for name in names):
        raise InputError(f"--fields 不能有空项：{text!r}；可选 {', '.join(FIELDS)}")
    unknown = [name for name in names if name not in FIELDS]
    if unknown:
        raise InputError(f"--fields 含未知字段：{'、'.join(unknown)}；可选 {', '.join(FIELDS)}")
    return [name for name in FIELDS if name in names]


# ------------------------------------------------------------------ 读取复核文件


@dataclass
class Row:
    """复核文件的一行。values 中缺失的字段（留空）为 _MISSING。"""

    line: int
    observation_id: Any
    entity_id: Any
    values: dict[str, Any]
    columns: dict[str, str]
    note: str | None = None
    malformed: str | None = None


@dataclass
class InputFile:
    format: str
    sha256: str
    rows: list[Row] = field(default_factory=list)


def _detect_format(path: Path, declared: str | None) -> str:
    if declared is not None:
        return declared
    suffix = path.suffix.lower()
    if suffix in (".csv", ".jsonl"):
        return suffix[1:]
    raise InputError(f"无法从扩展名判断复核文件格式：{path.name}；请用 --format csv|jsonl 指明")


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(
            "复核文件不是 UTF-8 编码：请在表格软件中另存为“CSV UTF-8”后再导入"
        ) from exc


def read_input(path: Path, fields: Sequence[str], declared_format: str | None = None) -> InputFile:
    if not path.is_file():
        raise InputError(f"复核结果文件不存在：{path.name}")
    file_format = _detect_format(path, declared_format)
    data = path.read_bytes()
    text = _decode(data)
    if not text.strip():
        raise InputError(f"复核结果文件是空文件：{path.name}")
    parsed = InputFile(file_format, sha256_bytes(data))
    if file_format == "csv":
        parsed.rows = _csv_rows(text, fields)
    else:
        parsed.rows = _jsonl_rows(text, fields)
    return parsed


def _csv_rows(text: str, fields: Sequence[str]) -> list[Row]:
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [name.strip() for name in next(reader)]
        named = [name for name in header if name]
        repeated = sorted({name for name in named if named.count(name) > 1})
        if repeated:
            raise InputError(f"复核文件的表头有重复的列：{'、'.join(repeated)}")
        missing_ids = [name for name in ("observation_id", "entity_id") if name not in header]
        if missing_ids:
            raise InputError(f"复核文件缺少必需的列：{'、'.join(missing_ids)}")
        columns: dict[str, str] = {}
        missing: list[str] = []
        for name in fields:
            if f"recheck_{name}" in header:
                columns[name] = f"recheck_{name}"
            elif name in header:
                columns[name] = name
            else:
                missing.append(f"recheck_{name}（或 {name}）")
        if missing:
            raise InputError(f"复核文件缺少复核列：{'、'.join(missing)}")
        position = {name: header.index(name) for name in header if name}
        rows: list[Row] = []
        previous_line = reader.line_num
        for cells in reader:
            line = previous_line + 1
            previous_line = reader.line_num
            if not any(cell.strip() for cell in cells):
                continue
            row = Row(line, None, None, {}, dict(columns))
            if len(cells) > len(header) and any(cell.strip() for cell in cells[len(header) :]):
                row.malformed = f"单元格数（{len(cells)}）多于表头的列数（{len(header)}）"
                rows.append(row)
                continue
            cells = cells + [""] * (len(header) - len(cells))
            row.observation_id = cells[position["observation_id"]].strip()
            row.entity_id = cells[position["entity_id"]].strip()
            for name, column in columns.items():
                cell = cells[position[column]]
                row.values[name] = cell if cell.strip() else _MISSING
            if NOTE_COLUMN in position and cells[position[NOTE_COLUMN]].strip():
                row.note = cells[position[NOTE_COLUMN]].strip()
            rows.append(row)
    except csv.Error as exc:
        raise InputError(f"CSV 解析失败（第 {reader.line_num} 行）：{exc}") from exc
    return rows


def _jsonl_rows(text: str, fields: Sequence[str]) -> list[Row]:
    rows: list[Row] = []
    for line, raw in enumerate(text.split("\n"), start=1):
        if not raw.strip():
            continue
        row = Row(line, None, None, {}, {})
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            row.malformed = f"不是合法 JSON：{exc.msg}"
            rows.append(row)
            continue
        if not isinstance(record, dict):
            row.malformed = "不是 JSON 对象"
            rows.append(row)
            continue
        row.observation_id = record.get("observation_id")
        row.entity_id = record.get("entity_id")
        for name in fields:
            column = f"recheck_{name}" if f"recheck_{name}" in record else name
            row.columns[name] = column
            value = record.get(column, _MISSING)
            if isinstance(value, str) and not value.strip():
                value = _MISSING
            row.values[name] = value
        note = record.get(NOTE_COLUMN)
        if isinstance(note, str) and note.strip():
            row.note = note.strip()
        rows.append(row)
    return rows


# ------------------------------------------------------------------ 取值解析


def _parse_bool(value: Any, *, nullable: bool) -> tuple[bool, bool | None]:
    if value is None:
        return nullable, None
    if isinstance(value, bool):
        return True, value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in TRUE_WORDS:
            return True, True
        if word in FALSE_WORDS:
            return True, False
        if word in NULL_WORDS:
            return nullable, None
    return False, None


def _parse_rank(value: Any) -> tuple[bool, int | None]:
    if value is None:
        return True, None
    if isinstance(value, bool):
        return False, None
    if isinstance(value, int):
        return value >= 1, value if value >= 1 else None
    if isinstance(value, str):
        word = value.strip().lower()
        if word in NULL_WORDS:
            return True, None
        if _INTEGER.fullmatch(word) and int(word) >= 1:
            return True, int(word)
    return False, None


def parse_value(name: str, value: Any) -> tuple[bool, Any]:
    if name == "rank":
        return _parse_rank(value)
    return _parse_bool(value, nullable=name == "recommended")


_ALLOWED = {
    "mentioned": "true / false（是 / 否），不能为空值",
    "recommended": "true / false（是 / 否）或 null（未判断）",
    "rank": "≥ 1 的整数，或 null（无位次）",
}


# ------------------------------------------------------------------ 计划


@dataclass(frozen=True)
class PlanItem:
    observation_id: str
    entity_id: str
    source: str
    annotation_id: str | None


def load_plan(root: Path) -> tuple[list[PlanItem], str] | None:
    """读取 recheck/plan.json，返回（计划项，文件 SHA-256）；没有计划文件时返回 None。"""
    path = root / PLAN_PATH
    if not path.exists():
        return None
    data = read_json(path)
    if not isinstance(data, dict) or data.get("kind") != "recheck_plan":
        raise InputError(f"{PLAN_PATH} 结构不对：应为 kind = recheck_plan 的 JSON 对象")
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise InputError(f"{PLAN_PATH} 结构不对：缺少 items 列表")
    items: list[PlanItem] = []
    seen: set[tuple[str, str]] = set()
    for position, item in enumerate(raw_items, start=1):
        where = f"{PLAN_PATH} 的第 {position} 个复核项"
        if not isinstance(item, dict):
            raise InputError(f"{where}不是 JSON 对象")
        observation_id, entity_id = item.get("observation_id"), item.get("entity_id")
        if not isinstance(observation_id, str) or not isinstance(entity_id, str):
            raise InputError(f"{where}缺少字符串类型的 observation_id 或 entity_id")
        source = item.get("source", "sample")
        if source not in SOURCES:
            raise InputError(f"{where}的 source 只能是 sample 或 dispute")
        annotation_id = item.get("annotation_id")
        if annotation_id is not None and not isinstance(annotation_id, str):
            raise InputError(f"{where}的 annotation_id 必须是字符串")
        if (observation_id, entity_id) in seen:
            raise InputError(f"{where}与前面的项重复：{observation_id} / {entity_id}")
        seen.add((observation_id, entity_id))
        items.append(PlanItem(observation_id, entity_id, source, annotation_id))
    return items, sha256_bytes(path.read_bytes())


# ------------------------------------------------------------------ 比对


@dataclass
class _Checker:
    data: ProjectData
    fields: Sequence[str]
    plan: dict[tuple[str, str], PlanItem] | None
    errors: list[dict[str, Any]] = field(default_factory=list)
    counted: dict[tuple[str, str], int] = field(default_factory=dict)

    def error(self, row: Row, code: str, column: str | None, message: str) -> None:
        self.errors.append({"line": row.line, "code": code, "column": column, "message": message})

    def _ids(self, row: Row) -> bool:
        ok = True
        for name, value, known, unknown_code, label in (
            (
                "observation_id",
                row.observation_id,
                self.data.observations,
                "unknown_observation",
                "观测",
            ),
            ("entity_id", row.entity_id, self.data.entities, "unknown_entity", "实体"),
        ):
            if not isinstance(value, str) or not value.strip():
                self.error(row, "missing_id", name, f"缺少{label}编号（{name}）")
                ok = False
            elif value not in known:
                self.error(row, unknown_code, name, f"未知{label}：{value}")
                ok = False
        return ok

    def _values(self, row: Row) -> dict[str, Any] | None:
        parsed: dict[str, Any] = {}
        ok = True
        for name in self.fields:
            column = row.columns[name]
            raw = row.values[name]
            if raw is _MISSING:
                self.error(
                    row,
                    "incomplete",
                    column,
                    f"{column} 没有填写：同一行的复核列要么全填，要么全空",
                )
                ok = False
                continue
            valid, value = parse_value(name, raw)
            if not valid:
                self.error(
                    row,
                    "invalid_value",
                    column,
                    f"{column} 的取值 {raw!r} 不合法：应为 {_ALLOWED[name]}",
                )
                ok = False
                continue
            parsed[name] = value
        if not ok:
            return None
        if parsed.get("mentioned") is False:
            if parsed.get("recommended") is True:
                self.error(row, "inconsistent", None, "复核结论自相矛盾：未提及却被推荐")
                return None
            if parsed.get("rank") is not None:
                self.error(row, "inconsistent", None, "复核结论自相矛盾：未提及却有位次")
                return None
        return parsed

    def _annotation(self, row: Row, key: tuple[str, str]) -> dict[str, Any] | None:
        item = self.plan.get(key) if self.plan is not None else None
        if item is not None and item.annotation_id is not None:
            annotation = self.data.annotations_by_id.get(item.annotation_id)
            if annotation is None or (annotation["observation_id"], annotation["entity_id"]) != key:
                self.error(
                    row,
                    "no_annotation",
                    None,
                    f"计划指向的标注 {item.annotation_id} 不存在或与该复核项不符，无法比对",
                )
                return None
            return annotation
        annotation = self.data.effective.get(key)
        if annotation is None:
            self.error(row, "no_annotation", None, f"{key[0]} / {key[1]} 没有生效标注，无法比对")
        return annotation

    def check(self, row: Row) -> tuple[dict[str, Any], dict[str, Any], str | None] | str:
        """返回（复核值，原标注，备注）；未填写返回 "blank"，非法返回 "invalid"。"""
        if row.malformed is not None:
            self.error(row, "malformed_row", None, row.malformed)
            return "invalid"
        before = len(self.errors)
        ids_ok = self._ids(row)
        if all(row.values[name] is _MISSING for name in self.fields):
            return "invalid" if len(self.errors) > before else "blank"
        values = self._values(row)
        if not ids_ok or values is None:
            return "invalid"
        key = (row.observation_id, row.entity_id)
        if key in self.counted:
            self.error(
                row, "duplicate", None, f"与第 {self.counted[key]} 行重复：{key[0]} / {key[1]}"
            )
            return "invalid"
        annotation = self._annotation(row, key)
        if annotation is None:
            return "invalid"
        self.counted[key] = row.line
        return values, annotation, row.note


def _summary(items: list[dict[str, Any]], planned: int | None) -> dict[str, Any]:
    """一组复核项的计数；planned 为 None 表示没有计划（不统计未复核数）。"""
    checked = len(items)
    agreed = sum(1 for item in items if item["agree"])
    return {
        "planned": planned or 0,
        "checked": checked,
        "agreed": agreed,
        "disagreed": checked - agreed,
        "not_checked": 0 if planned is None else planned - checked,
        "pass_rate": agreed / checked if checked else None,
    }


def compare(
    root: Path, input_path: Path, fields: Sequence[str], declared_format: str | None = None
) -> dict[str, Any]:
    """比对人工复核结果，返回 recheck/result.json 的内容（不写文件）。"""
    data = load_project_data(root)
    loaded = load_plan(root)
    parsed = read_input(input_path, fields, declared_format)
    plan_items = loaded[0] if loaded is not None else None
    plan = {(item.observation_id, item.entity_id): item for item in plan_items or []}
    checker = _Checker(data, fields, plan if loaded is not None else None)
    compared: dict[tuple[str, str], dict[str, Any]] = {}
    blank = invalid = 0
    for row in parsed.rows:
        outcome = checker.check(row)
        if outcome == "blank":
            blank += 1
            continue
        if isinstance(outcome, str):
            invalid += 1
            continue
        values, annotation, note = outcome
        key = (annotation["observation_id"], annotation["entity_id"])
        original = {name: annotation[name] for name in fields}
        differences = diff_fields(original, values, fields)
        plan_item = plan.get(key)
        compared[key] = {
            "observation_id": key[0],
            "entity_id": key[1],
            "agree": not differences,
            "differences": differences,
            "source": plan_item.source if plan_item is not None else None,
            "annotation_id": annotation["annotation_id"],
            "original": original,
            "rechecked": values,
            "note": note,
        }
    ordered = [compared[key] for key in sorted(compared)]
    if loaded is None:
        items, unplanned_items = ordered, []
    else:
        items = [item for item in ordered if (item["observation_id"], item["entity_id"]) in plan]
        unplanned_items = [
            item for item in ordered if (item["observation_id"], item["entity_id"]) not in plan
        ]
    planned_keys = sorted(plan)
    not_checked_items = [
        {"observation_id": key[0], "entity_id": key[1], "source": plan[key].source}
        for key in planned_keys
        if key not in compared
    ]
    totals = _summary(items, len(plan) if loaded is not None else None)
    by_source: dict[str, Any] = {}
    if loaded is not None:
        for source in SOURCES:
            planned = sum(1 for item in plan.values() if item.source == source)
            by_source[source] = _summary(
                [item for item in items if item["source"] == source], planned
            )
    notices = _notices(data, loaded, totals, unplanned_items, invalid)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "recheck_result",
        "project_id": data.project["project_id"],
        "plan_hash": loaded[1] if loaded is not None else None,
        "fields": list(fields),
        **totals,
        "null_reason": None
        if totals["checked"]
        else "没有已复核的项（checked = 0），一致率无法计算",
        "items": items,
        "not_checked_items": not_checked_items,
        "unplanned": len(unplanned_items),
        "unplanned_items": unplanned_items,
        "by_source": by_source,
        "input": {
            "format": parsed.format,
            "sha256": parsed.sha256,
            "rows": len(parsed.rows),
            "blank_rows": blank,
            "invalid_rows": invalid,
        },
        "errors": checker.errors,
        "notices": notices,
    }
    if data.project.get("synthetic") is True:
        result["synthetic"] = True
    return result


def _notices(
    data: ProjectData,
    loaded: tuple[list[PlanItem], str] | None,
    totals: Mapping[str, Any],
    unplanned_items: list[dict[str, Any]],
    invalid: int,
) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    if loaded is None:
        notices.append(
            notice(
                "no_plan",
                "warning",
                f"没有 {PLAN_PATH}：文件中每个合法的复核行都计入，结果不对应预先登记的抽样复核清单。",
            )
        )
    else:
        changed = sum(
            1
            for item in loaded[0]
            if item.annotation_id is not None
            and (current := data.effective.get((item.observation_id, item.entity_id))) is not None
            and current["annotation_id"] != item.annotation_id
        )
        if changed:
            notices.append(
                notice(
                    "annotations_changed_since_plan",
                    "warning",
                    f"生成计划后有 {changed} 个复核项的生效标注发生了变化：本次仍与计划所依据的标注比对；"
                    "如需复核新标注，请重新生成计划。",
                    count=changed,
                )
            )
    if unplanned_items:
        notices.append(
            notice(
                "unplanned_items",
                "warning",
                f"有 {len(unplanned_items)} 个复核项不在计划内：已列在 unplanned_items，不计入一致率。",
                count=len(unplanned_items),
            )
        )
    if totals["not_checked"]:
        notices.append(
            notice(
                "incomplete_recheck",
                "warning",
                f"计划内还有 {totals['not_checked']} 项没有复核：一致率只覆盖已复核的项。",
                count=totals["not_checked"],
            )
        )
    if invalid:
        notices.append(
            notice(
                "invalid_rows",
                "error",
                f"复核文件有 {invalid} 行不合法，未计入比对，已在 errors 中逐行列出。",
                count=invalid,
            )
        )
    return notices


def write_result(root: Path, result: Mapping[str, Any]) -> None:
    write_json(root / RESULT_PATH, dict(result))
