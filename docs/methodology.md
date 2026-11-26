# 方法学与统计口径

本文说明 `geo-verify` 的指标定义、统计公式、双通道交叉验证、标注规则、剔除审计、异常识别、预检结论判定规则与已知限制。所有描述均以当前代码实现为准（口径版本 `1.0`），供委托方、服务方与复核人独立核对与复算。

> **重要说明**
>
> - 本工具只给出**预检结论**（`通过` / `有条件通过` / `不通过` / `数据不足`），不构成独立第三方验证。
> - 模拟环境或合成数据集通过，不代表真实 AI 平台可用；所有真实平台评测须由人工确认环境与样本。
> - 本工具不对任何第三方平台今后的展示、提及或推荐结果作承诺。

---

## 1. 有效观测与数据口径

### 1.1 有效观测的定义

从 `observations.jsonl` 建索引时（`geo_verify/metrics/index.py`），按下列顺序逐条判定每条观测的状态；满足全部条件的才属于**有效观测**（`valid`）：

1. **按 `observation_id` 去重**：重复出现的 `observation_id` 仅保留文件中最先出现的一条，其余记为 `duplicate_observation_id`，不计入任何指标；
2. **封存题库版本一致**：`prompt_set_hash` 必须等于当前所选封存题库（默认最新版本，或 `--prompt-set-version` 指定版本）的 `content_hash`；属于其他版本的观测记为 `prompt_set_version_mismatch`，**禁止混算**，只计数不进入任何指标；
3. **未被审计事件剔除**：若 `audit.jsonl` 中存在 `action = exclude` 的事件列出该 `observation_id`，记为 `audit:<rule_id>`，不进入指标分母（原始观测文件保持不变）；同一观测被多次剔除时保留最早的规则；
4. **题目在封存题库中**：`query_id` 必须在所选封存题库的 `prompts` 中，否则记为 `unknown_query_id`；
5. **采集状态有效**：`status == "valid"`；`status == "invalid"`（如 `timeout`、`rate_limited`、`empty_answer`、`truncated`、`garbled`、`error`、`other`）记为 `status=invalid:<invalid_reason>`，不进入指标分母。

“全部观测”（记为 `universe`，用于 `valid_observation_rate`、`raw_record_completeness`）指满足条件 1 和 2（属于所选封存题库版本且未重复）的全部观测。

### 1.2 事后指标与预先登记

指标的用途与门槛在封存题库前预先登记于 `project.json` 的 `preregistration.metrics`；封存时对整个 `preregistration` 对象计算 RFC 8785 + SHA-256 哈希写入 `prompt_set.preregistration_hash`。

- **验收指标**（`role = acceptance`，须带 `threshold`）：参与预检结论的阈值判定和样本充分性判定；
- **描述性指标**（`role = descriptive`）：仅作展示，不参与阈值判定；
- **事后指标**（`preregistered = false`、`role = post_hoc`）：以下任一情形均降为事后指标，**只能展示，不得用于验收结论**：
  1. 未在 `preregistration.metrics` 中登记；
  2. `preregistration` 在题库封存后被改动（当前哈希与 `prompt_set.preregistration_hash` 不符，触发 `preregistration_changed`，**全部指标**降为事后指标）；
  3. 登记的 `definition_version` 与工具实现版本（`1.0`）不一致；
  4. 计算对象不是项目的 `target_entity_id`（例如查看竞品指标时）。

预先登记支持以下组名展开：

- `accuracy` → 按 `preregistration.accuracy_basis` 展开为 `accuracy_verifiable` 或 `accuracy_strict`
- `sentiment_distribution` → `sentiment_positive_share`、`sentiment_neutral_share`、`sentiment_negative_share`
- `temporal_stability` → `temporal_std`、`temporal_range`、`temporal_cv`
- `cross_platform_consistency` → `cross_platform_core_facts`、`cross_platform_sources`、`cross_platform_recommendation`
- `process_quality` → `valid_observation_rate`、`environment_consistency_rate`、`raw_record_completeness`、`evidence_completeness`、`recheck_pass_rate`

### 1.3 分母为 0 的处理与下钻

- **分母为 0**：`value` 必须为 `null` 并给出中文 `null_reason`，`interval` 和 `clustering` 为 `null`，**绝不返回 `0` 或 `1`（0% 或 100%）**。未进入分母的观测数量与规则记录在 `excluded`。
- **下钻承诺**：每个 `metric_result` 在 `drilldown` 中给出计数单位（`unit`：`observation` / `query` / `citation` / `judgment` / `window` / `fact`），并对排序后的分子、分母编号列表计算 RFC 8785 + SHA-256 哈希（`numerator_ids_hash`、`denominator_ids_hash`）；列表长度 ≤ 200 时同时内联 `numerator_ids` 与 `denominator_ids`。任意结果都可用 `geo-verify metrics --drilldown METRIC [--platform ...] [--channel ...] [--window ...] [--date ...]` 复核完整编号及对应观测元信息。

