"""读不了的输入文件一律是输入错误（退出 2），不能按内部错误退出 4、打印 traceback。

“读不了”包括：路径是目录、内容不是 UTF-8、没有读权限（用 monkeypatch 模拟，避免依赖运行用户）。
联调时发现 validate、prompts lint、exclude --ids-file 等命令遇到这类文件会直接抛出 Python 异常。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli
from helpers_pipeline import write_inputs

from geo_verify import jsonl

BAD_BYTES = b"\xff\xfe\x00bad"
LOCKED = "locked"
APP = f"{Path(__file__).with_name('mock_openai_app.py')}:app"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write_inputs(root)
    document = json.loads((root / "project.json").read_text(encoding="utf-8"))
    document["platforms"][0]["channels"].append("share_import")
    (root / "project.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    code, _, err = run_cli(
        "prompts", "seal", str(root), "--by", "测试", "--now", "2026-10-02T03:00:00Z"
    )
    assert code == 0, err
    return root


def _lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """名字以 locked 开头的文件读取时抛出 PermissionError（模拟没有读权限）。"""

    def guard(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self.name.startswith(LOCKED):
                raise PermissionError(13, "Permission denied", str(self))
            return original(self, *args, **kwargs)

        return wrapper

    for name in ("open", "read_bytes", "read_text"):
        monkeypatch.setattr(Path, name, guard(getattr(Path, name)))


def _make(tmp_path: Path, kind: str, suffix: str) -> Path:
    if kind == "directory":
        path = tmp_path / f"folder{suffix}"
        path.mkdir()
    elif kind == "not_utf8":
        path = tmp_path / f"bad{suffix}"
        path.write_bytes(BAD_BYTES)
    else:
        path = tmp_path / f"{LOCKED}{suffix}"
        path.write_text("ok\n", encoding="utf-8")
    return path


COMMANDS: dict[str, tuple[str, list[str]]] = {
    "validate": (".jsonl", ["validate", "{file}", "--type", "observation"]),
    "validate_json": (".json", ["validate", "{file}", "--type", "project"]),
    "prompts_lint": (".jsonl", ["prompts", "lint", "{root}", "--file", "{file}"]),
    "import": (".csv", ["import", "{root}", "--input", "{file}"]),
    "annotate_import": (
        ".csv",
        ["annotate", "import", "{root}", "--input", "{file}", "--annotator", "甲"],
    ),
    "recheck_compare": (".csv", ["recheck-compare", "{root}", "--input", "{file}"]),
    "exclude": (
        ".txt",
        [
            "exclude",
            "{root}",
            "--rule-id",
            "EX-DUP",
            "--ids-file",
            "{file}",
            "--actor",
            "甲",
            "--reason",
            "测试",
        ],
    ),
    "manual_fill": (
        ".csv",
        ["collect", "{root}", "--adapter", "manual", "--action", "fill", "--input", "{file}"],
    ),
    "share": (
        ".json",
        [
            "collect",
            "{root}",
            "--adapter",
            "share",
            "--input",
            "{file}",
            "--platform",
            "engine-a",
            "--window",
            "W1",
            "--query",
            "Q001",
        ],
    ),
    "platforms_file": (
        ".toml",
        [
            "collect",
            "{root}",
            "--adapter",
            "openai",
            "--platform",
            "engine-a",
            "--window",
            "W1",
            "--asgi-app",
            APP,
            "--platform-config",
            "deepseek",
            "--platforms-file",
            "{file}",
        ],
    ),
}


@pytest.mark.parametrize("kind", ["directory", "not_utf8", "unreadable"])
@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_unreadable_input_is_an_input_error(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, kind: str
) -> None:
    suffix, template = COMMANDS[command]
    path = _make(tmp_path, kind, suffix)
    _lock(monkeypatch)
    argv = [part.format(root=project, file=path) for part in template]

    code, out, err = run_cli(*argv, "--json")

    assert "Traceback" not in err
    assert code == 2, (out, err)
    payload = json.loads(out)
    assert payload["error"] == "input_error"
    assert path.name in payload["message"]  # 指明是哪个文件读不了


@pytest.mark.parametrize("kind", ["directory", "not_utf8", "unreadable"])
@pytest.mark.parametrize("name", ["project.json", "observations.jsonl"])
def test_unreadable_project_file_is_an_input_error(
    project: Path, monkeypatch: pytest.MonkeyPatch, name: str, kind: str
) -> None:
    target = project / name
    if target.exists():
        target.unlink()
    if kind == "directory":
        target.mkdir()
    else:
        target.write_bytes(BAD_BYTES if kind == "not_utf8" else b"{}\n")
    if kind == "unreadable":

        def deny(original: Callable[..., Any]) -> Callable[..., Any]:
            def wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
                if self.name == name:
                    raise PermissionError(13, "Permission denied", str(self))
                return original(self, *args, **kwargs)

            return wrapper

        for method in ("open", "read_bytes", "read_text"):
            monkeypatch.setattr(Path, method, deny(getattr(Path, method)))

    code, out, err = run_cli("metrics", str(project), "--json")

    assert "Traceback" not in err
    assert code == 2, (out, err)
    assert json.loads(out)["error"] == "input_error"


def test_read_helpers_name_the_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "folder.json"
    folder.mkdir()
    with pytest.raises(jsonl.InputError, match="是目录"):
        jsonl.read_json(folder)
    with pytest.raises(jsonl.InputError, match="是目录"):
        jsonl.read_jsonl(folder)
    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(b'{"a":1}\n' + BAD_BYTES + b"\n")
    with pytest.raises(jsonl.InputError, match="不是 UTF-8"):
        jsonl.read_jsonl(bad)
    with pytest.raises(jsonl.InputError, match="不是 UTF-8"):
        jsonl.read_text(bad)
    locked = tmp_path / f"{LOCKED}.json"
    locked.write_text("{}", encoding="utf-8")
    _lock(monkeypatch)
    with pytest.raises(jsonl.InputError, match="无法读取"):
        jsonl.read_json(locked)
    with pytest.raises(jsonl.InputError, match="无法读取"):
        jsonl.read_bytes(locked)
    with pytest.raises(jsonl.InputError, match="文件不存在"):
        jsonl.read_bytes(tmp_path / "none.bin")
