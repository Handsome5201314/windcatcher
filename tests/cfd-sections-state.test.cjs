"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "cfd-sections.js"), "utf8");

function fixture() {
  class Element {
    constructor() {
      this.children = []; this.style = {}; this.dataset = {}; this.value = "50"; this.checked = true;
      this.textContent = ""; this.attributes = {}; this.events = new Map(); this.disabled = false;
      this.hidden = false; this.classList = { toggle() {} };
    }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, handler) { this.events.set(name, handler); }
    removeEventListener(name) { this.events.delete(name); }
  }
  const elements = new Map();
  const context = vm.createContext({
    window: {}, document: { getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }, createElement() { return new Element(); } },
    ResizeObserver: class { observe() {} disconnect() {} }, AbortController, setTimeout, clearTimeout, Map, JSON,
    fetch: async () => ({ ok: true, async json() { return {}; } }),
  });
  vm.runInContext(source, context);
  const controller = new context.window.CfdSections();
  const viewer = {
    clearCount: 0, busy: [], pending: [], sections: [],
    clearAnalysisSection() { this.clearCount += 1; },
    setAnalysisSection(data, callbacks) { this.section = data; this.callbacks = callbacks; this.sections.push(data); },
    setAnalysisVisible() {}, setAnalysisBusy(value) { this.busy.push(value); },
    setAnalysisPositionFraction(value) { this.position = value; }, setAnalysisPending(value) { this.pending.push(value); },
  };
  controller.caseId = "case"; controller.viewer = viewer; controller.abortController = new AbortController();
  return { controller, viewer, elements, context };
}

function preview(fraction = 0.5) {
  return {
    status: "preview", source_signature: "field-a", geometry_signature: "geometry-a",
    plane: { position_fraction: fraction, position_coordinate_mm: fraction * 100, position_bounds_mm: [0, 100], normal_lps: [0, 1, 0] },
    section_mesh: { positions: [0, 0, 0, 0.001, 0, 0, 0, 0.001, 0], uv_mm: [0, 0, 1, 0, 0, 1], indices: [0, 1, 2], speeds: [1, 2, 3] },
  };
}

function result(fraction = 0.5) {
  return {
    ...preview(fraction), status: "ready", analysis_type: "whole_section", analysis_signature: "analysis-a",
    totals: { area_mm2: 12, forward_lpm: 10, reverse_lpm: 1, net_lpm: 9 },
    velocity: { mean_mps: 1.5, p95_mps: 2.8, max_mps: 3 }, validation: { status: "pending", reasons: [] },
  };
}

function renderedText(element) { return [element.textContent, ...element.children.map(renderedText)].join(" "); }

test("loading a missing result previews the current section without region controls", async () => {
  const { controller, viewer } = fixture();
  controller.request = async (path) => path === "" ? { status: "missing", source_signature: "field-a", geometry_signature: "geometry-a" } : preview();
  await controller.load("case", viewer);
  assert.equal(controller.previewData.status, "preview");
  assert.equal(viewer.section.plane.position_fraction, 0.5);
  assert.equal(controller.el.regions, undefined);
  assert.equal(controller.el.canvas, undefined);
});

test("releasing the model drag submits one whole-section analysis request", async () => {
  const { controller, viewer } = fixture();
  controller.previewData = preview();
  const calls = [];
  controller.request = async (path, options) => {
    calls.push([path, JSON.parse(options.body)]);
    return result(calls[0][1].position_fraction);
  };
  viewer.callbacks = controller.callbacks();
  viewer.callbacks.onPositionChange(0.62);
  assert.equal(controller.result, null);
  await viewer.callbacks.onPositionCommit(0.62);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/analyze");
  assert.equal(calls[0][1].position_fraction, 0.62);
  assert.equal(calls[0][1].source_signature, "field-a");
  assert.equal(controller.result.analysis_type, "whole_section");
});

test("slider change uses the same analyze endpoint and renders whole-section metrics", async () => {
  const { controller, viewer, elements } = fixture();
  controller.previewData = preview();
  controller.request = async (path) => path === "/analyze" ? result(0.7) : preview(0.7);
  await controller.selectPosition(0.7);
  assert.equal(elements.get("cfd-section-position").value, "70");
  const text = renderedText(elements.get("cfd-section-result"));
  assert.match(text, /截面面积/);
  assert.match(text, /平均 \/ P95 \/ 最大流速/);
  assert.match(text, /10\.00 \/ 1\.00 L\/min/);
  assert.equal(viewer.sections.at(-1).analysis_type, "whole_section");
});

test("stale source clears the old preview and reads a fresh section", async () => {
  const { controller, viewer } = fixture();
  controller.previewData = preview();
  let previewCalls = 0;
  controller.request = async (path) => {
    if (path === "/analyze") throw new Error("截面预览已过期，请刷新");
    previewCalls += 1; return { ...preview(), source_signature: "field-b" };
  };
  await controller.selectPosition(0.8);
  assert.equal(previewCalls, 1);
  assert.equal(controller.previewData.source_signature, "field-b");
  assert.equal(viewer.clearCount, 0);
});

test("dispose aborts in-flight case requests and removes listeners", () => {
  const { controller } = fixture();
  const signal = controller.abortController.signal;
  const bindings = [...controller.listeners];
  controller.dispose();
  assert.equal(signal.aborted, true);
  assert.equal(controller.listeners.length, 0);
  assert.ok(bindings.every(([element, name]) => !element.events.has(name)));
});
