"""已保存的分享数据导入（collect --adapter share）。

样例文件在 tests/samples/share/ 下，内容全部虚构。预期值按样例内容手工推算。
测试环境禁止联网（conftest），样例 HTML 里的外部脚本、样式、图片都不会被加载。
"""

from __future__ import annotations

import hashlib
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from factory import make_observation, write_records
from helpers_basic import INTENT_TEXTS, sealed_project
from helpers_collect import independent_digest
from hypothesis import given, settings
from hypothesis import strategies as st

from geo_verify import project, spec
from geo_verify.collect import share as share_module

NOW = "2026-10-20T08:00:00Z"
SAMPLES = Path(__file__).with_name("samples") / "share"
TEXTS = list(INTENT_TEXTS.values())  # Q001 … Q011
PLATFORMS = [
    {
        "platform_id": "engine-a",
        "display_name": "模拟平台 A",
        "channels": ["api", "manual", "share_import"],
    },
    {"platform_id": "engine-b", "display_name": "模拟平台 B", "channels": ["api"]},
]
HIDDEN_MARKERS = (
    "SCRIPT",
    "HIDDEN-ATTR",
    "DISPLAY-NONE",
    "VISIBILITY-HIDDEN",
    "ARIA-HIDDEN",
    "隐藏链接",
    "TEMPLATE-TEXT",
    "NOSCRIPT-TEXT",
    "COMMENT-TEXT",
    "IMG-ALT",
    "BUTTON-TEXT",
    "SOURCES-HEADING",
    "首页",
    "登录",
    "关于",
    "window.__STATE__",
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    sealed_project(path, platforms=PLATFORMS)
    return path


def share(root: Path, source: Path, query: str = "Q002", *extra: str) -> tuple[int, Any]:
    return run_cli_json(
        "collect", str(root), "--adapter", "share", "--input", str(source),
        "--platform", "engine-a", "--window", "W1", "--query", query, "--now", NOW, *extra,
    )  # fmt: skip


def observations(root: Path) -> list[dict[str, Any]]:
    records = project.load_observations(root)
    for record in records:
        assert spec.iter_issues("observation", record) == [], record
    return records


def only(root: Path) -> dict[str, Any]:
    records = observations(root)
    assert len(records) == 1
    return records[0]


def digest_for(root: Path, query_id: str, attempt: int = 1) -> str:
    sealed = project.select_prompt_set(root)
    return independent_digest(
        "auto-demo", sealed["content_hash"], "engine-a", "share_import", "W1", query_id, attempt
    )


def write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def conversation(question: str = TEXTS[1], answer: str = "（虚构回答）", **fields: Any) -> dict:
    payload: dict[str, Any] = {
        "messages": [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]
    }
    payload.update(fields)
    return payload


def page(*blocks: str, head: str = "") -> str:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>{head}</head><body>"
        + "".join(blocks)
        + "</body></html>"
    )


def user_block(text: str = TEXTS[1]) -> str:
    return f'<div data-message-author-role="user"><p>{html.escape(text)}</p></div>'


