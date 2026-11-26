"""浏览器辅助插件（extension/）的测试。

插件只做一件事：用户主动点击后，把其在页面上选中的文字和其中的链接保存为本地文件。
这里从四个方面把关：

1. 清单：MV3、权限最小化，不声明任何自动注入、常驻脚本或站点权限；
2. 静态扫描：代码文件里不出现联网、自动操作页面、读取身份数据、指纹之类的写法；
   另对插件实际加载的三个脚本做结构检查（见 helpers_extension.py），堵住换一种写法
   绕开词表的常见路子：计算属性访问、全局对象别名、反射与动态代码、字符串解码、
   代码里的转义，并对成员名与全局名做白名单；
3. 示例文件与 FORMAT.md 约定的字段集合一致，域名全部虚构；
4. 行为：用 Node 运行 extension/tests/ 下的 JS 测试。没有 Node 时失败而不是跳过。

静态扫描的局限：它检查的是写法而不是运行时行为；词法处理是启发式的；白名单按名字
而不按所属对象或作用域判断。运行时的只读与不越界由 JS 测试里的白名单式替身补充验证，
两者都不能代替在真实浏览器里的人工验证。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, SPEC_DIR
from helpers_extension import (
    Finding,
    JsLexError,
    analyze,
    assignment_targets,
    declared_names,
    strip_js,
    used_names,
)

EXT = REPO_ROOT / "extension"
EXT_TESTS = EXT / "tests"

CODE_SUFFIXES = frozenset({".js", ".mjs", ".html", ".css"})
ALLOWED_SUFFIXES = CODE_SUFFIXES | {".json", ".md"}

EXPECTED_FILES = frozenset(
    {
        "manifest.json",
        "popup.html",
        "popup.css",
        "popup.js",
        "lib/capture.js",
        "lib/page.js",
        "tests/fakes.mjs",
        "tests/capture.test.mjs",
        "tests/page.test.mjs",
        "tests/popup.test.mjs",
        "examples/capture.example.json",
        "FORMAT.md",
        "README.md",
    }
)

JS_TEST_FILES = ("capture.test.mjs", "page.test.mjs", "popup.test.mjs")

# FORMAT.md 约定的字段集合（按任务给定的格式手工列出，不取自程序输出）。
TOP_LEVEL_FIELDS = (
    "format",
    "capture",
    "saved_at",
    "url",
    "title",
    "platform",
    "window_id",
    "query_id",
    "messages",
    "sources",
    "model",
    "environment",
    "operator",
)
ENVIRONMENT_FIELDS = (
    "entry_form",
    "entry_features",
    "logged_in",
    "history_cleared",
    "web_search",
    "region",
    "device",
)

# 弹窗里由用户手工填写的项：元素 id -> 控件类型。
FORM_CONTROLS = {
    "f-prompt": "textarea",
    "f-platform": "input",
    "f-window-id": "input",
    "f-query-id": "input",
    "f-model": "input",
    "f-entry-features": "input",
    "f-logged-in": "select",
    "f-history-cleared": "select",
    "f-web-search": "select",
    "f-region": "input",
    "f-device": "input",
    "f-operator": "input",
}


def _relative(path: Path) -> str:
    return path.relative_to(EXT).as_posix()


def _all_files() -> list[Path]:
    return sorted(p for p in EXT.rglob("*") if p.is_file())


def _code_files() -> list[Path]:
    return [p for p in _all_files() if p.suffix in CODE_SUFFIXES]


def _is_test_file(path: Path) -> bool:
    return EXT_TESTS in path.parents


def _read(relative: str) -> str:
    return (EXT / relative).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- 文件清单


def test_extension_contains_exactly_the_expected_files() -> None:
    """目录里只有清单、代码、文档和示例；没有图片、字体、压缩包等无法扫描的文件。"""
    names = {_relative(p) for p in _all_files()}
    assert names == EXPECTED_FILES
    assert {p.suffix for p in _all_files()} <= ALLOWED_SUFFIXES
    for path in _all_files():
        assert path.stat().st_size < 100_000, f"{_relative(path)} 过大"


def test_extension_is_not_packaged_into_the_wheel() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert all("extension" not in item for item in wheel["packages"])
    assert all("extension" not in key for key in wheel.get("force-include", {}))


# --------------------------------------------------------------------------- 清单

ALLOWED_PERMISSIONS = {"activeTab", "scripting"}
ALLOWED_MANIFEST_KEYS = {
    "manifest_version",
    "name",
    "version",
    "description",
    "permissions",
    "action",
}
# 任务点名不得出现的清单字段（另有上面的白名单兜底）。
FORBIDDEN_MANIFEST_KEYS = (
    "host_permissions",
    "optional_host_permissions",
    "optional_permissions",
    "content_scripts",
    "background",
    "externally_connectable",
    "web_accessible_resources",
    "content_security_policy",
    "declarative_net_request",
    "update_url",
    "oauth2",
    "devtools_page",
    "chrome_url_overrides",
)
FORBIDDEN_PERMISSIONS = ("webRequest", "cookies", "tabs", "downloads", "storage", "<all_urls>")


def _manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_read("manifest.json"))
    return data


def _walk(value: Any) -> list[tuple[str, Any]]:
    """递归列出 JSON 中的全部（键，值）对。"""
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append((key, item))
            found.extend(_walk(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk(item))
    return found


def test_manifest_is_mv3_with_minimal_permissions() -> None:
    manifest = _manifest()
    assert manifest["manifest_version"] == 3
    permissions = manifest["permissions"]
    assert isinstance(permissions, list)
    assert len(permissions) == len(set(permissions))
    assert set(permissions) <= ALLOWED_PERMISSIONS
    # 两项都要用到：activeTab 让用户的点击临时授权当前标签页，scripting 用来执行只读函数。
    assert set(permissions) == ALLOWED_PERMISSIONS
    for name in FORBIDDEN_PERMISSIONS:
        assert name not in permissions


def test_manifest_declares_nothing_beyond_the_popup() -> None:
    manifest = _manifest()
    for key in FORBIDDEN_MANIFEST_KEYS:
        assert key not in manifest, f"清单不得声明 {key}"
    assert set(manifest) <= ALLOWED_MANIFEST_KEYS
    assert set(manifest["action"]) <= {"default_popup", "default_title"}
    assert manifest["action"]["default_popup"] == "popup.html"
    assert (EXT / manifest["action"]["default_popup"]).is_file()
    assert re.fullmatch(r"\d+(\.\d+){0,3}", manifest["version"])
    nested_keys = {key for key, _ in _walk(manifest)}
    assert not nested_keys & {"matches", "js", "css", "service_worker", "scripts", "run_at"}


def test_manifest_text_is_chinese_and_states_the_boundary() -> None:
    manifest = _manifest()
    name, description = manifest["name"], manifest["description"]
    assert re.search(r"[一-鿿]", name)
    assert "用户点击后保存当前所选内容" in description
    assert "不自动提问" in description
    assert "不自动翻页" in description
    # 浏览器对清单文字的长度限制。
    assert len(name) <= 45
    assert len(description) <= 132
    assert manifest["action"]["default_title"]


def test_manifest_contains_no_site_pattern_or_domain() -> None:
    raw = _read("manifest.json")
    assert "://" not in raw
    assert "*" not in raw
    for _, value in _walk(_manifest()):
        if isinstance(value, str):
            assert not REAL_DOMAIN.search(value), f"清单里出现了像域名的内容：{value}"


# --------------------------------------------------------------------------- 静态扫描


@dataclass(frozen=True)
class Rule:
    rule_id: str
    pattern: str
    suffixes: frozenset[str] = CODE_SUFFIXES
    skip_tests: bool = False

    def applies_to(self, suffix: str, *, is_test: bool) -> bool:
        return suffix in self.suffixes and not (self.skip_tests and is_test)


_SCRIPT = frozenset({".js", ".mjs", ".html"})
_STYLE = frozenset({".css", ".html"})

# 词表：代码文件里不得出现的接口与写法。扫描对象包含注释，所以注释里也不能写这些词。
RULES: tuple[Rule, ...] = (
    # —— 联网 ——
    # 不加词边界：fetchLater、prefetch 等同样算联网。
    Rule("network.fetch", r"fetch"),
    Rule("network.xhr", r"XMLHttpRequest"),
    Rule("network.socket", r"WebSocket|WebTransport|EventSource|RTCPeerConnection"),
    Rule("network.beacon", r"sendBeacon"),
    Rule(
        "network.other",
        r"\b(?:Image|Audio|Worker|SharedWorker|BroadcastChannel|MessageChannel)\s*\("
        r"|serviceWorker|postMessage|\bping\b|preconnect|preload",
    ),
    Rule("network.remote-code", r"importScripts|\bimport\s*\("),
    Rule("network.css-remote", r"@import|\burl\s*\(|image-set|@font-face", suffixes=_STYLE),
    # 测试文件里的虚构地址另由“域名全部虚构”的测试把关。
    Rule("network.url-literal", r"https?://|\bwss?://", skip_tests=True),
    # —— 对页面的写操作与自动操作 ——
    Rule("automation.click", r"\.click\s*\("),
    Rule(
        "automation.event",
        r"dispatchEvent|initEvent|new\s+Event\b"
        r"|\b(?:Keyboard|Mouse|Pointer|Input|Custom|Touch|Wheel)Event\b",
    ),
    Rule("automation.submit", r"submit"),
    Rule("automation.scroll", r"scroll"),
    Rule(
        "automation.timer",
        r"setInterval|setTimeout|requestAnimationFrame|requestIdleCallback",
    ),
    Rule("automation.observer", r"\w*Observer\b"),
    Rule(
        "automation.navigate",
        r"\blocation\s*\.\s*(?:assign|replace|reload)\b"
        r"|\blocation\b(?:\s*\.\s*href)?\s*=(?!=)"
        r"|\bhistory\s*\.|\bwindow\s*\.\s*open\b",
    ),
    Rule(
        "page-write.html",
        r"innerHTML|outerHTML|insertAdjacentHTML|document\s*\.\s*write|execCommand",
    ),
    Rule("page-write.value", r"\.value\s*=(?!=)", skip_tests=True),
    Rule("page-write.attribute", r"setAttribute|\.src\s*=(?!=)|srcdoc"),
    # 弹窗里只创建列表项（用来逐条显示链接）；测试替身里有同名方法的定义，不扫。
    Rule(
        "page-write.create-element",
        r"createElement\s*\((?!\s*\"li\"\s*\))",
        skip_tests=True,
    ),
    # Function( 只匹配大写开头：与 function 关键字区分。
    Rule("dynamic-code", r"\beval\b|new\s+Function\b|(?-i:\bFunction\s*\()"),
    Rule(
        "injection.scope",
        r"allFrames|\bworld\s*:|\bfiles\s*:|insertCSS|registerContentScripts",
    ),
    # —— 身份与本地存储 ——
    Rule("storage.cookie", r"cookie"),
    Rule("storage.web", r"localStorage|sessionStorage|indexedDB|\bcaches\b|CacheStorage"),
    # —— 指纹与反检测 ——
    Rule("fingerprint.navigator", r"navigator"),
    Rule(
        "fingerprint.probe",
        r"webdriver|userAgent|canvas|webgl|AudioContext|\bscreen\b"
        r"|devicePixelRatio|hardwareConcurrency|deviceMemory|getBattery",
    ),
)

_COMPILED = {rule.rule_id: re.compile(rule.pattern, re.IGNORECASE) for rule in RULES}


def scan(text: str, suffix: str, *, is_test: bool = False) -> list[tuple[str, int, str]]:
    """返回（规则编号，行号，该行内容）列表。"""
    findings: list[tuple[str, int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule in RULES:
            if rule.applies_to(suffix, is_test=is_test) and _COMPILED[rule.rule_id].search(line):
                findings.append((rule.rule_id, number, line.strip()))
    return findings


def test_code_files_contain_no_forbidden_api_or_pattern() -> None:
    files = _code_files()
    assert {_relative(p) for p in files} == {
        name for name in EXPECTED_FILES if Path(name).suffix in CODE_SUFFIXES
    }
    problems: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for rule_id, number, line in scan(text, path.suffix, is_test=_is_test_file(path)):
            problems.append(f"{_relative(path)}:{number} [{rule_id}] {line}")
    assert problems == []


# 每条规则至少配一个应被检出的写法，证明词表确实起作用（而不是恒为“未发现”）。
OFFENDING_SNIPPETS: tuple[tuple[str, str, str], ...] = (
    ("network.fetch", ".js", 'const r = await fetch("/collect");'),
    ("network.xhr", ".js", "const x = new XMLHttpRequest();"),
    ("network.socket", ".js", 'const s = new WebSocket("wss://collector.example/s");'),
    ("network.socket", ".js", "const es = new EventSource(endpoint);"),
    ("network.fetch", ".js", "fetchLater(request);"),
    ("network.fetch", ".html", '<link rel="prefetch" href="next.html">'),
    ("network.beacon", ".js", "navigator.sendBeacon(endpoint, body);"),
    ("network.other", ".js", "new Image().src = beacon;"),
    ("network.other", ".js", 'const worker = new Worker("w.js");'),
    ("network.other", ".js", 'parent.postMessage(data, "*");'),
    ("network.other", ".html", '<a href="#" ping="track.html">x</a>'),
    ("network.remote-code", ".js", 'importScripts("remote.js");'),
    ("network.remote-code", ".js", "const m = await import(moduleName);"),
    ("network.css-remote", ".css", '@import "theme.css";'),
    ("network.css-remote", ".css", "body { background: url(bg.png); }"),
    ("network.css-remote", ".html", '<p style="background:url(a.png)">x</p>'),
    ("network.css-remote", ".css", "@font-face { font-family: x; }"),
    ("network.css-remote", ".css", 'body { background: image-set("a.png" 1x); }'),
    ("network.url-literal", ".js", 'const endpoint = "https://collector.example/v1";'),
    ("network.url-literal", ".html", '<script src="http://cdn.example/a.js"></script>'),
    ("network.url-literal", ".css", "/* wss://collector.example */"),
    ("automation.click", ".js", "nextButton.click();"),
    ("automation.click", ".js", "el .click ();".replace(" .", ".")),
    ("automation.event", ".js", 'input.dispatchEvent(new Event("input"));'),
    ("automation.event", ".js", 'const e = new KeyboardEvent("keydown", { key: "Enter" });'),
    ("automation.submit", ".js", "form.requestSubmit();"),
    ("automation.submit", ".html", '<button type="submit">发送</button>'),
    ("automation.scroll", ".js", "window.scrollTo(0, document.body.scrollHeight);"),
    ("automation.scroll", ".js", "last.scrollIntoView();"),
    ("automation.scroll", ".css", "main { overflow-y: scroll; }"),
    ("automation.timer", ".js", "setInterval(poll, 1000);"),
    ("automation.timer", ".js", "setTimeout(nextPage, 500);"),
    ("automation.observer", ".js", "new MutationObserver(collect).observe(root, options);"),
    ("automation.observer", ".js", "new IntersectionObserver(loadMore);"),
    ("automation.navigate", ".js", "location.href = nextUrl;"),
    ("automation.navigate", ".js", "location.assign(nextUrl);"),
    ("automation.navigate", ".js", "history.back();"),
    ("automation.navigate", ".js", "window.open(link);"),
    ("page-write.html", ".js", "box.innerHTML = html;"),
    ("page-write.html", ".js", 'document.execCommand("insertText", false, question);'),
    ("page-write.value", ".js", "textarea.value = question;"),
    ("page-write.attribute", ".js", 'el.setAttribute("src", u);'),
    ("page-write.attribute", ".js", "frame.src = u;"),
    ("page-write.create-element", ".js", 'document.createElement("script");'),
    ("page-write.create-element", ".js", "document.createElement(tag);"),
    ("dynamic-code", ".js", "eval(code);"),
    ("dynamic-code", ".js", "const f = new Function(body);"),
    ("dynamic-code", ".js", 'const g = Function("return this")();'),
    (
        "injection.scope",
        ".js",
        "chrome.scripting.executeScript({ target: { tabId, allFrames: true } });",
    ),
    ("injection.scope", ".js", 'const options = { world: "MAIN" };'),
    ("injection.scope", ".js", 'const options = { files: ["inject.js"] };'),
    ("storage.cookie", ".js", "const c = document.cookie;"),
    ("storage.cookie", ".js", "chrome.cookies.getAll({});"),
    ("storage.web", ".js", 'const token = localStorage.getItem("token");'),
    ("storage.web", ".js", "sessionStorage.clear();"),
    ("storage.web", ".js", 'const cache = await caches.open("v1");'),
    ("fingerprint.navigator", ".js", "const lang = navigator.language;"),
    ("fingerprint.probe", ".js", 'Object.defineProperty(n, "webdriver", { get: () => false });'),
    ("fingerprint.probe", ".js", 'const gl = el.getContext("webgl");'),
    ("fingerprint.probe", ".js", "const w = screen.width;"),
)


@pytest.mark.parametrize(("rule_id", "suffix", "snippet"), OFFENDING_SNIPPETS)
def test_scanner_flags_offending_snippet(rule_id: str, suffix: str, snippet: str) -> None:
    flagged = {found for found, _, _ in scan(snippet, suffix)}
    assert rule_id in flagged


def test_every_rule_has_an_offending_snippet() -> None:
    assert {rule.rule_id for rule in RULES} == {rule_id for rule_id, _, _ in OFFENDING_SNIPPETS}
    assert len({rule.rule_id for rule in RULES}) == len(RULES)


def test_scanner_accepts_the_legitimate_read_only_patterns() -> None:
    """只读写法不应被误报：读取选区、读取表单值、比较运算、测试里的虚构地址。"""
    harmless = (
        (".js", "const selection = window.getSelection();", False),
        (".js", "const text = field.value;", False),
        (".js", 'if (field.value === "") { return null; }', False),
        (".js", "return { url: location.href, title: document.title };", False),
        (".js", "const parsed = new URL(raw);", False),
        (".js", 'button.addEventListener("click", onRead);', False),
        (".js", "const history_cleared = form.history_cleared;", False),
        (".mjs", 'const PAGE_URL = "https://chat.engine-a.example/share/abc";', True),
        (".mjs", 'element("f-prompt").value = "提问";', True),
        (".css", "pre { overflow: auto; }", False),
        (".js", 'const item = document.createElement("li");', False),
        (".js", "const parsed = new URL(url);", False),
        (".js", "var GeoCapture = (function () {", False),
    )
    for suffix, line, is_test in harmless:
        assert scan(line, suffix, is_test=is_test) == [], line


ALLOWED_CHROME_CALLS = {"chrome.tabs.query", "chrome.scripting.executeScript"}
_CHROME_REFERENCE = re.compile(r"\bchrome\b((?:\s*\.\s*[A-Za-z_$][\w$]*)*)")


def test_only_two_extension_api_calls_are_used() -> None:
    """扩展接口只用两个：查询当前活动标签页、在其中执行只读函数。且都只出现在 popup.js。"""
    used: dict[str, list[str]] = {}
    for path in _code_files():
        if _is_test_file(path):
            continue
        text = path.read_text(encoding="utf-8")
        references = [
            "chrome" + re.sub(r"\s+", "", match.group(1))
            for match in _CHROME_REFERENCE.finditer(text)
        ]
        if references:
            used[_relative(path)] = references
        assert not re.search(r"\bbrowser\s*\.", text), f"{_relative(path)} 使用了 browser.* 接口"
    assert set(used) == {"popup.js"}
    assert sorted(used["popup.js"]) == sorted(ALLOWED_CHROME_CALLS)


def test_pure_logic_modules_do_not_touch_the_popup_or_extension_api() -> None:
    capture = _read("lib/capture.js")
    assert not re.search(r"\b(?:chrome|document|window|location)\b", capture)
    page = _read("lib/page.js")
    assert not re.search(r"\bchrome\b", page)
    # 页面读取函数对页面对象只有三处读取：选区、标题、地址。
    assert re.findall(r"\bwindow\s*\.\s*(\w+)", page) == ["getSelection"]
    assert re.findall(r"\bdocument\s*\.\s*(\w+)", page) == ["title"]
    assert re.findall(r"\blocation\s*\.\s*(\w+)", page) == ["href"]


def test_id_pattern_matches_the_contract() -> None:
    """插件对编号的校验与观测契约里的 id 写法一致，导出的编号才能被导入。"""
    schema = json.loads((SPEC_DIR / "observation.schema.json").read_text(encoding="utf-8"))
    pattern = schema["$defs"]["id"]["pattern"]
    assert pattern == "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    assert f"/{pattern}/" in _read("lib/capture.js")


# --------------------------------------------------------------------------- 结构检查（防绕过）

# 插件实际加载的脚本：popup.html 引用的三个文件。extension/tests/ 下的文件只在 Node 里运行，
# 不被清单或弹窗引用（见 test_popup_html_references_only_local_files）。
PRODUCTION_SCRIPTS = ("lib/page.js", "lib/capture.js", "popup.js")

# 成员名白名单（按文件，必须与实际使用完全一致），逐项确认过：只读，或只写弹窗自己的元素；
# 不联网、不写页面、不读身份数据。代码新用一个成员就必须在这里登记并复核，不再使用的也要删掉。
_SELECTION_FIELDS = frozenset({"ok", "text", "links", "url", "title"})
_FORM_FIELDS = frozenset(
    {
        "prompt",
        "platform",
        "window_id",
        "query_id",
        "model",
        "entry_features",
        "logged_in",
        "history_cleared",
        "web_search",
        "region",
        "device",
        "operator",
    }
)
ALLOWED_MEMBERS: dict[str, frozenset[str]] = {
    # 页面读取函数：只碰选区、选区内的链接、页面标题与地址。
    "lib/page.js": frozenset(
        {
            "getSelection",
            "rangeCount",
            "isCollapsed",
            "toString",
            "trim",
            "getRangeAt",
            "commonAncestorContainer",
            "nodeType",
            "parentElement",
            "closest",
            "querySelectorAll",
            "intersectsNode",
            "href",
            "textContent",
            "title",
            "push",
            "exports",
        }
    ),
    # 纯逻辑：数据字段、字符串、数组、Map、URL 解析（只读 protocol）与 JSON 序列化。
    "lib/capture.js": _SELECTION_FIELDS
    | _FORM_FIELDS
    | {
        "environment",
        "saved_at",
        "sources",
        "name",
        "code",
        "trim",
        "test",
        "replace",
        "slice",
        "toLowerCase",
        "includes",
        "toISOString",
        "getTime",
        "isFinite",
        "isArray",
        "has",
        "get",
        "set",
        "push",
        "map",
        "join",
        "protocol",
        "stringify",
        "exports",
    },
    # 弹窗：读写弹窗自己的元素，调用两个扩展接口，生成本地文件地址。
    "popup.js": _SELECTION_FIELDS
    | {
        "sources",
        "ignored",
        "saved_at",
        "getElementById",
        "createElement",
        "addEventListener",
        "preventDefault",
        "value",
        "textContent",
        "hidden",
        "href",
        "download",
        "replaceChildren",
        "tabs",
        "query",
        "scripting",
        "executeScript",
        "id",
        "result",
        "length",
        "isArray",
        "message",
        "push",
        "join",
        "createObjectURL",
        "revokeObjectURL",
        "readSelection",
        "CaptureError",
        "NO_SELECTION_HINT",
        "buildCapture",
        "dedupeLinks",
        "captureWarnings",
        "buildFilename",
        "serializeCapture",
    },
}
# 未在本文件声明的名字（全局名）白名单，同样必须与实际使用完全一致。
ALLOWED_GLOBALS: dict[str, frozenset[str]] = {
    "lib/page.js": frozenset({"window", "document", "location", "String", "module"}),
    "lib/capture.js": frozenset(
        {"Error", "URL", "Map", "Number", "Array", "JSON", "module", "undefined"}
    ),
    "popup.js": frozenset(
        {
            "document",
            "chrome",
            "URL",
            "Blob",
            "Date",
            "Array",
            "String",
            "GeoCapture",
            "GeoCapturePage",
        }
    ),
}
# 弹窗里允许写入的属性：文字只经 textContent 写入；hidden 控制显示；href / download 只用于保存链接。
ALLOWED_ASSIGNMENTS: dict[str, frozenset[str]] = {
    "lib/page.js": frozenset({"exports"}),
    "lib/capture.js": frozenset({"name", "code", "exports"}),
    "popup.js": frozenset({"textContent", "hidden", "href", "download"}),
}


def _analyze_production(relative: str) -> list[Finding]:
    return analyze(
        _read(relative),
        allowed_members=ALLOWED_MEMBERS[relative],
        allowed_globals=ALLOWED_GLOBALS[relative],
    )


def test_production_scripts_are_exactly_what_the_popup_loads() -> None:
    script_sources = [attrs.get("src") for tag, attrs in _popup().tags if tag == "script"]
    assert sorted(str(src) for src in script_sources) == sorted(PRODUCTION_SCRIPTS)
    assert set(ALLOWED_MEMBERS) == set(ALLOWED_GLOBALS) == set(PRODUCTION_SCRIPTS)


@pytest.mark.parametrize("relative", PRODUCTION_SCRIPTS)
def test_production_script_has_no_indirect_access(relative: str) -> None:
    findings = _analyze_production(relative)
    assert [f"{relative}:{f.line} [{f.check_id}] {f.detail}" for f in findings] == []


@pytest.mark.parametrize("relative", PRODUCTION_SCRIPTS)
def test_whitelists_match_actual_usage_exactly(relative: str) -> None:
    """白名单不留没用到的项：多出的名字等于悄悄放宽了检查。"""
    used_members, used_globals = used_names(_read(relative))
    assert used_members == ALLOWED_MEMBERS[relative]
    assert used_globals == ALLOWED_GLOBALS[relative]


@pytest.mark.parametrize("relative", PRODUCTION_SCRIPTS)
def test_production_script_writes_only_allowed_properties(relative: str) -> None:
    assert set(assignment_targets(_read(relative))) <= ALLOWED_ASSIGNMENTS[relative]


def test_popup_sets_link_targets_only_to_a_local_object_url() -> None:
    """保存链接的地址只会是占位的 # 或 Blob 生成的本地地址。"""
    code = strip_js(_read("popup.js"))
    source = _read("popup.js")
    href_values = [
        source[m.start(1) : m.end(1)].strip()
        for m in re.finditer(r"\.href\s*=(?![=>])([^;]*);", code)
    ]
    assert href_values
    assert set(href_values) <= {'"#"', "objectUrl"}
    object_url_values = [
        source[m.start(1) : m.end(1)].strip()
        for m in re.finditer(r"\bobjectUrl\s*=(?![=>])([^;]*);", code)
    ]
    assert set(object_url_values) == {"null", "URL.createObjectURL(blob)"}


