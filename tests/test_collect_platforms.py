"""平台配置项（collect/platforms.toml）与 --platform-config 的用法。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import mock_openai_app as mock
import pytest
from conftest import run_cli_json
from helpers_basic import INTENT_TEXTS, sealed_project

from geo_verify import project
from geo_verify.collect import openai_compat, platforms
from geo_verify.errors import InputError

NOW = "2026-10-08T02:00:00Z"
KEY_ENV = "GEO_VERIFY_TEST_API_KEY"
BASE = "https://api.example.test/normal/v1"
APP = f"{Path(__file__).with_name('mock_openai_app.py')}:app"
BUILTIN = {
    "deepseek": "DeepSeek",
    "kimi": "Kimi",
    "yuanbao": "元宝",
    "doubao": "豆包",
    "wenxin": "文心",
    "qianwen": "千问",
}

VERIFIED_ENTRY = """
[platforms.example-engine]
display_name = "示例平台（虚构）"
base_url = "https://api.example.test/normal/v1"
model = "example-model"
web_search_params = { enable_search = true }
verified = true
status = "已核实"
source_url = "https://docs.example.test/api"
checked_at = "2026-11-01"
note = "测试用的虚构配置"
"""


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


@pytest.fixture
def fake_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_compat, "live_transport", lambda: httpx.ASGITransport(app=mock.app))
    monkeypatch.setenv(KEY_ENV, "sk-geo-test-FAKE-platforms")


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "my-platforms.toml"
    path.write_text(text, encoding="utf-8")
    return path


def live(root: Path, *extra: str, base: str = BASE) -> tuple[int, Any]:
    return run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--queries", "Q001", "--live", "--base-url", base, "--api-key-env", KEY_ENV,
        *extra,
    )  # fmt: skip


# ------------------------------------------------------------------ 随包分发的配置


def test_builtin_entries_are_placeholders_marked_unverified() -> None:
    configs = platforms.load_platform_configs()
    assert {name: item.display_name for name, item in configs.items()} == BUILTIN
    for item in configs.values():
        assert item.verified is False
        assert item.status == "未核实"
        assert (item.base_url, item.model, item.source_url, item.checked_at) == (
            None,
            None,
            None,
            None,
        )
        assert item.web_search_params == {}
        assert item.note is not None
        assert "人工" in item.note


def test_builtin_file_hard_codes_no_address_or_model() -> None:
    text = platforms.builtin_text()
    assert "://" not in text, "接口地址不得凭记忆写死"
    assert "须由人工查阅该平台文档后填写" in text
    assert "来源链接" in text
    assert "核对日期" in text
    assert "官方" not in text


def test_lookup_of_unknown_platform_lists_the_known_ones() -> None:
    with pytest.raises(InputError) as caught:
        platforms.platform_config("no-such-platform")
    assert "deepseek" in caught.value.message


# ------------------------------------------------------------------ 命令行


def test_unverified_platform_is_refused_for_live(root: Path, fake_live: None) -> None:
    code, data = live(root, "--model", "m", "--platform-config", "deepseek")
    assert code == 2
    assert "deepseek" in data["message"]
    assert "未核实" in data["message"]
    assert "核实" in data["message"]
    assert mock.CALLS == []
    assert project.load_observations(root) == []


def test_unverified_platform_may_be_used_against_the_mock(root: Path) -> None:
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", APP, "--base-url", "http://mock.invalid/normal/v1", "--queries", "Q001",
        "--platform-config", "kimi", "--now", NOW,
    )  # fmt: skip
    assert code == 0
    assert data["platform_config"]["verified"] is False
    assert any("未核实" in item for item in data["warnings"])
    assert mock.CALLS[0]["body"] == {
        "messages": [{"role": "user", "content": INTENT_TEXTS["brand"]}]
    }


def test_verified_config_supplies_model_and_web_search_params(
    root: Path, tmp_path: Path, fake_live: None
) -> None:
    config = write_config(tmp_path, VERIFIED_ENTRY)
    code, data = live(
        root, "--platform-config", "example-engine", "--platforms-file", str(config),
        "--web-search", "true",
    )  # fmt: skip
    assert code == 0, data
    assert mock.CALLS[0]["body"] == {
        "model": "example-model",
        "messages": [{"role": "user", "content": INTENT_TEXTS["brand"]}],
        "enable_search": True,
    }
    assert data["platform_config"] == {
        "name": "example-engine",
        "verified": True,
        "status": "已核实",
        "source_url": "https://docs.example.test/api",
        "checked_at": "2026-11-01",
    }
    assert data["warnings"] == []
    [obs] = project.load_observations(root)
    assert obs["web_search"] is True
    assert obs["ext"]["api"]["platform_config"] == "example-engine"
    assert obs["ext"]["api"]["request_model"] == "example-model"


def test_command_line_overrides_config(root: Path, tmp_path: Path, fake_live: None) -> None:
    config = write_config(tmp_path, VERIFIED_ENTRY)
    other = "https://api.example.test/normal/v2"
    code, data = live(
        root, "--platform-config", "example-engine", "--platforms-file", str(config),
        "--model", "override-model", "--web-search", "false", base=other,
    )  # fmt: skip
    assert code == 0, data
    assert mock.CALLS[0]["path"] == "/normal/v2/chat/completions"
    assert mock.CALLS[0]["body"] == {
        "model": "override-model",
        "messages": [{"role": "user", "content": INTENT_TEXTS["brand"]}],
    }, "未开启联网时不并入联网参数"
    assert any("base_url" in item for item in data["warnings"])


def test_web_search_without_known_parameter_only_records_the_declaration(
    root: Path, tmp_path: Path, fake_live: None
) -> None:
    config = write_config(tmp_path, VERIFIED_ENTRY.replace("{ enable_search = true }", "{}"))
    code, data = live(
        root, "--platform-config", "example-engine", "--platforms-file", str(config),
        "--web-search", "true",
    )  # fmt: skip
    assert code == 0
    assert "enable_search" not in mock.CALLS[0]["body"]
    assert any("联网参数" in item for item in data["warnings"])


def test_platforms_file_needs_platform_config(root: Path, tmp_path: Path) -> None:
    config = write_config(tmp_path, VERIFIED_ENTRY)
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", APP, "--platforms-file", str(config),
    )  # fmt: skip
    assert code == 2
    assert "--platform-config" in data["message"]


def test_unknown_platform_config_is_input_error(root: Path) -> None:
    code, data = run_cli_json(
        "collect", str(root), "--adapter", "openai", "--platform", "engine-a", "--window", "W1",
        "--asgi-app", APP, "--platform-config", "nope",
    )  # fmt: skip
    assert code == 2
    assert "nope" in data["message"]


# ------------------------------------------------------------------ 配置文件校验


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("[platforms.x\n", "TOML"),
        ("title = 1\n", "platforms"),
        ("platforms = 1\n", "platforms"),
        ("[platforms]\nx = 1\n", "x"),
        (VERIFIED_ENTRY.replace('note = "测试用的虚构配置"', 'colour = "red"'), "colour"),
        (VERIFIED_ENTRY.replace('display_name = "示例平台（虚构）"\n', ""), "display_name"),
        (VERIFIED_ENTRY.replace("verified = true", 'verified = "yes"'), "verified"),
        (VERIFIED_ENTRY.replace('model = "example-model"', "model = 7"), "model"),
        (VERIFIED_ENTRY.replace('status = "已核实"', 'status = "未核实"'), "status"),
        (VERIFIED_ENTRY.replace('status = "已核实"', 'status = "大概对"'), "status"),
        (VERIFIED_ENTRY.replace('model = "example-model"', 'model = ""'), "model"),
        (VERIFIED_ENTRY.replace('source_url = "https://docs.example.test/api"', 'source_url = ""'), "source_url"),
        (VERIFIED_ENTRY.replace('source_url = "https://docs.example.test/api"', 'source_url = "docs"'), "source_url"),
        (VERIFIED_ENTRY.replace('base_url = "https://api.example.test/normal/v1"', 'base_url = "ftp://x.test/v1"'), "base_url"),
        (VERIFIED_ENTRY.replace('checked_at = "2026-11-01"', 'checked_at = "2026-13-01"'), "checked_at"),
        (VERIFIED_ENTRY.replace('checked_at = "2026-11-01"', 'checked_at = "昨天"'), "checked_at"),
        (VERIFIED_ENTRY.replace("{ enable_search = true }", "true"), "web_search_params"),
        (VERIFIED_ENTRY.replace("{ enable_search = true }", '{ model = "x" }'), "web_search_params"),
        (VERIFIED_ENTRY.replace("[platforms.example-engine]", '[platforms."bad name"]'), "bad name"),
    ],
)  # fmt: skip
def test_invalid_config_files_are_rejected(tmp_path: Path, text: str, hint: str) -> None:
    path = write_config(tmp_path, text)
    with pytest.raises(InputError) as caught:
        platforms.load_platform_configs(path)
    assert hint in caught.value.message
    assert path.name in caught.value.message


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(InputError) as caught:
        platforms.load_platform_configs(tmp_path / "absent.toml")
    assert "absent.toml" in caught.value.message


def test_unverified_entry_may_leave_everything_empty(tmp_path: Path) -> None:
    text = VERIFIED_ENTRY.replace("verified = true", "verified = false").replace(
        'status = "已核实"', 'status = "未核实"'
    )
    for name in ("base_url", "model", "source_url", "checked_at"):
        text = "\n".join(
            f'{name} = ""' if line.startswith(f"{name} =") else line for line in text.splitlines()
        )
    path = write_config(tmp_path, text.replace('note = "测试用的虚构配置"', ""))
    [item] = platforms.load_platform_configs(path).values()
    assert (item.verified, item.base_url, item.model, item.note) == (False, None, None, None)
    with pytest.raises(InputError) as caught:
        platforms.require_verified(item)
    assert "example-engine" in caught.value.message


def test_config_file_must_be_utf8(tmp_path: Path) -> None:
    path = tmp_path / "gbk.toml"
    path.write_bytes(VERIFIED_ENTRY.encode("gbk"))
    with pytest.raises(InputError) as caught:
        platforms.load_platform_configs(path)
    assert "UTF-8" in caught.value.message