def write_html(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ JSON


def test_json_share_is_imported(root: Path) -> None:
    source = SAMPLES / "chat.json"
    code, data = share(root, source, "Q002", "--operator", "采集员乙")
    assert code == 0, data
    assert (data["written"], data["skipped_existing"]) == (1, 0)
    assert (data["status"], data["invalid_reason"]) == ("valid", None)
    assert data["format"] == "json"
    assert data["channel"] == "share_import"
    assert data["input"] == "chat.json", "只记文件名"
    assert data["prompt_text_differs"] is False
    assert data["time_source"] == "saved_at"
    assert data["prior_messages"] == 2
    assert any("之前" in item for item in data["warnings"]), "多轮对话要提示上下文"

    digest = digest_for(root, "Q002")
    obs = only(root)
    assert data["trace_id"] == obs["trace_id"] == "t-" + digest
    assert obs["observation_id"] == "o-" + digest
    assert (obs["channel"], obs["entry_form"]) == ("share_import", "web")
    assert (obs["query_id"], obs["prompt_text"]) == ("Q002", TEXTS[1])
    assert (obs["platform"], obs["window_id"], obs["attempt"]) == ("engine-a", "W1", 1)
    assert obs["answer_text"] == (
        "澜序 S7 长续航版的 CLTC 续航为 620 km[1]，WLTC 续航为 540 km。"
        "详见 https://auto-review.example/review/2002 。（虚构回答）"
    )
    assert obs["model_version"] == "engine-a-web-2026.10"
    assert (obs["started_at"], obs["ended_at"]) == ("2026-10-08T02:00:00Z", "2026-10-08T02:00:00Z")
    assert (obs["status"], obs["invalid_reason"]) == ("valid", None)
    assert obs["operator"] == "采集员乙"
    assert obs["synthetic"] is True
    for name in ("logged_in", "history_cleared", "web_search", "region", "device"):
        assert obs[name] is None
        assert obs["null_reasons"][name].strip()
    assert "2 条" in obs["null_reasons"]["history_cleared"]
    assert obs["citations"] == [
        {
            "order": 1,
            "url": "https://lanxu-auto.example/s7/spec",
            "domain": "lanxu-auto.example",
            "title": None,
            "placement": "inline",
        },
        {
            "order": 2,
            "url": "https://auto-review.example/review/2002",
            "domain": "auto-review.example",
            "title": "评测（虚构）",
            "placement": "inline",
        },
        {
            "order": 3,
            "url": "https://gov-data.example/data/1001",
            "domain": "gov-data.example",
            "title": "公共数据（虚构）",
            "placement": "source_list",
        },
        {
            "order": 4,
            "url": "https://lanxu-auto.example/s7/spec",
            "domain": "lanxu-auto.example",
            "title": None,
            "placement": "source_list",
        },
    ]
    raw = source.read_bytes()
    [snapshot] = obs["snapshots"]
    assert snapshot == {
        "path": f"snapshots/o-{digest}/chat.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "media_type": "application/json",
    }
    assert (root / snapshot["path"]).read_bytes() == raw, "原文件原样复制"
    assert obs["ext"]["share"] == {
        "format": "json",
        "source_file": "chat.json",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "share_url": "https://share.ev-forum.example/s/0001",
        "time_source": "saved_at",
        "time_field": "saved_at",
        "time_note": obs["ext"]["share"]["time_note"],
        "prompt_matches_sealed": True,
        "prior_messages": 2,
    }
    assert "保存时间" in obs["ext"]["share"]["time_note"]


def test_json_field_aliases(root: Path) -> None:
    code, data = share(root, SAMPLES / "alias.json", "Q001")
    assert code == 0, data
    obs = only(root)
    assert obs["prompt_text"] == TEXTS[0]
    assert obs["answer_text"] == "澜序是一个虚构的汽车品牌，主打纯电 SUV。[2]"
    assert obs["model_version"] == "engine-a-web-2026.11", "助手消息上的模型优先"
    assert obs["started_at"] == "2026-10-09T01:00:00Z", "数字时间戳按 Unix 秒换算为 UTC"
    assert obs["ext"]["share"]["time_field"] == "create_time"
    assert obs["ext"]["share"]["share_url"] == "https://share.ev-forum.example/s/0002"
    assert obs["ext"]["share"]["prior_messages"] == 0
    assert obs["ext"]["share"]["citations_skipped"] == 1
    assert data["warnings"] == []
    assert "之前" not in obs["null_reasons"]["history_cleared"]
    assert [(c["url"], c["title"], c["placement"]) for c in obs["citations"]] == [
        ("https://news-auto.example/article/1", "新闻（虚构）", "source_list"),
        ("https://lanxu-auto.example/", None, "inline"),  # 正文有 [2]
        ("https://gov-data.example/data/7", None, "inline"),  # 文件明确写了 placement
    ]


def test_top_level_list_and_content_parts(root: Path, tmp_path: Path) -> None:
    source = write_json(
        tmp_path / "list.json",
        [
            {"sender": "user", "content": [{"type": "text", "text": TEXTS[1]}]},
            {
                "sender": "bot",
                "content": ["澜序 S7 ", {"type": "text", "text": "续航 620 km（虚构）。"}],
                "citations": [{"url": "https://lanxu-auto.example/s7", "placement": "bogus"}, 7],
            },
        ],
    )
    code, _ = share(root, source)
    assert code == 0
    obs = only(root)
    assert obs["prompt_text"] == TEXTS[1]
    assert obs["answer_text"] == "澜序 S7 续航 620 km（虚构）。"
    assert obs["model_version"] is None
    assert "模型" in obs["null_reasons"]["model_version"]
    assert "未提供模型版本" in obs["entry_features"]
    assert obs["citations"][0]["placement"] == "source_list", "非法 placement 按通用规则判断"
    assert obs["ext"]["share"]["citations_skipped"] == 1


def test_last_pair_is_taken(root: Path, tmp_path: Path) -> None:
    source = write_json(
        tmp_path / "multi.json",
        {
            "messages": [
                {"role": "user", "content": TEXTS[0]},
                {"role": "assistant", "content": "第一轮回答（虚构）"},
                {"role": "user", "content": "先问一句别的"},
                {"role": "user", "content": TEXTS[1]},
                {"role": "assistant", "content": "第二轮回答（虚构）"},
                {"role": "tool", "content": "工具输出"},
            ]
        },
    )
    code, data = share(root, source)
    assert code == 0
    obs = only(root)
    assert (obs["prompt_text"], obs["answer_text"]) == (TEXTS[1], "第二轮回答（虚构）")
    assert data["prior_messages"] == 3


# ------------------------------------------------------------------ HTML


def test_html_share_is_imported_without_scripts_or_hidden_text(root: Path) -> None:
    source = SAMPLES / "page.html"
    code, data = share(root, source)
    assert code == 0, data
    assert data["format"] == "html"
    obs = only(root)
    assert obs["prompt_text"] == TEXTS[1]
    assert obs["answer_text"] == (
        "澜序 S7 长续航版的 CLTC 续航为 620 km，来源：澜序 S7 参数（虚构）。\n"
        "WLTC 工况下约为 540 km & 实际表现因路况而异。\n"
        "以上为虚构回答。"
    )
    for marker in HIDDEN_MARKERS:
        assert marker not in obs["answer_text"], marker
        assert marker not in obs["prompt_text"], marker
    assert obs["citations"] == [
        {
            "order": 1,
            "url": "https://lanxu-auto.example/s7/spec",
            "domain": "lanxu-auto.example",
            "title": "澜序 S7 参数（虚构）",
            "placement": "inline",
        },
        {
            "order": 2,
            "url": "https://auto-review.example/review/2002",
            "domain": "auto-review.example",
            "title": "评测（虚构）",
            "placement": "source_list",
        },
        {
            "order": 3,
            "url": "https://gov-data.example/data/1001",
            "domain": "gov-data.example",
            "title": None,
            "placement": "source_list",
        },
        {
            "order": 4,
            "url": "https://lanxu-auto.example/s7/spec",
            "domain": "lanxu-auto.example",
            "title": "澜序 S7 参数（虚构）",
            "placement": "source_list",
        },
    ]
    assert obs["ext"]["share"]["citations_skipped"] == 2, "javascript: 与相对链接不算引用"
    assert obs["model_version"] == "engine-a-web-2026.10"
    assert obs["started_at"] == "2026-10-08T02:30:00Z"
    assert obs["ext"]["share"]["share_url"] == "https://share.ev-forum.example/s/0003"
    assert obs["ext"]["share"]["time_field"] == "meta:saved_at"
    [snapshot] = obs["snapshots"]
    assert snapshot["path"].endswith("/page.html")
    assert snapshot["media_type"] == "text/html"
    assert (root / snapshot["path"]).read_bytes() == source.read_bytes()


def test_html_meta_alternatives_and_odd_markup(root: Path, tmp_path: Path) -> None:
    text = page(
        user_block(),
        '<section data-role="assistant" data-message-model-slug="slug-1">',
        "<p>第一段<br/>第二段",  # 未闭合的 <p>
        '<a href="https://lanxu-auto.example/a" title="标题来自属性">  </a>',
        "<div hidden><p>外层隐藏 <b>内层也隐藏 NESTED-HIDDEN</b></p></div>",
        "<aside>侧栏来源 ASIDE-TEXT <a href='https://auto-review.example/b'>B</a></aside>",
        "<p>尾句</p></section></span>",  # 多余的结束标签被忽略
        "<p>对话之外的文字 OUTSIDE</p>",
        head='<meta property="og:url" content="https://share.ev-forum.example/s/og">'
        '<meta name="description" content="页面描述">',
    )
    code, data = share(root, write_html(tmp_path / "odd.htm", text))
    assert code == 0, data
    obs = only(root)
    assert obs["answer_text"] == "第一段\n第二段\n尾句"
    assert obs["model_version"] == "slug-1"
    assert obs["ext"]["share"]["share_url"] == "https://share.ev-forum.example/s/og"
    assert [(c["url"], c["title"], c["placement"]) for c in obs["citations"]] == [
        ("https://lanxu-auto.example/a", "标题来自属性", "inline"),
        ("https://auto-review.example/b", "B", "source_list"),
    ]
    assert obs["ext"]["share"]["time_source"] == "import_time"


def test_html_in_gbk_with_meta_charset(root: Path, tmp_path: Path) -> None:
    text = page(
        user_block(),
        '<div data-role="assistant"><p>续航 620 km（虚构）</p></div>',
        head="",
    ).replace("charset='utf-8'", "charset='gbk'")
    path = tmp_path / "gbk.html"
    path.write_bytes(text.encode("gbk"))
    code, data = share(root, path)
    assert code == 0, data
    assert only(root)["answer_text"] == "续航 620 km（虚构）"


@settings(max_examples=40, deadline=None)
@given(
    visible=st.text(alphabet="澜序续航公里 abcXYZ012，。", min_size=1, max_size=30).filter(
        lambda value: value.strip()
    ),
    hidden=st.text(alphabet="隐藏脚本QWE<>&\"'", min_size=1, max_size=30),
)
def test_html_extraction_property(visible: str, hidden: str) -> None:
    marker = "##" + hidden  # "#" 不在可见文本的字符集里
    text = page(
        user_block(),
        '<div data-role="assistant">',
        f"<p>{html.escape(visible)}</p>",
        f"<script>var x = {json.dumps(marker)};</script>",
        f"<span hidden>{html.escape(marker)}</span>",
        "</div>",
    )
    parsed = share_module.parse_html(text)
    question, answer, _ = share_module.last_pair(parsed.messages)
    assert question.text == TEXTS[1]
    assert answer.text == " ".join(visible.split())
    assert "#" not in answer.text


# ------------------------------------------------------------------ 题目不一致、去重


def test_prompt_mismatch_is_imported_with_a_warning(root: Path) -> None:
    code, data = share(root, SAMPLES / "chat.json", "Q001")
    assert code == 0
    assert data["written"] == 1
    assert data["prompt_text_differs"] is True
    assert "不一致" in data["warnings"][0]
    obs = only(root)
    assert obs["query_id"] == "Q001"
    assert obs["prompt_text"] == TEXTS[1], "prompt_text 记录实际提问原文"
    assert obs["ext"]["share"]["prompt_matches_sealed"] is False


def test_prompt_mismatch_is_the_first_line_of_text_output(root: Path, tmp_path: Path) -> None:
    code, out, _ = run_cli(
        "collect", str(root), "--adapter", "share", "--input", str(SAMPLES / "chat.json"),
        "--platform", "engine-a", "--window", "W1", "--query", "Q001", "--now", NOW,
    )  # fmt: skip
    assert code == 0
    first = out.splitlines()[0]
    assert first.startswith("警告")
    assert "不一致" in first


def test_whitespace_differences_are_not_a_mismatch(root: Path, tmp_path: Path) -> None:
    spaced = "  " + TEXTS[1].replace(" ", "   ") + "\n"
    code, data = share(root, write_json(tmp_path / "spaced.json", conversation(spaced)))
    assert code == 0
    assert data["prompt_text_differs"] is False
    assert only(root)["prompt_text"] == spaced, "仍按原样记录实际提问"


def test_repeated_import_is_not_written_twice(root: Path, tmp_path: Path) -> None:
    source = SAMPLES / "chat.json"
    assert share(root, source)[0] == 0
    files_before = sorted(path for path in root.rglob("*") if path.is_file())
    code, data = share(root, source)
    assert code == 0
    assert (data["written"], data["skipped_existing"]) == (0, 1)
    assert data["audit_event_id"] is None
    assert len(observations(root)) == 1
    assert len(project.load_audit(root)) == 1
    assert sorted(path for path in root.rglob("*") if path.is_file()) == files_before

    code, data = share(root, source, "Q002", "--attempt", "2")
    assert code == 2, "同一份文件换个次数序号再导入会重复计数"
    assert "已导入" in data["message"]

    other = write_json(tmp_path / "other.json", conversation())
    code, data = share(root, other)
    assert code == 2, "同一任务已有另一份文件的导入记录，观测不可覆盖"
    assert "--attempt" in data["message"]
    code, data = share(root, other, "Q002", "--attempt", "2")
    assert code == 0
    assert data["trace_id"] == "t-" + digest_for(root, "Q002", 2)
    assert len(observations(root)) == 2


def test_same_files_give_identical_bytes(tmp_path: Path) -> None:
    roots = [tmp_path / "a" / "proj", tmp_path / "b" / "proj"]
    for item in roots:
        sealed_project(item, platforms=PLATFORMS)
        assert share(item, SAMPLES / "chat.json")[0] == 0
        assert share(item, SAMPLES / "page.html", "Q002", "--attempt", "2")[0] == 0
    files = [
        {
            str(path.relative_to(item)): path.read_bytes()
            for path in sorted(item.rglob("*"))
            if path.is_file()
        }
        for item in roots
    ]
    assert files[0] == files[1]
    for content in files[0].values():
        assert str(tmp_path).encode() not in content


# ------------------------------------------------------------------ 时间


def test_missing_saved_time_uses_import_time(root: Path, tmp_path: Path) -> None:
    code, data = share(root, write_json(tmp_path / "t.json", conversation()))
    assert code == 0
    assert data["time_source"] == "import_time"
    assert any("导入时间" in item for item in data["warnings"])
    obs = only(root)
    assert (obs["started_at"], obs["ended_at"]) == (NOW, NOW)
    assert obs["ext"]["share"]["time_source"] == "import_time"
    assert obs["ext"]["share"]["time_field"] is None
    assert "时间为导入时间，非采集时间" in obs["ext"]["share"]["time_note"]


def test_missing_saved_time_without_now_uses_the_clock(root: Path, tmp_path: Path) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    code, _, err = run_cli(
        "collect", str(root), "--adapter", "share",
        "--input", str(write_json(tmp_path / "t.json", conversation())),
        "--platform", "engine-a", "--window", "W1", "--query", "Q002",
    )  # fmt: skip
    after = datetime.now(UTC)
    assert code == 0, err
    started = datetime.fromisoformat(only(root)["started_at"].replace("Z", "+00:00"))
    assert before <= started <= after


@pytest.mark.parametrize(
    ("saved", "hint"),
    [
        ("2026-10-08T10:00:00", "时区"),
        ("昨天下午", "时区"),
        (10**20, "保存时间"),
        (True, "保存时间"),
        ({"time": 1}, "保存时间"),
    ],
)
def test_unusable_saved_time_falls_back_with_warning(
    root: Path, tmp_path: Path, saved: Any, hint: str
) -> None:
    code, data = share(root, write_json(tmp_path / "t.json", conversation(saved_at=saved)))
    assert code == 0
    assert data["time_source"] == "import_time"
    assert any(hint in item for item in data["warnings"])
    assert only(root)["started_at"] == NOW


# ------------------------------------------------------------------ 状态、参数


def test_empty_answer_is_an_invalid_observation(root: Path, tmp_path: Path) -> None:
    code, data = share(root, write_json(tmp_path / "e.json", conversation(answer="   ")))
    assert code == 0
    assert (data["status"], data["invalid_reason"]) == ("invalid", "empty_answer")
    obs = only(root)
    assert obs["answer_text"] is None
    assert obs["null_reasons"]["answer_text"].strip()


def test_garbled_answer_is_an_invalid_observation(root: Path, tmp_path: Path) -> None:
    garbled = chr(0xFFFD) * 8 + "S7"
    code, data = share(root, write_json(tmp_path / "g.json", conversation(answer=garbled)))
    assert code == 0
    assert (data["status"], data["invalid_reason"]) == ("invalid", "garbled")
    assert only(root)["answer_text"] == garbled


def test_entry_form_option(root: Path) -> None:
    code, data = share(root, SAMPLES / "chat.json", "Q002", "--entry-form", "app")
    assert code == 0, data
    assert only(root)["entry_form"] == "app"
    assert "app" in only(root)["entry_features"]


def test_entry_form_api_is_not_allowed(root: Path) -> None:
    code, _, err = run_cli(
        "collect", str(root), "--adapter", "share", "--input", str(SAMPLES / "chat.json"),
        "--platform", "engine-a", "--window", "W1", "--query", "Q002", "--entry-form", "api",
    )  # fmt: skip
    assert code == 2
    assert "--entry-form" in err


def test_audit_event(root: Path) -> None:
    source = SAMPLES / "chat.json"
    code, data = share(root, source, "Q002", "--operator", "采集员乙")
    assert code == 0
    [event] = project.load_audit(root)
    assert data["audit_event_id"] == event["event_id"]
    assert event["action"] == "import"
    assert event["actor"] == "采集员乙"
    assert event["target"] == {"type": "observations", "id": "chat.json"}
    assert event["occurred_at"] == NOW
    assert event["before_hash"] is None
    assert (
        event["after_hash"]
        == hashlib.sha256((root / "observations.jsonl").read_bytes()).hexdigest()
    )
    details = event["details"]
    assert details["adapter"] == "share"
    assert details["channel"] == "share_import"
    assert details["source_file"] == "chat.json"
    assert details["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert (details["format"], details["platform"], details["window_id"]) == (
        "json",
        "engine-a",
        "W1",
    )
    assert (details["query_id"], details["attempt"]) == ("Q002", 1)
    assert details["observation_id"] == "o-" + digest_for(root, "Q002")
    assert details["prompt_text_differs"] is False
    assert details["time_source"] == "saved_at"
    assert event["synthetic"] is True

    share(root, write_json(root.parent / "x.json", conversation()), "Q003")
    assert project.load_audit(root)[-1]["actor"] == "geo-verify collect（未指明操作人）"


@pytest.mark.parametrize(
    ("extra", "flag"),
    [
        (("--attempt", "0"), "--attempt"),
        (("--repeats", "2"), "--repeats"),
        (("--queries", "Q001"), "--queries"),
        (("--model", "m"), "--model"),
        (("--asgi-app", "x:app"), "--asgi-app"),
    ],
)
def test_option_errors(root: Path, extra: tuple[str, ...], flag: str) -> None:
    code, data = share(root, SAMPLES / "chat.json", "Q002", *extra)
    assert code == 2
    assert flag in data["message"]
    assert project.load_observations(root) == []


def test_required_options(root: Path) -> None:
    full = {
        "--input": str(SAMPLES / "chat.json"),
        "--platform": "engine-a",
        "--window": "W1",
        "--query": "Q002",
    }
    for missing in full:
        argv = [item for key, value in full.items() if key != missing for item in (key, value)]
        code, data = run_cli_json("collect", str(root), "--adapter", "share", *argv)
        assert code == 2
        assert missing in data["message"]


def test_query_and_platform_must_be_registered(root: Path) -> None:
    code, data = share(root, SAMPLES / "chat.json", "Q999")
    assert code == 2
    assert "Q999" in data["message"]
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "share", "--input", str(SAMPLES / "chat.json"),
        "--platform", "engine-b", "--window", "W1", "--query", "Q002",
    )  # fmt: skip
    assert code == 2
    assert "share_import" in data["message"]


