// 弹窗脚本的接线测试：用替身的文档和扩展接口，按 popup.html 的脚本顺序加载全部脚本，
// 再模拟用户的两次点击（读取所选内容、保存为文件）。
// 零依赖，直接运行：node extension/tests/popup.test.mjs
// 说明：这里验证的是脚本之间的接线和不越界，不能代替在真实浏览器里的人工验证。

import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";

import {
  fakeAnchor,
  fakeElement,
  fakePopupDocument,
  fakeRange,
  fakeSelection,
  fire,
  readExtensionFile,
  runInPage,
  scriptSources,
  strict,
} from "./fakes.mjs";

const PAGE_URL = "https://chat.engine-a.example/share/abc";
const PAGE_TITLE = "澜序 S7 续航（虚构示例）";
const ANSWER = "澜序 S7 长续航版的 CLTC 续航为 620 km。";
const NOW = Date.parse("2026-10-08T02:00:00.250Z");
const NO_SELECTION_HINT = "请先在页面上选中要保存的回答";

const FIELD_IDS = {
  prompt: "f-prompt",
  platform: "f-platform",
  window_id: "f-window-id",
  query_id: "f-query-id",
  model: "f-model",
  entry_features: "f-entry-features",
  logged_in: "f-logged-in",
  history_cleared: "f-history-cleared",
  web_search: "f-web-search",
  region: "f-region",
  device: "f-device",
  operator: "f-operator",
};

const plain = (value) => JSON.parse(JSON.stringify(value));

class FixedDate extends Date {
  constructor(...args) {
    if (args.length === 0) {
      super(NOW);
    } else {
      super(...args);
    }
  }
}

function selectionWithLinks(log) {
  const review = fakeAnchor("https://auto-review.example/a", "评测文章", log);
  const toggle = fakeAnchor("javascript:void(0)", "展开全部", log);
  const data = fakeAnchor("https://gov-data.example/b", "权威数据", log);
  const container = fakeElement({ anchors: [review, toggle, data, review] }, log);
  const range = fakeRange(container, [review, toggle, data], log);
  return fakeSelection({ text: ANSWER, ranges: [range] }, log);
}

function emptySelection(log) {
  return fakeSelection({ text: "", ranges: [] }, log);
}

/** 加载弹窗。makeSelection 决定“页面上”当前的选区；tabs、executeError 用来模拟浏览器的异常情况。 */
function openPopup({ makeSelection = selectionWithLinks, tabs = [{ id: 7 }], executeError = null } = {}) {
  const html = readExtensionFile("popup.html");
  const document = fakePopupDocument(html);
  const calls = [];
  const pageLog = [];
  const blobs = [];
  const revoked = [];
  const chromeLog = [];

  const chrome = strict(
    "chrome",
    {
      tabs: strict(
        "chrome.tabs",
        {
          async query(queryInfo) {
            calls.push(["tabs.query", plain(queryInfo)]);
            return tabs;
          },
        },
        chromeLog,
      ),
      scripting: strict(
        "chrome.scripting",
        {
          async executeScript(injection) {
            calls.push(["scripting.executeScript", injection]);
            if (executeError) {
              throw executeError;
            }
            const page = { selection: makeSelection(pageLog), pageUrl: PAGE_URL, pageTitle: PAGE_TITLE };
            return [{ frameId: 0, result: runInPage(injection.func, page, pageLog) }];
          },
        },
        chromeLog,
      ),
    },
    chromeLog,
  );

  class FakeURL extends URL {
    static createObjectURL(blob) {
      blobs.push(blob);
      return `blob:fake-${blobs.length}`;
    }

    static revokeObjectURL(url) {
      revoked.push(url);
    }
  }

  const context = vm.createContext({ document, chrome, URL: FakeURL, Blob, Date: FixedDate });
  const sources = scriptSources(html);
  for (const source of sources) {
    vm.runInContext(readExtensionFile(source), context, { filename: source });
  }

  const element = (id) => document.getElementById(id);
  const fill = (values) => {
    for (const [field, value] of Object.entries(values)) {
      element(FIELD_IDS[field]).value = value;
    }
  };
  const clickSave = () => {
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    const returned = fire(element("save-file"), "click", event);
    // 保存必须在点击事件处理期间同步完成，浏览器才会按新的地址下载。
    assert.equal(returned, undefined, "保存按钮的处理函数必须是同步的");
    return event;
  };
  return { context, document, element, fill, clickSave, calls, pageLog, blobs, revoked, sources, chromeLog };
}

