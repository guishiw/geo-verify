# 结果文件格式

本文约定各命令写入 `out/`、`recheck/` 的结果文件结构。`report` 只读取这些文件和项目文件，不重新计算；
第三方复算时可以逐个文件比对。数据契约（`spec/v1/`）之外的结果文件都以本文为准。

## 通用约定

- 每个文件是一个 JSON 对象，含 `schema_version`（`"1.0"`）、`kind`、`project_id`；项目为合成数据时带 `"synthetic": true`。
- 写出方式与 `jsonl.write_json` 一致：键排序、缩进 2、UTF-8、末尾换行。
- 不含生成时间、主机名、绝对路径：同一份输入在任何目录、任何时间计算，文件逐字节相同。
  需要时间戳的文件（证据清单、报告）只使用 `--now` 传入的时间。
- 比例一律用 0～1 的小数表示；无法计算时为 `null`，并在同级的 `null_reason` 或 `reason` 写明原因。
- `status` 用英文代码，另给中文 `status_label` 供展示。

## `out/metrics.json`（`kind: "metrics"`）

```text
project_id, target_entity_id, timezone
prompt_set            {version, content_hash}
parameters            {seed, iterations, confidence_level}
sufficiency_thresholds {min_windows, min_valid_observations}
observations          {total, in_selected_version, valid}
registered_metrics    预先登记的指标清单（展开后的 metric_ids、effective、note）
notices               [{code, severity: "info"|"warning"|"error", message, details?}]
exclusion_audit       见下
results               [metric_result 契约对象]
```

`notices[].code` 中会影响预检结论的有：

| code | 含义 |
|---|---|
| `preregistration_changed` | 预先登记内容在封存后被改动；全部结果降为事后指标 |
| `unregistered_exclusion_rules` | 使用了未预先登记的剔除规则 |
| `selective_exclusion` | 被剔除样本明显偏向不利样本 |
| `model_version_changed` | 同一平台同一通道在观测期内出现多个模型或产品版本，须重建基线 |
| `prompt_set_version_mismatch` | 存在不属于所选封存题库版本的观测（只计数，不参与计算） |

`exclusion_audit`：

```text
registered_rules      预先登记的剔除规则编号
rules                 [{rule_id, registered, listed_observations, excluded_observations}]
unregistered_rules    未登记的规则编号
excluded_observations 被审计事件剔除的观测数
thresholds            {alpha, min_excluded, min_difference}
comparisons           [{metric_id, direction, retained{numerator,denominator,value},
                        excluded{...}, value_if_included, p_value, selective, note}]
selective             是否判定为选择性剔除
```

## `out/crosscheck.json`（`kind: "crosscheck"`）

```text
prompt_set            {version, content_hash}
formula               "symmetric_relative_difference"
formula_text          "偏离度 = |A − B| / ((A + B) / 2)；A = B = 0 时为 0"
formula_note          说明此公式是本项目的定义（047 给了 30% 的阈值，未给公式）
threshold             0.3
preregistered         公式、阈值、指标、通道是否全部取自封存前登记内容（被命令行参数覆盖或封存后改口径则为 false）
registered            封存前登记的 crosscheck 配置
overridden            被命令行覆盖的字段名列表
metric_id, channel_a, channel_b, platform
parameters            {seed, iterations, confidence_level}
sufficiency_thresholds {min_windows, min_valid_observations}
status                "pass" | "fail" | "undetermined"
status_label          "通过" | "不通过" | "不可判定"
reason                整体结论的理由
notices               [{code, severity, message, details?}]
results               [{platform,                       null 表示全部平台合并
                        merged_platforms[],
                        a {channel, value, null_reason, numerator, denominator, queries,
                           windows, valid_observations, sufficient, sufficiency_reasons[],
                           interval, interval_note},
                        b {同上},
                        deviation,                      任一通道分母为 0 时为 null
                        difference,                     A − B（比例差，即百分点差 / 100）
                        interval {method, level, low, high} | null,
                        interval_note,
                        paired_queries,
                        status, status_label, reason}]
```

