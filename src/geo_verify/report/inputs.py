"""报告的输入：项目文件与各命令的结果文件。

report 只读取这些文件，不重新计算。结果文件缺失时记为 None（对应判定规则“未执行”）；
存在但结构不对时给出输入错误（退出码 2），并指明文件。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from geo_verify import project as project_module
from geo_verify import spec
from geo_verify.canon import sha256_file
from geo_verify.errors import InputError
from geo_verify.jsonl import read_json, read_jsonl

RESULT_FILES: dict[str, tuple[str, str]] = {
    "metrics": ("out/metrics.json", "metrics"),
    "crosscheck": ("out/crosscheck.json", "crosscheck"),
    "compare": ("out/compare.json", "compare"),
    "alerts": ("out/alerts.json", "alerts"),
    "evidence": ("out/evidence-verify.json", "evidence_verify"),
    "disputes": ("out/disputes.json", "disputes"),
    "plan": ("recheck/plan.json", "recheck_plan"),
    "result": ("recheck/result.json", "recheck_result"),
}


@dataclass
class Inputs:
    root: Path
    project: dict[str, Any]
    entities: dict[str, dict[str, Any]]
    sealed: list[dict[str, Any]]
    prompt_set: dict[str, Any] | None
    observations: list[dict[str, Any]]
    excluded: dict[str, str]
    results: dict[str, dict[str, Any] | None]
    hashes: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> dict[str, Any] | None:
        return self.results.get(name)

    @property
    def files(self) -> list[dict[str, str]]:
        return [{"path": path, "sha256": self.hashes[path]} for path in sorted(self.hashes)]

    @property
    def project_id(self) -> str:
        project_id: str = self.project["project_id"]
        return project_id


class _Checker:
    """结果文件的基本结构检查：只检查报告会用到的字段。"""

    def __init__(self, path: str) -> None:
        self.path = path

    def fail(self, message: str) -> InputError:
        return InputError(f"{self.path} 结构不对：{message}")

    def need(self, condition: bool, message: str) -> None:
        if not condition:
            raise self.fail(message)

    def field(
        self, doc: dict[str, Any], key: str, kinds: tuple[type, ...], *, optional: bool = False
    ) -> Any:
        if key not in doc:
            self.need(optional, f"缺少字段 {key}")
            return None
        value = doc[key]
        if value is None and optional:
            return None
        ok = isinstance(value, kinds) and not (isinstance(value, bool) and bool not in kinds)
        self.need(ok, f"字段 {key} 的类型不对")
        return value

    def enum(self, doc: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str:
        value = doc.get(key)
        self.need(value in allowed, f"字段 {key} 只能是 {' / '.join(allowed)}")
        return str(value)

    def objects(
        self, doc: dict[str, Any], key: str, *, optional: bool = False
    ) -> list[dict[str, Any]]:
        items = self.field(doc, key, (list,), optional=optional)
        if items is None:
            return []
        self.need(
            all(isinstance(item, dict) for item in items), f"{key} 的每一项都必须是 JSON 对象"
        )
        return list(items)

    def prompt_set(self, doc: Any, key: str = "prompt_set") -> None:
        if not isinstance(doc, dict) or doc.get(key) is None:
            return
        value = doc[key]
        self.need(
            isinstance(value, dict) and isinstance(value.get("content_hash"), str),
            f"{key} 须为含 content_hash 的对象",
        )


NUMBER = (int, float)


def _check_metrics(check: _Checker, doc: dict[str, Any]) -> None:
    for index, result in enumerate(check.objects(doc, "results"), start=1):
        spec.require_valid("metric_result", result, where=f"{check.path} 的第 {index} 个结果")
    for item in check.objects(doc, "notices", optional=True):
        check.need(isinstance(item.get("code"), str), "notices 的每一项都必须有字符串 code")
        check.need(isinstance(item.get("message", ""), str), "notices 的 message 必须是字符串")
    check.objects(doc, "registered_metrics", optional=True)
    check.prompt_set(doc)
    audit = check.field(doc, "exclusion_audit", (dict,), optional=True)
    if audit is not None:
        check.field(audit, "selective", (bool,))
        unregistered = check.field(audit, "unregistered_rules", (list,))
        check.need(
            all(isinstance(item, str) for item in unregistered), "unregistered_rules 须为字符串列表"
        )
        for rule in check.objects(audit, "rules"):
            check.need(isinstance(rule.get("rule_id"), str), "exclusion_audit.rules 须有 rule_id")
            check.field(rule, "excluded_observations", (int,))
        check.objects(audit, "comparisons")
        check.field(audit, "excluded_observations", (int,))


def _check_crosscheck(check: _Checker, doc: dict[str, Any]) -> None:
    check.enum(doc, "status", ("pass", "fail", "undetermined"))
    check.field(doc, "threshold", NUMBER)
    check.field(doc, "formula_text", (str,))
    check.field(doc, "formula_note", (str,))
    check.field(doc, "preregistered", (bool,))
    check.prompt_set(doc)
    for row in check.objects(doc, "results"):
        for side in ("a", "b"):
            check.need(isinstance(row.get(side), dict), f"results 的每一行都要有 {side} 对象")


def _check_compare(check: _Checker, doc: dict[str, Any]) -> None:
    check.enum(doc, "status", ("ok", "refused", "insufficient"))
    check.objects(doc, "results")
    check.field(doc, "refusal", (dict,), optional=True)
    check.prompt_set(doc.get("current"))


def _check_alerts(check: _Checker, doc: dict[str, Any]) -> None:
    check.enum(doc, "status", ("alert", "clear"))
    for item in check.objects(doc, "alerts"):
        check.need(isinstance(item.get("message"), str), "alerts 的每一项都要有字符串 message")
    for item in check.objects(doc, "leads", optional=True):
        check.need(isinstance(item.get("text"), str), "leads 的每一项都要有字符串 text")
    check.field(doc, "citation_changes", (dict,), optional=True)


def _check_evidence(check: _Checker, doc: dict[str, Any]) -> None:
    check.enum(doc, "status", ("pass", "fail"))
    check.objects(doc, "problems")


def _check_disputes(check: _Checker, doc: dict[str, Any]) -> None:
    check.objects(doc, "items")
    check.field(doc, "unresolved", (int,))


def _check_plan(check: _Checker, doc: dict[str, Any]) -> None:
    check.field(doc, "fraction", NUMBER)
    check.field(doc, "seed", (int,))
    check.field(doc, "preregistered", (bool,))
    query_ids = check.field(doc, "query_ids", (list,))
    check.need(all(isinstance(item, str) for item in query_ids), "query_ids 须为字符串列表")
    check.objects(doc, "items")
    check.prompt_set(doc)


def _check_result(check: _Checker, doc: dict[str, Any]) -> None:
    for key in ("planned", "checked", "agreed", "disagreed", "not_checked"):
        check.field(doc, key, (int,))
    check.field(doc, "pass_rate", NUMBER, optional=True)
    check.field(doc, "plan_hash", (str,), optional=True)
    check.objects(doc, "items")
    check.objects(doc, "errors", optional=True)


CHECKS: dict[str, Callable[[_Checker, dict[str, Any]], None]] = {
    "metrics": _check_metrics,
    "crosscheck": _check_crosscheck,
    "compare": _check_compare,
    "alerts": _check_alerts,
    "evidence": _check_evidence,
    "disputes": _check_disputes,
    "plan": _check_plan,
    "result": _check_result,
}


def _load_result(root: Path, name: str, project_id: str) -> dict[str, Any] | None:
    relative, kind = RESULT_FILES[name]
    path = root / relative
    if not path.exists():
        return None
    doc = read_json(path)
    check = _Checker(relative)
    check.need(isinstance(doc, dict), "应为 JSON 对象")
    check.need(doc.get("kind") == kind, f"kind 应为 {kind}")
    owner = doc.get("project_id", project_id)
    check.need(owner == project_id, f"属于另一个项目（{owner!r}），不能用于项目 {project_id}")
    CHECKS[name](check, doc)
    result: dict[str, Any] = doc
    return result


def _select_prompt_set(
    sealed: list[dict[str, Any]], metrics: dict[str, Any] | None
) -> dict[str, Any] | None:
    """报告所依据的封存题库：与 metrics.json 一致的版本，否则取最新版本。"""
    if not sealed:
        return None
    wanted = (metrics or {}).get("prompt_set") or {}
    for item in sealed:
        if item["content_hash"] == wanted.get("content_hash"):
            return item
    return sealed[-1]


def load_inputs(root: Path) -> Inputs:
    project_paths = project_module.paths(root)
    project = project_module.load_project(root)
    hashes: dict[str, str] = {"project.json": sha256_file(project_paths.project_file)}
    entities: dict[str, dict[str, Any]] = {}
    if project_paths.entities.exists():
        entities = {item["entity_id"]: item for item in project_module.load_entities(root)}
        hashes["entities.jsonl"] = sha256_file(project_paths.entities)
    sealed = project_module.load_sealed_prompt_sets(root)
    for item in sealed:
        relative = f"prompts/sealed/v{item['version']:04d}.json"
        hashes[relative] = sha256_file(root / relative)
    observations = read_jsonl(project_paths.observations, missing_ok=True)
    if project_paths.observations.exists():
        hashes["observations.jsonl"] = sha256_file(project_paths.observations)
    events = project_module.load_audit(root)
    if project_paths.audit.exists():
        hashes["audit.jsonl"] = sha256_file(project_paths.audit)
    results: dict[str, dict[str, Any] | None] = {}
    for name, (relative, _) in RESULT_FILES.items():
        results[name] = _load_result(root, name, project["project_id"])
        if results[name] is not None:
            hashes[relative] = sha256_file(root / relative)
    return Inputs(
        root=root,
        project=project,
        entities=entities,
        sealed=sealed,
        prompt_set=_select_prompt_set(sealed, results["metrics"]),
        observations=observations,
        excluded=project_module.exclusions(events),
        results=results,
        hashes=hashes,
    )
