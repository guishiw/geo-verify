"""别名规范化与匹配。

规则（方法说明见 docs/annotation.md）：

1. 规范化：NFKC（全半角）→ casefold（大小写）→ 去掉空白、连字符、间隔号和不可见格式字符。
   规范化后的每个字符都记着它在原文中的起止位置，命中结果可以定位回原文。
2. 候选：每个实体的名称与别名在规范化文本中的全部出现位置。
3. 词边界：别名以字母或数字开头（结尾）时，原文中紧邻的前（后）一个字符不能是字母或数字，
   所以“S7”不会命中“XS70”。
4. 排除词：候选完全落在本实体某个排除词的出现范围内时作废（“波澜序曲”里的“澜序”）。
5. 跨全部实体最长优先、互不重叠：“岑野 S7”整体归竞品车型，其中的“S7”不再算目标车型。
6. 同形异义：同一位置命中多个实体时，用同一句里最近的前文提及所属的品牌来消解；
   消解不了的不计入任何实体，单独列出供人工复核。
7. 异品牌归属：产品别名紧跟在另一品牌的提及之后（“岑野的 S7”），不算本产品的提及。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

_EXTRA_SEPARATORS = frozenset({"·", "・", "‧", "−"})
_SENTENCE_END = frozenset({"。", "！", "？", "!", "?", "\n"})
_POSSESSIVE_GAP = re.compile(r"^(?:的|家的?|旗下的?|品牌的?|汽车的?|牌的?)?$")
_FOLD_ROUNDS = 4


def _is_ignorable(ch: str) -> bool:
    if ch.isspace() or ch in _EXTRA_SEPARATORS:
        return True
    return unicodedata.category(ch) in {"Pd", "Cf"}


def _fold(unit: str) -> str:
    """一个字符单元（基字符 + 组合符号）的规范化结果，可能为空或多个字符。"""
    current = unit
    for _ in range(_FOLD_ROUNDS):
        folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", current).casefold())
        if folded == current:
            break
        current = folded
    return "".join(ch for ch in current if not _is_ignorable(ch))


def is_word_char(ch: str) -> bool:
    """字母或十进制数字（有大小写之分的字母、十进制数字）；汉字、假名及圆圈序号①等非十进制数字符号不算。"""
    if unicodedata.category(ch) == "No":
        return False
    for piece in unicodedata.normalize("NFKC", ch):
        if unicodedata.category(piece) == "Nd" or (
            piece.isalpha() and piece.lower() != piece.upper()
        ):
            return True
    return False


@dataclass(frozen=True)
class NormalizedText:
    """规范化文本及其到原文的位置映射。"""

    original: str
    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]


def normalize(text: str) -> NormalizedText:
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    index = 0
    length = len(text)
    while index < length:
        stop = index + 1
        while stop < length and unicodedata.category(text[stop]).startswith("M"):
            stop += 1
        for ch in _fold(text[index:stop]):
            chars.append(ch)
            starts.append(index)
            ends.append(stop)
        index = stop
    return NormalizedText(text, "".join(chars), tuple(starts), tuple(ends))


def normalize_key(text: str) -> str:
    """别名的匹配键：规范化后的文本。"""
    return normalize(text).text


@dataclass(frozen=True)
class Mention:
    """一次提及：实体、原文起止位置（左闭右开）和命中的别名。"""

    entity_id: str
    start: int
    end: int
    alias: str


@dataclass(frozen=True)
class AmbiguousMention:
    """同形异义且无法消解的片段：不计入任何实体。"""

    start: int
    end: int
    entity_ids: tuple[str, ...]


@dataclass(frozen=True)
class MatchResult:
    mentions: list[Mention]
    ambiguous: list[AmbiguousMention]


@dataclass(frozen=True)
class _Alias:
    key: str
    written: str
    entity_id: str
    head_is_word: bool
    tail_is_word: bool


def _occurrences(haystack: str, needle: str) -> list[int]:
    found: list[int] = []
    position = haystack.find(needle)
    while position != -1:
        found.append(position)
        position = haystack.find(needle, position + 1)
    return found


class EntityMatcher:
    """对一组实体做别名匹配。实体顺序只用于让结果确定，不影响谁被命中。"""

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self._order: dict[str, int] = {}
        self._parents: dict[str, str | None] = {}
        self._kinds: dict[str, str] = {}
        self._aliases: list[_Alias] = []
        self._exclusions: dict[str, list[str]] = {}
        for position, entity in enumerate(entities):
            entity_id: str = entity["entity_id"]
            self._order[entity_id] = position
            self._parents[entity_id] = entity.get("parent_entity_id")
            self._kinds[entity_id] = entity["kind"]
            seen: set[str] = set()
            for written in [entity["name"], *entity.get("aliases", [])]:
                key = normalize_key(written)
                if not key or key in seen:
                    continue
                seen.add(key)
                self._aliases.append(
                    _Alias(key, written, entity_id, is_word_char(key[0]), is_word_char(key[-1]))
                )
            self._exclusions[entity_id] = [
                key for key in map(normalize_key, entity.get("exclusions", [])) if key
            ]

    @property
    def entity_ids(self) -> list[str]:
        return list(self._order)

    def ancestors(self, entity_id: str) -> tuple[str, ...]:
        """沿 parent_entity_id 向上的全部上级实体（由近到远）。"""
        chain: list[str] = []
        seen = {entity_id}
        parent = self._parents.get(entity_id)
        while parent is not None and parent not in seen and parent in self._parents:
            chain.append(parent)
            seen.add(parent)
            parent = self._parents.get(parent)
        return tuple(chain)

    def root_of(self, entity_id: str) -> str:
        chain = self.ancestors(entity_id)
        return chain[-1] if chain else entity_id

    def find(self, text: str) -> MatchResult:
        normalized = normalize(text)
        candidates = self._candidates(text, normalized)
        chosen = self._select_longest(candidates)
        mentions, ambiguous = self._resolve_shared(text, normalized, chosen)
        mentions = self._drop_foreign_products(text, mentions)
        return MatchResult(mentions, ambiguous)

    # -- 内部步骤 ---------------------------------------------------------

    def _candidates(
        self, text: str, normalized: NormalizedText
    ) -> dict[tuple[int, int], dict[str, str]]:
        """{(规范化起点, 规范化终点): {实体编号: 命中的别名}}，已做词边界和排除词过滤。"""
        blocked = {
            entity_id: [
                (position, position + len(key))
                for key in keys
                for position in _occurrences(normalized.text, key)
            ]
            for entity_id, keys in self._exclusions.items()
            if keys
        }
        found: dict[tuple[int, int], dict[str, str]] = {}
        for alias in self._aliases:
            for position in _occurrences(normalized.text, alias.key):
                stop = position + len(alias.key)
                # 命中必须覆盖整个原文字符：一个字符规范化后展开为多个（如 ß → ss）时，
                # 别名不能从它的中间开始或在它的中间结束
                starts = normalized.starts
                if position > 0 and starts[position - 1] == starts[position]:
                    continue
                if stop < len(starts) and starts[stop] == starts[stop - 1]:
                    continue
                start, end = normalized.starts[position], normalized.ends[stop - 1]
                if alias.head_is_word and start > 0 and is_word_char(text[start - 1]):
                    continue
                if alias.tail_is_word and end < len(text) and is_word_char(text[end]):
                    continue
                if any(
                    low <= position and stop <= high
                    for low, high in blocked.get(alias.entity_id, [])
                ):
                    continue
                found.setdefault((position, stop), {}).setdefault(alias.entity_id, alias.written)
        return found

    @staticmethod
    def _select_longest(
        candidates: dict[tuple[int, int], dict[str, str]],
    ) -> list[tuple[int, int, dict[str, str]]]:
        taken: list[tuple[int, int]] = []
        chosen: list[tuple[int, int, dict[str, str]]] = []
        for start, stop in sorted(candidates, key=lambda span: (span[0] - span[1], span[0])):
            if any(start < high and low < stop for low, high in taken):
                continue
            taken.append((start, stop))
            chosen.append((start, stop, candidates[(start, stop)]))
        chosen.sort(key=lambda item: item[0])
        return chosen

    def _resolve_shared(
        self,
        text: str,
        normalized: NormalizedText,
        chosen: list[tuple[int, int, dict[str, str]]],
    ) -> tuple[list[Mention], list[AmbiguousMention]]:
        mentions: list[Mention] = []
        ambiguous: list[AmbiguousMention] = []
        for position, stop, owners in chosen:
            start, end = normalized.starts[position], normalized.ends[stop - 1]
            if len(owners) == 1:
                ((entity_id, written),) = owners.items()
                mentions.append(Mention(entity_id, start, end, written))
                continue
            context = self._context_root(text, mentions, start)
            matched = [entity_id for entity_id in owners if self.root_of(entity_id) == context]
            if len(matched) == 1:
                mentions.append(Mention(matched[0], start, end, owners[matched[0]]))
            else:
                ordered = tuple(sorted(owners, key=lambda item: self._order[item]))
                ambiguous.append(AmbiguousMention(start, end, ordered))
        return mentions, ambiguous

    def _context_root(self, text: str, mentions: list[Mention], start: int) -> str | None:
        """同一句里最近的前文提及所属的品牌；没有则为 None。"""
        if not mentions:
            return None
        previous = mentions[-1]
        if any(ch in _SENTENCE_END for ch in text[previous.end : start]):
            return None
        return self.root_of(previous.entity_id)

    def _drop_foreign_products(self, text: str, mentions: list[Mention]) -> list[Mention]:
        kept: list[Mention] = []
        previous: Mention | None = None
        for mention in mentions:
            foreign = (
                previous is not None
                and self._kinds[mention.entity_id] == "product"
                and self._kinds[previous.entity_id] == "brand"
                and self.root_of(previous.entity_id) != self.root_of(mention.entity_id)
                and _POSSESSIVE_GAP.match(normalize_key(text[previous.end : mention.start]))
                is not None
            )
            if not foreign:
                kept.append(mention)
            previous = mention
        return kept