---

## 2. 全部指标口径

`metrics` 默认对每个指标输出“整体”和“按平台”范围；`mention_rate` 额外按时间窗口、按采集通道展开；`recommendation_rate_daily` 按项目时区（默认 `Asia/Shanghai`）下的自然日（整体 + 按平台）展开；跨平台一致性指标仅输出整体范围。

依赖标注的指标只把“已有生效标注的有效观测”计入分母，缺少标注的有效观测记为 `unannotated` 单独计数。引用类指标按**观测内 URL 去重**（同一观测重复引用同一 URL 仅计 1 次，重复次数记入 `components.duplicate_citations`），域名无法解析的引用不进分母并记入 `components.unidentified_citations`。

| 指标编号 | 名称 | 分子 / 分母（或定义） | 单位 | 区间方法 | 已知限制 |
|---|---|---|---|---|---|
| `mention_rate` | 品牌提及率 | 提及目标主体的有效观测 / 有标注的有效观测 | `observation` | Wilson / 聚类 bootstrap | 依赖生效标注；无标注观测不进分母并单独计数 |
| `intent_coverage` | 意图覆盖率 | 出现目标主体的题 / 封存题总数（范围内无有效观测时返回 `null`） | `query` | Wilson（每题 1 单元） | 每题提问次数越多越容易“出现过”，不同采集量不宜直接比较 |
| `top1_rate` | 首位推荐率 | 位次为 1 的可排序观测 / 可排序观测 | `observation` | Wilson / 聚类 bootstrap | 只统计明确识别推荐序列的回答，不以出现先后代替位次 |
| `top3_rate` | 前三推荐率 | 位次 ≤ 3 的可排序观测 / 可排序观测 | `observation` | Wilson / 聚类 bootstrap | 同上 |
| `top5_rate` | 前五推荐率 | 位次 ≤ 5 的可排序观测 / 可排序观测 | `observation` | Wilson / 聚类 bootstrap | 同上 |
| `recommendation_rate` | 推荐达成率（整体） | 推荐成功次数 / 应推荐次数 | `observation` | Wilson / 聚类 bootstrap | “推荐成功”取自预先登记（`recommended` / `top_n` / `mentioned`）；“应推荐”取题目 `expects_recommendation = true` |
| `recommendation_rate_daily` | 推荐达成率（按日） | 同上（按项目时区自然日筛选） | `observation` | Wilson / 聚类 bootstrap | 单日样本通常较少，仅用于逐日对账 |
| `target_source_presence` | 目标信源出现率 | 出现目标信源的有效观测 / 有效观测 | `observation` | Wilson / 聚类 bootstrap | 目标信源 = 预先登记的 `target_sources` ∪ 目标主体 `owned_domains`（含子域名） |
| `target_source_share` | 目标信源引用占比 | 目标信源引用 / 全部可识别引用 | `citation` | Wilson / 聚类 bootstrap | 观测内 URL 去重；无法识别域名的引用不进分母 |
| `citation_validity` | 引用溯源有效率 | 有效且相关引用（`valid_relevant`） / 已核验的可识别引用 | `citation` | Wilson / 聚类 bootstrap | 只统计标注 `citation_checks` 中已核验的引用；未核验数单独列出，核验比例低时不能外推 |
| `authority_share` | 权威信源占比 | 权威信源引用 / 全部可识别引用 | `citation` | Wilson / 聚类 bootstrap | 权威等级由预先登记的 `authority.domain_tiers`（最长域名后缀优先）与 `authoritative_tiers` 决定；未登记等级的域名不算权威 |
| `accuracy_verifiable` | 回答准确率（可核验口径） | `supported` /（`supported` + `contradicted`） | `judgment` | Wilson / 聚类 bootstrap | 仅取有效观测上的 `answer_to_facts` 判定；本工具不评价事实核验判定器自身准确度 |
| `accuracy_strict` | 回答准确率（严格口径） | `supported` /（`supported` + `contradicted` + `unverifiable`） | `judgment` | Wilson / 聚类 bootstrap | 同上 |
| `unverifiable_rate` | 不可核验率 | `unverifiable` / 全部陈述判定 | `judgment` | Wilson / 聚类 bootstrap | `unverifiable` 不等于错误也不等于缺失 |
| `core_fact_presence` | 核心信息呈现率 | `present_correct` /（`present_correct` + `present_wrong` + `missing`） | `judgment` | Wilson / 聚类 bootstrap | 仅取有效观测上的 `facts_to_answer` 判定；只涵盖配置了 `core_fact_ids` 的题 |
| `negative_miscitation_rate` | 负面误引率 | 含无依据或错误负面（`unfounded_negative = true`）的有效观测 / 有标注的有效观测 | `observation` | Wilson / 聚类 bootstrap | 仅统计无依据或错误负面，不涉及真实负面；未评估项记入 `components.unassessed` |
| `sentiment_positive_share` `sentiment_neutral_share` `sentiment_negative_share` | 倾向分布（正 / 中 / 负） | 该倾向观测 / 有倾向标注的提及观测 | `observation` | Wilson / 聚类 bootstrap | 仅描述，不用于验收结论 |
| `temporal_std` `temporal_range` `temporal_cv` | 跨时间稳定性 | 各有数据时间窗口提及率的样本标准差（分母 $W-1$）、极差（$\max-\min$）、变异系数（$\text{std}/\text{mean}$） | `window` | 无区间 | 需 ≥ 2 个有数据窗口；均值为 0 时 `temporal_cv` 为 `null`；窗口少时估计不稳定 |
| `cross_platform_core_facts` | 跨平台一致性：核心事实项 | 各平台“正确呈现”多数结论一致的（题，核心事实） / 至少两平台有判定的（题，核心事实） | `fact` | Wilson / 聚类 bootstrap | **本项目定义**：平台内正确呈现比例 ≥ 0.5 记为“正确呈现”（整数比较 $2h \ge n$） |
| `cross_platform_sources` | 跨平台一致性：来源指向 | 各平台首要引用域名相同的题 / 至少两平台有可识别引用的题 | `query` | Wilson（每题 1 单元） | **本项目定义**：首要引用域名 = 平台内被最多观测引用的域名，并列取字典序最小者 |
| `cross_platform_recommendation` | 跨平台一致性：推荐逻辑 | 各平台推荐结论一致的题 / 至少两平台有标注观测的题 | `query` | Wilson（每题 1 单元） | **本项目定义**：平台内推荐成功比例 ≥ 0.5 记为“推荐” |
| `valid_observation_rate` | 有效观测率 | 有效观测 / 全部观测（所选题库版本） | `observation` | Wilson / 聚类 bootstrap | 其他题库版本的观测单独计数 |
| `environment_consistency_rate` | 环境一致率 | 与登记环境一致的有效观测 / 有效观测 | `observation` | Wilson / 聚类 bootstrap | `project.environment` 为 `null` 的项不作要求；`region` 支持前缀匹配（如登记 `CN` 匹配 `CN-SH`）；观测中未知项按不一致计并单列 |
| `raw_record_completeness` | 原始记录完整率 | 必备字段齐全的观测 / 全部观测 | `observation` | Wilson / 聚类 bootstrap | 可空字段为 `null` 但写明 `null_reasons` 视为齐全；只查结构齐备，不查内容真伪 |
| `evidence_completeness` | 证据完整率 | 快照文件均存在且 SHA-256 相符的有效观测 / 有效观测 | `observation` | Wilson / 聚类 bootstrap | 哈希只能证明未改动，不能证明采集时间真实 |
| `recheck_pass_rate` | 抽样复核通过率 | 复核一致的项 / 已复核的项 | `observation` | Wilson / 聚类 bootstrap | 取自 `recheck/result.json` 中对应有效观测的复核项（观测，实体） |
| `risk_identification_rate` | 风险识别率 | 已识别且复核确认的风险 / 复核确认应识别的风险 | `observation` | Wilson / 聚类 bootstrap | 仅含人工复核确认的风险；未复核项单列 |
| `risk_closure_rate` | 风险处置率 | 已闭环的风险 / 已识别的风险 | `observation` | Wilson / 聚类 bootstrap | “已识别”含复核否认的误报，误报数单独列出 |