def test_popup_calls_the_two_extension_apis_in_the_fixed_shape() -> None:
    """只查询当前窗口的活动标签页；只在该标签页的主框架执行固定的只读函数，不带文件或参数。"""
    source = _read("popup.js")
    code = strip_js(source)
    assert source.count("chrome.") == 2
    query = [
        re.sub(r"\s+", "", source[m.start(1) : m.end(1)])
        for m in re.finditer(r"chrome\.tabs\.query\(([^()]*)\)", code)
    ]
    assert query == ["{active:true,currentWindow:true}"]
    injection = [
        re.sub(r"\s+", "", source[m.start(1) : m.end(1)]).replace(",}", "}")
        for m in re.finditer(r"chrome\.scripting\.executeScript\(([^()]*)\)", code)
    ]
    assert injection == ["{target:{tabId:tab.id},func:GeoCapturePage.readSelection}"]


def test_only_click_listeners_on_the_two_popup_controls() -> None:
    """只响应用户在弹窗里的两次点击；页面读取函数和纯逻辑模块不登记任何监听。"""
    popup = strip_js(_read("popup.js"))
    source = _read("popup.js")
    calls = [
        source[m.start() : m.end()] for m in re.finditer(r"addEventListener\(\s*\"[^\"]*\"", popup)
    ]
    assert calls == ['addEventListener("click"', 'addEventListener("click"']
    for relative in ("lib/page.js", "lib/capture.js"):
        assert "addEventListener" not in _read(relative)
        assert "onclick" not in _read(relative).lower()


