"""题库检查：重复、近似重复、缺失的意图类型、个人信息、诱导性提问。

诱导性提问的识别结合实体表：先用别名匹配把题目里的目标主体和竞品替换成占位符，
再判断句式。所以“为什么 X 是最好的”只有在 X 是本项目的目标主体（含别名、全半角、
连字符等写法）时才算诱导，换成无关品牌不会误报。

严重程度：`error` 阻止封存；`warning` 只提示。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator

from geo_verify import spec
from geo_verify.annotate.matching import EntityMatcher, normalize_key
from geo_verify.errors import InputError

DEFAULT_NEAR_THRESHOLD = 0.85

INTENT_NAMES: dict[str, str] = {
    "brand": "品牌",
    "product": "产品",
    "category": "品类",
    "scenario": "场景",
    "problem": "问题",
    "comparison": "比较",
    "decision": "决策",
    "after_sales": "售后",
    "risk": "风险",
    "competitor": "竞品",
    "general": "综合",
}
INTENT_TYPES: tuple[str, ...] = tuple(INTENT_NAMES)

RULE_LABELS: dict[str, str] = {
    "invalid_prompt": "题目格式不合契约",
    "duplicate_query_id": "题目编号重复",
    "duplicate_text": "题目重复",
    "near_duplicate": "题目近似重复",
    "personal_info": "含个人信息",
    "leading_presupposition": "诱导性提问：预设结论",
    "leading_praise_only": "诱导性提问：限定褒义",
    "leading_exclude_competitor": "诱导性提问：排除竞品",
    "missing_intent_type": "缺失意图类型",
}


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    query_ids: tuple[str, ...]
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "rule": self.rule,
            "severity": self.severity,
            "query_ids": list(self.query_ids),
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(item.severity == "error" for item in findings)


# ---------------------------------------------------------------- 文本相似度


def text_key(text: str) -> str:
    """比较题目是否重复时用的键：规范化后只保留文字和数字。"""
    return "".join(ch for ch in normalize_key(text) if ch.isalnum())


def similarity(left: str, right: str) -> float:
    """相似度 = 2 × 公共匹配字符数 /（两段文本长度之和），取值 0～1。"""
    if not left and not right:
        return 1.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


# ---------------------------------------------------------------- 个人信息

_PII_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "id_card",
        "身份证号",
        re.compile(
            r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
            r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])"
        ),
    ),
    (
        "mobile",
        "手机号",
        re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d(?:[- ]?\d{4}){2}(?!\d)"),
    ),
    ("landline", "固定电话", re.compile(r"(?<!\d)0\d{2,3}[- ]\d{7,8}(?!\d)")),
    (
        "email",
        "邮箱",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
    ),
)


def find_personal_info(text: str) -> list[tuple[str, str]]:
    """返回题目中疑似个人信息的类别列表 [(类别编号, 中文名)]；不返回命中的原文。"""
    normalized = unicodedata.normalize("NFKC", text)
    return [(kind, label) for kind, label, pattern in _PII_PATTERNS if pattern.search(normalized)]


# ---------------------------------------------------------------- 诱导性提问

_T = "<T>"
_C = "<C>"
_CL = r"[^,。;!?]"  # 分句内
_SN = r"[^。;!?]"  # 整句内
_NE = r"[^,。;!?<]"  # 分句内且中间没有别的实体
_CLAUSE_SPLIT = re.compile(r"[,。;!?]")

_WHY = r"(?:为什么|为何|为啥|凭什么)"
_POSITIVE = (
    r"(?<![不没])(?:最好|更好|这么好|那么好|很好|比较好|好评|口碑好|卖得好|卖的好|更强|很强"
    r"|这么强|最强|优秀|出色|领先|受欢迎|值得|靠谱|厉害|畅销|热销|这么火|那么火|首选"
    r"|第一名|排名第一|销量第一|销量高|性价比高|胜过|赢过|吊打|秒杀|碾压|完胜|优于|最佳|最优"
    r"|天花板|无可替代|当之无愧|无敌)"
)
_SUPERLATIVE = (
    r"(?<![不没])(?:最好|最值得|最强|最佳|最优|最靠谱|最划算|最安全|最省心|最保值|最受欢迎"
    r"|最畅销|第一名|排名第一|销量第一|首选|天花板|遥遥领先|无可替代|当之无愧|无敌|吊打|秒杀"
    r"|碾压|完胜)"
)
_NEGATIVE = (
    r"(?:不如|比不上|这么差|那么差|很差|太差|更差|差劲|口碑差|质量差|不行|不好|不值得|不靠谱"
    r"|问题多|毛病多|垃圾|不推荐|不建议|不能买|别买|翻车|没人买|卖不动|不安全)"
)
_OPEN_CHOICE = re.compile(r"哪个|哪款|哪家|哪一|哪些|哪种|哪几|谁|还是|什么车|什么品牌|什么牌子")

_WHY_RE = re.compile(_WHY)
_TARGET_POSITIVE = re.compile(f"{_T}{_NE}*{_POSITIVE}|{_POSITIVE}{_NE}*(?:是|就是|当属|属于){_T}")
_TARGET_SUPERLATIVE = re.compile(f"{_T}{_NE}*{_SUPERLATIVE}|{_SUPERLATIVE}{_NE}*{_T}")
_TARGET_BETTER_THAN = re.compile(
    f"{_T}{_CL}*?比{_CL}*?(?:好|强|优秀|出色|领先|划算|省心|安全|靠谱|值得买?)"
    f"{_CL}{{0,2}}?(?:在哪|在什么地方|多少|哪些方面|哪里)"
)
_COMPETITOR_NEGATIVE = re.compile(f"{_C}{_NE}*{_NEGATIVE}")

_ONLY_POSITIVE = re.compile(
    f"(?:只|仅|光|单)(?:说|讲|谈|列出|列举|列|写|介绍|提|要|看|聊|回答|告诉我|需要)"
    f"{_CL}{{0,10}}?(?:优点|好处|优势|亮点|长处|正面|好话|好的方面|好的地方)"
)
_NO_NEGATIVE = re.compile(
    f"(?:不要|别|不用|不许|不准|不必|请勿|勿|无需|不需要|不能|禁止|避免)再?"
    f"(?:说|讲|谈|列|写|提到|提及|提|介绍|出现|涉及|包含)"
    f"{_CL}{{0,10}}?(?:缺点|不足|劣势|负面|坏处|短板|槽点|差评|缺陷|毛病|坏话)"
)
_PRAISE_VERB = re.compile(r"夸(?![张大口耀])|赞美|吹一吹|吹吹|吹爆|吹一波|美言|说好话")
_POSITIVE_TONE = re.compile(
    f"(?:正面|积极|褒义)的?(?:话|角度|语气|口吻|方式|词)"
    f"{_CL}{{0,6}}?(?:介绍|评价|描述|说|讲|写|形容)"
)

_FORBID = r"(?:不要|别|不用|不许|不准|不必|请勿|勿|无需|不需要|不能|禁止|避免|不可以)"
_MENTION_VERB = (
    r"(?:提到|提及|提起|提|说到|说|讲|推荐|介绍|列出|列举|列|出现|考虑|包含|包括|涉及"
    r"|对比|比较|带上|加入)"
)
_OTHERS = (
    f"(?:(?:其他|其它|别的|任何|别家|其余){_NE}{{0,4}}?"
    f"(?:品牌|牌子|车型|车|产品|厂家|厂商|竞品|对手)|竞品|竞争对手|友商)"
)
_FORBID_COMPETITOR = re.compile(f"{_FORBID}再?{_MENTION_VERB}{_NE}{{0,8}}?(?:{_C}|{_OTHERS})")
_EXCEPT_TARGET = re.compile(
    f"除了?{_T}{_SN}*?(?:不要|别(?!的)|不用|不许|不准|不必|不能|不想|都不|一律不|统统不|一概不)"
    f"{_SN}{{0,4}}?(?:推荐|提|说|讲|考虑|介绍|看|买|选)"
)
_ONLY_TARGET = re.compile(
    f"只(?:推荐|考虑|选|能选|能推荐|许推荐|准推荐|要推荐|认|买){_NE}{{0,6}}?{_T}"
)
_COMPETITOR_FORBIDDEN = re.compile(
    f"(?:(?:{_C}|{_OTHERS}){_NE}{{0,6}}?(?:就|都|也|一律|统统|一概)?"
    f"|(?:其他|其它|别的|别家|其余)的?(?:都|一律|统统|一概|全都|全)?)"
    f"{_FORBID}再?{_MENTION_VERB}"
)


def _plain(fragment: str) -> str:
    cleaned = fragment.replace("\n", "。").replace("<", "").replace(">", "")
    return normalize_key(cleaned)


def skeleton(text: str, matcher: EntityMatcher, roles: dict[str, str]) -> str:
    """把题目中的目标主体换成 <T>、竞品换成 <C>，其余文字规范化，供句式判断。"""
    pieces: list[str] = []
    cursor = 0
    for mention in matcher.find(text).mentions:
        pieces.append(_plain(text[cursor : mention.start]))
        pieces.append(_T if roles[mention.entity_id] == "target" else _C)
        cursor = mention.end
    pieces.append(_plain(text[cursor:]))
    return "".join(pieces)


def _readable(snippet: str) -> str:
    return snippet.replace(_T, "〔目标主体〕").replace(_C, "〔竞品〕")


def find_leading(text: str, matcher: EntityMatcher, roles: dict[str, str]) -> dict[str, str]:
    """返回 {规则编号: 命中的片段}。一条规则只报一次。"""
    whole = skeleton(text, matcher, roles)
    clauses = [clause for clause in _CLAUSE_SPLIT.split(whole) if clause]
    hits: dict[str, str] = {}

    def record(rule: str, match: re.Match[str] | None) -> None:
        if match is not None:
            hits.setdefault(rule, _readable(match.group(0)))

    open_choice = _OPEN_CHOICE.search(whole) is not None
    for clause in clauses:
        why = _WHY_RE.search(clause) is not None
        if why:
            record("leading_presupposition", _TARGET_POSITIVE.search(clause))
            record("leading_presupposition", _COMPETITOR_NEGATIVE.search(clause))
        if not open_choice:
            record("leading_presupposition", _TARGET_SUPERLATIVE.search(clause))
        record("leading_presupposition", _TARGET_BETTER_THAN.search(clause))

        record("leading_praise_only", _ONLY_POSITIVE.search(clause))
        record("leading_praise_only", _NO_NEGATIVE.search(clause))
        if _T in clause:
            record("leading_praise_only", _PRAISE_VERB.search(clause))

        record("leading_exclude_competitor", _FORBID_COMPETITOR.search(clause))
        record("leading_exclude_competitor", _ONLY_TARGET.search(clause))
        record("leading_exclude_competitor", _COMPETITOR_FORBIDDEN.search(clause))
    if _T in whole:
        record("leading_praise_only", _POSITIVE_TONE.search(whole))
    record("leading_exclude_competitor", _EXCEPT_TARGET.search(whole))
    return hits


# ---------------------------------------------------------------- 主流程


@cache
def _prompt_validator() -> Draft202012Validator:
    schema = spec.load_schema("prompt_set")
    item = dict(schema["properties"]["prompts"]["items"])
    item["$defs"] = schema["$defs"]
    return Draft202012Validator(item)


def prompt_issues(prompt: Any) -> list[str]:
    """单道题不合契约之处（可读说明）；空列表表示合法。"""
    errors = sorted(
        _prompt_validator().iter_errors(prompt),
        key=lambda error: (list(map(str, error.absolute_path)), error.message),
    )
    return [
        f"{'/'.join(map(str, error.absolute_path)) or '/'}：{error.message}" for error in errors
    ]


def _near_duplicates(unique: list[tuple[str, str]], threshold: float) -> list[Finding]:
    findings: list[Finding] = []
    bags = [Counter(key) for _, key in unique]
    for first in range(len(unique)):
        first_id, first_key = unique[first]
        for second in range(first + 1, len(unique)):
            second_id, second_key = unique[second]
            total = len(first_key) + len(second_key)
            if total == 0 or 2 * min(len(first_key), len(second_key)) / total < threshold:
                continue
            shared = sum((bags[first] & bags[second]).values())
            if 2 * shared / total < threshold:
                continue
            score = similarity(first_key, second_key)
            if score >= threshold:
                findings.append(
                    Finding(
                        "near_duplicate",
                        "warning",
                        (first_id, second_id),
                        f"题目 {first_id} 与 {second_id} 近似重复（相似度 {score:.3f}，"
                        f"阈值 {threshold:g}），请确认是否保留",
                        {"similarity": round(score, 6), "threshold": threshold},
                    )
                )
    return findings


def lint_prompts(
    prompts: list[Any],
    entities: list[dict[str, Any]],
    *,
    near_threshold: float = DEFAULT_NEAR_THRESHOLD,
) -> list[Finding]:
    """检查草稿题库，返回全部发现（顺序确定）。"""
    if not 0 < near_threshold <= 1:
        raise InputError(f"相似度阈值必须在 (0, 1] 之间：{near_threshold}")
    findings: list[Finding] = []
    valid: list[dict[str, Any]] = []
    for line, prompt in enumerate(prompts, start=1):
        issues = prompt_issues(prompt)
        if not issues:
            valid.append(prompt)
            continue
        query_id = prompt.get("query_id") if isinstance(prompt, dict) else None
        findings.append(
            Finding(
                "invalid_prompt",
                "error",
                (query_id,) if isinstance(query_id, str) else (),
                f"第 {line} 行不符合题目契约：{'；'.join(issues)}",
            )
        )

    by_id: dict[str, int] = Counter(prompt["query_id"] for prompt in valid)
    for query_id, count in by_id.items():
        if count > 1:
            findings.append(
                Finding(
                    "duplicate_query_id",
                    "error",
                    (query_id,),
                    f"题目编号 {query_id} 出现了 {count} 次",
                )
            )

    groups: dict[str, list[str]] = {}
    for prompt in valid:
        groups.setdefault(text_key(prompt["text"]), []).append(prompt["query_id"])
    for query_ids in groups.values():
        if len(query_ids) > 1:
            findings.append(
                Finding(
                    "duplicate_text",
                    "error",
                    tuple(query_ids),
                    f"题目 {'、'.join(query_ids)} 规范化后完全相同（忽略全半角、大小写、空格和标点）",
                )
            )
    unique = [(query_ids[0], key) for key, query_ids in groups.items()]
    findings.extend(_near_duplicates(unique, near_threshold))

    matcher = EntityMatcher(entities)
    roles = {entity["entity_id"]: entity["role"] for entity in entities}
    leading: list[Finding] = []
    for prompt in valid:
        kinds = find_personal_info(prompt["text"])
        if kinds:
            findings.append(
                Finding(
                    "personal_info",
                    "error",
                    (prompt["query_id"],),
                    f"题目 {prompt['query_id']} 含疑似个人信息："
                    f"{'、'.join(label for _, label in kinds)}（为避免扩散，此处不回显原文）",
                    {"kinds": [kind for kind, _ in kinds]},
                )
            )
        for rule, snippet in find_leading(prompt["text"], matcher, roles).items():
            leading.append(
                Finding(
                    rule,
                    "error",
                    (prompt["query_id"],),
                    f"题目 {prompt['query_id']} {RULE_LABELS[rule]}（命中“{snippet}”）",
                )
            )
    findings.extend(leading)

    present = {prompt["intent_type"] for prompt in valid}
    missing = [intent for intent in INTENT_TYPES if intent not in present]
    if missing:
        names = "、".join(f"{INTENT_NAMES[intent]}（{intent}）" for intent in missing)
        findings.append(
            Finding(
                "missing_intent_type",
                "warning",
                (),
                f"题库缺少 {len(missing)} 类意图的题目：{names}",
                {"missing": missing},
            )
        )
    return findings
