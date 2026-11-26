// 弹窗脚本。只响应用户在弹窗里的两次点击：
// 1. “读取所选内容”：查询当前窗口的活动标签页，在其中执行一次只读的页面读取函数，
//    取回用户选中的文字与链接，在弹窗里显示预览；
// 2. “保存为文件”：把读取结果与用户手工填写的各项组装成 JSON，用 Blob 在本机生成文件，
//    由用户点击的这个链接直接保存。
// 打开弹窗本身不读取页面、不调用任何扩展接口；页面上的文字只经 textContent 写进弹窗。

(function () {
  "use strict";

  // 最近一次成功读取的结果；读取失败或重新读取时清空，避免把旧内容当成新内容保存。
  let current = null;
  // 最近一次生成的本地文件地址；下一次点击保存时释放。
  let objectUrl = null;

  function element(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    element(id).textContent = text;
  }

  function readForm() {
    return {
      prompt: element("f-prompt").value,
      platform: element("f-platform").value,
      window_id: element("f-window-id").value,
      query_id: element("f-query-id").value,
      model: element("f-model").value,
      entry_features: element("f-entry-features").value,
      logged_in: element("f-logged-in").value,
      history_cleared: element("f-history-cleared").value,
      web_search: element("f-web-search").value,
      region: element("f-region").value,
      device: element("f-device").value,
      operator: element("f-operator").value,
    };
  }

  function clearPreview() {
    element("preview").hidden = true;
    setText("preview-title", "");
    setText("preview-url", "");
    setText("preview-text", "");
    element("preview-links").replaceChildren();
    setText("preview-links-note", "");
  }

  function showPreview(page) {
    const links = GeoCapture.dedupeLinks(page.links);
    setText("preview-title", page.title === "" ? "（页面没有标题）" : page.title);
    setText("preview-url", page.url);
    setText("preview-text", page.text);
    const items = [];
    for (const source of links.sources) {
      const item = document.createElement("li");
      const title = source.title === null ? "（链接没有文字）" : source.title;
      item.textContent = title + "：" + source.url;
      items.push(item);
    }
    element("preview-links").replaceChildren(...items);
    const notes = [];
    if (links.sources.length === 0) {
      notes.push("选区内没有可保存的链接。");
    }
    if (links.ignored > 0) {
      notes.push("已忽略 " + links.ignored + " 个不是 http / https 地址的链接（如页面内的按钮）。");
    }
    setText("preview-links-note", notes.join(""));
    element("preview").hidden = false;
  }

  async function onRead() {
    current = null;
    clearPreview();
    setText("read-status", "正在读取……");
    let tabs;
    try {
      tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    } catch {
      setText("read-status", "无法确定当前标签页，请回到要保存的页面后重新打开本插件。");
      return;
    }
    const tab = Array.isArray(tabs) && tabs.length > 0 ? tabs[0] : null;
    if (tab === null || typeof tab.id !== "number") {
      setText("read-status", "找不到当前标签页，请回到要保存的页面后重新打开本插件。");
      return;
    }
    let results;
    try {
      results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: GeoCapturePage.readSelection,
      });
    } catch {
      setText(
        "read-status",
        "无法读取当前页面：浏览器不允许插件在这类页面上运行（如浏览器设置页、扩展商店页），" +
          "或页面已关闭。",
      );
      return;
    }
    const page = Array.isArray(results) && results.length > 0 ? results[0].result : null;
    if (page === null || typeof page !== "object") {
      setText("read-status", "无法读取当前页面：没有取回结果，请刷新页面后重试。");
      return;
    }
    if (page.ok !== true) {
      setText("read-status", GeoCapture.NO_SELECTION_HINT);
      return;
    }
    current = page;
    showPreview(page);
    setText("read-status", "已读取所选内容。请核对预览，再填写下方各项并保存。");
  }

  // 必须同步完成：浏览器在点击事件处理结束后，按此时链接上的地址和文件名保存。
  function onSave(event) {
    const link = element("save-file");
    if (objectUrl !== null) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
    link.href = "#";
    let capture;
    try {
      capture = GeoCapture.buildCapture(current, readForm(), new Date());
    } catch (error) {
      event.preventDefault();
      link.download = "";
      const reason = error instanceof GeoCapture.CaptureError ? error.message : String(error);
      setText("save-status", "未保存：" + reason);
      return;
    }
    const filename = GeoCapture.buildFilename(capture);
    const blob = new Blob([GeoCapture.serializeCapture(capture)], { type: "application/json" });
    objectUrl = URL.createObjectURL(blob);
    link.href = objectUrl;
    link.download = filename;
    const lines = ["已生成文件 " + filename + "（保存时间 " + capture.saved_at + "，UTC）。"];
    for (const warning of GeoCapture.captureWarnings(capture)) {
      lines.push("提醒：" + warning);
    }
    setText("save-status", lines.join("\n"));
  }

  element("read-selection").addEventListener("click", onRead);
  element("save-file").addEventListener("click", onSave);
})();
