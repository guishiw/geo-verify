# 命令与目录约定

本文是 `geo-verify` 的接口说明：项目目录里有哪些文件、每个命令读什么写什么、退出码是什么。
第三方复算时只需要这份约定和 [`spec/v1/`](../spec/v1/) 下的数据契约。

## 通用约定

- 所有命令支持 `--json`，输出机器可读结果；不加时输出中文说明。
- 退出码：`0` 成功，`1` 检查未通过，`2` 输入错误，`3` 数据不足，`4` 内部错误。
- 会写入时间戳的命令支持 `--now <UTC 时间>`（如 `2026-10-08T02:00:00Z`）。传入后输出逐字节可复现；不传则取当前时间。
- 涉及随机的命令支持 `--seed`；默认取 `project.json` 中预先登记的 seed。同输入同 seed 输出逐字节一致。
- 原始记录（观测、标注、审计事件）只追加，任何命令都不会覆盖或删除已有行。
- 默认不联网。只有 `collect --adapter openai --live` 会访问外部地址，密钥只从环境变量读取。

## 项目目录

```text
PROJECT/
├── project.json               project 契约：委托方、服务方、时区、平台、预先登记的指标口径和阈值、利益关系披露
├── entities.jsonl             entity 契约：品牌、产品、竞品及别名、排除词、自有域名
├── prompts/
│   ├── draft.jsonl            草稿题库，每行一题（结构同 prompt_set.prompts 的元素）
│   ├── sealed/v0001.json      已封存题库（prompt_set 契约），只增不改
│   └── diffs/v0001-v0002.json 版本差异记录
├── observations.jsonl         observation 契约，只追加
├── snapshots/                 快照文件（观测中以相对路径和 SHA-256 引用）
├── annotations.jsonl          annotation 契约，只追加（保留全部历史）
├── judgments.jsonl            judgment 契约，由事实核验工具产出后放入
├── audit.jsonl                audit_event 契约，哈希链，只追加
├── recheck/
│   ├── plan.json              抽样复核计划
│   └── result.json            抽样复核比对结果
├── evidence/
│   ├── manifest-0001.json     manifest 契约，按序号成链
│   └── index/                 JSONL 文件的逐行哈希索引
└── out/                       生成物
    ├── metrics.json           指标结果（metric_result 契约的列表）
    ├── crosscheck.json        双通道交叉验证结果
    ├── compare.json           基线对比结果
    ├── alerts.json            异常与引文变化
    ├── evidence-verify.json   证据校验结果
    ├── disputes.json          双人标注争议项
    ├── report.json            预检报告（机器复核）
    ├── report.html            预检报告（可打印）
    └── observations.csv       原始记录索引
```

`report` 只读取 `out/` 下各命令的结果文件和项目文件，不重新计算指标；某个结果文件缺失时，对应判定规则记为“未执行”。

## 指标编号

| 编号 | 指标 | 分子 / 分母 |
|---|---|---|
| `mention_rate` | 品牌提及率 | 提及目标主体的有效观测 / 有效观测 |
| `intent_coverage` | 意图覆盖率 | 出现目标主体的题 / 封存题总数 |
| `top1_rate` `top3_rate` `top5_rate` | 首位、前三、前五推荐率 | 处于该位置的可排序观测 / 可排序观测 |
| `recommendation_rate` | 推荐达成率（整体） | 推荐成功次数 / 应推荐次数 |
| `recommendation_rate_daily` | 推荐达成率（按日，`scope.date` 为项目时区下的自然日） | 同上 |
| `target_source_presence` | 目标信源出现率 | 出现目标信源的有效观测 / 有效观测 |
| `target_source_share` | 目标信源引用占比 | 目标信源引用 / 全部可识别引用 |
| `citation_validity` | 引用溯源有效率 | 有效且相关引用 / 全部可识别引用 |
| `authority_share` | 权威信源占比 | 权威信源引用 / 全部可识别引用 |
| `accuracy_verifiable` | 回答准确率（可核验口径） | 支持 /（支持 + 矛盾） |
| `accuracy_strict` | 回答准确率（严格口径） | 支持 / 全部陈述 |
| `unverifiable_rate` | 不可核验率 | 不可核验 / 全部陈述 |
| `core_fact_presence` | 核心信息呈现率 | 正确呈现的核心事实 / 应呈现核心事实 |
| `negative_miscitation_rate` | 负面误引率 | 含无依据或错误负面的观测 / 有效观测 |
| `sentiment_positive_share` `sentiment_neutral_share` `sentiment_negative_share` | 倾向分布（仅描述） | 该倾向的观测 / 有倾向标注的提及观测 |
| `temporal_std` `temporal_range` `temporal_cv` | 跨时间稳定性（各窗口提及率的标准差、极差、变异系数） | — |
| `cross_platform_core_facts` `cross_platform_sources` `cross_platform_recommendation` | 跨平台一致性（核心事实项、来源指向、推荐逻辑） | — |
| `valid_observation_rate` | 有效观测率 | 有效观测 / 全部观测 |
| `environment_consistency_rate` | 环境一致率 | 与登记环境一致的有效观测 / 有效观测 |
| `raw_record_completeness` | 原始记录完整率 | 必备字段齐全的观测 / 全部观测 |
| `evidence_completeness` | 证据完整率 | 快照存在且哈希相符的有效观测 / 有效观测 |
| `recheck_pass_rate` | 抽样复核通过率 | 复核一致的项 / 已复核的项 |
| `risk_identification_rate` | 风险识别率 | 已识别且复核确认的风险 / 复核确认应识别的风险 |
| `risk_closure_rate` | 风险处置率 | 已闭环的风险 / 已识别的风险 |

