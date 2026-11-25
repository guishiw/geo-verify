# geo-verify

GEO（生成式引擎优化）项目的**验收预检工具**：题库封存、采集导入、提及与位次标注、指标与统计、双通道交叉验证、证据包和报告。

目标是让甲方、乙方、第三方拿同一份原始数据，能独立算出同一个结果，并能查到每个数字对应的原始回答。

> **重要声明**
>
> - 本工具只给出**预检结论**（`通过` / `有条件通过` / `不通过` / `数据不足`），不构成独立第三方验证；报告带利益关系披露栏和人工签字栏，结论需由人签字确认。
> - 本工具不保证任何第三方平台的结果，也不提供代理池、账号池、验证码绕过或反爬规避能力。
> - 测试和默认运行不访问任何真实 AI 平台；真实调用必须显式 `--live`，密钥只从环境变量读取。
> - 仓库内全部样例数据为合成数据（`synthetic: true`），品牌、产品、域名均为虚构，不代表任何真实效果。

## 安装

需要 Python ≥ 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
make setup    # 安装依赖，需联网一次
make check    # 静态检查 + 类型检查 + 测试（可断网）
```

## 快速开始

```bash
# 1. 生成虚构汽车品牌的完整样例项目
.venv/bin/geo-verify init out/auto-demo --example auto

# 2. 走一遍完整流程：题库检查与封存 → 导入 → 标注 → 指标 → 证据包 → 报告
sh examples/auto/run.sh out/auto-demo
```

逐步说明见 [docs/quickstart.md](docs/quickstart.md)，命令和项目目录约定见 [docs/cli.md](docs/cli.md)。

## 命令一览

```text
geo-verify init | validate | import | collect | annotate
geo-verify prompts lint | seal | diff
geo-verify metrics | compare | crosscheck | samplesize | alerts
geo-verify recheck-plan | recheck-compare
geo-verify evidence build | verify
geo-verify report
```

所有命令支持 `--json`。退出码：`0` 成功，`1` 检查未通过，`2` 输入错误，`3` 数据不足，`4` 内部错误。

## 数据契约

契约在 [`spec/v1/`](spec/v1/)（JSON Schema 2020-12），是 `geo-verify` 与 `geo-facts` 之间唯一的数据接口。两个工具只通过文件交换数据，互不依赖对方代码。

## 文档

- [方法学](docs/methodology.md)：指标口径、统计方法、偏离度公式、样本充分性
- [条款对照表](docs/clause-map.md)：功能与团体标准条款号的对应关系（只引用条款号）
- [快速开始](docs/quickstart.md)
- [命令与目录约定](docs/cli.md)
- [证据与哈希的边界](docs/evidence.md)

## 许可证

[Apache-2.0](LICENSE)