# 每项结构检查至少配一个应被检出的写法，证明检查确实起作用。
_BACKSLASH = chr(0x5C)
OFFENDING_STRUCTURES: tuple[tuple[str, str], ...] = (
    ("indirect.computed-access", "window['fe' + 'tch']('/collect');"),
    ("indirect.computed-access", 'self["XMLHttp" + "Request"];'),
    ("indirect.computed-access", "const f = document[name];"),
    ("indirect.computed-access", "api?.[key]();"),
    ("indirect.computed-access", "getApi()[k]();"),
    ("indirect.computed-access", "list[0][key];"),
    ("indirect.computed-key", "const options = { [key]: value };"),
    ("indirect.computed-key", "const options = { a: 1, [key]: value };"),
    ("indirect.global-alias", "globalThis.open(u);"),
    ("indirect.global-alias", "const w = window; w.open(u);"),
    ("indirect.global-alias", "Object.values(document);"),
    ("indirect.global-alias", "const g = (function () { return this; })();"),
    ("indirect.global-alias", "top.location;"),
    ("indirect.global-alias", "self.close();"),
    ("indirect.global-alias", "const c = chrome;"),
    ("indirect.reflection", "Reflect.get(api, name)();"),
    ("indirect.reflection", "(0, eval)(code);"),
    ("indirect.reflection", 'Function("return th" + "is")();'),
    ("indirect.reflection", '[].constructor.constructor("code")();'),
    ("indirect.reflection", "Object.getOwnPropertyDescriptor(api, name);"),
    ("indirect.reflection", 'const m = require("http");'),
    ("indirect.reflection", 'const m = import("./x.js");'),
    ("indirect.reflection", "with (api) { run(); }"),
    ("indirect.reflection", "new Proxy(target, handler);"),
    ("indirect.string-decode", 'const name = atob("ZmV0Y2g=");'),
    ("indirect.string-decode", "String.fromCharCode(102, 101, 116);"),
    ("indirect.string-decode", "const t = String.raw`x`;"),
    ("indirect.escape-in-code", f"f{_BACKSLASH}u0065tch(u);"),
    ("indirect.escape-in-code", "const 取 = api;"),
    # 经典脚本把 <!-- 当作单行注释：后面的反引号在浏览器里不算模板开头，在词法处理里却算。
    ("indirect.html-comment", "<!-- `\nwindow[k]();\n`;"),
    ("indirect.html-comment", "x = 1;\n--> y"),
    ("api.unknown-member", "document.defaultView.alert(1);"),
    ("api.unknown-member", "box.innerText = text;"),
    ("api.unknown-member", "item.insertAdjacentText('beforeend', text);"),
    ("api.unknown-global", "open(u);"),
    ("api.unknown-global", "queueMicrotask(next);"),
    # 不是方法定义的裸 constructor 指向全局对象的构造器。
    ("api.unknown-global", "const W = constructor;"),
)

