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
- `--live` 不能与 `--now` 同时使用（退出码 2）：真实调用的时间只取本机时钟，不得指定（R8）。

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

## `--adapter share`：已保存的分享数据导入

```text
collect PROJECT --adapter share --input FILE --platform ID --window W --query QUERY_ID
        [--attempt N] [--entry-form web|app|mini_program|plugin|other] [--operator NAME] [--now T]
```

只解析使用者提供的**本地文件**：不联网，不访问文件里的任何链接，不执行脚本，不加载外部资源。
一次导入一个文件，对应封存题库中 `--query` 指明的题目、同一窗口内的第 `--attempt` 次（默认 1）。
平台须在 `project.json` 中登记 `share_import` 通道（浏览器辅助插件导出的文件为 `browser_assist`，见下文）。`--repeats`、`--queries` 不适用，给出时报错。

### 文件格式

按扩展名判断（`.json`；`.html` `.htm` `.xhtml`），没有可识别的扩展名时按内容判断（以 `{` 或 `[` 开头为 JSON，
以 `<` 开头为 HTML）。不支持 MHTML（`.mhtml` `.mht`），请另存为“网页，仅 HTML”或导出 JSON。

**JSON**（UTF-8）：

```json
{"messages": [{"role": "user", "content": "…"}, {"role": "assistant", "content": "…"}],
 "model": "…", "citations": ["https://…", {"url": "https://…", "title": "…"}],
 "url": "分享页地址", "saved_at": "2026-10-08T10:00:00+08:00"}
```

兼容的别名（取第一个出现的）：

| 含义 | 字段名 |
|---|---|
| 消息列表 | `messages` `conversation` `chat_messages`；顶层也可以直接是消息列表 |
| 角色 | `role` `author` `sender`（值可以是 `{"role": …}`）；`user` `human` 为提问，`assistant` `ai` `bot` `model` 为回答，其他角色（如 system、tool）忽略 |
| 内容 | `content` `text`；可以是文本、内容片段列表（文本或 `{"text": …}`，其他片段如图片忽略）、`{"parts": [...]}` 或 `{"text": …}` |
| 模型 | `model` `model_slug` `model_version`；回答消息上的优先于顶层 |
| 引用 | `citations` `sources` `references`；回答消息上的优先于顶层。元素为 URL 文本，或含 `url` / `link` / `href`、`title` / `name`、可选 `placement` 的对象 |
| 分享页地址 | `url` `share_url` `link`（只记录，不访问） |
| 保存时间 | `saved_at` `exported_at` `created_at` `create_time`；带时区的 ISO 8601 文本或 Unix 秒 |

**HTML**（尽力而为的解析）：用标准库 `html.parser` 读取，规则如下。

- 消息：带角色标记的元素，即属性 `data-message-author-role`、`data-role`、`data-author` 或 `data-sender`
  的值为上表中的提问或回答角色。嵌套在另一条消息里的角色标记不再切分。没有任何角色标记的页面无法导入。
- 可见正文：跳过 `script` `style` `noscript` `template` `head` `svg` `button` `select` `textarea`
  `iframe` `object` `canvas` 元素，带 `hidden` 属性、`aria-hidden="true"`、或内联样式含
  `display:none` / `visibility:hidden` 的元素（及其全部子元素），以及注释。块级元素和 `<br>` 处换行；
  每行内连续空白合并为一个空格，空行去掉。消息元素之外的文字（导航、页脚等）不计入。
- 引用：回答消息内可见的 `<a href>`，只收 `http://`、`https://` 链接（其他如 `javascript:`、相对链接跳过并计数）。
  链接文字作标题，没有文字时取 `title` 属性。位于来源区——`data-role` / `data-section` 为
  `sources` `citations` `references` 的元素，或 `<aside>`——的链接为 `source_list`，其余为 `inline`；
  来源区里的文字不计入回答正文。重复链接原样保留。
- 模型：回答元素的 `data-model` 或 `data-message-model-slug` 属性。
- 分享页地址：`<link rel="canonical">`，其次 `<meta property="og:url">`。保存时间：`<meta name="saved_at">`（或 `saved-at`）。
- 编码：先按 UTF-8，失败时按 `<meta charset>` 声明的编码。
- 不处理浏览器的隐式闭合规则、CSS 类样式和脚本生成的内容；复杂页面建议导出 JSON 再导入。

### 浏览器辅助插件导出的文件（`geo-verify-capture/1`）

JSON 顶层 `format` 为 `geo-verify-capture/1` 的文件（格式见 [extension/FORMAT.md](../extension/FORMAT.md)）按插件的固定格式读取，
与上面的通用 JSON 有以下不同：

- 观测的 `channel = browser_assist`（平台须在 `project.json` 中登记该通道，`trace_id` 也按此通道生成）。
- 结构按 FORMAT.md 严格检查，不符合即为输入错误（退出 2）：`capture` 须为 `browser_assist`；`messages` 恰好两条、先 `user` 后 `assistant`；
  `environment` 为对象，`entry_form` 不能为 `api`，`logged_in` / `history_cleared` / `web_search` 只能是 `true`、`false` 或 `null`；
  其余各项为文本或 `null`。`format` 是 `geo-verify-capture/` 的其他版本时拒绝导入。
