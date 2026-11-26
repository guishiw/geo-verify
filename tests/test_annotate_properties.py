"""规则标注的随机输入性质（hypothesis）。

- 任意文本下每个 span 都在原文范围内，提及 span 截出的原文规范化后正是该实体（或其下级产品）的别名；
- 由判断生成的标注都符合 annotation 契约，且满足契约之外的一致性（未提及不推荐、否定不推荐……）；
- 对输入做全半角变换后，提及、推荐、否定、位次、倾向结论与 span 都不变；
- 在不含推荐语的片段前加“不推荐”，该实体必为否定提及。
"""

from __future__ import annotations

from functools import cache

from helpers_annotate import NOW, auto_annotator, fullwidth
from helpers_basic import auto_entities
from hypothesis import given, settings
from hypothesis import strategies as st

from geo_verify import spec
from geo_verify.annotate.matching import EntityMatcher, normalize_key
from geo_verify.annotate.rules import Judgment
from geo_verify.annotate.run_rules import build_annotation

SURFACES = [
    "澜序",
    "澜序汽车",
    "LANXU",
    "Lan-Xu",
    "澜序 S7",
    "S7",
    "澜序 X5",
    "X5",
    "岑野",
    "岑野 S7",
    "岑野 K5",
    "K5",
    "朔风",
    "朔风 T6",
    "T6",
    "波澜序曲",
]
CUES = [
    "推荐",
    "不推荐",
    "值得考虑",
    "不建议选择",
    "不是不推荐",
    "未必值得买",
    "首选",
    "其次",
    "再次",
    "别买",
    "比",
    "相比",
    "不如",
    "除了",
    "而不是",
    "推荐顺序：",
    " > ",
    "要不要",
]
PUNCTUATION = ["，", "。", "；", "\n", "\n\n", "、", "：", "但", "而", "？", ",", ".", "!"]
MARKERS = [
    "1. ",
    "2. ",
    "3. ",
    "1、",
    "2、",
    "①",
    "②",
    "（1）",
    "（2）",
    "第一名：",
    "第二名：",
    "### 1. ",
    "- ",
]
FILLERS = [
    "续航长",
    "价格低",
    "故障多",
    "出色",
    "的",
    " ",
    "车型",
    "21.98 万元",
    "5.9 秒",
    "可靠",
    "Pro",
]
FRAGMENT = st.sampled_from(SURFACES + CUES + PUNCTUATION + MARKERS + FILLERS)
ANSWER = st.lists(FRAGMENT, max_size=24).map("".join)


@cache
def alias_keys() -> dict[str, set[str]]:
    """每个实体可被计为提及的别名键：自己和下级产品的名称与别名。"""
    entities = auto_entities()
    matcher = EntityMatcher(entities)
    keys: dict[str, set[str]] = {entity["entity_id"]: set() for entity in entities}
    for entity in entities:
        own = {normalize_key(text) for text in [entity["name"], *entity["aliases"]]}
        for target in (entity["entity_id"], *matcher.ancestors(entity["entity_id"])):
            keys[target] |= own
    return keys


def conclusions(result: Judgment) -> tuple[object, ...]:
    return (
        result.mentioned,
        result.recommended,
        result.negated,
        result.rankable,
        result.rank,
        result.rank_list_size,
        result.sentiment,
        result.unfounded_negative,
        tuple(result.spans),
    )


def as_annotation(result: Judgment) -> dict[str, object]:
    observation = {"observation_id": "obs-000001", "project_id": "auto-demo"}
    return build_annotation(observation, result, created_at=NOW, synthetic=True)


def check_judgment(text: str, result: Judgment) -> None:
    record = as_annotation(result)
    assert spec.iter_issues("annotation", record) == []
    for kind, start, end in result.spans:
        assert 0 <= start < end <= len(text), (kind, start, end)
        if kind == "mention":
            assert normalize_key(text[start:end]) in alias_keys()[result.entity_id]
    if not result.mentioned:
        assert (result.recommended, result.negated, result.rank, result.sentiment) == (
            False,
            False,
            None,
            None,
        )
        assert not [span for span in result.spans if span[0] == "mention"]
    if result.negated:
        assert result.recommended is False
    if not result.rankable:
        assert result.rank is None
        assert result.rank_list_size is None
    if result.rank is not None:
        assert result.rank_list_size is not None
        assert 1 <= result.rank <= result.rank_list_size


@settings(max_examples=300, deadline=None)
@given(ANSWER)
def test_generated_answers_give_consistent_valid_annotations(text: str) -> None:
    for result in auto_annotator().judge(text).values():
        check_judgment(text, result)


@settings(max_examples=150, deadline=None)
@given(st.text(max_size=80))
def test_arbitrary_text_gives_valid_annotations(text: str) -> None:
    for result in auto_annotator().judge(text).values():
        check_judgment(text, result)


@settings(max_examples=300, deadline=None)
@given(ANSWER)
def test_width_transform_keeps_every_conclusion(text: str) -> None:
    plain = auto_annotator().judge(text)
    wide = auto_annotator().judge(fullwidth(text))
    for entity_id, result in plain.items():
        assert conclusions(result) == conclusions(wide[entity_id])


SAFE_FILLER = st.lists(
    st.sampled_from(["续航长", "价格低", "车型", "的", " ", "21.98 万元"]), max_size=4
)
PRODUCTS = st.sampled_from(
    [
        ("澜序 S7", "product-lanxu-s7"),
        ("澜序 X5", "product-lanxu-x5"),
        ("岑野 K5", "product-cenye-k5"),
        ("岑野 S7", "product-cenye-s7"),
        ("朔风 T6", "product-shuofeng-t6"),
    ]
)


@given(
    PRODUCTS, SAFE_FILLER, st.sampled_from(["不推荐", "不建议选择", "建议避开", "别买", "不要选"])
)
def test_prefixing_a_negation_negates_the_mention(
    product: tuple[str, str], filler: list[str], negation: str
) -> None:
    surface, entity_id = product
    # 车型名后先接空格：紧跟数字（“S721.98”）按词边界本来就不算提及
    text = negation + surface + " " + "".join(filler) + "。"
    result = auto_annotator().judge(text)[entity_id]
    assert (result.mentioned, result.recommended, result.negated) == (True, False, True)


@given(
    PRODUCTS, SAFE_FILLER, st.sampled_from(["推荐", "建议选择", "值得考虑的是", "首选", "可以考虑"])
)
def test_prefixing_a_recommendation_recommends_the_mention(
    product: tuple[str, str], filler: list[str], cue: str
) -> None:
    surface, entity_id = product
    text = cue + surface + " " + "".join(filler) + "。"
    result = auto_annotator().judge(text)[entity_id]
    assert (result.mentioned, result.recommended, result.negated) == (True, True, False)
