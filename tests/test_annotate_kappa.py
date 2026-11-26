"""`annotate kappa`：两名标注者的 Cohen's kappa、争议项与裁决。

kappa 的期望值全部手算（κ = (p_o − p_e) / (1 − p_e)），写在各测试的注释里。
2×2 表记法：a = 两人都判“是”，b = A 是 B 否，c = A 否 B 是，d = 两人都判“否”。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import run_cli, run_cli_json
from helpers_annotate import annotation_project
from helpers_collect import write_csv

from geo_verify.annotate import run_rules
from geo_verify.annotate.kappa import cohen_kappa
from geo_verify.jsonl import read_json

T1 = "2026-10-09T01:00:00Z"
T2 = "2026-10-09T02:00:00Z"
T3 = "2026-10-09T03:00:00Z"
T4 = "2026-10-09T04:00:00Z"
FIELDS = [
    "observation_id",
    "entity_id",
    "mentioned",
    "recommended",
    "negated",
    "rankable",
    "rank",
    "sentiment",
]
ENTITY = "brand-lanxu"


def table(a: int, b: int, c: int, d: int) -> list[tuple[Any, Any]]:
    return [(True, True)] * a + [(True, False)] * b + [(False, True)] * c + [(False, False)] * d


def test_kappa_hand_example_one() -> None:
    # a/b/c/d = 20/5/10/15，n = 50
    # p_o = (20 + 15) / 50 = 0.7
    # A 判是 25/50 = 0.5，B 判是 30/50 = 0.6
    # p_e = 0.5 × 0.6 + 0.5 × 0.4 = 0.5
    # κ = (0.7 − 0.5) / (1 − 0.5) = 0.4
    result = cohen_kappa(table(20, 5, 10, 15))
    assert (result.compared, result.agreements, result.disagreements) == (50, 35, 15)
    assert result.kappa == pytest.approx(0.4, abs=1e-6)
    assert result.null_reason is None


def test_kappa_hand_example_two() -> None:
    # a/b/c/d = 45/15/25/15，n = 100
    # p_o = (45 + 15) / 100 = 0.6
    # A 判是 60/100 = 0.6，B 判是 70/100 = 0.7
    # p_e = 0.6 × 0.7 + 0.4 × 0.3 = 0.42 + 0.12 = 0.54
    # κ = (0.6 − 0.54) / (1 − 0.54) = 0.06 / 0.46 = 3/23 ≈ 0.130435
    result = cohen_kappa(table(45, 15, 25, 15))
    assert (result.compared, result.agreements, result.disagreements) == (100, 60, 40)
    assert result.kappa == pytest.approx(3 / 23, abs=1e-6)


def test_kappa_negative_agreement() -> None:
    # a/b/c/d = 1/4/4/1：p_o = 0.2；两人判是都是 0.5，p_e = 0.5；κ = (0.2 − 0.5) / 0.5 = −0.6
    assert cohen_kappa(table(1, 4, 4, 1)).kappa == pytest.approx(-0.6, abs=1e-6)


def test_kappa_perfect_agreement() -> None:
    # a/b/c/d = 10/0/0/10：p_o = 1，p_e = 0.5，κ = 1
    result = cohen_kappa(table(10, 0, 0, 10))
    assert result.kappa == pytest.approx(1.0)
    assert result.disagreements == 0


def test_kappa_undefined_when_both_use_one_category() -> None:
    # 两人全部判“是”：p_e = 1 × 1 = 1，分母为 0，κ 无定义
    result = cohen_kappa(table(5, 0, 0, 0))
    assert result.kappa is None
    assert result.null_reason is not None
    assert "期望一致率为 1" in result.null_reason
    assert (result.compared, result.agreements) == (5, 5)


def test_kappa_when_only_one_annotator_is_constant() -> None:
    # A 全判是，B 两是两否：p_o = 0.5；p_e = 1 × 0.5 + 0 × 0.5 = 0.5；κ = 0
    assert cohen_kappa(table(2, 2, 0, 0)).kappa == pytest.approx(0.0, abs=1e-9)


def test_kappa_with_a_null_category() -> None:
    # recommended 可以为 null（无法判断），按三个类别计算：
    # (是,是) (否,否) (null,否) (是,null)：p_o = 2/4 = 0.5
    # A：是 2、否 1、null 1 → 0.5 / 0.25 / 0.25；B：是 1、否 2、null 1 → 0.25 / 0.5 / 0.25
    # p_e = 0.5×0.25 + 0.25×0.5 + 0.25×0.25 = 0.3125
    # κ = (0.5 − 0.3125) / (1 − 0.3125) = 0.1875 / 0.6875 = 3/11 ≈ 0.272727
    pairs = [(True, True), (False, False), (None, False), (True, None)]
    assert cohen_kappa(pairs).kappa == pytest.approx(3 / 11, abs=1e-6)


def test_kappa_without_items() -> None:
    result = cohen_kappa([])
    assert (result.compared, result.kappa) == (0, None)
    assert result.null_reason is not None


# ------------------------------------------------------------------ 端到端

# 10 条观测上两人对 brand-lanxu 的 recommended：
# obs1-4 (是,是)  obs5 (是,否)  obs6-7 (否,是)  obs8-10 (否,否)  →  a/b/c/d = 4/1/2/3
# p_o = 7/10 = 0.7；A 判是 5/10 = 0.5，B 判是 6/10 = 0.6；p_e = 0.3 + 0.2 = 0.5；κ = 0.4
A_RECOMMENDED = [True, True, True, True, True, False, False, False, False, False]
B_RECOMMENDED = [True, True, True, True, False, True, True, False, False, False]


def labels(values: list[bool], **overrides: bool) -> list[dict[str, str]]:
    rows = []
    for number, value in enumerate(values, start=1):
        observation = f"obs-{number:06d}"
        value = overrides.get(observation, value)
        rows.append(
            {
                "observation_id": observation,
                "entity_id": ENTITY,
                "mentioned": "true",
                "recommended": "true" if value else "false",
                "negated": "false",
                "rankable": "false",
                "rank": "",
                "sentiment": "",
            }
        )
    return rows


def import_labels(root: Path, annotator: str, rows: list[dict[str, str]], now: str) -> None:
    source = write_csv(root / f"{annotator}-{now[11:13]}.csv", FIELDS, rows)
    code, data = run_cli_json(
        "annotate",
        "import",
        str(root),
        "--input",
        str(source),
        "--annotator",
        annotator,
        "--now",
        now,
    )
    assert code == 0, data


@pytest.fixture
def root(tmp_path: Path) -> Path:
    annotation_project(tmp_path, ["澜序口碑不错。"] * 10)
    import_labels(tmp_path, "annotator-a", labels(A_RECOMMENDED), T1)
    import_labels(tmp_path, "annotator-b", labels(B_RECOMMENDED), T2)
    return tmp_path


def kappa(root: Path, *extra: str) -> tuple[int, Any]:
    return run_cli_json(
        "annotate", "kappa", str(root), "--a", "annotator-a", "--b", "annotator-b", *extra
    )


def adjudicate(root: Path, observation: str, value: str, now: str = T3) -> None:
    code, data = run_cli_json(
        "annotate",
        "override",
        str(root),
        "--observation",
        observation,
        "--entity",
        ENTITY,
        "--set",
        f"recommended={value}",
        "--annotator",
        "adjudicator",
        "--reason",
        "争议项裁决",
        "--now",
        now,
    )
    assert code == 0, data


def field(data: dict[str, Any], name: str) -> dict[str, Any]:
    (entry,) = [item for item in data["fields"] if item["field"] == name]
    return entry


def test_disagreement_gives_kappa_and_disputes(root: Path) -> None:
    code, data = kappa(root)
    assert code == 1  # 有未解决的争议项，进入复核
    recommended = field(data, "recommended")
    assert (recommended["compared"], recommended["agreements"], recommended["disagreements"]) == (
        10,
        7,
        3,
    )
    assert recommended["kappa"] == pytest.approx(0.4, abs=1e-6)
    assert recommended["null_reason"] is None
    mentioned = field(data, "mentioned")
    assert (mentioned["compared"], mentioned["agreements"], mentioned["disagreements"]) == (
        10,
        10,
        0,
    )
    assert mentioned["kappa"] is None
    assert "期望一致率为 1" in mentioned["null_reason"]
    assert data["items"] == [
        {
            "observation_id": "obs-000005",
            "entity_id": ENTITY,
            "field": "recommended",
            "a": True,
            "b": False,
            "resolved": False,
        },
        {
            "observation_id": "obs-000006",
            "entity_id": ENTITY,
            "field": "recommended",
            "a": False,
            "b": True,
            "resolved": False,
        },
        {
            "observation_id": "obs-000007",
            "entity_id": ENTITY,
            "field": "recommended",
            "a": False,
            "b": True,
            "resolved": False,
        },
    ]
    assert data["unresolved"] == 3
    written = read_json(root / "out" / "disputes.json")
    assert written["schema_version"] == "1.0"
    assert written["kind"] == "disputes"
    assert written["project_id"] == "auto-demo"
    assert written["synthetic"] is True
    assert (written["annotator_a"], written["annotator_b"]) == ("annotator-a", "annotator-b")
    assert written["items"] == data["items"]
    assert written["fields"] == data["fields"]
    assert written["unresolved"] == 3


def test_adjudication_resolves_disputes(root: Path) -> None:
    adjudicate(root, "obs-000005", "false")
    code, data = kappa(root)
    assert code == 1
    assert [item["resolved"] for item in data["items"]] == [True, False, False]
    assert data["unresolved"] == 2
    # 裁决者是第三个人，两名标注者各自最新的标注没变，kappa 不变
    assert field(data, "recommended")["kappa"] == pytest.approx(0.4, abs=1e-6)

    adjudicate(root, "obs-000006", "true")
    adjudicate(root, "obs-000007", "false")
    code, data = kappa(root)
    assert code == 0
    assert data["unresolved"] == 0
    assert all(item["resolved"] for item in data["items"])
    assert read_json(root / "out" / "disputes.json")["unresolved"] == 0


def test_override_before_a_new_disagreement_does_not_resolve_it(root: Path) -> None:
    adjudicate(root, "obs-000006", "true", now=T3)
    _, data = kappa(root)
    assert next(item for item in data["items"] if item["observation_id"] == "obs-000006")[
        "resolved"
    ]
    # B 在裁决之后又重新标注了 obs6（推荐仍为“是”，与 A 分歧）：这条分歧在裁决之后产生，未解决
    relabel = {**labels(B_RECOMMENDED)[5], "sentiment": "positive"}
    import_labels(root, "annotator-b", [relabel], T4)
    _, data = kappa(root)
    item = next(entry for entry in data["items"] if entry["observation_id"] == "obs-000006")
    assert item["resolved"] is False


def test_latest_label_of_each_annotator_is_used(root: Path) -> None:
    # A 改判 obs5 为“否”：a/b/c/d = 4/0/2/4
    # p_o = 8/10 = 0.8；A 判是 4/10 = 0.4，B 判是 6/10 = 0.6；p_e = 0.24 + 0.24 = 0.48
    # κ = (0.8 − 0.48) / (1 − 0.48) = 0.32 / 0.52 = 8/13 ≈ 0.615385
    import_labels(root, "annotator-a", labels(A_RECOMMENDED, **{"obs-000005": False}), T4)
    _, data = kappa(root, "--field", "recommended")
    assert field(data, "recommended")["kappa"] == pytest.approx(8 / 13, abs=1e-6)
    assert field(data, "recommended")["agreements"] == 8
    assert [item["observation_id"] for item in data["items"]] == ["obs-000006", "obs-000007"]


def test_single_field(root: Path) -> None:
    code, data = kappa(root, "--field", "mentioned")
    assert code == 0
    assert [item["field"] for item in data["fields"]] == ["mentioned"]
    assert data["items"] == []
    assert data["unresolved"] == 0


def test_disputes_file_is_reproducible(root: Path) -> None:
    kappa(root)
    first = (root / "out" / "disputes.json").read_bytes()
    kappa(root)
    assert (root / "out" / "disputes.json").read_bytes() == first


def test_rule_annotator_can_be_compared_with_a_human(tmp_path: Path) -> None:
    annotation_project(tmp_path, ["澜序口碑不错。"] * 10)
    run_rules.annotate_project(tmp_path, now="2026-10-08T03:00:00Z")
    import_labels(tmp_path, "annotator-a", labels(A_RECOMMENDED), T1)
    code, data = run_cli_json(
        "annotate",
        "kappa",
        str(tmp_path),
        "--a",
        "geo-verify-rules",
        "--b",
        "annotator-a",
        "--field",
        "recommended",
    )
    # 规则：“澜序口碑不错。”只是提及、未推荐；A 判了 5 条推荐 → 5 条分歧
    assert code == 1
    assert field(data, "recommended")["disagreements"] == 5
    assert field(data, "recommended")["compared"] == 10


def test_no_common_items_is_insufficient_data(root: Path) -> None:
    code, data = run_cli_json("annotate", "kappa", str(root), "--a", "annotator-a", "--b", "nobody")
    assert code == 3
    assert data["error"] == "insufficient_data"


def test_same_annotator_twice_is_an_input_error(root: Path) -> None:
    code, data = run_cli_json(
        "annotate", "kappa", str(root), "--a", "annotator-a", "--b", "annotator-a"
    )
    assert code == 2
    assert data["error"] == "input_error"


def test_text_output(root: Path) -> None:
    code, out, _ = run_cli(
        "annotate", "kappa", str(root), "--a", "annotator-a", "--b", "annotator-b"
    )
    assert code == 1
    assert "recommended：比较 10 项，一致 7，分歧 3，kappa = 0.4" in out
    assert "mentioned：比较 10 项，一致 10，分歧 0，kappa 无定义" in out
    assert "3 项争议未解决，进入复核" in out
