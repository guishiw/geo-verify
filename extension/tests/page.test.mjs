// 页面读取函数的行为测试：只读用户当前选区；没有选区时什么都不读；对页面零写入。
// 函数按浏览器注入的方式运行：源码序列化后放进只有替身对象的全新上下文。
// 零依赖，直接运行：node extension/tests/page.test.mjs

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

import {
  fakeAnchor,
  fakeElement,
  fakeRange,
  fakeSelection,
  fakeTextNode,
  loadAsClassicScript,
  runInPage,
} from "./fakes.mjs";

const require = createRequire(import.meta.url);
const page = require("../lib/page.js");

const PAGE_URL = "https://chat.engine-a.example/share/abc";
const PAGE_TITLE = "澜序 S7 续航（虚构示例）";

/** 页面读取函数允许碰的全部属性；出现清单以外的访问即视为越界。 */
const ALLOWED_ACCESS = new Set([
  "window.getSelection",
  "selection.rangeCount",
  "selection.isCollapsed",
  "selection.toString",
  "selection.getRangeAt",
  "range.commonAncestorContainer",
  "range.intersectsNode",
  "element.nodeType",
  "element.closest",
  "element.querySelectorAll",
  "text.nodeType",
  "text.parentElement",
  "a.href",
  "a.textContent",
  "document.title",
  "location.href",
]);

function assertOnlyAllowed(log) {
  for (const entry of log) {
    assert.ok(ALLOWED_ACCESS.has(entry), `页面读取函数访问了清单以外的属性：${entry}`);
  }
}

test("没有选区时返回 no_selection，且不读取页面标题、地址或任何页面内容", () => {
  const cases = [
    { text: "", ranges: [] },
    { text: "", ranges: [], isCollapsed: true },
    { text: "   \n\t ", ranges: [], isCollapsed: false },
  ];
  for (const options of cases) {
    const log = [];
    const selection = fakeSelection(options, log);
    // pageUrl、pageTitle 都不提供：一旦读取 document 或 location 的任何属性就会抛错。
    const result = runInPage(page.readSelection, { selection }, log);
    assert.deepEqual(result, { ok: false, reason: "no_selection" });
    assert.equal(log.some((entry) => entry.startsWith("document.")), false);
    assert.equal(log.some((entry) => entry.startsWith("location.")), false);
    assert.equal(log.includes("selection.getRangeAt"), false);
    assertOnlyAllowed(log);
  }
});

test("选区为空白但浏览器仍报告有范围时，同样不读取页面", () => {
  const log = [];
  const container = fakeElement({ anchors: [] }, log);
  const range = fakeRange(container, [], log);
  const selection = fakeSelection({ text: " \n ", ranges: [range], isCollapsed: false }, log);
  const result = runInPage(page.readSelection, { selection }, log);
  assert.deepEqual(result, { ok: false, reason: "no_selection" });
  assert.equal(log.some((entry) => entry.startsWith("range.")), false);
  assert.equal(log.some((entry) => entry.startsWith("element.")), false);
});

test("只收集与选区相交的链接，保持文档顺序，文本原样返回", () => {
  const log = [];
  const first = fakeAnchor("https://auto-review.example/a", "  评测\n文章 ", log);
  const outside = fakeAnchor("https://ev-forum.example/t/1", "选区外的链接", log);
  const second = fakeAnchor("https://gov-data.example/b", "权威数据", log);
  const container = fakeElement({ anchors: [first, outside, second] }, log);
  const range = fakeRange(container, [first, second], log);
  const text = "  澜序 S7 长续航版的 CLTC 续航为 620 km。\n参考：评测文章、权威数据 ";
  const selection = fakeSelection({ text, ranges: [range] }, log);

  const result = runInPage(page.readSelection, { selection, pageUrl: PAGE_URL, pageTitle: PAGE_TITLE }, log);

  assert.deepEqual(result, {
    ok: true,
    text,
    links: [
      { url: "https://auto-review.example/a", title: "  评测\n文章 " },
      { url: "https://gov-data.example/b", title: "权威数据" },
    ],
    url: PAGE_URL,
    title: PAGE_TITLE,
  });
  assertOnlyAllowed(log);
  // 选区外的链接连地址和文字都没有被读取。
  assert.equal(log.filter((entry) => entry === "a.href").length, 2);
  assert.equal(log.filter((entry) => entry === "a.textContent").length, 2);
});

