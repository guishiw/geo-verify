"""人工采集包：生成任务清单（pack）与回填导入（fill）。

人工通道由采集人在平台的网页、应用等入口逐题提问，再把看到的回答、引用和快照回填到
任务清单。本模块只负责出清单和导入回填结果，没有任何自动提问能力。

导入时逐行核对只读列（任务编号必须能由题目、次数、平台、窗口、题库哈希重新算出），
拿不到的字段填 null 并写明原因，不编造默认值；未回填回答的行只计数，不写入。
"""

from __future__ import annotations

import argparse
import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from geo_verify import audit, project
from geo_verify.canon import sha256_bytes, sha256_file
from geo_verify.cli import CommandResult
from geo_verify.collect.adapter import AdapterSpec, reject_options, require_options
from geo_verify.collect.base import (
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
    to_utc,
    trace_id_for,
)
from geo_verify.collect.fileimport import ImportContext, RowError, read_csv_table
from geo_verify.collect.tasks import TaskPlan, plan_tasks, split_ids
from geo_verify.errors import EXIT_CHECK_FAILED, InputError

CHANNEL = "manual"
DEFAULT_ENTRY_FORM = "web"
ACTIONS = ("pack", "fill")
TASKS_FILE = "tasks.csv"
README_FILE = "README.md"
SNAPSHOTS_DIR = "snapshots"
MAX_TEXT_ERRORS = 50

READ_ONLY_COLUMNS: tuple[str, ...] = (
    "trace_id",
    "query_id",
    "attempt",
    "platform",
    "window_id",
    "prompt_set_hash",
    "prompt_text",
)
MANUAL_ENTRY_FORMS: tuple[str, ...] = tuple(item for item in ENTRY_FORMS if item != "api")
# (列名, 是否必填, 填写说明)
FILL_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "entry_form",
        "否",
        f"入口形态：{' / '.join(MANUAL_ENTRY_FORMS)}。留空按 {DEFAULT_ENTRY_FORM}（网页端）记录；"
        "人工通道不是接口调用，不能填 api",
    ),
    (
        "answer_text",
        "视情况",
        "回答原文：整段原样粘贴，不改写、不删减。留空表示这一行尚未采集",
    ),
    ("citations", "否", "回答给出的引用，每行一条，写法见下文“引用的写法”；没有引用就留空"),
    (
        "snapshot_files",
        "否",
        "快照文件（截图、另存的页面等）的文件名，多个用分号分隔；"
        "路径相对于任务清单所在目录。导入时复制进项目的 snapshots/ 并计算 SHA-256",
    ),
    ("model_version", "视情况", "页面展示的模型或产品版本标识；页面没有展示就留空，并填写入口特征"),
    (
        "entry_features",
        "视情况",
        "入口特征，如“网页端默认模式，未展示模型名”；model_version 留空时必填",
    ),
    ("logged_in", "否", "提问时是否登录：是 / 否；不确定就留空"),
    ("history_cleared", "否", "提问前是否清除历史或新开对话：是 / 否；不确定就留空"),
    ("web_search", "否", "是否开启联网搜索：是 / 否；不确定就留空"),
    ("region", "否", "地域，如 CN-SH；不确定就留空"),
    ("device", "否", "设备，如 desktop-chrome；不确定就留空"),
    (
        "started_at",
        "是",
        "提问开始时间：ISO 8601 且必须带时区，如 2026-10-08T10:00:00+08:00；导入时换算为 UTC",
    ),
    ("ended_at", "是", "回答结束时间：要求同上，且不早于开始时间"),
    ("operator", "否", "采集人（可填代号）；留空时取导入命令的 --operator"),
    (
        "status",
        "否",
        "valid 或 invalid。留空时：填了 invalid_reason 记为 invalid，否则有回答记为 valid",
    ),
    (
        "invalid_reason",
        "视情况",
        f"无效原因：{' / '.join(INVALID_REASONS)}；status 为 invalid 时必填",
    ),
)
FILL_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _, _ in FILL_COLUMNS)
COLUMN_NAMES: tuple[str, ...] = READ_ONLY_COLUMNS + FILL_COLUMN_NAMES
REQUIRED_COLUMNS: tuple[str, ...] = (*READ_ONLY_COLUMNS, "answer_text", "started_at", "ended_at")

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_SNAPSHOT_SEPARATORS = re.compile(r"[;；\n]")
_ENVIRONMENT_LABELS: tuple[tuple[str, str], ...] = (
    ("logged_in", "是否登录"),
    ("history_cleared", "提问前是否清除历史"),
    ("web_search", "是否联网"),
    ("region", "地域"),
    ("device", "设备"),
)
_NULL_REASONS = {
    "model_version": "人工回填未填写模型或产品版本（见入口特征）",
    "logged_in": "人工回填未填写登录状态",
    "history_cleared": "人工回填未填写是否清除历史",
    "web_search": "人工回填未填写是否联网",
    "region": "人工回填未填写地域",
    "device": "人工回填未填写设备",
    "answer_text": "人工回填未提供回答（无效观测）",
    "operator": "任务清单和命令行均未填写采集人",
    "snapshots": "人工回填未提供快照文件",
}

