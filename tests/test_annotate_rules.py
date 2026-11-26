"""规则标注：提及、推荐、否定提及与倾向。每条规则都有正例和反例。

期望值都是人工阅读例句后给出的判断；span 的位置由手工数原文字符得到。
实体表见 helpers_basic.auto_entities：目标品牌澜序（S7、X5），竞品岑野（K5、与目标同名的 S7）、朔风（T6）。
"""

from __future__ import annotations

import pytest
from helpers_annotate import judge, span_text, verdict
from helpers_basic import entity

from geo_verify.annotate.rules import RuleAnnotator

S7 = "product-lanxu-s7"
X5 = "product-lanxu-x5"
LANXU = "brand-lanxu"
K5 = "product-cenye-k5"
CENYE_S7 = "product-cenye-s7"
CENYE = "brand-cenye"
T6 = "product-shuofeng-t6"

MENTION_ONLY = (True, False, False)
RECOMMENDED = (True, True, False)
NEGATED = (True, False, True)
UNSURE = (True, None, False)
ABSENT = (False, False, False)


# ------------------------------------------------------------------ 提及


def test_product_mention_also_counts_for_its_brand() -> None:
    text = "澜序 S7 续航 620 km。"
    result = judge(text)
    assert verdict(text, S7) == MENTION_ONLY
    assert verdict(text, LANXU) == MENTION_ONLY
    # 澜(0)序(1) (2)S(3)7(4)：品牌的提及位置就是产品的位置
    assert ("mention", 0, 5) in result[LANXU].spans
    assert "mention_via_product" in result[LANXU].codes
    assert "mention_direct" in result[S7].codes
    assert verdict(text, X5) == ABSENT
    assert verdict(text, CENYE) == ABSENT


def test_brand_mention_does_not_imply_product_mention() -> None:
    text = "澜序这个品牌口碑不错。"
    assert judge(text)[LANXU].mentioned is True
    assert judge(text)[S7].mentioned is False


def test_competitor_product_with_the_same_name_is_not_the_target() -> None:
    text = "岑野 S7 值得考虑。"
    assert verdict(text, S7) == ABSENT
    assert verdict(text, LANXU) == ABSENT
    assert verdict(text, CENYE_S7) == RECOMMENDED
    assert verdict(text, CENYE) == RECOMMENDED


def test_exclusion_phrase_is_not_a_mention() -> None:
    assert verdict("《波澜序曲》很好听，推荐。", LANXU) == ABSENT


def test_unresolvable_homograph_is_listed_for_review_not_counted() -> None:
    entities = [
        entity("brand-a", "甲牌", kind="brand"),
        entity("prod-a-k5", "甲牌 K5", aliases=("K5",), parent="brand-a"),
        entity("brand-b", "乙牌", kind="brand", role="competitor"),
        entity("prod-b-k5", "乙牌 K5", aliases=("K5",), parent="brand-b", role="competitor"),
    ]
    annotator = RuleAnnotator(entities, ["brand-a", "prod-a-k5", "brand-b", "prod-b-k5"])
    result = annotator.judge("K5 值得考虑")
    for entity_id in ("brand-a", "prod-a-k5", "brand-b", "prod-b-k5"):
        assert result[entity_id].mentioned is False
        assert result[entity_id].ambiguous == [(0, 2)]
        assert "ambiguous_alias" in result[entity_id].codes
        assert result[entity_id].note is not None
        assert "同形异义" in result[entity_id].note


# ------------------------------------------------------------------ 否定


@pytest.mark.parametrize(
    "text",
    [
        "不推荐岑野 K5。",
        "不建议选择岑野 K5。",
        "岑野 K5 不值得买。",
        "建议避开岑野 K5。",
        "不要选岑野 K5。",
        "别买岑野 K5。",
        "岑野 K5 不是好的选择。",
        "我们并不推荐岑野 K5。",
        "不太推荐岑野 K5。",
        "岑野 K5 慎选。",
        "岑野 K5 真的不值得考虑。",
    ],
)
def test_negated_recommendation_is_a_negated_mention(text: str) -> None:
    assert verdict(text, K5) == NEGATED
    assert verdict(text, CENYE) == NEGATED


@pytest.mark.parametrize(
    "text",
    [
        "岑野 K5 值得买吗？",
        "要不要买岑野 K5？",
        "值不值得买岑野 K5",
        "岑野 K5 推不推荐？",
        "岑野 K5 的推荐度如何",
    ],
)
def test_questions_are_neither_recommendation_nor_negation(text: str) -> None:
    assert verdict(text, K5) == MENTION_ONLY