---

## 3. 统计方法与公式

### 3.1 Wilson 置信区间

用于单题范围比例、每题仅 1 个计数单元的比例（如 `intent_coverage`），以及全 0 / 全 1 退化场景。采用无连续性校正的 Wilson 区间（`geo_verify/stats/wilson.py`）：

设成功数 $x$、分母 $n > 0$、样本比例 $\hat{p} = x / n$、双侧置信水平 $1 - \alpha$（默认 $0.95$）对应的标准正态分位数 $z = \Phi^{-1}(1 - \alpha/2)$：

$$\text{中心} = \frac{\hat{p} + \frac{z^2}{2n}}{1 + \frac{z^2}{n}}, \qquad \text{半宽} = \frac{z \sqrt{\frac{\hat{p}(1-\hat{p})}{n} + \frac{z^2}{4n^2}}}{1 + \frac{z^2}{n}}$$

$$[\text{low}, \text{high}] = [\max(0, \text{中心} - \text{半宽}), \min(1, \text{中心} + \text{半宽})]$$

当 $x = 0$ 时下界精确为 $0$，当 $x = n$ 时上界精确为 $1$。

### 3.2 按题聚类的 Bootstrap

同一道题的多次提问彼此相关，**不能当独立样本计算区间**。跨题汇总比例（题数 $k \ge 2$ 且存在题目计数单元 $> 1$）采用按 `query_id` 聚类的非参数百分位 bootstrap（`geo_verify/stats/bootstrap.py`）：

