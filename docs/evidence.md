# 证据包：清单链、逐行索引与校验

本文说明 `geo-verify evidence build | verify` 生成和校验的证据包：包含什么、清单链如何工作、第三方如何复核，
以及哈希能证明什么、不能证明什么。命令参数见 [cli.md](cli.md)，校验结果格式见 [outputs.md](outputs.md)，
清单的数据契约见 [`spec/v1/manifest.schema.json`](../spec/v1/manifest.schema.json)。

## 证明边界（先读）

- **哈希只能证明“未改动”，不能证明“采集时间真实”。** 清单证明的是：自清单生成以来，清单里登记的文件没有被改动、删除、
  插入或重排。它不能证明观测是在记录的时间采集的，也不能证明回答原文就是平台当时的输出。
- **可信时间戳仅提供接口。** 清单的 `timestamp_token` 是接口位，本工具未接入任何时间戳服务，默认写 `null`。
  要证明“某时刻之前清单已存在”，需要另行接入可信时间戳服务，并由第三方核验令牌；`evidence verify` 不核验令牌。
- **清单链需要一个外部锚点。** 链上每份清单都指向前一份的哈希，能发现中间任何一份被改、被删、被插入；
  但如果有人重做全部数据、全部清单和审计日志，链本身无法识别。因此每次生成清单后，应把最新的 `manifest_hash`
  交给委托方或第三方留存（邮件、工单、签字页均可），复核时与留存值比对。
- 本工具只给预检结论，不构成独立第三方验证。

## 证据包包含什么