- `entry_form`、`entry_features`、`logged_in`、`history_cleared`、`web_search`、`region`、`device` 取文件中的 `environment`；
  `model` → `model_version`，`operator` → `operator`，`sources` → `citations`（位置按上面的通用规则判断：插件只保存选区内的链接，
  分不清正文引用与来源列表）。文件里为 `null`（采集人未填写）的项保持 `null`，`null_reasons` 写“浏览器辅助采集：采集人未填写…”；
  没有入口特征时 `entry_features` 写明“采集人未填写入口特征”。
- 时间：`saved_at` 是**用户点击保存的时间**，如实记为 `started_at = ended_at`，`ext.share.time_source = "saved_at"`，
  `ext.share.time_note = "时间为用户点击保存的时间，非提问时间"`。观测契约的 `null_reasons` 不允许 `started_at` 键，
  所以这条说明只写在 `ext.share`。`saved_at` 无法使用时同通用规则，改用导入时间并给出警告。
- 文件里手工填写的 `platform`、`window_id`、`query_id` 与命令行 `--platform`、`--window`、`--query` 不一致时**以命令行为准**，
  每一处给一条警告；文件里没填（`null`）不算不一致。`--entry-form`、`--operator` 显式给出且与文件不同时同样以命令行为准并警告。
- `ext.share.format = "geo-verify-capture/1"`，`ext.share.share_url = null`，另有
  `ext.share.capture = {page_url, page_title, file_ids: {platform, window_id, query_id}, mismatched: [不一致的键]}`
  （`page_url` 是读取时的页面地址，只记录，不访问）。`--json` 输出的 `channel` 为 `browser_assist`、`format` 为
  `geo-verify-capture/1`，另有 `page_url`。审计事件的 `details.channel`、`details.format` 同上，`actor` 缺省取文件中的 `operator`。

### 写成观测

- 取最后一组“用户提问 + 助手回答”：最后一条回答，配它之前最近的一条提问。之前还有对话消息时，
  `ext.share.prior_messages` 记条数，`null_reasons.history_cleared` 和警告里都会提示回答可能受前文影响。
- `prompt_text` 是**文件中的实际提问原文**。它与封存题目原文不一致（忽略空白差异后比较）时照样导入，
  输出 `prompt_text_differs: true`，警告排在第一条（文本输出的第一行），`ext.share.prompt_matches_sealed = false`。
- `channel = share_import`，`entry_form` 默认 `web`（`--entry-form` 可改，不能为 `api`）。
- 是否登录、是否清除历史、是否联网、地域、设备一律 `null` 并写原因；文件没有模型版本时 `model_version` 为 `null`，
  `entry_features` 注明。
- 回答为空 → 无效观测 `empty_answer`；乱码（同 openai 的判定）→ `garbled`；否则有效。
- 引用位置：文件明确给出的 `placement` 为准；否则正文出现 `[n]`（n 为该条在文件引用列表中的序号）或该链接时为
  `inline`，否则为 `source_list`。HTML 按上面的来源区规则。
- 时间：`started_at = ended_at = 文件中的保存时间`，`ext.share.time_source = "saved_at"`，`time_field` 记字段名
  （HTML 为 `meta:saved_at`）。文件里没有、或时间缺少时区、格式不对时，取导入时间（`--now`，没给则当前时间），
  `time_source = "import_time"`，`ext.share.time_note` 写明“时间为导入时间，非采集时间”，并给出警告。
- 原文件原样复制为 `snapshots/<observation_id>/<文件名>` 并记录 SHA-256；已有同名同内容文件时复用，内容不同时改用
  `<文件名主干>.2<扩展名>`，任何已有文件都不覆盖。
- `ext.share`：`format`、`source_file`（文件名）、`source_sha256`、`share_url`、`time_source`、`time_field`、`time_note`、
  `prompt_matches_sealed`、`prior_messages`，有跳过的引用条目时另有 `citations_skipped`。

### 去重

- `trace_id` 由（项目、题库哈希、平台、通道 `share_import`、窗口、题目、次数）确定生成。
- 同一文件以同一任务再次导入：跳过（`skipped_existing = 1`），退出 0，不写审计事件。
- 同一任务已有来自另一个文件的记录：拒绝（退出 2），提示用新的 `--attempt`；观测写入后不可覆盖。
- 同一文件（按 SHA-256）已导入为别的任务：拒绝（退出 2），避免重复计数。

### 输出与审计

`--json` 输出：`adapter "share"`、`channel "share_import"`、`input`（文件名）、`format`、`platform`、`window_id`、
`query_id`、`attempt`、`prompt_set_version`、`prompt_set_hash`、`trace_id`、`observation_id`、`written`、
`skipped_existing`、`status`、`invalid_reason`、`audit_event_id`、`warnings`；写入时另有 `entry_form`、
`prompt_text_differs`、`time_source`、`citations`（条数）、`share_url`、`prior_messages`、`snapshot`（快照相对路径）。

写入后追加一条 `action = import` 的审计事件：`target = {type: "observations", id: <文件名>}`，`actor` 取 `--operator`，
缺省为“geo-verify collect（未指明操作人）”，时间取 `--now`（没给则当前时间）；`details` 含 `adapter`、`channel`、
`source_file`、`source_sha256`、`format`、`platform`、`window_id`、`query_id`、`attempt`、`observation_id`、`status`、
`prompt_text_differs`、`time_source`。

本适配器不提供浏览器自动化，也不抓取分享链接。