# ------------------------------------------------------------------ 非法文件


INVALID_FILES: list[tuple[str, bytes, str]] = [
    ("bad.json", b'{"messages": [', "JSON"),
    ("scalar.json", b"42", "对象"),
    ("nomsg.json", b'{"title": "x"}', "messages"),
    ("notlist.json", json.dumps({"messages": "x"}).encode(), "messages"),
    ("item.json", json.dumps({"messages": ["x"]}).encode(), "第 1 条消息"),
    (
        "system.json",
        json.dumps({"messages": [{"role": "system", "content": "x"}]}).encode(),
        "提问",
    ),
    (
        "useronly.json",
        json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode(),
        "提问",
    ),
    (
        "reversed.json",
        json.dumps(
            {"messages": [{"role": "assistant", "content": "a"}, {"role": "user", "content": "q"}]}
        ).encode(),
        "提问",
    ),
    (
        "content.json",
        json.dumps({"messages": [{"role": "user", "content": 7}]}).encode(),
        "第 1 条消息",
    ),
    (
        "blankq.json",
        json.dumps(
            {"messages": [{"role": "user", "content": " "}, {"role": "assistant", "content": "a"}]}
        ).encode(),
        "提问",
    ),
    (
        "gbk.json",
        json.dumps({"messages": []}, ensure_ascii=False).encode() + "中".encode("gbk"),
        "UTF-8",
    ),
    ("plain.html", page("<p>没有角色标记的页面</p>").encode(), "角色"),
    ("useronly.html", page(user_block()).encode(), "提问"),
    ("latin.html", b"<html><body>\xff\xfe\xfa</body></html>", "UTF-8"),
    ("empty.json", b"", "空"),
    ("blob.bin", b"\x00\x01\x02", "格式"),
    ("saved.mhtml", b"MIME-Version: 1.0\r\n", "MHTML"),
]


