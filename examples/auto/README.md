# 虚构汽车品牌完整样例（澜序汽车）

本目录提供一套**全部合成、可离线运行**的端到端示例，展示从项目初始化、题库封存、观测导入、规则标注、事实核验判定接入、指标计算、双通道交叉验证、异常识别、抽样复核计划、证据包构建与校验到预检报告生成的完整流程。

> **合成数据声明**
>
> - 样例中的品牌（澜序 / 岑野 / 朔风）、车型（澜序 S7 / X5 / E3 / M9、岑野 K5 / S7、朔风 T6）、平台（`engine-a` / `engine-b`）与域名（`*.example`）均为虚构，记录均带 `"synthetic": true`。
> - 生成的 HTML 报告带可见“合成数据”水印，不得作为任何真实品牌或真实平台效果宣传。
> - 工具只输出**预检结论**，不构成独立第三方验证。

## 如何运行

在仓库根目录完成一次依赖安装（`make setup`）后，直接离线运行：

```bash
# 运行到指定输出目录（目录须不存在或为空）
sh examples/auto/run.sh out/auto-demo

# 或不传参数，自动在临时目录中运行
sh examples/auto/run.sh
```

脚本优先使用仓库内的 `.venv/bin/geo-verify`，也可通过环境变量 `GEO_VERIFY_BIN` 指定可执行文件路径（或使用 `uv run geo-verify` 手工逐步执行）。

## 样例包含什么

随包分发在 `src/geo_verify/examples/auto/` 中（由固定 seed 的脚本 [`tools/generate_auto_example.py`](../../tools/generate_auto_example.py) 确定性生成）：

| 文件 | 说明 |
|---|---|
| `project.json` | 项目配置与预先登记口径（2 个模拟平台 `engine-a` / `engine-b`、双通道 `api` / `manual`、验收指标与阈值、交叉验证口径、样本充分性 3 窗口 / 30 次、固定 seed） |
| `entities.jsonl` | 10 个虚构汽车品牌与车型实体，含别名、同形异义排除词（`波澜序曲`）、竞品同名车型（`岑野 S7`）及自有域名 |
| `prompts/draft.jsonl` | 22 道草稿题，覆盖全部 11 类意图，部分题目配置 `expects_recommendation` 与 `core_fact_ids` |
| `inbox/observations.csv` | 3 个时间窗口（W1～W3）、双平台双通道的 532 条合成观测（含 4 条无效观测），包含推荐序列、否定句、别名变体与 inline / source_list 引用 |
| `inbox/judgments.jsonl` | 模拟 `geo-facts` 事实核验工具产出的双向判定样例（`answer_to_facts` 与 `facts_to_answer`），放入项目根目录 `judgments.jsonl` 后用于计算回答准确率与核心信息呈现率 |

## 预期产出与预检结论

脚本执行完成后，将在输出目录下生成：

- `out/report.json`：机器复核用完整预检报告（含全部 18 条判定规则与输入文件 SHA-256）
- `out/report.html`：自包含、可打印的中文报告（含合成数据水印、利益关系披露栏与人工签字栏）
- `out/observations.csv`：UTF-8 BOM、防公式注入的原始记录索引
- `out/metrics.json`、`out/crosscheck.json`、`out/alerts.json`、`out/evidence-verify.json`、`recheck/plan.json`、`evidence/manifest-0001.json`

由于演示脚本只生成了抽样复核计划（`recheck/plan.json`）、尚未由人工回填 `recheck-compare`，预检结论默认会是 **`有条件通过`**（在 `R-RECHECK` 项给出保留意见），完整演示了“人工复核未闭环时不得无条件通过”的设计。
