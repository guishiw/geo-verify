"""插件代码的轻量 JS 词法处理与结构检查，供 test_extension.py 使用。

这不是完整的 JS 解析器。它只做两件事：

1. ``strip_js``：去掉注释，把字符串、模板、正则字面量的内容换成空白（保留定界符和换行），
   让后面的检查只看到“代码”本身，不被字符串里的方括号、冒号误导；
2. ``analyze``：在去掉字面量后的代码上做结构检查，堵住“换一种写法绕开词表”的常见路子：
   计算属性访问（``window['fe'+'tch']``）、全局对象别名（``globalThis``、``self``、把 ``window``
   当作值传递）、反射与动态代码（``Reflect``、``Function``、``.constructor``）、字符串解码
   （``atob``、``fromCharCode``）、代码里的转义或非 ASCII 标识符（``f\\u0065tch``），
   以及成员名和全局名的白名单。

已知局限（如实记录，不夸大）：
- 词法处理是启发式的：正则字面量与除号按前一个记号区分；遇到无法闭合的字面量直接报错，
  宁可让测试失败也不放过；
- 白名单检查的是“名字”，不区分作用域：局部变量与同名全局不分；成员名不区分所属对象；
- 静态检查不能证明运行时行为，运行时的只读与不越界由 extension/tests/ 下的 JS 测试
  （白名单式替身对象）补充验证；两者都不能代替在真实浏览器里的人工验证。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# 这些关键字之后出现的 / 是正则字面量的开头，出现的 [ 是数组字面量的开头。
_EXPRESSION_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "do",
        "else",
        "yield",
        "await",
        "default",
        "extends",
    }
)
_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%<>~^")

JS_KEYWORDS = frozenset(
    {
        "async",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "function",
        "if",
        "in",
        "instanceof",
        "let",
        "new",
        "null",
        "of",
        "return",
        "static",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "yield",
    }
)


class JsLexError(ValueError):
    """字面量或注释没有闭合：无法可靠地区分代码与文本。"""


def _blank(text: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in text)


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def _quoted_end(source: str, start: int, quote: str) -> int:
    """source[start] 是开引号；返回闭引号之后的位置。"""
    i = start + 1
    while i < len(source):
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        if ch == "\n":
            break
        i += 1
    raise JsLexError(f"第 {source.count(chr(10), 0, start) + 1} 行的字符串没有闭合")


def _template_chunk_end(source: str, start: int) -> tuple[int, bool]:
    """从模板字面量内部的 start 开始，返回（结束位置，是否遇到 ${）。

    遇到闭合反引号时，结束位置是反引号之后；遇到 ${ 时，结束位置是 ${ 之后。
    """
    i = start
    while i < len(source):
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            return i + 1, False
        if ch == "$" and source.startswith("${", i):
            return i + 2, True
        i += 1
    raise JsLexError("模板字面量没有闭合")


def _regex_end(source: str, start: int) -> int:
    """source[start] 是正则字面量开头的 /；返回标志位之后的位置。"""
    i = start + 1
    in_class = False
    while i < len(source):
        ch = source[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "\n":
            break
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "/":
            i += 1
            while i < len(source) and _is_ident_char(source[i]):
                i += 1
            return i
        i += 1
    raise JsLexError(f"第 {source.count(chr(10), 0, start) + 1} 行的正则字面量没有闭合")


def strip_js(source: str) -> str:
    """去掉注释，清空字符串 / 模板 / 正则字面量的内容；输出与输入等长、换行位置不变。"""
    out: list[str] = []
    i = 0
    n = len(source)
    depth = 0  # 当前花括号深度
    template_depths: list[int] = []  # 每个尚未闭合的 ${ 进入时的深度
    prev = ""  # 最近一个非空白代码字符
    prev2 = ""  # 再往前一个非空白代码字符（用来认出 ++ / --）
    word = ""  # 最近一个代码记号若是标识符，则为它
    while i < n:
        ch = source[i]
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end == -1 else end
            out.append(_blank(source[i:end]))
            i = end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end == -1:
                raise JsLexError("块注释没有闭合")
            out.append(_blank(source[i : end + 2]))
            i = end + 2
            continue
        if ch in "'\"":
            end = _quoted_end(source, i, ch)
            out.append(ch + _blank(source[i + 1 : end - 1]) + ch)
            i, prev, prev2, word = end, ch, "", ""
            continue
        if ch == "`" or (ch == "}" and template_depths and template_depths[-1] == depth - 1):
            if ch == "}":
                template_depths.pop()
                depth -= 1
            end, entered = _template_chunk_end(source, i + 1)
            closing = "${" if entered else "`"
            out.append(ch + _blank(source[i + 1 : end - len(closing)]) + closing)
            if entered:
                template_depths.append(depth)
                depth += 1
                prev, word = "{", ""
            else:
                prev, word = "`", ""
            prev2 = ""
            i = end
            continue
        if ch == "/" and (prev in {")", "}"} or (prev in {"+", "-"} and prev2 == prev)):
            # ) 或 } 之后、++ / -- 之后的 /，靠前一个记号分不清是除号还是正则字面量。
            # 判错会把真正的代码当成字面量清空、躲过后面的检查，所以宁可报错，要求改写。
            line = source.count("\n", 0, i) + 1
            raise JsLexError(f"第 {line} 行的 / 无法可靠判断是除号还是正则字面量，请改写")
        if ch == "/" and (
            prev == "" or prev in _REGEX_PRECEDERS or (word and word in _EXPRESSION_KEYWORDS)
        ):
            end = _regex_end(source, i)
            body_end = source.rindex("/", i, end)
            # 标志位（如 g、i）也清空：它们不是标识符。
            out.append(
                "/" + _blank(source[i + 1 : body_end]) + "/" + _blank(source[body_end + 1 : end])
            )
            i, prev, prev2, word = end, "/", "", ""
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        out.append(ch)
        if not ch.isspace():
            if _is_ident_char(ch):
                adjacent = i > 0 and _is_ident_char(source[i - 1])
                word = word + ch if adjacent else ch
            else:
                word = ""
            prev2, prev = prev, ch
        i += 1
    if template_depths:
        raise JsLexError("模板字面量里的 ${ 没有闭合")
    return "".join(out)


# --------------------------------------------------------------------------- 记号


@dataclass(frozen=True)
class Identifier:
    name: str
    line: int
    member: bool  # 前面是 . 或 ?.（成员访问），不含展开运算符 ...
    key: bool  # 对象字面量的键：前面是 { 或 ,，后面是 :
    dotted: bool  # 后面紧跟 . 或 ?.（作为成员访问的对象）
    start: int  # 在代码里的起始位置


_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def identifiers(code: str) -> list[Identifier]:
    """列出去掉字面量后的代码里的全部标识符（含关键字），按出现顺序。"""
    found: list[Identifier] = []
    for match in _IDENT.finditer(code):
        start, end = match.span()
        if start > 0 and _is_ident_char(code[start - 1]):
            continue  # 数字字面量的一部分，如 1e5、0x1F
        before = code[:start].rstrip()
        after = code[end:].lstrip()
        member = before.endswith(".") and not before.endswith("...")
        key = (before.endswith(("{", ","))) and after.startswith(":")
        dotted = after.startswith(".") and not after.startswith("...")
        dotted = dotted or after.startswith("?.")
        line = code.count("\n", 0, start) + 1
        found.append(Identifier(match.group(0), line, member, key, dotted, start))
    return found


_DECLARATION = re.compile(r"\b(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)")
_DESTRUCTURING = re.compile(r"\b(?:const|let|var)\s*[\{\[]([^\}\]]*)[\}\]]")
_PARAMETER_LISTS = re.compile(
    r"\bfunction\b\s*[\w$]*\s*\(([^()]*)\)|\(([^()]*)\)\s*=>|\bcatch\s*\(([^()]*)\)"
)
# 类或对象里的方法定义：name(a, b) {。if / for / while 等语句形似，需排除。
_METHOD_DEFINITION = re.compile(
    r"(?:^|[{};,])\s*(?:async\s+)?(?:static\s+)?([A-Za-z_$][\w$]*)\s*\(([^()]*)\)\s*\{",
    re.MULTILINE,
)
_STATEMENT_KEYWORDS = frozenset({"if", "for", "while", "switch", "catch", "with", "function"})
_SINGLE_ARROW_PARAMETER = re.compile(r"([A-Za-z_$][\w$]*)\s*=>")
_PARAMETER = re.compile(r"^\s*(?:\.\.\.)?([A-Za-z_$][\w$]*)\s*(?:=.*)?$", re.DOTALL)


def _parameters(text: str) -> Iterable[str]:
    for part in text.split(","):
        match = _PARAMETER.match(part)
        if match and match.group(1) not in JS_KEYWORDS:
            yield match.group(1)


def declared_names(code: str) -> set[str]:
    """近似收集代码里声明过的名字（变量、函数、类、参数）。不区分作用域。"""
    names = {match.group(1) for match in _DECLARATION.finditer(code)}
    for match in _DESTRUCTURING.finditer(code):
        names.update(_parameters(match.group(1).replace(":", ",")))
    for match in _PARAMETER_LISTS.finditer(code):
        for group in match.groups():
            if group is not None:
                names.update(_parameters(group))
    for match in _METHOD_DEFINITION.finditer(code):
        if match.group(1) not in _STATEMENT_KEYWORDS:
            names.update(_parameters(match.group(2)))
    names.update(match.group(1) for match in _SINGLE_ARROW_PARAMETER.finditer(code))
    return names - JS_KEYWORDS


# --------------------------------------------------------------------------- 结构检查


@dataclass(frozen=True)
class Finding:
    check_id: str
    line: int
    detail: str


# 只能作为成员访问的对象出现（后面必须紧跟 .），不能当作值传递、赋给别名或放进函数参数。
BARE_FORBIDDEN_OBJECTS = frozenset({"window", "document", "location", "chrome", "this"})
# 指向全局对象或其他窗口的别名：任何形式都不允许出现。
GLOBAL_ALIASES = frozenset({"globalThis", "self", "frames", "opener", "parent", "top"})
# 反射、代理、动态代码与模块加载：拿到任意属性或执行任意文本的通用手段。
REFLECTION_NAMES = frozenset(
    {
        "Reflect",
        "Proxy",
        "Function",
        "eval",
        "arguments",
        "import",
        "require",
        "with",
        "__proto__",
        "prototype",
        "getPrototypeOf",
        "setPrototypeOf",
        "getOwnPropertyDescriptor",
        "getOwnPropertyDescriptors",
        "getOwnPropertyNames",
        "getOwnPropertySymbols",
        "defineProperty",
        "defineProperties",
        "__defineGetter__",
        "__defineSetter__",
        "__lookupGetter__",
        "__lookupSetter__",
    }
)
# 把编码过的文本还原成字符串的手段：配合计算属性或动态代码即可藏起接口名。
DECODING_NAMES = frozenset(
    {
        "atob",
        "btoa",
        "fromCharCode",
        "fromCodePoint",
        "unescape",
        "escape",
        "decodeURI",
        "decodeURIComponent",
        "TextDecoder",
    }
)
# 只在作为成员访问时才算解码手段的名字（String.raw）；同名的局部变量不算。
DECODING_MEMBERS = frozenset({"raw"})


def _computed_brackets(code: str) -> list[Finding]:
    findings: list[Finding] = []
    for index, ch in enumerate(code):
        if ch != "[":
            continue
        line = code.count("\n", 0, index) + 1
        close = _matching_bracket(code, index)
        inner = code[index + 1 : close]
        before = code[:index].rstrip()
        after = code[close + 1 :].lstrip()
        previous = before[-1:] if before else ""
        word_match = re.search(r"[A-Za-z_$][\w$]*$", before)
        previous_word = word_match.group(0) if word_match else ""
        is_member = previous == "." or (
            previous != "" and (_is_ident_char(previous) or previous in ")]")
        )
        if is_member and previous_word in _EXPRESSION_KEYWORDS:
            is_member = False
        if is_member:
            if not re.fullmatch(r"\s*\d+\s*", inner):
                findings.append(Finding("indirect.computed-access", line, f"[{inner.strip()}]"))
        elif previous in {"{", ","} and after.startswith(":"):
            findings.append(Finding("indirect.computed-key", line, f"[{inner.strip()}]:"))
        elif previous in {"{", "}", ";", ","} and after.startswith("("):
            findings.append(Finding("indirect.computed-key", line, f"[{inner.strip()}]("))
    return findings


def _matching_bracket(code: str, start: int) -> int:
    level = 0
    for index in range(start, len(code)):
        if code[index] == "[":
            level += 1
        elif code[index] == "]":
            level -= 1
            if level == 0:
                return index
    raise JsLexError(f"第 {code.count(chr(10), 0, start) + 1} 行的方括号没有闭合")


def _method_name_starts(code: str) -> set[int]:
    """类或对象里方法定义的名字位置（如 constructor(code, message) { 中的 constructor）。"""
    return {
        match.start(1)
        for match in _METHOD_DEFINITION.finditer(code)
        if match.group(1) not in _STATEMENT_KEYWORDS
    }


def _free_tokens(code: str, tokens: list[Identifier]) -> list[Identifier]:
    """未在本段代码里声明、也不是关键字、对象键或方法名的标识符：即对全局名的引用。"""
    declared = declared_names(code)
    methods = _method_name_starts(code)
    return [
        token
        for token in tokens
        if not token.member
        and not token.key
        and token.start not in methods
        and token.name not in JS_KEYWORDS
        and token.name not in declared
    ]


def used_names(source: str) -> tuple[set[str], set[str]]:
    """返回（用到的成员名，用到的全局名）。"""
    code = strip_js(source)
    tokens = identifiers(code)
    members = {token.name for token in tokens if token.member}
    return members, {token.name for token in _free_tokens(code, tokens)}


def analyze(
    source: str,
    *,
    allowed_members: frozenset[str] | None = None,
    allowed_globals: frozenset[str] | None = None,
) -> list[Finding]:
    """对一段 JS 做结构检查。白名单传 None 时不做对应检查。"""
    code = strip_js(source)
    findings = _computed_brackets(code)
    for number, line in enumerate(code.splitlines(), start=1):
        odd = re.search(r"[\\]|[^\x00-\x7f]", line)
        if odd:
            findings.append(Finding("indirect.escape-in-code", number, odd.group(0)))
        # 经典脚本把 <!-- 和行首的 --> 当作单行注释，这里的词法处理却当作代码：两边理解不一致，
        # 可能让后面的内容被误当成字面量而躲过检查，所以直接禁止。
        if "<!--" in line or "-->" in line:
            findings.append(Finding("indirect.html-comment", number, line.strip()))
    tokens = identifiers(code)
    free = {token.start for token in _free_tokens(code, tokens)}
    for token in tokens:
        name = token.name
        if token.key:
            continue
        if not token.member and name in BARE_FORBIDDEN_OBJECTS and not token.dotted:
            findings.append(Finding("indirect.global-alias", token.line, name))
        if not token.member and name in GLOBAL_ALIASES:
            findings.append(Finding("indirect.global-alias", token.line, name))
        if name in REFLECTION_NAMES or (token.member and name == "constructor"):
            findings.append(Finding("indirect.reflection", token.line, name))
        if name in DECODING_NAMES or (token.member and name in DECODING_MEMBERS):
            findings.append(Finding("indirect.string-decode", token.line, name))
        if token.member:
            if allowed_members is not None and name not in allowed_members:
                findings.append(Finding("api.unknown-member", token.line, name))
        elif token.start in free and allowed_globals is not None and name not in allowed_globals:
            findings.append(Finding("api.unknown-global", token.line, name))
    return findings


_ASSIGNMENT = re.compile(
    r"\.\s*([A-Za-z_$][\w$]*)\s*(?:\*\*|<<|>>>|>>|&&|\|\||\?\?|[-+*/%&|^])?=(?![=>])"
)


def assignment_targets(source: str) -> list[str]:
    """列出 x.name = ... 与 x.name += ... 等形式的赋值目标 name（不含 ==、===、=>）。"""
    return _ASSIGNMENT.findall(strip_js(source))