1. 按 `query_id` 升序排列所有分母大于 0 的 $k$ 道题，第 $i$ 题的分子与分母为 $(y_i, n_i)$；
2. 用 NumPy `PCG64` 随机数生成器以预先登记的 `seed` 生成 $B$ 轮（默认 $1000$）有放回的题下标矩阵 $I \in \{0,\dots,k-1\}^{B \times k}$；
3. 第 $b$ 轮的汇总比例估计为 $\hat{p}^{(b)} = \left(\sum_{j=1}^k y_{I_{b,j}}\right) / \left(\sum_{j=1}^k n_{I_{b,j}}\right)$（整数求和后做一次除法）；
4. 取 $\{\hat{p}^{(b)}\}_{b=1}^B$ 的 $\alpha/2$ 与 $1 - \alpha/2$ 分位数作为置信区间。

**退化保护**：当全部观测均命中（$\sum y_i = \sum n_i$）或全部未命中（$\sum y_i = 0$）时，重抽区间退化为零宽；代码自动改用以题数 $k$ 为样本量的 Wilson 区间（相当于假设题内完全相关，偏保守），并在 `components.interval_note` 中注明。

### 3.3 题内相关系数（ICC）、设计效应与有效样本量

对有数据的 $k$ 道题（第 $i$ 题命中 $y_i$ 次、共 $n_i$ 次，$N = \sum_{i=1}^k n_i$，$\bar{p} = (\sum y_i)/N$，$p_i = y_i / n_i$），采用适用于不平衡聚类二值数据的单因素方差分析（ANOVA）估计（`geo_verify/stats/clustering.py`）：

$$\text{MSB} = \frac{\sum_{i=1}^k n_i (p_i - \bar{p})^2}{k - 1}, \qquad \text{MSW} = \frac{\sum_{i=1}^k y_i (1 - p_i)}{N - k}$$

$$n_0 = \frac{N - \frac{\sum_{i=1}^k n_i^2}{N}}{k - 1}, \qquad \widehat{\text{ICC}} = \text{clip}_{[0,1]}\!\left(\frac{\text{MSB} - \text{MSW}}{\text{MSB} + (n_0 - 1)\cdot \text{MSW}}\right)$$

当 $k < 2$、$N = k$（每题仅 1 次）或分母 $\le 0$（全 0 或全 1）时，$\widehat{\text{ICC}}$ 无法估计，返回 `null`。

设计效应与有效样本量：

$$m = \frac{\sum_{i=1}^k n_i^2}{N}, \qquad \text{deff} = 1 + (m - 1) \cdot \widehat{\text{ICC}}, \qquad N_{\text{eff}} = \frac{N}{\text{deff}}$$

各题次数相等时 $m$ 即每题提问次数。例如每题 $m = 10$ 次、$\text{ICC} = 0.5$ 时，$\text{deff} = 5.5$，$300$ 次观测约折合 $54.5$ 次独立观测。

### 3.4 基线配对比较（`compare`）

- **拒绝比较（`status = refused`，退出 1）**：两期所选封存题库的 `content_hash` 不同（禁止混算），或任一（平台，通道）在基线期与当前期之间的 `model_version` 集合不同，或任一期内部出现多个模型/产品版本（须重建基线）。不提供绕过开关。
- **范围对齐**：仅合并两期均有数据的（平台，通道），避免渠道或平台结构变化制造虚假变化。
- **同题配对估计**（`geo_verify/stats/compare.py`）：对两期分母均 $> 0$ 的共同题目集合 $Q$，逐题计算比例差 $d_q = p_{q,\text{当前}} - p_{q,\text{基线}}$，点估计为每题等权均值 $\bar{d} = \frac{1}{|Q|}\sum_{q\in Q} d_q$；$|Q| \ge 2$ 时对 $\{d_q\}$ 做按题聚类 bootstrap（重抽 $Q$ 的均值的百分位区间）。
- **结论规则**：
  - 任一期不满足各自预先登记的样本充分性，或 $|Q| < 2$ → 一律 `不能判定`（`inconclusive`）；
  - 区间下界 $> 0$ → `提升`（`improved`）；
  - 区间上界 $< 0$ → `下降`（`declined`）；
  - 区间包含 $0$（含端点）→ `不能判定`（`inconclusive`）。
  - 另输出 `favorable` 表示对目标主体是否有利（对“越低越好”的指标如 `negative_miscitation_rate`，下降为有利）。

### 3.5 样本量计算（`samplesize`）

