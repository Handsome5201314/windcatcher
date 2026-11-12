"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");
const source = fs.readFileSync(path.join(root, "static", "quick-preview.js"), "utf8");

function node() {
  return {
    innerHTML: "", textContent: "", hidden: false, disabled: false,
    attributes: {}, listeners: {}, classList: { toggle() {}, add() {}, remove() {} },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    scrollIntoView() {}, focus() {},
  };
}

function harness() {
  const ids = [
    "quick-preview-panel", "quick-preview-case", "quick-preview-source", "quick-preview-stages",
    "quick-preview-primary", "quick-preview-rebuild", "quick-preview-quality", "quick-preview-metrics", "quick-preview-results",
    "advanced-workflow-panel", "quick-preview-evidence", "quick-preview-evidence-content",
  ];
  const nodes = Object.fromEntries(ids.map(id => [id, node()]));
  const context = vm.createContext({
    window: {},
    document: { getElementById: id => nodes[id] || null, querySelectorAll: () => [] },
    setTimeout, clearTimeout,
  });
  vm.runInContext(source, context, { filename: "static/quick-preview.js" });
  return { api: context.window.WindcatcherQuickPreview, nodes };
}

test("workbench exposes a primary quick preview and keeps the old workflow in a secondary panel", () => {
  assert.match(index, /id="quick-preview-panel"/);
  assert.match(index, /id="advanced-workflow-panel"/);
  assert.match(index, /id="quick-preview-primary"/);
  assert.match(index, /id="quick-preview-rebuild"/);
  assert.match(index, /完整工程流程/);
  assert.match(index, /id="quick-preview-evidence"/);
});

test("failed CFD exposes the rebuild action in the simple preview", () => {
  const h = harness();
  let clicks = 0;
  h.api.bind({ onRebuild: () => { clicks += 1; } });
  const data = { case_id: "case-a", stages: {
    segment: { status: "ok" }, surface: { status: "ok" },
    cfd_prep: { status: "ok" }, cfd_run: { status: "failed" },
  } };
  h.api.render(data, { action: { label: "开始气流求解" }, rebuild: { visible: true, disabled: false } });
  assert.equal(h.nodes["quick-preview-rebuild"].hidden, false);
  assert.equal(h.nodes["quick-preview-rebuild"].disabled, false);
  h.nodes["quick-preview-rebuild"].listeners.click();
  assert.equal(clicks, 1);
  h.api.render(data, { action: { label: "开始气流求解" }, rebuild: { visible: false } });
  assert.equal(h.nodes["quick-preview-rebuild"].hidden, true);
  assert.equal(h.nodes["quick-preview-rebuild"].disabled, true);
});

test("quick preview renders only recorded values and labels unresolved stages", () => {
  const h = harness();
  h.api.render({
    case_id: "case-20261108-100000-aaaa",
    stages: {
      import: { status: "ok" }, qc: { status: "ok" },
      segment: { status: "warn", data: { volume_ml: 12.4, need_human_confirm: true } },
      surface: { status: "pending" }, cfd_prep: { status: "pending" }, cfd_run: { status: "pending" }, report: { status: "pending" },
    },
  }, { action: { label: "打开专家处理", disabled: false } });
  assert.equal(h.nodes["quick-preview-panel"].hidden, false);
  assert.match(h.nodes["quick-preview-case"].textContent, /case-20261108-100000-aaaa/);
  assert.match(h.nodes["quick-preview-stages"].innerHTML, /待复核/);
  assert.match(h.nodes["quick-preview-metrics"].innerHTML, /12\.4/);
  assert.match(h.nodes["quick-preview-metrics"].innerHTML, /未生成/);
  assert.doesNotMatch(h.nodes["quick-preview-metrics"].innerHTML, /3\.42|18\.6/);
  assert.match(h.nodes["quick-preview-evidence-content"].innerHTML, /HU/);
  assert.match(h.nodes["quick-preview-evidence-content"].innerHTML, /主流程基线<\/dt><dd>未记录<\/dd>/);
  assert.match(h.nodes["quick-preview-evidence-content"].innerHTML, /实验分支<\/dt><dd>未记录<\/dd>/);
});

