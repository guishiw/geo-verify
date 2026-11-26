"""分句与全半角折叠：否定和推荐的作用范围以分句为单位。期望的切分由人工给出。"""

from __future__ import annotations

import pytest
from helpers_annotate import fullwidth
from hypothesis import given
from hypothesis import strategies as st

from geo_verify.annotate.segment import fold_width, sentence_end, split_clauses, split_lines


def pieces(text: str) -> list[str]:
    """按折叠文本切分，再用同样的位置截原文（折叠不改变位置）。"""
    return [text[clause.start : clause.end] for clause in split_clauses(fold_width(text))]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("不推荐 A，推荐 B。", ["不推荐 A", "推荐 B"]),
        ("A 很好；B 一般", ["A 很好", "B 一般"]),
        ("A 很好\nB 一般", ["A 很好", "B 一般"]),
        ("A 续航长但价格高", ["A 续航长", "但价格高"]),
        ("A 续航长，但是价格高", ["A 续航长", "但是价格高"]),
        ("A 续航长不过价格高", ["A 续航长", "不过价格高"]),
        ("A 续航长而 B 价格低", ["A 续航长", "而 B 价格低"]),
        ("A 续航长然而价格高", ["A 续航长", "然而价格高"]),
        ("A 续航长可是价格高", ["A 续航长", "可是价格高"]),
        ("推荐：A、B", ["推荐：A、B"]),
        ("售价 21.98 万元，约 1,000 公里", ["售价 21.98 万元", "约 1,000 公里"]),
        ("Great car. Buy it!", ["Great car", "Buy it"]),
    ],
)
def test_clause_boundaries(text: str, expected: list[str]) -> None:
    assert pieces(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "A 不但续航长，而且价格低",
        "对家庭而言 A 更合适",
        "A 只是便宜而已",
        "A 因而更受欢迎",
        "A 从而省下油费",
        "A 反而更贵",
        "A 比不过 B",
        "但凡预算够就选 A",
    ],
)
def test_words_that_look_like_transitions_do_not_split(text: str) -> None:
    folded = fold_width(text)
    joined = "".join(folded[c.start : c.end] for c in split_clauses(folded))
    # 只有逗号可以切分，“不但 / 而且 / 而言 / 而已 / 因而 / 从而 / 反而 / 比不过 / 但凡”都不是转折
    assert joined == folded.replace("，", "").replace(",", "")
    assert len(split_clauses(folded)) == text.count("，") + 1


def test_questions_are_marked() -> None:
    folded = fold_width("A 值得买吗，B 呢？C 很好。")
    flags = [clause.question for clause in split_clauses(folded)]
    assert flags == [True, True, False]


def test_sentence_numbers_follow_sentence_ends() -> None:
    folded = fold_width("A，B。C\nD")
    assert [clause.sentence for clause in split_clauses(folded)] == [0, 0, 1, 2]
    assert sentence_end(folded, 0) == 3
    assert sentence_end(folded, 4) == 5
    assert sentence_end(folded, 6) == len(folded)


def test_lines_and_indents() -> None:
    lines = split_lines("a\n  b\n\n\tc")
    assert [(line.start, line.end, line.indent, line.blank) for line in lines] == [
        (0, 1, 0, False),
        (2, 5, 2, False),
        (6, 6, 0, True),
        (7, 9, 4, False),
    ]


@given(st.text(max_size=60))
def test_fold_width_keeps_length_and_undoes_fullwidth(text: str) -> None:
    assert len(fold_width(text)) == len(text)
    assert fold_width(fullwidth(text)) == fold_width(text)
    for clause in split_clauses(fold_width(text)):
        assert 0 <= clause.start < clause.end <= len(text)
