"""全流程联调：经 CLI 从手工准备的输入跑到预检报告。

prompts lint → prompts seal → import → annotate rules →
annotate import（两名标注者）→ annotate kappa → annotate override（裁决）→ annotate kappa →
recheck-plan → recheck-compare → metrics → crosscheck → alerts →
compare（与基线期项目）→ evidence build → evidence verify → report。

输入数据见 tests/helpers_pipeline.py（虚构）。期望值全部按那里的计划手工推算：
- 提及率：api 33/36、manual 30/36，合并 63/72；偏离度 = (3/36) / (63/72) = 2/21 ≈ 0.0952 ≤ 0.3。
- 各题合并提及率 Q001～Q004 为 1，Q005 为 9/12，Q006 为 6/12；6 道题的按题聚类区间远高于 0.6。
- 基线期每题每通道一半提及（36/72）；同题配对差值 = 平均(1,1,1,1,0.75,0.5) − 0.5 = 0.375，
  每道题都不下降、且有 4 道题差 0.5，区间下界 > 0 → “提升”。
- kappa：8 项，mentioned 两人全一致（4 是 4 否）→ 1；recommended 一处分歧，
  p_o = 7/8，p_e = (4/8)(3/8) + (4/8)(5/8) = 1/2 → (7/8 − 1/2) / (1/2) = 0.75。
- 抽样复核：⌈0.2 × 6⌉ = 2 道题，每题 12 条有效观测 × 4 个实体 = 96 项，复核结论照抄原标注 → 一致率 1。
- 每个窗口的回答写法相同 → alerts 无告警。

另有两个“故意不通过”的变体：交叉验证偏离度超过阈值（R-CROSSCHECK），以及证据被篡改（R-EVIDENCE）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import helpers_pipeline as hp
from conftest import run_cli_json
from helpers_recheck import reference_sample

from geo_verify import spec
from geo_verify.report.inputs import load_inputs

SEALED_AT = "2026-10-02T03:00:00Z"
RULE_IDS = (
    "R-EVIDENCE",
    "R-EXCLUSION-UNREGISTERED",
    "R-EXCLUSION-SELECTIVE",
    "R-PREREG-CHANGED",
    "R-PROMPTS-SEALED",
    "R-METRICS",
    "R-ACCEPTANCE-DEFINED",
    "R-SUFFICIENCY",
    "R-MODEL-VERSION",
    "R-THRESHOLD",
    "R-CROSSCHECK",
    "R-RECHECK",
    "R-DISPUTES",
    "R-ALERTS",
    "R-COMPARE",
    "R-PROMPT-VERSION",
    "R-DATA-NOTICES",
    "R-INPUT-CONSISTENCY",
)


def cli(expected: int, *argv: str) -> dict[str, Any]:
    code, data = run_cli_json(*argv)
    assert code == expected, (argv, code, data)
    assert isinstance(data, dict)
    return data


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_project(root: Path, plan: hp.Plan, dates: dict[str, str], *, now: str) -> None:
    """手工输入 → 题库检查与封存 → 导入观测 → 规则标注。"""
    hp.write_inputs(root)
    source = root.parent / f"{root.name}-observations.csv"
    hp.write_csv(source, hp.OBSERVATION_COLUMNS, hp.observation_rows(plan, dates))
    lint = cli(0, "prompts", "lint", str(root))
    assert {item["severity"] for item in lint["findings"]} == {"warning"}  # 只有意图类型提醒
    cli(0, "prompts", "seal", str(root), "--by", "甲方项目负责人（虚构）", "--now", SEALED_AT)
    imported = cli(0, "import", str(root), "--input", str(source), "--now", now)
    assert (imported["written"], imported["valid"], imported["invalid"]) == (73, 72, 1)
    rules = cli(0, "annotate", "rules", str(root), "--now", now)
    assert rules["observations_eligible"] == 72  # 无效观测不标注
    assert rules["written"] == 72 * 4


def run_pipeline(
    root: Path, plan: hp.Plan, *, crosscheck_exit: int = 0, baseline: Path | None = None
) -> None:
    collect_project(root, plan, hp.WINDOW_DATES, now="2026-10-11T01:00:00Z")
    project = str(root)
    # 两名标注者各标 8 条（4 条提及、4 条未提及），乙在一条上把“推荐”标成否
    mentioned, other = hp.kappa_sample(root / "observations.jsonl")
    sheet_a, sheet_b = root.parent / "annotator-a.csv", root.parent / "annotator-b.csv"
    hp.write_csv(sheet_a, hp.ANNOTATION_COLUMNS, hp.human_rows(mentioned, other))
    hp.write_csv(sheet_b, hp.ANNOTATION_COLUMNS, hp.human_rows(mentioned, other, flip=mentioned[0]))
    for sheet, name, now in ((sheet_a, "标注员甲", "03:00"), (sheet_b, "标注员乙", "03:10")):
        cli(0, "annotate", "import", project, "--input", str(sheet), "--annotator", name,
            "--now", f"2026-10-11T{now}:00Z")  # fmt: skip
    kappa = ("annotate", "kappa", project, "--a", "标注员甲", "--b", "标注员乙")
    disputed = cli(1, *kappa)
    assert disputed["unresolved"] == 1
    cli(0, "annotate", "override", project, "--observation", mentioned[0], "--entity",
        "brand-lanxu", "--set", "recommended=true", "--annotator", "裁决人丙",
        "--reason", "回答以编号列表推荐澜序 S7（虚构）", "--now", "2026-10-11T03:20:00Z")  # fmt: skip
    cli(0, *kappa)
    cli(0, "recheck-plan", project, "--now", "2026-10-11T04:00:00Z")
    filled = root.parent / f"{root.name}-recheck.csv"
    assert hp.fill_recheck(root / "recheck" / "plan.csv", filled) == 96
    cli(0, "recheck-compare", project, "--input", str(filled))
    cli(0, "metrics", project)
    cli(crosscheck_exit, "crosscheck", project)
    cli(0, "alerts", project)
    if baseline is not None:
        cli(0, "compare", str(baseline), project)
    cli(0, "evidence", "build", project, "--now", "2026-10-11T05:00:00Z")
    cli(0, "evidence", "verify", project)


def baseline_project(root: Path) -> Path:
    collect_project(root, hp.plan_baseline, hp.BASELINE_DATES, now="2026-09-11T01:00:00Z")
    return root


# docs/outputs.md 约定的字段（独立于各实现手写；实现可以多给字段，但不能少）
DOCUMENTED: dict[str, tuple[str, set[str]]] = {
    "out/metrics.json": (
        "metrics",
        {
            "target_entity_id",
            "timezone",
            "prompt_set",
            "parameters",
            "sufficiency_thresholds",
            "observations",
            "registered_metrics",
            "notices",
            "exclusion_audit",
            "results",
        },
    ),
    "out/crosscheck.json": (
        "crosscheck",
        {
            "prompt_set",
            "formula",
            "formula_text",
            "formula_note",
            "threshold",
            "preregistered",
            "metric_id",
            "channel_a",
            "channel_b",
            "parameters",
            "status",
            "status_label",
            "reason",
            "results",
        },
    ),
    "out/compare.json": (
        "compare",
        {"baseline", "current", "parameters", "status", "refusal", "results"},
    ),
    "out/alerts.json": (
        "alerts",
        {
            "baseline_window",
            "current_window",
            "thresholds",
            "status",
            "alerts",
            "citation_changes",
            "leads",
        },
    ),
    "out/evidence-verify.json": (
        "evidence_verify",
        {"status", "manifests", "latest_manifest", "checked_files", "problems"},
    ),
    "out/disputes.json": (
        "disputes",
        {"annotator_a", "annotator_b", "fields", "items", "unresolved"},
    ),
    "recheck/plan.json": (
        "recheck_plan",
        {"prompt_set", "fraction", "seed", "preregistered", "total_queries", "query_ids", "items"},
    ),
    "recheck/result.json": (
        "recheck_result",
        {
            "plan_hash",
            "fields",
            "planned",
            "checked",
            "agreed",
            "disagreed",
            "not_checked",
            "pass_rate",
            "null_reason",
            "items",
        },
    ),
    "out/report.json": (
        "report",
        {
            "generated_at",
            "notice",
            "verdict",
            "rules",
            "project",
            "preregistration",
            "metrics",
            "crosscheck",
            "compare",
            "alerts",
            "evidence",
            "exclusions",
            "recheck",
            "disputes",
            "limitations",
            "disclosure",
            "signatures",
            "inputs",
        },
    ),
}
ROW_FIELDS: dict[str, tuple[str, set[str]]] = {
    "out/crosscheck.json": (
        "results",
        {
            "platform",
            "a",
            "b",
            "deviation",
            "difference",
            "interval",
            "paired_queries",
            "status",
            "status_label",
            "reason",
        },
    ),
    "out/compare.json": (
        "results",
        {
            "metric_id",
            "platform",
            "baseline",
            "current",
            "difference",
            "interval",
            "paired_queries",
            "sufficient",
            "conclusion",
            "conclusion_code",
            "reason",
        },
    ),
    "out/disputes.json": ("items", {"observation_id", "entity_id", "field", "a", "b", "resolved"}),
    "recheck/plan.json": ("items", {"observation_id", "query_id", "entity_id", "source"}),
    "recheck/result.json": ("items", {"observation_id", "entity_id", "agree", "differences"}),
    "out/report.json": (
        "rules",
        {"rule_id", "title", "status", "status_label", "basis", "details"},
    ),
}


def assert_well_formed(root: Path) -> None:
    for relative, (kind, keys) in DOCUMENTED.items():
        document = read(root / relative)
        assert document["schema_version"] == "1.0", relative
        assert document["kind"] == kind, relative
        assert document["project_id"] == hp.PROJECT_ID, relative
        assert document["synthetic"] is True, relative
        assert keys <= set(document), (relative, keys - set(document))
    for relative, (key, fields) in ROW_FIELDS.items():
        rows = read(root / relative)[key]
        assert rows, relative
        for row in rows:
            assert fields <= set(row), (relative, fields - set(row))
    for result in read(root / "out" / "metrics.json")["results"]:
        assert spec.iter_issues("metric_result", result) == []
    for name, contract in (
        ("entities.jsonl", "entity"),
        ("observations.jsonl", "observation"),
        ("annotations.jsonl", "annotation"),
        ("audit.jsonl", "audit_event"),
    ):
        for line in (root / name).read_text(encoding="utf-8").splitlines():
            assert spec.iter_issues(contract, json.loads(line)) == [], name
    sealed = read(root / "prompts" / "sealed" / "v0001.json")
    assert spec.iter_issues("prompt_set", sealed) == []
    assert spec.iter_issues("manifest", read(root / "evidence" / "manifest-0001.json")) == []
    # report 自己的读取与结构检查也接受上游的真实输出
    inputs = load_inputs(root)
    assert all(inputs.get(name) is not None for name in ("metrics", "crosscheck", "alerts"))


def overall(document: dict[str, Any], metric_id: str) -> dict[str, Any]:
    (result,) = [
        item
        for item in document["results"]
        if item["metric_id"] == metric_id
        and all(item["scope"].get(key) is None for key in ("platform", "channel", "window_id"))
        and item["scope"].get("date") is None
    ]
    return result


def statuses(report: dict[str, Any]) -> dict[str, str]:
    return {item["rule_id"]: item["status"] for item in report["rules"]}


def test_pipeline_end_to_end_passes_and_reproduces_in_another_directory(tmp_path: Path) -> None:
    baseline = baseline_project(tmp_path / "base" / "proj")
    root = tmp_path / "a" / "proj"
    run_pipeline(root, hp.plan_pass, baseline=baseline)
    report = cli(0, "report", str(root), "--now", "2026-10-11T06:00:00Z")

    assert_well_formed(root)
    assert report["verdict"]["code"] == "pass"
    assert report["verdict"]["label"] == "通过"
    assert [item["rule_id"] for item in report["rules"]] == list(RULE_IDS)
    assert statuses(report) == dict.fromkeys(RULE_IDS, "pass")

    mention = overall(read(root / "out" / "metrics.json"), "mention_rate")
    assert (mention["numerator"], mention["denominator"]) == (63, 72)
    assert mention["interval"]["low"] > 0.6
    assert mention["sufficiency"]["sufficient"] is True

    crosscheck = read(root / "out" / "crosscheck.json")
    assert crosscheck["status"] == "pass"
    assert crosscheck["preregistered"] is True
    merged = next(row for row in crosscheck["results"] if row["platform"] is None)
    assert (merged["a"]["numerator"], merged["a"]["denominator"]) == (33, 36)
    assert (merged["b"]["numerator"], merged["b"]["denominator"]) == (30, 36)
    assert abs(merged["deviation"] - 2 / 21) < 1e-12
    assert abs(merged["difference"] - 3 / 36) < 1e-12

    alerts = read(root / "out" / "alerts.json")
    assert (alerts["status"], alerts["baseline_window"], alerts["current_window"]) == (
        "clear",
        "W2",
        "W3",
    )
    assert alerts["citation_changes"]["added"] == []

    disputes = read(root / "out" / "disputes.json")
    kappas = {item["field"]: item["kappa"] for item in disputes["fields"]}
    assert kappas == {"mentioned": 1.0, "recommended": 0.75}
    assert disputes["unresolved"] == 0
    assert [item["resolved"] for item in disputes["items"]] == [True]

    plan = read(root / "recheck" / "plan.json")
    assert plan["query_ids"] == reference_sample(hp.QUERIES, 1, 5, 20261001)
    assert len(plan["items"]) == 96
    result = read(root / "recheck" / "result.json")
    assert (result["planned"], result["checked"], result["agreed"], result["pass_rate"]) == (
        96,
        96,
        96,
        1.0,
    )

    compare = read(root / "out" / "compare.json")
    assert compare["status"] == "ok"
    row = next(item for item in compare["results"] if item["platform"] is None)
    assert (row["baseline"]["numerator"], row["current"]["numerator"]) == (36, 63)
    assert abs(row["difference"] - 0.375) < 1e-12
    assert row["conclusion"] == "提升"
    compare_rule = next(item for item in report["rules"] if item["rule_id"] == "R-COMPARE")
    assert compare_rule["basis"] == "基线对比已完成（1 个指标）：品牌提及率（mention_rate）整体提升"

    evidence = read(root / "out" / "evidence-verify.json")
    assert (evidence["status"], evidence["problems"]) == ("pass", [])

    # 同一证据包复制到另一个目录后独立复算：校验通过，指标与报告逐字节一致
    other = tmp_path / "b" / "proj"
    shutil.copytree(root, other)
    project = str(other)
    cli(0, "evidence", "verify", project)
    cli(0, "metrics", project)
    cli(0, "crosscheck", project)
    cli(0, "alerts", project)
    cli(0, "annotate", "kappa", project, "--a", "标注员甲", "--b", "标注员乙")
    cli(0, "report", project, "--now", "2026-10-11T06:00:00Z")
    for relative in (
        "out/metrics.json",
        "out/crosscheck.json",
        "out/alerts.json",
        "out/disputes.json",
        "out/evidence-verify.json",
        "out/report.json",
        "out/report.html",
        "out/observations.csv",
    ):
        assert (root / relative).read_bytes() == (other / relative).read_bytes(), relative


def test_pipeline_fails_when_crosscheck_deviation_exceeds_threshold(tmp_path: Path) -> None:
    # api 36/36 = 1，manual 12/36 = 1/3：偏离度 = (2/3) / (2/3) = 1.0 > 0.3（样本充分）
    root = tmp_path / "proj"
    run_pipeline(root, hp.plan_crosscheck_fail, crosscheck_exit=1)
    report = cli(1, "report", str(root), "--now", "2026-10-11T06:00:00Z")

    crosscheck = read(root / "out" / "crosscheck.json")
    assert crosscheck["status"] == "fail"
    merged = next(row for row in crosscheck["results"] if row["platform"] is None)
    assert abs(merged["deviation"] - 1.0) < 1e-12
    assert merged["a"]["sufficient"] is True
    assert merged["b"]["sufficient"] is True
    assert report["verdict"]["code"] == "fail"
    assert report["verdict"]["label"] == "不通过"
    assert report["verdict"]["decided_by"] == ["R-CROSSCHECK"]
    # 合并提及率 48/72 = 2/3 ≥ 0.6，但 6 道题里只有 2 道全提及、4 道一半提及：
    # 按题重抽 6 道全落在“一半”的概率 (2/3)^6 ≈ 0.088 > 0.025，区间下界为 0.5，跨过阈值 → 有保留
    expected = dict.fromkeys(RULE_IDS, "pass")
    expected.update({"R-CROSSCHECK": "fail", "R-THRESHOLD": "warning", "R-COMPARE": "not_run"})
    assert statuses(report) == expected
    assert [item["rule_id"] for item in report["verdict"]["reservations"]] == ["R-THRESHOLD"]


def test_pipeline_fails_when_evidence_is_tampered_after_the_manifest(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    run_pipeline(root, hp.plan_pass)
    lines = (root / "observations.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[4])
    record["answer_text"] = record["answer_text"].replace("续航长", "续航短")
    lines[4] = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    (root / "observations.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    verify = cli(1, "evidence", "verify", str(root))
    report = cli(1, "report", str(root), "--now", "2026-10-11T06:00:00Z")

    problems = {(item["file"], item["kind"], item["line"]) for item in verify["problems"]}
    assert problems == {
        ("observations.jsonl", "file_modified", None),
        ("observations.jsonl", "line_modified", 5),
    }
    assert report["verdict"]["code"] == "fail"
    assert report["verdict"]["decided_by"] == ["R-EVIDENCE"]
    assert statuses(report)["R-EVIDENCE"] == "fail"