@pytest.mark.parametrize(("name", "content", "hint"), INVALID_FILES)
def test_invalid_files_are_input_errors(
    root: Path, tmp_path: Path, name: str, content: bytes, hint: str
) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    code, data = share(root, path)
    assert code == 2, data
    assert data["error"] == "input_error"
    assert hint in data["message"]
    assert project.load_observations(root) == []
    assert project.load_audit(root) == []
    assert not (root / "snapshots").exists()


def test_missing_input_file(root: Path, tmp_path: Path) -> None:
    code, data = share(root, tmp_path / "absent.json")
    assert code == 2
    assert "absent.json" in data["message"]


def test_format_is_sniffed_without_extension(root: Path, tmp_path: Path) -> None:
    json_file = write_json(tmp_path / "export", conversation())
    code, data = share(root, json_file)
    assert (code, data["format"]) == (0, "json")
    html_file = write_html(
        tmp_path / "saved-page",
        page(user_block(TEXTS[2]), '<div data-role="assistant">回答（虚构）</div>'),
    )
    code, data = share(root, html_file, "Q003")
    assert (code, data["format"]) == (0, "html")
    assert only_by_query(root, "Q003")["snapshots"][0]["media_type"] == "application/octet-stream"


def only_by_query(root: Path, query_id: str) -> dict[str, Any]:
    [record] = [item for item in observations(root) if item["query_id"] == query_id]
    return record