def test_unrelated_negative_words_do_not_negate() -> None:
    # “不要犹豫”不是否定推荐；第二个分句里的“值得考虑”推荐 K5
    assert verdict("不要犹豫，岑野 K5 值得考虑。", K5) == RECOMMENDED


def test_negation_span_covers_the_negator() -> None:
    text = "不推荐澜序 S7。"
    result = judge(text)[S7]
    # 不(0)推(1)荐(2)澜(3)序(4) (5)S(6)7(7)
    assert result.spans == [("negation", 0, 3), ("mention", 3, 8)]


@pytest.mark.parametrize(
    "text",
    [
        "不推荐岑野 K5，推荐澜序 S7。",
        "不推荐岑野 K5；澜序 S7 值得考虑。",
        "不推荐岑野 K5，但澜序 S7 值得考虑。",
        "不推荐岑野 K5 而推荐澜序 S7。",
        "岑野 K5 不值得买，不过澜序 S7 值得买。",
        "不推荐岑野 K5。\n推荐澜序 S7。",
    ],
)
def test_negation_scope_ends_at_the_clause(text: str) -> None:
    assert verdict(text, K5) == NEGATED
    assert verdict(text, S7) == RECOMMENDED


def test_negated_product_negates_its_brand() -> None:
    assert verdict("不推荐澜序 S7。", LANXU) == NEGATED


def test_recommendation_and_negation_of_one_entity_is_conservative() -> None:
    text = "不推荐澜序 S7，推荐澜序 X5。"
    assert verdict(text, S7) == NEGATED
    assert verdict(text, X5) == RECOMMENDED
    brand = judge(text)[LANXU]
    assert (brand.mentioned, brand.recommended, brand.negated) == NEGATED
    assert "conflict" in brand.codes
    assert brand.note is not None
    assert "既有推荐也有否定" in brand.note


@pytest.mark.parametrize(
    "text",
    [
        "不是不推荐澜序 S7。",
        "并非不值得买澜序 S7。",
        "没有不推荐澜序 S7 的理由。",
        "澜序 S7 未必值得买。",
        "澜序 S7 不一定值得推荐。",
    ],
)
def test_double_negation_and_hedges_are_uncertain(text: str) -> None:
    result = judge(text)[S7]
    assert verdict(text, S7) == UNSURE
    assert "uncertain_cue" in result.codes
    assert result.note is not None


def test_mixed_cues_in_one_clause_are_uncertain() -> None:
    text = "推荐澜序 S7 不推荐岑野 K5"
    assert verdict(text, S7) == UNSURE
    assert verdict(text, K5) == UNSURE
    assert "mixed_cues" in judge(text)[S7].codes


# ------------------------------------------------------------------ 推荐


@pytest.mark.parametrize(
    "text",
    [
        "推荐澜序 S7。",
        "建议选择澜序 S7。",
        "澜序 S7 值得考虑。",
        "首选澜序 S7。",
        "优先考虑澜序 S7。",
        "可以考虑澜序 S7。",
        "澜序 S7 值得买。",
        "澜序 S7 是不错的选择。",
        "强烈推荐澜序 S7。",
        "不得不推荐澜序 S7。",
        "我个人更推荐澜序 S7。",
        "也都还是真的很非常推荐澜序 S7。",
    ],
)
def test_recommendation_expressions(text: str) -> None:
    assert verdict(text, S7) == RECOMMENDED
    assert verdict(text, LANXU) == RECOMMENDED
    assert "recommend_cue" in judge(text)[S7].codes


def test_recommendation_applies_to_every_entity_in_an_enumeration() -> None:
    text = "推荐：澜序 S7、澜序 X5。"
    assert verdict(text, S7) == RECOMMENDED
    assert verdict(text, X5) == RECOMMENDED


@pytest.mark.parametrize(
    "text",
    [
        "澜序 S7 的续航是 620 km。",
        "很多人问澜序 S7 怎么样。",
        "澜序 S7 于 2026 年 3 月上市。",
    ],
)
def test_plain_mention_is_not_a_recommendation(text: str) -> None:
    assert verdict(text, S7) == MENTION_ONLY


def test_recommendation_span_is_the_cue() -> None:
    text = "澜序 S7 续航 620 km，值得考虑。"
    result = judge(text)[S7]
    assert verdict(text, S7) == RECOMMENDED
    assert span_text(text, result, "recommendation") == ["值得考虑"]
    assert span_text(text, result, "mention") == ["澜序 S7"]
    assert "recommend_carry" in result.codes


