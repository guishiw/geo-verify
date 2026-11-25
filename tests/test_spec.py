"""契约测试：每个契约的合法样例全部通过、非法样例全部被拒，且拒绝原因与预期一致。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import SPEC_DIR, load_examples, valid_example
from jsonschema import Draft202012Validator

from geo_verify import spec
from geo_verify.errors import InputError


def _cases(kind: str) -> list[Any]:
    cases = []
    for name in spec.CONTRACTS:
        for index, item in enumerate(load_examples(name)[kind]):
            label = f"{name}-{index}" if kind == "valid" else f"{name}-{item['why']}"
            cases.append(pytest.param(name, item, id=label))
    return cases


def test_contract_list_matches_files() -> None:
    on_disk = sorted(
        path.name.removesuffix(".schema.json") for path in SPEC_DIR.glob("*.schema.json")
    )
    assert on_disk == sorted(spec.CONTRACTS)
    assert len(spec.CONTRACTS) == 11


@pytest.mark.parametrize("name", spec.CONTRACTS)
def test_schema_is_draft_2020_12_and_closed(name: str) -> None:
    schema = spec.load_schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False, "默认必须拒绝未知字段"
    assert "schema_version" in schema["required"]
    assert schema["properties"]["ext"] == {"type": "object"}, "扩展只能写入 ext"


@pytest.mark.parametrize("name", spec.CONTRACTS)
def test_each_contract_has_enough_examples(name: str) -> None:
    examples = load_examples(name)
    assert len(examples["valid"]) >= 1
    assert len(examples["invalid"]) >= 5


@pytest.mark.parametrize(("name", "instance"), _cases("valid"))
def test_valid_examples_pass(name: str, instance: dict[str, Any]) -> None:
    assert spec.iter_issues(name, instance) == []
    assert spec.is_valid(name, instance)
    spec.require_valid(name, instance)


@pytest.mark.parametrize(("name", "case"), _cases("invalid"))
def test_invalid_examples_rejected_for_expected_reason(name: str, case: dict[str, Any]) -> None:
    issues = spec.iter_issues(name, case["instance"])
    assert issues, f"非法样例未被拒绝：{case['why']}"
    found = {(issue.path, issue.keyword) for issue in issues}
    expected = (case["expect"]["path"], case["expect"]["keyword"])
    assert expected in found, f"{case['why']}：期望 {expected}，实际 {sorted(found)}"
    with pytest.raises(InputError) as excinfo:
        spec.require_valid(name, case["instance"], where="样例")
    assert excinfo.value.exit_code == 2
    assert excinfo.value.details


@pytest.mark.parametrize("name", spec.CONTRACTS)
def test_unknown_field_rejected_but_ext_allowed(name: str) -> None:
    instance = valid_example(name)
    instance["ext"] = {"vendor_note": "扩展字段只能放在 ext 里"}
    assert spec.is_valid(name, instance)
    instance["vendor_note"] = "顶层未知字段"
    assert not spec.is_valid(name, instance)


@pytest.mark.parametrize("name", spec.CONTRACTS)
def test_schema_version_required_and_checked(name: str) -> None:
    instance = valid_example(name)
    del instance["schema_version"]
    assert not spec.is_valid(name, instance)
    instance["schema_version"] = "2.0"
    assert not spec.is_valid(name, instance)
    instance["schema_version"] = "1.3"
    assert spec.is_valid(name, instance), "同一大版本的小版本应当兼容"


def test_intent_types_cover_eleven_kinds_and_business_level_is_separate() -> None:
    schema = spec.load_schema("prompt_set")
    intents = schema["$defs"]["intent_type"]["enum"]
    assert len(intents) == len(set(intents)) == 11
    prompt = schema["properties"]["prompts"]["items"]["properties"]
    assert prompt["business_level"]["enum"] == ["L1", "L2", "L3", None]
    assert not set(intents) & {"L1", "L2", "L3"}


def test_observation_null_requires_reason_for_every_nullable_field() -> None:
    nullable = ["logged_in", "history_cleared", "web_search", "region", "device", "operator"]
    for field in nullable:
        instance = valid_example("observation")
        instance[field] = None
        instance["null_reasons"] = {
            key: value for key, value in instance["null_reasons"].items() if key != field
        }
        other_nulls = [key for key in nullable if instance[key] is None and key != field]
        for key in other_nulls:
            instance["null_reasons"][key] = "测试占位原因"
        assert not spec.is_valid("observation", instance), field
        instance["null_reasons"][field] = "确实无法获得"
        assert spec.is_valid("observation", instance), field


def test_observation_citation_distinguishes_inline_and_source_list() -> None:
    schema = spec.load_schema("observation")
    placement = schema["properties"]["citations"]["items"]["properties"]["placement"]
    assert placement["enum"] == ["inline", "source_list"]


def test_unknown_contract_is_input_error() -> None:
    with pytest.raises(InputError):
        spec.schema_path("nonexistent")


def test_examples_are_marked_synthetic() -> None:
    for name in spec.CONTRACTS:
        first = load_examples(name)["valid"][0]
        assert first.get("synthetic") is True, f"{name} 的首个合法样例必须带 synthetic: true"


def test_examples_only_use_reserved_domains() -> None:
    text = "".join(
        (SPEC_DIR / "examples" / f"{name}.examples.json").read_text(encoding="utf-8")
        for name in spec.CONTRACTS
    )
    import re

    hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+)", text))
    assert hosts, "样例里应当有引用链接"
    assert all(host.endswith((".example", ".test")) for host in hosts), hosts
    assert json.loads(json.dumps(sorted(hosts)))


def test_spec_root_prefers_packaged_copy(tmp_path, monkeypatch) -> None:
    package = tmp_path / "site-packages" / "geo_verify"
    (package / "_spec").mkdir(parents=True)
    monkeypatch.setattr(spec, "__file__", str(package / "spec.py"))
    assert spec.spec_root() == (package / "_spec").resolve()


def test_spec_root_missing_is_input_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(spec, "__file__", str(tmp_path / "a" / "b" / "spec.py"))
    with pytest.raises(InputError, match="找不到契约目录"):
        spec.spec_root()


def test_missing_schema_file_is_input_error(tmp_path, monkeypatch) -> None:
    package = tmp_path / "site-packages" / "geo_verify"
    (package / "_spec" / "v1").mkdir(parents=True)
    monkeypatch.setattr(spec, "__file__", str(package / "spec.py"))
    spec.load_schema.cache_clear()
    try:
        with pytest.raises(InputError, match="契约文件缺失"):
            spec.load_schema("project")
    finally:
        spec.load_schema.cache_clear()


def test_require_valid_message_without_location() -> None:
    with pytest.raises(InputError) as excinfo:
        spec.require_valid("entity", {"schema_version": "1.0"})
    assert excinfo.value.message.startswith("不符合契约 entity")