const FULL_FORM = {
  prompt: "澜序 S7 的续航是多少？",
  platform: "engine-a",
  window_id: "W1",
  query_id: "Q001",
  model: "",
  entry_features: "网页端默认模式",
  logged_in: "yes",
  history_cleared: "no",
  web_search: "unknown",
  region: "上海",
  device: "",
  operator: "采集人甲",
};

test("脚本按依赖顺序加载：两个库在前，popup.js 在最后", () => {
  const { sources } = openPopup();
  assert.deepEqual([...sources].sort(), ["lib/capture.js", "lib/page.js", "popup.js"]);
  assert.equal(sources.at(-1), "popup.js");
});

test("打开弹窗本身不读取页面，也不调用任何扩展接口", () => {
  const popup = openPopup();
  assert.deepEqual(popup.calls, []);
  assert.deepEqual(popup.chromeLog, []);
  assert.deepEqual(popup.pageLog, []);
  assert.equal(popup.element("preview").hidden, true);
  assert.equal(popup.element("read-selection").listeners.click.length, 1);
  assert.equal(popup.element("save-file").listeners.click.length, 1);
});

test("点击“读取所选内容”：只向当前活动标签页注入一次页面读取函数，并显示预览", async () => {
  const popup = openPopup();
  await fire(popup.element("read-selection"), "click");

  assert.equal(popup.calls.length, 2);
  assert.deepEqual(popup.calls[0], ["tabs.query", { active: true, currentWindow: true }]);
  const [name, injection] = popup.calls[1];
  assert.equal(name, "scripting.executeScript");
  assert.deepEqual(Object.keys(injection).sort(), ["func", "target"]);
  assert.deepEqual(plain(injection.target), { tabId: 7 });
  assert.equal(injection.func, popup.context.GeoCapturePage.readSelection);

  assert.equal(popup.element("preview").hidden, false);
  assert.equal(popup.element("preview-title").textContent, PAGE_TITLE);
  assert.equal(popup.element("preview-url").textContent, PAGE_URL);
  assert.equal(popup.element("preview-text").textContent, ANSWER);
  const items = popup.element("preview-links").children;
  assert.equal(items.length, 2);
  assert.ok(items[0].textContent.includes("https://auto-review.example/a"));
  assert.ok(items[0].textContent.includes("评测文章"));
  assert.ok(items[1].textContent.includes("https://gov-data.example/b"));
  const note = popup.element("preview-links-note").textContent;
  assert.ok(note.includes("忽略") && note.includes("1"), `应说明忽略了 1 个链接：${note}`);
  assert.ok(popup.element("read-status").textContent.includes("已读取"));
});

test("页面上没有选区时提示用户先选中回答，不显示预览", async () => {
  const popup = openPopup({ makeSelection: emptySelection });
  await fire(popup.element("read-selection"), "click");

  assert.equal(popup.element("read-status").textContent, NO_SELECTION_HINT);
  assert.equal(popup.element("preview").hidden, true);
  assert.equal(popup.pageLog.some((entry) => entry.startsWith("document.")), false);

  popup.fill(FULL_FORM);
  const event = popup.clickSave();
  assert.equal(event.prevented, true);
  assert.ok(popup.element("save-status").textContent.includes(NO_SELECTION_HINT));
  assert.deepEqual(popup.blobs, []);
});

test("还没读取就点保存：不生成文件，提示先读取", () => {
  const popup = openPopup();
  popup.fill(FULL_FORM);
  const event = popup.clickSave();
  assert.equal(event.prevented, true);
  assert.ok(popup.element("save-status").textContent.includes("读取所选内容"));
  assert.deepEqual(popup.blobs, []);
  assert.deepEqual(popup.calls, []);
});

test("提问原文没填时不生成文件", async () => {
  const popup = openPopup();
  await fire(popup.element("read-selection"), "click");
  popup.fill({ ...FULL_FORM, prompt: "   " });
  const event = popup.clickSave();
  assert.equal(event.prevented, true);
  assert.ok(popup.element("save-status").textContent.includes("提问原文"));
  assert.deepEqual(popup.blobs, []);
  assert.equal(popup.element("save-file").download, "");
});