test("quick preview does not claim CFD preview before a real run is recorded", () => {
  const h = harness();
  h.api.render({ case_id: "case-a", stages: {
    import: { status: "ok" }, qc: { status: "ok" }, segment: { status: "ok" },
    surface: { status: "ok", data: { verdict: "pass" } }, cfd_prep: { status: "ok" },
    cfd_run: { status: "pending" }, report: { status: "pending" },
  } }, { action: { label: "开始气流求解", disabled: false } });
  assert.match(h.nodes["quick-preview-results"].textContent, /求解完成后显示真实流场/);
  assert.doesNotMatch(h.nodes["quick-preview-results"].textContent, /流场已生成/);
});

test("missing numeric results never render as zero but a recorded zero remains visible", () => {
  const h = harness();
  const stages = {
    segment: { status: "ok", data: { volume_ml: null } },
    surface: { status: "pending", data: { volume_deviation_percent: null } },
    cfd_prep: { status: "pending", data: { mean_inlet_velocity_mps: null, total_flow_lpm: null } },
    cfd_run: { status: "pending" },
  };
  h.api.render({ case_id: "case-a", stages });
  let metrics = h.nodes["quick-preview-metrics"].innerHTML;
  assert.match(metrics, /气道体积<\/span><strong[^>]*>未生成<\/strong>/);
  assert.match(metrics, /入口工况<\/span><strong[^>]*>未生成<\/strong>/);
  assert.match(metrics, /掩膜-模型体积差<\/span><strong[^>]*>未生成<\/strong>/);
  stages.segment.data.volume_ml = 0;
  stages.surface.data.volume_deviation_percent = 0;
  stages.cfd_prep.data.mean_inlet_velocity_mps = 0;
  h.api.render({ case_id: "case-a", stages });
  metrics = h.nodes["quick-preview-metrics"].innerHTML;
  assert.match(metrics, /气道体积<\/span><strong[^>]*>0 mL<\/strong>/);
  assert.match(metrics, /入口工况<\/span><strong[^>]*>0 m\/s<\/strong>/);
  assert.match(metrics, /掩膜-模型体积差<\/span><strong[^>]*>0 %<\/strong>/);
  stages.cfd_prep.data.mean_inlet_velocity_mps = " ";
  stages.cfd_prep.data.total_flow_lpm = 12;
  h.api.render({ case_id: "case-a", stages });
  assert.match(h.nodes["quick-preview-metrics"].innerHTML, /入口工况<\/span><strong[^>]*>12 L\/min<\/strong>/);
});

test("candidate confirmation and blocked or failed stages stay visible in the main flow", () => {
  const h = harness();
  const stages = {
    import: { status: "ok" }, qc: { status: "ok" },
    segment: { status: "ok", data: { need_human_confirm: true } },
    surface: { status: "blocked", message: "缺少三个局部端面" },
    cfd_prep: { status: "pending" }, cfd_run: { status: "failed" },
  };
  h.api.render({ case_id: "case-a", stages });
  assert.match(h.nodes["quick-preview-stages"].innerHTML, /主鼻腔候选已生成 · 待确认气道范围/);
  assert.match(h.nodes["quick-preview-stages"].innerHTML, /几何处理已暂停 · 查看原因/);
  assert.match(h.nodes["quick-preview-stages"].innerHTML, /气流求解失败 · 查看原因/);
  assert.match(h.nodes["quick-preview-quality"].innerHTML, /缺少三个局部端面/);
  assert.match(h.nodes["quick-preview-quality"].innerHTML, /quick-preview-quality-mark" aria-hidden="true">!<\/div>/);
  assert.match(h.nodes["quick-preview-results"].textContent, /气流求解失败 · 暂无流场/);
  assert.match(h.nodes["quick-preview-stages"].innerHTML, /quick-preview-stage is-warn/);
});
