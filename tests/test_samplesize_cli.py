"""`geo-verify samplesize` 命令测试。期望值取自 GOAL.md 5.4 黄金值。"""

from __future__ import annotations

import pytest
from conftest import run_cli, run_cli_json


@pytest.mark.parametrize(
    ("p1", "p2", "expected"),
    [("0.60", "0.75", 150), ("0.60", "0.65", 1468), ("0.30", "0.40", 354)],
)
def test_two_proportion_mode(p1: str, p2: str, expected: int) -> None:
    code, data = run_cli_json("samplesize", "--p1", p1, "--p2", p2)
    assert code == 0
    assert data["mode"] == "two_proportion"
    assert data["n_per_group"] == expected
    assert data["n_total"] == 2 * expected
    assert data["alpha"] == 0.05
    assert data["power"] == 0.8
    assert "非合并方差" in data["basis"]
    assert "无连续性校正" in data["basis"]


def test_two_proportion_text_output_states_basis() -> None:
    code, out, _ = run_cli("samplesize", "--p1", "0.6", "--p2", "0.75")
    assert code == 0
    assert "每组 150" in out
    assert "正态近似" in out
    assert "双侧" in out


def test_zero_failure_mode() -> None:
    code, data = run_cli_json("samplesize", "--zero-failure", "--target", "0.98")
    assert code == 0
    assert data["mode"] == "zero_failure"
    assert data["n"] == 149
    assert data["confidence"] == 0.95
    assert "单侧" in data["basis"]
    code, out, _ = run_cli(
        "samplesize", "--zero-failure", "--target", "0.9", "--confidence", "0.95"
    )
    assert code == 0
    assert "29" in out


def test_design_effect_mode() -> None:
    code, data = run_cli_json(
        "samplesize", "--design-effect", "--cluster-size", "10", "--icc", "0.5", "--n", "300"
    )
    assert code == 0
    assert data["mode"] == "design_effect"
    assert abs(data["design_effect"] - 5.5) <= 0.0001
    assert abs(data["effective_n"] - 54.5) < 0.05
    code, data = run_cli_json(
        "samplesize", "--design-effect", "--cluster-size", "10", "--icc", "0.5"
    )
    assert code == 0
    assert data["n"] is None
    assert data["effective_n"] is None
    code, out, _ = run_cli(
        "samplesize", "--design-effect", "--cluster-size", "10", "--icc", "0.5", "--n", "300"
    )
    assert "5.5" in out
    assert "54.5" in out
    code, out, _ = run_cli("samplesize", "--design-effect", "--cluster-size", "4", "--icc", "0")
    assert code == 0
    assert "有效样本量" not in out.split("口径")[0]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--p1", "0.6"],
        ["--p1", "0.6", "--p2", "0.6"],
        ["--p1", "0.6", "--p2", "1.5"],
        ["--zero-failure"],
        ["--zero-failure", "--target", "1"],
        ["--zero-failure", "--design-effect", "--target", "0.9"],
        ["--zero-failure", "--target", "0.9", "--p1", "0.5"],
        ["--design-effect", "--cluster-size", "10"],
        ["--design-effect", "--cluster-size", "10", "--icc", "2"],
        ["--design-effect", "--cluster-size", "10", "--icc", "0.5", "--target", "0.9"],
        ["--p1", "0.6", "--p2", "0.7", "--icc", "0.5"],
        ["--p1", "abc", "--p2", "0.7"],
    ],
)
def test_input_errors_exit_2(argv: list[str]) -> None:
    code, _, _ = run_cli("samplesize", *argv)
    assert code == 2
