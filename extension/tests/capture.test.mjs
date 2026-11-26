// 纯逻辑测试：导出对象的字段与空值、链接去重、三态解析、拒绝导出的条件、文件名。
// 零依赖，直接运行：node extension/tests/capture.test.mjs
// 期望值按 FORMAT.md 的约定手工写出，不取自程序输出。

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

import { loadAsClassicScript, readExtensionFile } from "./fakes.mjs";

const require = createRequire(import.meta.url);
const capture = require("../lib/capture.js");

const SAVED_AT = new Date("2026-10-08T02:00:00Z");

const TOP_LEVEL_KEYS = [
  "format",
  "capture",
  "saved_at",
  "url",
  "title",
  "platform",
  "window_id",
  "query_id",
  "messages",
  "sources",
  "model",
  "environment",
  "operator",
];

const ENVIRONMENT_KEYS = [
  "entry_form",
  "entry_features",
  "logged_in",
  "history_cleared",
  "web_search",
  "region",
  "device",
];

function samplePage(overrides = {}) {
  return {
    ok: true,
    text: "用户选中的回答文本",
    links: [{ url: "https://auto-review.example/a", title: "链接文字" }],
    url: "https://chat.engine-a.example/share/abc",
    title: "页面标题",
    ...overrides,
  };
}

function sampleForm(overrides = {}) {
  return {
    prompt: "实际提问原文",
    platform: "engine-a",
    window_id: "W1",
    query_id: "Q001",
    model: "",
    entry_features: "网页端默认模式",
    logged_in: "unknown",
    history_cleared: "unknown",
    web_search: "unknown",
    region: "",
    device: "",
    operator: "采集人",
    ...overrides,
  };
}

function expectCaptureError(action, code, messagePart) {
  assert.throws(action, (error) => {
    assert.ok(error instanceof capture.CaptureError, `应抛出 CaptureError，实际为 ${error}`);
    assert.equal(error.code, code);
    assert.ok(error.message.includes(messagePart), `提示应包含“${messagePart}”：${error.message}`);
    return true;
  });
}

test("导出对象与约定的示例逐字段一致", () => {
  const result = capture.buildCapture(samplePage(), sampleForm(), SAVED_AT);
  assert.deepEqual(result, {
    format: "geo-verify-capture/1",
    capture: "browser_assist",
    saved_at: "2026-10-08T02:00:00Z",
    url: "https://chat.engine-a.example/share/abc",
    title: "页面标题",
    platform: "engine-a",
    window_id: "W1",
    query_id: "Q001",
    messages: [
      { role: "user", content: "实际提问原文" },
      { role: "assistant", content: "用户选中的回答文本" },
    ],
    sources: [{ url: "https://auto-review.example/a", title: "链接文字" }],
    model: null,
    environment: {
      entry_form: "web",
      entry_features: "网页端默认模式",
      logged_in: null,
      history_cleared: null,
      web_search: null,
      region: null,
      device: null,
    },
    operator: "采集人",
  });
});

test("字段集合与顺序固定，导出对象不带 synthetic", () => {
  const result = capture.buildCapture(samplePage(), sampleForm(), SAVED_AT);
  assert.deepEqual(Object.keys(result), TOP_LEVEL_KEYS);
  assert.deepEqual(Object.keys(result.environment), ENVIRONMENT_KEYS);
  assert.equal("synthetic" in result, false);
  for (const message of result.messages) {
    assert.deepEqual(Object.keys(message), ["role", "content"]);
  }
  for (const source of result.sources) {
    assert.deepEqual(Object.keys(source), ["url", "title"]);
  }
});