# 白名单检查对照弹窗脚本的白名单。
_SNIPPET_MEMBERS = ALLOWED_MEMBERS["popup.js"]
_SNIPPET_GLOBALS = ALLOWED_GLOBALS["popup.js"]


@pytest.mark.parametrize(("check_id", "snippet"), OFFENDING_STRUCTURES)
def test_structure_check_flags_offending_snippet(check_id: str, snippet: str) -> None:
    findings = analyze(snippet, allowed_members=_SNIPPET_MEMBERS, allowed_globals=_SNIPPET_GLOBALS)
    assert check_id in {finding.check_id for finding in findings}, findings


def test_every_structure_check_has_an_offending_snippet() -> None:
    checks = {
        "indirect.computed-access",
        "indirect.computed-key",
        "indirect.global-alias",
        "indirect.reflection",
        "indirect.string-decode",
        "indirect.escape-in-code",
        "indirect.html-comment",
        "api.unknown-member",
        "api.unknown-global",
    }
    assert {check_id for check_id, _ in OFFENDING_STRUCTURES} == checks


def test_structure_checks_accept_ordinary_code() -> None:
    """数组字面量、整数下标、字符串与正则里的方括号、注释、类定义都不误报。"""
    harmless = (
        "const first = results[0];",
        "const items = [a, b];",
        "function f() { return []; }",
        "for (const x of [1, 2]) { use(x); }",
        "const ok = /^[A-Z][a-z0-9._:-]{0,3}$/.test(value);",
        "const s = 'a[b]c' + \"[window]\";",
        "const t = `${a}[x] ${b[0]}`;",
        "// window['fe' + 'tch'] 只是注释",
        "/* globalThis[name] */ const x = 1;",
        "class E extends Error { constructor(c, m) { super(m); this.code = c; } }",
        "list.replaceChildren(...items);",
        "const o = { a: 1, b: [2, 3] };",
        "const v = typeof x === 'string' ? x : null;",
        "const d = a / b / c;",
    )
    for snippet in harmless:
        assert analyze(snippet) == [], snippet


