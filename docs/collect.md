# 采集适配器说明

本文补充 [`cli.md`](cli.md) 中 `collect` 一节，说明各适配器的具体规则。所有适配器都只追加写入
`observations.jsonl`，产出的每条记录都先通过 observation 契约校验；拿不到的字段一律为 `null`
并在 `null_reasons` 写明原因，不编造默认值。

## `--adapter openai`：通用 OpenAI 兼容接口（API 通道）

```text
collect PROJECT --adapter openai --platform ID --window W
        [--repeats N] [--queries Q1,Q2] [--model NAME]
        [--asgi-app <模块或文件>:<对象>] [--base-url URL] [--web-search true|false]
        [--timeout 秒] [--max-retries N] [--retry-wait 秒] [--resume]
        [--operator NAME] [--now T]
        [--platform-config 名称 [--platforms-file FILE]]
        [--live --base-url URL --api-key-env 变量名 --model NAME]
```

### 连接方式

- **默认只连接进程内的模拟服务**：`--asgi-app mock_engines.app:app`（模块写法）或
  `--asgi-app path/to/app.py:app`（文件写法），经 `httpx.ASGITransport` 调用，不占端口、不联网。
  `--base-url` 默认 `http://mock.invalid/v1`，可改成 `http://mock.invalid/engine-a/v1` 之类；
  连接 ASGI 应用时只用到其路径。
- 没有 `--asgi-app` 又没有 `--live` 时拒绝运行（退出 2）。
- **真实调用**必须显式 `--live`，并同时给出 `--base-url`、`--api-key-env` 和 `--model`
  （或由已核实的平台配置提供 model）。`--live` 与 `--asgi-app` 不能同时使用。
  `--base-url` 只接受 http(s)，不得带用户名密码、查询参数或片段。
- 密钥只从 `--api-key-env` 指定的环境变量读取，只放进 `Authorization: Bearer …` 请求头；
  不写入观测、快照、审计事件、命令输出和报错信息。响应里若回显了密钥，保存前替换为
  `[REDACTED]`，并在观测的 `ext.api.secret_redacted` 记为 `true`。
- 不读取代理等环境配置，不跟随重定向，不做 429 以外的任何重试；请求逐条顺序发出。

### 请求

每个任务（题目 × 第几次）发一次：

```text
POST {base_url}/chat/completions
X-Request-Id: <trace_id>
{"model": <--model>, "messages": [{"role": "user", "content": <封存题目原文>}]}
```

没有给 `--model`（也没有已核实平台配置提供的 model）时，请求体里不带 `model` 字段。
使用已核实的平台配置且 `--web-search true` 时，配置中的 `web_search_params` 并入请求体。

### 响应解析

| 观测字段 | 取值 |
|---|---|
| `answer_text` | `choices[0].message.content`；内容片段列表时拼接其中 `type = text` 的文本 |
| `model_version` | 响应顶层的 `model`；没有时为 `null`，`entry_features` 与 `null_reasons.model_version` 写明 |
| `entry_features` | “API 通道：OpenAI 兼容 chat/completions 接口；请求模型：…”，必要时注明响应缺少 model |
| `citations` | 见下 |
| `channel` / `entry_form` | 固定 `api` / `api` |
| `web_search` | 取自 `--web-search`；不给为 `null` 并写原因 |
| `logged_in` `history_cleared` `region` `device` | API 通道拿不到，一律 `null` 并写原因 |
| `operator` | `--operator`；不给为 `null` 并写原因 |
| `snapshots` | 原始响应体，见下 |
| `ext.api` | `transport`（asgi / live）、`endpoint`、`request_model`、`http_status`、`finish_reason`、`rate_limit_retries`；无效时另有 `detail`；有无法解析的引用条目时有 `citations_skipped`；用了平台配置时有 `platform_config` |

引用（按出现顺序编号，**重复引用原样保留**，去重是指标层的事）：

- `choices[0].message.annotations[]` 中 `type = url_citation` 的 `url_citation{url, title}`：`placement = inline`。
- 顶层 `citations`（URL 字符串列表，或 `{url, title}` 对象列表）：正文中出现第 n 条对应的 `[n]`
  标记、或直接出现该链接时为 `inline`，否则为 `source_list`。
- `domain` 从 URL 解析，解析不出为 `null`；空白标题记为 `null`。缺少链接或类型不对的条目跳过并计入
  `ext.api.citations_skipped`。

### 故障与无效观测

故障一律写成无效观测（`status = invalid` + `invalid_reason`），**照样写入**，供计数，不进入指标分母。