test("未填写的项一律为 null，不编默认值", () => {
  const page = samplePage({ links: [], title: "   " });
  for (const blank of ["", "   ", undefined, null]) {
    const form = {
      prompt: "实际提问原文",
      platform: blank,
      window_id: blank,
      query_id: blank,
      model: blank,
      entry_features: blank,
      logged_in: blank,
      history_cleared: blank,
      web_search: blank,
      region: blank,
      device: blank,
      operator: blank,
    };
    const result = capture.buildCapture(page, form, SAVED_AT);
    assert.equal(result.title, null);
    assert.equal(result.platform, null);
    assert.equal(result.window_id, null);
    assert.equal(result.query_id, null);
    assert.equal(result.model, null);
    assert.equal(result.operator, null);
    assert.deepEqual(result.sources, []);
    assert.deepEqual(result.environment, {
      entry_form: "web",
      entry_features: null,
      logged_in: null,
      history_cleared: null,
      web_search: null,
      region: null,
      device: null,
    });
    // 没有任何键的值是 undefined：经过 JSON 往返后键集合不变。
    assert.deepEqual(Object.keys(JSON.parse(JSON.stringify(result))), TOP_LEVEL_KEYS);
  }
});

test("表单完全缺省时只有提问原文是必填", () => {
  const result = capture.buildCapture(samplePage(), { prompt: "只填了提问" }, SAVED_AT);
  assert.equal(result.messages[0].content, "只填了提问");
  assert.equal(result.platform, null);
  assert.equal(result.environment.logged_in, null);
});

test("已填写的项如实写入：文本去掉首尾空白，三态转为布尔", () => {
  const form = sampleForm({
    model: "  模型甲 v2  ",
    logged_in: "yes",
    history_cleared: "no",
    web_search: "yes",
    region: " 上海 ",
    device: "桌面浏览器",
    operator: " 采集人甲 ",
  });
  const result = capture.buildCapture(samplePage(), form, SAVED_AT);
  assert.equal(result.model, "模型甲 v2");
  assert.equal(result.operator, "采集人甲");
  assert.equal(result.environment.logged_in, true);
  assert.equal(result.environment.history_cleared, false);
  assert.equal(result.environment.web_search, true);
  assert.equal(result.environment.region, "上海");
  assert.equal(result.environment.device, "桌面浏览器");
});

test("提问原文保留内部换行，只去掉首尾空白；回答文本原样保留", () => {
  const page = samplePage({ text: "  第一行\n第二行  \n" });
  const form = sampleForm({ prompt: "\n第一问\n补充说明\n" });
  const result = capture.buildCapture(page, form, SAVED_AT);
  assert.equal(result.messages[0].content, "第一问\n补充说明");
  assert.equal(result.messages[1].content, "  第一行\n第二行  \n");
});

test("提问原文为空时拒绝导出", () => {
  for (const prompt of ["", "   ", "\n\t", undefined, null]) {
    expectCaptureError(
      () => capture.buildCapture(samplePage(), sampleForm({ prompt }), SAVED_AT),
      "prompt_required",
      "提问原文",
    );
  }
});

test("没有选区内容时拒绝导出", () => {
  const cases = [
    null,
    undefined,
    { ok: false, reason: "no_selection" },
    samplePage({ text: "" }),
    samplePage({ text: "  \n " }),
    samplePage({ text: undefined }),
  ];
  for (const page of cases) {
    expectCaptureError(
      () => capture.buildCapture(page, sampleForm(), SAVED_AT),
      "no_selection",
      "请先在页面上选中要保存的回答",
    );
  }
});

test("缺少页面地址时拒绝导出", () => {
  expectCaptureError(
    () => capture.buildCapture(samplePage({ url: "" }), sampleForm(), SAVED_AT),
    "page_url_missing",
    "页面地址",
  );
});

test("编号类字段不合契约的写法时拒绝导出，并指出是哪一项", () => {
  const cases = [
    ["platform", "引擎 A", "平台编号"],
    ["window_id", "W 1", "时间窗口"],
    ["query_id", "-Q001", "题目编号"],
    ["query_id", "Q/001", "题目编号"],
    ["platform", "a".repeat(129), "平台编号"],
  ];
  for (const [field, value, label] of cases) {
    expectCaptureError(
      () => capture.buildCapture(samplePage(), sampleForm({ [field]: value }), SAVED_AT),
      "invalid_id",
      label,
    );
  }
  const accepted = sampleForm({
    platform: "engine-a.web",
    window_id: "2026-10:W1",
    query_id: "Q_001",
  });
  const result = capture.buildCapture(samplePage(), accepted, SAVED_AT);
  assert.equal(result.platform, "engine-a.web");
  assert.equal(result.window_id, "2026-10:W1");
  assert.equal(result.query_id, "Q_001");
  const longest = "a".repeat(128);
  const longResult = capture.buildCapture(samplePage(), sampleForm({ platform: longest }), SAVED_AT);
  assert.equal(longResult.platform, longest);
});

