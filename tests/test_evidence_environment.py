"""证据包的代码版本与依赖锁定位。git 与安装元数据都用替身构造，结果不随运行环境变化。"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

import geo_verify
from geo_verify.evidence import environment

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def test_tool_identity() -> None:
    assert environment.tool() == {"name": "geo-verify", "version": geo_verify.__version__}


def test_code_version_starts_with_package_version() -> None:
    assert environment.code_version().startswith(geo_verify.__version__)


def test_without_source_root_only_the_package_version_is_used() -> None:
    assert environment.describe_code_version(None) == geo_verify.__version__


def fake_git(answers: dict[str, str | None]):  # type: ignore[no-untyped-def]
    def run(git: str, root: Path, *args: str) -> str | None:
        return answers[args[0] if args[0] != "rev-parse" else args[1]]

    return run


@pytest.mark.parametrize(
    ("status", "suffix"),
    [("", ""), (" M src/x.py", ".dirty"), (None, ".dirty")],
    ids=["clean", "dirty", "status-unknown"],
)
def test_git_commit_is_appended_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str | None, suffix: str
) -> None:
    monkeypatch.setattr(environment.shutil, "which", lambda name: "/usr/bin/git")
    answers = {"--show-toplevel": str(tmp_path), "HEAD": COMMIT, "status": status}
    monkeypatch.setattr(environment, "run_git", fake_git(answers))
    assert environment.describe_code_version(tmp_path) == (
        f"{geo_verify.__version__}+git.{COMMIT}{suffix}"
    )


@pytest.mark.parametrize(
    "answers",
    [
        {"--show-toplevel": None, "HEAD": COMMIT, "status": ""},
        {"--show-toplevel": "/somewhere/else", "HEAD": COMMIT, "status": ""},
        {"--show-toplevel": "ROOT", "HEAD": None, "status": ""},
        {"--show-toplevel": "ROOT", "HEAD": "not-a-commit", "status": ""},
    ],
    ids=["not-a-repo", "enclosing-repo", "no-head", "bad-head"],
)
def test_git_problems_fall_back_to_package_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: dict[str, str | None]
) -> None:
    monkeypatch.setattr(environment.shutil, "which", lambda name: "/usr/bin/git")
    resolved = {
        key: (str(tmp_path) if value == "ROOT" else value) for key, value in answers.items()
    }
    monkeypatch.setattr(environment, "run_git", fake_git(resolved))
    assert environment.git_state(tmp_path) is None
    assert environment.describe_code_version(tmp_path) == geo_verify.__version__


def test_missing_git_binary_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(environment.shutil, "which", lambda name: None)
    assert environment.git_state(tmp_path) is None


def test_run_git_returns_stripped_output_of_a_successful_command(tmp_path: Path) -> None:
    script = tmp_path / "fake-git"
    script.write_text('#!/bin/sh\necho "  fake output  "\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    assert environment.run_git(str(script), tmp_path, "rev-parse", "HEAD") == "fake output"


def test_run_git_failures_return_none(tmp_path: Path) -> None:
    assert environment.run_git(str(tmp_path / "no-such-git"), tmp_path, "status") is None
    # 换成一个会以非零状态退出的程序
    assert environment.run_git(sys.executable, tmp_path, "rev-parse", "HEAD") is None


def test_source_root_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = tmp_path / "site-packages" / "geo_verify" / "__init__.py"
    monkeypatch.setattr(geo_verify, "__file__", str(installed))
    assert environment.source_root() is None
    checkout = tmp_path / "checkout"
    (checkout / "src" / "geo_verify").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(geo_verify, "__file__", str(checkout / "src" / "geo_verify" / "x.py"))
    assert environment.source_root() == checkout.resolve()
    (checkout / "pyproject.toml").unlink()
    assert environment.source_root() is None


def direct_url(url: object) -> str:
    return json.dumps({"url": url, "dir_info": {"editable": True}})


def test_lock_is_found_next_to_a_local_source_directory(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    assert environment.lock_from_direct_url(direct_url(tmp_path.as_uri())) == tmp_path / "uv.lock"


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "{not json",
        "[1, 2]",
        json.dumps({"dir_info": {}}),
        direct_url(42),
        direct_url("https://example.test/geo-verify.tar.gz"),
    ],
    ids=["none", "empty", "broken", "not-object", "no-url", "url-not-text", "remote"],
)
def test_lock_lookup_without_usable_metadata(text: str | None) -> None:
    assert environment.lock_from_direct_url(text) is None


def test_lock_lookup_in_directory_without_lock(tmp_path: Path) -> None:
    assert environment.lock_from_direct_url(direct_url(tmp_path.as_uri())) is None


def test_installed_lock_lookup_for_unknown_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environment, "DIST_NAME", "geo-no-such-distribution")
    assert environment.installed_lock_path() is None


def test_installed_lock_lookup_returns_a_lock_file_or_none() -> None:
    found = environment.installed_lock_path()
    assert found is None or (found.name == "uv.lock" and found.is_file())