test("选区完全落在一个链接内部时，收集这个链接", () => {
  const log = [];
  const enclosing = fakeAnchor("https://lanxu-auto.example/s7", "澜序 S7 配置表", log);
  const span = fakeElement({ enclosing }, log);
  const textNode = fakeTextNode(span, log);
  const range = fakeRange(textNode, [], log);
  const selection = fakeSelection({ text: "S7 配置", ranges: [range] }, log);

  const result = runInPage(page.readSelection, { selection, pageUrl: PAGE_URL, pageTitle: PAGE_TITLE }, log);

  assert.deepEqual(result.links, [{ url: "https://lanxu-auto.example/s7", title: "澜序 S7 配置表" }]);
  assert.equal(result.text, "S7 配置");
  // 已经确定被一个链接包住，就不再向下查找。
  assert.equal(log.includes("element.querySelectorAll"), false);
  assertOnlyAllowed(log);
});

test("选区起止在同一个文本节点、且不在链接内时，没有链接", () => {
  const log = [];
  const paragraph = fakeElement({ anchors: [] }, log);
  const textNode = fakeTextNode(paragraph, log);
  const range = fakeRange(textNode, [], log);
  const selection = fakeSelection({ text: "续航为 620 km", ranges: [range] }, log);

  const result = runInPage(page.readSelection, { selection, pageUrl: PAGE_URL, pageTitle: PAGE_TITLE }, log);

  assert.deepEqual(result, { ok: true, text: "续航为 620 km", links: [], url: PAGE_URL, title: PAGE_TITLE });
});

test("多个范围的选区按范围顺序依次收集", () => {
  const log = [];
  const a = fakeAnchor("https://auto-review.example/a", "甲", log);
  const b = fakeAnchor("https://gov-data.example/b", "乙", log);
  const c = fakeAnchor("https://news-auto.example/c", "丙", log);
  const firstContainer = fakeElement({ anchors: [a, b] }, log);
  const secondContainer = fakeElement({ anchors: [c, a] }, log);
  const ranges = [fakeRange(firstContainer, [b], log), fakeRange(secondContainer, [c, a], log)];
  const selection = fakeSelection({ text: "两段选中的文字", ranges }, log);

  const result = runInPage(page.readSelection, { selection, pageUrl: PAGE_URL, pageTitle: PAGE_TITLE }, log);

  // 页面函数不去重：重复的链接交给纯逻辑模块统一处理。
  assert.deepEqual(
    result.links.map((link) => link.url),
    ["https://gov-data.example/b", "https://news-auto.example/c", "https://auto-review.example/a"],
  );
});

test("地址不是字符串的链接（如矢量图里的链接）被跳过；脱离文档的文本节点不报错", () => {
  const log = [];
  const vector = fakeAnchor({ baseVal: "#shape" }, "矢量图链接", log);
  const normal = fakeAnchor("https://auto-review.example/a", "评测文章", log);
  const container = fakeElement({ anchors: [vector, normal] }, log);
  const detached = fakeTextNode(null, log);
  const ranges = [fakeRange(detached, [], log), fakeRange(container, [vector, normal], log)];
  const selection = fakeSelection({ text: "选中的文字", ranges }, log);

  const result = runInPage(page.readSelection, { selection, pageUrl: PAGE_URL, pageTitle: PAGE_TITLE }, log);

  assert.deepEqual(result.links, [{ url: "https://auto-review.example/a", title: "评测文章" }]);
  assertOnlyAllowed(log);
});

test("返回值是纯数据，可以被结构化克隆", () => {
  const log = [];
  const link = fakeAnchor("https://auto-review.example/a", "评测文章", log);
  const container = fakeElement({ anchors: [link] }, log);
  const selection = fakeSelection({ text: "选中的文字", ranges: [fakeRange(container, [link], log)] }, log);
  const result = runInPage(page.readSelection, { selection, pageUrl: PAGE_URL, pageTitle: PAGE_TITLE }, log);
  assert.deepEqual(structuredClone(result), result);
  assert.deepEqual(Object.keys(result), ["ok", "text", "links", "url", "title"]);
});

test("同一个文件也能作为普通 <script> 加载，挂到全局 GeoCapturePage", () => {
  const context = loadAsClassicScript("lib/page.js");
  assert.deepEqual(Object.keys(context.GeoCapturePage), ["readSelection"]);
  assert.equal(context.GeoCapturePage.readSelection.toString(), page.readSelection.toString());
});
