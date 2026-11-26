"""别名规范化与匹配：全半角、大小写、连字符、空格、最长优先、排除词、同形异义。

期望的 span 全部由手工数原文字符位置得到。
"""

from __future__ import annotations

import pytest
from helpers_basic import auto_entities, entity
from hypothesis import assume, given
from hypothesis import strategies as st

from geo_verify.annotate.matching import EntityMatcher, normalize, normalize_key

FULLWIDTH_OFFSET = 0xFEE0
HYPHEN = chr(0x2010)  # Unicode 连字符
EN_DASH = chr(0x2013)
ZERO_WIDTH_SPACE = chr(0x200B)
IDEOGRAPHIC_SPACE = chr(0x3000)


def fullwidth(text: str) -> str:
    """把 ASCII 可见字符换成全角形式。"""
    return "".join(chr(ord(ch) + FULLWIDTH_OFFSET) if "!" <= ch <= "~" else ch for ch in text)


def spans(matcher: EntityMatcher, text: str) -> list[tuple[str, int, int]]:
    return [(item.entity_id, item.start, item.end) for item in matcher.find(text).mentions]


@pytest.fixture(scope="module")
def matcher() -> EntityMatcher:
    return EntityMatcher(auto_entities())


# ------------------------------------------------------------------ 规范化


def test_normalize_key_folds_width_case_hyphen_and_space() -> None:
    assert normalize_key("Lan-Xu") == "lanxu"
    assert normalize_key("LAN XU") == "lanxu"
    assert normalize_key(fullwidth("LanXu")) == "lanxu"
    assert normalize_key("Lan" + HYPHEN + "Xu") == "lanxu"
    assert normalize_key("lan" + EN_DASH + "xu") == "lanxu"
    assert normalize_key("澜序" + IDEOGRAPHIC_SPACE + "Ｓ７") == "澜序s7"
    assert normalize_key("S" + ZERO_WIDTH_SPACE + "7") == "s7"
    assert normalize_key(" - ") == ""


def test_normalize_keeps_original_positions() -> None:
    text = "买 Ｓ－７ 吧"
    normalized = normalize(text)
    assert normalized.text == "买s7吧"
    # 买(0) 空格(1) Ｓ(2) －(3) ７(4) 空格(5) 吧(6)
    assert normalized.starts == (0, 2, 4, 6)
    assert normalized.ends == (1, 3, 5, 7)


def test_normalize_groups_combining_marks_with_their_base() -> None:
    decomposed = "e" + chr(0x0301)  # e + 组合用锐音符
    assert normalize(decomposed).text == chr(0xE9)
    assert normalize(decomposed).starts == (0,)
    assert normalize(decomposed).ends == (2,)


@given(st.text(max_size=40), st.text(max_size=40))
def test_normalize_is_a_homomorphism_over_concatenation(left: str, right: str) -> None:
    import unicodedata

    # 右半段以组合符号开头时会和左半段的末字符合并，不在本性质范围内
    assume(not right or not unicodedata.category(right[0]).startswith("M"))
    assert normalize_key(left + right) == normalize_key(left) + normalize_key(right)


ALIAS_ALPHABET = st.sampled_from(list("abcxyzKS057澜序岑野朔风"))
SEPARATORS = st.sampled_from(
    ["", " ", "-", HYPHEN, EN_DASH, IDEOGRAPHIC_SPACE, ZERO_WIDTH_SPACE, " - "]
)


@st.composite
def decorated_alias(draw: st.DrawFn) -> tuple[str, str]:
    """返回 (别名, 加了空格、连字符、大小写和全角变化的写法)。"""
    alias = "".join(draw(st.lists(ALIAS_ALPHABET, min_size=1, max_size=6)))
    pieces: list[str] = []
    for index, ch in enumerate(alias):
        if index:
            pieces.append(draw(SEPARATORS))
        variant = draw(st.sampled_from(["same", "upper", "lower", "full"]))
        if variant == "upper":
            ch = ch.upper()
        elif variant == "lower":
            ch = ch.lower()
        elif variant == "full":
            ch = fullwidth(ch)
        pieces.append(ch)
    return alias, "".join(pieces)


@given(decorated_alias())
def test_decorated_alias_has_the_same_key(pair: tuple[str, str]) -> None:
    alias, written = pair
    assert normalize_key(written) == normalize_key(alias)


@given(
    decorated_alias(), st.sampled_from(["", "推荐", "听说"]), st.sampled_from(["", "。", "不错"])
)
def test_decorated_alias_is_found_with_exact_original_span(
    pair: tuple[str, str], prefix: str, suffix: str
) -> None:
    alias, written = pair
    found = EntityMatcher([entity("e-1", alias, kind="brand")]).find(prefix + written + suffix)
    assert [(item.entity_id, item.start, item.end) for item in found.mentions] == [
        ("e-1", len(prefix), len(prefix) + len(written))
    ]