| 情形 | `invalid_reason` |
|---|---|
| 客户端超时（`--timeout`，默认 60 秒），或 HTTP 408、504 | `timeout` |
| HTTP 429，按 `--max-retries`（默认 2）次、每次等待 `--retry-wait` 秒（默认 5）重试后仍为 429 | `rate_limited` |
| 回答为空或只有空白 | `empty_answer` |
| `finish_reason = length` | `truncated`（截断的文本照原样保存在 `answer_text`） |
| 去掉空白后，U+FFFD 与控制字符（Unicode 类别 Cc、Cs）占比 ≥ 20% | `garbled`（文本照原样保存） |
| 其他 HTTP 状态（含 3xx、4xx、5xx）、响应为空、非法 JSON、缺少 `choices[0].message`、连接失败、响应体超过 16 MiB | `error` |

乱码阈值 20% 是常量 `GARBLED_RATIO`：正常回答里几乎不会出现这类字符，偶发一两个替换字符
不至于让整段回答作废，编码错误或二进制内容则会让它们成片出现。

### 快照

有响应体时，原始响应体原样存为 `snapshots/<observation_id>.json`（能解析为 JSON 时）或
`snapshots/<observation_id>.raw`（不是 JSON 时，`media_type` 取响应的 Content-Type），并记录 SHA-256。
没有响应体（超时、连接失败、空响应体、超过上限）时 `snapshots` 为空，`null_reasons.snapshots` 写明原因。
已有同名文件且内容相同（上次运行中断前已写入）时直接复用；内容不同时改用 `<observation_id>.2.json` 等名字，
任何已有文件都不覆盖。

### 时间

- 给了 `--now T`：第 i 个任务（按计划顺序从 0 起数，含被跳过的任务）的 `started_at = ended_at = T + i 秒`，
  审计事件时间为 `T + 任务总数 秒`。同输入同 `--now` 时，观测、快照、审计事件逐字节一致。
- 没给 `--now`：用真实时钟（秒级）。

### 续跑与重问

- `trace_id` / `observation_id` 由（项目、题库哈希、平台、通道 `api`、窗口、题目、次数）确定生成。
  重跑同一命令（加不加 `--resume` 都一样）时，已有记录的任务——**无论有效还是无效**——直接跳过、不再请求，
  计入 `skipped_existing`，因此可以续跑且不重复计数。中途被打断（如 Ctrl-C）时，已写入的观测保留，
  并照样写审计事件（`details.interrupted = true`）。
- **没有“重试无效观测”的选项**：无效观测是原始记录，不能被覆盖。需要重问时，增大 `--repeats`，
  以新的次数序号采集（例如原来 `--repeats 10`，改为 `--repeats 12` 会补问第 11、12 次）。

### 平台配置项

随包分发的 `geo_verify/collect/platforms.toml` 列出 DeepSeek、Kimi、元宝、豆包、文心、千问六个条目
（名称 `deepseek` `kimi` `yuanbao` `doubao` `wenxin` `qianwen`），接口地址、模型名、联网参数全部留空，
标 `verified = false`、`status = "未核实"`：这些取值须由人工查阅该平台文档后填写，并记录来源链接
（`source_url`）与核对日期（`checked_at`）。

- `--platform-config 名称` 选用一个条目，`--platforms-file FILE` 改读自备的同格式文件（建议复制随包文件修改）。
- 未核实的条目在 `--live` 下一律拒绝（退出 2）；在连接 ASGI 模拟服务时可以选用，但其中的取值不被使用。
- 已核实的条目提供 `model` 的默认值和 `--web-search true` 时并入请求体的 `web_search_params`；
  命令行参数优先。`--live` 时 `--base-url` 仍须显式给出，与配置不一致时给出警告。
- 本工具不提供任何网页端自动提问能力。

### 输出（`--json`）

```text
adapter "openai", channel "api", transport "asgi"|"live", app (ASGI 标签，只含模块名或文件名) | null,
base_url, model, web_search, platform, window_id, prompt_set_version, prompt_set_hash,
queries, repeats, resume, api_key_env (变量名，不含密钥), platform_config {name, verified, status, source_url, checked_at} | null,
planned, skipped_existing, written, valid, invalid, invalid_by_reason {原因: 条数},
snapshots_written, audit_event_id, warnings []
```

有无效观测时退出码仍为 0（无效观测是正常的采集结果）。

### 审计事件

写入观测后追加一条 `action = collect` 的审计事件：`target = {type: "observations", id: "api:<平台>:<窗口>"}`，
`actor` 取 `--operator`，缺省为“geo-verify collect（未指明操作人）”；`details` 含上面输出中除
`queries`、`repeats`、`resume`、`audit_event_id`、`warnings` 以外的计数与参数（`platform_config` 只记名称），
另加 `interrupted`。没有新写入时不写审计事件。

API 通道的数据只反映接口返回的内容，与网页端、应用端看到的可能不同。