证据包就是项目目录本身（见 [cli.md 的项目目录](cli.md#项目目录)）。清单覆盖下列文件，**存在的才列入**：

| 路径 | 清单角色 `role` | 说明 |
|---|---|---|
| `project.json` | `config` | 口径：预先登记的指标、口径版本、阈值、seed、剔除规则 |
| `entities.jsonl` | `raw` | 实体、别名、排除词、自有域名；生成逐行索引，只能在末尾追加 |
| `observations.jsonl` | `raw` | 观测原始记录；生成逐行索引，只能在末尾追加 |
| `annotations.jsonl` | `raw` | 标注（含全部历史）；生成逐行索引，只能在末尾追加 |
| `judgments.jsonl` | `raw` | 事实核验判定；生成逐行索引，只能在末尾追加 |
| `audit.jsonl` | `raw` | 审计事件哈希链；生成逐行索引，只能在末尾追加 |
| `prompts/sealed/*`、`prompts/diffs/*` | `raw` | 封存题库与版本差异，只增不改 |
| `snapshots/**` | `snapshot` | 快照（递归），只增不改 |
| `recheck/*` | `derived` | 抽样复核计划与结果 |
| `uv.lock` 或 `evidence/lock/uv.lock` | `lock` | 依赖锁，见下 |
| `evidence/index/*` | `index` | 逐行哈希索引 |

不列入清单的：`out/`（生成物，可由原始数据复算）、`prompts/draft.jsonl`（草稿）、`evidence/manifest-*.json`
（清单本身，由清单链校验）、系统生成的元数据文件（`.DS_Store`、`Thumbs.db`、`desktop.ini`、`._*`）。
上表目录中不带 `**` 的只看目录下一层的文件。

清单还记录：

| 字段 | 内容 |
|---|---|
| `tool` | `{"name": "geo-verify", "version": 包版本}` |
| `code_version` | 包版本；从源码目录运行且取得到 git commit 时为 `0.1.0+git.<commit>`，工作区有未提交改动（或无法确认是否干净）时再加 `.dirty`；取不到 commit 时只写包版本 |
| `lock_hash` | 依赖锁的 SHA-256。先找 `PROJECT/uv.lock`；没有时按本包安装元数据（PEP 610 的 `direct_url.json` 指向的本地目录）找 `uv.lock`，找到后**复制到 `evidence/lock/uv.lock`**，使证据包自带依赖锁；都找不到时为 `null`，命令结果的 `lock.note` 写明原因 |
| `seed` | `project.json` 中预先登记的 `preregistration.statistics.seed` |
| `definitions` | 预先登记的 `metric_id → definition_version` |
| `timestamp_token` | 可信时间戳接口位，默认 `null` |

## 逐行哈希索引

每个上表中标明“生成逐行索引”的 JSONL 都有一个索引文件 `evidence/index/<文件名>.lines`：

- 源文件按 `\n` 切行，行内容不含结尾的 `\n`（文件末尾没有换行时，最后一段也算一行）；
- 对每行内容的**原始字节**取 SHA-256，按行写入索引：每行一个 64 位小写十六进制串，以 `\n` 结尾；
- 清单里源文件条目的 `records` 是行数，`line_index` 指向索引文件；索引文件自身也列入清单（`role = index`）。

索引用于在文件被改动后**定位到行**。行号一律从 1 起：`line_deleted` 用清单记录时的行号，其余用当前文件的行号。
定位规则（只依赖两份行哈希序列，结果确定）：

1. 去掉两份序列相同的前缀和后缀；
2. 中间段按哈希配对（相同内容第 k 次出现配第 k 次出现）；
3. 配上对的行，若在共同行中的先后名次变了，记 `line_reordered`（交换两行时两行都会报出）；
4. 没配上的行，按其前面有几行共同行分组：同组内旧行与新行依次配成 `line_modified`，多出的旧行记 `line_deleted`，
   多出的新行记 `line_inserted`（末尾追加也是 `line_inserted`）。

完全相同的行无法互相区分：删除连续相同行中的一行时，报出的行号可能是这几行中的另一行。

**索引只延长、不改写。** 原始 JSONL 只是在末尾追加时，下一次构建沿用并延长原来的索引文件（旧内容是新内容的字节前缀）；
发现历史行被改写或删除时，旧索引保留不动，另起一代 `evidence/index/<文件名>.<新清单序号>.lines`。
因此任何一份历史清单生成时的索引，都可以取该清单登记的字节数前缀并核对哈希重新得到。

## 生成清单：`evidence build`

1. 读取最新清单（序号最大的 `evidence/manifest-NNNN.json`），核对契约、文件名序号、`project_id` 和 `manifest_hash`；
   任何一项对不上都**拒绝续链**（退出 1），先用 `evidence verify` 查明原因。
2. 准备依赖锁，按覆盖范围在内存中算出候选文件清单（此时不写任何文件）。
3. **无变化不生成**：候选的文件列表（路径、哈希、字节数、角色、行数、索引路径）、工具与代码版本、依赖锁、seed、口径版本
   都与最新清单相同时，不生成新清单、不写审计事件，提示“无变化”，退出 0。只换 `--now` 不算变化。
4. 有变化时，**先**追加审计事件 `build_evidence`（`target = {type: "manifest", id: manifest_id}`，
   `details = {sequence, manifest, previous_manifest_hash}`，`before_hash` 为前一清单哈希，`after_hash` 为 `null`），
   **再**重新计算文件清单——这次的 `audit.jsonl` 已含该事件——然后写行索引和清单。
   这样清单覆盖的是包含本次事件的审计日志，生成后立即校验仍然通过；下一次构建时这条事件已在清单里，不会被当成变化。
5. `manifest_hash` 的计算：先对去掉 `timestamp_token` 和 `manifest_hash` 的清单内容做规范化 JSON（RFC 8785）+ SHA-256，
   得到内容摘要，交给时间戳接口（默认实现返回 `null`）；写入令牌后，再对除 `manifest_hash` 以外的全部字段计算 `manifest_hash`。
6. 构建时若发现相邻两份清单之间违反只追加约束（见下），清单照常生成，结果的 `append_only_violations` 逐条列出，退出 1；
   这处违规已写进清单链，此后每次 `evidence verify` 都会报告，不能靠重新构建抹掉。

清单文件名 `evidence/manifest-NNNN.json`，按 `jsonl.write_json` 写出（键排序、缩进 2），不含绝对路径；
同一输入、同一 `--now` 生成的清单逐字节一致（代码版本取自当前进程，同一份代码下一致）。

## 清单链与只追加约束

- 第 1 份清单的 `previous_manifest_hash` 为 `null`，此后每份指向前一份的 `manifest_hash`，`sequence` 与文件名序号一致、从 1 连续。
- 每份清单生成前都写了一条 `build_evidence` 审计事件。校验时两者互相核对：审计日志记录过、但清单文件不存在的序号
  （被删除的最新清单），以及没有生成记录的清单（插入或伪造的清单），都会报出。
- **只追加约束**（R6，047 §14）：相邻两份清单之间，
  - 原始 JSONL 旧的行索引必须是新行索引的前缀，否则报 `append_only_violated`，定位到第一处不符的行；
  - 其他原始文件（封存题库、差异记录）和快照不得改动；
  - 原始文件和快照不得从后一份清单中消失。
- 剔除观测只通过审计事件 `exclude` 表达（规则、数量、观测编号），`observations.jsonl` 不会被改动；
  更正同样通过新增审计事件和新版本记录表达。因此正常流程中原始文件只会在末尾增长。

## 校验：`evidence verify`

以**最新的可读清单**为准，依次检查：

1. 清单链：每份清单的契约、`project_id`、文件名序号、`manifest_hash`；序号连续；`previous_manifest_hash` 相符；
   与审计日志中的 `build_evidence` 事件一一对应。
2. 文件：逐个核对哈希。缺失记 `file_missing`；哈希不符记 `file_modified`，JSONL 再借助行索引定位到行；
   覆盖范围内清单外的文件记 `file_added`。行索引本身缺失或被改动时，源文件只能报到文件级。
3. 相邻清单之间的只追加约束。
4. 审计链（逐条事件的哈希、前后链接、编号）。
5. 封存题库（契约、内容哈希、版本连续、版本链）。

没有任何清单时报 `no_manifest`。有任何问题退出 1，否则退出 0。结果写入 `out/evidence-verify.json`，
每个问题含 `file`（相对项目目录的路径）、`kind`、`line`（1 起，不适用时为 `null`）、`message`，按文件、行号排序：

| `kind` | 含义 |
|---|---|
| `no_manifest` | 没有任何清单（`file` 为 `evidence`） |
| `manifest_invalid` | 清单不是合法 JSON、不合契约、文件名不合约定、属于别的项目，或登记的行索引与源文件对不上 |
| `manifest_modified` | 清单内容与其 `manifest_hash` 不符 |
| `manifest_chain_broken` | 缺少某份清单、序号重复或与文件名不符、前一清单哈希不符、与 `build_evidence` 审计事件对不上 |
| `file_missing` / `file_modified` / `file_added` | 文件缺失 / 被改动 / 清单外新增 |
| `line_modified` / `line_deleted` / `line_inserted` / `line_reordered` | 行被改动 / 被删除 / 被插入（含末尾追加）/ 先后次序变化 |
| `append_only_violated` | 相邻两份清单之间历史行被改写或删除、只增不改的文件被改动或消失，或旧行索引取不到而无法核对 |
| `audit_chain` | 审计事件被改动、链接断裂或编号不符（`line` 为事件序号，即 `audit.jsonl` 的行号） |
| `sealed_prompt_set` | 封存题库被改动或版本链断裂 |

清单生成之后又正常追加了观测、标注或审计事件时，校验会报出 `line_inserted`：这表示“当前数据比最新清单多”，
重新执行 `evidence build` 生成新清单后即可通过。

## 如何复核

1. 把整个项目目录复制到复核方自己的目录（路径随意；结果不含绝对路径，换目录不影响校验）。
2. 执行 `geo-verify evidence verify PROJECT --json`，确认 `status` 为 `pass`。
3. 把 `latest_manifest.manifest_hash` 与委托方留存的最新清单哈希比对——这是清单链唯一的外部锚点。
4. 需要不依赖本工具时，可以手工复核：
   - 对清单 `files[]` 中每个文件计算 SHA-256（如 `shasum -a 256 <文件>`），与 `sha256` 比对；
   - 对 JSONL 逐行计算 SHA-256（不含行尾 `\n`），与 `line_index` 指向的索引文件逐行比对；
   - 对清单去掉 `manifest_hash` 后做 RFC 8785 规范化再取 SHA-256，与 `manifest_hash` 比对；逐份核对 `previous_manifest_hash`。
5. 在复核方目录重新计算指标（`geo-verify metrics`），与交付的 `out/metrics.json` 逐字节比对。
