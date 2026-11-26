"""告警输出的措辞红线。

告警与引用域名变化只能表述为“相关”“同期出现”“待核查”的线索，不得写成因果；
也不得给出压制负面、保证平台结果一类的表述（GOAL 第 2 节 R3、R4、R9）。
本模块生成的每一段文字在写出前都经过 ensure_clean 检查，模板一旦写错会以内部错误暴露，
而不是把违规措辞写进结果文件。
"""

from __future__ import annotations

CAUSAL_TERMS = (
    "原因",
    "导致",
    "因为",
    "由于",
    "造成",
    "引起",
    "致使",
    "归因",
    "起因",
    "源于",
    "所以",
    "因此",
    "因而",
    "影响",
    "使得",
)
RED_LINE_TERMS = ("压制", "挤出", "删除负面", "保证", "必然", "首推", "官方", "认证", "对标")
FORBIDDEN_TERMS = CAUSAL_TERMS + RED_LINE_TERMS


def find_forbidden(text: str) -> list[str]:
    """返回文本中出现的禁用词（按清单顺序）。"""
    return [term for term in FORBIDDEN_TERMS if term in text]


def ensure_clean(text: str) -> str:
    """原样返回文本；含禁用词时抛出 RuntimeError（属于模板缺陷，按内部错误处理）。"""
    found = find_forbidden(text)
    if found:
        raise RuntimeError(f"告警文本含禁用措辞 {found}：{text}")
    return text
