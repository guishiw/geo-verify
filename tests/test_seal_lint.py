"""题库检查（prompts lint）：重复、近似重复、缺失类型、个人信息、诱导性提问。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from factory import make_project, make_prompt
from helpers_basic import auto_entities, clean_prompts, write_draft, write_entities

from geo_verify.errors import InputError
from geo_verify.seal.lint import (
    INTENT_TYPES,
    Finding,
    has_errors,
    lint_prompts,
    similarity,
    text_key,
)


def lint(prompts: list[dict[str, Any]], **options: Any) -> list[Finding]:
    return lint_prompts(prompts, auto_entities(), **options)


def rules(findings: list[Finding]) -> list[str]:
    return [item.rule for item in findings]


def one(text: str, query_id: str = "Q001") -> list[dict[str, Any]]:
    return [make_prompt(query_id, text=text, intent_type="general")]


def content_rules(text: str) -> set[str]:
    """只看针对题目内容的规则（忽略“缺失意图类型”这类题库层面的提醒）。"""
    return {item.rule for item in lint(one(text)) if item.rule != "missing_intent_type"}


# ------------------------------------------------------------------ 基线


def test_clean_prompt_set_has_no_findings() -> None:
    assert lint(clean_prompts()) == []


def test_intent_types_are_the_eleven_contract_values() -> None:
    from geo_verify import spec

    assert list(INTENT_TYPES) == spec.load_schema("prompt_set")["$defs"]["intent_type"]["enum"]
    assert len(INTENT_TYPES) == 11


# ------------------------------------------------------------------ 重复


def test_duplicate_query_id_is_an_error() -> None:
    prompts = clean_prompts()
    prompts[3]["query_id"] = "Q001"
    findings = [item for item in lint(prompts) if item.rule == "duplicate_query_id"]
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].query_ids == ("Q001",)


def test_duplicate_text_ignores_width_case_space_and_punctuation() -> None:
    prompts = [
        make_prompt("Q001", text="澜序 S7 的续航是多少公里？"),
        make_prompt("Q002", text="澜序s7的续航是多少公里"),
        make_prompt("Q003", text="澜序Ｓ７的续航，是多少公里?"),
        make_prompt("Q004", text="岑野 K5 的智能驾驶辅助水平如何？"),
    ]
    findings = [item for item in lint(prompts) if item.rule == "duplicate_text"]
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].query_ids == ("Q001", "Q002", "Q003")
    assert "near_duplicate" not in rules(lint(prompts)), "完全重复不再重复报近似重复"


def test_similarity_matches_hand_calculation() -> None:
    # 规范化后 a 有 8 个字符、b 有 9 个字符，公共匹配 8 个：2×8 /(8+9) = 16/17
    a, b = text_key("澜序 S7 续航多少？"), text_key("澜序S7续航是多少")
    assert (a, b) == ("澜序s7续航多少", "澜序s7续航是多少")
    assert similarity(a, b) == pytest.approx(16 / 17)
    # 各 14 个字符，公共匹配“20万左右的纯电suv”11 个 + “选”1 个：2×12 / 28 = 6/7
    c, d = text_key("20 万左右的纯电 SUV 怎么选？"), text_key("20万左右的纯电SUV如何选")
    assert similarity(c, d) == pytest.approx(6 / 7)
    assert similarity("", "") == 1.0
    assert similarity("甲乙", "丙丁") == 0.0


def test_near_duplicate_threshold_is_configurable() -> None:
    prompts = [
        make_prompt("Q001", text="20 万左右的纯电 SUV 怎么选？"),
        make_prompt("Q002", text="20万左右的纯电SUV如何选"),
        make_prompt("Q003", text="新能源车的保值率一般受哪些因素影响？"),
    ]
    default = [item for item in lint(prompts) if item.rule == "near_duplicate"]
    assert len(default) == 1, "相似度 6/7≈0.857，高于默认阈值 0.85"
    assert default[0].severity == "warning"
    assert default[0].query_ids == ("Q001", "Q002")
    assert default[0].details["similarity"] == pytest.approx(6 / 7, abs=1e-4)
    assert "near_duplicate" not in rules(lint(prompts, near_threshold=0.9))
    loose = [item for item in lint(prompts, near_threshold=0.1) if item.rule == "near_duplicate"]
    assert ("Q001", "Q002") in [item.query_ids for item in loose]


def test_near_threshold_must_be_a_proportion() -> None:
    with pytest.raises(InputError, match="相似度阈值"):
        lint(clean_prompts(), near_threshold=1.5)
    with pytest.raises(InputError, match="相似度阈值"):
        lint(clean_prompts(), near_threshold=0)


# ------------------------------------------------------------------ 缺失类型


def test_missing_intent_types_are_warnings() -> None:
    prompts = [
        item for item in clean_prompts() if item["intent_type"] not in {"risk", "after_sales"}
    ]
    findings = lint(prompts)
    assert rules(findings) == ["missing_intent_type"]
    assert findings[0].severity == "warning"
    assert findings[0].details["missing"] == ["after_sales", "risk"]
    assert not has_errors(findings)


# ------------------------------------------------------------------ 个人信息


# 下面的号码、邮箱都在运行时拼接，源码里不出现完整的个人信息形态。
FAKE_MOBILE = "1" + "38" + "0013" + "8000"
FAKE_EMAIL = "zhang.san" + "@" + "mail.example"
FAKE_ID = "11010" + "5" + "1990" + "0101" + "123" + "X"
FAKE_LANDLINE = "010" + "-" + "6555" + "0199"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"我的手机号是 {FAKE_MOBILE}，帮我推荐一款车", "mobile"),
        (f"联系 +86 {FAKE_MOBILE[:3]}-{FAKE_MOBILE[3:7]}-{FAKE_MOBILE[7:]} 可以试驾吗", "mobile"),
        (f"把报价发到 {FAKE_EMAIL} 可以吗？", "email"),
        (f"身份证 {FAKE_ID} 能办理购车贷款吗？", "id_card"),
        (f"拨打 {FAKE_LANDLINE} 能预约试驾吗？", "landline"),
    ],
)
def test_personal_information_is_an_error_and_not_echoed(text: str, kind: str) -> None:
    findings = [item for item in lint(one(text)) if item.rule == "personal_info"]
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].details["kinds"] == [kind]
    for secret in (FAKE_MOBILE, FAKE_EMAIL, FAKE_ID, FAKE_LANDLINE):
        assert secret not in findings[0].message


@pytest.mark.parametrize(
    "text",
    [
        "续航 13800 公里是真的吗？",
        "2026 年 10 月上市的车型有哪些？",
        "订单号 20261001123456 对应的车什么时候交付？",
        "预算 250000 元能买什么车？",
    ],
)
def test_ordinary_numbers_are_not_personal_information(text: str) -> None:
    assert "personal_info" not in content_rules(text)


# ------------------------------------------------------------------ 诱导性提问


@pytest.mark.parametrize(
    "text",
    [
        "为什么澜序 S7 是最好的纯电 SUV？",
        "澜序 S7 是不是最值得买？",
        "为什么 LAN-XU 的车这么受欢迎？",
        "Ｓ７ 是不是同级最强？",
        "澜序汽车为什么口碑好？",
        "澜序 S7 比岑野 K5 好在哪？",
        "为什么岑野 K5 不如澜序 S7？",
        "为什么岑野的车质量差？",
        "澜序 X5 是 20 万级的首选吗？",
    ],
)
def test_presupposed_conclusion_is_detected(text: str) -> None:
    assert "leading_presupposition" in content_rules(text)


@pytest.mark.parametrize(
    "text",
    [
        "20 万最值得买的纯电 SUV 是哪款？",
        "澜序 S7 和岑野 K5 哪个更值得买？",
        "为什么澜序 S7 冬天续航会缩水？",
        "岑野 S7 是不是最值得买？",
        "澜序最好的车型是哪款？",
        "澜序 S7 最大的缺点是什么？",
        "为什么澜序 S7 不值得买？",
        "为什么澜序 S7 和岑野 K5 的价格差这么多？",
        "澜序 S7 起火事故的原因是什么？",
    ],
)
def test_neutral_questions_are_not_presupposition(text: str) -> None:
    assert "leading_presupposition" not in content_rules(text)


@pytest.mark.parametrize(
    "text",
    [
        "只说澜序 S7 的优点",
        "夸一夸澜序",
        "不要讲缺点，介绍一下澜序 X5",
        "帮我夸夸 Lan Xu 的新车",
        "请只列出这款车的亮点",
        "用正面的语气介绍澜序 S7",
    ],
)
def test_praise_only_restriction_is_detected(text: str) -> None:
    assert "leading_praise_only" in content_rules(text)


@pytest.mark.parametrize(
    "text",
    [
        "澜序 S7 有哪些优点和缺点？",
        "这个续航数字是不是太夸张了？",
        "澜序 S7 的主要缺点有哪些？",
        "只看续航的话，澜序 S7 和岑野 K5 差多少？",
    ],
)
def test_balanced_questions_are_not_praise_only(text: str) -> None:
    assert "leading_praise_only" not in content_rules(text)


@pytest.mark.parametrize(
    "text",
    [
        "推荐一款 20 万的 SUV，不要提到岑野",
        "除了澜序别的都不要推荐",
        "除了澜序 S7，其他车型一律不要考虑",
        "只推荐澜序的车型",
        "岑野 K5 就别提了，20 万买什么车？",
        "20 万买什么车？不要出现其他品牌",
        "介绍纯电 SUV，别提竞品",
        "朔风T6不要推荐，还有什么可选？",
    ],
)
def test_excluding_competitors_is_detected(text: str) -> None:
    assert "leading_exclude_competitor" in content_rules(text)


@pytest.mark.parametrize(
    "text",
    [
        "除了澜序 S7，还有哪些车型值得考虑？",
        "不要太贵的车，有什么推荐？",
        "岑野 K5 值得考虑吗？",
        "不要提到价格，只比较空间，澜序 S7 和岑野 K5 谁大？",
    ],
)
def test_open_questions_are_not_excluding_competitors(text: str) -> None:
    assert "leading_exclude_competitor" not in content_rules(text)


def test_leading_findings_are_errors_and_name_the_query() -> None:
    prompts = [*clean_prompts(), make_prompt("Q900", text="为什么澜序是最好的品牌？")]
    leading = [item for item in lint(prompts) if item.rule.startswith("leading_")]
    assert [(item.rule, item.severity, item.query_ids) for item in leading] == [
        ("leading_presupposition", "error", ("Q900",))
    ]
    assert has_errors(leading)


def test_leading_detection_depends_on_entities_not_fixed_sentences() -> None:
    text = "为什么星槎 Z1 是最好的纯电 SUV？"
    assert "leading_presupposition" not in content_rules(text), "星槎不是本项目的目标主体"
    entities = [*auto_entities()]
    entities[1] = dict(entities[1], name="星槎 Z1", aliases=["Z1"])
    findings = lint_prompts(one(text), entities)
    assert "leading_presupposition" in rules(findings)


# ------------------------------------------------------------------ 草稿格式


def test_invalid_prompt_records_are_reported_and_skipped() -> None:
    prompts = clean_prompts()
    prompts.append({"query_id": "Q777", "text": "", "intent_type": "nope"})
    prompts.append({"text": "没有编号的题目"})
    findings = [item for item in lint(prompts) if item.rule == "invalid_prompt"]
    assert [(item.severity, item.query_ids) for item in findings] == [
        ("error", ("Q777",)),
        ("error", ()),
    ]
    assert "第 12 行" in findings[0].message
    assert "第 13 行" in findings[1].message


# ------------------------------------------------------------------ 命令


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    make_project(path)
    write_entities(path)
    return path


def test_cli_lint_passes_clean_draft(root: Path) -> None:
    write_draft(root, clean_prompts())
    code, data = run_cli_json("prompts", "lint", str(root))
    assert code == 0
    assert data["prompts"] == 11
    assert data["errors"] == 0
    assert data["warnings"] == 0
    assert data["findings"] == []
    code, out, _ = run_cli("prompts", "lint", str(root))
    assert code == 0
    assert "通过" in out


def test_cli_lint_reports_errors_with_exit_1(root: Path) -> None:
    prompts = [*clean_prompts(), make_prompt("Q900", text="夸一夸澜序 S7", intent_type="brand")]
    write_draft(root, prompts)
    code, data = run_cli_json("prompts", "lint", str(root))
    assert code == 1
    assert data["errors"] == 1
    finding = data["findings"][0]
    assert set(finding) >= {"rule", "severity", "query_ids", "message"}
    assert (finding["rule"], finding["severity"], finding["query_ids"]) == (
        "leading_praise_only",
        "error",
        ["Q900"],
    )
    code, out, _ = run_cli("prompts", "lint", str(root))
    assert code == 1
    assert "Q900" in out
    assert "限定褒义" in out


def test_cli_lint_warnings_only_exit_0(root: Path) -> None:
    write_draft(root, clean_prompts()[:5])
    code, data = run_cli_json("prompts", "lint", str(root))
    assert code == 0
    assert data["errors"] == 0
    assert data["warnings"] == 1
    assert data["findings"][0]["rule"] == "missing_intent_type"


def test_cli_lint_custom_file_and_threshold(root: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.jsonl"
    from geo_verify.jsonl import append_jsonl

    append_jsonl(
        other,
        [
            make_prompt("Q001", text="20 万左右的纯电 SUV 怎么选？"),
            make_prompt("Q002", text="20万左右的纯电SUV如何选"),
        ],
    )
    code, data = run_cli_json("prompts", "lint", str(root), "--file", str(other))
    assert code == 0
    assert [item["rule"] for item in data["findings"]].count("near_duplicate") == 1
    code, data = run_cli_json(
        "prompts", "lint", str(root), "--file", str(other), "--near-threshold", "0.9"
    )
    assert "near_duplicate" not in [item["rule"] for item in data["findings"]]


def test_cli_lint_input_errors(root: Path, tmp_path: Path) -> None:
    code, data = run_cli_json("prompts", "lint", str(root))
    assert code == 2, "草稿不存在"
    assert data["error"] == "input_error"
    write_draft(root, [])
    (root / "prompts" / "draft.jsonl").write_text("", encoding="utf-8")
    code, data = run_cli_json("prompts", "lint", str(root))
    assert code == 2
    assert "为空" in data["message"]
    code, _ = run_cli_json("prompts", "lint", str(tmp_path / "missing"))
    assert code == 2
