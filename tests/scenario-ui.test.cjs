"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
function productionFunction(name) {
  const match = source.match(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  assert.ok(match, `missing function ${name}`);
  const start = match.index;
  return source.slice(start, source.indexOf("\n}", start) + 2);
}

const context = vm.createContext({
  escapeHtml: (value) => String(value).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])),
  fmt: (value, suffix = "") => value == null ? null : `${value}${suffix}`,
});
vm.runInContext([
  productionFunction("renderScenarioPanel"),
  productionFunction("renderScenarioComparison"),
].join("\n"), context);

test("scenario panel exposes the closed-loop order and upload contract", () => {
  const html = context.renderScenarioPanel({ case_id: "case-1" });
  assert.match(html, /上传 → 人工摆位 → 审核 → 独立 CFD → 前后对比/);
  assert.match(html, /name="model"/);
  assert.match(html, /name="unit"/);
  assert.match(html, /data-scenario-upload-status/);
  assert.match(html, /研究预览/);
});

test("scenario cards include a real overlay viewport and task status", () => {
  const target = { innerHTML: "" };
  const listContext = vm.createContext({
    $: () => target,
    escapeHtml: (value) => String(value),
    destroyScenarioViewers() {},
  });
  vm.runInContext([
    productionFunction("scenarioPill"),
    productionFunction("scenarioVectorInputs"),
    productionFunction("renderScenarioList"),
  ].join("\n"), listContext);
  listContext.renderScenarioList([{
    scenario_id: "plug-test", current_version: 2,
    placement: { confirmed: true }, review: { approved: true },
    base_status: "current", cfd: { status: "running" },
  }]);
  assert.match(target.innerHTML, /data-scenario-viewer/);
  assert.match(target.innerHTML, /气道 × 鼻塞叠加预览/);
  assert.match(target.innerHTML, /data-scenario-task-status/);
});

test("scenario comparison keeps engineering review as a hard boundary", () => {
  const output = { hidden: true, innerHTML: "" };
  context.renderScenarioComparison(output, {
    status: "checked",
    baseline: { delta_pressure: 10, pressure_unit: "Pa" },
    scenario: { delta_pressure: 12, pressure_unit: "Pa" },
    delta: { pressure_percent: 20, flow_percent: 0 },
    eligibility: { numeric_ok: true },
    notes: ["必须完成网格敏感性和工程师复核"],
  });
  assert.equal(output.hidden, false);
  assert.match(output.innerHTML, /数值检查暂通过，仍需网格敏感性和 CFD 工程师复核/);
  assert.match(output.innerHTML, /20 %/);
  assert.match(output.innerHTML, /必须完成网格敏感性和工程师复核/);
});

test("unsaved placement disables review and CFD actions until a new version is saved", () => {
  const controls = [
    { dataset: { scenarioBaseDisabled: "false" }, disabled: false },
    { dataset: { scenarioBaseDisabled: "true" }, disabled: true },
  ];
  const card = { querySelectorAll: () => controls };
  const actionContext = vm.createContext({});
  vm.runInContext(productionFunction("syncScenarioCardActions"), actionContext);
  actionContext.syncScenarioCardActions(card, true);
  assert.deepEqual(controls.map((control) => control.disabled), [true, true]);
  actionContext.syncScenarioCardActions(card, false);
  assert.deepEqual(controls.map((control) => control.disabled), [false, true]);
});
