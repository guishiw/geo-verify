"""通用 OpenAI 兼容 API 适配器（collect --adapter openai）。

全部请求都发往 tests/mock_openai_app.py 这个进程内 ASGI 应用（httpx.ASGITransport），
不监听端口、不联网。预期值按题目文本、约定的编号规则和场景定义独立推算。
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import mock_openai_app as mock
import pytest
from conftest import run_cli, run_cli_json
from factory import make_observation, write_records
from helpers_basic import INTENT_TEXTS, sealed_project
from helpers_collect import independent_digest
from hypothesis import given
from hypothesis import strategies as st

from geo_verify import project, spec
from geo_verify.collect import openai_compat

NOW = "2026-10-08T02:00:00Z"
APP_FILE = Path(__file__).with_name("mock_openai_app.py")
APP = f"{APP_FILE}:app"
TEXTS = list(INTENT_TEXTS.values())  # Q001 … Q011 的题目原文
FAKE_KEY = "sk-geo-test-FAKE-0123456789abcdef"
KEY_ENV = "GEO_VERIFY_TEST_API_KEY"


@pytest.fixture(autouse=True)
def _reset_mock() -> Iterator[None]:
    mock.reset()
    yield
    mock.reset()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    sealed_project(path)
    return path


def collect(root: Path, scenario: str = "normal", *extra: str, queries: str | None = "Q001"):
    argv = [
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", APP, "--base-url", f"http://mock.invalid/{scenario}/v1",
        "--retry-wait", "0", "--now", NOW,
    ]  # fmt: skip
    if queries is not None:
        argv += ["--queries", queries]
    return run_cli_json(*argv, *extra)


def observations(root: Path) -> list[dict[str, Any]]:
    records = project.load_observations(root)
    for record in records:
        assert spec.iter_issues("observation", record) == [], record
    return records


def only(root: Path) -> dict[str, Any]:
    records = observations(root)
    assert len(records) == 1
    return records[0]


def task_ids(root: Path, query_id: str, attempt: int = 1, platform: str = "engine-a") -> str:
    sealed = project.select_prompt_set(root)
    return independent_digest(
        "auto-demo", sealed["content_hash"], platform, "api", "W1", query_id, attempt
    )


def at(seconds: int) -> str:
    moment = datetime(2026, 10, 8, 2, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def response_bytes(payload: dict[str, Any]) -> bytes:
    """按测试服务的写法独立序列化一次响应，用来核对快照是否原样保存。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def all_files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ------------------------------------------------------------------ 正常请求


def test_normal_request_writes_a_complete_api_observation(root: Path) -> None:
    code, data = collect(
        root, "normal", "--model", "req-model", "--operator", "采集员甲", "--web-search", "true"
    )
    assert code == 0, data
    assert (data["planned"], data["skipped_existing"], data["written"]) == (1, 0, 1)
    assert (data["valid"], data["invalid"], data["invalid_by_reason"]) == (1, 0, {})
    assert data["channel"] == "api"
    assert data["transport"] == "asgi"
    assert data["app"] == "mock_openai_app.py:app", "只记文件名，不带绝对路径"

    digest = task_ids(root, "Q001")
    [call] = mock.CALLS
    assert call["method"] == "POST"
    assert call["path"] == "/normal/v1/chat/completions"
    assert call["body"] == {
        "model": "req-model",
        "messages": [{"role": "user", "content": TEXTS[0]}],
    }
    assert call["headers"]["x-request-id"] == "t-" + digest
    assert "authorization" not in call["headers"], "没有给 --api-key-env 时不发送授权头"

    obs = only(root)
    assert obs["trace_id"] == "t-" + digest
    assert obs["observation_id"] == "o-" + digest
    assert obs["project_id"] == "auto-demo"
    assert obs["prompt_set_hash"] == project.select_prompt_set(root)["content_hash"]
    assert (obs["query_id"], obs["prompt_text"]) == ("Q001", TEXTS[0])
    assert (obs["platform"], obs["window_id"], obs["attempt"]) == ("engine-a", "W1", 1)
    assert (obs["channel"], obs["entry_form"]) == ("api", "api")
    assert obs["model_version"] == mock.MODEL
    assert "API 通道" in obs["entry_features"]
    assert "req-model" in obs["entry_features"]
    assert obs["answer_text"] == mock.ANSWER
    assert (obs["status"], obs["invalid_reason"]) == ("valid", None)
    assert obs["citations"] == []
    assert (obs["started_at"], obs["ended_at"]) == (NOW, NOW)
    assert obs["web_search"] is True
    assert obs["operator"] == "采集员甲"
    assert obs["collector"]["tool"] == "geo-verify"
    assert obs["synthetic"] is True
    for name in ("logged_in", "history_cleared", "region", "device"):
        assert obs[name] is None
        assert obs["null_reasons"][name].strip()
    assert set(obs["null_reasons"]) == {"logged_in", "history_cleared", "region", "device"}

    [snapshot] = obs["snapshots"]
    assert snapshot["path"] == f"snapshots/o-{digest}.json"
    assert snapshot["media_type"] == "application/json"
    stored = (root / snapshot["path"]).read_bytes()
    assert stored == response_bytes(mock.completion()), "原始响应体原样保存"
    assert snapshot["sha256"] == hashlib.sha256(stored).hexdigest()

    api = obs["ext"]["api"]
    assert api["transport"] == "asgi"
    assert api["endpoint"] == "http://mock.invalid/normal/v1/chat/completions"
    assert api["request_model"] == "req-model"
    assert (api["http_status"], api["finish_reason"], api["rate_limit_retries"]) == (200, "stop", 0)
    assert "detail" not in api


