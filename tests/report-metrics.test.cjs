"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
function productionFunction(name) {
  const start = source.match(new RegExp(`^function ${name}\\(`, "m")).index;
  return source.slice(start, source.indexOf("\n}", start) + 2);
}
function fixture() {
  const elements = new Map();
  const context = vm.createContext({
    fmt: (value, unit = "") => `${value}${unit}`,
    $: (id) => {
      if (!elements.has(id)) elements.set(id, { innerHTML: "" });
      return elements.get(id);
    },
  });
  vm.runInContext(["escapeHtml", "reportNumber", "reportMetricValue", "reportMetric", "reportStatus", "renderReportSummary", "renderCfdValidation"].map(productionFunction).join("\n"), context);
  return { context, elements };
}
test("missing numeric values remain missing while actual zero remains zero", () => {
  const { context } = fixture();
  for (const value of [null, undefined, "", "  ", true, false, [], {}, NaN, Infinity]) assert.equal(context.reportNumber(value), null);
  assert.equal(context.reportNumber(0), 0);
  assert.equal(context.reportNumber("0"), 0);
});
test("requested flow is never labeled as actual and converted drop uses its own Pa unit", () => {
  const { context } = fixture();
  const html = context.renderReportSummary({ baseline_flow: {
    status: "pending_validation", requested_flow_lpm: 15, flow_lpm: null,
    pressure_drop: { value: 12, unit: "Pa" },
    velocity_distribution: { pressure: { unit: "m2/s2" } },
  } });
  assert.match(html, /设定入口流量<\/span><strong>15 L\/min/);
  assert.match(html, /实际入口流量<\/span><strong>未记录/);
  assert.match(html, /入口至出口压降<\/span><strong>12 Pa/);
  assert.doesNotMatch(html, /实际入口流量<\/span><strong>15|12 m2\/s2/);
});
test("pressure inlet does not fall back to a legacy case flow", () => {
  const { context } = fixture();
  const html = context.renderReportSummary({ cfd_prep: { data: { total_flow_lpm: 15 } }, baseline_flow: {
    status: "pending_validation", requested_flow_lpm: null, flow_lpm: null,
    pressure_drop: { value: 12, unit: "Pa" },
  } });
  assert.match(html, /设定入口流量<\/span><strong>未记录/);
  assert.doesNotMatch(html, /设定入口流量<\/span><strong>15/);
});
test("missing conservation error is not displayed as zero", () => {
  const { context, elements } = fixture();
  context.renderCfdValidation({ metrics: { mass_conservation_error_percent: null } });
  assert.match(elements.get("cfd-results-validation").innerHTML, /质量守恒尚无记录/);
  context.renderCfdValidation({ metrics: { mass_conservation_error_percent: 0 } });
  assert.match(elements.get("cfd-results-validation").innerHTML, /0\.0000%/);
});
test("stale region results cannot show previous percentages and unavailable resistance stays absent", () => {
  const { context } = fixture();
  const html = context.renderReportSummary({ baseline_flow: {
    status: "pending_validation", resistance: { status: "not_available", value: null, reason: "缺少空气密度" },
    region_flow: { status: "stale", reason: "截面结果已过期", regions: [{ label: "旧区域", forward_lpm: 99, percent_of_section: 99 }] },
  } });
  assert.match(html, /模拟鼻阻力<\/span><strong>未计算|缺少空气密度|截面结果已过期/);
  assert.doesNotMatch(html, /99|旧区域/);
});
test("unassigned flow remains visible without renormalizing annotated regions", () => {
  const { context } = fixture();
  const html = context.renderReportSummary({ baseline_flow: {
    status: "pending_validation", region_flow: { status: "ready", coverage_complete: false,
      totals: { forward_lpm: 10, reverse_lpm: 2, net_lpm: 8 },
      regions: [{ id: "left-upper", label: "左侧上部区域", forward_lpm: 4, percent_of_section: 40, percent_of_side: null }],
      unassigned: { id: "unassigned", label: "未标注区域", forward_lpm: 6, percent_of_section: 60 },
    },
  } });
  assert.match(html, /左侧上部区域<\/th><td>4 L\/min<\/td><td>40 %<\/td><td>待补齐分区/);
  assert.match(html, /未标注区域<\/th><td>6 L\/min<\/td><td>60 %<\/td><td>—/);
  assert.match(html, /整截面正向流量 10 L\/min；回流 2 L\/min；净流量 8 L\/min/);
});