test("保存时间取传入时刻，统一为 UTC、精确到秒", () => {
  assert.equal(capture.formatSavedAt(new Date(Date.UTC(2026, 9, 8, 2, 0, 0))), "2026-10-08T02:00:00Z");
  // 带时区偏移的时刻换算为 UTC。
  assert.equal(capture.formatSavedAt(new Date("2026-10-08T10:00:00+08:00")), "2026-10-08T02:00:00Z");
  // 毫秒截断而不是进位，不把时间写得比实际更晚。
  assert.equal(capture.formatSavedAt(new Date("2026-10-08T02:00:00.999Z")), "2026-10-08T02:00:00Z");
  assert.equal(capture.formatSavedAt(new Date("2026-12-31T23:59:59.500Z")), "2026-12-31T23:59:59Z");
});

test("保存时间无效时拒绝导出", () => {
  for (const bad of [new Date(Number.NaN), "2026-10-08T02:00:00Z", 1791424800000, null, undefined]) {
    expectCaptureError(() => capture.formatSavedAt(bad), "invalid_time", "保存时间");
    expectCaptureError(
      () => capture.buildCapture(samplePage(), sampleForm(), bad),
      "invalid_time",
      "保存时间",
    );
  }
});

test("链接去重并保持首次出现的顺序", () => {
  const a = "https://auto-review.example/a";
  const b = "https://gov-data.example/b";
  const c = "https://lanxu-auto.example/c";
  const { sources, ignored } = capture.dedupeLinks([
    { url: a, title: "甲" },
    { url: b, title: "乙" },
    { url: a, title: "甲（重复）" },
    { url: c, title: "丙" },
    { url: b, title: "乙（重复）" },
  ]);
  assert.deepEqual(sources, [
    { url: a, title: "甲" },
    { url: b, title: "乙" },
    { url: c, title: "丙" },
  ]);
  assert.equal(ignored, 0);
});

test("链接文字压缩空白；为空时记为 null，重复链接里取首个非空文字", () => {
  const a = "https://auto-review.example/a";
  const b = "https://gov-data.example/b";
  const { sources } = capture.dedupeLinks([
    { url: a, title: "  \n " },
    { url: b, title: "  权威\n   数据\t页 " },
    { url: a, title: "评测 文章" },
    { url: a, title: "更晚出现的文字" },
    { url: "https://ev-forum.example/t/1", title: "" },
    { url: "https://news-auto.example/n", title: undefined },
  ]);
  assert.deepEqual(sources, [
    { url: a, title: "评测 文章" },
    { url: b, title: "权威 数据 页" },
    { url: "https://ev-forum.example/t/1", title: null },
    { url: "https://news-auto.example/n", title: null },
  ]);
});

test("链接地址按原样保存：只去首尾空白，不同写法不合并", () => {
  const { sources } = capture.dedupeLinks([
    { url: "  https://auto-review.example/a  ", title: "甲" },
    { url: "https://auto-review.example/a#part", title: "乙" },
    { url: "https://auto-review.example/a?from=share", title: "丙" },
    { url: "https://auto-review.example/a", title: "丁" },
  ]);
  assert.deepEqual(sources, [
    { url: "https://auto-review.example/a", title: "甲" },
    { url: "https://auto-review.example/a#part", title: "乙" },
    { url: "https://auto-review.example/a?from=share", title: "丙" },
  ]);
});

test("只保留 http / https 链接，其余计入忽略数", () => {
  const { sources, ignored } = capture.dedupeLinks([
    { url: "javascript:void(0)", title: "展开" },
    { url: "http://ev-forum.example/t/2", title: "论坛帖" },
    { url: "data:text/plain,x", title: "数据" },
    { url: "ftp://files.lanxu-auto.example/a", title: "文件" },
    { url: "about:blank", title: "空白页" },
    { url: "不是地址", title: "坏链接" },
    { url: "", title: "空" },
    { url: null, title: "空" },
    { url: { baseVal: "x" }, title: "矢量图链接" },
    null,
    "https://auto-review.example/a",
  ]);
  assert.deepEqual(sources, [{ url: "http://ev-forum.example/t/2", title: "论坛帖" }]);
  assert.equal(ignored, 10);
});