1. **两比例比较（每组样本量）**：正态近似、非合并方差、双侧显著性水平 $\alpha$（默认 $0.05$）、功效 $1 - \beta$（默认 $0.80$）、无连续性校正、向上取整：
   $$n = \left\lceil \frac{\left(z_{1-\alpha/2} + z_{\text{power}}\right)^2 \left[p_1(1-p_1) + p_2(1-p_2)\right]}{(p_1 - p_2)^2} \right\rceil$$
   该公式假定观测独立；若同题重复提问，总观测数须再乘以设计效应 $\text{deff}$。
2. **零失败样本量**：证明合格率 $\ge \text{target}$、单侧置信度 $c$（默认 $0.95$）所需连续无失败次数：
   $$n = \left\lceil \frac{\ln(1 - c)}{\ln(\text{target})} \right\rceil$$
3. **设计效应换算**：给定每题次数 $m$、$\text{ICC}$ 和总观测数 $N$，输出 $\text{deff} = 1 + (m-1)\cdot \text{ICC}$ 与 $N_{\text{eff}} = N / \text{deff}$。

### 3.6 样本充分性

默认要求（对应 047 §8.3，可在封存前通过 `preregistration.sufficiency` 登记）：

- 时间窗口数 $\ge 3$；
- 单一关键指标的有效观测数 $\ge 30$。

两项均满足才记 `sufficient = true`；任一项不满足则不能给出肯定结论（预检报告判 `数据不足`，交叉验证判 `不可判定`，基线对比判 `不能判定`）。

---

## 4. 双通道交叉验证（`crosscheck`）

### 4.1 偏离度公式（本项目定义）

设通道 A 比例为 $A = a / n$，通道 B 比例为 $B = b / m$：

$$\text{偏离度} = \begin{cases} 0, & A = B = 0 \\ \dfrac{|A - B|}{(A + B) / 2} = \dfrac{2|am - bn|}{am + bn}, & \text{其他情形} \end{cases}$$

取值范围 $[0, 2]$，默认阈值 $0.3$（30%）。判定使用精确有理数比较（`Fraction`），偏离度 $\le$ 阈值记为通过，避免边界浮点误差。

> **重要声明**：**此公式是本项目的定义**。047 给出了 30% 的阈值，但未给出偏离度的计算公式；因此公式、阈值、指标与通道均须在题库封存前预先登记（`preregistration.crosscheck`），若命令行覆盖了任一参数或封存后改动了预先登记，结果记 `preregistered = false`。

### 4.2 百分点差与差值区间

同时输出比例差 $A - B$（报告中以百分点差展示）及其置信区间：

- 若两通道覆盖的题目集合完全相同且题数 $\ge 2$：使用同题配对的聚类 bootstrap（`paired_cluster_bootstrap`）；
- 若两通道题目集合不完全相同且各自题数 $\ge 2$：改用两通道各自独立按题重抽（`unpaired_cluster_bootstrap`，A 侧用 `seed`、B 侧用 `seed + 1`，偏保守）并附说明；
- 任一通道不足 2 道题时差值区间为 `null`。

### 4.3 合并与不可判定规则

- 每个平台单独一行；合并行（`platform = null`）**只合并两个通道均有数据（分母 $> 0$）的平台**；
- 任一通道分母为 0 或样本不充分时，该行结论为 `undetermined`（`不可判定`），**即使偏离度为 0 也不判通过**；
- 整体结论：合并行或任一样本充分的平台行为 `fail` → `fail`（退出 1）；否则合并行 `undetermined` → `undetermined`（退出 3）；否则 `pass`（退出 0）。

---

## 5. 提及、推荐、否定、位次与标注一致性

### 5.1 别名匹配与消解（`annotate/matching.py`）

1. **规范化**：按字符单元做 NFKC（全半角折叠）→ `casefold`（大小写折叠）→ 去除空白、连字符（Unicode `Pd`）、间隔号（`·`、`・`、`‧`、`−`）和不可见格式字符（`Cf`），并保留每字符在原文中的起止索引；
2. **词边界**：别名以字母或数字开头/结尾时，原文紧邻的前/后字符不能是字母或数字（避免 `S7` 误命中 `XS70`）；
3. **排除词**：候选完全落在本实体 `exclusions` 短语范围内时作废（如“波澜序曲”中的“澜序”）；
4. **跨实体最长优先、互不重叠**：先匹配全部实体再归属，保证“岑野 S7”整体归属竞品车型，不拆分误算目标车型；
5. **同形异义与异品牌归属**：同一位置命中多个实体时，按同句最近前文提及的品牌消解；无法消解的不计入任何实体，写入 `ext.ambiguous_spans` 供人工复核。产品别名紧跟其他品牌领属短语后（如“岑野的 S7”）不算目标产品提及。
6. **层级汇总**：产品被提及时，其 `parent_entity_id` 链上的上级品牌也记为提及。