“有效观测”指 `status = valid`、属于当前封存题库版本、且未被审计事件剔除的观测。
分母为 0 时 `value` 为 `null` 并给出 `null_reason`，不返回 0 或 100%。
未在封存前登记的指标 `preregistered = false`、`role = post_hoc`，不能用于验收结论。

## 命令

### `init PROJECT [--example auto] [--project-id ID] [--now T]`

新建项目目录。`--example auto` 写入虚构汽车品牌的完整样例（项目配置、实体、草稿题库、待导入的观测文件）。目录非空时退出 2。

### `validate PATH --type CONTRACT`

按契约校验 `.json`（单个对象或数组）或 `.jsonl`。不合契约退出 1，逐条列出位置和原因。

### `prompts lint PROJECT [--file FILE]`

检查草稿题库：重复、近似重复、缺失的意图类型、个人信息、诱导性提问（预设结论、限定褒义、排除竞品）。
输出 `findings`（`rule`、`severity`、`query_ids`、`message`）。有 `error` 级问题退出 1。

### `prompts seal PROJECT --by NAME [--reason TEXT] [--now T]`

封存草稿题库。先执行 lint，有 `error` 级问题时**拒绝封存**（退出 1）。
首次封存生成 `prompts/sealed/v0001.json` 并写审计事件 `seal`。
已有封存版本且内容有变化时，必须给 `--reason`，生成新版本、差异记录和审计事件 `amend_prompts`；内容无变化时不产生新版本。
封存时把 `project.json` 预先登记内容的哈希写入 `preregistration_hash`。

### `prompts diff PROJECT [--from N] [--to M]`

输出两个封存版本之间新增、删除、修改的题目。默认比较最近两个版本。

### `import PROJECT --input FILE [--format csv|jsonl] [--resume] [--operator NAME] [--now T]`
### `import --template csv|jsonl [--out FILE]`

从 CSV / JSONL 导入观测。逐行报告错误（行号、列、原因）；合法行追加写入，非法行不写入。有非法行退出 1。
以 `trace_id` 去重：重复执行或加 `--resume` 续导时，已存在的 `trace_id` 被跳过并计数，不会重复写入。
`--template` 输出导入模板。

### `collect PROJECT --adapter openai|manual|share ...`

- `--adapter openai --platform ID --asgi-app <模块或文件>:<对象> --window W [--repeats N] [--queries Q1,Q2] [--model NAME] [--resume] [--now T]`
  通过 OpenAI 兼容接口采集，默认只连接进程内的模拟服务。观测的 `channel` 固定为 `api`。
  超时、限流、空回答、截断、乱码记为无效观测（`status = invalid` + `invalid_reason`），不进入指标分母。
  续跑时已有记录的任务被跳过，不重复计数。
  真实调用必须显式 `--live --base-url URL --api-key-env 变量名`；密钥只从环境变量读取。
- `--adapter manual --action pack --platform ID --window W [--repeats N] --out DIR` 生成人工采集任务清单；
  `--adapter manual --action fill --input FILE` 把人工回填的回答、引用和快照导入为观测（`channel = manual`）。
- `--adapter share --input FILE --platform ID --window W`（P1）解析用户提供的本地分享文件，不联网。

