"""可打印的 HTML 报告：自包含（内联样式、无脚本、无外链资源），全部内容转义。

内容与 report.json 一致；回答原文、题目、实体名等来自外部的文本一律经 html.escape 转义后输出。
合成数据的报告带固定定位的水印（打印时每页可见），并在页首、页尾重复水印文字。
"""

from __future__ import annotations

from html import escape
from typing import Any

from geo_verify.report.labels import STATUS_LABELS

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1100px; padding: 24px 16px 48px; background: #ffffff; color: #1f2328;
  font: 14px/1.6 "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; }
h1 { font-size: 24px; margin: 0 0 8px; }
h2 { font-size: 18px; margin: 28px 0 8px; padding-bottom: 4px; border-bottom: 2px solid #d0d7de; }
h3 { font-size: 15px; margin: 16px 0 6px; }
table { width: 100%; border-collapse: collapse; margin: 6px 0 12px; font-size: 12px; }
th, td { border: 1px solid #d0d7de; padding: 4px 6px; text-align: left; vertical-align: top;
  overflow-wrap: anywhere; }
th { background: #f6f8fa; font-weight: 600; }
dl.kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 4px 0; }
dl.kv dt { color: #57606a; }
dl.kv dd { margin: 0; overflow-wrap: anywhere; }
.notice { border: 2px solid #9a6700; background: #fff8c5; padding: 8px 12px; font-weight: 600; }
.banner { border: 2px dashed #cf222e; color: #cf222e; padding: 6px 12px; text-align: center; font-weight: 700; }
.verdict { font-size: 20px; font-weight: 700; padding: 10px 12px; border-left: 6px solid #57606a;
  background: #f6f8fa; }
.verdict.pass { border-color: #1a7f37; }
.verdict.conditional_pass { border-color: #9a6700; }
.verdict.fail { border-color: #cf222e; }
.verdict.insufficient_data { border-color: #8250df; }
.status { display: inline-block; padding: 0 6px; border-radius: 4px; font-weight: 600; white-space: nowrap; }
.status.pass { background: #dafbe1; color: #116329; }
.status.fail { background: #ffebe9; color: #a40e26; }
.status.warning { background: #fff8c5; color: #7d4e00; }
.status.insufficient { background: #fbefff; color: #6e40c9; }
.status.not_run { background: #eaeef2; color: #424a53; }
.warning-box { border: 2px solid #cf222e; background: #ffebe9; padding: 8px 12px; font-weight: 600; }
.muted { color: #57606a; }
.sign td { height: 56px; }
.watermark { position: fixed; top: 42%; left: 0; right: 0; text-align: center; transform: rotate(-24deg);
  font-size: 34px; font-weight: 700; color: #cf222e; opacity: 0.13; pointer-events: none; z-index: 10; }
footer { margin-top: 32px; font-size: 12px; color: #57606a; }
@media print {
  body { max-width: none; padding: 0; font-size: 11px; }
  h2 { break-after: avoid; }
  table, dl.kv { break-inside: auto; }
  tr { break-inside: avoid; }
  .watermark { position: fixed; }
}
"""


def esc(value: Any) -> str:
    return escape(_plain(value), quote=True)


def _plain(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _value(value: Any) -> str:
    """把任意 JSON 值渲染为转义后的 HTML。"""
    if isinstance(value, dict):
        if not value:
            return '<span class="muted">—</span>'
        rows = "".join(f"<dt>{esc(key)}</dt><dd>{_value(item)}</dd>" for key, item in value.items())
        return f'<dl class="kv">{rows}</dl>'
    if isinstance(value, list):
        if not value:
            return '<span class="muted">（无）</span>'
        if all(isinstance(item, dict) for item in value):
            columns = list(dict.fromkeys(key for item in value for key in item))
            return _table(columns, [[item.get(key) for key in columns] for item in value], raw=True)
        return esc("、".join(_plain(item) for item in value))
    return esc(value)


def _table(header: list[str], rows: list[list[Any]], *, raw: bool = False) -> str:
    head = "".join(f"<th>{esc(name)}</th>" for name in header)
    body = "".join(
        "<tr>" + "".join(f"<td>{_value(cell) if raw else cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _kv(pairs: list[tuple[str, Any]]) -> str:
    rows = "".join(f"<dt>{esc(key)}</dt><dd>{_value(value)}</dd>" for key, value in pairs)
    return f'<dl class="kv">{rows}</dl>'


def _status(status: str) -> str:
    return f'<span class="status {esc(status)}">{esc(STATUS_LABELS.get(status, status))}</span>'


def _section(title: str, body: str) -> str:
    return f"<section><h2>{esc(title)}</h2>{body}</section>"


def _not_run(section: dict[str, Any], what: str) -> str | None:
    if section.get("status") == "not_run":
        return f'<p class="muted">{esc(what)}未执行（缺少对应的结果文件）。</p>'
    return None


# ------------------------------------------------------------------ 各节


def _verdict(report: dict[str, Any]) -> str:
    verdict = report["verdict"]
    parts = [
        f'<p class="verdict {esc(verdict["code"])}">预检结论：{esc(verdict["label"])}</p>',
        f"<p>{esc(verdict['summary'])}</p>",
    ]
    if verdict["reservations"]:
        items = "".join(
            f"<li><strong>{esc(item['rule_id'])}</strong>：{esc(item['text'])}</li>"
            for item in verdict["reservations"]
        )
        parts.append(f"<h3>保留意见</h3><ul>{items}</ul>")
    parts.append(f'<p class="notice">{esc(report["notice"])}</p>')
    return _section("预检结论", "".join(parts))


def _rules(report: dict[str, Any]) -> str:
    rows = [
        [
            esc(item["rule_id"]),
            esc(item["title"]),
            _status(item["status"]),
            esc(item["basis"]),
            _value(item["details"]),
        ]
        for item in report["rules"]
    ]
    return _section("判定规则", _table(["编号", "规则", "状态", "依据", "明细"], rows))


def _project(report: dict[str, Any]) -> str:
    project = report["project"]
    period = project["period"]
    platforms = [
        [
            esc(item["platform_id"]),
            esc(item["display_name"]),
            esc("、".join(f"{entry['channel']}（{entry['label']}）" for entry in item["channels"])),
        ]
        for item in project["platforms"]
    ]
    competitors = "、".join(
        f"{item['name'] or '—'}（{item['entity_id']}）" for item in project["competitors"]
    )
    target = project["target"]
    body = _kv(
        [
            ("项目", f"{project['name']}（{project['project_id']}）"),
            ("委托方", project["client"]["name"]),
            ("服务方", project["provider"]["name"]),
            ("目标主体", f"{target['name'] or '—'}（{target['entity_id']}）"),
            ("竞品", competitors or None),
            ("时区", project["timezone"]),
            ("周期", f"{period['date_from'] or '—'} 至 {period['date_to'] or '—'}"),
            ("时间窗口", period["windows"]),
            ("观测", period["observations"]),
            ("各通道观测数", period["channels"]),
            ("采集环境（登记）", project["environment"]),
            ("封存题库", project["prompt_set"]),
        ]
    )
    body += "<h3>平台范围</h3>" + _table(["平台", "名称", "通道"], platforms)
    return _section("项目与采集", body)


def _preregistration(report: dict[str, Any]) -> str:
    prereg = report["preregistration"]
    rows = [
        [
            esc(item["metric_id"]),
            esc(item["title"]),
            esc(item["role_label"]),
            esc(_threshold(item["threshold"])),
            esc(item["definition_version"]),
        ]
        for item in prereg["metrics"]
    ]
    crosscheck = prereg["crosscheck"]
    body = _kv(
        [
            ("登记时间", prereg["registered_at"]),
            ("登记人", prereg["registered_by"]),
            ("封存后是否改动", prereg["changed_after_seal"]),
            ("准确率口径", prereg["accuracy_basis"]),
            ("推荐成功的定义", prereg["recommendation_success"]),
            ("样本充分性", prereg["sufficiency"]),
            ("统计参数", prereg["statistics"]),
            ("抽样复核", prereg["recheck"]),
            ("目标信源", prereg["target_sources"]),
            ("剔除规则", prereg["exclusion_rules"]),
        ]
    )
    body += "<h3>预先登记的指标与阈值</h3>" + _table(
        ["编号", "指标", "用途", "阈值", "口径版本"], rows
    )
    body += "<h3>交叉验证口径</h3>" + _kv(
        [
            ("公式", crosscheck["formula_text"]),
            ("说明", crosscheck["formula_note"]),
            ("声明", crosscheck["formula_declaration"]),
            ("阈值", crosscheck["threshold"]),
            ("指标", crosscheck["metric_id"]),
            ("通道", f"{crosscheck['channel_a']} / {crosscheck['channel_b']}"),
        ]
    )
    return _section("预先登记的口径", body)


def _threshold(threshold: dict[str, Any] | None) -> str:
    if threshold is None:
        return "—"
    return f"{threshold['op']} {threshold['value']}"


def _interval(item: dict[str, Any]) -> str:
    interval = item["interval"]
    if interval is None:
        return str(item["method_label"])
    return f"[{interval['low']:.4f}, {interval['high']:.4f}]（{item['method_label']}，{interval['level']:g}）"


def _coverage(coverage: dict[str, Any] | None) -> str:
    if coverage is None:
        return "—"
    channels = coverage.get("channels") or {}
    return (
        f"平台 {'、'.join(coverage.get('platforms') or []) or '—'}；题量 {coverage.get('queries')}；"
        f"日期 {coverage.get('date_from') or '—'} 至 {coverage.get('date_to') or '—'}；"
        f"窗口 {'、'.join(coverage.get('windows') or []) or '—'}；"
        f"通道 {'、'.join(f'{key} {value}' for key, value in channels.items()) or '—'}"
    )


def _metrics(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    missing = _not_run(metrics, "指标计算")
    if missing:
        return _section("指标披露", missing)
    rows = []
    for item in metrics["items"]:
        value = (
            esc(item["value"])
            if item["value"] is not None
            else f'— <span class="muted">{esc(item["null_reason"])}</span>'
        )
        sufficiency = item["sufficiency"]
        rows.append(
            [
                esc(f"{item['title']}（{item['metric_id']}）"),
                esc(item["scope_label"]),
                value,
                esc(f"{item['numerator']} / {item['denominator']}"),
                esc(_interval(item)),
                esc(
                    f"{'充分' if sufficiency['sufficient'] else '不充分'}"
                    f"（窗口 {sufficiency['windows']}，有效观测 {sufficiency['valid_observations']}）"
                ),
                esc(_coverage(item["coverage"])),
                esc(f"{item['use_label']}；阈值 {_threshold(item['threshold'])}"),
                esc("；".join(item["limitations"]) or "—"),
            ]
        )
    header = [
        "指标",
        "范围",
        "值",
        "次数（分子 / 分母）",
        "区间与方法",
        "样本充分性",
        "平台、题量、日期、通道",
        "用途",
        "限制",
    ]
    body = _kv(
        [
            ("题库", metrics["prompt_set"]),
            ("统计参数", metrics["parameters"]),
            ("样本充分性门槛", metrics["sufficiency_thresholds"]),
            ("观测", metrics["observations"]),
        ]
    )
    environment = report["project"]["environment"]
    body += f'<p class="muted">各指标的采集环境均为项目登记的环境：{esc(_plain_env(environment))}。剔除数量与规则见各指标的明细与“剔除”一节。</p>'
    body += _table(header, rows)
    if metrics["notices"]:
        body += "<h3>指标计算提示</h3>" + _value(metrics["notices"])
    return _section("指标披露", body)


def _plain_env(environment: dict[str, Any]) -> str:
    return "，".join(f"{key} = {_plain(value)}" for key, value in environment.items())


def _crosscheck(report: dict[str, Any]) -> str:
    section = report["crosscheck"]
    missing = _not_run(section, "双通道交叉验证")
    if missing:
        return _section("双通道交叉验证", missing)
    rows = []
    for row in section["results"]:
        interval = row.get("interval_pp")
        rows.append(
            [
                esc(row.get("platform") or "全部平台合并"),
                esc(
                    f"{row['a'].get('label')}：{_plain(row['a'].get('value'))}（{row['a'].get('numerator')}/{row['a'].get('denominator')}）"
                ),
                esc(
                    f"{row['b'].get('label')}：{_plain(row['b'].get('value'))}（{row['b'].get('numerator')}/{row['b'].get('denominator')}）"
                ),
                esc(row.get("deviation")),
                esc(row.get("difference_pp")),
                esc(
                    f"[{_plain(interval['low'])}, {_plain(interval['high'])}]" if interval else None
                ),
                esc(row.get("status_label")),
                esc(row.get("reason")),
            ]
        )
    body = _kv(
        [
            ("结论", section["status_label"]),
            ("理由", section["reason"]),
            ("公式", section["formula_text"]),
            ("说明", section["formula_note"]),
            ("声明", section["formula_declaration"]),
            ("阈值", section["threshold"]),
            ("参数取自预先登记", section["preregistered"]),
            ("指标", f"{section['metric_title']}（{section['metric_id']}）"),
            ("通道 A", section["channel_a"]),
            ("通道 B", section["channel_b"]),
        ]
    )
    body += _table(
        ["平台", "通道 A", "通道 B", "偏离度", "百分点差", "百分点差区间", "结论", "理由"], rows
    )
    return _section("双通道交叉验证", body)


def _generic(
    report: dict[str, Any], key: str, title: str, what: str, fields: list[tuple[str, str]]
) -> str:
    section = report[key]
    missing = _not_run(section, what)
    if missing:
        return _section(title, missing)
    return _section(title, _kv([(label, section.get(name)) for label, name in fields]))


def _alerts(report: dict[str, Any]) -> str:
    section = report["alerts"]
    missing = _not_run(section, "异常识别")
    if missing:
        return _section("告警与待核查的相关线索", missing)
    pairs: list[tuple[str, Any]] = [("状态", section["status_label"])]
    if section.get("reason"):
        pairs.append(("理由", section["reason"]))
    if section.get("mode") == "date":
        pairs += [("基线日", section["baseline_date"]), ("当前日", section["current_date"])]
    else:
        pairs += [("基线窗口", section["baseline_window"]), ("当前窗口", section["current_window"])]
    body = _kv(pairs)
    body += "<h3>告警</h3>" + _value(section["alerts"])
    body += "<h3>待核查的相关线索</h3>"
    if section["leads"]:
        body += (
            "<ul>" + "".join(f"<li>{esc(item['text'])}</li>" for item in section["leads"]) + "</ul>"
        )
    else:
        body += '<p class="muted">（无）</p>'
    body += "<h3>引用域名变化</h3>" + _value(section["citation_changes"])
    return _section("告警与待核查的相关线索", body)


def _recheck(report: dict[str, Any]) -> str:
    section = report["recheck"]
    missing = _not_run(section, "抽样复核")
    if missing:
        return _section("抽样复核", missing)
    body = ""
    plan = section["plan"]
    if plan is None:
        body += '<p class="muted">没有复核计划文件。</p>'
    else:
        body += _kv(
            [
                ("抽取比例", plan["fraction"]),
                ("seed", plan["seed"]),
                ("抽样参数取自预先登记", plan["preregistered"]),
                ("题库题数", plan["total_queries"]),
                ("抽中题数", len(plan["query_ids"])),
                ("复核项", plan["item_count"]),
            ]
        )
        rows = [[esc(item["query_id"]), esc(item["text"])] for item in plan["queries"]]
        body += "<h3>抽中的题目</h3>" + _table(["编号", "题目"], rows)
    result = section["result"]
    if result is None:
        body += '<p class="muted">尚未比对复核结果。</p>'
    else:
        body += "<h3>复核结果</h3>" + _kv(
            [
                ("比对字段", result["fields"]),
                (
                    "计划 / 已复核 / 未复核",
                    f"{result['planned']} / {result['checked']} / {result['not_checked']}",
                ),
                ("一致 / 不一致", f"{result['agreed']} / {result['disagreed']}"),
                (
                    "一致率",
                    result["pass_rate"]
                    if result["pass_rate"] is not None
                    else result["null_reason"],
                ),
                ("与当前计划一致", result["plan_matches"]),
                ("复核文件中的错误", result["errors"]),
            ]
        )
    return _section("抽样复核", body)


def _exclusions(report: dict[str, Any]) -> str:
    section = report["exclusions"]
    missing = _not_run(section, "剔除审计")
    if missing:
        return _section("剔除", missing)
    body = _kv(
        [
            ("被剔除的观测", section["excluded_observations"]),
            ("未登记的规则", section["unregistered_rules"]),
            ("判定为选择性剔除", section["selective"]),
            ("判定门槛", section["thresholds"]),
        ]
    )
    body += "<h3>剔除规则与数量</h3>" + _value(section["rules"])
    body += "<h3>保留与被剔除样本的对比</h3>" + _value(section["comparisons"])
    return _section("剔除", body)


def _observation_index(report: dict[str, Any]) -> str:
    index = report["observation_index"]
    shown = [
        "line",
        "observation_id",
        "query_id",
        "platform",
        "channel_label",
        "window_id",
        "local_date",
        "status",
        "excluded",
        "answer_excerpt",
    ]
    rows = [[esc(row[name]) for name in shown] for row in index["preview"]]
    body = f'<p class="muted">共 {esc(index["total"])} 行原始记录，以下为前 {esc(len(index["preview"]))} 行；完整索引见 observations.csv。</p>'
    body += _table(shown, rows)
    return _section("原始记录索引（摘录）", body)


def _limitations(report: dict[str, Any]) -> str:
    items = "".join(f"<li>{esc(item['text'])}</li>" for item in report["limitations"])
    return _section("限制条件", f"<ul>{items}</ul>")


def _disclosure(report: dict[str, Any]) -> str:
    disclosure = report["disclosure"]
    body = ""
    if disclosure["operator_warning"]:
        body += f'<p class="warning-box">{esc(disclosure["operator_warning"])}</p>'
    body += _kv(
        [
            ("声明", disclosure["statement"]),
            ("工具操作方", disclosure["tool_operator"]),
            ("工具操作方即服务方", disclosure["operator_is_provider"]),
        ]
    )
    body += "<h3>利益关系</h3>" + _value(disclosure["relationships"])
    return _section("利益关系披露", body)


def _signatures(report: dict[str, Any]) -> str:
    rows = [[esc(item["role"]), esc(item["party"]), "", "", ""] for item in report["signatures"]]
    table = _table(["角色", "单位", "姓名", "签字", "日期"], rows).replace(
        "<table>", '<table class="sign">', 1
    )
    note = '<p class="muted">以下由人工填写；未签字的报告只是工具预检结论。</p>'
    return _section("人工签字", note + table)


def _inputs(report: dict[str, Any]) -> str:
    rows = [[esc(item["path"]), esc(item["sha256"])] for item in report["inputs"]]
    return _section("输入文件与哈希", _table(["文件", "SHA-256"], rows))


def render_html(report: dict[str, Any]) -> str:
    watermark = report.get("watermark")
    title = f"预检报告：{report['project']['name']}"
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>",
    ]
    if watermark:
        parts.append(f'<div class="watermark" aria-hidden="true">{esc(watermark)}</div>')
        parts.append(f'<p class="banner">{esc(watermark)}</p>')
    parts.append(f'<header><h1>{esc(title)}</h1><p class="notice">{esc(report["notice"])}</p>')
    if report["generated_at"]:
        parts.append(f'<p class="muted">生成时间：{esc(report["generated_at"])}</p>')
    parts.append("</header>")
    parts.extend(
        [
            _verdict(report),
            _rules(report),
            _project(report),
            _preregistration(report),
            _metrics(report),
            _crosscheck(report),
            _generic(
                report,
                "compare",
                "基线与对比",
                "基线对比",
                [
                    ("状态", "status_label"),
                    ("拒绝原因", "refusal"),
                    ("基线", "baseline"),
                    ("当前", "current"),
                    ("结果", "results"),
                ],
            ),
            _alerts(report),
            _generic(
                report,
                "evidence",
                "证据校验",
                "证据校验",
                [
                    ("状态", "status_label"),
                    ("清单数", "manifests"),
                    ("最新清单", "latest_manifest"),
                    ("已校验文件", "checked_files"),
                    ("问题", "problems"),
                ],
            ),
            _exclusions(report),
            _recheck(report),
            _generic(
                report,
                "disputes",
                "双人标注争议",
                "双人标注一致性检查",
                [
                    ("标注者甲", "annotator_a"),
                    ("标注者乙", "annotator_b"),
                    ("一致性", "fields"),
                    ("未解决", "unresolved"),
                    ("未解决的争议项", "unresolved_items"),
                ],
            ),
            _observation_index(report),
            _limitations(report),
            _disclosure(report),
            _signatures(report),
            _inputs(report),
        ]
    )
    footer = esc(report["notice"])
    if watermark:
        footer += "｜" + esc(watermark)
    parts.append(f"<footer>{footer}</footer></body></html>")
    return "\n".join(parts) + "\n"