### 5.2 推荐、否定与倾向（`annotate/rules.py`、`lexicon.py`）

- **分句判断**：按分句识别正向推荐语（`推荐`、`首推`、`值得考虑` 等）、否定推荐语（`不建议`、`不要买`、`慎选` 等），先滤除中性疑问词（`推不推荐`、`值不值得` 等）和疑问分句；
- **否定修饰与双重否定**：正向推荐语前有 1 个否定词（`不`、`没有`、`不太` 等）判为否定推荐（“不推荐 X”不是推荐）；2 个及以上否定词（“不是不推荐”）或否定推荐语前再带否定词（“不是不建议”）或带“未必 / 不一定”一律保守记为拿不准（`recommended = null`）；
- **比较对象排除**：“比 X 更值得买”“与 X 相比”“X 不如 …”“而不是 X”中的 X 不受该分句推荐语和倾向词影响；
- **同句单主体承接**：无提及（或仅比较对象）的分句含推荐语或倾向词时，若同句前面最近一个有提及的分句只有一个主体且自身无推荐语，则承接给该主体；
- **冲突保守规则**：同一回答对同一实体（含下级产品）既有推荐又有否定表达时，保守记 `negated = true`、`recommended = false` 并注明 `conflict`；
- **倾向与风险**：仅按分句内倾向词和推荐语方向给出 `positive` / `neutral` / `negative`（正负并存或未提及为 `null`）。规则不判定风险和引用有效性，存在负面信号时 `unfounded_negative` 记为 `null` 交由事实核验或人工判断。

### 5.3 推荐序列与位次（`annotate/sequence.py`）

仅在回答中存在**唯一、明确**的推荐序列时才给位次，**绝不以出现先后代替位次**：

- 识别三类结构：编号列表（`1.`、`1、`、`1)`、`(1)`、`①`、`第一名：` 等，排除小数）、排序语（须先有“首选/首推”紧接“其次”）、推荐顺序链（须有“推荐顺序/排名/优先级”等引导词接 `A > B > C`）；
- 忽略不含已知实体的步骤/参数列表；引导语为否定推荐的列表记为否定列表（不给位次，项主体按否定提及处理）；
- 编号未从 1 起连续、不足 2 项、任一项主体不明或重复、项主体在本项中被否定/拿不准、或回答中存在多个推荐序列时，保守记 `rankable = false`、`rank = null`、`rank_list_size = null`。

### 5.4 人工覆盖与 Cohen's Kappa

- **历史保留与生效优先级**：`annotations.jsonl` 只追加；`annotate override` 追加人工标注并通过 `supersedes` 指向原标注，同时在 `audit.jsonl` 写入 `action = override` 事件。解析生效标注时排除已被 supersede 的记录，优先级 `human > model > rule`，同级取 `created_at` 最新及 `annotation_id` 较大者。
- **双人标注一致性**（`annotate/kappa.py`）：对两名标注者共同标注的（观测，实体），在 `mentioned` 与 `recommended`（`null` 作为独立类别）上用整数公式计算 Cohen's kappa：
  $$\kappa = \frac{n \cdot \text{一致数} - \sum_k n_A(k) n_B(k)}{n^2 - \sum_k n_A(k) n_B(k)}$$
  保留 6 位小数；当期望一致率 $p_e = 1$（两人全部给出同一类别）时 $\kappa$ 无定义，返回 `null` 并写明原因。所有分歧写入 `out/disputes.json`；若分歧记录之后出现一条带审计事件 `override` 的人工标注，该争议项记为 `resolved = true`。

---

## 6. 剔除审计与选择性剔除（`metrics/exclusion.py`）

观测剔除只能通过 `exclude` 命令写入 `audit.jsonl`（规则编号、数量、观测编号、操作人与理由），`observations.jsonl` 保持不变。

计算 `metrics` 时自动执行剔除审计：

1. **规则登记检查**：若使用了未在 `project.json` 的 `preregistration.exclusion_rules` 中预先登记的规则，触发 `unregistered_exclusion_rules`，预检直接判**不通过**；
2. **反事实对比**：将被审计事件剔除、但本身 `status = valid` 且题目在封存题库中的观测放回索引，对 `mention_rate` 及所有预先登记为验收指标的可比较比例指标（`mention_rate`、`top1/3/5_rate`、`recommendation_rate`、`negative_miscitation_rate`、`target_source_presence`），对比保留样本与被剔除样本的比例；
3. **选择性剔除判定条件**（三项同时满足即判 `selective = true`，触发 `selective_exclusion`，预检判**不通过**）：
   - 被剔除样本中可比较的观测数 $\ge 5$（`SELECTIVE_MIN_EXCLUDED`）；
   - 保留样本相对被剔除样本朝“抬高结果”方向的比例差 $\ge 0.10$（`SELECTIVE_MIN_DIFFERENCE`）；
   - 单侧超几何检验（Fisher 精确检验单侧形式）$p < 0.05$（`SELECTIVE_ALPHA`）。