test("链接列表不是数组时按没有链接处理", () => {
  for (const links of [undefined, null, "https://auto-review.example/a", {}]) {
    assert.deepEqual(capture.dedupeLinks(links), { sources: [], ignored: 0 });
  }
  const result = capture.buildCapture(samplePage({ links: undefined }), sampleForm(), SAVED_AT);
  assert.deepEqual(result.sources, []);
});

test("三态解析：是 / 否 / 未知", () => {
  for (const value of ["yes", "YES", " yes ", "true", "是", true]) {
    assert.equal(capture.parseTriState(value), true, `应为 true：${String(value)}`);
  }
  for (const value of ["no", "No", "false", "否", false]) {
    assert.equal(capture.parseTriState(value), false, `应为 false：${String(value)}`);
  }
  for (const value of ["unknown", "", "   ", "未知", null, undefined]) {
    assert.equal(capture.parseTriState(value), null, `应为 null：${String(value)}`);
  }
});

test("三态解析遇到认不出的值时报错，不猜", () => {
  for (const value of ["maybe", "1", "0", 1, 0, {}, "是的"]) {
    expectCaptureError(() => capture.parseTriState(value), "invalid_tristate", "未知");
  }
  expectCaptureError(
    () => capture.buildCapture(samplePage(), sampleForm({ web_search: "maybe" }), SAVED_AT),
    "invalid_tristate",
    "是否联网",
  );
});

test("文件名由平台、窗口、题目编号和保存时间组成", () => {
  const full = capture.buildCapture(samplePage(), sampleForm(), SAVED_AT);
  assert.equal(capture.buildFilename(full), "geo-capture_engine-a_W1_Q001_20261008T020000Z.json");

  const bare = capture.buildCapture(samplePage(), { prompt: "只填了提问" }, SAVED_AT);
  assert.equal(capture.buildFilename(bare), "geo-capture_20261008T020000Z.json");

  const withColon = capture.buildCapture(
    samplePage(),
    sampleForm({ window_id: "2026-10:W1", query_id: "Q_001" }),
    SAVED_AT,
  );
  assert.equal(
    capture.buildFilename(withColon),
    "geo-capture_engine-a_2026-10-W1_Q_001_20261008T020000Z.json",
  );
});

test("文件名不含非法字符", () => {
  const SAFE = /^geo-capture(_[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)*\.json$/;
  const control = String.fromCharCode(0, 9, 10, 13, 27, 127);
  const zeroWidth = String.fromCharCode(0x200b, 0xfeff, 0x202e);
  const fixed = [
    "../../etc/passwd",
    "..",
    ".hidden",
    "a/b\\c:d*e?f\"g<h>i|j",
    "CON",
    "名称 带 空格",
    "trailing.",
    "trailing-",
    "___",
    "a..b",
    control,
    zeroWidth,
    `x${control}y${zeroWidth}z`,
    "a".repeat(500),
    "",
  ];
  const alphabet = `abcXYZ019._-:/\\*?"<>| ${control}${zeroWidth}名称`;
  let state = 20261008;
  const next = () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state;
  };
  const random = [];
  for (let i = 0; i < 300; i += 1) {
    let value = "";
    const length = next() % 60;
    for (let j = 0; j < length; j += 1) {
      value += alphabet[next() % alphabet.length];
    }
    random.push(value);
  }
  const samples = [...fixed, ...random];
  for (let i = 0; i < samples.length; i += 1) {
    const name = capture.buildFilename({
      platform: samples[i],
      window_id: samples[(i + 1) % samples.length],
      query_id: samples[(i + 2) % samples.length],
      saved_at: i % 3 === 0 ? samples[(i + 3) % samples.length] : "2026-10-08T02:00:00Z",
    });
    assert.match(name, SAFE, `文件名含非法字符：${JSON.stringify(name)}`);
    assert.equal(name.includes(".."), false, `文件名含连续的点：${name}`);
    // 前缀 11 字符 + 4 段（每段分隔符 1 + 至多 40）+ 扩展名 5 = 180。
    assert.ok(name.length <= 180, `文件名过长（${name.length}）：${name}`);
  }
  assert.equal(
    capture.buildFilename({ platform: null, window_id: undefined, query_id: 7, saved_at: null }),
    "geo-capture.json",
  );
});

