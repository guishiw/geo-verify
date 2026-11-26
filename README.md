# geo-verify

GEO（生成式引擎优化）项目的**可复核验收预检工具**（裁判侧）：题库封存、多通道采集导入、提及与位次标注、指标与统计区间、双通道交叉验证、异常识别、抽样复核、不可篡改证据包与预检报告。

目标很明确：**裁判与运动员分离**。本仓库只负责“观测 → 指标 → 证据 → 预检报告”，不依赖 `geo-facts` 或任何私有仓库代码，只通过 [`spec/v1/`](spec/v1/) 的 JSON Schema 文件契约交换数据；甲方、乙方与复核人拿同一份原始数据，能独立算出逐字节一致的结果，并下钻查到每个数字对应的原始回答。

> **重要声明**
>
> - **预检结论不构成独立第三方验证**：本工具只给出 `通过` / `有条件通过` / `不通过` / `数据不足` 四种预检结论；报告强制包含利益关系披露栏（工具操作方与服务方为同一主体时会显式警示）和人工签字栏，结论须由人工复核并签字确认。
> - **不对第三方平台结果作任何承诺**：不输出对排名位置或收录结果的承诺性表述，不实现压制或挤出真实负面的功能，不提供代理池、账号池、验证码绕过、反爬规避或设备指纹伪装。
> - **默认离线、显式 `--live`**：测试和默认命令不访问任何真实 AI 平台；真实 API 调用必须显式传入 `--live`，密钥只从环境变量读取，内置平台配置项默认未核实并会在 `--live` 下拒绝使用，须由人工查阅平台文档核实后启用。
> - **全部样例均为合成数据**：仓库内置样例与测试数据均带 `synthetic: true`，品牌（澜序 / 岑野 / 朔风）、车型、平台与 `*.example` 域名均为虚构，示例 HTML 报告带可见水印，不得作为任何真实效果宣传。

## 安装

需要 Python ≥ 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
make setup    # 安装依赖（仅首次需要联网）
make check    # 静态检查（ruff）+ 严格类型检查（mypy）+ 全部测试与模块覆盖率门槛（全程离线）
```

## 5 分钟样例

仓库内置了虚构汽车品牌（澜序汽车）的完整合成样例，包含 22 道题（覆盖 11 类意图）、3 个时间窗口、双平台双通道的 532 条观测及由 `geo-facts` 产出的事实核验判定样例。在完成 `make setup` 后可直接离线跑通：

```bash
# 一键初始化样例并跑完从题库检查、封存、导入、标注、指标、交叉验证、告警、复核计划、证据包到报告的全流程
sh examples/auto/run.sh out/auto-demo
```

不传目录参数时脚本会在临时目录中运行。执行完成后可打开 `out/auto-demo/out/report.html` 查看带合成数据水印的中文报告。

如需初始化自己的项目骨架：

```bash
.venv/bin/geo-verify init path/to/my-project --project-id my-project
```

逐步上手与替换自有数据指南见 [docs/quickstart.md](docs/quickstart.md)，样例详情见 [examples/auto/README.md](examples/auto/README.md)。

## 命令一览

```text
geo-verify init | validate | import | collect | annotate | exclude
geo-verify prompts lint | seal | diff
geo-verify metrics | compare | crosscheck | samplesize | alerts
geo-verify recheck-plan | recheck-compare
geo-verify evidence build | verify
geo-verify report
```

- 所有叶子命令支持 `--json` 输出机器可读结果。
- 统一退出码：`0` 成功，`1` 检查未通过，`2` 输入错误，`3` 数据不足，`4` 内部错误。
- 原始记录（`observations.jsonl`、`annotations.jsonl`、`audit.jsonl`）只追加，不覆盖、不静默删除；剔除必须通过 `exclude` 写入审计事件。

## 数据契约

[`spec/v1/`](spec/v1/) 包含 11 个对象的 JSON Schema（Draft 2020-12）与合法/非法样例，是 `geo-verify` 与 `geo-facts` 之间唯一的数据接口：

- `project`、`prompt_set`、`entity`、`observation`、`annotation`、`fact`、`claim`、`judgment`、`metric_result`、`manifest`、`audit_event`。
- 均要求 `schema_version`，默认拒绝未知字段（扩展仅允许写入 `ext`）；拿不到的字段须填 `null` 并写明 `null_reasons`，禁止编造默认值。

## 文档索引

- [快速开始](docs/quickstart.md)：5 分钟上手、每步说明、退出码与真实平台 `--live` 注意事项
- [方法学与统计口径](docs/methodology.md)：有效观测、全部指标口径、Wilson / 按题聚类 bootstrap / ICC / 样本量公式、交叉验证公式、标注与剔除审计、告警规则、预检结论规则表与已知限制
- [条款对照表](docs/clause-map.md)：功能与团体标准条款号的对应关系（仅引用条款号，不复制正文，须由人工复核）
- [命令与目录约定](docs/cli.md)：完整命令参数、项目目录结构与指标编号
- [结果文件格式](docs/outputs.md)：`out/` 与 `recheck/` 下全部 JSON 结果文件结构
- [采集适配器说明](docs/collect.md)：OpenAI 兼容接口（模拟 ASGI 与 `--live`）、人工采集包、本地分享数据与插件导入
- [抽样复核说明](docs/recheck.md)：固定 seed 抽样规则、复核表与比对口径
- [证据包与哈希边界](docs/evidence.md)：清单链、逐行哈希索引、复核步骤与哈希的证明边界
- [虚构汽车品牌样例说明](examples/auto/README.md)
- [浏览器辅助插件（MV3）](extension/README.md)：用户主动点击保存所选内容与链接（不自动提问、不自动翻页）

## 参与贡献与安全

- 贡献流程与项目边界见 [CONTRIBUTING.md](CONTRIBUTING.md)
- 漏洞报告与安全边界见 [SECURITY.md](SECURITY.md)
- 版本变更记录见 [CHANGELOG.md](CHANGELOG.md)

## 许可证

[Apache-2.0](LICENSE)