### `annotate rules PROJECT [--now T]`

按规则为每个（观测，实体）生成提及、推荐、否定、位次标注。已有生效标注的不重复生成。

### `annotate override PROJECT --observation ID --entity ID --set k=v[,k=v] --annotator NAME --reason TEXT [--now T]`

人工覆盖：追加一条人工标注并用 `supersedes` 指向被覆盖的标注，同时写审计事件 `override`。历史全部保留。

### `annotate import PROJECT --input FILE --annotator NAME [--now T]`

批量导入一名标注者的人工标注（CSV / JSONL）。

### `annotate kappa PROJECT --a NAME --b NAME [--field mentioned|recommended]`

计算两名标注者的 Cohen's kappa，输出一致数、分歧数；争议项写入 `out/disputes.json` 并进入复核。

### `exclude PROJECT --rule-id ID (--ids ID1,ID2 | --ids-file FILE) --actor NAME --reason TEXT [--now T]`

剔除观测。只写审计事件（规则、数量、观测编号），不改动 `observations.jsonl`。
规则未在 `project.json` 的 `exclusion_rules` 中预先登记时仍会记录，但报告的预检结论会因此不通过。

### `metrics PROJECT [--prompt-set-version N] [--seed N] [--iterations N] [--out FILE]`
### `metrics PROJECT --drilldown METRIC [--platform P] [--channel C] [--window W] [--date D]`

计算全部指标，写入 `out/metrics.json`。每个结果含分子、分母、剔除数与规则、区间、样本充分性、口径版本。
只计算所选封存题库版本的观测；属于其他版本的观测单独计数并列出，不参与计算。
`--drilldown` 列出某个指标在某个范围内的分子、分母编号列表，其哈希与结果中的 `drilldown` 承诺一致。

### `compare BASELINE_PROJECT CURRENT_PROJECT [--metric ID] [--platform P] [--seed N] [--out FILE]`

同题配对的基线对比，输出差值、区间和结论 `提升` / `下降` / `不能判定`。
题库版本不同、或模型/产品版本发生变化时拒绝比较（退出 1），并提示须重建基线。样本不足退出 3。

### `crosscheck PROJECT [--metric ID] [--channel-a A] [--channel-b B] [--platform P]`

双通道交叉验证。`偏离度 = |A − B| / ((A + B) / 2)`，`A = B = 0` 时为 0；**此公式是本项目的定义**。
同时输出百分点差和区间。不超过阈值退出 0，超过退出 1，样本不足为“不可判定”退出 3。结果写入 `out/crosscheck.json`。

### `samplesize --p1 X --p2 Y [--alpha 0.05] [--power 0.8]`
### `samplesize --zero-failure --target X [--confidence 0.95]`
### `samplesize --design-effect --cluster-size M --icc R [--n N]`

样本量计算：两比例比较（正态近似、非合并方差、双侧、无连续性校正、向上取整）、零失败样本量、设计效应与有效样本量。

### `alerts PROJECT [--baseline-window W] [--current-window W]`

识别提及率骤降、错误负面扩散、引用大面积失效、竞品异常提升、同平台口径冲突，并输出引用域名变化。
引文变化只作为**待核查的相关线索**，不表示因果。有告警退出 1。结果写入 `out/alerts.json`。

### `recheck-plan PROJECT [--fraction 0.2] [--seed N]`

按固定 seed 抽取题目生成抽样复核清单，写入 `recheck/plan.json`。

### `recheck-compare PROJECT --input FILE`

把人工复核结果与原标注比对，输出一致率，写入 `recheck/result.json`。

### `evidence build PROJECT [--now T]`

生成证据清单 `evidence/manifest-NNNN.json`：原始数据、快照、口径配置、代码版本、依赖锁哈希、seed，以及指向前一清单的哈希。

### `evidence verify PROJECT`

校验清单链和全部文件，定位到文件和行：新增、删除、修改、重排。有问题退出 1。结果写入 `out/evidence-verify.json`。

### `report PROJECT [--formats json,html,csv] [--now T]`

生成预检报告。结论只有 `通过`、`有条件通过`、`不通过`、`数据不足`，并逐条列出判定规则。
`通过` 和 `有条件通过` 退出 0，`不通过` 退出 1，`数据不足` 退出 3。
报告含基线、周期、抽样复核清单、限制条件、利益关系披露栏和人工签字栏；只是预检结论，不构成独立第三方验证。