test("序列化为带缩进的 UTF-8 JSON 文本，中文不转义", () => {
  const result = capture.buildCapture(samplePage(), sampleForm(), SAVED_AT);
  const text = capture.serializeCapture(result);
  assert.deepEqual(JSON.parse(text), result);
  assert.ok(text.endsWith("}\n"));
  assert.ok(text.startsWith('{\n  "format": "geo-verify-capture/1",\n  "capture": "browser_assist",\n'));
  assert.ok(text.includes('"content": "实际提问原文"'));
  assert.ok(text.includes('"model": null'));
});

test("提示：模型与入口特征都没填、或没填题目编号时提醒用户，但不阻止导出", () => {
  const complete = capture.buildCapture(samplePage(), sampleForm(), SAVED_AT);
  assert.deepEqual(capture.captureWarnings(complete), []);

  const onlyModel = capture.buildCapture(
    samplePage(),
    sampleForm({ model: "模型甲 v2", entry_features: "" }),
    SAVED_AT,
  );
  assert.deepEqual(capture.captureWarnings(onlyModel), []);

  const neither = capture.buildCapture(
    samplePage(),
    sampleForm({ model: "", entry_features: "" }),
    SAVED_AT,
  );
  const neitherWarnings = capture.captureWarnings(neither);
  assert.equal(neitherWarnings.length, 1);
  assert.ok(neitherWarnings[0].includes("入口特征"));

  const noQuery = capture.buildCapture(samplePage(), sampleForm({ query_id: "" }), SAVED_AT);
  const noQueryWarnings = capture.captureWarnings(noQuery);
  assert.equal(noQueryWarnings.length, 1);
  assert.ok(noQueryWarnings[0].includes("题目编号"));
});

test("示例文件就是插件对同样输入的导出结果，外加 synthetic 标记", () => {
  const example = JSON.parse(readExtensionFile("examples/capture.example.json"));
  const triState = (value) => (value === null ? "unknown" : value ? "yes" : "no");
  const page = {
    ok: true,
    text: example.messages[1].content,
    links: example.sources,
    url: example.url,
    title: example.title,
  };
  const form = {
    prompt: example.messages[0].content,
    platform: example.platform,
    window_id: example.window_id,
    query_id: example.query_id,
    model: example.model,
    entry_features: example.environment.entry_features,
    logged_in: triState(example.environment.logged_in),
    history_cleared: triState(example.environment.history_cleared),
    web_search: triState(example.environment.web_search),
    region: example.environment.region,
    device: example.environment.device,
    operator: example.operator,
  };
  const built = capture.buildCapture(page, form, new Date(example.saved_at));
  assert.deepEqual({ ...built, synthetic: true }, example);
  assert.deepEqual(Object.keys(example), [...TOP_LEVEL_KEYS, "synthetic"]);
  assert.equal(
    readExtensionFile("examples/capture.example.json"),
    capture.serializeCapture({ ...built, synthetic: true }),
  );
});

test("同一个文件也能作为普通 <script> 加载，挂到全局 GeoCapture", () => {
  // 浏览器页面里天然有 URL 构造器；全新的脚本上下文没有，这里显式提供。
  const context = loadAsClassicScript("lib/capture.js", { URL });
  assert.deepEqual(Object.keys(context.GeoCapture).sort(), Object.keys(capture).sort());
  const result = context.GeoCapture.buildCapture(samplePage(), sampleForm(), SAVED_AT);
  assert.equal(JSON.stringify(result), JSON.stringify(capture.buildCapture(samplePage(), sampleForm(), SAVED_AT)));
  assert.equal(context.GeoCapture.FORMAT, "geo-verify-capture/1");
  assert.equal(context.GeoCapture.CAPTURE, "browser_assist");
});
