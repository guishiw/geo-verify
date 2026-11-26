# 快速开始（5 分钟上手）

本文带你用虚构汽车品牌（澜序汽车）的合成样例，离线跑完一次从项目初始化到生成预检报告的完整流程，并说明如何换成你自己的项目数据。

> **先读这三条**
>
> 1. **只给预检结论**：`geo-verify` 输出的是可复核的**预检结论**（`通过` / `有条件通过` / `不通过` / `数据不足`），不构成独立第三方验证。报告自带利益关系披露栏与人工签字栏，结论须由人工复核并签字。
> 2. **全部样例均为合成数据**：内置样例中的品牌、车型、平台与 `*.example` 域名均为虚构，记录带 `synthetic: true`，HTML 报告带可见水印，不得用于真实效果宣传。
> 3. **默认不联网**：测试和默认命令不访问任何真实 AI 平台；只有显式传入 `collect --adapter openai --live` 才会发出外部网络请求，密钥只从环境变量读取，且平台配置项须由人工核实。

---

## 1. 安装依赖

需要 Python ≥ 3.12 与 [uv](https://docs.astral.sh/uv/)。在仓库根目录执行一次：

```bash
make setup    # 安装依赖（仅首次需要联网）
```

安装完成后，`.venv/bin/geo-verify` 即为本仓库的命令行入口（也可以用 `uv run geo-verify`）。

可选：运行一次自检，确认静态检查、类型检查与测试全部通过（全程离线）：

```bash
make check
```

---

## 2. 一键跑通虚构汽车品牌样例

仓库内置了虚构汽车品牌 **澜序汽车**（竞品：岑野、朔风）的完整样例。直接执行演示脚本：

```bash
sh examples/auto/run.sh out/auto-demo
```

不传目录参数时，脚本会自动创建一个临时目录运行。几秒钟后你会看到：

- 终端输出每一步的结果与最终预检结论（默认是 **`有条件通过`**，原因见下文）
- `out/auto-demo/out/report.html`：用浏览器打开即可查看或打印完整中文报告（带合成数据水印、判定规则表、指标区间、交叉验证、证据校验、利益关系披露与签字栏）
- `out/auto-demo/out/report.json`：机器复核用的完整结构化报告
- `out/auto-demo/out/observations.csv`：UTF-8 BOM、防公式注入的原始记录索引

---

## 3. 每一步在做什么

`examples/auto/run.sh` 依次执行以下命令，你也可以手动逐条运行并观察每一步的输出（所有命令都支持 `--json`）：

```bash
# 1. 初始化样例项目（含 project.json、entities.jsonl、prompts/draft.jsonl 与 inbox/ 待导入文件）
.venv/bin/geo-verify init out/auto-demo --example auto

# 2. 检查草稿题库（重复、近似重复、缺失意图类型、个人信息、诱导性提问）
.venv/bin/geo-verify prompts lint out/auto-demo

# 3. 封存题库（生成 prompts/sealed/v0001.json，并锁定 project.json 的预先登记哈希）
.venv/bin/geo-verify prompts seal out/auto-demo --by "示例封存人（虚构）" --now 2026-10-02T02:00:00Z

# 4. 导入观测（逐行报错、按 trace_id 去重、只追加写入 observations.jsonl）
.venv/bin/geo-verify import out/auto-demo --input out/auto-demo/inbox/observations.csv --now 2026-10-11T01:00:00Z

# 5. 规则标注（识别提及、推荐、否定与明确推荐序列中的位次）
.venv/bin/geo-verify annotate rules out/auto-demo --now 2026-10-11T02:00:00Z

# 6. 放入由 geo-facts 产出的事实核验判定并按契约校验
cp out/auto-demo/inbox/judgments.jsonl out/auto-demo/judgments.jsonl
.venv/bin/geo-verify validate out/auto-demo/judgments.jsonl --type judgment

# 7. 计算全部指标（含 Wilson / 按题聚类 bootstrap 区间、样本充分性、剔除审计与下钻哈希）
.venv/bin/geo-verify metrics out/auto-demo

# 8. 双通道交叉验证（比较 api 与 manual 的偏离度，默认阈值 30%）
.venv/bin/geo-verify crosscheck out/auto-demo

# 9. 异常识别与引用域名变化（线索只表述“相关”，不表述因果）
.venv/bin/geo-verify alerts out/auto-demo

# 10. 生成抽样复核清单（固定 seed 抽 20% 题目，输出 recheck/plan.json 与 recheck/plan.csv）
.venv/bin/geo-verify recheck-plan out/auto-demo --now 2026-10-11T03:00:00Z

# 11. 构建与校验不可篡改证据包（清单链 + JSONL 逐行哈希索引 + 依赖锁）
.venv/bin/geo-verify evidence build out/auto-demo --now 2026-10-11T04:00:00Z
.venv/bin/geo-verify evidence verify out/auto-demo

# 12. 汇总生成预检报告（只读取已有结果文件，不重算指标）
.venv/bin/geo-verify report out/auto-demo --now 2026-10-11T05:00:00Z
```

**为什么样例默认结论是“有条件通过”？**
因为演示脚本只生成了抽样复核计划（`recheck/plan.csv`），还没有模拟人工填表并执行 `recheck-compare`。报告中的 `R-RECHECK` 规则会给出保留意见——这正是工具的预期行为：未完成人工复核时不能无条件通过。如果你想看到完全 `通过`，可以按 `recheck/plan.csv` 填写 `recheck_*` 列，再运行 `recheck-compare`、`evidence build`、`evidence verify` 和 `report`。

---

## 4. 退出码说明

所有命令遵守统一的退出码约定，便于在脚本或流水线中精确处理：

| 退出码 | 含义 | 常见触发场景 |
|---|---|---|
| `0` | 成功 | 检查通过；`report` 结论为 `通过` 或 `有条件通过`；`alerts` 无告警 |
| `1` | 检查未通过 | `prompts lint`/`seal` 有 error；`import` 有非法行；`crosscheck` 超阈值；`alerts` 有告警；`evidence verify` 发现篡改或不一致；`report` 结论为 `不通过` |
| `2` | 输入错误 | 参数非法、文件不存在、目录非空、编码非 UTF-8、JSON/CSV 格式损坏、未核实平台配置用于 `--live` |
| `3` | 数据不足 | `crosscheck` 样本不足（不可判定）；`compare` 样本不足；`alerts` 可比较窗口不足；`report` 结论为 `数据不足` |
| `4` | 内部错误 | 非预期的程序异常（会打印堆栈，不伪装成成功） |

---

## 5. 换成你自己的项目数据

1. **创建项目骨架**（不带 `--example`）：
   ```bash
   .venv/bin/geo-verify init path/to/my-project --project-id my-brand-q4
   ```
2. **填写配置与实体**：
   - 打开 `path/to/my-project/project.json`，把带 `请填写` 或 `replace-` 的占位项替换为你的委托方、服务方、平台、目标主体、竞品、预先登记的验收指标与阈值、利益关系披露。配置中的 `ext.template_notes` 给出了逐项填写说明。
   - 编辑 `entities.jsonl`，按 `spec/v1/entity.schema.json` 填入目标品牌、产品与竞品（含别名、排除词、自有域名）。
   - 编辑 `prompts/draft.jsonl`，按 11 类意图准备评测题目，并用 `prompts lint` 消除重复、个人信息和诱导性提问。
   - 用 `validate` 校验文件是否符合契约：
     ```bash
     .venv/bin/geo-verify validate path/to/my-project/project.json --type project
     .venv/bin/geo-verify validate path/to/my-project/entities.jsonl --type entity
     ```
3. **封存后再采集**：
   - 确认口径不再改动后运行 `prompts seal --by "<封存人>"`。封存后修改 `preregistration` 会被自动检出并导致预检不通过。
   - **导入观测**：用 `geo-verify import --template csv` 导出空白导入模板，填入采集结果后执行 `import`。
   - **人工采集包**：用 `collect --adapter manual --action pack` 生成任务清单，人工回填后用 `--action fill` 导入（通道 `manual`）。
   - **分享文件 / 浏览器辅助插件**：用 `collect --adapter share --input <本地文件>` 导入（离线，不访问外链）。
   - **接入事实核验判定**：如果同时使用 `geo-facts`，将 `geo-facts judge` 产出的 `judgments.jsonl` 放入项目根目录即可计算准确率和核心信息呈现率。

---

## 6. 关于真实 AI 平台调用（`--live`）

默认情况下，`collect --adapter openai` 只连接进程内的模拟 ASGI 服务，**绝不会发起任何外部网络请求**。

如果你需要对真实平台的 OpenAI 兼容接口采集数据：

1. 必须显式传入 `--live --base-url <URL> --api-key-env <环境变量名> --model <模型名>`；
2. 密钥只能通过环境变量提供，工具不会把密钥写入观测、快照、日志或证据包；
3. 如果使用随包的平台配置（`src/geo_verify/collect/platforms.toml`），内置条目默认均为**未核实**状态（地址与模型名留空）。使用前须复制该文件、查阅各平台文档由人工核实并填写来源链接与核对日期，再通过 `--platforms-file` 指定；未核实的配置在 `--live` 下会被直接拒绝（退出码 2）；
4. API 通道采集的数据固定标记为 `channel = api`，报告中会明确提示它只反映接口返回，不等同于网页端或应用端用户的真实所见。

更多细节见：

- [命令与目录约定](cli.md)
- [结果文件格式](outputs.md)
- [采集适配器说明](collect.md)
- [方法学与统计口径](methodology.md)
- [证据包与哈希边界](evidence.md)