test("点击“保存为文件”：在本地生成 JSON 文件，内容与约定格式一致", async () => {
  const popup = openPopup();
  await fire(popup.element("read-selection"), "click");
  popup.fill(FULL_FORM);
  const event = popup.clickSave();

  assert.equal(event.prevented, false);
  const link = popup.element("save-file");
  assert.equal(link.href, "blob:fake-1");
  assert.equal(link.download, "geo-capture_engine-a_W1_Q001_20261008T020000Z.json");
  assert.equal(popup.blobs.length, 1);
  assert.equal(popup.blobs[0].type, "application/json");
  const text = await popup.blobs[0].text();
  assert.ok(text.endsWith("}\n"));
  assert.deepEqual(JSON.parse(text), {
    format: "geo-verify-capture/1",
    capture: "browser_assist",
    saved_at: "2026-10-08T02:00:00Z",
    url: PAGE_URL,
    title: PAGE_TITLE,
    platform: "engine-a",
    window_id: "W1",
    query_id: "Q001",
    messages: [
      { role: "user", content: "澜序 S7 的续航是多少？" },
      { role: "assistant", content: ANSWER },
    ],
    sources: [
      { url: "https://auto-review.example/a", title: "评测文章" },
      { url: "https://gov-data.example/b", title: "权威数据" },
    ],
    model: null,
    environment: {
      entry_form: "web",
      entry_features: "网页端默认模式",
      logged_in: true,
      history_cleared: false,
      web_search: null,
      region: "上海",
      device: null,
    },
    operator: "采集人甲",
  });
  const status = popup.element("save-status").textContent;
  assert.ok(status.includes("geo-capture_engine-a_W1_Q001_20261008T020000Z.json"));
  assert.ok(status.includes("保存时间") && status.includes("2026-10-08T02:00:00Z"));
  // 保存不再触碰页面，也不产生新的扩展接口调用。
  assert.equal(popup.calls.length, 2);
});

test("再次保存时释放上一次的本地地址", async () => {
  const popup = openPopup();
  await fire(popup.element("read-selection"), "click");
  popup.fill(FULL_FORM);
  popup.clickSave();
  assert.deepEqual(popup.revoked, []);
  popup.fill({ query_id: "Q002" });
  popup.clickSave();
  assert.deepEqual(popup.revoked, ["blob:fake-1"]);
  assert.equal(popup.element("save-file").href, "blob:fake-2");
  assert.equal(popup.element("save-file").download, "geo-capture_engine-a_W1_Q002_20261008T020000Z.json");
});

test("保存成功但缺少建议填写的项时，给出提醒而不阻止保存", async () => {
  const popup = openPopup();
  await fire(popup.element("read-selection"), "click");
  popup.fill({ ...FULL_FORM, entry_features: "", query_id: "" });
  const event = popup.clickSave();
  assert.equal(event.prevented, false);
  assert.equal(popup.blobs.length, 1);
  const status = popup.element("save-status").textContent;
  assert.ok(status.includes("入口特征"));
  assert.ok(status.includes("题目编号"));
});

test("浏览器不允许在当前页面运行时给出说明，不抛出未处理的错误", async () => {
  const popup = openPopup({ executeError: new Error("Cannot access this page") });
  await fire(popup.element("read-selection"), "click");
  assert.ok(popup.element("read-status").textContent.includes("无法读取"));
  assert.equal(popup.element("preview").hidden, true);
  const event = popup.clickSave();
  assert.equal(event.prevented, true);
});

test("找不到当前标签页时给出说明，不注入任何脚本", async () => {
  const popup = openPopup({ tabs: [] });
  await fire(popup.element("read-selection"), "click");
  assert.ok(popup.element("read-status").textContent.includes("标签页"));
  assert.deepEqual(popup.calls.map((call) => call[0]), ["tabs.query"]);
});

test("重新读取失败后，之前读到的内容不会被继续用于保存", async () => {
  let current = selectionWithLinks;
  const popup = openPopup({ makeSelection: (log) => current(log) });
  await fire(popup.element("read-selection"), "click");
  assert.equal(popup.element("preview").hidden, false);
  current = emptySelection;
  await fire(popup.element("read-selection"), "click");
  assert.equal(popup.element("preview").hidden, true);
  popup.fill(FULL_FORM);
  const event = popup.clickSave();
  assert.equal(event.prevented, true);
  assert.deepEqual(popup.blobs, []);
});
