# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号遵循语义化版本。

## [未发布]

### 0.1.0（开发中）

#### 新增

- **项目骨架与工具链**：`Makefile`（`setup` / `lint` / `type` / `test` / `check` / `perf`）、`ruff`、`mypy --strict`、`pytest` + `hypothesis`、按模块行覆盖与分支覆盖 ≥ 90% 门槛检查（`tools/check_coverage.py`）与 GitHub Actions CI（Python 3.12 / 3.13 / 3.14 + Node 20）。
- **数据契约（`spec/v1/`）与校验**：11 个核心对象的 JSON Schema 2020-12 契约及合法/非法样例，`geo-verify validate` 校验命令，RFC 8785 规范化 JSON + SHA-256 哈希，只追加 JSONL 与哈希链审计日志（`audit.jsonl`）。
- **项目初始化与虚构汽车品牌完整样例（`init`、`examples/auto/`）**：
  - `geo-verify init` 支持新建带填写说明的项目骨架，以及 `--example auto` 随包分发的虚构汽车品牌（澜序 / 岑野 / 朔风）完整合成样例（22 题覆盖 11 类意图、3 窗口、双平台双通道 532 条观测及 504 条 `geo-facts` 判定样例）。
  - 固定 seed 样例生成脚本（`tools/generate_auto_example.py`）与一键离线端到端演示脚本（`examples/auto/run.sh`）。
- **题库检查与封存（`prompts lint | seal | diff`）**：检测重复、近似重复、缺失意图类型、个人信息与三类诱导性提问；封存生成不可变版本链与差异记录，并锁定预先登记哈希。
- **多通道采集适配器（`import`、`collect`、`extension/`）**：
  - CSV / JSONL 导入与模板导出（逐行报错、按 `trace_id` 去重、支持续导）；
  - 通用 OpenAI 兼容 API 适配器（默认仅连接进程内 ASGI 模拟服务，真实调用须显式 `--live` 与环境变量密钥，内置 6 个未核实平台占位配置 `platforms.toml`）；
  - 人工采集包生成与回填（`--adapter manual`）；
  - 已保存本地分享文件导入（`--adapter share`，离线解析 JSON / HTML）与浏览器辅助插件（MV3，用户主动点击保存，`channel = browser_assist`）。
- **提及、推荐、否定、位次与双人标注一致性（`annotate`）**：
  - 别名规范化（全半角、大小写、连字符、最长优先、排除词、同形异义消解）、推荐/否定/双重否定/比较对象/承接判断、明确推荐序列位次识别；
  - 人工覆盖（`annotate override`，`supersedes` 链与审计留痕）、批量导入（`annotate import`）与双人标注 Cohen's kappa 及争议项输出（`annotate kappa` → `out/disputes.json`）。
- **指标、统计、基线对比与双通道交叉验证（`metrics`、`compare`、`crosscheck`、`samplesize`、`exclude`）**：
  - 全部 29 项（含分组展开 32 个编号）指标计算、下钻哈希承诺与 `--drilldown` 复核、10 万观测级性能基准（`perf/test_perf.py`）；
  - Wilson 区间、按题聚类 bootstrap、单因素 ANOVA ICC、设计效应与有效样本量、样本量与零失败样本量计算、样本充分性门槛（047 §8.3）；
  - 同题配对基线对比（题库或模型版本变更时拒绝比较）；
  - 双通道交叉验证（本项目定义的对称相对差异偏离度公式、30% 默认阈值、样本不足判不可判定）；
  - `exclude` 只追加剔除命令与反事实选择性剔除审计（单侧超几何检验）。
- **异常识别与引用域名变化（`alerts`）**：五类告警规则（提及率骤降与掉位、错误负面扩散、引用大面积失效、竞品异常提升、同平台口径冲突）及同期引用域名变化相关线索，措辞强制禁止因果与红线词。
- **不可篡改证据包（`evidence build | verify`）**：清单链、JSONL 逐行哈希索引、依赖锁随包保存、只追加约束校验与篡改/删除/重排逐行定位。
- **抽样复核与预检报告（`recheck-plan`、`recheck-compare`、`report`）**：固定 seed 抽样复核清单与比对；18 条固定规则预检结论（`通过` / `有条件通过` / `不通过` / `数据不足`），输出 `report.json`、自包含可打印 `report.html`（合成数据带可见水印、利益关系披露栏与人工签字栏）及防公式注入 UTF-8 BOM `observations.csv`。
- **完整文档**：`README.md`、`docs/quickstart.md`、`docs/methodology.md`、`docs/clause-map.md`、`docs/cli.md`、`docs/outputs.md`、`docs/collect.md`、`docs/recheck.md`、`docs/evidence.md`、`CONTRIBUTING.md`、`SECURITY.md`。

#### 修复

- `collect --adapter openai --live` 不再接受 `--now`：真实调用的观测与审计时间只取本机时钟，防止回填到任意时间窗口（R8）。
