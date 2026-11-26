"""推荐序列与位次：只在可明确识别的推荐序列中给位次，不得用出现先后代替。

期望值都是人工阅读例句后给出的判断。
"""

from __future__ import annotations

import pytest
from helpers_annotate import judge, ranking, span_text, verdict
from helpers_basic import entity

from geo_verify.annotate.rules import RuleAnnotator
from geo_verify.annotate.sequence import parse_number

S7 = "product-lanxu-s7"
X5 = "product-lanxu-x5"
LANXU = "brand-lanxu"
K5 = "product-cenye-k5"
CENYE_S7 = "product-cenye-s7"
CENYE = "brand-cenye"
T6 = "product-shuofeng-t6"
SHUOFENG = "brand-shuofeng"
NOT_RANKABLE = (False, None, None)

BASIC_LIST = "推荐以下车型：\n1. 澜序 S7：续航长\n2. 岑野 K5：价格低\n3. 朔风 T6：空间大"


def test_numbered_list_gives_ranks_and_list_size() -> None:
    assert ranking(BASIC_LIST, S7) == (True, 1, 3)
    assert ranking(BASIC_LIST, K5) == (True, 2, 3)
    assert ranking(BASIC_LIST, T6) == (True, 3, 3)
    # 品牌的位次取第一个以它的产品为主体的项
    assert ranking(BASIC_LIST, LANXU) == (True, 1, 3)
    assert ranking(BASIC_LIST, CENYE) == (True, 2, 3)
    assert ranking(BASIC_LIST, SHUOFENG) == (True, 3, 3)
    # 序列存在但实体不在其中
    assert ranking(BASIC_LIST, X5) == (True, None, 3)
    assert ranking(BASIC_LIST, CENYE_S7) == (True, None, 3)
    assert judge(BASIC_LIST)[X5].mentioned is False


def test_entities_in_a_recommendation_sequence_are_recommended() -> None:
    assert verdict(BASIC_LIST, K5) == (True, True, False)
    result = judge(BASIC_LIST)[S7]
    # 位次的 span 从编号开始，到该项结束
    assert span_text(BASIC_LIST, result, "rank") == ["1. 澜序 S7：续航长"]
    assert "recommend_sequence" in result.codes
    assert "rank_in_sequence" in result.codes
    assert "rank_not_in_sequence" in judge(BASIC_LIST)[X5].codes


@pytest.mark.parametrize(
    "text",
    [
        "推荐：\n1、澜序 S7\n2、岑野 K5",
        "推荐：\n1）澜序 S7\n2）岑野 K5",
        "推荐：\n1) 澜序 S7\n2) 岑野 K5",
        "推荐：\n（1）澜序 S7\n（2）岑野 K5",
        "推荐：\n①澜序 S7\n②岑野 K5",
        "推荐：①澜序 S7；②岑野 K5。",
        "推荐：(1) 澜序 S7；(2) 岑野 K5。",
        "推荐：1、澜序 S7；2、岑野 K5。",
        "第一名：澜序 S7\n第二名：岑野 K5",
        "第一名是澜序 S7，第二名是岑野 K5。",
        "推荐：\n１．澜序 S7\n２．岑野 K5",
        "### 1. **澜序 S7**\n- 续航 620 km\n- 售价 21.98 万元\n\n### 2. **岑野 K5**\n- 价格更低",
        "推荐：\n\n1. 澜序 S7：续航长\n\n2. 岑野 K5：价格低\n\n总结：澜序 S7 更均衡。",
        "**1.** 澜序 S7\n**2.** 岑野 K5",
    ],
)
def test_marker_styles(text: str) -> None:
    assert ranking(text, S7) == (True, 1, 2)
    assert ranking(text, K5) == (True, 2, 2)


def test_ordering_words_form_a_sequence() -> None:
    text = "首选澜序 S7，其次岑野 K5，再次朔风 T6。"
    assert ranking(text, S7) == (True, 1, 3)
    assert ranking(text, K5) == (True, 2, 3)
    assert ranking(text, T6) == (True, 3, 3)
    assert verdict(text, K5) == (True, True, False)