样本少于 5 条时不做选择性判定，但仍完整列出两组比例与 $p$ 值供人工核查。

---

## 7. 异常识别与引用域名变化（`alerts`）

`alerts` 比较两个时间窗口（默认最近两个有有效观测的窗口）或项目时区下的两个自然日。阈值覆盖优先级为：`默认值 < project.json 的 ext.alerts.thresholds < 命令行 --threshold 规则.键=值`。

比例变化类规则同时检查两窗最小样本量、绝对变化幅度、相对变化幅度与单侧检验显著性（合并方差两比例单侧 z 检验；平均位次用 Welch 单侧 z 检验）。由于同题重复提问彼此相关，此处独立假设下的 $p$ 值偏乐观，仅作为防止小样本误报的门槛之一，不替代聚类置信区间。

| 规则编号 | 监测内容 | 默认触发阈值（均须同时满足） | `critical` 升级线 |
|---|---|---|---|
| `mention_drop` | 提及率骤降（整体与分平台） | 两窗有效标注观测各 $\ge 20$；绝对下降 $\ge 0.15$；相对下降 $\ge 20\%$；单侧 $p \le 0.05$ | 绝对下降 $\ge 0.30$ |
| `mention_drop`（掉位） | 前三推荐率下滑 / 平均位次后移 | 可排序各 $\ge 10$、绝对下降 $\ge 0.20$、相对下降 $\ge 20\%$、$p \le 0.05$；或有位次各 $\ge 10$、平均位次后移 $\ge 1.0$ 位、$p \le 0.05$ | 前三绝对下降 $\ge 0.30$；或平均位次后移 $\ge 2.0$ 位 |
| `negative_spread` | 无依据/错误负面占比上升或扩散平台数增加 | 占比：两窗各 $\ge 20$、当前 $\ge 3$ 条、绝对上升 $\ge 0.05$、相对上升 $\ge 50\%$、$p \le 0.05$；扩散：单平台 $\ge 3$ 条算“出现”，出现平台数增加 $\ge 1$ | 占比上升 $\ge 0.15$；或增加平台数 $\ge 2$ |
| `citation_failure` | 已核验引用大面积失效 | 当前已核验引用 $\ge 20$、失效数 $\ge 5$ 且失效占比 $\ge 0.30$；或两窗各 $\ge 20$、当前失效 $\ge 5$、绝对上升 $\ge 0.15$、相对上升 $\ge 50\%$、$p \le 0.05$ | 当前失效占比 $\ge 0.50$ |
| `competitor_surge` | 竞品提及率或推荐率异常提升 | 两窗各 $\ge 20$、绝对上升 $\ge 0.15$、相对上升 $\ge 25\%$、$p \le 0.05$ | 绝对上升 $\ge 0.30$ |
| `platform_conflict` | 同平台对同一核心事实的口径冲突 | 当前窗口/日期内，同一平台上同一核心事实 `present_correct` 与 `present_wrong` 各 $\ge 3$ 条观测 | 固定 `warning` |

**引用域名变化与相关线索**：按观测内 URL 去重统计基线与当前各域名引用次数，输出 `added` / `removed` / `changed`。每条告警附一条待核查线索（默认最多列 10 个变化最大的域名）。**所有告警文本与线索只表述“同期出现”“相关线索”“待核查”，绝不表述因果**；输出前经 `alerts/wording.py` 强制过滤因果词和红线禁用词。

---

## 8. 预检结论的判定规则（`report`）

`report` 只读取 `out/`、`recheck/` 与项目文件，不重新计算指标。共评估 **18 条固定编号规则**，分属 4 个规则组（`geo_verify/report/rules.py`）：

