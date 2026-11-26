// 页面读取函数。用户在弹窗里点“读取所选内容”后，弹窗通过 scripting 接口把 readSelection
// 放到当前活动标签页里执行一次，取回：选中的文字、选区内的链接（地址与文字）、页面地址与标题。
//
// 只读：不修改页面，不读取选区以外的正文；没有选区时什么都不读，直接返回 no_selection。
// 浏览器会把 readSelection 的源码单独拿去执行，所以它必须自包含，不能引用本文件里的其他名字。
//
// 同一个文件既能作为普通 <script> 加载（挂到全局 GeoCapturePage），也能被 Node 以 CommonJS 加载。

var GeoCapturePage = (function () {
  "use strict";

  function readSelection() {
    "use strict";
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
      return { ok: false, reason: "no_selection" };
    }
    const text = String(selection.toString());
    if (text.trim() === "") {
      return { ok: false, reason: "no_selection" };
    }

    // 链接按选区范围、文档顺序收集；去重交给纯逻辑模块。
    const links = [];
    function add(anchor) {
      const url = anchor.href;
      // 矢量图里的链接，href 不是字符串：跳过。
      if (typeof url === "string") {
        links.push({ url: url, title: String(anchor.textContent) });
      }
    }

    for (let index = 0; index < selection.rangeCount; index += 1) {
      const range = selection.getRangeAt(index);
      const container = range.commonAncestorContainer;
      const element = container.nodeType === 1 ? container : container.parentElement;
      if (!element) {
        continue;
      }
      // 选区整个落在一个链接里面：就是这个链接。
      const enclosing = element.closest("a[href]");
      if (enclosing) {
        add(enclosing);
        continue;
      }
      // 否则只取与选区相交的链接，选区以外的链接连地址都不读。
      for (const anchor of element.querySelectorAll("a[href]")) {
        if (range.intersectsNode(anchor)) {
          add(anchor);
        }
      }
    }

    return { ok: true, text: text, links: links, url: location.href, title: document.title };
  }

  return { readSelection: readSelection };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = GeoCapturePage;
}