def test_start_times_follow_task_order_under_now(root: Path) -> None:
    code, data = collect(root, "normal", "--repeats", "2", queries="Q002,Q001")
    assert code == 0
    assert data["planned"] == 4
    records = observations(root)
    assert [(item["query_id"], item["attempt"]) for item in records] == [
        ("Q001", 1),
        ("Q001", 2),
        ("Q002", 1),
        ("Q002", 2),
    ], "题目按封存顺序、每题依次第 1、2 次"
    assert [item["started_at"] for item in records] == [at(0), at(1), at(2), at(3)]
    assert [item["ended_at"] for item in records] == [at(0), at(1), at(2), at(3)]
    assert [call["body"]["messages"][0]["content"] for call in mock.CALLS] == [
        TEXTS[0],
        TEXTS[0],
        TEXTS[1],
        TEXTS[1],
    ]
    assert [call["headers"]["x-request-id"] for call in mock.CALLS] == [
        "t-" + task_ids(root, "Q001", 1),
        "t-" + task_ids(root, "Q001", 2),
        "t-" + task_ids(root, "Q002", 1),
        "t-" + task_ids(root, "Q002", 2),
    ]


def test_without_now_the_real_clock_is_used(root: Path) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    code, _, err = run_cli(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", APP, "--base-url", "http://mock.invalid/normal/v1", "--queries", "Q001",
    )  # fmt: skip
    after = datetime.now(UTC)
    assert code == 0, err
    obs = only(root)
    started = datetime.fromisoformat(obs["started_at"].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(obs["ended_at"].replace("Z", "+00:00"))
    assert before <= started <= ended <= after
    event = project.load_audit(root)[-1]
    occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
    assert before <= occurred <= after


def test_unspecified_model_and_web_search_are_not_invented(root: Path) -> None:
    code, _ = collect(root, "normal")
    assert code == 0
    [call] = mock.CALLS
    assert call["body"] == {"messages": [{"role": "user", "content": TEXTS[0]}]}, (
        "没有给 --model 时不编造模型名"
    )
    obs = only(root)
    assert obs["web_search"] is None
    assert "--web-search" in obs["null_reasons"]["web_search"]
    assert obs["operator"] is None
    assert obs["null_reasons"]["operator"].strip()
    assert obs["ext"]["api"]["request_model"] is None
    assert "未指定" in obs["entry_features"]


def test_web_search_false_is_recorded(root: Path) -> None:
    assert collect(root, "normal", "--web-search", "false")[0] == 0
    assert only(root)["web_search"] is False


@pytest.mark.parametrize("scenario", ["no-model", "blank-model"])
def test_missing_model_in_response_is_null_with_reason(root: Path, scenario: str) -> None:
    code, _ = collect(root, scenario, "--model", "req-model")
    assert code == 0
    obs = only(root)
    assert obs["model_version"] is None, "不拿请求里的模型名冒充响应的模型版本"
    assert "model" in obs["null_reasons"]["model_version"]
    assert "未返回" in obs["entry_features"]
    assert obs["status"] == "valid"


def test_content_parts_are_joined(root: Path) -> None:
    assert collect(root, "parts")[0] == 0
    obs = only(root)
    assert obs["answer_text"] == "澜序 S7 值得考虑（虚构）。"
    assert obs["status"] == "valid"


# ------------------------------------------------------------------ 引用


def test_annotation_citations_are_inline_and_duplicates_kept(root: Path) -> None:
    assert collect(root, "annotations")[0] == 0
    obs = only(root)
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
            "url": "https://auto-review.example/review/1001",
            "domain": "auto-review.example",
            "title": None,
            "placement": "inline",
        },
        {
            "order": 3,
            "url": "https://lanxu-auto.example/s7/spec",
            "domain": "lanxu-auto.example",
            "title": "澜序 S7 参数（虚构）",
            "placement": "inline",
        },
    ]
    assert obs["ext"]["api"]["citations_skipped"] == 3


def test_top_level_url_citations_use_markers_or_urls_for_placement(root: Path) -> None:
    assert collect(root, "citations-urls")[0] == 0
    obs = only(root)
    assert [(item["order"], item["domain"], item["placement"]) for item in obs["citations"]] == [
        (1, "gov-data.example", "inline"),  # 正文有 [1]
        (2, "auto-review.example", "source_list"),  # 既无 [2] 也无链接
        (3, "ev-forum.example", "inline"),  # 正文直接出现该链接
        (4, "gov-data.example", "source_list"),  # 与第 1 条重复，原样保留；正文无 [4]
    ]
    assert all(item["title"] is None for item in obs["citations"])
    assert "citations_skipped" not in obs["ext"]["api"]


