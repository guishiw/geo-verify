"""组装 out/report.json：结论、判定规则、项目与登记口径、各结果摘要、限制、披露栏、签字栏、输入哈希。

只读取输入，不重新计算指标；同一份输入在任何目录生成的报告逐字节相同（不含绝对路径；
生成时间只取自 --now，未传时为 null）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from geo_verify.errors import InputError
from geo_verify.report.inputs import Inputs
from geo_verify.report.labels import (
    ALERT_LABELS,
    COMPARE_LABELS,
    CROSSCHECK_LABELS,
    DEFAULT_FORMULA_NOTE,
    DEFAULT_FORMULA_TEXT,
    EVIDENCE_LABELS,
    FORMULA_DECLARATION,
    METHOD_LABELS,
    NOTICE,
    POST_HOC_LABEL,
    ROLE_LABELS,
    USE_LABELS,
    WATERMARK,
    channel_entry,
    channel_label,
    metric_title,
)
from geo_verify.report.observations import COLUMNS, observation_rows
from geo_verify.report.rules import (
    Acceptance,
    Rule,
    evaluate,
    is_overall,
    meets,
    registered_listing,
    verdict,
)
from geo_verify.timeutil import local_date

SCHEMA_VERSION = "1.0"
PREVIEW_ROWS = 20
NOT_RUN: dict[str, Any] = {"status": "not_run"}


def _not_run() -> dict[str, Any]:
    return dict(NOT_RUN)


# ------------------------------------------------------------------ 项目


def _entity(inputs: Inputs, entity_id: str) -> dict[str, Any]:
    return {"entity_id": entity_id, "name": inputs.entities.get(entity_id, {}).get("name")}


def _period(inputs: Inputs) -> dict[str, Any]:
    unique: dict[str, dict[str, Any]] = {}
    for item in inputs.observations:
        unique.setdefault(str(item.get("observation_id")), item)
    observations = list(unique.values())
    selected = inputs.prompt_set["content_hash"] if inputs.prompt_set else None
    dates = []
    for item in observations:
        try:
            dates.append(
                local_date(item.get("started_at", ""), inputs.project["timezone"]).isoformat()
            )
        except InputError:
            continue
    channels = Counter(str(item.get("channel")) for item in observations)
    return {
        "date_from": min(dates) if dates else None,
        "date_to": max(dates) if dates else None,
        "windows": sorted({str(item.get("window_id")) for item in observations}),
        "observations": {
            "total": len(observations),
            "valid": sum(1 for item in observations if item.get("status") == "valid"),
            "in_selected_prompt_set": sum(
                1 for item in observations if item.get("prompt_set_hash") == selected
            ),
            "excluded": sum(1 for key in unique if key in inputs.excluded),
        },
        "channels": dict(sorted(channels.items())),
    }


def _project(inputs: Inputs) -> dict[str, Any]:
    project = inputs.project
    prompt_set = inputs.prompt_set
    return {
        "project_id": project["project_id"],
        "name": project["name"],
        "industry": project.get("industry"),
        "client": project["client"],
        "provider": project["provider"],
        "timezone": project["timezone"],
        "platforms": [
            {
                "platform_id": item["platform_id"],
                "display_name": item.get("display_name"),
                "channels": [channel_entry(channel) for channel in item["channels"]],
            }
            for item in project["platforms"]
        ],
        "target": _entity(inputs, project["target_entity_id"]),
        "competitors": [_entity(inputs, item) for item in project["competitor_entity_ids"]],
        "environment": project["environment"],
        "prompt_set": None
        if prompt_set is None
        else {
            "version": prompt_set["version"],
            "content_hash": prompt_set["content_hash"],
            "total_queries": len(prompt_set["prompts"]),
            "sealed_at": prompt_set["sealed_at"],
            "sealed_by": prompt_set["sealed_by"],
        },
        "period": _period(inputs),
    }


def _preregistration(inputs: Inputs, rules: list[Rule]) -> dict[str, Any]:
    prereg = inputs.project["preregistration"]
    crosscheck = inputs.get("crosscheck") or {}
    changed = next(item for item in rules if item.rule_id == "R-PREREG-CHANGED")
    return {
        "registered_at": prereg["registered_at"],
        "registered_by": prereg["registered_by"],
        "changed_after_seal": None if changed.status == "not_run" else changed.status == "fail",
        "metrics": registered_listing(inputs),
        "accuracy_basis": prereg["accuracy_basis"],
        "recommendation_success": prereg["recommendation_success"],
        "crosscheck": {
            **prereg["crosscheck"],
            "formula_text": crosscheck.get("formula_text", DEFAULT_FORMULA_TEXT),
            "formula_note": crosscheck.get("formula_note", DEFAULT_FORMULA_NOTE),
            "formula_declaration": FORMULA_DECLARATION,
        },
        "sufficiency": prereg["sufficiency"],
        "statistics": prereg["statistics"],
        "recheck": prereg["recheck"],
        "target_sources": prereg["target_sources"],
        "authority": prereg["authority"],
        "exclusion_rules": prereg["exclusion_rules"],
    }


# ------------------------------------------------------------------ 指标


def _scope_label(scope: dict[str, Any]) -> str:
    parts = []
    if scope.get("platform"):
        parts.append(f"平台 {scope['platform']}")
    if scope.get("channel"):
        parts.append(f"通道 {scope['channel']}（{channel_label(scope['channel'])}）")
    if scope.get("window_id"):
        parts.append(f"窗口 {scope['window_id']}")
    if scope.get("date"):
        parts.append(f"日期 {scope['date']}")
    return "、".join(parts) or "整体"


def _coverage(result: dict[str, Any]) -> dict[str, Any] | None:
    ext = result.get("ext")
    coverage = ext.get("coverage") if isinstance(ext, dict) else None
    return coverage if isinstance(coverage, dict) else None


def _channels_of(
    inputs: Inputs, result: dict[str, Any], coverage: dict[str, Any] | None
) -> set[str]:
    if coverage is not None and isinstance(coverage.get("channels"), dict):
        return set(coverage["channels"])
    if result["scope"].get("channel"):
        return {result["scope"]["channel"]}
    return {channel for item in inputs.project["platforms"] for channel in item["channels"]}


def _metric_item(
    inputs: Inputs, result: dict[str, Any], thresholds: dict[str, Any]
) -> dict[str, Any]:
    metric_id = result["metric_id"]
    role = result["role"]
    threshold = thresholds.get(metric_id) if role == "acceptance" else None
    if role == "post_hoc" or not result["preregistered"]:
        use = "post_hoc"
    elif role == "descriptive":
        use = "descriptive"
    elif threshold is not None and is_overall(result):
        use = "acceptance"
    else:
        use = "display_only"
    interval = result["interval"]
    coverage = _coverage(result)
    limitations: list[str] = []
    if use == "post_hoc":
        limitations.append(POST_HOC_LABEL)
    if result["value"] is None:
        limitations.append(f"无法计算：{result['null_reason']}")
    limitations.extend(f"样本不充分：{reason}" for reason in result["sufficiency"]["reasons"])
    if result["excluded"]["count"]:
        limitations.append(
            f"有 {result['excluded']['count']} 条观测未进入分母，规则与数量见 excluded"
        )
    components = result.get("components")
    if isinstance(components, dict) and isinstance(components.get("interval_note"), str):
        limitations.append(components["interval_note"])
    if result["value"] is not None and interval is None:
        limitations.append("没有区间，无法评估不确定性")
    if "api" in _channels_of(inputs, result, coverage):
        limitations.append("含 API 通道数据：接口返回的结果与网页端界面展示的结果可能不同")
    return {
        "metric_id": metric_id,
        "title": metric_title(metric_id),
        "scope": result["scope"],
        "scope_label": _scope_label(result["scope"]),
        "value": result["value"],
        "null_reason": result["null_reason"],
        "numerator": result["numerator"],
        "denominator": result["denominator"],
        "interval": interval,
        "method_label": METHOD_LABELS.get(interval["method"], interval["method"])
        if interval
        else "无区间",
        "clustering": result.get("clustering"),
        "sufficiency": result["sufficiency"],
        "excluded": result["excluded"],
        "coverage": coverage,
        "environment": inputs.project["environment"],
        "definition_version": result["definition_version"],
        "role": role,
        "role_label": ROLE_LABELS[role],
        "preregistered": result["preregistered"],
        "threshold": threshold,
        "threshold_met": meets(result["value"], threshold)
        if use == "acceptance" and result["value"] is not None and threshold is not None
        else None,
        "use": use,
        "use_label": USE_LABELS[use],
        "limitations": limitations,
    }


def _metrics(inputs: Inputs, acceptance: Acceptance) -> dict[str, Any]:
    metrics = inputs.get("metrics")
    if metrics is None:
        return _not_run()
    thresholds = {item.metric_id: item.threshold for item in acceptance.targets}
    return {
        "status": "ok",
        "target_entity_id": metrics.get("target_entity_id"),
        "prompt_set": metrics.get("prompt_set"),
        "parameters": metrics.get("parameters"),
        "sufficiency_thresholds": metrics.get("sufficiency_thresholds"),
        "observations": metrics.get("observations"),
        "notices": metrics.get("notices", []),
        "items": [_metric_item(inputs, result, thresholds) for result in metrics["results"]],
    }


# ------------------------------------------------------------------ 其他结果文件


def _side(side: dict[str, Any]) -> dict[str, Any]:
    channel = side.get("channel")
    return {**side, "label": channel_label(channel) if isinstance(channel, str) else None}


def _percent(value: Any) -> float | None:
    return value * 100 if isinstance(value, int | float) and not isinstance(value, bool) else None


def _crosscheck(inputs: Inputs) -> dict[str, Any]:
    doc = inputs.get("crosscheck")
    if doc is None:
        return _not_run()
    rows = []
    for row in doc["results"]:
        interval = row.get("interval")
        rows.append(
            {
                **row,
                "a": _side(row["a"]),
                "b": _side(row["b"]),
                "difference_pp": _percent(row.get("difference")),
                "interval_pp": {
                    "low": _percent(interval.get("low")),
                    "high": _percent(interval.get("high")),
                }
                if isinstance(interval, dict)
                else None,
            }
        )
    return {
        "status": doc["status"],
        "status_label": CROSSCHECK_LABELS[doc["status"]],
        "reason": doc.get("reason"),
        "formula": doc.get("formula"),
        "formula_text": doc["formula_text"],
        "formula_note": doc["formula_note"],
        "formula_declaration": FORMULA_DECLARATION,
        "threshold": doc["threshold"],
        "preregistered": doc["preregistered"],
        "metric_id": doc.get("metric_id"),
        "metric_title": metric_title(str(doc.get("metric_id"))),
        "channel_a": channel_entry(doc["channel_a"])
        if isinstance(doc.get("channel_a"), str)
        else None,
        "channel_b": channel_entry(doc["channel_b"])
        if isinstance(doc.get("channel_b"), str)
        else None,
        "parameters": doc.get("parameters"),
        "prompt_set": doc.get("prompt_set"),
        "results": rows,
    }


def _compare(inputs: Inputs) -> dict[str, Any]:
    doc = inputs.get("compare")
    if doc is None:
        return _not_run()
    return {
        "status": doc["status"],
        "status_label": COMPARE_LABELS[doc["status"]],
        "refusal": doc["refusal"],
        "baseline": doc.get("baseline"),
        "current": doc.get("current"),
        "parameters": doc.get("parameters"),
        "results": [
            {**row, "metric_title": metric_title(str(row.get("metric_id")))}
            for row in doc["results"]
        ],
    }


def _alerts(inputs: Inputs) -> dict[str, Any]:
    doc = inputs.get("alerts")
    if doc is None:
        return _not_run()
    return {
        "status": doc["status"],
        "status_label": ALERT_LABELS[doc["status"]],
        "baseline_window": doc.get("baseline_window"),
        "current_window": doc.get("current_window"),
        "thresholds": doc.get("thresholds"),
        "alerts": doc["alerts"],
        "citation_changes": doc.get("citation_changes"),
        "leads": doc.get("leads", []),
    }


def _evidence(inputs: Inputs) -> dict[str, Any]:
    doc = inputs.get("evidence")
    if doc is None:
        return _not_run()
    return {
        "status": doc["status"],
        "status_label": EVIDENCE_LABELS[doc["status"]],
        "manifests": doc.get("manifests"),
        "latest_manifest": doc.get("latest_manifest"),
        "checked_files": doc.get("checked_files"),
        "problems": doc["problems"],
    }


def _exclusions(inputs: Inputs) -> dict[str, Any]:
    metrics = inputs.get("metrics")
    audit = metrics.get("exclusion_audit") if metrics is not None else None
    if audit is None:
        return _not_run()
    descriptions = {
        item["rule_id"]: item["description"]
        for item in inputs.project["preregistration"]["exclusion_rules"]
    }
    return {
        "status": "ok",
        "registered_rules": audit.get("registered_rules", []),
        "rules": [
            {
                "rule_id": item["rule_id"],
                "description": descriptions.get(item["rule_id"]),
                "registered": item.get("registered", item["rule_id"] in descriptions),
                "listed_observations": item.get("listed_observations"),
                "excluded_observations": item["excluded_observations"],
            }
            for item in audit["rules"]
        ],
        "unregistered_rules": audit["unregistered_rules"],
        "excluded_observations": audit["excluded_observations"],
        "thresholds": audit.get("thresholds"),
        "comparisons": audit["comparisons"],
        "selective": audit["selective"],
    }


def _query_texts(inputs: Inputs, plan: dict[str, Any]) -> dict[str, str]:
    wanted = (plan.get("prompt_set") or {}).get("content_hash")
    sealed = next(
        (item for item in inputs.sealed if item["content_hash"] == wanted), inputs.prompt_set
    )
    if sealed is None:
        return {}
    return {item["query_id"]: item["text"] for item in sealed["prompts"]}


def _recheck(inputs: Inputs) -> dict[str, Any]:
    plan, result = inputs.get("plan"), inputs.get("result")
    if plan is None and result is None:
        return _not_run()
    plan_hash = inputs.hashes.get("recheck/plan.json")
    section: dict[str, Any] = {"status": "ok", "plan": None, "result": None}
    if plan is not None:
        texts = _query_texts(inputs, plan)
        section["plan"] = {
            "sha256": plan_hash,
            "prompt_set": plan.get("prompt_set"),
            "fraction": plan["fraction"],
            "seed": plan["seed"],
            "preregistered": plan["preregistered"],
            "total_queries": plan.get("total_queries"),
            "query_ids": plan["query_ids"],
            "queries": [{"query_id": qid, "text": texts.get(qid)} for qid in plan["query_ids"]],
            "item_count": len(plan["items"]),
        }
    if result is not None:
        section["result"] = {
            "plan_hash": result["plan_hash"],
            "plan_matches": None if plan is None else result["plan_hash"] == plan_hash,
            "fields": result.get("fields"),
            "planned": result["planned"],
            "checked": result["checked"],
            "agreed": result["agreed"],
            "disagreed": result["disagreed"],
            "not_checked": result["not_checked"],
            "pass_rate": result["pass_rate"],
            "null_reason": result.get("null_reason"),
            "errors": len(result.get("errors") or []),
        }
    return section


def _disputes(inputs: Inputs) -> dict[str, Any]:
    doc = inputs.get("disputes")
    if doc is None:
        return _not_run()
    return {
        "status": "ok",
        "annotator_a": doc.get("annotator_a"),
        "annotator_b": doc.get("annotator_b"),
        "fields": doc.get("fields", []),
        "unresolved": doc["unresolved"],
        "total_items": len(doc["items"]),
        "unresolved_items": [item for item in doc["items"] if not item.get("resolved")],
    }


# ------------------------------------------------------------------ 限制、披露、签字


def _limitations(
    inputs: Inputs, rules: list[Rule], metrics: dict[str, Any]
) -> list[dict[str, str]]:
    items = [
        ("precheck_only", f"{NOTICE}；结论须经委托方、服务方与复核人核对并签字后使用。"),
        (
            "hash_scope",
            "证据哈希只能证明文件在登记后未被改动，不能证明采集时间真实；可信时间戳仅提供接口。",
        ),
        (
            "sampling_scope",
            "指标只反映所选封存题库、平台、时间窗口和采集环境下的抽样观测；AI 平台的回答会随时间、"
            "模型版本和上下文变化，本报告不对第三方平台今后的结果作任何承诺。",
        ),
        (
            "statistics",
            "区间按预先登记的置信水平计算：单题比例用 Wilson 区间，跨题汇总用按题聚类的 bootstrap；"
            "样本充分性默认要求至少 3 个时间窗口、单一关键指标至少 30 次有效观测（047 §8.3）。",
        ),
        (
            "annotation",
            "提及、推荐与位次标注由规则或人工给出，可能存在误判；抽样复核情况见“抽样复核”一节。",
        ),
        (
            "crosscheck_formula",
            f"交叉验证的偏离度公式（{DEFAULT_FORMULA_TEXT}）：{FORMULA_DECLARATION}。",
        ),
    ]
    if inputs.project.get("synthetic") is True:
        items.append(
            (
                "synthetic_data",
                "本报告使用合成数据：合成集 / 模拟环境通过不等于真实平台可用，不得作为真实效果宣传。",
            )
        )
    channels = {str(item.get("channel")) for item in inputs.observations} | {
        channel for item in inputs.project["platforms"] for channel in item["channels"]
    }
    if "api" in channels:
        items.append(
            ("api_channel", "API 通道的数据来自接口调用，与网页端界面展示的结果可能不同。")
        )
    not_run = [f"{item.rule_id}（{item.title}）" for item in rules if item.status == "not_run"]
    if not_run:
        items.append(
            ("checks_not_run", "以下检查未执行，相应结论不可得：" + "、".join(not_run) + "。")
        )
    notices = (
        {item["code"] for item in metrics.get("notices", [])}
        if metrics.get("status") == "ok"
        else set()
    )
    if "model_version_changed" in notices:
        items.append(
            (
                "model_version_changed",
                "观测期内模型或产品版本发生变化：版本变化前后的数据不得合并比较，须重建基线。",
            )
        )
    if "prompt_set_version_mismatch" in notices:
        items.append(
            (
                "prompt_set_version_mismatch",
                "有观测不属于所选封存题库版本：不同版本的题库不得混算，这些观测未参与指标。",
            )
        )
    if any(item["use"] == "post_hoc" for item in metrics.get("items", [])):
        items.append(("post_hoc_metrics", "事后指标只能展示，不能用于验收结论。"))
    audit = (inputs.get("metrics") or {}).get("exclusion_audit") or {}
    if audit.get("excluded_observations"):
        items.append(
            (
                "exclusions",
                f"有 {audit['excluded_observations']} 条观测按剔除规则被排除，规则与数量见“剔除”一节。",
            )
        )
    if inputs.project["disclosure"]["operator_is_provider"]:
        items.append(
            (
                "operator_is_provider",
                "工具操作方与服务方为同一主体，存在利益关系，见利益关系披露栏。",
            )
        )
    return [{"code": code, "text": text} for code, text in items]


def _disclosure(inputs: Inputs) -> dict[str, Any]:
    disclosure = inputs.project["disclosure"]
    warning = None
    if disclosure["operator_is_provider"]:
        warning = (
            f"注意：工具操作方与服务方为同一主体（{disclosure['tool_operator']}），"
            "本报告由利益相关方生成，只是预检结论，须由委托方或独立复核人核对。"
        )
    return {**disclosure, "operator_warning": warning}


def _signatures(inputs: Inputs) -> list[dict[str, Any]]:
    project = inputs.project
    return [
        {"role": role, "party": party, "name": None, "signature": None, "date": None}
        for role, party in (
            ("委托方", project["client"]["name"]),
            ("服务方", project["provider"]["name"]),
            ("复核人", None),
        )
    ]


def _observation_index(inputs: Inputs) -> dict[str, Any]:
    rows = observation_rows(inputs)
    return {
        "total": len(rows),
        "columns": list(COLUMNS),
        "preview": [dict(zip(COLUMNS, row, strict=True)) for row in rows[:PREVIEW_ROWS]],
    }


def build_report(inputs: Inputs, *, generated_at: str | None) -> dict[str, Any]:
    rules, acceptance = evaluate(inputs)
    metrics = _metrics(inputs, acceptance)
    synthetic = inputs.project.get("synthetic") is True
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "report",
        "project_id": inputs.project_id,
        "watermark": WATERMARK if synthetic else None,
        "generated_at": generated_at,
        "notice": NOTICE,
        "verdict": verdict(rules),
        "rules": [item.to_dict() for item in rules],
        "project": _project(inputs),
        "preregistration": _preregistration(inputs, rules),
        "metrics": metrics,
        "crosscheck": _crosscheck(inputs),
        "compare": _compare(inputs),
        "alerts": _alerts(inputs),
        "evidence": _evidence(inputs),
        "exclusions": _exclusions(inputs),
        "recheck": _recheck(inputs),
        "disputes": _disputes(inputs),
        "observation_index": _observation_index(inputs),
        "limitations": _limitations(inputs, rules, metrics),
        "disclosure": _disclosure(inputs),
        "signatures": _signatures(inputs),
        "inputs": inputs.files,
    }
    if synthetic:
        report["synthetic"] = True
    return report