def test_carry_stays_inside_the_sentence() -> None:
    assert verdict("澜序 S7 续航 620 km。值得考虑。", S7) == MENTION_ONLY


def test_carry_needs_a_single_subject() -> None:
    text = "澜序 S7 和岑野 K5 续航都不错，值得考虑。"
    assert verdict(text, S7) == MENTION_ONLY
    assert verdict(text, K5) == MENTION_ONLY


def test_carry_of_a_negation_without_an_explicit_object() -> None:
    text = "澜序 S7 续航长，但不推荐新手购买。"
    assert verdict(text, S7) == NEGATED
    assert "negation_carry" in judge(text)[S7].codes


def test_carry_does_not_override_an_explicit_cue() -> None:
    # 第二个分句的“值得考虑”没有对象，前一分句已经有自己的推荐语（否定），不承接
    assert verdict("不推荐岑野 K5，值得考虑。", K5) == NEGATED
    assert "conflict" not in judge("不推荐岑野 K5，值得考虑。")[K5].codes


# ------------------------------------------------------------------ 比较对象


@pytest.mark.parametrize(
    ("text", "winner", "other"),
    [
        ("澜序 S7 比岑野 K5 更值得买。", S7, K5),
        ("相比岑野 K5，更推荐澜序 S7。", S7, K5),
        ("与岑野 K5 相比，澜序 S7 更值得考虑。", S7, K5),
        ("岑野 K5 不如澜序 S7 值得买。", S7, K5),
        ("推荐澜序 S7 而不是岑野 K5。", S7, K5),
        ("澜序 S7 续航长，比岑野 K5 更值得买。", S7, K5),
        ("除了澜序 S7 之外，岑野 K5 也值得考虑。", K5, S7),
    ],
)
def test_comparison_object_is_not_recommended(text: str, winner: str, other: str) -> None:
    assert verdict(text, winner) == RECOMMENDED
    assert verdict(text, other) == MENTION_ONLY
    assert "comparison_object" in judge(text)[other].codes


def test_comparison_object_does_not_receive_a_carried_cue() -> None:
    assert verdict("除了岑野 K5，其他都推荐。", K5) == MENTION_ONLY


# ------------------------------------------------------------------ 倾向与风险


@pytest.mark.parametrize(
    ("text", "entity_id", "sentiment", "unfounded"),
    [
        ("澜序 S7 续航出色。", S7, "positive", False),
        ("岑野 K5 故障较多。", K5, "negative", None),
        ("澜序 S7 的续航是 620 km。", S7, "neutral", False),
        ("澜序 S7 续航出色，但召回过一次。", S7, None, None),
        ("澜序 S7 不够可靠。", S7, "negative", None),
        ("澜序 S7 没有明显缺点。", S7, "neutral", False),
        ("不推荐岑野 K5。", K5, "negative", None),
        ("推荐澜序 S7。", S7, "positive", False),
        ("岑野 K5 续航很长。", S7, None, False),
    ],
)
def test_sentiment_and_unfounded_negative(
    text: str, entity_id: str, sentiment: str | None, unfounded: bool | None
) -> None:
    result = judge(text)[entity_id]
    assert result.sentiment == sentiment
    assert result.unfounded_negative is unfounded


def test_sentiment_of_the_comparison_object_is_not_judged() -> None:
    # “比 K5 出色”说的是 S7 出色，K5 只是比较对象
    assert judge("澜序 S7 比岑野 K5 出色。")[K5].sentiment == "neutral"
    assert judge("澜序 S7 比岑野 K5 出色。")[S7].sentiment == "positive"


def test_mixed_sentiment_is_flagged() -> None:
    assert "sentiment_mixed" in judge("澜序 S7 续航出色，但召回过一次。")[S7].codes


def test_width_and_case_variants_give_the_same_conclusions() -> None:
    plain = judge("不推荐 Lan-Xu S7，推荐 LANXU X5。")
    wide = judge("不推荐 Ｌａｎ－Ｘｕ Ｓ７，推荐 ｌａｎｘｕ Ｘ５。")
    for entity_id in (S7, X5, LANXU):
        assert (plain[entity_id].mentioned, plain[entity_id].recommended) == (
            wide[entity_id].mentioned,
            wide[entity_id].recommended,
        )
        assert plain[entity_id].negated == wide[entity_id].negated
    assert verdict("不推荐 Ｌａｎ－Ｘｕ Ｓ７，推荐 ｌａｎｘｕ Ｘ５。", S7) == NEGATED