def test_top_level_object_citations(root: Path) -> None:
    assert collect(root, "citations-objects")[0] == 0
    obs = only(root)
    assert obs["citations"] == [
        {
            "order": 1,
            "url": "https://news-auto.example/article/1",
            "domain": "news-auto.example",
            "title": "新闻（虚构）",
            "placement": "source_list",
        },
        {
            "order": 2,
            "url": "https://lanxu-auto.example/s7/price",
            "domain": "lanxu-auto.example",
            "title": "价格（虚构）",
            "placement": "inline",
        },
        {
            "order": 3,
            "url": "not a url",
            "domain": None,
            "title": None,
            "placement": "inline",
        },
    ]
    assert obs["ext"]["api"]["citations_skipped"] == 2


def test_annotations_that_are_not_a_list_are_skipped(root: Path) -> None:
    assert collect(root, "annotations-not-list")[0] == 0
    obs = only(root)
    assert obs["citations"] == []
    assert obs["ext"]["api"]["citations_skipped"] == 1


def test_citations_that_are_not_a_list_are_skipped(root: Path) -> None:
    assert collect(root, "citations-not-list")[0] == 0
    obs = only(root)
    assert obs["citations"] == []
    assert obs["ext"]["api"]["citations_skipped"] == 1
    assert obs["status"] == "valid"


URL_POOL = [f"https://{host}/page/{n}" for host in ("a.example", "b.test") for n in range(3)]


@given(
    urls=st.lists(st.sampled_from(URL_POOL), max_size=12),
    marked=st.sets(st.integers(min_value=1, max_value=12)),
)
def test_citation_placement_property(urls: list[str], marked: set[int]) -> None:
    content = "回答正文（虚构）" + "".join(f"[{n}]" for n in sorted(marked))
    citations, skipped = openai_compat.extract_citations({"citations": urls}, content)
    assert skipped == 0
    assert [item["url"] for item in citations] == urls, "顺序与重复都原样保留"
    assert [item["order"] for item in citations] == list(range(1, len(urls) + 1))
    for index, item in enumerate(citations, start=1):
        assert item["placement"] == ("inline" if index in marked else "source_list")
        assert item["domain"] == item["url"].split("/")[2]


# ------------------------------------------------------------------ 故障 → 无效观测


def assert_invalid(root: Path, data: dict[str, Any], reason: str) -> dict[str, Any]:
    assert (data["written"], data["valid"], data["invalid"]) == (1, 0, 1)
    assert data["invalid_by_reason"] == {reason: 1}
    obs = only(root)
    assert (obs["status"], obs["invalid_reason"]) == ("invalid", reason)
    assert obs["ext"]["api"]["detail"].strip()
    return obs


def assert_no_answer(obs: dict[str, Any]) -> None:
    assert obs["answer_text"] is None
    assert obs["null_reasons"]["answer_text"].strip()


def assert_no_snapshot(obs: dict[str, Any]) -> None:
    assert obs["snapshots"] == []
    assert obs["null_reasons"]["snapshots"].strip()


def test_http_504_is_timeout_and_body_is_kept(root: Path) -> None:
    code, data = collect(root, "timeout504")
    assert code == 0, "无效观测是正常的采集结果"
    obs = assert_invalid(root, data, "timeout")
    assert_no_answer(obs)
    assert obs["ext"]["api"]["http_status"] == 504
    [snapshot] = obs["snapshots"]
    assert json.loads((root / snapshot["path"]).read_bytes())["error"]["code"] == 504


def test_http_408_without_body_is_timeout_without_snapshot(root: Path) -> None:
    _, data = collect(root, "timeout408")
    obs = assert_invalid(root, data, "timeout")
    assert obs["ext"]["api"]["http_status"] == 408
    assert_no_snapshot(obs)


def test_client_side_timeout(root: Path) -> None:
    _, data = collect(root, "slow", "--timeout", "0.05")
    obs = assert_invalid(root, data, "timeout")
    assert obs["ext"]["api"]["http_status"] is None
    assert_no_answer(obs)
    assert_no_snapshot(obs)
    assert "超时" in obs["null_reasons"]["snapshots"]


def test_persistent_429_becomes_rate_limited_after_retries(root: Path) -> None:
    _, data = collect(root, "ratelimit", "--max-retries", "2")
    obs = assert_invalid(root, data, "rate_limited")
    assert len(mock.CALLS) == 3, "首次请求 + 2 次重试"
    assert {call["headers"]["x-request-id"] for call in mock.CALLS} == {obs["trace_id"]}
    assert obs["ext"]["api"]["rate_limit_retries"] == 2
    assert obs["ext"]["api"]["http_status"] == 429
    assert_no_answer(obs)


