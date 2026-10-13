"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "cfd-sections.js"), "utf8");

function fixture() {
  class Element {
    constructor() { this.children = []; this.style = {}; this.dataset = {}; this.value = "0"; this.checked = false; this.textContent = ""; this.attributes = {}; this.events = new Map(); this.classList = { toggle() {} }; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, handler) { this.events.set(name, handler); }
    removeEventListener(name) { this.events.delete(name); }
    getContext() { return null; }
    focus() {}
  }
  const elements = new Map();
  const context = vm.createContext({ window: {}, document: { getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }, createElement() { return new Element(); } }, ResizeObserver: class { observe() {} disconnect() {} }, AbortController, setTimeout, clearTimeout, Map, JSON, fetch: async () => ({ ok: true, async json() { return {}; } }) });
  vm.runInContext(source, context);
  const controller = new context.window.CfdSections();
  const viewer = { clearCount: 0, clearAnalysisSection() { this.clearCount += 1; }, setAnalysisSection(data, callback) { this.section = data; this.callback = callback; }, setAnalysisVisible() {}, selectAnalysisRegion(id) { this.selected = id; } };
  controller.caseId = "case"; controller.viewer = viewer; controller.abortController = new AbortController();
  return { controller, viewer, elements, context };
}

const preview = () => ({ source_signature: "field-a", geometry_signature: "geometry-a", plane: { position_fraction: 0.5 }, section_mesh: { positions: [0, 0, 0, 0.001, 0, 0, 0, 0.001, 0], uv_mm: [0, 0, 1, 0, 0, 1], indices: [0, 1, 2] } });
const region = { id: "left-upper", label: "左侧上部区域", side: "left", level: "upper", polygon_uv_mm: [[0, 0], [1, 0], [0, 1]] };
function renderedText(element) { return [element.textContent, ...element.children.map(renderedText)].join(" "); }

test("same-case refresh preserves unsaved polygons while the real source is unchanged", async () => {
  const { controller, viewer } = fixture();
  controller.previewData = preview(); controller.dirty = true;
  controller.polygons.set(region.id, region.polygon_uv_mm);
  controller.request = async () => ({ status: "missing", current_source_signature: "field-a", current_geometry_signature: "geometry-a" });
  await controller.load("case", viewer);
  assert.equal(controller.polygons.size, 1);
  assert.equal(controller.previewData.source_signature, "field-a");
  assert.equal(controller.hasDraft(), true);
});

test("unavailable source on a missing analysis disables old polygons and prevents saving", async () => {
  const { controller, viewer, elements } = fixture();
  controller.previewData = preview(); controller.dirty = true; controller.el.confirm.checked = true;
  controller.polygons.set(region.id, region.polygon_uv_mm);
  controller.request = async () => ({ status: "missing", source_status: "unavailable", current_source_signature: null, current_geometry_signature: null, reason: "最终速度场缺失" });
  await controller.load("case", viewer);
  assert.equal(controller.previewData, null);
  assert.equal(controller.polygons.size, 0);
  assert.equal(elements.get("cfd-section-compute").disabled, true);
  assert.match(elements.get("cfd-section-status").textContent, /速度场缺失/);
});

test("a changed field signature clears annotation before requesting a new real section", async () => {
  const { controller, viewer } = fixture();
  controller.previewData = preview(); controller.dirty = true;
  controller.polygons.set(region.id, region.polygon_uv_mm);
  let previewCalls = 0;
  controller.request = async () => ({ status: "stale", current_source_signature: "field-b", current_geometry_signature: "geometry-a" });
  controller.preview = async () => { previewCalls += 1; assert.equal(controller.previewData, null); assert.equal(controller.polygons.size, 0); };
  await controller.load("case", viewer);
  assert.equal(previewCalls, 1);
  assert.equal(controller.el.compute.disabled, true);
});

test("zero forward denominator is shown as unavailable and uncovered anatomy is shown separately", () => {
  const { controller, elements } = fixture();
  const data = { ...preview(), status: "ready", annotation: { regions: [region], confirmed: true }, totals: { forward_lpm: 0, reverse_lpm: 0, net_lpm: 0 }, regions: [{ ...region, forward_lpm: 0, reverse_lpm: 0, net_lpm: 0, percent_of_side: null, percent_of_section: null }], sides: [], unassigned: { forward_lpm: 0, reverse_lpm: 0, net_lpm: 0, percent_of_section: null }, coverage_complete: true, validation: { status: "pending", coverage_status: "complete" } };
  controller.useResult(data);
  assert.equal(controller.regionButtons.get(region.id).value.textContent, "占比不可用");
  assert.doesNotMatch(renderedText(elements.get("cfd-section-result")), /待标注完整/);
  controller.result = { ...data, coverage_complete: false, validation: { status: "pending", coverage_status: "incomplete" } };
  controller.renderResult();
  assert.equal(controller.regionButtons.get(region.id).value.textContent, "待标注完整");
  assert.match(renderedText(elements.get("cfd-section-result")), /未标注区正向流量/);
});

test("3D selection of the unassigned mesh displays its actual flux and total flux", () => {
  const { controller, viewer, elements } = fixture();
  controller.useResult({ ...preview(), status: "ready", annotation: { regions: [region], confirmed: true }, totals: { forward_lpm: 10, reverse_lpm: 1, net_lpm: 9 }, regions: [], sides: [], unassigned: { forward_lpm: 2, reverse_lpm: 0.5, net_lpm: 1.5, percent_of_section: 20 }, coverage_complete: false, validation: { status: "pending" } });
  viewer.callback("unassigned");
  assert.equal(viewer.selected, "unassigned");
  const text = renderedText(elements.get("cfd-section-result"));
  assert.match(text, /未标注区域/);
  assert.match(text, /2\.00 L\/min/);
  assert.match(text, /20\.0%/);
  assert.match(text, /全截面正向流量 10\.00 L\/min/);
  assert.equal(controller.el["add-point"].disabled, true);
});

test("concave boundary is rejected rather than rearranged into a misleading region", () => {
  const { controller } = fixture();
  controller.previewData = preview();
  controller.draft = [[0, 0], [2, 0], [1, 0.5], [2, 2], [0, 2]];
  controller.closePolygon();
  assert.equal(controller.polygons.size, 0);
  assert.match(controller.el.status.textContent, /凸多边形/);
  controller.draft = [[0, 0], [2, 0], [2, 2], [0, 2]];
  controller.closePolygon();
  assert.equal(controller.polygons.size, 1);
  assert.equal(controller.draft.length, 0);
});

test("editing never keeps old colored region overlays or old percentages visible", () => {
  const { controller, viewer } = fixture();
  controller.previewData = { ...preview(), region_meshes: [{ id: "left-upper" }] };
  controller.result = controller.previewData;
  controller.addPoint([0, 0]);
  assert.equal(controller.result, null);
  assert.equal(viewer.section.region_meshes, undefined);
  assert.equal(controller.el.confirm.checked, false);
});

test("dispose removes bound listeners and aborts in-flight case requests", () => {
  const { controller } = fixture();
  const signal = controller.abortController.signal;
  const bindings = [...controller.listeners];
  controller.dispose();
  assert.equal(signal.aborted, true);
  assert.equal(controller.listeners.length, 0);
  assert.ok(bindings.every(([element, name]) => !element.events.has(name)));
});
test("pipeline errors surface their structured reason", async () => {
  const { controller, context } = fixture();
  context.fetch = async () => ({ ok: false, status: 409, async json() { return { detail: { reason: "最终速度场正在更新" } }; } });
  await assert.rejects(() => controller.request(""), /最终速度场正在更新/);
});