def test_recommendation_order_chain() -> None:
    text = "推荐顺序：澜序 S7 > 岑野 K5 > 朔风 T6。"
    assert ranking(text, S7) == (True, 1, 3)
    assert ranking(text, K5) == (True, 2, 3)
    assert ranking(text, T6) == (True, 3, 3)
    assert ranking("推荐顺序：澜序 S7 ＞ 岑野 K5", K5) == (True, 2, 2)


def test_list_and_ordering_words_describing_the_same_sequence() -> None:
    text = "1. 首选：澜序 S7\n2. 其次：岑野 K5"
    assert ranking(text, S7) == (True, 1, 2)
    assert ranking(text, K5) == (True, 2, 2)


@pytest.mark.parametrize(
    "text",
    [
        "澜序 S7 和岑野 K5 都可以考虑。",
        "首选澜序 S7。",
        "其次，岑野 K5 的价格更低。",
        "续航：澜序 S7 > 岑野 K5。",
        "澜序 S7 排名第一。",
        "澜序 S7 售价 21.98 万元，百公里加速 5.9 秒。",
    ],
)
def test_no_identifiable_sequence_means_not_rankable(text: str) -> None:
    # 出现先后不是位次：先出现的 S7 也不给 rank
    assert ranking(text, S7) == NOT_RANKABLE
    assert "rank_no_sequence" in judge(text)[S7].codes


def test_step_list_without_entities_is_not_a_sequence() -> None:
    text = "购车前的步骤：\n1. 确定预算\n2. 试驾对比\n3. 了解保险\n\n澜序 S7 值得考虑。"
    assert ranking(text, S7) == NOT_RANKABLE
    assert verdict(text, S7) == (True, True, False)


def test_parameter_list_without_entities_is_not_a_sequence() -> None:
    text = "澜序 S7 的参数：\n1. 续航 620 km\n2. 售价 21.98 万元\n3. 百公里加速 5.9 秒"
    assert ranking(text, S7) == NOT_RANKABLE
    assert verdict(text, S7) == (True, False, False)


def test_step_list_next_to_a_recommendation_list() -> None:
    text = "购车步骤：\n1. 确定预算\n2. 试驾\n\n推荐车型：\n1. 澜序 S7\n2. 岑野 K5"
    assert ranking(text, S7) == (True, 1, 2)
    assert ranking(text, K5) == (True, 2, 2)


def test_entity_in_item_body_is_not_ranked() -> None:
    text = "1. 澜序 S7：续航比岑野 K5 长\n2. 朔风 T6：价格更低"
    assert ranking(text, S7) == (True, 1, 2)
    assert ranking(text, T6) == (True, 2, 2)
    assert ranking(text, K5) == (True, None, 2)
    assert verdict(text, K5) == (True, False, False)


def test_entity_after_the_list_is_not_ranked() -> None:
    text = "推荐：\n1. 澜序 S7\n2. 岑野 K5\n\n另外，朔风 T6 空间更大。"
    assert ranking(text, T6) == (True, None, 2)
    lazy = "推荐：\n1. 澜序 S7\n2. 岑野 K5\n朔风 T6 也可以了解一下。"
    assert ranking(lazy, K5) == (True, 2, 2)
    assert ranking(lazy, T6) == (True, None, 2)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("1. 澜序 S7 和岑野 K5：都适合家用\n2. 朔风 T6：适合通勤", "rank_unclear_list"),
        ("1. 澜序 S7\n2. 岑野 K5\n4. 朔风 T6", "rank_unclear_list"),
        ("2. 澜序 S7\n3. 岑野 K5", "rank_unclear_list"),
        ("1. 澜序 S7", "rank_unclear_list"),
        ("1. 澜序 S7 长续航版\n2. 澜序 S7 标准版", "rank_unclear_list"),
        ("1. 澜序 S7：推荐\n2. 岑野 K5：不推荐", "rank_unclear_list"),
        (
            "1. 家用：\n   (1) 澜序 S7\n   (2) 岑野 K5\n2. 通勤：\n   (1) 朔风 T6",
            "rank_unclear_list",
        ),
        (
            "家用推荐：\n1. 澜序 S7\n2. 岑野 K5\n\n通勤推荐：\n1. 朔风 T6\n2. 澜序 X5",
            "rank_multiple_sequences",
        ),
        ("首选澜序 S7。其次，价格方面也要看。", "rank_unclear_list"),
        ("推荐：\n0. 澜序 S7\n1. 岑野 K5\n2. 朔风 T6", "rank_unclear_list"),
        ("推荐：①澜序 S7\n另外②岑野 K5", "rank_unclear_list"),
    ],
)
def test_unclear_sequences_are_not_rankable(text: str, reason: str) -> None:
    result = judge(text)[S7]
    assert (result.rankable, result.rank, result.rank_list_size) == NOT_RANKABLE
    assert reason in result.codes
    assert result.note is not None