# ------------------------------------------------------------------ 匹配


@pytest.mark.parametrize(
    "written",
    ["Lan-Xu", "Lan Xu", "LanXu", "lanxu", "LANXU", fullwidth("LANXU"), "澜序", "澜序汽车"],
)
def test_brand_alias_variants(matcher: EntityMatcher, written: str) -> None:
    assert spans(matcher, written) == [("brand-lanxu", 0, len(written))]


@pytest.mark.parametrize("written", ["S7", "S 7", "S-7", "s7", fullwidth("S7"), "Ｓ－７"])
def test_product_alias_variants(matcher: EntityMatcher, written: str) -> None:
    text = "推荐" + written + "这款车"
    assert spans(matcher, text) == [("product-lanxu-s7", 2, 2 + len(written))]


@pytest.mark.parametrize(
    "text", ["XS70 很省电", "S70 很省电", "XS7 很省电", "S7Pro 很省电", "型号 0S7"]
)
def test_alphanumeric_alias_needs_word_boundary(matcher: EntityMatcher, text: str) -> None:
    assert spans(matcher, text) == []


def test_word_boundary_also_applies_to_mixed_alias_ends(matcher: EntityMatcher) -> None:
    # “澜序 S70”不是“澜序 S7”；其中的“澜序”仍是品牌提及
    assert spans(matcher, "澜序 S70 是另一款车") == [("brand-lanxu", 0, 2)]
    assert spans(matcher, "看看澜序S7。") == [("product-lanxu-s7", 2, 6)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("①LANXU：表现均衡", [("brand-lanxu", 1, 6)]),
        ("②CENYE S7：值得考虑", [("product-cenye-s7", 1, 9)]),
        ("③lanxu S7：续航稳定", [("brand-lanxu", 1, 6), ("product-lanxu-s7", 7, 9)]),
        ("④S7：性价比高", [("product-lanxu-s7", 1, 3)]),
        ("⑴Lan-Xu 很省电", [("brand-lanxu", 1, 7)]),
    ],
)
def test_circled_list_markers_do_not_block_alphanumeric_word_boundary(
    matcher: EntityMatcher, text: str, expected: list[tuple[str, int, int]]
) -> None:
    # 圆圈序号 ① 等 Unicode No 类符号不是词内数字，紧接西文别名时不应阻断词边界
    assert spans(matcher, text) == expected


SHARP_S = chr(0xDF)  # ß，casefold 后展开为两个字符 "ss"


@pytest.mark.parametrize("text", [f"{SHARP_S}7 很省电", f"推荐{SHARP_S}7", f"X{SHARP_S}7"])
def test_alias_cannot_start_inside_a_character_that_folds_to_several(
    matcher: EntityMatcher, text: str
) -> None:
    # 原文“ß7”规范化为“ss7”：别名“s7”只覆盖了“ß”展开后的后半个字符，原文里并没有 S7
    assert spans(matcher, text) == []


def test_alias_cannot_end_inside_a_character_that_folds_to_several() -> None:
    # 原文“Aß”规范化为“ass”：别名“AS”只盖住“ß”的前半个字符，不算命中
    custom = EntityMatcher([entity("brand-as", "AS", aliases=("AS",), kind="brand")])
    assert spans(custom, f"A{SHARP_S} 很好") == []
    assert spans(custom, "AS 很好") == [("brand-as", 0, 2)]


def test_character_that_folds_to_several_still_matches_when_whole() -> None:
    # 别名本身含 ß 时，整个字符参与匹配仍然命中，span 指回原文的整个字符
    custom = EntityMatcher([entity("brand-gross", "Groß", aliases=("Groß",), kind="brand")])
    assert spans(custom, "选 GROSS 还是 Groß？") == [
        ("brand-gross", 2, 7),
        ("brand-gross", 11, 15),
    ]


@pytest.mark.parametrize("text", ["1S7 很省电", "１S7 很省电", "2LANXU 很省电"])
def test_decimal_digits_still_enforce_word_boundary(matcher: EntityMatcher, text: str) -> None:
    assert spans(matcher, text) == []


def test_longest_match_wins_across_entities(matcher: EntityMatcher) -> None:
    # “岑野 S7”被竞品车型整体吃掉，其中的“S7”不再算目标车型，“岑野”也不再单独算一次
    assert spans(matcher, "岑野 S7 也不错") == [("product-cenye-s7", 0, 5)]
    assert spans(matcher, "岑野S7和澜序S7") == [
        ("product-cenye-s7", 0, 4),
        ("product-lanxu-s7", 5, 9),
    ]