def test_429_then_recovery_is_valid(root: Path) -> None:
    code, data = collect(root, "flaky429", "--max-retries", "2")
    assert code == 0
    assert (data["valid"], data["invalid"]) == (1, 0)
    obs = only(root)
    assert obs["status"] == "valid"
    assert obs["ext"]["api"]["rate_limit_retries"] == mock.FLAKY_FAILURES
    assert len(mock.CALLS) == 3


@pytest.mark.parametrize(("retries", "calls"), [(0, 1), (1, 2)])
def test_retry_budget_is_respected(root: Path, retries: int, calls: int) -> None:
    _, data = collect(root, "flaky429", "--max-retries", str(retries))
    assert_invalid(root, data, "rate_limited")
    assert len(mock.CALLS) == calls


def test_retry_wait_is_configurable(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(openai_compat, "retry_sleep", record)
    code, _ = collect(root, "ratelimit", "--max-retries", "2", "--retry-wait", "1.5")
    assert code == 0
    assert waits == [1.5, 1.5]


@pytest.mark.parametrize("scenario", ["empty", "null-content"])
def test_empty_answer(root: Path, scenario: str) -> None:
    _, data = collect(root, scenario)
    obs = assert_invalid(root, data, "empty_answer")
    assert_no_answer(obs)
    assert len(obs["snapshots"]) == 1


def test_truncated_answer_keeps_partial_text(root: Path) -> None:
    _, data = collect(root, "truncated")
    obs = assert_invalid(root, data, "truncated")
    assert obs["answer_text"] == "澜序 S7 的 CLTC 续航为", "原始记录保真：截断的文本照原样保存"
    assert obs["ext"]["api"]["finish_reason"] == "length"


@pytest.mark.parametrize("scenario", ["garbled", "control-chars"])
def test_garbled_answer(root: Path, scenario: str) -> None:
    _, data = collect(root, scenario)
    obs = assert_invalid(root, data, "garbled")
    assert obs["answer_text"]


def test_a_few_replacement_characters_are_not_garbled(root: Path) -> None:
    _, data = collect(root, "few-replacements")
    assert data["valid"] == 1


def test_garbled_ratio_threshold() -> None:
    bad = chr(0xFFFD)
    assert openai_compat.GARBLED_RATIO == 0.2
    assert openai_compat.garbled_ratio("") == 0.0
    assert openai_compat.garbled_ratio("  \n\t ") == 0.0
    assert openai_compat.garbled_ratio("abcd" + bad) == pytest.approx(0.2)
    assert openai_compat.garbled_ratio("ab cd e" + bad) == pytest.approx(1 / 6), "空白不计入"
    assert openai_compat.garbled_ratio("a" + chr(0x01) + chr(0x7F)) == pytest.approx(2 / 3)
    assert openai_compat.is_garbled("abcd" + bad)
    assert not openai_compat.is_garbled("abcde" + bad)
    assert not openai_compat.is_garbled("第一行\n第二行\t（制表符和换行不算乱码）")


@pytest.mark.parametrize(
    ("scenario", "status", "snapshot_media"),
    [
        ("invalid-json", 200, "application/json"),
        ("not-object", 200, "application/json"),
        ("no-choices", 200, "application/json"),
        ("bad-message", 200, "application/json"),
        ("bad-content", 200, "application/json"),
        ("server-error", 500, "application/json"),
        ("error-with-choices", 503, "application/json"),
        ("html-502", 502, "text/html"),
        ("binary-500", 500, "application/octet-stream"),
        ("no-such-scenario", 404, "application/json"),
    ],
)
def test_other_failures_are_error(
    root: Path, scenario: str, status: int, snapshot_media: str
) -> None:
    _, data = collect(root, scenario)
    obs = assert_invalid(root, data, "error")
    assert obs["ext"]["api"]["http_status"] == status
    if status != 200:
        assert obs["ext"]["api"]["detail"].startswith(f"HTTP {status}")
        assert obs["answer_text"] is None, "非 2xx 响应里的内容不当作回答"
    [snapshot] = obs["snapshots"]
    assert snapshot["media_type"] == snapshot_media
    stored = (root / snapshot["path"]).read_bytes()
    assert snapshot["sha256"] == hashlib.sha256(stored).hexdigest()
    if scenario in ("invalid-json", "html-502", "binary-500"):
        assert snapshot["path"].endswith(".raw"), "不是 JSON 的响应体不冒充 .json"
        assert obs["answer_text"] is None


def test_invalid_json_snapshot_is_the_exact_body(root: Path) -> None:
    collect(root, "invalid-json")
    [snapshot] = only(root)["snapshots"]
    assert (root / snapshot["path"]).read_bytes() == b'{"choices": ['


@pytest.mark.parametrize("scenario", ["no-body-200", "crash"])
def test_failures_without_body(root: Path, scenario: str) -> None:
    _, data = collect(root, scenario)
    obs = assert_invalid(root, data, "error")
    assert_no_snapshot(obs)
    assert obs["ext"]["api"]["http_status"] in (200, 500)


def test_oversized_response_is_error_and_not_stored(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openai_compat, "MAX_RESPONSE_BYTES", 1000)
    _, data = collect(root, "large")
    obs = assert_invalid(root, data, "error")
    assert_no_snapshot(obs)
    assert "上限" in obs["ext"]["api"]["detail"]


def test_transport_errors_are_error(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Refusing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连接被拒绝（测试）", request=request)

    monkeypatch.setattr(openai_compat, "asgi_transport", lambda app: Refusing())
    _, data = collect(root, "normal")
    obs = assert_invalid(root, data, "error")
    assert obs["ext"]["api"]["http_status"] is None
    assert "ConnectError" in obs["ext"]["api"]["detail"]
    assert_no_snapshot(obs)


# ------------------------------------------------------------------ 续跑与计数（S15）

MIXED_EXPECTED = {
    "Q002": "timeout",  # 续航
    "Q005": "timeout",  # 续航
    "Q008": "rate_limited",  # 质保
    "Q009": "empty_answer",  # 自燃
    "Q011": "truncated",  # 保值率
}


def test_faults_are_counted_and_rerun_does_not_duplicate(root: Path) -> None:
    code, data = collect(root, "mixed", "--repeats", "2", queries=None)
    assert code == 0
    assert data["planned"] == 22
    assert data["written"] == 22
    assert data["valid"] == 12
    assert data["invalid"] == 10
    assert data["invalid_by_reason"] == {
        "empty_answer": 2,
        "rate_limited": 2,
        "timeout": 4,
        "truncated": 2,
    }
    records = observations(root)
    for record in records:
        expected = MIXED_EXPECTED.get(record["query_id"])
        assert record["invalid_reason"] == expected
        assert record["status"] == ("valid" if expected is None else "invalid")
    calls = len(mock.CALLS)
    audit_events = project.load_audit(root)
    assert len(audit_events) == 1

    code, data = collect(root, "mixed", "--repeats", "2", "--resume", queries=None)
    assert code == 0
    assert (data["planned"], data["skipped_existing"], data["written"]) == (22, 22, 0)
    assert data["resume"] is True
    assert len(mock.CALLS) == calls, "已有记录的任务（含无效）不再发请求"
    assert len(project.load_observations(root)) == 22
    assert len(project.load_audit(root)) == 1, "没有新写入就不写审计事件"

    code, data = collect(root, "mixed", "--repeats", "3", queries=None)
    assert code == 0
    assert (data["planned"], data["skipped_existing"], data["written"]) == (33, 22, 11)
    assert data["invalid_by_reason"] == {
        "empty_answer": 1,
        "rate_limited": 1,
        "timeout": 2,
        "truncated": 1,
    }
    records = observations(root)
    assert len({item["trace_id"] for item in records}) == len(records) == 33
    assert sorted({item["attempt"] for item in records}) == [1, 2, 3]
    assert [item["started_at"] for item in records[22:]] == [
        at(index * 3 + 2) for index in range(11)
    ], "开始时间按任务在计划中的序号推算，续跑也不变"


def test_interrupted_run_is_audited_and_can_be_resumed(root: Path) -> None:
    argv = [
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", APP, "--base-url", "http://mock.invalid/interrupt/v1", "--now", NOW, "--json",
    ]  # fmt: skip
    with pytest.raises(KeyboardInterrupt):
        run_cli(*argv)
    assert len(observations(root)) == mock.INTERRUPT_AFTER["calls"]
    [event] = project.load_audit(root)
    assert event["details"]["interrupted"] is True
    assert event["details"]["written"] == mock.INTERRUPT_AFTER["calls"]

    code, data = collect(root, "normal", queries=None)
    assert code == 0
    assert (data["skipped_existing"], data["written"]) == (3, 8)
    assert len(observations(root)) == 11
    assert project.load_audit(root)[-1]["details"]["interrupted"] is False


def test_same_input_and_now_give_identical_bytes(tmp_path: Path) -> None:
    roots = [tmp_path / "a" / "proj", tmp_path / "b" / "proj"]
    outputs = []
    for item in roots:
        sealed_project(item)
        mock.reset()
        code, data = collect(
            item, "mixed", "--repeats", "2", "--operator", "采集员甲", queries=None
        )
        assert code == 0
        outputs.append(data)
    assert outputs[0] == outputs[1]
    first, second = all_files(roots[0]), all_files(roots[1])
    assert first == second, "观测、快照、审计事件逐字节一致"
    assert any(name.startswith("snapshots/") for name in first)
    for content in first.values():
        assert str(tmp_path).encode() not in content, "输出不含绝对路径"


def test_audit_event(root: Path) -> None:
    code, data = collect(root, "mixed", "--operator", "采集员甲", queries="Q001,Q002")
    assert code == 0
    [event] = project.load_audit(root)
    assert data["audit_event_id"] == event["event_id"]
    assert event["action"] == "collect"
    assert event["actor"] == "采集员甲"
    assert event["target"] == {"type": "observations", "id": "api:engine-a:W1"}
    assert event["occurred_at"] == at(2), "--now 加上任务数（秒）：在全部任务之后"
    assert event["before_hash"] is None
    assert (
        event["after_hash"]
        == hashlib.sha256((root / "observations.jsonl").read_bytes()).hexdigest()
    )
    details = event["details"]
    assert details["adapter"] == "openai"
    assert details["channel"] == "api"
    assert (details["platform"], details["window_id"]) == ("engine-a", "W1")
    assert details["transport"] == "asgi"
    assert details["app"] == "mock_openai_app.py:app"
    assert details["base_url"] == "http://mock.invalid/mixed/v1"
    assert (details["planned"], details["written"], details["valid"], details["invalid"]) == (
        2,
        2,
        1,
        1,
    )
    assert details["invalid_by_reason"] == {"timeout": 1}
    assert details["skipped_existing"] == 0
    assert details["interrupted"] is False
    assert event["synthetic"] is True

    collect(root, "normal", queries="Q003")
    second = project.load_audit(root)[-1]
    assert second["before_hash"] == event["after_hash"]
    assert second["actor"] == "geo-verify collect（未指明操作人）"


def test_module_form_of_asgi_app(root: Path) -> None:
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", "mock_openai_app:app", "--base-url", "http://mock.invalid/normal/v1",
        "--queries", "Q001", "--now", NOW,
    )  # fmt: skip
    assert code == 0, data
    assert data["app"] == "mock_openai_app:app"
    assert only(root)["status"] == "valid"


def test_default_base_url(root: Path) -> None:
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-b", "--window", "W1",
        "--asgi-app", APP, "--queries", "Q001", "--now", NOW,
    )  # fmt: skip
    assert code == 0
    assert data["base_url"] == "http://mock.invalid/v1"
    assert mock.CALLS[0]["path"] == "/v1/chat/completions"
    obs = only(root)
    assert obs["platform"] == "engine-b"
    assert obs["invalid_reason"] == "error", "测试服务在这个路径上返回 404"


