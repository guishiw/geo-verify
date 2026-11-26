"""规则标注：提及、推荐、否定提及、位次与倾向（规则版本见 RULES_VERSION）。

提及、推荐、否定提及分开判断，“不推荐 X”不是推荐。各步骤的细则见同目录模块：
别名匹配（matching）、分句（segment）、推荐语与倾向词（lexicon）、推荐序列（sequence）。

标注哪些实体
    project.target_entity_id 与 project.competitor_entity_ids（目标主体在前，竞品按登记顺序），
    以及 entities.jsonl 中沿 parent_entity_id 向上能追到它们的实体（按 entities.jsonl 的顺序排在
    各自的上级之后）。别名匹配始终使用 entities.jsonl 的全部实体，这样“岑野 S7”这类竞品同名车型
    被整体归给竞品，其中的“S7”不再算目标车型。

标注哪些观测
    status = valid 且有回答原文的观测。被审计事件剔除的、属于其他封存题库版本的观测同样标注
    （剔除审计要比较被剔除样本的指标，指标模块自行按题库版本和剔除记录筛选）。
    （观测，实体）已有生效标注（project.effective_annotations）的不再生成；因此重复执行不会重复写入。

提及（mentioned）
    别名匹配命中即为提及，span 记录原文位置（kind = mention）。产品被提及时，它的各级上级品牌也算被提及
    （span 记录产品的位置）。无法消解的同形异义片段不计为提及，写入 ext.ambiguous_spans 供人工复核。

推荐与否定（recommended / negated）
    按分句判断：提及所在分句中的推荐语决定该处提及的状态——只有推荐表达 → 推荐；只有否定推荐表达 → 否定；
    推荐与否定并存、或出现拿不准的写法（双重否定、“未必”）→ 拿不准；没有推荐语 → 仅提及。
    比较对象（“比 X 更值得买”中的 X、“与 X 相比”、“X 不如 …”、“而不是 X”）不受分句推荐语影响。
    承接：一个分句本身没有提及（或只有比较对象）、却有推荐语或倾向词时（“澜序 S7 续航长，值得考虑”），
    它承接同一句中前面最近一个有提及的分句——该分句必须只有一个主体、且自身没有推荐语；否则不承接。
    否定列表（引导语为否定推荐的编号列表）中、本身没有推荐语的提及，记为否定。
    处于推荐序列中、作为某项主体的实体，记为推荐。
    实体（含其下级产品的提及）汇总：
      既有推荐又有否定 → negated = true、recommended = false（保守，ext 记 conflict）；
      只有否定 → negated = true、recommended = false；只有推荐 → recommended = true；
      只有拿不准 → recommended = null（无法判断）；其余 → recommended = false。
      未提及 → recommended = false、negated = false。

位次（rankable / rank / rank_list_size）
    见 sequence 模块：只在可明确识别的推荐序列中给位次，不用出现先后代替；rank_list_size 为序列项数，
    rankable = false 时为 null。

倾向（sentiment）
    该实体各处提及所在分句（含承接的分句）中的倾向信号：倾向词（lexicon）、推荐语（正面）、
    否定推荐语（负面）。只有正面 → positive；只有负面 → negative；都没有 → neutral；
    正负并存 → null（判断不了）；未提及 → null。

风险（risk）
    规则不识别风险：flags = []、identified = false、confirmed = null（未复核）、closed = null。
    unfounded_negative：未提及、或提及但没有任何负面信号时为 false；有否定提及、负面或正负并存的倾向时
    为 null——负面内容是否有依据要靠事实核验或人工判断，规则不下结论。
    citation_checks：规则不核验引用，不写该字段。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from geo_verify.annotate import sequence
from geo_verify.annotate.lexicon import Cue, find_cues, is_comparison_object, sentiment_signals
from geo_verify.annotate.matching import EntityMatcher, Mention
from geo_verify.annotate.segment import Clause, fold_width, split_clauses

RULES_VERSION = "1.0"
RULE_ANNOTATOR: dict[str, str] = {
    "type": "rule",
    "id": "geo-verify-rules",
    "version": RULES_VERSION,
}

RECOMMENDED = frozenset({"recommended", "recommended_carry"})
NEGATED = frozenset({"negated", "negated_carry", "negated_list"})
UNSURE = frozenset({"uncertain", "mixed"})

_NOTES = {
    "conflict": "同一回答对该实体既有推荐也有否定表达，保守记为否定提及、未推荐",
    "unsure": "推荐语拿不准（双重否定、“未必”等，或同一分句内推荐与否定并存），推荐记为无法判断",
    "ambiguous_alias": "存在无法消解的同形异义片段，未计为提及，需人工复核",
    "rank_unclear_list": "回答中的编号列表或排序不明确，保守不给位次",
    "rank_multiple_sequences": "回答中有多个推荐序列，无法确定位次",
}


@dataclass
class Judgment:
    """一个实体在一条回答中的规则判断。"""

    entity_id: str
    mentioned: bool
    recommended: bool | None
    negated: bool
    rankable: bool
    rank: int | None
    rank_list_size: int | None
    sentiment: str | None
    unfounded_negative: bool | None
    spans: list[tuple[str, int, int]]
    codes: list[str]
    ambiguous: list[tuple[int, int]]
    note: str | None


@dataclass
class _Occurrence:
    mention: Mention
    clause: int
    status: str = "plain"
    cue_spans: list[tuple[str, int, int]] = field(default_factory=list)
    signals: set[tuple[int, int, int]] = field(default_factory=set)


def _cue_state(cues: list[Cue]) -> str | None:
    polarities = {cue.polarity for cue in cues}
    if not polarities:
        return None
    if "uncertain" in polarities:
        return "uncertain"
    if polarities == {"positive", "negative"}:
        return "mixed"
    return "recommended" if "positive" in polarities else "negated"


def _cue_spans(cues: list[Cue]) -> list[tuple[str, int, int]]:
    kinds = {"positive": "recommendation", "negative": "negation"}
    return [(kinds[cue.polarity], cue.start, cue.end) for cue in cues if cue.polarity in kinds]


def _clause_of(clauses: list[Clause], position: int) -> int:
    low, high = 0, len(clauses) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if clauses[middle].start <= position:
            low = middle
        else:
            high = middle - 1
    return low


class RuleAnnotator:
    """对一组实体做规则标注。entities 是 entities.jsonl 的全部实体，annotated 是要输出判断的实体。"""

    def __init__(self, entities: list[dict[str, Any]], annotated: Iterable[str]) -> None:
        self.matcher = EntityMatcher(entities)
        self.annotated = list(annotated)

    def judge(self, text: str) -> dict[str, Judgment]:
        folded = fold_width(text)
        found = self.matcher.find(text)
        clauses = split_clauses(folded)
        occurrences = self._occurrences(folded, clauses, found.mentions)
        clause_cues = [find_cues(folded, c.start, c.end, question=c.question) for c in clauses]
        clause_signals = [
            self._signals(folded, c, cues) for c, cues in zip(clauses, clause_cues, strict=True)
        ]
        self._apply_clause_cues(occurrences, clause_cues, clause_signals)
        self._apply_carry(occurrences, clauses, clause_cues, clause_signals)
        spots = [
            sequence.Spot(
                item.mention.entity_id,
                item.mention.start,
                item.mention.end,
                comparison=item.status == "comparison",
                doubtful=item.status in NEGATED or item.status in UNSURE,
            )
            for item in occurrences
        ] + [sequence.Spot(None, item.start, item.end) for item in found.ambiguous]
        ranking, _ = sequence.analyze(folded, spots, self.matcher.ancestors)
        self._apply_negative_lists(folded, occurrences, ranking)
        return {
            entity_id: self._judge_entity(entity_id, occurrences, ranking, found.ambiguous)
            for entity_id in self.annotated
        }

    # -- 分句层面 ---------------------------------------------------------

    @staticmethod
    def _occurrences(
        folded: str, clauses: list[Clause], mentions: list[Mention]
    ) -> list[_Occurrence]:
        result: list[_Occurrence] = []
        for mention in mentions:
            index = _clause_of(clauses, mention.start)
            clause = clauses[index]
            occurrence = _Occurrence(mention, index)
            if is_comparison_object(folded, mention.start, mention.end, clause.start, clause.end):
                occurrence.status = "comparison"
            result.append(occurrence)
        return result

    @staticmethod
    def _signals(folded: str, clause: Clause, cues: list[Cue]) -> set[tuple[int, int, int]]:
        signals = set(sentiment_signals(folded, clause.start, clause.end))
        for cue in cues:
            if cue.polarity != "uncertain":
                signals.add((cue.start, cue.end, 1 if cue.polarity == "positive" else -1))
        return signals

    @staticmethod
    def _apply_clause_cues(
        occurrences: list[_Occurrence],
        clause_cues: list[list[Cue]],
        clause_signals: list[set[tuple[int, int, int]]],
    ) -> None:
        for item in occurrences:
            if item.status == "comparison":
                continue
            cues = clause_cues[item.clause]
            state = _cue_state(cues)
            if state is not None:
                item.status = state
                item.cue_spans = _cue_spans(cues)
            item.signals |= clause_signals[item.clause]

    def _apply_carry(
        self,
        occurrences: list[_Occurrence],
        clauses: list[Clause],
        clause_cues: list[list[Cue]],
        clause_signals: list[set[tuple[int, int, int]]],
    ) -> None:
        by_clause: dict[int, list[_Occurrence]] = {}
        for item in occurrences:
            if item.status != "comparison":
                by_clause.setdefault(item.clause, []).append(item)
        for source in range(len(clauses)):
            if source in by_clause or not (clause_cues[source] or clause_signals[source]):
                continue
            target = self._carry_target(source, clauses, clause_cues, by_clause)
            if target is None:
                continue
            state = _cue_state(clause_cues[source])
            for item in by_clause[target]:
                if state is not None:
                    item.status = state if state in UNSURE else f"{state}_carry"
                    item.cue_spans.extend(_cue_spans(clause_cues[source]))
                item.signals |= clause_signals[source]

    def _carry_target(
        self,
        source: int,
        clauses: list[Clause],
        clause_cues: list[list[Cue]],
        by_clause: dict[int, list[_Occurrence]],
    ) -> int | None:
        sentence_index = clauses[source].sentence
        for index in range(source - 1, -1, -1):
            if clauses[index].sentence != sentence_index:
                return None
            if index in by_clause:
                if clause_cues[index]:
                    return None
                ids = {item.mention.entity_id for item in by_clause[index]}
                tops = {e for e in ids if not any(e in self.matcher.ancestors(o) for o in ids)}
                return index if len(tops) == 1 else None
            if clause_cues[index]:
                return None
        return None

    @staticmethod
    def _apply_negative_lists(
        folded: str, occurrences: list[_Occurrence], ranking: sequence.RankResult
    ) -> None:
        for candidate in ranking.negative_lists:
            lead_start = candidate.lead_start if candidate.lead_start is not None else 0
            lead_end = candidate.lead_end if candidate.lead_end is not None else 0
            lead_clauses = split_clauses(folded[lead_start:lead_end])
            last = lead_clauses[-1]
            cues = find_cues(folded, lead_start + last.start, lead_start + last.end)
            spans = _cue_spans(cues)
            for item in occurrences:
                if item.status != "plain":
                    continue
                if any(
                    entry.start <= item.mention.start and item.mention.end <= entry.end
                    for entry in candidate.items
                ):
                    item.status = "negated_list"
                    item.cue_spans.extend(spans)
                    item.signals |= {(start, end, -1) for _, start, end in spans}

    # -- 实体层面 ---------------------------------------------------------

    def _judge_entity(
        self,
        entity_id: str,
        occurrences: list[_Occurrence],
        ranking: sequence.RankResult,
        ambiguous: list[Any],
    ) -> Judgment:
        ancestors = self.matcher.ancestors
        own = [
            item
            for item in occurrences
            if item.mention.entity_id == entity_id or entity_id in ancestors(item.mention.entity_id)
        ]
        codes: set[str] = set()
        notes: list[str] = []
        statuses = {item.status for item in own}
        item = ranking.rank_of(entity_id, ancestors)
        recommended_any = bool(statuses & RECOMMENDED) or item is not None
        negated_any = bool(statuses & NEGATED)
        mentioned = bool(own)
        recommended: bool | None
        negated = False
        if not mentioned:
            recommended = False
        elif recommended_any and negated_any:
            negated, recommended = True, False
            codes.add("conflict")
            notes.append(_NOTES["conflict"])
        elif negated_any:
            negated, recommended = True, False
        elif recommended_any:
            recommended = True
        elif statuses & UNSURE:
            recommended = None
            notes.append(_NOTES["unsure"])
        else:
            recommended = False

        spans: set[tuple[str, int, int]] = set()
        for entry in own:
            spans.add(("mention", entry.mention.start, entry.mention.end))
            codes.add(
                "mention_direct" if entry.mention.entity_id == entity_id else "mention_via_product"
            )
            codes.update(_STATUS_CODES.get(entry.status, ()))
            if entry.status in RECOMMENDED or entry.status in NEGATED:
                spans.update(entry.cue_spans)
        if item is not None:
            spans.add(("rank", item.marker, item.end))
            codes.add("recommend_sequence")

        if ranking.rankable:
            codes.add("rank_in_sequence" if item is not None else "rank_not_in_sequence")
        else:
            code = f"rank_{ranking.reason}"
            codes.add(code)
            if mentioned and code in _NOTES:
                notes.append(_NOTES[code])

        sentiment = self._sentiment(own) if mentioned else None
        if mentioned and sentiment is None:
            codes.add("sentiment_mixed")
        unfounded: bool | None = False
        if mentioned and (negated or (sentiment != "positive" and sentiment != "neutral")):
            unfounded = None

        ambiguous_spans = [
            (entry.start, entry.end)
            for entry in ambiguous
            if any(
                candidate == entity_id or entity_id in ancestors(candidate)
                for candidate in entry.entity_ids
            )
        ]
        if ambiguous_spans:
            codes.add("ambiguous_alias")
            notes.append(_NOTES["ambiguous_alias"])

        return Judgment(
            entity_id=entity_id,
            mentioned=mentioned,
            recommended=recommended,
            negated=negated,
            rankable=ranking.rankable,
            rank=item.number if item is not None else None,
            rank_list_size=(len(ranking.sequence.items) if ranking.sequence is not None else None),
            sentiment=sentiment,
            unfounded_negative=unfounded,
            spans=sorted(spans, key=lambda span: (span[1], span[2], span[0])),
            codes=sorted(codes),
            ambiguous=ambiguous_spans,
            note="；".join(notes) if notes else None,
        )

    @staticmethod
    def _sentiment(own: list[_Occurrence]) -> str | None:
        signs = {sign for entry in own for _, _, sign in entry.signals}
        if signs == {1}:
            return "positive"
        if signs == {-1}:
            return "negative"
        return "neutral" if not signs else None


_STATUS_CODES: dict[str, tuple[str, ...]] = {
    "comparison": ("comparison_object",),
    "recommended": ("recommend_cue",),
    "recommended_carry": ("recommend_carry",),
    "negated": ("negation_cue",),
    "negated_carry": ("negation_carry",),
    "negated_list": ("negation_list",),
    "uncertain": ("uncertain_cue",),
    "mixed": ("mixed_cues",),
}
