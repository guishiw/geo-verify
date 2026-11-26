"""测试用的极小 OpenAI 兼容 ASGI 应用：进程内调用，不监听端口、不联网。

    POST /<场景>/v1/chat/completions

路径的第一段选择场景（见 SCENARIOS）。每次请求都记入 CALLS，供测试断言请求体和请求头；
测试开始前调用 reset() 清空记录和计数。全部内容为虚构。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
Reply = tuple[int, bytes, str, float]  # (状态码, 响应体, Content-Type, 延迟秒数)

MODEL = "mock-model-2026.10"
ANSWER = "澜序 S7 的 CLTC 续航为 620 km（虚构回答）。"
JSON_TYPE = "application/json"
SLOW_SECONDS = 5.0
FLAKY_FAILURES = 2

CALLS: list[dict[str, Any]] = []
_ATTEMPTS: Counter[tuple[str, str]] = Counter()
INTERRUPT_AFTER = {"calls": 3}


class Interrupted(KeyboardInterrupt):
    """模拟使用者在采集中途按下 Ctrl-C。"""


def reset() -> None:
    CALLS.clear()
    _ATTEMPTS.clear()


def _json(payload: Any, status: int = 200, delay: float = 0.0) -> Reply:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return status, body, JSON_TYPE, delay


def completion(
    content: Any = ANSWER,
    *,
    finish_reason: str | None = "stop",
    model: str | None = MODEL,
    annotations: Any = None,
    citations: Any = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if annotations is not None:
        message["annotations"] = annotations
    payload: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1791000000,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if model is not None:
        payload["model"] = model
    if citations is not None:
        payload["citations"] = citations
    return payload


def _error(status: int, message: str) -> Reply:
    return _json({"error": {"message": message, "type": "test_error", "code": status}}, status)


def url_citation(url: Any, title: Any = None) -> dict[str, Any]:
    return {"type": "url_citation", "url_citation": {"url": url, "title": title}}


def _annotations(prompt: str, request: dict[str, Any]) -> Reply:
    annotations = [
        url_citation("https://lanxu-auto.example/s7/spec", "澜序 S7 参数（虚构）"),
        url_citation("https://auto-review.example/review/1001", "  "),
        {"type": "file_citation", "file_citation": {"file_id": "file-1"}},
        {"type": "url_citation", "url_citation": {"title": "缺少链接"}},
        "不是对象",
        url_citation("https://lanxu-auto.example/s7/spec", "澜序 S7 参数（虚构）"),
    ]
    return _json(completion(annotations=annotations))


def _citations_urls(prompt: str, request: dict[str, Any]) -> Reply:
    content = (
        "澜序 S7 的 CLTC 续航为 620 km[1]。更多评测见 https://ev-forum.example/topic/3003 。"
        "（虚构回答，第 10 条来源未在正文标注）"
    )
    citations = [
        "https://gov-data.example/data/1001",
        "https://auto-review.example/review/2002",
        "https://ev-forum.example/topic/3003",
        "https://gov-data.example/data/1001",
    ]
    return _json(completion(content, citations=citations))


def _citations_objects(prompt: str, request: dict[str, Any]) -> Reply:
    content = "澜序 S7 售价 21.98 万元[2]，上市时间为 2026-03-18[3]。（虚构回答）"
    citations = [
        {"url": "https://news-auto.example/article/1", "title": "新闻（虚构）"},
        {"url": "https://lanxu-auto.example/s7/price", "title": "价格（虚构）"},
        {"url": "not a url", "title": None},
        {"title": "没有链接"},
        42,
    ]
    return _json(completion(content, citations=citations))


def _flaky(prompt: str, request: dict[str, Any]) -> Reply:
    key = ("flaky429", request["request_id"])
    _ATTEMPTS[key] += 1
    if _ATTEMPTS[key] <= FLAKY_FAILURES:
        return _error(429, "请求过于频繁（测试）")
    return _json(completion())


def _mixed(prompt: str, request: dict[str, Any]) -> Reply:
    """按题目文本里的关键词决定结果，便于测试独立推算每道题的预期状态。"""
    if "续航" in prompt:
        return _error(504, "上游响应超时（测试）")
    if "质保" in prompt:
        return _error(429, "请求过于频繁（测试）")
    if "自燃" in prompt:
        return _json(completion(""))
    if "保值率" in prompt:
        return _json(completion("新能源车的保值率受", finish_reason="length"))
    return _json(completion())


def _interrupt(prompt: str, request: dict[str, Any]) -> Reply:
    if len([call for call in CALLS if call["scenario"] == "interrupt"]) > INTERRUPT_AFTER["calls"]:
        raise Interrupted
    return _json(completion())


def _echo_auth(prompt: str, request: dict[str, Any]) -> Reply:
    authorization = request["headers"].get("authorization", "")
    payload = completion(f"收到的授权头：{authorization}（测试服务故意回显）")
    payload["echo"] = {"authorization": authorization}
    return _json(payload)


def _crash(prompt: str, request: dict[str, Any]) -> Reply:
    raise RuntimeError("测试服务内部异常")


def _per_prompt_model(prompt: str, request: dict[str, Any]) -> Reply:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:6]
    return _json(completion(f"（虚构回答 {digest}）可以考虑澜序 S7。"))


SCENARIOS: dict[str, Callable[[str, dict[str, Any]], Reply]] = {
    "normal": lambda prompt, request: _json(completion()),
    "varied": _per_prompt_model,
    "no-model": lambda prompt, request: _json(completion(model=None)),
    "blank-model": lambda prompt, request: _json(completion(model="  ")),
    "annotations": _annotations,
    "citations-urls": _citations_urls,
    "citations-objects": _citations_objects,
    "citations-not-list": lambda prompt, request: _json(completion(citations={"url": "x"})),
    "annotations-not-list": lambda prompt, request: _json(
        completion(annotations={"type": "url_citation"})
    ),
    "parts": lambda prompt, request: _json(
        completion(
            [
                {"type": "text", "text": "澜序 S7 "},
                {"type": "image_url", "image_url": {"url": "https://lanxu-auto.example/a.png"}},
                {"type": "text", "text": "值得考虑（虚构）。"},
            ]
        )
    ),
    "bad-content": lambda prompt, request: _json(completion({"text": "不是字符串"})),
    "timeout504": lambda prompt, request: _error(504, "上游响应超时（测试）"),
    "timeout408": lambda prompt, request: (408, b"", "text/plain", 0.0),
    "slow": lambda prompt, request: _json(completion(), delay=SLOW_SECONDS),
    "ratelimit": lambda prompt, request: _error(429, "请求过于频繁（测试）"),
    "flaky429": _flaky,
    "empty": lambda prompt, request: _json(completion("  \n ")),
    "null-content": lambda prompt, request: _json(completion(None)),
    "truncated": lambda prompt, request: _json(
        completion("澜序 S7 的 CLTC 续航为", finish_reason="length")
    ),
    "garbled": lambda prompt, request: _json(completion(chr(0xFFFD) * 6 + "S7" + chr(0xFFFD) * 4)),
    "control-chars": lambda prompt, request: _json(completion("澜序" + chr(0x07) * 3 + chr(0x00))),
    "few-replacements": lambda prompt, request: _json(
        completion("澜序 S7 的 CLTC 续航为 620 km，" + chr(0xFFFD) + "（虚构回答）。")
    ),
    "invalid-json": lambda prompt, request: (200, b'{"choices": [', JSON_TYPE, 0.0),
    "not-object": lambda prompt, request: _json(["不是对象"]),
    "no-choices": lambda prompt, request: _json({"object": "chat.completion", "choices": []}),
    "bad-message": lambda prompt, request: _json({"choices": [{"message": "不是对象"}]}),
    "html-502": lambda prompt, request: (
        502,
        "<html><body>网关错误（测试）</body></html>".encode(),
        "text/html; charset=utf-8",
        0.0,
    ),
    "binary-500": lambda prompt, request: (500, b"\x00\x01\x02", "", 0.0),
    "server-error": lambda prompt, request: _error(500, "内部错误（测试）"),
    "error-with-choices": lambda prompt, request: _json(completion(), status=503),
    "no-body-200": lambda prompt, request: (200, b"", JSON_TYPE, 0.0),
    "large": lambda prompt, request: _json(completion("长" * 4096)),
    "mixed": _mixed,
    "interrupt": _interrupt,
    "echo-auth": _echo_auth,
    "crash": _crash,
}


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] != "http":
        return
    parts = [part for part in str(scope["path"]).split("/") if part]
    headers = {
        bytes(name).decode("latin-1").lower(): bytes(value).decode("utf-8")
        for name, value in scope.get("headers", [])
    }
    raw = await _read_body(receive)
    scenario = parts[0] if parts else ""
    request: dict[str, Any] = {
        "scenario": scenario,
        "method": scope["method"],
        "path": scope["path"],
        "headers": headers,
        "request_id": headers.get("x-request-id", ""),
        "body": json.loads(raw.decode("utf-8")) if raw else None,
    }
    CALLS.append(request)
    handler = SCENARIOS.get(scenario)
    if handler is None or parts[1:] != ["v1", "chat", "completions"]:
        reply = _error(404, "路径不存在（测试）")
    else:
        prompt = request["body"]["messages"][-1]["content"]
        reply = handler(prompt, request)
    status, body, content_type, delay = reply
    if delay:
        await asyncio.sleep(delay)
    response_headers = [(b"content-length", str(len(body)).encode("ascii"))]
    if content_type:
        response_headers.append((b"content-type", content_type.encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": response_headers})
    await send({"type": "http.response.body", "body": body})


NOT_AN_APP = "这不是 ASGI 应用"
