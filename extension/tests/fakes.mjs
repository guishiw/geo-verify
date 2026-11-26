// 测试替身：只模拟页面读取函数和弹窗脚本会用到的最小接口。
// 页面一侧的替身是“白名单式”的：读取未列出的属性、或做任何写入，都会直接抛错，
// 以此证明页面读取函数只读、且只碰选区相关的内容。

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

export const EXTENSION_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

export function readExtensionFile(relativePath) {
  return readFileSync(join(EXTENSION_ROOT, relativePath), "utf8");
}

/** 只允许读取 props 中列出的属性；其余读取和一切写入都抛错。访问记录写入 log。 */
export function strict(name, props, log) {
  return new Proxy(props, {
    get(target, key) {
      if (typeof key === "symbol" || !Object.hasOwn(target, key)) {
        throw new Error(`不允许读取 ${name}.${String(key)}`);
      }
      log.push(`${name}.${key}`);
      return target[key];
    },
    set(_target, key) {
      throw new Error(`不允许写入 ${name}.${String(key)}`);
    },
    defineProperty(_target, key) {
      throw new Error(`不允许定义 ${name}.${String(key)}`);
    },
    deleteProperty(_target, key) {
      throw new Error(`不允许删除 ${name}.${String(key)}`);
    },
  });
}

function checkSelector(selector) {
  if (typeof selector !== "string" || !selector.includes("a[href]")) {
    throw new Error(`意料之外的选择器：${String(selector)}`);
  }
}

export function fakeAnchor(href, textContent, log) {
  return strict("a", { href, textContent }, log);
}

/** 元素节点。enclosing 是包住它的链接（没有则为 null），anchors 是它内部的链接（文档顺序）。 */
export function fakeElement({ enclosing = null, anchors = [] }, log) {
  return strict(
    "element",
    {
      nodeType: 1,
      closest(selector) {
        checkSelector(selector);
        return enclosing;
      },
      querySelectorAll(selector) {
        checkSelector(selector);
        return anchors;
      },
    },
    log,
  );
}

export function fakeTextNode(parentElement, log) {
  return strict("text", { nodeType: 3, parentElement }, log);
}

/** 选区范围。intersecting 是与该范围相交的节点列表。 */
export function fakeRange(container, intersecting, log) {
  return strict(
    "range",
    {
      commonAncestorContainer: container,
      intersectsNode(node) {
        return intersecting.includes(node);
      },
    },
    log,
  );
}

export function fakeSelection({ text, ranges = [], isCollapsed = ranges.length === 0 }, log) {
  return strict(
    "selection",
    {
      rangeCount: ranges.length,
      isCollapsed,
      getRangeAt(index) {
        return ranges[index];
      },
      toString() {
        return text;
      },
    },
    log,
  );
}

/**
 * 像浏览器注入脚本那样运行页面函数：把函数源码序列化后放进一个全新的上下文执行，
 * 上下文里只有 window / document / location 三个替身，返回值按结构化克隆的效果转成纯数据。
 * pageUrl 或 pageTitle 传 undefined 时，对应属性不可读（读取即抛错）。
 */
export function runInPage(func, { selection, pageUrl, pageTitle }, log) {
  const documentProps = pageTitle === undefined ? {} : { title: pageTitle };
  const locationProps = pageUrl === undefined ? {} : { href: pageUrl };
  const sandbox = {
    window: strict("window", { getSelection: () => selection }, log),
    document: strict("document", documentProps, log),
    location: strict("location", locationProps, log),
  };
  const result = vm.runInNewContext(`(${func.toString()})()`, sandbox);
  return JSON.parse(JSON.stringify(result));
}

/** 把脚本当作普通 <script> 在全新上下文里执行，返回上下文的全局对象。 */
export function loadAsClassicScript(relativePath, sandbox = {}) {
  const context = vm.createContext(sandbox);
  vm.runInContext(readExtensionFile(relativePath), context, { filename: relativePath });
  return context;
}

/** 弹窗文档里的元素替身。 */
export function fakePopupElement(id, tagName, hidden = false) {
  return {
    id,
    tagName,
    hidden,
    value: "",
    textContent: "",
    href: "",
    download: "",
    children: [],
    listeners: {},
    addEventListener(type, listener) {
      (this.listeners[type] ??= []).push(listener);
    },
    replaceChildren(...nodes) {
      this.children = nodes;
    },
    append(...nodes) {
      this.children.push(...nodes);
    },
  };
}

/** 按 popup.html 里实际出现的 id 构造弹窗文档替身；取不存在的 id 直接抛错。 */
export function fakePopupDocument(html) {
  const elements = new Map();
  for (const match of html.matchAll(/<([a-z0-9]+)\b([^>]*)>/g)) {
    const idMatch = /\bid="([^"]+)"/.exec(match[2]);
    if (idMatch) {
      const hidden = /\bhidden\b/.test(match[2]);
      elements.set(idMatch[1], fakePopupElement(idMatch[1], match[1], hidden));
    }
  }
  return {
    elements,
    getElementById(id) {
      if (!elements.has(id)) {
        throw new Error(`popup.html 里没有 id 为 ${id} 的元素`);
      }
      return elements.get(id);
    },
    createElement(tagName) {
      return fakePopupElement("", tagName);
    },
  };
}

/** popup.html 里按出现顺序引用的脚本路径。 */
export function scriptSources(html) {
  return [...html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)].map((match) => match[1]);
}

/** 调用元素上登记的事件监听器，返回最后一个监听器的返回值。 */
export function fire(element, type, event = {}) {
  const listeners = element.listeners[type] ?? [];
  if (listeners.length === 0) {
    throw new Error(`元素 ${element.id} 没有登记 ${type} 监听器`);
  }
  let result;
  for (const listener of listeners) {
    result = listener(event);
  }
  return result;
}
