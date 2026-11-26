"""推荐、否定与倾向的词表规则。

全部在折叠后的文本（见 segment.fold_width）上、在一个分句之内判断。

推荐语（cue）在分句内从左到右、同一起点取最长匹配，互不重叠：

- 推荐表达：推荐、首推、力荐、建议选择、建议购买、值得考虑、值得买、首选、优先考虑、可以考虑、
  不错的选择、最佳选择……（POSITIVE_CUES）；
- 否定推荐表达：不建议、不值得、不要选、不要买、别买、别选、慎选、建议避开、避免选择……（NEGATIVE_CUES）；
- 中性的疑问写法先被整体吃掉，不产生推荐语：推不推荐、值不值得、要不要、是否值得、推荐度……（NEUTRAL_CUES）。

推荐语前面紧邻的修饰（从右往左逐个识别，最多 6 个，跳过“很、太、更、都、也”等副词）决定最终极性：

- 推荐表达前有一个否定词（不、没、没有、不是、并非、不太、不在、算不上……）→ 否定推荐（“不推荐 X”）；
- 推荐表达前有两个及以上否定词（“不是不推荐”“没有不推荐”）→ 拿不准；
- 否定推荐表达前再有否定词（“不是不建议”“并非不值得”）→ 拿不准（双重否定一律保守处理）；
- 前有“未必、不一定、不见得”→ 拿不准；
- 疑问分句里的推荐语一律不生效。

比较对象：提及前紧邻“比、比起、相比、相较于、对比、除了、不是、而不是、优于、胜过……”，
或前有“与 / 和 / 跟 / 同”且后接“相比 / 比较 / 比起来 / 对比”，或后接“不如”的，是比较的对象，
不受本分句推荐语的影响，也不计入倾向。

倾向词：正面词（出色、可靠、省心、划算、口碑好……）与负面词（故障、投诉、缺陷、召回、虚标……）。
正面词前紧邻否定词（不、没有、不够、不太、算不上……）记为负面；负面词前紧邻否定词（“没有故障”）不计。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Polarity = Literal["positive", "negative", "uncertain"]

POSITIVE_CUES: tuple[str, ...] = (
    "推荐",
    "首推",
    "力荐",
    "建议选择",
    "建议选",
    "建议购买",
    "建议买",
    "建议入手",
    "建议考虑",
    "建议优先",
    "值得考虑",
    "值得推荐",
    "值得买",
    "值得购买",
    "值得入手",
    "值得选择",
    "首选",
    "优先考虑",
    "优先选择",
    "可以考虑",
    "不妨考虑",
    "可考虑",
    "可以选择",
    "可以入手",
    "不错的选择",
    "好的选择",
    "好选择",
    "最佳选择",
    "理想选择",
    "理想的选择",
    "更好的选择",
    "明智的选择",
)

NEGATIVE_CUES: tuple[str, ...] = (
    "不建议",
    "不值得",
    "不要选",
    "不要买",
    "不要考虑",
    "不要入手",
    "别买",
    "别选",
    "别考虑",
    "别入手",
    "慎选",
    "慎买",
    "慎入",
    "谨慎选择",
    "谨慎购买",
    "建议避开",
    "最好避开",
    "尽量避开",
    "应该避开",
    "避免选择",
    "避免购买",
    "不宜选择",
    "不宜购买",
    "不予推荐",
)

NEUTRAL_CUES: tuple[str, ...] = (
    "推不推荐",
    "推荐不推荐",
    "值不值得",
    "值得不值得",
    "建不建议",
    "要不要",
    "买不买",
    "选不选",
    "可不可以",
    "能不能",
    "是否值得",
    "是否推荐",
    "是否建议",
    "推荐度",
    "推荐指数",
)

NEGATORS: tuple[str, ...] = (
    "并不是",
    "并没有",
    "不是说",
    "算不上",
    "称不上",
    "谈不上",
    "不怎么",
    "不太",
    "不大",
    "不很",
    "并不",
    "并非",
    "不是",
    "没有",
    "不在",
    "不算",
    "不要",
    "不用",
    "不必",
    "无需",
    "不",
    "没",
    "别",
)
HEDGES: tuple[str, ...] = ("未必", "不一定", "不见得")
SKIPPABLE: tuple[str, ...] = (
    "不得不",
    "很",
    "太",
    "特别",
    "那么",
    "这么",
    "十分",
    "非常",
    "最",
    "更",
    "比较",
    "真的",
    "还是",
    "也",
    "都",
    "就",
    "会",
    "能",
    "再",
)
_MAX_PREFIX_TOKENS = 6

POSITIVE_WORDS: tuple[str, ...] = (
    "出色",
    "优秀",
    "优异",
    "不错",
    "很好",
    "良好",
    "领先",
    "可靠",
    "省心",
    "划算",
    "实惠",
    "超值",
    "性价比高",
    "好评",
    "口碑好",
    "口碑不错",
    "满意",
    "舒适",
    "亮眼",
    "突出",
    "稳定",
    "安全性高",
)
NEGATIVE_WORDS: tuple[str, ...] = (
    "较差",
    "很差",
    "太差",
    "差评",
    "糟糕",
    "故障",
    "投诉",
    "缺陷",
    "短板",
    "槽点",
    "失望",
    "落后",
    "隐患",
    "召回",
    "缩水",
    "偏贵",
    "太贵",
    "虚标",
    "异响",
    "劣势",
    "缺点",
)
_SENTIMENT_NEGATORS = re.compile(
    r"(?:不|没|没有|并不|不太|不够|不算|不是很|谈不上|算不上|称不上|毫无)"
    r"[ \t]*(?:明显的?|什么|任何|太|很|那么)?[ \t]*$"
)

_COMPARISON_BEFORE = re.compile(
    r"(?:(?<!价)比|比起|相比于?|相较于?|对比|除了|除|不是|并非|并不是|而不是|优于|胜过|强于|好于|超过|超越)"
    r"[ \t]*$"
)
_PAIR_BEFORE = re.compile(r"(?:与|和|跟|同)[ \t]*$")
_PAIR_AFTER = re.compile(r"^[ \t]*(?:相比|比较|比起来|对比)")
_WORSE_AFTER = re.compile(r"^[ \t]*不如")


def _alternation(words: tuple[str, ...]) -> re.Pattern[str]:
    ordered = sorted(set(words), key=lambda word: (-len(word), word))
    return re.compile("|".join(re.escape(word) for word in ordered))


_CUE_KIND: dict[str, str] = {
    **dict.fromkeys(POSITIVE_CUES, "positive"),
    **dict.fromkeys(NEGATIVE_CUES, "negative"),
    **dict.fromkeys(NEUTRAL_CUES, "neutral"),
}
_CUE_PATTERN = _alternation(tuple(_CUE_KIND))
_WORD_SIGN: dict[str, int] = {
    **dict.fromkeys(POSITIVE_WORDS, 1),
    **dict.fromkeys(NEGATIVE_WORDS, -1),
}
_WORD_PATTERN = _alternation(tuple(_WORD_SIGN))
_PREFIX_TOKENS = sorted({*NEGATORS, *HEDGES, *SKIPPABLE}, key=lambda word: (-len(word), word))


@dataclass(frozen=True)
class Cue:
    """一处推荐语：起止位置（含前置否定词）、极性和命中的词。"""

    start: int
    end: int
    polarity: Polarity
    word: str


@dataclass(frozen=True)
class _Prefix:
    negations: int
    hedged: bool
    start: int


def _scan_prefix(text: str, low: int, position: int) -> _Prefix:
    """从 position 往左识别紧邻的否定词、拿不准词和可跳过的副词（不越过 low）。"""
    cursor = position
    negations = 0
    hedged = False
    start = position
    for _ in range(_MAX_PREFIX_TOKENS):
        while cursor > low and text[cursor - 1] in " \t":
            cursor -= 1
        token = next(
            (
                word
                for word in _PREFIX_TOKENS
                if cursor - len(word) >= low and text.startswith(word, cursor - len(word))
            ),
            None,
        )
        if token is None:
            break
        cursor -= len(token)
        if token in NEGATORS:
            negations += 1
            start = cursor
        elif token in HEDGES:
            hedged = True
            start = cursor
    return _Prefix(negations, hedged, start)


def _polarity(kind: str, prefix: _Prefix) -> Polarity:
    if prefix.hedged:
        return "uncertain"
    if kind == "positive":
        if prefix.negations == 0:
            return "positive"
        return "negative" if prefix.negations == 1 else "uncertain"
    return "negative" if prefix.negations == 0 else "uncertain"


def find_cues(folded: str, start: int, end: int, *, question: bool = False) -> list[Cue]:
    """一个分句内的推荐语。疑问分句返回空列表。"""
    if question:
        return []
    cues: list[Cue] = []
    for match in _CUE_PATTERN.finditer(folded, start, end):
        kind = _CUE_KIND[match.group()]
        if kind == "neutral":
            continue
        prefix = _scan_prefix(folded, start, match.start())
        cues.append(Cue(prefix.start, match.end(), _polarity(kind, prefix), match.group()))
    return cues


def sentiment_signals(folded: str, start: int, end: int) -> list[tuple[int, int, int]]:
    """一个分句内的倾向词：[(起点, 终点, +1 或 -1)]。"""
    signals: list[tuple[int, int, int]] = []
    for match in _WORD_PATTERN.finditer(folded, start, end):
        sign = _WORD_SIGN[match.group()]
        if _SENTIMENT_NEGATORS.search(folded, start, match.start()):
            if sign < 0:
                continue
            sign = -1
        signals.append((match.start(), match.end(), sign))
    return signals


def is_comparison_object(folded: str, start: int, end: int, low: int, high: int) -> bool:
    """[start, end) 处的提及在分句 [low, high) 中是否只是比较或排除的对象。"""
    before = folded[low:start]
    after = folded[end:high]
    if _COMPARISON_BEFORE.search(before) or _WORSE_AFTER.match(after):
        return True
    return bool(_PAIR_BEFORE.search(before) and _PAIR_AFTER.match(after))