Problem = tuple[str | None, str]


# ---------------------------------------------------------------- pack


def guard_formula(text: str) -> str:
    """防公式注入：以 = + - @ 等开头的单元格前加单引号，表格软件就不会把它当公式执行。"""
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def tasks_csv(plan: TaskPlan) -> str:
    """任务清单的 CSV 文本：只读列按任务填好，待回填列全部留空（不预填任何值）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMN_NAMES)
    blanks = [""] * len(FILL_COLUMN_NAMES)
    for task in plan.tasks:
        cells = [
            task.trace_id,
            task.query_id,
            str(task.attempt),
            plan.platform,
            plan.window_id,
            plan.prompt_set_hash,
            task.prompt_text,
        ]
        writer.writerow([guard_formula(cell) for cell in cells] + blanks)
    return buffer.getvalue()


def _environment_value(value: Any) -> str:
    if value is None:
        return "未登记（不作要求）"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def readme_text(plan: TaskPlan, proj: dict[str, Any]) -> str:
    """随任务清单发放的填写说明。内容只取决于项目配置和任务，不含生成时间。"""
    display = next(
        item["display_name"] for item in proj["platforms"] if item["platform_id"] == plan.platform
    )
    lines = ["# 人工采集任务清单", ""]
    if proj.get("synthetic") is True:
        lines += [
            "> 合成数据项目（synthetic: true）：本清单及回填内容均为演示用途，不代表任何真实效果。",
            "",
        ]
    lines += [
        f"- 项目：{plan.project_id}",
        f"- 平台：{plan.platform}（{display}）",
        f"- 采集通道：{CHANNEL}（人工采集）",
        f"- 时间窗口：{plan.window_id}",
        f"- 题库：第 {plan.prompt_set_version} 版，内容哈希 {plan.prompt_set_hash}",
        f"- 任务数：{len(plan.tasks)}（{len(plan.query_ids)} 题 × {plan.repeats} 次）",
        "",
        f"本清单由 `geo-verify collect --adapter {CHANNEL} --action pack` 生成。"
        "工具不会代替人提问：请采集人在平台入口逐条提问，把看到的内容如实回填到 "
        f"`{TASKS_FILE}`。",
        "",
        "## 采集步骤",
        "",
        f"1. 用表格软件打开 `{TASKS_FILE}`（UTF-8 编码）。每一行是一个任务：一道题的第几次提问。",
        "2. 按下面登记的采集环境打开平台入口，把 `prompt_text` 原样粘贴提问；一次只问一题，"
        "不追加任何引导语。以单引号开头的题目（如 `'=…`）是为了防止表格软件把它当成公式，"
        "提问时去掉开头的单引号。",
        "3. 回答完整输出后，把回答原文、引用、时间等填入该行的待回填列，并保存截图等快照文件。",
        "4. 保存为 CSV（UTF-8 编码）后执行：",
        "",
        "   ```",
        f"   geo-verify collect <项目目录> --adapter {CHANNEL} --action fill --input {TASKS_FILE}",
        "   ```",
        "",
        "没有回填回答的行计为“未完成”，不会写入；补填后重复执行同一条命令即可，"
        "已导入的行不会重复写入。观测写入后不可更改：某一行导入之后再改动表格内容不会生效。",
        "",
        "## 登记的采集环境",
        "",
    ]
    environment = proj["environment"]
    lines += [
        f"- {label}：{_environment_value(environment[key])}" for key, label in _ENVIRONMENT_LABELS
    ]
    lines += [
        "",
        "实际情况与登记不一致时照实填写，不要为了显得一致而改填。",
        "",
        "## 只读列（不要改动）",
        "",
        f"{'、'.join(f'`{name}`' for name in READ_ONLY_COLUMNS)} "
        "用来把回填结果对应到封存题库中的题目。导入时会核对这些列，被改动的行会被拒绝。",
        "",
        "## 待回填列",
        "",
        "| 列 | 是否必填 | 说明 |",
        "|---|---|---|",
    ]
    lines += [f"| `{name}` | {need} | {text} |" for name, need, text in FILL_COLUMNS]
    lines += [
        "",
        "## 引用的写法",
        "",
        "`citations` 每行一条，格式为“位置 | 链接 | 标题”，标题可省。位置必须写明：",
        "`inline` 表示正文引用，`source_list` 表示仅出现在来源列表。例如：",
        "",
        "```",
        "inline | https://lanxu-auto.example/s7 | 页面标题（虚构示例）",
        "source_list | https://auto-review.example/list",
        "```",
        "",
        "## 填写原则",
        "",
        "- 拿不到或不确定的信息一律留空，不要猜，也不要填“无”“未知”一类的占位词；"
        "留空的字段导入后记为 null 并注明原因。",
        "- 回答原文不改写、不删减、不纠错；引用按页面实际显示的顺序填写，重复的引用照原样保留。",
        "- 提问失败（超时、被限流、空回答、回答被截断或乱码）也要回填：把 `status` 写成 `invalid` "
        "并填写 `invalid_reason`。这样的记录计入无效观测，不进入指标分母；"
        "不要删除该行，也不要反复重问直到得到满意的回答。",
        "",
    ]
    return "\n".join(lines)


def write_pack(
    root: str | Path,
    out: Path,
    *,
    platform: str,
    window: str,
    repeats: int = 1,
    queries: list[str] | None = None,
) -> TaskPlan:
    """生成任务清单目录（tasks.csv + README.md）。全部检查通过后才创建目录，不覆盖已有内容。"""
    plan = plan_tasks(
        root, platform=platform, channel=CHANNEL, window=window, repeats=repeats, queries=queries
    )
    proj = project.load_project(root)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise InputError(f"输出目录已存在且不为空，拒绝覆盖：{out}")
    out.mkdir(parents=True, exist_ok=True)
    # CSV 带 BOM，便于表格软件正确识别中文；回填导入时会自动去掉
    (out / TASKS_FILE).write_text(tasks_csv(plan), encoding="utf-8-sig", newline="")
    (out / README_FILE).write_text(readme_text(plan, proj), encoding="utf-8", newline="\n")
    return plan


# ---------------------------------------------------------------- fill


@dataclass
class FillReport:
    source: str
    rows: int = 0
    pending_lines: list[int] = field(default_factory=list)
    skipped_existing: int = 0
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    rejected_rows: int = 0
    errors: list[RowError] = field(default_factory=list)
    snapshots_copied: int = 0


@dataclass(frozen=True)
class _Identity:
    """一行任务经核对后的只读信息。prompt_text 取封存题目原文，不取单元格内容。"""

    trace_id: str
    observation_id: str
    prompt_set_hash: str
    query_id: str
    platform: str
    window_id: str
    attempt: int
    prompt_text: str


@dataclass(frozen=True)
class _Snapshot:
    entry: dict[str, str]
    content: bytes


def _cell(cells: dict[str, str], name: str) -> str | None:
    """去掉首尾空白后的单元格文本；空白或缺列一律视为未填写。"""
    return cells.get(name, "").strip() or None


def _same_text(cell: str, sealed: str) -> bool:
    def normalize(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n").strip()

    return normalize(cell) in (normalize(sealed), normalize(guard_formula(sealed)))


def _identify(cells: dict[str, str], ctx: ImportContext) -> tuple[_Identity | None, list[Problem]]:
    """核对只读列：各列自身合法，且任务编号能由其余只读列重新算出。"""
    values = {name: _cell(cells, name) or "" for name in READ_ONLY_COLUMNS}
    problems: list[Problem] = [
        (name, "只读列不能为空（任务清单生成后不要改动只读列）")
        for name in READ_ONLY_COLUMNS
        if not values[name]
    ]
    if problems:
        return None, problems
    attempt_text = values["attempt"]
    attempt = int(attempt_text) if attempt_text.isascii() and attempt_text.isdigit() else 0
    if attempt < 1:
        problems.append(("attempt", f"应为不小于 1 的整数：{attempt_text!r}"))
    prompt_set_hash, query_id = values["prompt_set_hash"], values["query_id"]
    sealed_text: str | None = None
    if prompt_set_hash not in ctx.texts:
        problems.append(("prompt_set_hash", "不属于任何已封存的题库版本"))
    else:
        sealed_text = ctx.texts[prompt_set_hash].get(query_id)
        if sealed_text is None:
            problems.append(
                (
                    "query_id",
                    f"题目 {query_id} 不在第 {ctx.versions[prompt_set_hash]} 版封存题库中",
                )
            )
    scope = scope_problem(ctx.project, values["platform"], CHANNEL)
    if scope:
        problems.append(scope)
    if problems or sealed_text is None:
        return None, problems
    parts = (
        ctx.project["project_id"],
        prompt_set_hash,
        values["platform"],
        CHANNEL,
        values["window_id"],
        query_id,
        attempt,
    )
    if values["trace_id"] != trace_id_for(*parts):
        return None, [
            (
                "trace_id",
                "与本行的题目、次数、平台、窗口、题库哈希对不上：只读列被改动过，"
                "或这一行不是本项目生成的任务",
            )
        ]
    if not _same_text(cells["prompt_text"], sealed_text):
        return None, [("prompt_text", "与封存题目原文不一致：只读列被改动过")]
    return (
        _Identity(
            trace_id=values["trace_id"],
            observation_id=observation_id_for(*parts),
            prompt_set_hash=prompt_set_hash,
            query_id=query_id,
            platform=values["platform"],
            window_id=values["window_id"],
            attempt=attempt,
            prompt_text=sealed_text,
        ),
        [],
    )


def _read_snapshots(
    raw: str, base_dir: Path, root: Path, observation_id: str
) -> dict[str, _Snapshot]:
    """读取一行列出的快照文件，返回 {项目内相对路径: 快照}。只读取，不写入项目目录。

    快照按文件名存入 snapshots/<观测编号>/。同名而内容不同的文件会互相覆盖，一律拒绝；
    同名且内容相同（含项目里已存在的那一份）视为同一份快照。
    """
    column = "snapshot_files"
    found: dict[str, _Snapshot] = {}
    for piece in _SNAPSHOT_SEPARATORS.split(raw):
        name = piece.strip().replace("\\", "/")
        if not name:
            continue
        parts = name.split("/")
        if name.startswith("/") or ".." in parts or not parts[-1]:
            raise FieldError(column, f"快照应写成相对路径的文件名，且不能包含 ..：{name!r}")
        source = base_dir / name
        if not source.is_file():
            raise FieldError(column, f"快照文件不存在：{name}（相对于目录 {base_dir.name}）")
        content = source.read_bytes()
        digest = sha256_bytes(content)
        stored = f"{SNAPSHOTS_DIR}/{observation_id}/{parts[-1]}"
        if stored in found:
            if found[stored].entry["sha256"] != digest:
                raise FieldError(
                    column, f"多个快照文件同名但内容不同，导入后会互相覆盖：{parts[-1]}（请先改名）"
                )
            continue
        target = root / stored
        if target.exists() and (not target.is_file() or sha256_file(target) != digest):
            raise FieldError(column, f"项目中已有同名快照且内容不同，拒绝覆盖：{stored}")
        found[stored] = _Snapshot(snapshot_entry(root, stored, sha256=digest), content)
    return found


def _store_snapshots(root: Path, snapshots: dict[str, _Snapshot]) -> int:
    """把快照写入项目目录，返回新写入的文件数。已存在的同内容文件不重写，任何文件都不覆盖。"""
    copied = 0
    for stored, snapshot in snapshots.items():
        target = root / stored
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(snapshot.content)
        copied += 1
    return copied


def _build(
    cells: dict[str, str], identity: _Identity, ctx: ImportContext, base_dir: Path
) -> tuple[dict[str, Any] | None, list[Problem], dict[str, _Snapshot]]:
    """把一行已回填的任务转换成观测。返回 (观测或 None, [(列, 原因)], 待写入的快照)。"""
    problems: list[Problem] = []

    def capture(convert: Any, *args: Any) -> Any:
        try:
            return convert(*args)
        except FieldError as exc:
            problems.append((exc.column, exc.reason))
            return None

    answer = cells.get("answer_text", "") if _cell(cells, "answer_text") else None
    invalid_reason = (_cell(cells, "invalid_reason") or "").lower() or None
    status = (_cell(cells, "status") or "").lower() or (
        "invalid" if invalid_reason is not None else "valid"
    )
    entry_form = (_cell(cells, "entry_form") or DEFAULT_ENTRY_FORM).lower()
    model_version = _cell(cells, "model_version")
    entry_features = _cell(cells, "entry_features")

    if entry_form not in MANUAL_ENTRY_FORMS:
        problems.append(
            (
                "entry_form",
                f"取值 {entry_form!r} 不在允许范围内（{' / '.join(MANUAL_ENTRY_FORMS)}）；"
                "人工通道不是接口调用，不能填 api",
            )
        )
    if status not in ("valid", "invalid"):
        problems.append(("status", f"取值 {status!r} 不在允许范围内（valid / invalid）"))
    if invalid_reason is not None and invalid_reason not in INVALID_REASONS:
        problems.append(
            (
                "invalid_reason",
                f"取值 {invalid_reason!r} 不在允许范围内（{' / '.join(INVALID_REASONS)}）",
            )
        )
    elif status == "invalid" and invalid_reason is None:
        problems.append(("invalid_reason", "status 为 invalid 时必填"))
    elif status == "valid" and invalid_reason is not None:
        problems.append(("invalid_reason", "status 为 valid 时必须留空"))
    if status == "valid" and answer is None:
        problems.append(("answer_text", "status 为 valid 时必须有回答原文"))
    if model_version is None and entry_features is None:
        problems.append(("entry_features", "模型版本未知时必须填写入口特征"))

    flags = {
        name: capture(parse_bool, cells.get(name), name)
        for name in ("logged_in", "history_cleared", "web_search")
    }
    started = capture(to_utc, cells.get("started_at"), "started_at")
    ended = capture(to_utc, cells.get("ended_at"), "ended_at")
    if started is not None and ended is not None and ended[1] < started[1]:
        problems.append(("ended_at", "结束时间早于开始时间"))
    citations = capture(parse_citations, cells.get("citations"))
    snapshots: dict[str, _Snapshot] = (
        capture(
            _read_snapshots,
            cells.get("snapshot_files", ""),
            base_dir,
            ctx.root,
            identity.observation_id,
        )
        or {}
    )
    if problems:
        return None, problems, {}

    observation: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observation_id": identity.observation_id,
        "project_id": ctx.project["project_id"],
        "trace_id": identity.trace_id,
        "prompt_set_hash": identity.prompt_set_hash,
        "query_id": identity.query_id,
        "prompt_text": identity.prompt_text,
        "platform": identity.platform,
        "entry_form": entry_form,
        "model_version": model_version,
        "entry_features": entry_features,
        "channel": CHANNEL,
        "started_at": started[0],
        "ended_at": ended[0],
        **flags,
        "region": _cell(cells, "region"),
        "device": _cell(cells, "device"),
        "answer_text": answer,
        "citations": citations,
        "snapshots": [snapshot.entry for snapshot in snapshots.values()],
        "collector": collector(),
        "operator": _cell(cells, "operator") or ctx.operator,
        "window_id": identity.window_id,
        "attempt": identity.attempt,
        "status": status,
        "invalid_reason": invalid_reason,
    }
    fill_null_reasons(observation, _NULL_REASONS)
    if ctx.project.get("synthetic") is True:
        observation["synthetic"] = True
    issues = observation_issues(observation)
    if issues:
        return (
            None,
            [(issue.path.strip("/").split("/")[0] or None, issue.message) for issue in issues],
            {},
        )
    return observation, [], snapshots


def fill_file(
    root: str | Path,
    source: Path,
    *,
    snapshots_dir: Path | None = None,
    operator: str | None = None,
) -> tuple[FillReport, ObservationSink]:
    """导入回填后的任务清单。合法行追加写入；非法行记入报告；未回填的行只计数。"""
    ctx = ImportContext.load(root, operator)
    if not source.is_file():
        raise InputError(f"文件不存在：{source}")
    base_dir = source.parent if snapshots_dir is None else snapshots_dir
    if not base_dir.is_dir():
        raise InputError(f"快照目录不存在：{base_dir}")
    report = FillReport(source=source.name)
    written_lines: dict[str, int] = {}

    def reject(line: int, problems: list[Problem]) -> None:
        report.rejected_rows += 1
        report.errors.extend(RowError(line, column, reason) for column, reason in problems)

    with ObservationSink(root) as sink:
        for line, cells in read_csv_table(source, COLUMN_NAMES, REQUIRED_COLUMNS):
            report.rows += 1
            if cells is None:
                reject(line, [(None, "单元格数量多于表头列数")])
                continue
            identity, problems = _identify(cells, ctx)
            if identity is None:
                reject(line, problems)
                continue
            if identity.trace_id in written_lines:
                report.duplicates.append(
                    {
                        "line": line,
                        "trace_id": identity.trace_id,
                        "first_line": written_lines[identity.trace_id],
                    }
                )
                continue
            if sink.has_trace(identity.trace_id):
                report.skipped_existing += 1
                continue
            if not any(_cell(cells, name) for name in ("answer_text", "status", "invalid_reason")):
                report.pending_lines.append(line)
                continue
            observation, problems, snapshots = _build(cells, identity, ctx, base_dir)
            if observation is None:
                reject(line, problems)
                continue
            if sink.id_conflict(identity.observation_id, identity.trace_id):
                reject(line, [("trace_id", "对应的观测编号已被另一条记录（不同 trace_id）占用")])
                continue
            report.snapshots_copied += _store_snapshots(ctx.root, snapshots)
            written_lines[identity.trace_id] = line
            sink.add(observation)
    return report, sink


# ---------------------------------------------------------------- 命令行


def _run_pack(args: argparse.Namespace) -> CommandResult:
    context = f"--adapter {CHANNEL} --action pack"
    require_options(args, context, "--platform", "--window", "--out")
    reject_options(args, context, "--input", "--snapshots-dir", hint="（它们只用于 --action fill）")
    out = Path(args.out)
    plan = write_pack(
        args.project,
        out,
        platform=args.platform,
        window=args.window,
        repeats=1 if args.repeats is None else args.repeats,
        queries=split_ids(args.queries),
    )
    data: dict[str, Any] = {
        "adapter": CHANNEL,
        "action": "pack",
        "channel": CHANNEL,
        "platform": plan.platform,
        "window_id": plan.window_id,
        "prompt_set_version": plan.prompt_set_version,
        "prompt_set_hash": plan.prompt_set_hash,
        "queries": len(plan.query_ids),
        "repeats": plan.repeats,
        "tasks": len(plan.tasks),
        "out": out.name,
        "files": [TASKS_FILE, README_FILE],
        "tasks_sha256": sha256_file(out / TASKS_FILE),
    }
    text = "\n".join(
        [
            f"已生成人工采集任务清单：{len(plan.tasks)} 个任务"
            f"（{len(plan.query_ids)} 题 × {plan.repeats} 次），平台 {plan.platform}，"
            f"窗口 {plan.window_id}，题库第 {plan.prompt_set_version} 版。",
            f"文件：{out / TASKS_FILE}（任务清单）、{out / README_FILE}（填写说明）。",
            f"回填后执行：geo-verify collect {args.project} --adapter {CHANNEL} --action fill "
            f"--input {out / TASKS_FILE}",
        ]
    )
    return CommandResult(data=data, text=text)


def _run_fill(args: argparse.Namespace) -> CommandResult:
    context = f"--adapter {CHANNEL} --action fill"
    require_options(args, context, "--input")
    reject_options(
        args,
        context,
        "--platform",
        "--window",
        "--repeats",
        "--queries",
        "--out",
        hint="（平台、窗口、题目和次数都取自任务清单的只读列）",
    )
    occurred_at = audit.resolve_now(args.now)
    paths = project.paths(args.project)
    proj = project.load_project(args.project)
    source = Path(args.input)
    operator = args.operator.strip() if args.operator and args.operator.strip() else None
    before = sha256_file(paths.observations) if paths.observations.exists() else None
    report, sink = fill_file(
        args.project,
        source,
        snapshots_dir=Path(args.snapshots_dir) if args.snapshots_dir else None,
        operator=operator,
    )
    counts = sink.counts()
    errors = [item.to_dict() for item in report.errors]
    summary: dict[str, Any] = {
        "rows": report.rows,
        **counts,
        "skipped_existing": report.skipped_existing,
        "skipped_duplicate_in_input": len(report.duplicates),
        "pending": len(report.pending_lines),
        "rejected_rows": report.rejected_rows,
        "snapshots_copied": report.snapshots_copied,
    }
    data: dict[str, Any] = {
        "adapter": CHANNEL,
        "action": "fill",
        "channel": CHANNEL,
        "input": report.source,
        **summary,
        "pending_lines": report.pending_lines,
        "duplicates": report.duplicates,
        "errors": errors,
        "errors_total": len(errors),
        "audit_event_id": None,
    }
    if counts["written"]:
        event = audit.append_event(
            args.project,
            actor=operator or "geo-verify collect（未指明操作人）",
            action="collect",
            target_type="observations",
            target_id=report.source,
            reason=f"人工采集包回填导入：{report.source}",
            occurred_at=occurred_at,
            before_hash=before,
            after_hash=sha256_file(paths.observations),
            details={
                "adapter": CHANNEL,
                "channel": CHANNEL,
                "source_file": report.source,
                "source_sha256": sha256_file(source),
                **summary,
            },
            synthetic=True if proj.get("synthetic") is True else None,
        )
        data["audit_event_id"] = event["event_id"]

    lines = [
        f"回填导入 {report.source}：共 {report.rows} 行，写入 {counts['written']} 条"
        f"（有效 {counts['valid']}，无效 {counts['invalid']}），"
        f"跳过已存在 {report.skipped_existing} 条，未完成 {len(report.pending_lines)} 行，"
        f"文件内重复 {len(report.duplicates)} 条，非法 {report.rejected_rows} 行。"
    ]
    for item in errors[:MAX_TEXT_ERRORS]:
        column = f" [{item['column']}]" if item["column"] else ""
        lines.append(f"  第 {item['line']} 行{column}：{item['reason']}")
    if len(errors) > MAX_TEXT_ERRORS:
        lines.append(f"  ……共 {len(errors)} 处错误，完整清单请加 --json 查看")
    return CommandResult(
        data=data,
        text="\n".join(lines),
        exit_code=EXIT_CHECK_FAILED if report.rejected_rows else 0,
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(f"人工采集包（--adapter {CHANNEL}）")
    group.add_argument(
        "--action",
        choices=ACTIONS,
        help="pack=生成任务清单（需 --platform、--window、--out）；fill=导入回填结果（需 --input）",
    )
    group.add_argument("--out", help="pack：任务清单输出目录（须不存在或为空）")
    group.add_argument("--snapshots-dir", help="fill：快照文件所在目录，默认取任务清单所在的目录")


def run(args: argparse.Namespace) -> CommandResult:
    if args.action is None:
        raise InputError(
            f"--adapter {CHANNEL} 需要 --action pack（生成任务清单）或 --action fill（导入回填结果）"
        )
    return _run_pack(args) if args.action == "pack" else _run_fill(args)


ADAPTER = AdapterSpec(
    name=CHANNEL,
    summary="人工采集包：生成任务清单，人工回填回答、引用和快照后导入",
    add_arguments=add_arguments,
    run=run,
)