def test_structure_whitelists_track_declared_names() -> None:
    code = strip_js(
        "const a = 1; let b; var c; function d(e, f) {} class G {}"
        " const { h, i: j } = k; (l, m) => l; n => n; try {} catch (o) {}"
        " class P { method(q, r) { if (s) { return q; } } }"
    )
    names = declared_names(code)
    assert {"a", "b", "c", "d", "e", "f", "G", "h", "j", "l", "m", "n", "o", "P", "q", "r"} <= names
    # if (s) 不是方法定义，s 不算声明过。
    assert "s" not in names
    findings = analyze(
        "const x = 1; use(x, y);", allowed_members=frozenset(), allowed_globals=frozenset({"use"})
    )
    assert [(f.check_id, f.detail) for f in findings] == [("api.unknown-global", "y")]
    # 正则的标志位不是标识符。
    flags = analyze(
        "const ok = /a b/gi.test(v);",
        allowed_members=frozenset({"test"}),
        allowed_globals=frozenset({"v"}),
    )
    assert flags == []


def test_lexer_keeps_positions_and_rejects_unterminated_literals() -> None:
    source = 'const a = "x[1]"; // c\nconst b = `t${ c["k"] }t`;\nconst r = /[/]/g;'
    code = strip_js(source)
    assert len(code) == len(source)
    assert code.count("\n") == source.count("\n")
    assert "x[1]" not in code
    assert '"k"' not in code.replace(" ", "")
    assert '[" "]' in code
    for bad in ('const a = "x;\n', "const t = `abc", "/* x", "const r = /abc\n"):
        with pytest.raises(JsLexError):
            strip_js(bad)
    targets = assignment_targets('a.b = 1; c.d += 2; e.f === 3; g.h >= 4; i.j ??= "k.l = 5";')
    assert targets == ["b", "d", "j"]