@pytest.mark.parametrize("declared", [b"<meta charset='no-such-codec'>", b"<meta charset='ascii'>"])
def test_unusable_declared_charset(root: Path, tmp_path: Path, declared: bytes) -> None:
    path = tmp_path / "bad-charset.html"
    path.write_bytes(b"<html><head>" + declared + b"</head><body>\xff\xfe</body></html>")
    code, data = share(root, path)
    assert code == 2
    assert "charset" in data["message"]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, None),  # 空内容 → 空回答
        ({"text": "对象里的文本（虚构）"}, "对象里的文本（虚构）"),
        (
            ["片段一", {"type": "image_url", "image_url": {"url": "https://x.example/a.png"}}],
            "片段一",
        ),
    ],
)
def test_more_content_shapes(
    root: Path, tmp_path: Path, content: Any, expected: str | None
) -> None:
    payload = conversation()
    payload["messages"][1]["content"] = content
    code, data = share(root, write_json(tmp_path / "shape.json", payload))
    assert code == 0, data
    assert only(root)["answer_text"] == expected


@pytest.mark.parametrize("content", [[7], {"unknown": 1}])
def test_unrecognised_content_shapes(root: Path, tmp_path: Path, content: Any) -> None:
    payload = conversation()
    payload["messages"][1]["content"] = content
    code, data = share(root, write_json(tmp_path / "shape.json", payload))
    assert code == 2
    assert "第 2 条消息" in data["message"]


