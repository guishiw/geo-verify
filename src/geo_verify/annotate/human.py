"""人工标注：覆盖（override）与批量导入（import）。全部历史保留，只追加。

可设置 / 可导入的字段（字段名与 override 的 --set 键、导入文件的列名一致）：

    mentioned  recommended  negated  rankable  rank  rank_list_size  sentiment
    risk.flags  risk.unfounded_negative  risk.identified  risk.confirmed  risk.closed
    citation_checks（仅导入）  note（仅导入；覆盖时 note 取 --reason）

取值：是否类为 true / false（也认 1 / 0、yes / no、是 / 否，不区分大小写）；rank、rank_list_size 为正整数；
sentiment 为 positive / neutral / negative；risk.flags 用 `|` 分隔（JSONL 中为数组）。
契约允许为 null 的字段（recommended、rank、rank_list_size、sentiment、risk.unfounded_negative、
risk.confirmed、risk.closed）写 null 或留空即为 null；其余字段不能为空。JSONL 中取值必须是对应的 JSON 类型。

覆盖规则
    取（观测，实体）当前生效的标注为底本，只改 --set 指定的字段，其余字段（含 citation_checks）沿用底本；
    spans、ext 不沿用（它们是规则的证据，不代表人工结论）。新标注 supersedes 指向底本，
    annotator = {type: human, id: --annotator, version: --annotator-version（默认 manual）}，
    note = --reason，ext.override.fields 记录本次设置的字段。合并后的标注须符合契约
    （如把 mentioned 改为 false 时须同时给出 recommended=false、negated=false、rank=null），
    rank 不得大于 rank_list_size，--now 不得早于底本时间，且写入后必须成为生效标注，否则为输入错误、不写入。
    审计事件 override：before_hash / after_hash 为底本与新标注的规范化 JSON 哈希。

导入规则
    逐行校验，合法行写入，非法行逐行报错（行号、列、原因）。新标注 supersedes = null：
    不覆盖任何标注者（含自己）的已有标注，生效规则仍由 project.effective_annotations 决定。
    同一标注者对同一（观测，实体）最新一条的内容与本行完全相同时不再写入；同一文件里重复的
    （观测，实体）：内容相同的跳过，内容不同的后一行报错。
    未提供的风险字段：flags = []、unfounded_negative = null（未评估）、identified = false、
    confirmed = null（未复核）、closed = null。
    citation_checks 为 JSON 数组，元素 {"order" 或 "url", "valid", "relevant"}；url 须与该观测的某条引用的
    url 完全一致，写入时换成 order 并按 order 排序。
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from geo_verify import audit, project, spec
from geo_verify.annotate.records import SCHEMA_VERSION, annotation_id, append_annotations
from geo_verify.canon import hash_obj, sha256_file
from geo_verify.collect.fileimport import detect_format, read_csv_table, read_text
from geo_verify.errors import InputError
from geo_verify.jsonl import dumps_line

DEFAULT_VERSION = "manual"
BOOLEAN_FIELDS = (
    "mentioned",
    "recommended",
    "negated",
    "rankable",
    "risk.unfounded_negative",
    "risk.identified",
    "risk.confirmed",
    "risk.closed",
)
INTEGER_FIELDS = ("rank", "rank_list_size")
SETTABLE = (
    "mentioned",
    "recommended",
    "negated",
    "rankable",
    "rank",
    "rank_list_size",
    "sentiment",
    "risk.flags",
    "risk.unfounded_negative",
    "risk.identified",
    "risk.confirmed",
    "risk.closed",
)
NULLABLE = frozenset(
    {
        "recommended",
        "rank",
        "rank_list_size",
        "sentiment",
        "risk.unfounded_negative",
        "risk.confirmed",
        "risk.closed",
    }
)
SENTIMENTS = ("positive", "neutral", "negative")
RISK_FLAGS = ("unfounded_negative", "wrong_fact", "outdated_fact", "compliance", "other")
RISK_DEFAULTS: dict[str, Any] = {
    "flags": [],
    "unfounded_negative": None,
    "identified": False,
    "confirmed": None,
    "closed": None,
}
CONTENT_FIELDS = (
    "mentioned",
    "recommended",
    "negated",
    "rankable",
    "rank",
    "rank_list_size",
    "sentiment",
    "risk",
    "citation_checks",
    "note",
)
_TRUE = frozenset({"true", "1", "yes", "y", "是"})
_FALSE = frozenset({"false", "0", "no", "n", "否"})
_NULL = frozenset({"", "null"})

# (列名, 是否必填, 说明)
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("observation_id", "是", "观测编号，须存在于 observations.jsonl"),
    ("entity_id", "是", "实体编号，须存在于 entities.jsonl"),
    ("mentioned", "是", "是否提及：true / false"),
    ("recommended", "是", "是否推荐：true / false；无法判断留空（null）"),
    ("negated", "是", "是否否定提及（如“不推荐 X”）：true / false"),
    ("rankable", "是", "回答中是否有可明确识别的推荐序列：true / false"),
    ("rank", "是", "实体在推荐序列中的序号（正整数）；没有位次留空"),
    ("rank_list_size", "否", "推荐序列的项数（正整数）；不适用留空"),
    ("sentiment", "是", "倾向：positive / neutral / negative；无法判断留空"),
    (
        "risk.flags",
        "否",
        "风险标记，用 | 分隔：unfounded_negative / wrong_fact / outdated_fact / compliance / other",
    ),
    ("risk.unfounded_negative", "否", "是否含无依据或错误的负面：true / false；未评估留空"),
    ("risk.identified", "否", "是否识别出风险：true / false；留空为 false"),
    ("risk.confirmed", "否", "复核是否确认为应识别的风险：true / false；未复核留空"),
    ("risk.closed", "否", "风险是否已闭环处置：true / false；留空为 null"),
    (
        "citation_checks",
        "否",
        '引用核验，JSON 数组：[{"order": 1, "valid": true, "relevant": true}]，'
        "也可用该观测引用的 url 代替 order；valid / relevant 可为 null",
    ),
    ("note", "否", "备注"),
)
COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _, _ in COLUMNS)
REQUIRED_COLUMNS: tuple[str, ...] = tuple(name for name, need, _ in COLUMNS if need == "是")
_TEMPLATE_ROW: dict[str, Any] = {
    "observation_id": "obs-000001",
    "entity_id": "brand-lanxu",
    "mentioned": True,
    "recommended": True,
    "negated": False,
    "rankable": True,
    "rank": 1,
    "rank_list_size": 3,
    "sentiment": "positive",
    "risk.unfounded_negative": False,
    "risk.identified": False,
    "note": "示例行（虚构），导入前请替换为实际标注",
}


class FieldProblem(Exception):
    def __init__(self, column: str | None, reason: str) -> None:
        super().__init__(reason)
        self.column = column
        self.reason = reason


def human_annotator(name: str, version: str) -> dict[str, str]:
    if not name or not name.strip():
        raise InputError("必须写明标注者（--annotator）")
    if not version or not version.strip():
        raise InputError("标注者版本（--annotator-version）不能为空")
    return {"type": "human", "id": name.strip(), "version": version.strip()}


# -- 取值解析 -------------------------------------------------------------


def parse_value(key: str, raw: Any, *, from_text: bool) -> Any:
    """把一个字段的取值转换成契约类型。from_text：取值来自命令行或 CSV 单元格（文本）。"""
    if from_text:
        assert isinstance(raw, str)
        token = raw.strip()
        if token.lower() in _NULL:
            if key == "risk.flags":
                return []
            if key not in NULLABLE:
                raise FieldProblem(key, "不能为空")
            return None
        return _parse_text(key, token)
    if raw is None:
        if key not in NULLABLE:
            raise FieldProblem(key, "不能为 null")
        return None
    return _check_json(key, raw)


def _parse_text(key: str, token: str) -> Any:
    if key in BOOLEAN_FIELDS:
        lowered = token.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise FieldProblem(key, f"无法识别的是否取值 {token!r}（可用 true / false）")
    if key in INTEGER_FIELDS:
        if not token.isdigit() or int(token) < 1:
            raise FieldProblem(key, f"应为正整数：{token!r}")
        return int(token)
    if key == "sentiment":
        return _check_json(key, token)
    return _check_json(key, [part.strip() for part in token.split("|") if part.strip()])


def _check_json(key: str, value: Any) -> Any:
    if key in BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise FieldProblem(key, f"应为 true / false：{value!r}")
        return value
    if key in INTEGER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise FieldProblem(key, f"应为正整数：{value!r}")
        return value
    if key == "sentiment":
        if value not in SENTIMENTS:
            raise FieldProblem(key, f"应为 {' / '.join(SENTIMENTS)}：{value!r}")
        return value
    if not isinstance(value, list) or any(item not in RISK_FLAGS for item in value):
        raise FieldProblem(key, f"风险标记只能取 {' / '.join(RISK_FLAGS)}：{value!r}")
    if len(set(value)) != len(value):
        raise FieldProblem(key, "风险标记重复")
    return list(value)


def parse_assignments(text: str) -> dict[str, Any]:
    """解析 --set 的 `k=v[,k=v]`。"""
    values: dict[str, Any] = {}
    pieces = [piece.strip() for piece in text.split(",") if piece.strip()]
    if not pieces:
        raise InputError("--set 至少要给出一个字段，如 --set recommended=false")
    for piece in pieces:
        key, sep, raw = piece.partition("=")
        key = key.strip()
        if not sep:
            raise InputError(f"--set 的写法应为 字段=值：{piece!r}（字段 {key}）")
        if key not in SETTABLE:
            raise InputError(f"--set 中的字段 {key} 不可设置；可设置：{'、'.join(SETTABLE)}")
        if key in values:
            raise InputError(f"--set 中的字段 {key} 重复")
        try:
            values[key] = parse_value(key, raw, from_text=True)
        except FieldProblem as exc:
            raise InputError(f"--set 中的字段 {key}：{exc.reason}") from None
    return values


def merge(base: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """在底本的结论字段上应用新取值，返回结论字段（risk 为嵌套对象）。"""
    content = {key: base[key] for key in CONTENT_FIELDS if key in base and key != "note"}
    risk = dict(RISK_DEFAULTS)
    risk.update(content.get("risk") or {})
    for key, value in values.items():
        if key.startswith("risk."):
            risk[key.removeprefix("risk.")] = value
        else:
            content[key] = value
    content["risk"] = risk
    content.setdefault("rank_list_size", None)
    return content


def rank_problem(content: dict[str, Any]) -> str | None:
    rank, size = content.get("rank"), content.get("rank_list_size")
    if isinstance(rank, int) and isinstance(size, int) and rank > size:
        return f"rank（{rank}）不能大于 rank_list_size（{size}）"
    return None


# -- 覆盖 -----------------------------------------------------------------


def override(
    root: str | Path,
    *,
    observation_id: str,
    entity_id: str,
    assignments: str,
    annotator: str,
    version: str,
    reason: str,
    now: str,
) -> dict[str, Any]:
    proj = project.load_project(root)
    who = human_annotator(annotator, version)
    if not reason or not reason.strip():
        raise InputError("人工覆盖必须写明理由（--reason）")
    if observation_id not in {item["observation_id"] for item in project.load_observations(root)}:
        raise InputError(f"观测不存在：{observation_id}")
    if entity_id not in {item["entity_id"] for item in project.load_entities(root)}:
        raise InputError(f"实体不存在：{entity_id}")
    values = parse_assignments(assignments)
    existing = project.load_annotations(root)
    base = project.effective_annotations(existing).get((observation_id, entity_id))
    if base is None:
        raise InputError(
            f"（{observation_id}，{entity_id}）还没有可覆盖的标注：请先运行 annotate rules 或 annotate import"
        )
    if now < base["created_at"]:
        raise InputError(f"覆盖时间 {now} 早于被覆盖标注的时间 {base['created_at']}")
    content = merge(base, values)
    problem = rank_problem(content)
    if problem:
        raise InputError(problem)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observation_id": observation_id,
        "project_id": base["project_id"],
        "entity_id": entity_id,
        **content,
        "annotator": who,
        "created_at": now,
        "supersedes": base["annotation_id"],
        "note": reason.strip(),
        "ext": {"override": {"fields": sorted(values)}},
    }
    if proj.get("synthetic") is True:
        record["synthetic"] = True
    record["annotation_id"] = annotation_id(dict(record))
    spec.require_valid("annotation", record, where="覆盖后的标注")
    if (
        project.effective_annotations([*existing, record])[(observation_id, entity_id)]
        is not record
    ):
        raise InputError(
            "覆盖标注写入后不会成为生效标注：该（观测，实体）还有时间不早于 --now 的其他人工标注，"
            "请换用更晚的 --now 时间"
        )
    changes = {
        key: {"from": _current(base, key), "to": value} for key, value in sorted(values.items())
    }
    paths = project.paths(root)
    append_annotations(paths.annotations, [record])
    event = audit.append_event(
        root,
        actor=who["id"],
        action="override",
        target_type="annotation",
        target_id=record["annotation_id"],
        reason=reason.strip(),
        occurred_at=now,
        before_hash=hash_obj(base),
        after_hash=hash_obj(record),
        details={
            "observation_id": observation_id,
            "entity_id": entity_id,
            "annotation_id": record["annotation_id"],
            "supersedes": base["annotation_id"],
            "changes": changes,
        },
        synthetic=True if proj.get("synthetic") is True else None,
    )
    return {
        "annotation_id": record["annotation_id"],
        "observation_id": observation_id,
        "entity_id": entity_id,
        "supersedes": base["annotation_id"],
        "annotator": who,
        "changes": changes,
        "audit_event_id": event["event_id"],
    }


def _current(annotation: dict[str, Any], key: str) -> Any:
    if key.startswith("risk."):
        return (annotation.get("risk") or {}).get(key.removeprefix("risk."))
    return annotation.get(key)


# -- 导入 -----------------------------------------------------------------


@dataclass
class RowError:
    line: int
    column: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "column": self.column, "reason": self.reason}


@dataclass
class ImportReport:
    source: str
    fmt: str
    annotator: dict[str, str]
    rows: int = 0
    written: int = 0
    skipped_identical: int = 0
    skipped_duplicate_in_input: int = 0
    rejected_rows: int = 0
    errors: list[RowError] = field(default_factory=list)
    audit_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.source,
            "format": self.fmt,
            "annotator": self.annotator,
            "rows": self.rows,
            "written": self.written,
            "skipped_identical": self.skipped_identical,
            "skipped_duplicate_in_input": self.skipped_duplicate_in_input,
            "rejected_rows": self.rejected_rows,
            "errors": [item.to_dict() for item in self.errors],
            "audit_event_id": self.audit_event_id,
        }


RowItem = tuple[int, dict[str, Any] | None, list[RowError], bool]


def _csv_rows(path: Path) -> Iterator[RowItem]:
    for line, cells in read_csv_table(path, COLUMN_NAMES, REQUIRED_COLUMNS):
        if cells is None:
            yield line, None, [RowError(line, None, "单元格数量多于表头列数")], True
        else:
            yield line, dict(cells), [], True


def _jsonl_rows(path: Path) -> Iterator[RowItem]:
    for line, raw in enumerate(read_text(path).split("\n"), start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            yield line, None, [RowError(line, None, f"不是合法 JSON：{exc.msg}")], False
            continue
        if not isinstance(value, dict):
            yield line, None, [RowError(line, None, "不是 JSON 对象")], False
            continue
        flat: dict[str, Any] = {}
        errors: list[RowError] = []
        for key, item in value.items():
            if key == "risk":
                if not isinstance(item, dict):
                    errors.append(RowError(line, "risk", "应为对象"))
                    continue
                flat.update({f"risk.{name}": inner for name, inner in item.items()})
            else:
                flat[key] = item
        errors.extend(RowError(line, key, "未知字段") for key in flat if key not in COLUMN_NAMES)
        errors.extend(
            RowError(line, key, "缺少必填字段") for key in REQUIRED_COLUMNS if key not in flat
        )
        yield line, flat, errors, False


def _citation_checks(
    raw: Any, observation: dict[str, Any], *, from_text: bool
) -> list[dict[str, Any]] | None:
    if from_text:
        if not str(raw).strip():
            return None
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FieldProblem("citation_checks", f"不是合法的 JSON：{exc.msg}") from None
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise FieldProblem("citation_checks", "应为 JSON 数组")
    citations = observation.get("citations") or []
    by_url = {item["url"]: item["order"] for item in citations}
    orders = {item["order"] for item in citations}
    checks: dict[int, dict[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - {"order", "url", "valid", "relevant"}:
            raise FieldProblem(
                "citation_checks", f"元素只能含 order / url / valid / relevant：{entry!r}"
            )
        if "valid" not in entry or "relevant" not in entry:
            raise FieldProblem(
                "citation_checks", f"元素须含 valid 和 relevant（可为 null）：{entry!r}"
            )
        order = entry.get("order")
        if "url" in entry:
            if entry["url"] not in by_url:
                raise FieldProblem("citation_checks", f"该观测没有这条引用：{entry['url']}")
            if order is not None and order != by_url[entry["url"]]:
                raise FieldProblem("citation_checks", f"order 与 url 不一致：{entry!r}")
            order = by_url[entry["url"]]
        if isinstance(order, bool) or not isinstance(order, int) or order not in orders:
            raise FieldProblem("citation_checks", f"该观测没有第 {order!r} 条引用")
        for key in ("valid", "relevant"):
            if entry[key] is not None and not isinstance(entry[key], bool):
                raise FieldProblem("citation_checks", f"{key} 应为 true / false / null：{entry!r}")
        if order in checks:
            raise FieldProblem("citation_checks", f"第 {order} 条引用重复核验")
        checks[order] = {"order": order, "valid": entry["valid"], "relevant": entry["relevant"]}
    return [checks[order] for order in sorted(checks)] or None


def _content_key(annotation: dict[str, Any]) -> str:
    return dumps_line({key: annotation.get(key) for key in CONTENT_FIELDS})


class _Importer:
    def __init__(self, root: str | Path, who: dict[str, str]) -> None:
        self.proj = project.load_project(root)
        self.observations = {
            item["observation_id"]: item for item in project.load_observations(root)
        }
        self.entities = {item["entity_id"] for item in project.load_entities(root)}
        self.existing = project.load_annotations(root)
        self.who = who
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for item in self.existing:
            if item["annotator"]["id"] != who["id"]:
                continue
            key = (item["observation_id"], item["entity_id"])
            current = latest.get(key)
            if current is None or (item["created_at"], item["annotation_id"]) >= (
                current["created_at"],
                current["annotation_id"],
            ):
                latest[key] = item
        self.latest = {key: _content_key(item) for key, item in latest.items()}
        self.known_ids = {item["annotation_id"] for item in self.existing}

    def build(self, row: dict[str, Any], *, from_text: bool, now: str) -> dict[str, Any]:
        problems: list[FieldProblem] = []
        observation_id = row.get("observation_id")
        entity_id = row.get("entity_id")
        observation = (
            self.observations.get(observation_id) if isinstance(observation_id, str) else None
        )
        if observation is None:
            problems.append(FieldProblem("observation_id", f"观测不存在：{observation_id!r}"))
        if not isinstance(entity_id, str) or entity_id not in self.entities:
            problems.append(FieldProblem("entity_id", f"实体不存在：{entity_id!r}"))
        values: dict[str, Any] = {}
        for key in SETTABLE:
            if key not in row:
                continue
            if from_text and key not in REQUIRED_COLUMNS and not str(row[key]).strip():
                continue  # CSV 中留空的可选列视为未提供
            try:
                values[key] = parse_value(key, row[key], from_text=from_text)
            except FieldProblem as exc:
                problems.append(exc)
        checks = None
        if observation is not None and "citation_checks" in row:
            try:
                checks = _citation_checks(row["citation_checks"], observation, from_text=from_text)
            except FieldProblem as exc:
                problems.append(exc)
        note = row.get("note")
        if note is not None and not isinstance(note, str):
            problems.append(FieldProblem("note", "应为文本"))
        if problems:
            raise _RowProblems(problems)
        assert observation is not None
        assert isinstance(entity_id, str)
        content = merge({}, values)
        if checks:
            content["citation_checks"] = checks
        problem = rank_problem(content)
        if problem:
            raise _RowProblems([FieldProblem("rank", problem)])
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "observation_id": observation["observation_id"],
            "project_id": observation["project_id"],
            "entity_id": entity_id,
            **content,
            "annotator": self.who,
            "created_at": now,
            "supersedes": None,
        }
        if isinstance(note, str) and note.strip():
            record["note"] = note.strip()
        if self.proj.get("synthetic") is True or observation.get("synthetic") is True:
            record["synthetic"] = True
        record["annotation_id"] = annotation_id(record)
        issues = spec.iter_issues("annotation", record)
        if issues:
            first = issues[0]
            raise _RowProblems(
                [FieldProblem(None, f"不符合 annotation 契约：{first.path or '/'} {first.message}")]
            )
        return record


class _RowProblems(Exception):
    def __init__(self, problems: list[FieldProblem]) -> None:
        super().__init__(problems[0].reason)
        self.problems = problems


def import_file(
    root: str | Path,
    source: Path,
    *,
    annotator: str,
    version: str,
    fmt: str | None,
    now: str,
) -> ImportReport:
    who = human_annotator(annotator, version)
    importer = _Importer(root, who)
    resolved = detect_format(source, fmt)
    report = ImportReport(source=source.name, fmt=resolved, annotator=who)
    rows = _csv_rows(source) if resolved == "csv" else _jsonl_rows(source)
    records: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], str] = {}
    for line, row, early, from_text in rows:
        report.rows += 1
        if early or row is None:
            report.errors.extend(early)
            report.rejected_rows += 1
            continue
        try:
            record = importer.build(row, from_text=from_text, now=now)
        except _RowProblems as exc:
            report.errors.extend(RowError(line, item.column, item.reason) for item in exc.problems)
            report.rejected_rows += 1
            continue
        key = (record["observation_id"], record["entity_id"])
        content = _content_key(record)
        if key in seen:
            if seen[key] == content:
                report.skipped_duplicate_in_input += 1
            else:
                report.errors.append(
                    RowError(line, None, "同一文件中该（观测，实体）重复出现且内容不同")
                )
                report.rejected_rows += 1
            continue
        seen[key] = content
        if importer.latest.get(key) == content or record["annotation_id"] in importer.known_ids:
            report.skipped_identical += 1
            continue
        records.append(record)
    paths = project.paths(root)
    before = sha256_file(paths.annotations) if paths.annotations.exists() else None
    report.written = append_annotations(paths.annotations, records)
    if report.written:
        event = audit.append_event(
            root,
            actor=who["id"],
            action="annotate",
            target_type="annotations",
            target_id=report.source,
            reason=f"导入标注者 {who['id']} 的人工标注（{report.source}）",
            occurred_at=now,
            before_hash=before,
            after_hash=sha256_file(paths.annotations),
            details={
                "annotator": who,
                "source_file": report.source,
                "source_sha256": sha256_file(source),
                "format": resolved,
                "rows": report.rows,
                "written": report.written,
                "skipped_identical": report.skipped_identical,
                "skipped_duplicate_in_input": report.skipped_duplicate_in_input,
                "rejected_rows": report.rejected_rows,
            },
            synthetic=True if importer.proj.get("synthetic") is True else None,
        )
        report.audit_event_id = event["event_id"]
    return report


def template(fmt: str) -> str:
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(COLUMN_NAMES)
        writer.writerow([_template_cell(_TEMPLATE_ROW.get(name)) for name in COLUMN_NAMES])
        return buffer.getvalue()
    sample: dict[str, Any] = {}
    risk: dict[str, Any] = {}
    for name in COLUMN_NAMES:
        if name not in _TEMPLATE_ROW and name not in REQUIRED_COLUMNS:
            continue
        value = _TEMPLATE_ROW.get(name)
        if name.startswith("risk."):
            risk[name.removeprefix("risk.")] = value
        else:
            sample[name] = value
    sample["risk"] = risk
    return dumps_line(sample) + "\n"


def _template_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def column_help() -> list[dict[str, str]]:
    return [{"name": name, "required": need, "description": text} for name, need, text in COLUMNS]
