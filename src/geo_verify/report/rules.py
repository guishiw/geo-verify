"""预检结论的判定规则。

结论只有四种，按下列顺序判定（规则编号固定，联调场景按编号断言）：
1. 完整性硬伤（group = integrity）任一 fail → 不通过；
2. 否则任一规则为 insufficient（group = sufficiency）→ 数据不足；
3. 否则未达标（group = target）任一 fail → 不通过；
4. 否则有保留：任一 warning，或证据校验 / 交叉验证 / 抽样复核未执行 → 有条件通过，逐条列出保留意见；
5. 否则通过。
只有预先登记为 acceptance 且带阈值的指标参与结论；事后指标只展示。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from geo_verify import project as project_module
from geo_verify.report.inputs import RESULT_FILES, Inputs
from geo_verify.report.labels import REGISTERED_GROUPS, STATUS_LABELS, VERDICT_LABELS, metric_title

INTEGRITY = "integrity"
SUFFICIENCY = "sufficiency"
TARGET = "target"
RESERVATION = "reservation"
RESERVE_IF_NOT_RUN = frozenset({"R-EVIDENCE", "R-CROSSCHECK", "R-RECHECK"})
DEDICATED_NOTICES = frozenset(
    {
        "preregistration_changed",
        "unregistered_exclusion_rules",
        "selective_exclusion",
        "model_version_changed",
        "prompt_set_version_mismatch",
    }
)
DAILY_METRICS = frozenset({"recommendation_rate_daily"})


@dataclass
class Rule:
    rule_id: str
    title: str
    group: str
    status: str
    basis: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def reserves(self) -> bool:
        return self.status == "warning" or (
            self.status == "not_run" and self.rule_id in RESERVE_IF_NOT_RUN
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "group": self.group,
            "status": self.status,
            "status_label": STATUS_LABELS[self.status],
            "basis": self.basis,
            "details": self.details,
        }


def _path(name: str) -> str:
    return RESULT_FILES[name][0]


def _missing(name: str, what: str) -> str:
    return f"缺少 {_path(name)}，{what}未执行"


def _notices(inputs: Inputs, code: str | None = None) -> list[dict[str, Any]]:
    metrics = inputs.get("metrics") or {}
    notices: list[dict[str, Any]] = metrics.get("notices") or []
    return [item for item in notices if code is None or item["code"] == code]


def _scope_text(scope: dict[str, Any]) -> str:
    parts = [
        f"{key} = {scope[key]}"
        for key in ("platform", "channel", "window_id", "date")
        if scope.get(key)
    ]
    return "整体" if not parts else "、".join(parts)


# ------------------------------------------------------------------ 验收指标


@dataclass(frozen=True)
class Target:
    registered_id: str
    metric_id: str
    threshold: dict[str, Any]


def _expand(registered_id: str, inputs: Inputs) -> list[str]:
    metrics = inputs.get("metrics") or {}
    for entry in metrics.get("registered_metrics") or []:
        ids = entry.get("metric_ids")
        if entry.get("registered_id") == registered_id and isinstance(ids, list) and ids:
            return [str(item) for item in ids]
    if registered_id == "accuracy":
        return [f"accuracy_{inputs.project['preregistration']['accuracy_basis']}"]
    return list(REGISTERED_GROUPS.get(registered_id, (registered_id,)))


def acceptance_targets(inputs: Inputs) -> list[Target]:
    """预先登记为验收指标且带阈值的指标（组名按登记口径展开，准确率取 accuracy_basis）。"""
    targets: list[Target] = []
    for item in inputs.project["preregistration"]["metrics"]:
        if item["role"] != "acceptance" or item["threshold"] is None:
            continue
        for metric_id in _expand(item["metric_id"], inputs):
            targets.append(Target(item["metric_id"], metric_id, item["threshold"]))
    return targets


def is_overall(result: dict[str, Any]) -> bool:
    scope = result["scope"]
    keys = (
        ("platform", "channel", "window_id")
        if result["metric_id"] in DAILY_METRICS
        else (
            "platform",
            "channel",
            "window_id",
            "date",
        )
    )
    if any(scope[key] is not None for key in keys):
        return False
    return result["metric_id"] not in DAILY_METRICS or scope["date"] is not None


def acceptance_results(inputs: Inputs, target: Target) -> list[dict[str, Any]]:
    """某个验收指标的整体范围结果（按日指标为每天一个结果，按日期排序）。"""
    metrics = inputs.get("metrics") or {}
    target_entity = inputs.project["target_entity_id"]
    found = [
        result
        for result in metrics.get("results", [])
        if result["metric_id"] == target.metric_id
        and result["role"] == "acceptance"
        and result["preregistered"] is True
        and result["scope"]["entity_id"] in (None, target_entity)
        and is_overall(result)
    ]
    return sorted(found, key=lambda item: item["scope"]["date"] or "")


def meets(value: float, threshold: dict[str, Any]) -> bool:
    limit = float(threshold["value"])
    op = threshold["op"]
    if op == ">=":
        return value >= limit
    if op == ">":
        return value > limit
    if op == "<=":
        return value <= limit
    return value < limit


def interval_crosses(interval: dict[str, Any], threshold: dict[str, Any]) -> bool:
    """区间里是否有不满足阈值的值：越高越好看下端，越低越好看上端。"""
    bound = interval["low"] if threshold["op"] in (">=", ">") else interval["high"]
    return not meets(bound, threshold)


def threshold_text(threshold: dict[str, Any] | None) -> str:
    if threshold is None:
        return "无阈值"
    return f"{threshold['op']} {threshold['value']:g}"


# ------------------------------------------------------------------ 各条规则


def _rule_evidence(inputs: Inputs) -> Rule:
    doc = inputs.get("evidence")
    title = "证据校验"
    if doc is None:
        return Rule("R-EVIDENCE", title, INTEGRITY, "not_run", _missing("evidence", "证据校验"))
    problems = doc["problems"]
    details = {
        "status": doc["status"],
        "checked_files": doc.get("checked_files"),
        "problems": problems,
    }
    if doc["status"] == "fail":
        basis = f"{_path('evidence')}：status = fail，{len(problems)} 处问题（证据清单或原始记录被改动）"
        return Rule("R-EVIDENCE", title, INTEGRITY, "fail", basis, details)
    basis = f"{_path('evidence')}：status = pass，已校验 {doc.get('checked_files')} 个文件"
    return Rule("R-EVIDENCE", title, INTEGRITY, "pass", basis, details)


def _audit(inputs: Inputs) -> dict[str, Any] | None:
    metrics = inputs.get("metrics")
    if metrics is None:
        return None
    audit: dict[str, Any] | None = metrics.get("exclusion_audit")
    return audit


def _rule_unregistered(inputs: Inputs) -> Rule:
    title = "剔除规则均已预先登记"
    audit = _audit(inputs)
    if audit is None:
        basis = (
            _missing("metrics", "剔除规则检查")
            if inputs.get("metrics") is None
            else "metrics.json 没有 exclusion_audit，剔除审计未执行"
        )
        return Rule("R-EXCLUSION-UNREGISTERED", title, INTEGRITY, "not_run", basis)
    unregistered = list(audit["unregistered_rules"])
    count = sum(
        item["excluded_observations"] for item in audit["rules"] if item["rule_id"] in unregistered
    )
    details = {
        "unregistered_rules": unregistered,
        "excluded_observations": count,
        "rules": audit["rules"],
    }
    if unregistered:
        basis = f"使用了未预先登记的剔除规则 {'、'.join(unregistered)}，剔除 {count} 条观测"
        return Rule("R-EXCLUSION-UNREGISTERED", title, INTEGRITY, "fail", basis, details)
    basis = f"剔除共 {audit['excluded_observations']} 条，所用规则均已预先登记"
    return Rule("R-EXCLUSION-UNREGISTERED", title, INTEGRITY, "pass", basis, details)


def _rule_selective(inputs: Inputs) -> Rule:
    title = "无选择性剔除"
    audit = _audit(inputs)
    if audit is None:
        basis = (
            _missing("metrics", "选择性剔除检查")
            if inputs.get("metrics") is None
            else "metrics.json 没有 exclusion_audit，剔除审计未执行"
        )
        return Rule("R-EXCLUSION-SELECTIVE", title, INTEGRITY, "not_run", basis)
    details = {
        "excluded_observations": audit["excluded_observations"],
        "rules": [
            {"rule_id": item["rule_id"], "excluded_observations": item["excluded_observations"]}
            for item in audit["rules"]
        ],
        "comparisons": audit["comparisons"],
    }
    if audit["selective"]:
        metrics = [item.get("metric_id") for item in audit["comparisons"] if item.get("selective")]
        basis = (
            f"被剔除的 {audit['excluded_observations']} 条观测明显偏向不利样本"
            f"（指标：{'、'.join(str(item) for item in metrics) or '见明细'}），保留与被剔除样本的对比见明细"
        )
        return Rule("R-EXCLUSION-SELECTIVE", title, INTEGRITY, "fail", basis, details)
    basis = f"剔除共 {audit['excluded_observations']} 条，未判定为选择性剔除"
    return Rule("R-EXCLUSION-SELECTIVE", title, INTEGRITY, "pass", basis, details)


def _rule_prereg(inputs: Inputs) -> Rule:
    title = "预先登记内容在封存后未改动"
    reported = bool(_notices(inputs, "preregistration_changed"))
    prompt_set = inputs.prompt_set
    details: dict[str, Any] = {
        "reported_by_metrics": reported,
        "sealed_hash": None,
        "current_hash": None,
    }
    if prompt_set is None and inputs.get("metrics") is None:
        return Rule(
            "R-PREREG-CHANGED",
            title,
            INTEGRITY,
            "not_run",
            "题库尚未封存，也没有指标结果，无法核对预先登记内容",
            details,
        )
    changed = reported
    if prompt_set is not None:
        current = project_module.preregistration_hash(inputs.project)
        details.update(sealed_hash=prompt_set["preregistration_hash"], current_hash=current)
        changed = changed or current != prompt_set["preregistration_hash"]
    if changed:
        basis = "预先登记内容在题库封存后被改动：当前口径与封存时登记的口径不一致"
        return Rule("R-PREREG-CHANGED", title, INTEGRITY, "fail", basis, details)
    return Rule(
        "R-PREREG-CHANGED", title, INTEGRITY, "pass", "当前预先登记内容与封存时一致", details
    )


def _rule_metrics(inputs: Inputs) -> Rule:
    title = "指标结果"
    metrics = inputs.get("metrics")
    if metrics is None:
        return Rule(
            "R-METRICS",
            title,
            SUFFICIENCY,
            "insufficient",
            f"缺少 {_path('metrics')}：没有可判定的指标",
        )
    count = len(metrics["results"])
    details = {"results": count, "observations": metrics.get("observations")}
    return Rule(
        "R-METRICS", title, SUFFICIENCY, "pass", f"{_path('metrics')}：{count} 个指标结果", details
    )


@dataclass
class Acceptance:
    targets: list[Target]
    found: list[tuple[Target, dict[str, Any]]]
    missing: list[str]


def _acceptance(inputs: Inputs) -> Acceptance:
    targets = acceptance_targets(inputs)
    found: list[tuple[Target, dict[str, Any]]] = []
    missing: list[str] = []
    for target in targets:
        results = acceptance_results(inputs, target)
        if not results:
            missing.append(target.metric_id)
        found.extend((target, result) for result in results)
    return Acceptance(targets, found, missing)


def _rule_defined(inputs: Inputs, acceptance: Acceptance) -> Rule:
    title = "有预先登记的验收指标及其结果"
    if inputs.get("metrics") is None:
        return Rule(
            "R-ACCEPTANCE-DEFINED",
            title,
            SUFFICIENCY,
            "not_run",
            _missing("metrics", "验收指标检查"),
        )
    details = {
        "registered": [
            {
                "registered_id": item.registered_id,
                "metric_id": item.metric_id,
                "threshold": item.threshold,
            }
            for item in acceptance.targets
        ],
        "missing": acceptance.missing,
    }
    if not acceptance.targets:
        basis = "没有预先登记为验收指标且带阈值的指标，不能给出验收结论"
        return Rule("R-ACCEPTANCE-DEFINED", title, SUFFICIENCY, "insufficient", basis, details)
    if acceptance.missing:
        basis = (
            f"验收指标 {'、'.join(acceptance.missing)} 在 {_path('metrics')} 中没有整体范围的验收结果"
            "（结果缺失，或已被降为事后指标）"
        )
        return Rule("R-ACCEPTANCE-DEFINED", title, SUFFICIENCY, "insufficient", basis, details)
    names = "、".join(
        dict.fromkeys(
            f"{item.metric_id}（{threshold_text(item.threshold)}）" for item in acceptance.targets
        )
    )
    return Rule("R-ACCEPTANCE-DEFINED", title, SUFFICIENCY, "pass", f"验收指标：{names}", details)


def _rule_sufficiency(inputs: Inputs, acceptance: Acceptance) -> Rule:
    title = "验收指标样本充分"
    if not acceptance.found:
        basis = (
            _missing("metrics", "样本充分性检查")
            if inputs.get("metrics") is None
            else "没有可评估的验收指标结果"
        )
        return Rule("R-SUFFICIENCY", title, SUFFICIENCY, "not_run", basis)
    entries = []
    short = []
    for target, result in acceptance.found:
        sufficiency = result["sufficiency"]
        ok = sufficiency["sufficient"] and result["value"] is not None
        entries.append(
            {
                "metric_id": target.metric_id,
                "scope": result["scope"],
                "value": result["value"],
                "null_reason": result["null_reason"],
                "sufficient": sufficiency["sufficient"],
                "windows": sufficiency["windows"],
                "valid_observations": sufficiency["valid_observations"],
                "reasons": sufficiency["reasons"],
            }
        )
        if not ok:
            why = (
                result["null_reason"]
                if result["value"] is None
                else "；".join(sufficiency["reasons"]) or "样本不充分"
            )
            short.append(f"{target.metric_id}（{_scope_text(result['scope'])}）：{why}")
    details = {"metrics": entries}
    if short:
        basis = "样本不充分或无法计算，不能给出肯定结论：" + "；".join(short)
        return Rule("R-SUFFICIENCY", title, SUFFICIENCY, "insufficient", basis, details)
    return Rule("R-SUFFICIENCY", title, SUFFICIENCY, "pass", "全部验收指标样本充分", details)


def _rule_model_version(inputs: Inputs) -> Rule:
    title = "观测期内模型与产品版本未变化"
    if inputs.get("metrics") is None:
        return Rule(
            "R-MODEL-VERSION", title, SUFFICIENCY, "not_run", _missing("metrics", "版本检查")
        )
    changed = _notices(inputs, "model_version_changed")
    details = {"notices": changed}
    if changed:
        basis = "观测期内模型或产品版本发生变化，前后数据不得合并比较，须重建基线：" + "；".join(
            item.get("message", "") for item in changed
        )
        return Rule("R-MODEL-VERSION", title, SUFFICIENCY, "insufficient", basis, details)
    return Rule("R-MODEL-VERSION", title, SUFFICIENCY, "pass", "未发现模型或产品版本变化", details)


def _rule_threshold(inputs: Inputs, acceptance: Acceptance) -> Rule:
    title = "验收指标达到登记阈值"
    evaluable = [
        (target, result) for target, result in acceptance.found if result["value"] is not None
    ]
    if not evaluable:
        basis = (
            _missing("metrics", "阈值判定")
            if inputs.get("metrics") is None
            else "没有可判定的验收指标值"
        )
        return Rule("R-THRESHOLD", title, TARGET, "not_run", basis)
    entries = []
    failed, uncertain = [], []
    for target, result in evaluable:
        met = meets(result["value"], target.threshold)
        interval = result["interval"]
        crosses = None if interval is None else interval_crosses(interval, target.threshold)
        entries.append(
            {
                "metric_id": target.metric_id,
                "scope": result["scope"],
                "value": result["value"],
                "threshold": target.threshold,
                "met": met,
                "interval": interval,
                "interval_crosses_threshold": crosses,
            }
        )
        label = f"{target.metric_id}（{_scope_text(result['scope'])}）= {result['value']:.4f}，阈值 {threshold_text(target.threshold)}"
        if not met:
            failed.append(label)
        elif interval is None:
            uncertain.append(f"{label}，没有区间，无法评估不确定性")
        elif crosses:
            uncertain.append(
                f"{label}，但区间 [{interval['low']:.4f}, {interval['high']:.4f}] 跨过阈值"
            )
    details = {"metrics": entries}
    if failed:
        return Rule(
            "R-THRESHOLD",
            title,
            TARGET,
            "fail",
            "点估计未达到登记阈值：" + "；".join(failed),
            details,
        )
    if uncertain:
        return Rule(
            "R-THRESHOLD",
            title,
            TARGET,
            "warning",
            "点估计达标但存在不确定性：" + "；".join(uncertain),
            details,
        )
    return Rule(
        "R-THRESHOLD", title, TARGET, "pass", "全部验收指标的点估计与区间均满足登记阈值", details
    )


def _rule_crosscheck(inputs: Inputs) -> Rule:
    title = "双通道交叉验证"
    doc = inputs.get("crosscheck")
    if doc is None:
        return Rule("R-CROSSCHECK", title, TARGET, "not_run", _missing("crosscheck", "交叉验证"))
    details = {
        "status": doc["status"],
        "threshold": doc["threshold"],
        "preregistered": doc["preregistered"],
        "rows": [
            {
                "platform": row.get("platform"),
                "deviation": row.get("deviation"),
                "status": row.get("status"),
            }
            for row in doc["results"]
        ],
    }
    reason = doc.get("reason") or ""
    if doc["status"] == "fail":
        return Rule(
            "R-CROSSCHECK",
            title,
            TARGET,
            "fail",
            f"偏离度超过阈值 {doc['threshold']:g}：{reason}",
            details,
        )
    if doc["status"] == "undetermined":
        return Rule(
            "R-CROSSCHECK",
            title,
            TARGET,
            "warning",
            f"交叉验证不可判定（样本不足）：{reason}",
            details,
        )
    if not doc["preregistered"]:
        basis = "交叉验证通过，但公式、阈值、指标或通道未全部取自预先登记内容"
        return Rule("R-CROSSCHECK", title, TARGET, "warning", basis, details)
    return Rule(
        "R-CROSSCHECK", title, TARGET, "pass", f"偏离度不超过阈值 {doc['threshold']:g}", details
    )


def _rule_recheck(inputs: Inputs) -> Rule:
    title = "抽样复核"
    plan, result = inputs.get("plan"), inputs.get("result")
    if plan is None and result is None:
        return Rule(
            "R-RECHECK",
            title,
            RESERVATION,
            "not_run",
            "缺少 recheck/plan.json 与 recheck/result.json，抽样复核未执行",
        )
    problems: list[str] = []
    plan_hash = inputs.hashes.get(_path("plan"))
    if plan is None:
        problems.append("复核结果没有对应的复核计划（缺少 recheck/plan.json）")
    elif not plan["preregistered"]:
        problems.append("复核计划未采用预先登记的抽样参数")
    if result is None:
        problems.append("已生成复核计划，尚未比对复核结果（缺少 recheck/result.json）")
    else:
        if plan is not None and result["plan_hash"] != plan_hash:
            problems.append("复核结果所依据的计划与当前 recheck/plan.json 不一致")
        if result["pass_rate"] is None:
            problems.append("没有已复核的项，一致率不可判定")
        if result["not_checked"]:
            problems.append(f"计划内还有 {result['not_checked']} 项没有复核")
        if result.get("errors"):
            problems.append(f"复核文件有 {len(result['errors'])} 处错误未计入")
    details = {
        "problems": problems,
        "pass_rate": None if result is None else result["pass_rate"],
        "checked": None if result is None else result["checked"],
        "planned": None if result is None else result["planned"],
    }
    if problems:
        return Rule("R-RECHECK", title, RESERVATION, "warning", "；".join(problems), details)
    basis = (
        f"已复核 {result['checked']} 项（计划 {result['planned']} 项），一致率 {result['pass_rate']:.4f}"
        if result
        else ""
    )
    return Rule("R-RECHECK", title, RESERVATION, "pass", basis, details)


def _rule_disputes(inputs: Inputs) -> Rule:
    title = "双人标注争议已解决"
    doc = inputs.get("disputes")
    if doc is None:
        return Rule(
            "R-DISPUTES", title, RESERVATION, "not_run", _missing("disputes", "双人标注一致性检查")
        )
    details = {"unresolved": doc["unresolved"], "items": len(doc["items"])}
    if doc["unresolved"]:
        return Rule(
            "R-DISPUTES",
            title,
            RESERVATION,
            "warning",
            f"有 {doc['unresolved']} 个双人标注争议项尚未解决",
            details,
        )
    return Rule("R-DISPUTES", title, RESERVATION, "pass", "没有未解决的双人标注争议项", details)


def _rule_alerts(inputs: Inputs) -> Rule:
    title = "无异常告警"
    doc = inputs.get("alerts")
    if doc is None:
        return Rule("R-ALERTS", title, RESERVATION, "not_run", _missing("alerts", "异常识别"))
    alerts = [
        {key: item.get(key) for key in ("rule_id", "severity", "platform", "entity_id", "message")}
        for item in doc["alerts"]
    ]
    details = {"alerts": alerts}
    if doc["status"] == "alert":
        basis = f"有 {len(alerts)} 条告警：" + "；".join(item["message"] for item in alerts)
        return Rule("R-ALERTS", title, RESERVATION, "warning", basis, details)
    return Rule("R-ALERTS", title, RESERVATION, "pass", "没有告警", details)


def _rule_compare(inputs: Inputs) -> Rule:
    title = "基线对比可进行"
    doc = inputs.get("compare")
    if doc is None:
        return Rule("R-COMPARE", title, RESERVATION, "not_run", _missing("compare", "基线对比"))
    details = {"status": doc["status"], "refusal": doc["refusal"]}
    if doc["status"] == "refused":
        message = (doc["refusal"] or {}).get("message") or "见明细"
        return Rule(
            "R-COMPARE", title, RESERVATION, "warning", f"基线对比被拒绝：{message}", details
        )
    if doc["status"] == "insufficient":
        return Rule(
            "R-COMPARE",
            title,
            RESERVATION,
            "warning",
            "基线对比样本不足，不能判定提升或下降",
            details,
        )
    return Rule(
        "R-COMPARE",
        title,
        RESERVATION,
        "pass",
        f"基线对比已完成（{len(doc['results'])} 项）",
        details,
    )


def _rule_prompt_version(inputs: Inputs) -> Rule:
    title = "观测均属于所选题库版本"
    if inputs.get("metrics") is None:
        return Rule(
            "R-PROMPT-VERSION", title, RESERVATION, "not_run", _missing("metrics", "题库版本检查")
        )
    found = _notices(inputs, "prompt_set_version_mismatch")
    details = {"notices": found}
    if found:
        basis = "存在不属于所选封存题库版本的观测，已排除在指标之外：" + "；".join(
            item.get("message", "") for item in found
        )
        return Rule("R-PROMPT-VERSION", title, RESERVATION, "warning", basis, details)
    return Rule(
        "R-PROMPT-VERSION", title, RESERVATION, "pass", "全部观测属于所选封存题库版本", details
    )


def _rule_data_notices(inputs: Inputs) -> Rule:
    title = "指标计算无其他警示"
    if inputs.get("metrics") is None:
        return Rule(
            "R-DATA-NOTICES", title, RESERVATION, "not_run", _missing("metrics", "指标提示检查")
        )
    found = [
        item
        for item in _notices(inputs)
        if item["code"] not in DEDICATED_NOTICES and item.get("severity") in ("warning", "error")
    ]
    details = {
        "notices": [
            {"code": item["code"], "severity": item.get("severity"), "message": item.get("message")}
            for item in found
        ]
    }
    if found:
        basis = "指标计算有需要注意的提示：" + "；".join(
            f"{item['code']}：{item.get('message', '')}" for item in found
        )
        return Rule("R-DATA-NOTICES", title, RESERVATION, "warning", basis, details)
    return Rule("R-DATA-NOTICES", title, RESERVATION, "pass", "指标计算没有其他警示", details)


def _prompt_refs(inputs: Inputs) -> Iterable[tuple[str, Any]]:
    """各结果文件所依据的题库（compare 取当前项目一侧）。"""
    for name in ("metrics", "crosscheck", "plan", "compare"):
        doc = inputs.get(name)
        if doc is None:
            continue
        holder = (doc.get("current") or {}) if name == "compare" else doc
        prompt_set = holder.get("prompt_set")
        if isinstance(prompt_set, dict):
            yield _path(name), prompt_set["content_hash"]


def _rule_consistency(inputs: Inputs) -> Rule:
    title = "结果文件依据同一题库版本"
    refs = list(_prompt_refs(inputs))
    expected = (
        inputs.prompt_set["content_hash"] if inputs.prompt_set else (refs[0][1] if refs else None)
    )
    files = [
        {"path": path, "content_hash": value, "matches": value == expected} for path, value in refs
    ]
    details = {"expected": expected, "files": files}
    mismatched = [item["path"] for item in files if not item["matches"]]
    if mismatched:
        basis = f"{'、'.join(mismatched)} 所依据的题库版本与报告所用的封存题库不一致"
        return Rule("R-INPUT-CONSISTENCY", title, RESERVATION, "warning", basis, details)
    return Rule(
        "R-INPUT-CONSISTENCY", title, RESERVATION, "pass", "各结果文件所依据的题库版本一致", details
    )


def evaluate(inputs: Inputs) -> tuple[list[Rule], Acceptance]:
    acceptance = (
        _acceptance(inputs)
        if inputs.get("metrics") is not None
        else Acceptance(acceptance_targets(inputs), [], [])
    )
    rules = [
        _rule_evidence(inputs),
        _rule_unregistered(inputs),
        _rule_selective(inputs),
        _rule_prereg(inputs),
        _rule_metrics(inputs),
        _rule_defined(inputs, acceptance),
        _rule_sufficiency(inputs, acceptance),
        _rule_model_version(inputs),
        _rule_threshold(inputs, acceptance),
        _rule_crosscheck(inputs),
        _rule_recheck(inputs),
        _rule_disputes(inputs),
        _rule_alerts(inputs),
        _rule_compare(inputs),
        _rule_prompt_version(inputs),
        _rule_data_notices(inputs),
        _rule_consistency(inputs),
    ]
    return rules, acceptance


def verdict(rules: list[Rule]) -> dict[str, Any]:
    integrity = [item for item in rules if item.group == INTEGRITY and item.status == "fail"]
    insufficient = [item for item in rules if item.status == "insufficient"]
    target = [item for item in rules if item.group == TARGET and item.status == "fail"]
    reserved = [item for item in rules if item.reserves]
    if integrity:
        code, decided = "fail", integrity
    elif insufficient:
        code, decided = "insufficient_data", insufficient
    elif target:
        code, decided = "fail", target
    elif reserved:
        code, decided = "conditional_pass", reserved
    else:
        code, decided = "pass", []
    label = VERDICT_LABELS[code]
    if decided:
        summary = f"{label}：" + "；".join(
            f"{item.rule_id}（{item.title}）{item.basis}" for item in decided
        )
    else:
        summary = f"{label}：全部判定规则通过。"
    return {
        "code": code,
        "label": label,
        "decided_by": [item.rule_id for item in decided],
        "reservations": [
            {"rule_id": item.rule_id, "text": f"{item.title}：{item.basis}"} for item in reserved
        ],
        "summary": summary,
    }


def registered_listing(inputs: Inputs) -> list[dict[str, Any]]:
    """预先登记的指标清单（报告展示用）。"""
    labels = {"acceptance": "验收指标", "descriptive": "描述性指标"}
    return [
        {
            "metric_id": item["metric_id"],
            "title": metric_title(item["metric_id"]),
            "definition_version": item["definition_version"],
            "role": item["role"],
            "role_label": labels.get(item["role"], item["role"]),
            "threshold": item["threshold"],
        }
        for item in inputs.project["preregistration"]["metrics"]
    ]