def test_matches_do_not_overlap(matcher: EntityMatcher) -> None:
    result = matcher.find("澜序汽车的澜序 S7、岑野汽车的 K5、朔风T6")
    assert [(m.entity_id, m.start, m.end) for m in result.mentions] == [
        ("brand-lanxu", 0, 4),
        ("product-lanxu-s7", 5, 10),
        ("brand-cenye", 11, 15),
        ("product-cenye-k5", 17, 19),
        ("product-shuofeng-t6", 20, 24),
    ]
    for first, second in zip(result.mentions, result.mentions[1:], strict=False):
        assert first.end <= second.start


def test_exclusion_phrase_blocks_only_the_covered_alias(matcher: EntityMatcher) -> None:
    assert spans(matcher, "波澜序曲是一首钢琴曲") == []
    # 波(0)澜(1)序(2)曲(3)和(4)澜(5)序(6)汽(7)车(8)
    assert spans(matcher, "波澜序曲和澜序汽车无关") == [("brand-lanxu", 5, 9)]
    assert spans(matcher, "波澜 序曲") == []


def test_exclusion_belongs_to_one_entity_only() -> None:
    entities = [
        entity("brand-a", "序曲", kind="brand"),
        entity("brand-b", "澜序", kind="brand", exclusions=("波澜序曲",), role="competitor"),
    ]
    # “波澜序曲”只是 brand-b 的排除词；brand-a 的“序曲”照常命中，位置 2～4
    assert spans(EntityMatcher(entities), "波澜序曲") == [("brand-a", 2, 4)]


def test_product_after_another_brand_is_not_attributed(matcher: EntityMatcher) -> None:
    # “岑野的 S7”说的是岑野的车，不能算成澜序 S7
    assert spans(matcher, "岑野的 S7 也可以看看") == [("brand-cenye", 0, 2)]
    assert spans(matcher, "岑野旗下的S7") == [("brand-cenye", 0, 2)]
    # 同品牌前缀不受影响：澜序(0-2) 的(2) S7(3-5)
    assert spans(matcher, "澜序的S7") == [("brand-lanxu", 0, 2), ("product-lanxu-s7", 3, 5)]
    # 隔了别的词就不再归属给前一个品牌
    assert spans(matcher, "岑野不如 S7") == [("brand-cenye", 0, 2), ("product-lanxu-s7", 5, 7)]


def test_same_alias_shared_by_two_entities_is_resolved_by_brand_context() -> None:
    entities = [
        entity("brand-a", "甲牌", kind="brand"),
        entity("prod-a-k5", "甲牌 K5", aliases=("K5",), parent="brand-a"),
        entity("brand-b", "乙牌", kind="brand", role="competitor"),
        entity("prod-b-k5", "乙牌 K5", aliases=("K5",), parent="brand-b", role="competitor"),
    ]
    shared = EntityMatcher(entities)
    # 没有上下文：同形异义无法判断，不计入任何一方，但要如实列出
    result = shared.find("K5 值得考虑")
    assert result.mentions == []
    assert [(item.start, item.end, item.entity_ids) for item in result.ambiguous] == [
        (0, 2, ("prod-a-k5", "prod-b-k5"))
    ]
    # 同一句里前文出现过乙牌：归乙牌的 K5。乙(0)牌(1)口(2)碑(3)好(4)，(5)K5(6-8)
    result = shared.find("乙牌口碑好，K5 值得考虑")
    assert [(m.entity_id, m.start, m.end) for m in result.mentions] == [
        ("brand-b", 0, 2),
        ("prod-b-k5", 6, 8),
    ]
    assert result.ambiguous == []
    # 换了一句话，上下文不再延续
    result = shared.find("乙牌口碑好。K5 值得考虑")
    assert [m.entity_id for m in result.mentions] == ["brand-b"]
    assert len(result.ambiguous) == 1


def test_matcher_ignores_blank_aliases_and_reports_roots() -> None:
    entities = auto_entities()
    entities[0]["aliases"].append(" - ")
    built = EntityMatcher(entities)
    assert spans(built, " - ") == []
    assert built.root_of("product-lanxu-s7") == "brand-lanxu"
    assert built.root_of("brand-lanxu") == "brand-lanxu"
    assert built.ancestors("product-lanxu-s7") == ("brand-lanxu",)
    assert built.ancestors("brand-cenye") == ()


def test_parent_cycle_does_not_hang() -> None:
    entities = [
        entity("a", "甲", parent="b"),
        entity("b", "乙", parent="a"),
    ]
    built = EntityMatcher(entities)
    assert built.ancestors("a") == ("b",)
    assert built.root_of("a") == "b"