def test_citations_that_are_not_a_list(root: Path, tmp_path: Path) -> None:
    payload = conversation(sources={"url": "https://lanxu-auto.example/"})
    code, _ = share(root, write_json(tmp_path / "c.json", payload))
    assert code == 0
    obs = only(root)
    assert obs["citations"] == []
    assert obs["ext"]["share"]["citations_skipped"] == 1


def test_unclosed_markup_at_end_of_file(root: Path, tmp_path: Path) -> None:
    text = (
        "<html><body>" + user_block() + '<div data-role="assistant"><p>回答（虚构）'
        '<a href="https://lanxu-auto.example/end">未闭合的链接'
    )
    code, data = share(root, write_html(tmp_path / "cut.html", text))
    assert code == 0, data
    obs = only(root)
    assert obs["answer_text"] == "回答（虚构）未闭合的链接"
    assert [(c["url"], c["title"]) for c in obs["citations"]] == [
        ("https://lanxu-auto.example/end", "未闭合的链接")
    ]


def test_existing_snapshot_files_are_never_overwritten(root: Path, tmp_path: Path) -> None:
    source = SAMPLES / "chat.json"
    folder = root / "snapshots" / ("o-" + digest_for(root, "Q002"))
    folder.mkdir(parents=True)
    (folder / "chat.json").write_bytes(source.read_bytes())  # 上次中断前已复制的同一份文件
    assert share(root, source)[0] == 0
    assert only(root)["snapshots"][0]["path"].endswith("/chat.json")

    other_root = tmp_path / "other"
    sealed_project(other_root, platforms=PLATFORMS)
    stale = other_root / "snapshots" / ("o-" + digest_for(other_root, "Q002")) / "chat.json"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")
    assert share(other_root, source)[0] == 0
    [snapshot] = only(other_root)["snapshots"]
    assert snapshot["path"].endswith("/chat.2.json")
    assert stale.read_bytes() == b"stale"


def test_observation_id_taken_by_another_record(root: Path) -> None:
    proj = project.load_project(root)
    sealed = project.select_prompt_set(root)
    other = make_observation(
        proj, sealed, 1, observation_id="o-" + digest_for(root, "Q002"), trace_id="t-other"
    )
    write_records(root, observations=[other])
    code, data = share(root, SAMPLES / "chat.json")
    assert code == 2
    assert "t-other" in data["message"]


def test_non_synthetic_project(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    sealed_project(root, platforms=PLATFORMS, synthetic=False)
    assert share(root, SAMPLES / "chat.json")[0] == 0
    assert "synthetic" not in only(root)
    assert "synthetic" not in project.load_audit(root)[0]