def test_two_lists_sharing_one_entity_family_are_still_distinct_subjects() -> None:
    text = "推荐：\n1. 澜序 S7\n2. 澜序 X5"
    assert ranking(text, S7) == (True, 1, 2)
    assert ranking(text, X5) == (True, 2, 2)
    assert ranking(text, LANXU) == (True, 1, 2)


def test_negative_list_is_not_a_recommendation_sequence() -> None:
    text = "以下车型不建议购买：\n1. 岑野 K5\n2. 朔风 T6"
    assert verdict(text, K5) == (True, False, True)
    assert verdict(text, T6) == (True, False, True)
    assert ranking(text, K5) == NOT_RANKABLE
    assert "negation_list" in judge(text)[K5].codes
    assert span_text(text, judge(text)[K5], "negation") == ["不建议"]


def test_recommendation_list_next_to_a_negative_list() -> None:
    text = "推荐：\n1. 澜序 S7\n2. 澜序 X5\n\n不推荐：\n1. 岑野 K5\n2. 朔风 T6"
    assert ranking(text, S7) == (True, 1, 2)
    assert ranking(text, X5) == (True, 2, 2)
    assert ranking(text, LANXU) == (True, 1, 2)
    assert ranking(text, K5) == (True, None, 2)
    assert verdict(text, K5) == (True, False, True)
    assert verdict(text, CENYE) == (True, False, True)
    assert verdict(text, S7) == (True, True, False)


def test_negated_item_inside_a_list_keeps_its_clause_verdicts() -> None:
    text = "1. 澜序 S7：推荐\n2. 岑野 K5：不推荐"
    assert verdict(text, S7) == (True, True, False)
    assert verdict(text, K5) == (True, False, True)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1", 1),
        ("12", 12),
        ("一", 1),
        ("九", 9),
        ("十", 10),
        ("十二", 12),
        ("二十", 20),
        ("二十一", 21),
        ("十十", 0),
        ("零", 0),
    ],
)
def test_parse_number(text: str, value: int) -> None:
    assert parse_number(text) == value


def test_item_with_an_unresolvable_homograph_makes_the_list_unclear() -> None:
    entities = [
        entity("brand-a", "甲牌", kind="brand"),
        entity("prod-a-k5", "甲牌 K5", aliases=("K5",), parent="brand-a"),
        entity("prod-a-k7", "甲牌 K7", aliases=("K7",), parent="brand-a"),
        entity("brand-b", "乙牌", kind="brand", role="competitor"),
        entity("prod-b-k5", "乙牌 K5", aliases=("K5",), parent="brand-b", role="competitor"),
    ]
    annotator = RuleAnnotator(entities, ["prod-a-k7", "prod-a-k5"])
    # 第二项的“K5”换了一行，前文品牌不再延续，同形异义无法消解，第二项主体不明
    result = annotator.judge("推荐：\n1. 甲牌 K7\n2. K5")
    assert (result["prod-a-k7"].rankable, result["prod-a-k7"].rank) == (False, None)
    assert "rank_unclear_list" in result["prod-a-k7"].codes
    clear = annotator.judge("推荐：\n1. 甲牌 K7\n2. 甲牌 K5")
    assert (clear["prod-a-k7"].rank, clear["prod-a-k5"].rank) == (1, 2)


def test_lead_line_without_any_clause() -> None:
    assert ranking("，，\n1. 澜序 S7\n2. 岑野 K5", K5) == (True, 2, 2)
