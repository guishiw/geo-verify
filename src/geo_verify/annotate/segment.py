"""回答原文的切分：全半角折叠、分句、按行定位。

规则标注的所有结构判断（分句、推荐语、列表编号）都在“折叠后”的文本上进行：
只把全角 ASCII（U+FF01～U+FF5E）换成半角、把全角空格换成半角空格，逐字符替换、长度不变，
因此折叠后文本中的位置就是原文中的位置，span 可以直接定位回原文。

分句规则（否定和推荐的作用范围以分句为单位）：

- 句末符号 `。！？；` 及换行结束一个分句，同时结束一个“句子”；
- 逗号 `，` 结束一个分句，但不结束句子；
- 半角句点 `.` 和逗号 `,` 夹在两个数字之间时（21.98、1,000）不切分；
- 转折词 `但是 / 但 / 不过 / 然而 / 可是 / 而` 另起一个分句（转折词归入后一分句），
  但“不但、但凡、而且、而言、而已、因而、从而、进而、继而、反而、比不过、信不过”等不是转折；
- 顿号 `、` 和冒号 `：` 不切分（“推荐：A、B”中的推荐作用于 A 和 B）；
- 以问号结尾或以“吗”收尾的分句记为疑问分句，其中的推荐语不生效。
"""

from __future__ import annotations

from dataclasses import dataclass

_WIDTH_TABLE: dict[int, int] = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_WIDTH_TABLE[0x3000] = 0x20

SENTENCE_END = frozenset("。!?;\n")
_TRANSITIONS = ("但是", "然而", "可是", "不过", "但", "而")
_NOT_TRANSITION_AFTER = {"但": ("凡",), "而": ("且", "言", "已")}
_NOT_TRANSITION_BEFORE = {
    "但": ("不",),
    "而": ("因", "从", "进", "继", "反", "然", "故"),
    "不过": ("比", "超", "信", "打", "说", "敌"),
}


def fold_width(text: str) -> str:
    """全角 ASCII 与全角空格换成半角；逐字符替换，长度与位置不变。"""
    return text.translate(_WIDTH_TABLE)


@dataclass(frozen=True)
class Clause:
    """一个分句：折叠文本中的起止位置（左闭右开）、所属句子序号、是否疑问。"""

    start: int
    end: int
    sentence: int
    question: bool


def _between_digits(text: str, index: int) -> bool:
    return 0 < index < len(text) - 1 and text[index - 1].isdigit() and text[index + 1].isdigit()


def transition_at(text: str, index: int) -> int:
    """index 处若是转折词，返回其长度，否则返回 0。"""
    for word in _TRANSITIONS:
        if not text.startswith(word, index):
            continue
        after = text[index + len(word) : index + len(word) + 1]
        before = text[index - 1 : index] if index else ""
        if after and after in _NOT_TRANSITION_AFTER.get(word, ()):
            return 0
        if before and before in _NOT_TRANSITION_BEFORE.get(word, ()):
            return 0
        return len(word)
    return 0


def split_clauses(folded: str) -> list[Clause]:
    """把折叠后的文本切成分句。只含空白的分句被丢弃。"""
    clauses: list[Clause] = []
    sentence = 0
    start = 0

    def emit(stop: int, *, question: bool) -> None:
        low, high = start, stop
        while low < high and folded[low].isspace():
            low += 1
        while high > low and folded[high - 1].isspace():
            high -= 1
        if low < high:
            asks = question or folded[high - 1] == "吗"
            clauses.append(Clause(low, high, sentence, asks))

    index = 0
    length = len(folded)
    while index < length:
        ch = folded[index]
        if ch in SENTENCE_END or (ch == "." and not _between_digits(folded, index)):
            emit(index, question=ch == "?")
            sentence += 1
            start = index + 1
            index += 1
            continue
        if ch == "," and not _between_digits(folded, index):
            emit(index, question=False)
            start = index + 1
            index += 1
            continue
        step = transition_at(folded, index)
        if step:
            emit(index, question=False)
            start = index
            index += step
            continue
        index += 1
    emit(length, question=False)
    return clauses


def sentence_end(folded: str, index: int) -> int:
    """从 index 起第一个句末符号的位置；没有则为文本末尾。"""
    for position in range(index, len(folded)):
        ch = folded[position]
        if ch in SENTENCE_END or (ch == "." and not _between_digits(folded, position)):
            return position
    return len(folded)


@dataclass(frozen=True)
class Line:
    """一行：起止位置（不含换行符）与缩进宽度（制表符按 4 计）。"""

    start: int
    end: int
    indent: int
    blank: bool


def split_lines(text: str) -> list[Line]:
    lines: list[Line] = []
    start = 0
    for piece in text.split("\n"):
        stop = start + len(piece)
        stripped = piece.lstrip(" \t")
        width = sum(4 if ch == "\t" else 1 for ch in piece[: len(piece) - len(stripped)])
        lines.append(Line(start, stop, width, not stripped.strip()))
        start = stop + 1
    return lines
