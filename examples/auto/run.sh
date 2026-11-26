#!/bin/sh
# 虚构汽车品牌（澜序汽车）完整预检流程演示脚本。
# 用法：sh examples/auto/run.sh [输出目录]
# 未指定输出目录时在临时目录中运行；全程离线，不联网、不需要任何密钥。

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

# 解析 geo-verify 可执行文件：优先环境变量 GEO_VERIFY_BIN，其次仓库 .venv，再次 PATH
if [ -n "${GEO_VERIFY_BIN:-}" ]; then
  CLI="$GEO_VERIFY_BIN"
elif [ -x "$REPO_ROOT/.venv/bin/geo-verify" ]; then
  CLI="$REPO_ROOT/.venv/bin/geo-verify"
elif command -v geo-verify >/dev/null 2>&1; then
  CLI="geo-verify"
else
  echo "错误：未找到 geo-verify，请先在仓库根目录执行 make setup（或通过 GEO_VERIFY_BIN 指定路径）。" >&2
  exit 2
fi

if [ $# -ge 1 ]; then
  PROJECT_DIR="$1"
else
  PROJECT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/geo-verify-auto.XXXXXX")
fi

# 固定时间戳，便于不同机器或不同目录复算比对
SEAL_NOW="2026-10-02T02:00:00Z"
IMPORT_NOW="2026-10-11T01:00:00Z"
ANNOTATE_NOW="2026-10-11T02:00:00Z"
RECHECK_NOW="2026-10-11T03:00:00Z"
EVIDENCE_NOW="2026-10-11T04:00:00Z"
REPORT_NOW="2026-10-11T05:00:00Z"

echo "==> 1. 初始化虚构汽车品牌样例：$PROJECT_DIR"
"$CLI" init "$PROJECT_DIR" --example auto

echo "==> 2. 检查草稿题库（prompts lint）"
"$CLI" prompts lint "$PROJECT_DIR"

echo "==> 3. 封存题库（prompts seal）"
"$CLI" prompts seal "$PROJECT_DIR" --by "示例封存人（虚构）" --now "$SEAL_NOW"

echo "==> 4. 导入观测记录（import）"
"$CLI" import "$PROJECT_DIR" --input "$PROJECT_DIR/inbox/observations.csv" --now "$IMPORT_NOW"

echo "==> 5. 规则标注：提及、推荐、否定与位次（annotate rules）"
"$CLI" annotate rules "$PROJECT_DIR" --now "$ANNOTATE_NOW"

echo "==> 6. 放入由 geo-facts 产出的事实核验判定样例（inbox/judgments.jsonl -> judgments.jsonl）"
cp "$PROJECT_DIR/inbox/judgments.jsonl" "$PROJECT_DIR/judgments.jsonl"
"$CLI" validate "$PROJECT_DIR/judgments.jsonl" --type judgment

echo "==> 7. 计算全部指标（metrics）"
"$CLI" metrics "$PROJECT_DIR"

echo "==> 8. 双通道交叉验证（crosscheck）"
cc_rc=0
"$CLI" crosscheck "$PROJECT_DIR" || cc_rc=$?
case "$cc_rc" in
  0)
    ;;
  1)
    echo "提示：crosscheck 退出码 1（偏离度超过阈值，已记录到 out/crosscheck.json，继续生成报告）。"
    ;;
  3)
    echo "提示：crosscheck 退出码 3（样本不足，结论为不可判定，继续生成报告）。"
    ;;
  *)
    echo "错误：crosscheck 异常失败（退出码 $cc_rc）。" >&2
    exit "$cc_rc"
    ;;
esac

echo "==> 9. 异常识别与引用域名变化线索（alerts）"
alerts_rc=0
"$CLI" alerts "$PROJECT_DIR" || alerts_rc=$?
case "$alerts_rc" in
  0)
    ;;
  1)
    echo "提示：alerts 退出码 1（检测到异常告警，已写入 out/alerts.json，继续生成报告）。"
    ;;
  3)
    echo "提示：alerts 退出码 3（可比较窗口或日期不足，已写入状态为 insufficient 的 out/alerts.json）。"
    ;;
  *)
    echo "错误：alerts 异常失败（退出码 $alerts_rc）。" >&2
    exit "$alerts_rc"
    ;;
esac

echo "==> 10. 生成抽样复核计划（recheck-plan）"
"$CLI" recheck-plan "$PROJECT_DIR" --now "$RECHECK_NOW"

echo "==> 11. 生成并校验不可篡改证据包（evidence build / verify）"
"$CLI" evidence build "$PROJECT_DIR" --now "$EVIDENCE_NOW"
"$CLI" evidence verify "$PROJECT_DIR"

echo "==> 12. 生成预检报告（report）"
report_rc=0
"$CLI" report "$PROJECT_DIR" --now "$REPORT_NOW" || report_rc=$?
case "$report_rc" in
  0)
    echo "预检报告退出码 0（通过或有条件通过）。"
    ;;
  1)
    echo "提示：预检报告退出码 1（预检结论为不通过，详见报告规则明细）。"
    ;;
  3)
    echo "提示：预检报告退出码 3（预检结论为数据不足，详见报告规则明细）。"
    ;;
  *)
    echo "错误：report 异常失败（退出码 $report_rc）。" >&2
    exit "$report_rc"
    ;;
esac

echo ""
echo "================================================================"
echo "虚构样例全流程执行完成（全部数据为合成数据，不代表真实平台效果）"
echo "项目目录：$PROJECT_DIR"
echo "机器复核报告：$PROJECT_DIR/out/report.json"
echo "可打印报告：  $PROJECT_DIR/out/report.html"
echo "原始记录索引：$PROJECT_DIR/out/observations.csv"
echo "================================================================"