def test_text_output_calls_it_api_channel(root: Path) -> None:
    code, out, _ = run_cli(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", APP, "--base-url", "http://mock.invalid/mixed/v1", "--now", NOW,
        "--queries", "Q001,Q002",
    )  # fmt: skip
    assert code == 0
    assert "API 通道" in out
    assert "用户真实所见" not in out
    assert "计划 2" in out
    assert "timeout" in out


# ------------------------------------------------------------------ 密钥与联网


def test_api_key_is_sent_but_never_written(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    outputs = []
    for scenario in ("echo-auth", "server-error", "slow"):
        code, out, err = run_cli(
            "collect", str(root), "--adapter", "openai", "--platform", "engine-a",
            "--window", "W1", "--asgi-app", APP, "--base-url", f"http://mock.invalid/{scenario}/v1",
            "--api-key-env", KEY_ENV, "--timeout", "0.05", "--now", NOW, "--json",
            "--queries", {"echo-auth": "Q001", "server-error": "Q002", "slow": "Q003"}[scenario],
        )  # fmt: skip
        assert code == 0, err
        outputs.append(out + err)
    assert [call["headers"]["authorization"] for call in mock.CALLS] == [f"Bearer {FAKE_KEY}"] * 3
    for text in outputs:
        assert FAKE_KEY not in text
    for name, content in all_files(root).items():
        assert FAKE_KEY.encode() not in content, f"{name} 含有密钥"
    echoed = observations(root)[0]
    assert "[REDACTED]" in echoed["answer_text"]
    assert echoed["ext"]["api"]["secret_redacted"] is True
    assert b"[REDACTED]" in (root / echoed["snapshots"][0]["path"]).read_bytes()
    assert "secret_redacted" not in observations(root)[1]["ext"]["api"]
    event = project.load_audit(root)[0]
    assert event["details"]["api_key_env"] == KEY_ENV


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_api_key_variable_is_input_error(
    root: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(KEY_ENV, value)
    code, data = collect(root, "normal", "--api-key-env", KEY_ENV)
    assert code == 2
    assert KEY_ENV in data["message"]
    assert mock.CALLS == []
    assert project.load_observations(root) == []


def test_default_run_refuses_anything_but_asgi(root: Path) -> None:
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--base-url", "https://api.example.test/v1", "--queries", "Q001",
    )  # fmt: skip
    assert code == 2
    assert "--asgi-app" in data["message"]
    assert "--live" in data["message"]
    assert project.load_observations(root) == []


LIVE = ("--live", "--base-url", "https://api.example.test/v1", "--api-key-env", KEY_ENV)


def live(root: Path, *extra: str) -> tuple[int, Any]:
    return run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--queries", "Q001", "--retry-wait", "0", "--now", NOW, *extra,
    )  # fmt: skip


