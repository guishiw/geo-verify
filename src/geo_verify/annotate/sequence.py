"""推荐序列与位次。

只有回答里存在**可明确识别**的推荐序列时才给位次；不得用出现先后代替位次。识别三类序列：

1. 编号列表：行首的 `1.`（含 Markdown 有序列表、`### 1.`、`**1.**`）、`1、`、`1)` / `1）`、
   `(1)` / `（1）`、`①`～`⑳`、`第一名：`。其中 `(1)`、`①`、`1、`、`第一名：/第一名是` 也可以出现在行内。
   `21.98`、`5.9` 这类小数不是编号。
   同一样式、同一缩进的编号组成一个列表；编号回到 1、或中间出现“空行后的非缩进正文 / 标题 /
   另一个列表的编号行”时，另起一个列表。行内编号以换行为界。
2. 排序语：“首选… 其次… 再次…”（也认“首推”“第一选择 / 第二选择 / 第三选择”）。
   必须先有“首选”、紧接着有“其次”才算序列；单独的“首选”只是推荐语，单独的“其次”只是行文顺序。
3. 推荐顺序链：“推荐顺序 / 推荐排序 / 推荐排名 / 优先顺序 / 优先级 / 排序 / 排名”后接
   `A > B > C`（`＞` 同）。没有这些引导词的 `A > B`（如“续航：A > B”）只是比较，不算序列。

列表项的主体：先看项首片段（去掉开头的空白、`*`、`#`、冒号后，到第一个 `：，；。！？` 或换行为止），
其中不是比较对象的提及若只指向一个实体（产品和它自己的品牌算同一个，取更具体的产品）就是主体；
项首片段没有提及时看整项；指向两个及以上实体、或含无法消解的同形异义片段时主体不明。

每个候选序列的判定：

- 各项都不含任何已知实体（步骤列表、参数列表）→ 不是推荐序列，忽略；
- 列表的引导分句（列表前一行，或同一行编号前的文字的最后一个分句）是否定推荐（“以下车型不建议购买：”）
  → 否定列表：不给位次，其中各项的实体按否定提及处理；
- 编号不是从 1 起连续、只有一项、某项主体不明、两项主体相同、某项主体在本项中被否定或拿不准
  → 序列不明确；
- 其余为推荐序列。

整条回答：存在任何不明确的序列 → rankable = false；恰好一个推荐序列 → rankable = true；
两个及以上推荐序列（多个列表并存）→ rankable = false；没有推荐序列 → rankable = false。
rankable = true 时，实体的 rank 是第一个以它（或它的下级产品）为主体的项的序号，
rank_list_size 是序列的项数；实体不在序列中时 rank = null。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from geo_verify.annotate.lexicon import find_cues
from geo_verify.annotate.segment import Line, sentence_end, split_clauses, split_lines

Ancestors = Callable[[str], tuple[str, ...]]

_CIRCLED_FIRST = 0x2460
_CIRCLED = f"[{chr(_CIRCLED_FIRST)}-{chr(_CIRCLED_FIRST + 19)}]"
_MARKER = re.compile(
    r"(?<![\d.])(?P<dot>\d{1,2})\.(?!\d)"
    r"|(?<![\d(])(?P<close>\d{1,2})\)"
    r"|\((?P<both>\d{1,2})\)"
    r"|(?<!\d)(?P<comma>\d{1,2})、"
    rf"|(?P<circled>{_CIRCLED})"
    r"|第(?P<ordinal>[一二三四五六七八九十]{1,3}|\d{1,2})[名位][ \t]*(?=[:是为]):?"
)
_LINE_ONLY_STYLES = frozenset({"dot", "close"})
_LINE_PREFIX = re.compile(r"[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*|__)?[ \t]*")
_INLINE_COMMA_AFTER = frozenset(" \t:;,。、(")
_BULLET = re.compile(r"[ \t]*(?:[-*+•·][ \t])")
_HEADING = re.compile(r"[ \t]*#{1,6}[ \t]")
_LEAD_SKIP = frozenset(" \t*_#:,、-—–·.")
_LEAD_STOP = frozenset(":,;。!?\n")
_CHINESE_DIGITS = {ch: value for value, ch in enumerate("一二三四五六七八九", start=1)}

_TIERS: dict[str, int] = {
    "首选": 1,
    "首推": 1,
    "第一选择": 1,
    "第一推荐": 1,
    "其次": 2,
    "次选": 2,
    "第二选择": 2,
    "第二推荐": 2,
    "再次": 3,
    "再其次": 3,
    "第三选择": 3,
    "第三推荐": 3,
}
_TIER_PATTERN = re.compile(
    "|".join(re.escape(word) for word in sorted(_TIERS, key=lambda word: (-len(word), word)))
)
_CHAIN_LEAD = re.compile(
    r"(?:推荐顺序|推荐排序|推荐排名|优先顺序|优先级|排序|排名)[ \t]*(?:为|是)?[ \t]*:?[ \t]*"
)


@dataclass(frozen=True)
class Spot:
    """序列识别看到的一处提及。entity_id 为 None 表示无法消解的同形异义片段。

    doubtful：该提及在所在分句里被否定、或推荐语拿不准。
    """

    entity_id: str | None
    start: int
    end: int
    comparison: bool = False
    doubtful: bool = False


@dataclass(frozen=True)
class Item:
    """序列中的一项：序号、编号起点（排序语为该词起点、顺序链为该段起点）、内容起止、主体。"""

    number: int
    marker: int
    start: int
    end: int
    subject: str | None


@dataclass(frozen=True)
class Candidate:
    """一个候选序列。status：sequence / negative / unclear / plain。"""

    kind: str
    status: str
    reason: str
    items: tuple[Item, ...]
    lead_start: int | None = None
    lead_end: int | None = None


@dataclass(frozen=True)
class RankResult:
    rankable: bool
    reason: str
    sequence: Candidate | None
    negative_lists: tuple[Candidate, ...]

    def rank_of(self, entity_id: str, ancestors: Ancestors) -> Item | None:
        if self.sequence is None:
            return None
        for item in self.sequence.items:
            if item.subject is not None and (
                item.subject == entity_id or entity_id in ancestors(item.subject)
            ):
                return item
        return None


@dataclass(frozen=True)
class _Marker:
    start: int
    end: int
    style: str
    number: int
    line: int
    line_start: bool


def parse_number(text: str) -> int:
    """阿拉伯数字或一～九十九的中文数字；无法解析时为 0。"""
    if text.isdigit():
        return int(text)
    if "十" not in text:
        return _CHINESE_DIGITS.get(text, 0)
    tens, _, ones = text.partition("十")
    if (tens and tens not in _CHINESE_DIGITS) or (ones and ones not in _CHINESE_DIGITS):
        return 0
    return (_CHINESE_DIGITS[tens] if tens else 1) * 10 + (_CHINESE_DIGITS[ones] if ones else 0)


def _line_index(lines: list[Line], position: int) -> int:
    low, high = 0, len(lines) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if lines[middle].start <= position:
            low = middle
        else:
            high = middle - 1
    return low


def _markers(folded: str, lines: list[Line]) -> list[_Marker]:
    found: list[_Marker] = []
    for match in _MARKER.finditer(folded):
        style = next(name for name, value in match.groupdict().items() if value is not None)
        value = match.group(style)
        number = ord(value) - _CIRCLED_FIRST + 1 if style == "circled" else parse_number(value)
        line = _line_index(lines, match.start())
        line_start = _LINE_PREFIX.fullmatch(folded, lines[line].start, match.start()) is not None
        if not line_start:
            if style in _LINE_ONLY_STYLES:
                continue
            if style == "comma" and folded[match.start() - 1] not in _INLINE_COMMA_AFTER:
                continue
        found.append(_Marker(match.start(), match.end(), style, number, line, line_start))
    return found


class _Layout:
    """行结构：判断列表在哪里中断。"""

    def __init__(self, folded: str, markers: list[_Marker]) -> None:
        self.folded = folded
        self.lines = split_lines(folded)
        self.marker_lines = {marker.line for marker in markers if marker.line_start}

    def is_break(self, index: int, indent: int) -> bool:
        """第 index 行是否结束缩进为 indent 的列表。"""
        line = self.lines[index]
        if line.blank or line.indent > indent:
            return False
        text = self.folded[line.start : line.end]
        if _BULLET.match(text):
            return False
        previous_blank = index > 0 and self.lines[index - 1].blank
        return previous_blank or index in self.marker_lines or bool(_HEADING.match(text))

    def continues(self, last: _Marker, marker: _Marker) -> bool:
        """marker 能否接在 last 之后组成同一个列表（同一行，或下面几行之间没有中断）。"""
        if marker.line == last.line:
            return True
        if not marker.line_start:
            return False
        indent = self.lines[marker.line].indent
        return not any(self.is_break(index, indent) for index in range(last.line + 1, marker.line))

    def last_item_end(self, marker: _Marker) -> int:
        if not marker.line_start:
            return self.lines[marker.line].end
        indent = self.lines[marker.line].indent
        for index in range(marker.line + 1, len(self.lines)):
            if self.is_break(index, indent):
                return self.lines[index].start
        return len(self.folded)


def _group(markers: list[_Marker], layout: _Layout) -> list[list[_Marker]]:
    """把编号分成列表：同一样式、同一缩进；编号回到 1 或中间有中断时另起一个列表。"""
    groups: list[list[_Marker]] = []
    open_lists: dict[tuple[str, int], list[_Marker]] = {}
    for marker in markers:
        key = (marker.style, layout.lines[marker.line].indent)
        current = open_lists.get(key)
        if current is not None and marker.number != 1 and layout.continues(current[-1], marker):
            current.append(marker)
            continue
        current = [marker]
        groups.append(current)
        open_lists[key] = current
    groups.sort(key=lambda group: group[0].start)
    return groups


_TRAILING = frozenset(",;、")


def _trim(folded: str, start: int, end: int) -> tuple[int, int]:
    """去掉首尾空白，以及结尾的逗号、分号、顿号。"""
    while start < end and folded[start].isspace():
        start += 1
    while end > start and (folded[end - 1].isspace() or folded[end - 1] in _TRAILING):
        end -= 1
    return start, end


class _Resolver:
    def __init__(self, folded: str, spots: list[Spot], ancestors: Ancestors) -> None:
        self.folded = folded
        self.spots = spots
        self.ancestors = ancestors

    def inside(self, start: int, end: int) -> list[Spot]:
        return [spot for spot in self.spots if start <= spot.start and spot.end <= end]

    def subject(self, start: int, end: int) -> tuple[str, str | None]:
        """("ok", 实体) / ("none", None) / ("ambiguous", None)。"""
        spots = [spot for spot in self.inside(start, end) if not spot.comparison]
        if not spots:
            return "none", None
        if any(spot.entity_id is None for spot in spots):
            return "ambiguous", None
        ids = {spot.entity_id for spot in spots if spot.entity_id is not None}
        lineage_tops = {
            entity for entity in ids if not any(entity in self.ancestors(other) for other in ids)
        }
        if len(lineage_tops) == 1:
            return "ok", next(iter(lineage_tops))
        return "ambiguous", None

    def item_subject(self, start: int, end: int) -> tuple[str, str | None]:
        lead = start
        while lead < end and self.folded[lead] in _LEAD_SKIP:
            lead += 1
        stop = lead
        while stop < end and self.folded[stop] not in _LEAD_STOP:
            stop += 1
        state, subject = self.subject(lead, stop)
        if state == "none":
            state, subject = self.subject(start, end)
        return state, subject

    def doubtful(self, item: Item) -> bool:
        return any(
            spot.doubtful and spot.entity_id == item.subject
            for spot in self.inside(item.start, item.end)
        )


def _classify(
    kind: str,
    raw_items: list[tuple[int, int, int, int]],
    resolver: _Resolver,
    *,
    lead: tuple[int, int] | None = None,
) -> Candidate:
    items: list[Item] = []
    states: list[str] = []
    for number, marker, start, end in raw_items:
        low, high = _trim(resolver.folded, start, end)
        state, subject = resolver.item_subject(low, high)
        items.append(Item(number, min(marker, low), low, high, subject))
        states.append(state)
    frozen = tuple(items)
    lead_start, lead_end = lead if lead else (None, None)

    def result(status: str, reason: str) -> Candidate:
        return Candidate(kind, status, reason, frozen, lead_start, lead_end)

    if not any(resolver.inside(item.start, item.end) for item in items):
        return result("plain", "no_entity")
    if lead is not None and _negative_lead(resolver.folded, *lead):
        return result("negative", "negative_lead")
    if [item.number for item in items] != list(range(1, len(items) + 1)):
        return result("unclear", "numbering")
    if len(items) < 2:
        return result("unclear", "single_item")
    if any(state != "ok" for state in states):
        return result("unclear", "item_subject")
    subjects = [item.subject for item in items]
    if len(set(subjects)) != len(subjects):
        return result("unclear", "duplicate_subject")
    if any(resolver.doubtful(item) for item in items):
        return result("unclear", "negated_item")
    return result("sequence", "sequence")


def _negative_lead(folded: str, start: int, end: int) -> bool:
    clauses = split_clauses(folded[start:end])
    if not clauses:
        return False
    last = clauses[-1]
    cues = find_cues(folded, start + last.start, start + last.end, question=last.question)
    polarities = {cue.polarity for cue in cues}
    return polarities == {"negative"}


def _lead_region(layout: _Layout, first: _Marker) -> tuple[int, int] | None:
    if not first.line_start:
        start = layout.lines[first.line].start
        return (start, first.start) if start < first.start else None
    for index in range(first.line - 1, -1, -1):
        line = layout.lines[index]
        if not line.blank:
            return line.start, line.end
    return None


def _lists(folded: str, resolver: _Resolver) -> list[Candidate]:
    lines = split_lines(folded)
    markers = _markers(folded, lines)
    layout = _Layout(folded, markers)
    candidates: list[Candidate] = []
    for group in _group(markers, layout):
        raw: list[tuple[int, int, int, int]] = []
        for position, marker in enumerate(group):
            if position + 1 < len(group):
                following = group[position + 1]
                end = (
                    layout.lines[following.line].start if following.line_start else following.start
                )
            else:
                end = layout.last_item_end(marker)
            raw.append((marker.number, marker.start, marker.end, end))
        candidates.append(_classify("list", raw, resolver, lead=_lead_region(layout, group[0])))
    return candidates


def _orderings(folded: str, resolver: _Resolver) -> list[Candidate]:
    occurrences = [(match, _TIERS[match.group()]) for match in _TIER_PATTERN.finditer(folded)]
    candidates: list[Candidate] = []
    index = 0
    while index < len(occurrences):
        match, tier = occurrences[index]
        if tier != 1:
            index += 1
            continue
        chain = [match]
        cursor = index + 1
        while cursor < len(occurrences):
            following, following_tier = occurrences[cursor]
            gap = folded[chain[-1].end() : following.start()]
            if following_tier != len(chain) + 1 or re.search(r"\n[ \t]*\n", gap):
                break
            chain.append(following)
            cursor += 1
        if len(chain) >= 2:
            raw = [
                (
                    position + 1,
                    item.start(),
                    item.end(),
                    chain[position + 1].start()
                    if position + 1 < len(chain)
                    else sentence_end(folded, item.end()),
                )
                for position, item in enumerate(chain)
            ]
            candidates.append(_classify("ordering", raw, resolver))
        index = cursor
    return candidates


def _chains(folded: str, resolver: _Resolver) -> list[Candidate]:
    candidates: list[Candidate] = []
    for match in _CHAIN_LEAD.finditer(folded):
        start = match.end()
        end = sentence_end(folded, start)
        pieces = folded[start:end].split(">")
        if len(pieces) < 2:
            continue
        raw: list[tuple[int, int, int, int]] = []
        cursor = start
        for number, piece in enumerate(pieces, start=1):
            raw.append((number, cursor, cursor, cursor + len(piece)))
            cursor += len(piece) + 1
        candidates.append(_classify("chain", raw, resolver))
    return candidates


def _subjects(candidate: Candidate) -> tuple[str | None, ...]:
    return tuple(item.subject for item in candidate.items)


def analyze(
    folded: str, spots: list[Spot], ancestors: Ancestors
) -> tuple[RankResult, list[Candidate]]:
    """识别全部候选序列并给出整条回答的位次结论。"""
    resolver = _Resolver(folded, spots, ancestors)
    candidates = [
        *_lists(folded, resolver),
        *_orderings(folded, resolver),
        *_chains(folded, resolver),
    ]
    sequences: list[Candidate] = []
    for item in candidates:
        # 同一组主体按同一顺序出现（如编号列表的各项又写了“首选 / 其次”）算同一个序列
        if item.status == "sequence" and all(
            _subjects(item) != _subjects(other) for other in sequences
        ):
            sequences.append(item)
    negatives = tuple(item for item in candidates if item.status == "negative")
    if any(item.status == "unclear" for item in candidates):
        result = RankResult(False, "unclear_list", None, negatives)
    elif len(sequences) == 1:
        result = RankResult(True, "sequence", sequences[0], negatives)
    elif sequences:
        result = RankResult(False, "multiple_sequences", None, negatives)
    else:
        result = RankResult(False, "no_sequence", None, negatives)
    return result, candidates