def test_lexer_refuses_slashes_it_cannot_classify() -> None:
    """除号与正则分不清时报错：判错会把真正的代码当成字面量清空，从而躲过检查。"""
    ambiguous = (
        # 浏览器里 {} 是对象、/ 是除号，后面的调用会执行；当成正则就会被清空。
        "x = {} / window[k]() / 1;",
        # 浏览器里这是正则 /"/；当成除号，后面的引号就会吞掉一整段代码。
        'if (ok) /"/.test(s); window[k]();',
        "n = i++ / 2;",
        "n = i-- / 2;",
    )
    for snippet in ambiguous:
        with pytest.raises(JsLexError, match="无法可靠判断"):
            strip_js(snippet)
    # 明确的情形照常处理：开头的正则、关键字之后的正则、标识符之后的除号。
    for clear in ("/a/.test(s);", "function f() { return /a/.test(s); }", "n = a / b;"):
        strip_js(clear)


# --------------------------------------------------------------------------- HTML


class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline: list[tuple[str, str]] = []
        self.text: list[tuple[str, str]] = []
        self._stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag not in {"meta", "link", "input", "br", "hr"}:
            self._stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self._stack, f"多余的结束标签 </{tag}>"
        assert self._stack[-1] == tag, f"标签未正确闭合：期望 </{self._stack[-1]}>，遇到 </{tag}>"
        self._stack.pop()

    def handle_data(self, data: str) -> None:
        if not data.strip() or not self._stack:
            return
        current = self._stack[-1]
        if current in {"script", "style"}:
            self.inline.append((current, data))
        else:
            self.text.append((current, data.strip()))