@pytest.fixture
def fake_live(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把“真实网络”换成进程内测试服务，记录每次请求的完整地址。"""
    seen: list[str] = []

    class Recording(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.inner = httpx.ASGITransport(app=mock.app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return await self.inner.handle_async_request(request)

    monkeypatch.setattr(openai_compat, "live_transport", Recording)
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    return seen


def test_live_requires_base_url_key_env_and_model(root: Path, fake_live: list[str]) -> None:
    cases = [
        (("--live", "--api-key-env", KEY_ENV, "--model", "m"), "--base-url"),
        (("--live", "--base-url", "https://api.example.test/v1", "--model", "m"), "--api-key-env"),
        ((*LIVE,), "--model"),
        ((*LIVE, "--model", "m", "--asgi-app", APP), "--asgi-app"),
    ]
    for extra, flag in cases:
        code, data = live(root, *extra)
        assert code == 2, extra
        assert flag in data["message"]
    assert fake_live == []
    assert project.load_observations(root) == []


def test_live_run_uses_the_real_transport(root: Path, fake_live: list[str]) -> None:
    base = "https://api.example.test/normal/v1"
    code, data = live(
        root, "--live", "--base-url", base, "--api-key-env", KEY_ENV, "--model", "m", "--json"
    )
    assert code == 0, data
    assert data["transport"] == "live"
    assert data["app"] is None
    assert fake_live == [f"{base}/chat/completions"]
    assert mock.CALLS[0]["headers"]["authorization"] == f"Bearer {FAKE_KEY}"
    obs = only(root)
    assert obs["ext"]["api"]["transport"] == "live"
    assert obs["ext"]["api"]["endpoint"] == f"{base}/chat/completions"
    assert obs["status"] == "valid"
    for content in all_files(root).values():
        assert FAKE_KEY.encode() not in content


def test_live_transport_factory_builds_a_plain_http_transport() -> None:
    transport = openai_compat.live_transport()
    assert isinstance(transport, httpx.AsyncHTTPTransport)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.example.test/v1",
        "api.example.test/v1",
        "https:///v1",
        "https://user:secret@api.example.test/v1",
        "https://api.example.test/v1?key=abc",
        "https://api.example.test/v1#frag",
        "http://[::1/v1",
        "https://api.example.test:99999/v1",
    ],
)
def test_bad_base_url_is_rejected(root: Path, fake_live: list[str], url: str) -> None:
    code, data = live(root, "--live", "--base-url", url, "--api-key-env", KEY_ENV, "--model", "m")
    assert code == 2
    assert "--base-url" in data["message"]
    assert "secret" not in data["message"]
    assert fake_live == []


def test_trailing_slash_of_base_url_is_trimmed(root: Path) -> None:
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", APP, "--base-url", "http://mock.invalid/normal/v1/", "--queries", "Q001",
        "--now", NOW,
    )  # fmt: skip
    assert code == 0
    assert data["base_url"] == "http://mock.invalid/normal/v1"
    assert mock.CALLS[0]["path"] == "/normal/v1/chat/completions"


# ------------------------------------------------------------------ 参数检查


@pytest.mark.parametrize(
    ("extra", "flag"),
    [
        (("--timeout", "0"), "--timeout"),
        (("--max-retries", "-1"), "--max-retries"),
        (("--retry-wait", "-0.5"), "--retry-wait"),
        (("--model", "  "), "--model"),
        (("--input", "x.csv"), "--input"),
        (("--out", "pack"), "--out"),
        (("--action", "pack"), "--action"),
        (("--repeats", "0"), "--repeats"),
        (("--queries", "Q999"), "Q999"),
    ],
)
def test_option_errors(root: Path, extra: tuple[str, ...], flag: str) -> None:
    code, data = collect(root, "normal", *extra)
    assert code == 2
    assert data["error"] == "input_error"
    assert flag in data["message"]
    assert mock.CALLS == []
    assert project.load_observations(root) == []


def test_platform_and_window_are_required(root: Path) -> None:
    for missing in ("--platform", "--window"):
        argv = ["--platform", "engine-a", "--window", "W1"]
        index = argv.index(missing)
        del argv[index : index + 2]
        code, data = run_cli_json(
            "collect", str(root), "--adapter", "openai", "--asgi-app", APP, *argv
        )
        assert code == 2
        assert missing in data["message"]


def test_platform_must_register_the_api_channel(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    sealed_project(
        root,
        platforms=[{"platform_id": "engine-m", "display_name": "人工平台", "channels": ["manual"]}],
    )
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-m", "--window", "W1",
        "--asgi-app", APP,
    )  # fmt: skip
    assert code == 2
    assert "api" in data["message"]
    assert mock.CALLS == []


@pytest.mark.parametrize(
    ("ref", "hint"),
    [
        ("no_colon_here", "模块或文件"),
        ("mock_openai_app:", "模块或文件"),
        ("geo_verify_no_such_module_xyz:app", "geo_verify_no_such_module_xyz"),
        ("does/not/exist.py:app", "exist.py"),
        ("mock_openai_app:nope", "nope"),
        ("mock_openai_app:NOT_AN_APP", "NOT_AN_APP"),
    ],
)
def test_bad_asgi_app_reference(root: Path, ref: str, hint: str) -> None:
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", ref, "--queries", "Q001",
    )  # fmt: skip
    assert code == 2
    assert hint in data["message"]
    assert project.load_observations(root) == []


def test_asgi_file_that_fails_to_import(root: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken_app.py"
    broken.write_text("raise ValueError('导入失败（测试）')\n", encoding="utf-8")
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", f"{broken}:app", "--queries", "Q001",
    )  # fmt: skip
    assert code == 2
    assert "ValueError" in data["message"]


def test_observation_id_taken_by_another_record_is_refused(root: Path) -> None:
    proj = project.load_project(root)
    sealed = project.select_prompt_set(root)
    digest = task_ids(root, "Q001")
    other = make_observation(proj, sealed, 1, observation_id="o-" + digest, trace_id="t-other")
    write_records(root, observations=[other])
    code, data = collect(root, "normal")
    assert code == 2
    assert "o-" + digest in data["message"]
    assert mock.CALLS == []


def test_other_adapters_reject_openai_options(root: Path, tmp_path: Path) -> None:
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "manual", "--action", "pack", "--platform", "engine-a",
        "--window", "W1", "--out", str(tmp_path / "pack"), "--model", "m",
    )  # fmt: skip
    assert code == 2
    assert "--model" in data["message"]
    assert not (tmp_path / "pack").exists()


def test_existing_snapshot_files_are_never_overwritten(root: Path) -> None:
    digest = task_ids(root, "Q001")
    snapshots = root / "snapshots"
    snapshots.mkdir()
    same = snapshots / f"o-{digest}.json"
    same.write_bytes(response_bytes(mock.completion()))  # 上次运行中断前已写入的同一份响应
    code, data = collect(root, "normal")
    assert code == 0
    assert data["snapshots_written"] == 0
    assert only(root)["snapshots"][0]["path"] == f"snapshots/o-{digest}.json"

    other_root = root.parent / "other"
    sealed_project(other_root)
    stale = other_root / "snapshots" / f"o-{digest}.json"
    stale.parent.mkdir()
    stale.write_bytes(b"stale content")
    code, data = collect(other_root, "normal")
    assert code == 0
    assert data["snapshots_written"] == 1
    [snapshot] = only(other_root)["snapshots"]
    assert snapshot["path"] == f"snapshots/o-{digest}.2.json"
    assert stale.read_bytes() == b"stale content", "已有文件不被覆盖"
    assert (other_root / snapshot["path"]).read_bytes() == response_bytes(mock.completion())


def test_non_synthetic_project_has_no_synthetic_flag(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    sealed_project(root, synthetic=False)
    assert collect(root, "normal")[0] == 0
    assert "synthetic" not in only(root)
    assert "synthetic" not in project.load_audit(root)[0]


def test_secret_never_shows_its_value() -> None:
    secret = openai_compat.Secret(FAKE_KEY)
    assert FAKE_KEY not in repr(secret)
    assert FAKE_KEY not in str(secret)
    assert FAKE_KEY not in f"{secret}"
    assert secret.header() == f"Bearer {FAKE_KEY}"
    redacted, found = secret.redact(f'{{"a":"{FAKE_KEY}"}}'.encode())
    assert (redacted, found) == (b'{"a":"[REDACTED]"}', True)
    assert secret.redact(b"nothing here") == (b"nothing here", False)


def test_bad_api_key_variable_name(root: Path) -> None:
    code, data = collect(root, "normal", "--api-key-env", "NOT-A-NAME")
    assert code == 2
    assert "--api-key-env" in data["message"]


def test_bad_now_is_input_error(root: Path) -> None:
    code, data = collect(root, "normal", "--now", "2026-10-08 02:00")
    assert code == 2
    assert "2026-10-08 02:00" in data["message"]
    assert mock.CALLS == []


def test_asgi_file_that_is_not_python(root: Path, tmp_path: Path) -> None:
    other = tmp_path / "app.txt"
    other.write_text("app = None\n", encoding="utf-8")
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", f"{other}:app", "--queries", "Q001",
    )  # fmt: skip
    assert code == 2
    assert "app.txt" in data["message"]


def test_asgi_file_is_loaded_once(root: Path, tmp_path: Path) -> None:
    fresh = tmp_path / "fresh_app.py"
    fresh.write_text(
        "from mock_openai_app import app\nLOADS = []\nLOADS.append(1)\n", encoding="utf-8"
    )
    for query_id in ("Q001", "Q002"):
        code, data = run_cli_json(
            "collect", str(root), "--adapter", "openai", "--platform", "engine-a",
            "--window", "W1", "--asgi-app", f"{fresh}:app",
            "--base-url", "http://mock.invalid/normal/v1", "--queries", query_id, "--now", NOW,
        )  # fmt: skip
        assert code == 0, data
        assert data["app"] == "fresh_app.py:app"
    assert len(mock.CALLS) == 2
    [loaded] = [
        module
        for module in sys.modules.values()
        if getattr(module, "__file__", None) == str(fresh.resolve())
    ]
    assert loaded.LOADS == [1], "同一文件只执行一次"