规则：任一通道样本不足时该行为 `undetermined`（即使偏离度为 0 也不得判通过）；偏离度不超过阈值为 `pass`，超过为 `fail`。
整体 `status`：合并行或任何一个样本充分的平台行为 `fail` 则为 `fail`；否则合并行不可判定则为 `undetermined`；否则 `pass`。
退出码：`pass` 0，`fail` 1，`undetermined` 3。

## `out/compare.json`（`kind: "compare"`，默认写入**当前**项目的 `out/`）

```text
baseline              {project_id, prompt_set{version, content_hash}, windows, valid_observations}
current               {同上}
metrics               参与比较的指标编号列表
platform              --platform 过滤值（或 null）
parameters            {seed, iterations, confidence_level}
sufficiency_thresholds {baseline{min_windows, min_valid_observations}, current{同上}}
method                差值、区间、结论、favorable 与范围对齐的口径说明
limitations           基线对比的已知限制列表
status                "ok" | "refused" | "insufficient"
status_label          "已比较" | "拒绝比较" | "样本不足"
reason                整体结论说明
refusal               null | {code: "prompt_set_mismatch" | "model_version_changed", message, details}
notices               [{code, severity, message, details?}]
results               [{metric_id, platform, higher_is_better,
                        baseline {value, null_reason, numerator, denominator, queries, windows,
                                  valid_observations, sufficient, sufficiency_reasons[]},
                        current {同上},
                        cells [{platform, channel}],
                        difference,                     同题配对比例差（当前 − 基线）的等权均值
                        baseline_paired_mean, current_paired_mean,
                        interval {method: "paired_cluster_bootstrap", level, low, high} | null,
                        paired_queries, baseline_only_queries, current_only_queries,
                        sufficient,
                        conclusion: "提升" | "下降" | "不能判定",
                        conclusion_code: "improved" | "declined" | "inconclusive",
                        favorable: true | false | null,
                        unmatched_cells [{platform, channel, side}],
                        preregistered, role, reason}]
```

规则：题库 `content_hash` 不同 → `refused`（禁止混算）；任一平台同通道的模型或产品版本集合在两期之间不同，或任一期内部出现多个版本 → `refused`（须重建基线）。
区间下界 > 0 为提升，上界 < 0 为下降，否则不能判定；样本不足时一律不能判定。
退出码：`ok` 0，`refused` 1，`insufficient` 3。`refused` 时仍写出文件，`results` 为空。

## `out/alerts.json`（`kind: "alerts"`）

```text
target_entity_id, prompt_set {version, content_hash}
mode                  "window" | "date"（按窗口或按项目时区的自然日比较）
baseline_window, current_window       按窗口比较时的窗口编号；按日比较时为 null
baseline_date, current_date           按日比较时的日期（YYYY-MM-DD）；按窗口比较时为 null
baseline, current     {window_id, date, valid_observations, first_observed_at, last_observed_at}；无法比较时为 null
available_windows     有有效观测的窗口，按时间排序
platform, platforms   --platform 过滤值（或 null）；两侧有数据的平台
thresholds            各规则使用的阈值；threshold_overrides [{key, value, source}]
status                "alert" | "clear" | "insufficient"
status_label          "有告警" | "无告警" | "无法比较"
reason, details       仅 insufficient：无法比较的原因与明细（如 available_windows）
alerts                [{alert_id, rule_id: "mention_drop" | "negative_spread" | "citation_failure"
                                  | "competitor_surge" | "platform_conflict",
                        severity: "warning" | "critical",
                        platform, entity_id, message, evidence{...}, observation_ids[]}]
rule_summary          [{rule_id, title, evaluated, triggered, skipped[]}]
citation_changes      {added [{domain, current}], removed [{domain, baseline}],
                       changed [{domain, baseline, current, delta}], baseline_citations,
                       current_citations, by_platform[], note}；insufficient 时为 null
leads                 [{alert_id, alert_rule_id, platform, text, domains[]}]   待核查的相关线索
notice
```

