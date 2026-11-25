"""CSV / JSONL 观测导入：模板、逐行错误报告、按 trace_id 去重（可断点续导）。

一行对应一条观测。CSV 的单元格都是文本，先按列转换成对应类型；JSONL 的每行是一个对象，
字段名与 CSV 列名相同（也接受一条完整的 observation 记录）。非法行不写入，合法行照常写入。
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from geo_verify import project
from geo_verify.canon import sha256_file
from geo_verify.collect.base import (
    CHANNELS,
    ENTRY_FORMS,
    INVALID_REASONS,
    SCHEMA_VERSION,
    FieldError,
    ObservationSink,
    collector,
    fill_null_reasons,
    observation_id_for,
    observation_issues,
    parse_bool,
    parse_citations,
    scope_problem,
    snapshot_entry,
    text_or_none,
    to_utc,
    trace_id_for,
)
from geo_verify.errors import InputError
from geo_verify.jsonl import dumps_line, write_json

DEFAULT_CHANNEL = "file_import"
ERRORS_RELPATH = "out/import-errors.json"
_FIELD_SIZE_LIMIT = 16 * 1024 * 1024

# (列名, 是否必填, 说明)
COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "trace_id",
        "否",
        "追踪编号，用于去重；留空时由“项目|题库哈希|平台|通道|窗口|题目|次数”确定生成",
    ),
    ("observation_id", "否", "观测编号；留空时确定生成"),
    ("query_id", "是", "题目编号，必须属于所指的封存题库版本"),
    ("prompt_text", "否", "实际提问原文；留空时取封存题目原文"),
    ("prompt_set_hash", "否", "所属封存题库的内容哈希；留空时取最新封存版本"),
    ("platform", "是", "平台编号，须在 project.json 的 platforms 中登记"),
    (
        "channel",
        "否",
        "采集通道：api / manual / share_import / browser_assist / file_import；留空为 file_import；"
        "须在 project.json 中为该平台登记",
    ),
    (
        "entry_form",
        "视情况",
        "入口形态：api / web / app / mini_program / plugin / other；通道为 api 时可留空",
    ),
    ("model_version", "否", "模型或产品版本标识；未知请留空，并填写 entry_features"),
    ("entry_features", "视情况", "入口特征描述；model_version 留空时必填"),
    ("window_id", "是", "时间窗口编号，如 W1"),
    ("attempt", "是", "同一窗口内第几次提问，从 1 开始"),
    ("started_at", "是", "开始时间，ISO 8601，须带时区（…Z 或 +08:00）；存储时换算为 UTC"),
    ("ended_at", "是", "结束时间，要求同上，且不早于开始时间"),
    ("logged_in", "否", "是否登录：true/false 或 是/否；未知留空"),
    ("history_cleared", "否", "是否清除历史：同上"),
    ("web_search", "否", "是否联网：同上"),
    ("region", "否", "地域；未知留空"),
    ("device", "否", "设备；未知留空"),
    ("answer_text", "视情况", "回答原文；status 为 valid 时必填"),
    (
        "citations",
        "否",
        '引用列表：JSON 数组 [{"url","placement","title"}]，或每行一条“placement | url | 标题”；'
        "placement 必填：inline=正文引用，source_list=仅在来源列表",
    ),
    (
        "snapshots",
        "否",
        '快照：JSON 数组（元素为项目内相对路径，或 {"path","sha256","media_type"}），'
        "也可用分号分隔多个相对路径；文件存在时自动计算 SHA-256",
    ),
    ("status", "是", "valid 或 invalid"),
    (
        "invalid_reason",
        "视情况",
        "status 为 invalid 时必填：timeout / rate_limited / empty_answer / truncated / garbled / error / other",
    ),
    ("operator", "否", "采集人；留空时取命令行 --operator"),
    ("collector_tool", "否", "采集工具名；留空为 geo-verify"),
    ("collector_version", "否", "采集工具版本；留空为本工具版本"),
    (
        "null_reasons",
        "否",
        'JSON 对象：为取值为空的字段说明原因，如 {"region":"页面未显示"}；不填则写入默认说明',
    ),
    ("ext", "否", "JSON 对象：扩展信息"),
    ("synthetic", "否", "是否为合成数据：true/false"),
)
COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _, _ in COLUMNS)
REQUIRED_COLUMNS: tuple[str, ...] = tuple(name for name, need, _ in COLUMNS if need == "是")
_EXTRA_JSONL_KEYS = frozenset({"schema_version", "project_id"})
_BOOL_COLUMNS = ("logged_in", "history_cleared", "web_search", "synthetic")
_TEXT_COLUMNS = (
    "trace_id",
    "observation_id",
    "query_id",
    "prompt_set_hash",
    "platform",
    "channel",
    "entry_form",
    "model_version",
    "entry_features",
    "window_id",
    "region",
    "device",
    "status",
    "invalid_reason",
    "operator",
    "collector_tool",
    "collector_version",
)
_RAW_TEXT_COLUMNS = frozenset({"answer_text", "prompt_text", "citations"})
_JSON_COLUMNS = frozenset({"null_reasons", "ext"})
_DEFAULT_REASONS = {
    "model_version": "导入文件未提供模型版本",
    "logged_in": "导入文件未提供登录状态",
    "history_cleared": "导入文件未提供是否清除历史",
    "web_search": "导入文件未提供是否联网",
    "region": "导入文件未提供地域",
    "device": "导入文件未提供设备",
    "answer_text": "导入文件未提供回答（无效观测）",
    "operator": "导入文件和命令行均未提供采集人",
    "snapshots": "导入文件未提供快照",
}

_SAMPLE_ROW: dict[str, str] = {
    "query_id": "Q001",
    "platform": "engine-a",
    "channel": "manual",
    "entry_form": "web",
    "entry_features": "网页端默认模式，页面未展示模型名（虚构示例）",
    "window_id": "W1",
    "attempt": "1",
    "started_at": "2026-10-08T10:00:00+08:00",
    "ended_at": "2026-10-08T10:00:20+08:00",
    "logged_in": "否",
    "history_cleared": "是",
    "web_search": "是",
    "region": "CN-SH",
    "answer_text": "可以考虑：1. 澜序 S7；2. 岑野 K5。（虚构示例回答）",
    "citations": "inline | https://lanxu-auto.example/s7/spec | 澜序 S7 参数（虚构）\n"
    "source_list | https://auto-review.example/ev-suv",
    "status": "valid",
    "operator": "采集员甲（虚构）",
    "synthetic": "true",
}

Problem = tuple[str | None, str]
RowItem = tuple[int, dict[str, Any] | None, list["RowError"]]


@dataclass(frozen=True)
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
    rows: int = 0
    skipped_existing: int = 0
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    rejected_rows: int = 0
    errors: list[RowError] = field(default_factory=list)
    prompt_text_differs: int = 0


# ---------------------------------------------------------------- 模板


def template(fmt: str) -> str:
    """导入模板：CSV 为表头加一行示例；JSONL 为一行示例对象。示例数据均为虚构。"""
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(COLUMN_NAMES)
        writer.writerow([_SAMPLE_ROW.get(name, "") for name in COLUMN_NAMES])
        return buffer.getvalue()
    if fmt == "jsonl":
        sample: dict[str, Any] = dict(_SAMPLE_ROW)
        sample.update(
            attempt=1,
            logged_in=False,
            history_cleared=True,
            web_search=True,
            synthetic=True,
            citations=[
                {
                    "url": "https://lanxu-auto.example/s7/spec",
                    "placement": "inline",
                    "title": "澜序 S7 参数（虚构）",
                },
                {"url": "https://auto-review.example/ev-suv", "placement": "source_list"},
            ],
        )
        return dumps_line(sample) + "\n"
    raise InputError(f"未知模板格式：{fmt}（可选 csv、jsonl）")


def column_help() -> list[dict[str, str]]:
    return [{"name": name, "required": need, "description": text} for name, need, text in COLUMNS]


# ---------------------------------------------------------------- 读取


def detect_format(path: Path, declared: str | None) -> str:
    if declared:
        return declared
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in {".jsonl", ".ndjson"}:
        return "jsonl"
    raise InputError(f"无法从扩展名判断格式，请用 --format 指明 csv 或 jsonl：{path.name}")


def read_text(path: Path) -> str:
    if not path.is_file():
        raise InputError(f"文件不存在：{path}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(f"{path.name} 不是 UTF-8 编码") from exc


def _csv_cell(name: str, raw: str) -> Any:
    """把 CSV 单元格文本转换成对应类型；空白一律视为未提供。"""
    text = raw.strip()
    if not text:
        return None
    if name == "attempt":
        try:
            return int(text)
        except ValueError:
            raise FieldError(name, f"应为正整数：{text!r}") from None
    if name in _JSON_COLUMNS or (name == "snapshots" and text.startswith("[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise FieldError(name, f"不是合法的 JSON：{exc.msg}") from None
    if name == "snapshots":
        return [part.strip() for part in text.split(";") if part.strip()]
    return raw if name in _RAW_TEXT_COLUMNS else text


def read_csv_table(
    path: Path, known: tuple[str, ...], required: tuple[str, ...]
) -> Iterator[tuple[int, dict[str, str] | None]]:
    """读取带表头的 CSV，逐条产出 (起始行号, {列: 原始文本})；单元格多于表头时产出 None。"""
    csv.field_size_limit(_FIELD_SIZE_LIMIT)
    try:
        reader = csv.reader(io.StringIO(read_text(path), newline=""))
        header = next(reader, None)
        if header is None:
            raise InputError(f"{path.name} 是空文件")
        names = [name.strip() for name in header]
        unknown = [name for name in names if name not in known]
        if unknown:
            raise InputError(f"{path.name} 含未知列：{'、'.join(unknown)}")
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise InputError(f"{path.name} 表头有重复列：{'、'.join(repeated)}")
        missing = [name for name in required if name not in names]
        if missing:
            raise InputError(f"{path.name} 缺少必需列：{'、'.join(missing)}")
        line = reader.line_num + 1
        for cells in reader:
            start, line = line, reader.line_num + 1
            if not any(cell.strip() for cell in cells):
                continue
            if len(cells) > len(names):
                yield start, None
            else:
                yield start, dict(zip(names, cells, strict=False))
    except csv.Error as exc:
        raise InputError(f"{path.name} 不是合法的 CSV：{exc}") from exc


def _csv_rows(path: Path) -> Iterator[RowItem]:
    for line, cells in read_csv_table(path, COLUMN_NAMES, REQUIRED_COLUMNS):
        if cells is None:
            yield line, None, [RowError(line, None, "单元格数量多于表头列数")]
            continue
        row: dict[str, Any] = {}
        errors: list[RowError] = []
        for name, raw in cells.items():
            try:
                value = _csv_cell(name, raw)
            except FieldError as exc:
                errors.append(RowError(line, exc.column, exc.reason))
                continue
            if value is not None:
                row[name] = value
        yield line, row, errors


def _jsonl_rows(path: Path) -> Iterator[RowItem]:
    for line, raw in enumerate(read_text(path).split("\n"), start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            yield line, None, [RowError(line, None, f"不是合法 JSON：{exc.msg}")]
            continue
        if not isinstance(value, dict):
            yield line, None, [RowError(line, None, "不是 JSON 对象")]
            continue
        row = {key: item for key, item in value.items() if item is not None}
        errors: list[RowError] = []
        collector_info = row.pop("collector", None)
        if isinstance(collector_info, dict):
            row.update(
                {
                    f"collector_{key}": collector_info[key]
                    for key in ("tool", "version")
                    if collector_info.get(key) is not None
                }
            )
        elif collector_info is not None:
            errors.append(RowError(line, "collector", "应为含 tool 和 version 的对象"))
        unknown = sorted(set(row) - set(COLUMN_NAMES) - _EXTRA_JSONL_KEYS)
        errors.extend(RowError(line, key, "未知字段") for key in unknown)
        yield line, row, errors


# ---------------------------------------------------------------- 行 → 观测


@dataclass
class ImportContext:
    root: Path
    project: dict[str, Any]
    versions: dict[str, int]
    texts: dict[str, dict[str, str]]
    latest_hash: str
    operator: str | None

    @classmethod
    def load(cls, root: str | Path, operator: str | None) -> ImportContext:
        sealed = project.load_sealed_prompt_sets(root)
        if not sealed:
            raise InputError("题库尚未封存：请先执行 geo-verify prompts seal，再导入观测")
        return cls(
            root=project.paths(root).root,
            project=project.load_project(root),
            versions={item["content_hash"]: item["version"] for item in sealed},
            texts={
                item["content_hash"]: {
                    prompt["query_id"]: prompt["text"] for prompt in item["prompts"]
                }
                for item in sealed
            },
            latest_hash=sealed[-1]["content_hash"],
            operator=operator.strip() if operator and operator.strip() else None,
        )


def _capture(problems: list[Problem], convert: Callable[..., Any], *args: Any) -> Any:
    try:
        return convert(*args)
    except FieldError as exc:
        problems.append((exc.column, exc.reason))
        return None


def _snapshots(root: Path, value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FieldError("snapshots", "应为快照列表")
    entries: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            entries.append(snapshot_entry(root, item))
        elif isinstance(item, dict) and isinstance(item.get("path"), str):
            entries.append(
                snapshot_entry(
                    root, item["path"], sha256=item.get("sha256"), media_type=item.get("media_type")
                )
            )
        else:
            raise FieldError("snapshots", "快照元素应为相对路径，或含 path 的对象")
    return entries


def _enum_problem(name: str, value: Any, allowed: tuple[str, ...]) -> Problem | None:
    if value is not None and value not in allowed:
        return name, f"取值 {value!r} 不在允许范围内（{' / '.join(allowed)}）"
    return None


def build_observation(
    row: dict[str, Any], ctx: ImportContext
) -> tuple[dict[str, Any] | None, list[Problem], bool]:
    """把一行转换成观测。返回 (观测或 None, [(列, 原因)], 提问原文是否与封存题目不同)。"""
    problems: list[Problem] = []
    values: dict[str, Any] = {
        name: _capture(problems, text_or_none, row.get(name), name) for name in _TEXT_COLUMNS
    }
    for name in _BOOL_COLUMNS:
        values[name] = _capture(problems, parse_bool, row.get(name), name)
    for name in ("answer_text", "prompt_text"):
        values[name] = _capture(problems, text_or_none, row.get(name), name)
    citations = _capture(problems, parse_citations, row.get("citations"))
    snapshots = _capture(problems, _snapshots, ctx.root, row.get("snapshots"))
    started = _capture(problems, to_utc, row.get("started_at"), "started_at")
    ended = _capture(problems, to_utc, row.get("ended_at"), "ended_at")

    attempt = row.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        problems.append(("attempt", "必填，应为不小于 1 的整数"))
    for name in ("null_reasons", "ext"):
        if name in row and not isinstance(row[name], dict):
            problems.append((name, "应为 JSON 对象"))
    project_id: str = ctx.project["project_id"]
    if row.get("project_id", project_id) != project_id:
        problems.append(("project_id", f"与本项目编号 {project_id} 不一致"))
    reported = {column for column, _ in problems}
    problems.extend(
        (name, "必填")
        for name in ("query_id", "platform", "window_id", "status")
        if values[name] is None and name not in reported
    )

    channel = values["channel"] or DEFAULT_CHANNEL
    status = values["status"]
    for name, value, allowed in (
        ("channel", channel, CHANNELS),
        ("entry_form", values["entry_form"], ENTRY_FORMS),
        ("status", status, ("valid", "invalid")),
        ("invalid_reason", values["invalid_reason"], INVALID_REASONS),
    ):
        problem = _enum_problem(name, value, allowed)
        if problem:
            problems.append(problem)

    prompt_set_hash = values["prompt_set_hash"] or ctx.latest_hash
    prompt_text = values["prompt_text"]
    differs = False
    if prompt_set_hash not in ctx.texts:
        problems.append(("prompt_set_hash", "不属于任何已封存的题库版本"))
    elif values["query_id"] is not None:
        sealed_text = ctx.texts[prompt_set_hash].get(values["query_id"])
        if sealed_text is None:
            problems.append(
                (
                    "query_id",
                    f"题目 {values['query_id']} 不在第 {ctx.versions[prompt_set_hash]} 版封存题库中",
                )
            )
        else:
            differs = prompt_text is not None and prompt_text != sealed_text
            prompt_text = prompt_text if prompt_text is not None else sealed_text

    platform = values["platform"]
    if platform is not None and channel in CHANNELS:
        scope = scope_problem(ctx.project, platform, channel)
        if scope:
            problems.append(scope)
    entry_form = values["entry_form"] or ("api" if channel == "api" else None)
    if entry_form is None:
        problems.append(("entry_form", f"必填（{' / '.join(ENTRY_FORMS)}）；仅通道为 api 时可留空"))
    answer = values["answer_text"]
    if status == "valid" and answer is None:
        problems.append(("answer_text", "status 为 valid 时必须有回答原文"))
    if status == "invalid" and values["invalid_reason"] is None:
        problems.append(("invalid_reason", "status 为 invalid 时必填"))
    if status == "valid" and values["invalid_reason"] is not None:
        problems.append(("invalid_reason", "status 为 valid 时必须留空"))
    if values["model_version"] is None and values["entry_features"] is None:
        problems.append(("entry_features", "模型版本未知时必须填写入口特征"))
    if started is not None and ended is not None and ended[1] < started[1]:
        problems.append(("ended_at", "结束时间早于开始时间"))
    if problems:
        return None, problems, differs

    parts = (
        project_id,
        prompt_set_hash,
        platform,
        channel,
        values["window_id"],
        values["query_id"],
        attempt,
    )
    observation: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observation_id": values["observation_id"] or observation_id_for(*parts),
        "project_id": project_id,
        "trace_id": values["trace_id"] or trace_id_for(*parts),
        "prompt_set_hash": prompt_set_hash,
        "query_id": values["query_id"],
        "prompt_text": prompt_text,
        "platform": platform,
        "entry_form": entry_form,
        "model_version": values["model_version"],
        "entry_features": values["entry_features"],
        "channel": channel,
        "started_at": started[0],
        "ended_at": ended[0],
        "logged_in": values["logged_in"],
        "history_cleared": values["history_cleared"],
        "web_search": values["web_search"],
        "region": values["region"],
        "device": values["device"],
        "answer_text": answer,
        "citations": citations,
        "snapshots": snapshots,
        "collector": collector(values["collector_tool"], values["collector_version"]),
        "operator": values["operator"] or ctx.operator,
        "window_id": values["window_id"],
        "attempt": attempt,
        "status": status,
        "invalid_reason": values["invalid_reason"],
    }
    if "null_reasons" in row:
        observation["null_reasons"] = dict(row["null_reasons"])
    fill_null_reasons(observation, _DEFAULT_REASONS)
    if "ext" in row:
        observation["ext"] = row["ext"]
    if ctx.project.get("synthetic") is True or values["synthetic"] is True:
        observation["synthetic"] = True
    issues = observation_issues(observation)
    if issues:
        return (
            None,
            [(issue.path.strip("/").split("/")[0] or None, issue.message) for issue in issues],
            differs,
        )
    return observation, [], differs


# ---------------------------------------------------------------- 主流程


def import_file(
    root: str | Path,
    source: Path,
    *,
    fmt: str | None = None,
    operator: str | None = None,
) -> tuple[ImportReport, ObservationSink]:
    """导入一个文件。合法行追加写入；非法行记入报告；已存在的 trace_id 跳过并计数。"""
    ctx = ImportContext.load(root, operator)
    resolved = detect_format(source, fmt)
    rows = _csv_rows(source) if resolved == "csv" else _jsonl_rows(source)
    report = ImportReport(source=source.name, fmt=resolved)
    seen_in_file: dict[str, int] = {}
    with ObservationSink(root) as sink:
        for line, row, early in rows:
            report.rows += 1
            if row is None or early:
                report.rejected_rows += 1
                report.errors.extend(early)
                continue
            observation, problems, differs = build_observation(row, ctx)
            if observation is None:
                explicit = row.get("trace_id")
                if isinstance(explicit, str) and sink.has_trace(explicit):
                    report.skipped_existing += 1
                    continue
                report.rejected_rows += 1
                report.errors.extend(RowError(line, column, reason) for column, reason in problems)
                continue
            trace_id = observation["trace_id"]
            if trace_id in seen_in_file:
                report.duplicates.append(
                    {"line": line, "trace_id": trace_id, "first_line": seen_in_file[trace_id]}
                )
                continue
            if sink.has_trace(trace_id):
                report.skipped_existing += 1
                continue
            if sink.id_conflict(observation["observation_id"], trace_id):
                report.rejected_rows += 1
                report.errors.append(
                    RowError(line, "observation_id", "观测编号已被另一条记录（不同 trace_id）占用")
                )
                continue
            seen_in_file[trace_id] = line
            report.prompt_text_differs += differs
            sink.add(observation)
    write_json(
        ctx.root / ERRORS_RELPATH,
        {
            "input": source.name,
            "input_sha256": sha256_file(source),
            "rows": report.rows,
            "rejected_rows": report.rejected_rows,
            "errors": [item.to_dict() for item in report.errors],
        },
    )
    return report, sink