def _popup() -> _Html:
    parser = _Html()
    parser.feed(_read("popup.html"))
    parser.close()
    assert parser._stack == []
    return parser


URL_ATTRIBUTES = ("src", "href", "action", "formaction", "data", "poster", "srcset", "ping")
FORBIDDEN_TAGS = {
    "iframe",
    "frame",
    "object",
    "embed",
    "form",
    "img",
    "picture",
    "video",
    "audio",
    "source",
    "base",
    "style",
    "svg",
}


def test_popup_html_references_only_local_files() -> None:
    popup = _popup()
    raw = _read("popup.html")
    assert "http://" not in raw
    assert "https://" not in raw
    assert popup.inline == [], "不得有内联脚本或内联样式"
    for tag, attrs in popup.tags:
        assert tag not in FORBIDDEN_TAGS, f"不得使用 <{tag}>"
        assert not [name for name in attrs if name.startswith("on")], f"<{tag}> 带内联事件处理"
        assert "style" not in attrs
        assert tag != "meta" or "http-equiv" not in attrs
        for name in URL_ATTRIBUTES:
            if name not in attrs:
                continue
            value = attrs[name] or ""
            if tag == "a" and name == "href" and value == "#":
                continue
            assert ":" not in value, f"<{tag} {name}> 不是本地相对路径：{value}"
            assert not value.startswith("/"), f"<{tag} {name}> 不是本地相对路径：{value}"
            assert ".." not in value
            assert (EXT / value).is_file(), f"<{tag} {name}> 指向不存在的文件：{value}"
    scripts = [attrs for tag, attrs in popup.tags if tag == "script"]
    assert [attrs.get("src") for attrs in scripts] == ["lib/page.js", "lib/capture.js", "popup.js"]
    links = [attrs for tag, attrs in popup.tags if tag == "link"]
    assert [(attrs.get("rel"), attrs.get("href")) for attrs in links] == [
        ("stylesheet", "popup.css")
    ]


def test_popup_form_has_every_manual_field_and_no_prefilled_value() -> None:
    popup = _popup()
    controls = {
        attrs["id"]: (tag, attrs)
        for tag, attrs in popup.tags
        if tag in {"input", "textarea", "select"}
    }
    assert {key: tag for key, (tag, _) in controls.items()} == FORM_CONTROLS
    labelled = {attrs.get("for") for tag, attrs in popup.tags if tag == "label"}
    assert set(FORM_CONTROLS) <= labelled
    for control_id, (tag, attrs) in controls.items():
        # 不预填任何值：没填就是没填，导出为 null。
        assert "value" not in attrs, f"{control_id} 不得预填默认值"
        assert "checked" not in attrs
        if tag == "input":
            assert attrs.get("type") == "text"
    assert not [text for tag, text in popup.text if tag == "textarea"]
    # 只有提问原文是必填。
    assert [key for key, (_, attrs) in controls.items() if "required" in attrs] == ["f-prompt"]


def test_tristate_selects_default_to_unknown() -> None:
    popup = _popup()
    options: dict[str, list[tuple[str | None, bool]]] = {}
    current: str | None = None
    for tag, attrs in popup.tags:
        if tag == "select":
            current = attrs["id"]
            options[str(current)] = []
        elif tag == "option":
            assert current is not None
            options[current].append((attrs.get("value"), "selected" in attrs))
    assert set(options) == {key for key, tag in FORM_CONTROLS.items() if tag == "select"}
    for select_id, items in options.items():
        # 第一项即浏览器的默认选中项：未知。不得默认选“是”或“否”。
        assert [value for value, _ in items] == ["unknown", "yes", "no"], select_id
        assert [value for value, selected in items if selected] in ([], ["unknown"])
    labels = [text for tag, text in popup.text if tag == "option"]
    assert labels == ["未知", "是", "否"] * 3


def test_popup_has_the_two_user_triggered_controls_and_states_the_boundary() -> None:
    popup = _popup()
    by_id = {attrs["id"]: (tag, attrs) for tag, attrs in popup.tags if attrs.get("id")}
    assert by_id["read-selection"][0] == "button"
    assert by_id["read-selection"][1].get("type") == "button"
    assert by_id["save-file"][0] == "a"
    assert "download" in by_id["save-file"][1]
    assert [tag for tag, _ in popup.tags].count("button") == 1
    assert [tag for tag, _ in popup.tags].count("a") == 1
    visible = " ".join(text for _, text in popup.text)
    assert "读取所选内容" in visible
    assert "保存为文件" in visible
    assert "不自动提问" in visible
    assert "不自动翻页" in visible
    assert "不联网" in visible
    # popup.html 里的每个 id 都被 popup.js 用到（没有接不上线的元素）。
    script = _read("popup.js")
    for element_id in by_id:
        assert f'"{element_id}"' in script, f"popup.js 没有用到元素 {element_id}"


# --------------------------------------------------------------------------- 示例与文档

REAL_DOMAIN = re.compile(
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|cn|net|org|ai|io|app|dev)\b", re.IGNORECASE
)
_URL = re.compile(r"\b(?:https?|wss?|ftp)://([^/\s\"'<>)`]+)", re.IGNORECASE)
FICTIONAL_SUFFIXES = (".example", ".test")


def _example() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_read("examples/capture.example.json"))
    return data


