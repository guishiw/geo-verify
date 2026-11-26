# 抽样复核（recheck-plan / recheck-compare）

抽样复核用于核对标注质量：按固定 seed 抽取一部分题目，由人工复核这些题目下的标注，再与原标注比对得到一致率（指标 `recheck_pass_rate`）。
一致率只作为过程质量指标披露，命令本身不判定是否达标——门槛属于项目预先登记的口径。

## 抽样规则

`recheck-plan PROJECT [--fraction F] [--seed N] [--prompt-set-version V] [--now T]`

1. 取所选封存题库（默认最新版本）的 `query_id`，去重后排序，题数记为 N。
2. 抽取数量 k = ⌈F × N⌉。F 按其十进制写法精确计算：0.1 × 30 = 3，不会因浮点误差多抽一题。F ∈ (0, 1]。
3. 对每道题计算 `sha256("<seed>|<query_id>")`（UTF-8 编码），按十六进制摘要升序取前 k 道，结果按 `query_id` 排序。

结果只取决于题库和 seed，与观测文件、标注文件的行序无关；同输入同 seed 时 `recheck/plan.json` 与 `recheck/plan.csv` 逐字节相同。
F 与 seed 默认取 `project.json` 的 `preregistration.recheck`；命令行给出不同的值，或预先登记内容在封存后被改动时，`preregistered = false` 并给出提示。
计划文件不含时间戳，`--now` 只做格式校验。

## 复核项

- 复核项 =（观测，实体）。被抽中题目下的每条**有效观测**（`status = valid`、属于所选题库版本、题目在题库中、未被审计事件剔除），对其每个已有生效标注的实体列一项，`source = sample`。没有任何生效标注的有效观测计入 `counts.unannotated_observations` 并提示。
- `out/disputes.json` 中未解决（`resolved` 不为 true）的争议项按（观测，实体）去重后并入，`source = dispute`，排在抽样项之后；已在抽样项里的只标 `disputed = true`；无法并入的（观测不存在、不是有效观测、没有生效标注）逐条列在 `disputes.skipped`。
- 每项记录 `annotation_id`：生成计划时生效的那条标注。比对时用这条标注，事后改标注不会让一致率变好。

`recheck/plan.json` 在 outputs.md 约定的字段之外还有：`sampling {method: "sha256_rank", rule}`、`counts`、`disputes`、`notices`；`items[]` 另含 `platform`、`disputed`、`annotation_id`。

## 复核表（recheck/plan.csv）

UTF-8 BOM、CRLF；以 `=`、`+`、`-`、`@`、制表符、回车开头的单元格加 `'` 前缀，防止表格软件当作公式执行。列：

`observation_id, entity_id, entity_name, query_id, platform, source, prompt_text, answer_text, original_mentioned, original_recommended, original_rank, recheck_mentioned, recheck_recommended, recheck_rank, recheck_note`

复核人只填 `recheck_*` 四列，另存为“CSV UTF-8”。原标注的推荐为“未判断”、或没有位次时，`original_recommended` / `original_rank` 写作 `null`（不留空）；复核人认同时照抄 `null` 即可——复核列留空表示“未复核”。

## 比对

`recheck-compare PROJECT --input FILE [--fields mentioned,recommended,rank] [--format csv|jsonl]`

- 输入为 CSV 或 JSONL，每行含 `observation_id`、`entity_id` 和复核字段；复核列优先取 `recheck_<字段>`，没有时取 `<字段>`。
- 取值：提及 / 推荐 `true yes y 1 是` 为是，`false no n 0 否` 为否（不区分大小写）；推荐另可填 `null none n/a 无` 表示未判断；提及不能为空值。位次为 ≥ 1 的整数，`null none n/a 无` 表示无位次。
- 一行的复核列全空 = 未复核；只填一部分为 `incomplete`。逐行报错的代码：`missing_id`、`unknown_observation`、`unknown_entity`、`invalid_value`、`incomplete`、`inconsistent`（未提及却被推荐或有位次）、`duplicate`、`no_annotation`、`malformed_row`。
- 只统计计划内的项；计划外的合法行列在 `unplanned_items`，不计入一致率。没有计划文件时 `plan_hash = null`，文件中每个合法行都计入。
- `pass_rate = agreed / checked`；`checked = 0` 时为 `null` 并给出 `null_reason`。
- 退出码：有非法行 1；否则 `checked = 0` 为 3；否则 0。文件缺失、编码不是 UTF-8、缺少必需的列、计划文件结构不对为 2（不写结果文件）。

`recheck/result.json` 在 outputs.md 约定的字段之外还有：`not_checked_items`、`unplanned`、`unplanned_items`、`by_source`（`sample` / `dispute` 分别计数）、`input {format, sha256, rows, blank_rows, invalid_rows}`、`errors [{line, code, column, message}]`、`notices`；`items[]` 另含 `source`、`annotation_id`、`original`、`rechecked`、`note`。