`citation_changes.note` 与 `leads[].text` 只能使用“相关”“待核查”一类措辞，不表达因果。
退出码：有告警 1，无告警 0，无法比较（窗口或日期不足）3。**无法比较时也写出本文件**（`status = "insufficient"`，
`alerts`、`leads`、`rule_summary` 为空，`baseline` / `current` / `citation_changes` 为 null），避免 `report` 读到上一次运行留下的旧结论；
`--json` 的标准输出仍是错误对象 `{"error": "insufficient_data", "message", "details"}`，`details.output` 为结果文件路径。
`report` 读到 `insufficient` 时把 `R-ALERTS` 记为“不可判定”（status `warning`，有保留）。

## `out/evidence-verify.json`（`kind: "evidence_verify"`）

```text
status                "pass" | "fail"
manifests             清单数量
latest_manifest       {sequence, manifest_hash} | null
checked_files         已校验的文件数
problems              [{file, kind, line, message}]      line 为 1 起的行号，不适用时为 null
```

`problems[].kind`：`no_manifest`、`manifest_invalid`、`manifest_modified`、`manifest_chain_broken`、
`file_missing`、`file_modified`、`file_added`、`line_modified`、`line_deleted`、`line_inserted`、`line_reordered`、
`append_only_violated`（相邻两份清单之间，只追加文件的历史行发生变化）、`audit_chain`、`sealed_prompt_set`。
退出码：`pass` 0，`fail` 1。

## `out/disputes.json`（`kind: "disputes"`）

```text
annotator_a, annotator_b
fields                [{field: "mentioned" | "recommended", compared, agreements, disagreements,
                        kappa, null_reason}]
items                 [{observation_id, entity_id, field, a, b, resolved}]
unresolved            未解决的争议项数量
```

`resolved`：该（观测，实体）在两条分歧标注之后又有一条带审计事件 `override` 的人工覆盖标注，则视为已裁决。
重新运行 `annotate kappa` 会刷新该文件。

## `recheck/plan.json`（`kind: "recheck_plan"`）

```text
prompt_set            {version, content_hash}
fraction, seed, preregistered
total_queries
query_ids             被抽中的题目编号（排序）
sampling              {method: "sha256_rank", rule}
counts, disputes, notices
items                 [{observation_id, query_id, entity_id, platform, source: "sample" | "dispute",
                        disputed, annotation_id}]
```

详细字段与 `recheck/plan.csv` 约定见 [recheck.md](recheck.md)。

## `recheck/result.json`（`kind: "recheck_result"`）

```text
plan_hash             所依据的 recheck/plan.json 的 SHA-256；没有计划文件时为 null
fields                比对的字段，如 ["mentioned", "recommended", "rank"]
planned, checked, agreed, disagreed, not_checked
pass_rate             agreed / checked；checked 为 0 时为 null
null_reason
by_source, input, errors, notices, not_checked_items, unplanned, unplanned_items
items                 [{observation_id, entity_id, source, annotation_id, agree,
                        original, rechecked, differences [{field, original, rechecked}], note}]
```

## `out/report.json`（`kind: "report"`）

```text
watermark             合成数据时为水印文案，否则为 null
generated_at          仅取自 --now；未传时为 null
notice                "本报告为工具预检结论，不构成独立第三方验证"
verdict               {code: "pass" | "conditional_pass" | "fail" | "insufficient_data",
                       label: "通过" | "有条件通过" | "不通过" | "数据不足",
                       summary, decided_by[], reservations[]}
rules                 [{rule_id, title, group, status: "pass" | "fail" | "warning" | "insufficient" | "not_run",
                        status_label, basis, details}]
project               项目、委托方、服务方、平台、周期与窗口、环境、封存题库摘要
preregistration       预先登记的指标、口径、阈值、交叉验证公式
metrics               每个披露指标：值、分子分母、区间、平台、题量、日期、次数、环境、统计方法、限制、是否事后指标
crosscheck, compare, alerts, evidence, exclusions, recheck, disputes
                      取自对应结果文件的摘要；文件缺失时为 {"status": "not_run"}
observation_index     {total, columns[], preview[]}（完整索引见 out/observations.csv）
limitations           [{code, text}]   限制条件
disclosure            利益关系披露（取自 project.json，含 operator_warning）
signatures            人工签字栏（留空待签）
inputs                [{path, sha256}]   报告读取的每个文件及其哈希
```

退出码：`通过`、`有条件通过` 0，`不通过` 1，`数据不足` 3。