def test_example_matches_the_documented_field_set() -> None:
    example = _example()
    assert tuple(example) == (*TOP_LEVEL_FIELDS, "synthetic")
    assert example["synthetic"] is True
    assert example["format"] == "geo-verify-capture/1"
    assert example["capture"] == "browser_assist"
    assert tuple(example["environment"]) == ENVIRONMENT_FIELDS
    assert example["environment"]["entry_form"] == "web"
    for field in ("logged_in", "history_cleared", "web_search"):
        assert example["environment"][field] in (True, False, None)
    for field in ("entry_features", "region", "device"):
        value = example["environment"][field]
        assert value is None or (isinstance(value, str) and value.strip())
    for field in ("title", "platform", "window_id", "query_id", "model", "operator"):
        value = example[field]
        assert value is None or (isinstance(value, str) and value.strip())
    assert [m["role"] for m in example["messages"]] == ["user", "assistant"]
    for message in example["messages"]:
        assert tuple(message) == ("role", "content")
        assert isinstance(message["content"], str)
        assert message["content"].strip()
    assert example["sources"], "示例应至少包含一个链接"
    for source in example["sources"]:
        assert tuple(source) == ("url", "title")
        assert source["title"] is None or source["title"].strip()
    urls = [source["url"] for source in example["sources"]]
    assert len(urls) == len(set(urls))


def test_example_values_fit_the_observation_contract() -> None:
    """时间和编号的写法取自观测契约，保证示例可以被导入。"""
    schema = json.loads((SPEC_DIR / "observation.schema.json").read_text(encoding="utf-8"))
    defs = schema["$defs"]
    example = _example()
    assert re.search(defs["utc_time"]["pattern"], example["saved_at"])
    for field in ("platform", "window_id", "query_id"):
        assert re.search(defs["id"]["pattern"], example[field])
    assert example["capture"] in defs["channel"]["enum"]
    assert example["environment"]["entry_form"] in defs["entry_form"]["enum"]


def test_example_is_visibly_synthetic_and_uses_fictional_domains() -> None:
    example = _example()
    assert "虚构" in example["title"]
    hosts = [_URL.match(example["url"])] + [_URL.match(s["url"]) for s in example["sources"]]
    for match in hosts:
        assert match is not None
        assert match.group(1).endswith(".example")


def test_every_url_under_extension_is_fictional() -> None:
    """包括文档和测试在内，出现的网址只用 .example / .test；不出现看起来像真实域名的内容。"""
    for path in _all_files():
        text = path.read_text(encoding="utf-8")
        for match in _URL.finditer(text):
            host = match.group(1).lower().split(":")[0]
            assert host.endswith(FICTIONAL_SUFFIXES), f"{_relative(path)} 出现非虚构域名：{host}"
        real = REAL_DOMAIN.search(text)
        assert real is None, f"{_relative(path)} 出现疑似真实域名：{real.group(0) if real else ''}"


def test_format_doc_describes_every_field_and_rule() -> None:
    doc = _read("FORMAT.md")
    for field in (*TOP_LEVEL_FIELDS, *ENVIRONMENT_FIELDS, "synthetic", "role", "content"):
        assert f"`{field}`" in doc, f"FORMAT.md 没有说明字段 {field}"
    for phrase in (
        "geo-verify-capture/1",
        "browser_assist",
        "null",
        "保存时间",
        "examples/capture.example.json",
        "去重",
        "UTC",
    ):
        assert phrase in doc, f"FORMAT.md 缺少：{phrase}"


def test_readme_states_purpose_boundary_install_and_usage() -> None:
    readme = _read("README.md")
    for phrase in (
        "只在用户主动点击时保存其选中的内容和链接",
        "不自动提问",
        "不自动翻页",
        "不联网",
        "不绕过任何访问限制",
        "加载已解压的扩展程序",
        "读取所选内容",
        "保存为文件",
        "geo-verify collect",
        "--adapter share --input",
        "browser_assist",
        "保存时间",
        "已知限制",
        "Node",
        "FORMAT.md",
    ):
        assert phrase in readme, f"README 缺少：{phrase}"


def test_extension_texts_avoid_forbidden_wording() -> None:
    for path in _all_files():
        text = path.read_text(encoding="utf-8")
        for word in ("官方", "认证", "对标", "保证首推", "必然收录", "独立第三方"):
            assert word not in text, f"{_relative(path)} 出现不应使用的措辞：{word}"


# --------------------------------------------------------------------------- JS 行为测试


def _require_node(search_path: str | None = None) -> str:
    """找不到 Node 时让测试失败（不是跳过）：插件的逻辑必须被实际执行过才算验证。"""
    node = shutil.which("node", path=search_path)
    if node is None:
        pytest.fail(
            "需要 Node 才能验证插件逻辑：请安装 Node 并确保 node 在 PATH 中（见 extension/README.md）",
            pytrace=False,
        )
    return node


def test_missing_node_is_a_failure_not_a_skip(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="需要 Node 才能验证插件逻辑"):
        _require_node(str(tmp_path))


def test_js_test_files_are_all_listed() -> None:
    assert sorted(p.name for p in EXT_TESTS.glob("*.test.mjs")) == sorted(JS_TEST_FILES)


def test_js_files_parse() -> None:
    node = _require_node()
    scripts = [p for p in _code_files() if p.suffix in {".js", ".mjs"}]
    assert len(scripts) == 7
    for path in scripts:
        result = subprocess.run(
            [node, "--check", str(path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, f"{_relative(path)} 语法错误：\n{result.stderr}"


def _summary(output: str, key: str) -> int:
    match = re.search(rf"^# {key} (\d+)$", output, re.MULTILINE)
    assert match is not None, f"测试输出里没有 {key} 计数：\n{output}"
    return int(match.group(1))


@pytest.mark.parametrize("name", JS_TEST_FILES)
def test_js_behaviour(name: str) -> None:
    node = _require_node()
    result = subprocess.run(
        [node, "--test-reporter=tap", f"extension/tests/{name}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    report = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, f"{name} 未通过：\n{report}"
    total = _summary(result.stdout, "tests")
    assert total >= 8, f"{name} 只运行了 {total} 个测试"
    assert _summary(result.stdout, "pass") == total
    assert _summary(result.stdout, "fail") == 0
    assert _summary(result.stdout, "skipped") == 0
    assert _summary(result.stdout, "todo") == 0
    assert _summary(result.stdout, "cancelled") == 0
