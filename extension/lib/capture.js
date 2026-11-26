// 插件的纯逻辑：把页面读取结果和用户手工填写的表单值组装成导出对象（格式见 FORMAT.md），
// 链接去重、三态解析、保存时间格式化、文件名生成与序列化。
//
// 不依赖浏览器或扩展接口，没有任何副作用。同一个文件既能作为普通 <script> 加载
// （挂到全局 GeoCapture），也能被 Node 以 CommonJS 加载（module.exports）。
//
// 约定：没填写的项一律为 null，不编默认值；时间只用调用方传入的保存时刻。

var GeoCapture = (function () {
  "use strict";

  const FORMAT = "geo-verify-capture/1";
  const CAPTURE = "browser_assist";
  const ENTRY_FORM = "web";
  // 与观测契约 spec/v1/observation.schema.json 中 $defs.id 的写法一致，导出的编号才能被导入。
  const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
  const NO_SELECTION_HINT = "请先在页面上选中要保存的回答";
  // 文件名里每一段最多保留的字符数。
  const SEGMENT_LIMIT = 40;

  const TRUE_WORDS = ["yes", "true", "是"];
  const FALSE_WORDS = ["no", "false", "否"];
  const UNKNOWN_WORDS = ["", "unknown", "未知"];

  class CaptureError extends Error {
    constructor(code, message) {
      super(message);
      this.name = "CaptureError";
      this.code = code;
    }
  }

  function fail(code, message) {
    throw new CaptureError(code, message);
  }

  /** 可选文本：去掉首尾空白；没填（缺省、null、空白）为 null。 */
  function optionalText(value, label) {
    if (value === undefined || value === null) {
      return null;
    }
    if (typeof value !== "string") {
      fail("invalid_field", label + "必须是文本。");
    }
    const trimmed = value.trim();
    return trimmed === "" ? null : trimmed;
  }

  /** 可选编号：没填为 null；填了就必须符合契约的编号写法。 */
  function optionalId(value, label) {
    const text = optionalText(value, label);
    if (text !== null && !ID_PATTERN.test(text)) {
      fail(
        "invalid_id",
        label +
          "“" +
          text +
          "”不合要求：只能用英文字母、数字和 . _ : -，以字母或数字开头，最长 128 个字符。",
      );
    }
    return text;
  }

  function triState(value, label) {
    if (value === true || value === false) {
      return value;
    }
    if (value === undefined || value === null) {
      return null;
    }
    if (typeof value === "string") {
      const word = value.trim().toLowerCase();
      if (TRUE_WORDS.includes(word)) {
        return true;
      }
      if (FALSE_WORDS.includes(word)) {
        return false;
      }
      if (UNKNOWN_WORDS.includes(word)) {
        return null;
      }
    }
    const shown = typeof value === "string" ? value : typeof value;
    return fail("invalid_tristate", label + "只能是 是 / 否 / 未知，收到“" + shown + "”。");
  }

  /** 三态解析：是 → true，否 → false，未知或没填 → null；认不出的值报错，不猜。 */
  function parseTriState(value) {
    return triState(value, "取值");
  }

  /** 保存时间：传入时刻的 UTC 写法，精确到秒（截断毫秒，不进位）。 */
  function formatSavedAt(date) {
    const valid =
      date !== null &&
      typeof date === "object" &&
      typeof date.getTime === "function" &&
      typeof date.toISOString === "function" &&
      Number.isFinite(date.getTime());
    if (!valid) {
      fail("invalid_time", "保存时间无效：需要本机的当前时刻。");
    }
    const text = date.toISOString().slice(0, 19);
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(text)) {
      fail("invalid_time", "保存时间超出可写范围。");
    }
    return text + "Z";
  }

  /** 链接地址：只接受 http / https，按原样保存（只去首尾空白）；其他一律返回 null。 */
  function webUrl(link) {
    if (link === null || typeof link !== "object" || typeof link.url !== "string") {
      return null;
    }
    const url = link.url.trim();
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      return null;
    }
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? url : null;
  }

  function linkTitle(title) {
    const text = typeof title === "string" ? title.replace(/\s+/g, " ").trim() : "";
    return text === "" ? null : text;
  }

  /**
   * 链接去重：按地址去重并保持首次出现的顺序；文字压缩空白，为空记 null，
   * 重复的链接里取首个非空文字。非 http / https 的链接不保存，计入 ignored。
   */
  function dedupeLinks(links) {
    if (!Array.isArray(links)) {
      return { sources: [], ignored: 0 };
    }
    const order = [];
    const titles = new Map();
    let ignored = 0;
    for (const link of links) {
      const url = webUrl(link);
      if (url === null) {
        ignored += 1;
        continue;
      }
      const title = linkTitle(link.title);
      if (!titles.has(url)) {
        order.push(url);
        titles.set(url, title);
      } else if (titles.get(url) === null) {
        titles.set(url, title);
      }
    }
    return {
      sources: order.map((url) => ({ url: url, title: titles.get(url) })),
      ignored: ignored,
    };
  }

  /**
   * 组装导出对象。page 是页面读取函数的返回值，form 是表单值，savedAt 是用户点击保存的时刻。
   * 没有选区、没填提问原文、缺页面地址、编号或三态写法不对、时间无效时抛出 CaptureError。
   */
  function buildCapture(page, form, savedAt) {
    if (
      page === null ||
      typeof page !== "object" ||
      page.ok !== true ||
      typeof page.text !== "string" ||
      page.text.trim() === ""
    ) {
      fail("no_selection", NO_SELECTION_HINT + "，然后点“读取所选内容”。");
    }
    const url = optionalText(page.url, "页面地址");
    if (url === null) {
      fail("page_url_missing", "缺少页面地址，请重新点“读取所选内容”。");
    }
    const values = form !== null && typeof form === "object" ? form : {};
    const prompt = optionalText(values.prompt, "实际提问原文");
    if (prompt === null) {
      fail("prompt_required", "请填写实际提问原文（必填）。");
    }
    const platform = optionalId(values.platform, "平台编号");
    const windowId = optionalId(values.window_id, "时间窗口");
    const queryId = optionalId(values.query_id, "题目编号");
    const model = optionalText(values.model, "模型或产品版本");
    const environment = {
      entry_form: ENTRY_FORM,
      entry_features: optionalText(values.entry_features, "入口特征"),
      logged_in: triState(values.logged_in, "是否登录"),
      history_cleared: triState(values.history_cleared, "是否清除历史"),
      web_search: triState(values.web_search, "是否联网"),
      region: optionalText(values.region, "地域"),
      device: optionalText(values.device, "设备"),
    };
    const operator = optionalText(values.operator, "采集人");
    const savedText = formatSavedAt(savedAt);
    return {
      format: FORMAT,
      capture: CAPTURE,
      saved_at: savedText,
      url: url,
      title: optionalText(page.title, "页面标题"),
      platform: platform,
      window_id: windowId,
      query_id: queryId,
      messages: [
        { role: "user", content: prompt },
        { role: "assistant", content: page.text },
      ],
      sources: dedupeLinks(page.links).sources,
      model: model,
      environment: environment,
      operator: operator,
    };
  }

  /** 建议填写但没填的项：只提醒，不阻止导出。 */
  function captureWarnings(capture) {
    const warnings = [];
    if (capture.model === null && capture.environment.entry_features === null) {
      warnings.push(
        "没有填写模型或产品版本，也没有填写入口特征：版本未知时请填写入口特征（如“网页端默认模式”）。",
      );
    }
    if (capture.platform === null) {
      warnings.push("没有填写平台编号：导入时需要在命令行指定平台编号。");
    }
    if (capture.window_id === null) {
      warnings.push("没有填写时间窗口：导入时需要在命令行指定时间窗口。");
    }
    if (capture.query_id === null) {
      warnings.push("没有填写题目编号：导入时需要在命令行指定对应的题目编号。");
    }
    return warnings;
  }

  /** 文件名的一段：只留英文字母、数字和 . _ -，不出现连续的标点，首尾是字母或数字。 */
  function segment(value) {
    if (typeof value !== "string") {
      return "";
    }
    const cleaned = value
      .replace(/[^A-Za-z0-9._-]/g, "-")
      .replace(/[._-]{2,}/g, "-")
      .replace(/^[._-]+|[._-]+$/g, "");
    return cleaned.slice(0, SEGMENT_LIMIT).replace(/[._-]+$/, "");
  }

  function compactTime(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) {
      return "";
    }
    return value.replace(/[-:]/g, "");
  }

  /** 文件名：geo-capture_平台_窗口_题目_保存时间.json，没填的段省略。 */
  function buildFilename(capture) {
    const parts = ["geo-capture"];
    const candidates = [
      segment(capture.platform),
      segment(capture.window_id),
      segment(capture.query_id),
      compactTime(capture.saved_at),
    ];
    for (const part of candidates) {
      if (part !== "") {
        parts.push(part);
      }
    }
    return parts.join("_") + ".json";
  }

  /** 序列化：两空格缩进、中文不转义、以换行结尾。 */
  function serializeCapture(capture) {
    return JSON.stringify(capture, null, 2) + "\n";
  }

  return {
    FORMAT: FORMAT,
    CAPTURE: CAPTURE,
    NO_SELECTION_HINT: NO_SELECTION_HINT,
    CaptureError: CaptureError,
    buildCapture: buildCapture,
    captureWarnings: captureWarnings,
    dedupeLinks: dedupeLinks,
    parseTriState: parseTriState,
    formatSavedAt: formatSavedAt,
    buildFilename: buildFilename,
    serializeCapture: serializeCapture,
  };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = GeoCapture;
}