| 顺序 | 规则编号 | 所属组 | 名称 | 触发 `fail` / `insufficient` / `warning` 的条件 |
|---|---|---|---|---|
| 1 | `R-EVIDENCE` | `integrity` | 证据校验 | `evidence-verify.json` 为 `fail` → `fail`；文件缺失 → `not_run`（计入保留） |
| 2 | `R-EXCLUSION-UNREGISTERED` | `integrity` | 剔除规则均已预先登记 | 使用未预先登记的剔除规则 → `fail`；缺少 `exclusion_audit` → `warning` |
| 3 | `R-EXCLUSION-SELECTIVE` | `integrity` | 无选择性剔除 | 判定为选择性剔除 → `fail`；缺少 `exclusion_audit` → `warning` |
| 4 | `R-PREREG-CHANGED` | `integrity` | 预先登记内容在封存后未改动 | 封存后 `preregistration` 哈希变化 → `fail` |
| 5 | `R-PROMPTS-SEALED` | `integrity` | 封存题库未被改动 | 封存题库内容哈希或版本链校验失败 → `fail` |
| 6 | `R-METRICS` | `sufficiency` | 指标结果 | 缺少 `out/metrics.json` → `insufficient` |
| 7 | `R-ACCEPTANCE-DEFINED` | `sufficiency` | 有预先登记的验收指标及其结果 | 无验收指标，或任一验收指标缺少整体范围验收结果（含被降为事后指标）→ `insufficient` |
| 8 | `R-SUFFICIENCY` | `sufficiency` | 验收指标样本充分 | 任一验收指标样本不充分或值为 `null` → `insufficient` |
| 9 | `R-MODEL-VERSION` | `sufficiency` | 观测期内模型与产品版本未变化 | 存在 `model_version_changed` 提示 → `insufficient` |
| 10 | `R-THRESHOLD` | `target` | 验收指标达到登记阈值 | 点估计未达阈值 → `fail`；点估计达标但区间跨过阈值或无区间 → `warning` |
| 11 | `R-CROSSCHECK` | `target` | 双通道交叉验证 | `status = fail` → `fail`；`undetermined` 或 `preregistered = false` → `warning`；缺失 → `not_run`（计入保留） |
| 12 | `R-RECHECK` | `reservation` | 抽样复核 | 仅生成计划未比对、参数非预先登记、计划哈希不一致、有未复核项或错误行 → `warning`；缺失 → `not_run`（计入保留） |
| 13 | `R-DISPUTES` | `reservation` | 双人标注争议已解决 | `unresolved > 0` → `warning` |
| 14 | `R-ALERTS` | `reservation` | 无异常告警 | `status = alert` 或 `insufficient`（无法比较）→ `warning` |
| 15 | `R-COMPARE` | `reservation` | 基线对比可进行 | `status = refused` 或 `insufficient` → `warning` |
| 16 | `R-PROMPT-VERSION` | `reservation` | 观测均属于所选题库版本 | 存在不属于所选封存题库版本的观测 → `warning` |
| 17 | `R-DATA-NOTICES` | `reservation` | 指标计算无其他警示 | 存在未标注观测、重复编号、未登记平台等其他 `warning`/`error` 提示 → `warning` |
| 18 | `R-INPUT-CONSISTENCY` | `reservation` | 结果文件依据同一题库版本 | 各结果文件的 `prompt_set.content_hash` 与当前封存题库不一致 → `warning` |

### 判定优先级与退出码

按下列严格顺序得出唯一结论：

1. `integrity` 组任一规则为 `fail` → **`不通过`**（`fail`，退出码 `1`）；
2. 否则任一规则为 `insufficient` → **`数据不足`**（`insufficient_data`，退出码 `3`）；
3. 否则 `target` 组任一规则为 `fail` → **`不通过`**（`fail`，退出码 `1`）；
4. 否则存在任一 `warning`，或 `R-EVIDENCE`、`R-CROSSCHECK`、`R-RECHECK` 任一项为 `not_run` → **`有条件通过`**（`conditional_pass`，退出码 `0`），逐条列出保留意见；
5. 否则 → **`通过`**（`pass`，退出码 `0`）。

---

## 9. 已知限制

1. **只给预检结论**：工具输出不能替代独立第三方验收，涉及利益关联时报告会显式给出警示，最终结论须由相关方核对并人工签字。
2. **模拟与合成不等于真实**：模拟 ASGI 引擎与合成数据集（`synthetic: true`）用于验证代码逻辑与统计性质，不代表任何真实 AI 平台的效果或可用性。
3. **API 通道不等于用户真实所见**：通过 OpenAI 兼容接口（`channel = api`）采集的回答可能与网页端、App 端在系统提示、联网检索策略、模型版本及界面展示上存在显著差异，不能直接称作“用户真实所见”。
4. **规则标注的语义边界**：规则引擎无法覆盖反讽、隐晦指代、跨段落指代或复杂排版推荐列表，且不自动判断负面信息是否有事实依据或引用链接是否真实可达，须配合 `recheck-plan` / `recheck-compare` 与 `annotate override` 做人工复核。
5. **哈希的证明边界**：证据清单链和行哈希只能证明文件自建包后未被篡改，不能证明采集时间真实或回答确系平台当时输出；可信时间戳（`timestamp_token`）仅预留接口，未接入外部时间戳服务（详见 [evidence.md](evidence.md)）。
6. **模型版本可见性局限**：网页端或人工采集渠道若未展示模型版本（`model_version = null`），工具无法自动识别该渠道背后的静默模型升级。
7. **事实判定准确度边界**：`geo-verify` 只消费外部事实核验工具（如 `geo-facts`）产出的 `judgments.jsonl`，本身不评估判定器的混淆矩阵与精度；判定器校准须在事实核验侧完成。
