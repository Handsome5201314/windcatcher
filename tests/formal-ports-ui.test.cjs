"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { createHash, webcrypto } = require("node:crypto");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");

function functionSource(name) {
  const location = source.indexOf("function " + name + "(");
  assert.ok(location >= 0, name + " exists");
  const start = source.lastIndexOf("\n", location) + 1;
  const end = source.indexOf("\n}", location);
  assert.ok(end > start, name + " is complete");
  return source.slice(start, end + 2);
}

function element() {
  const classes = new Set();
  return {
    hidden: false, disabled: false, textContent: "", children: [],
    setAttribute() {}, addEventListener() {}, removeAttribute() {},
    classList: {
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
      add(name) { classes.add(name); },
      contains(name) { return classes.has(name); },
    },
    replaceChildren() { this.children = []; },
    append(...children) { this.children.push(...children); },
  };
}

function renderHarness(data, options = {}) {
  const nodes = Object.fromEntries([
    "endplane-formal-ports-status", "endplane-formal-ports-list", "endplane-formal-ports-refresh",
  ].map((id) => [id, element()]));
  const ctx = vm.createContext({
    state: { endplanes: {
      formalPorts: { status: options.status || "ready", data, error: options.error || null },
      dirty: Boolean(options.dirty),
    } },
    endplaneContext: () => ({ caseId: "case-test", scope: "main_airway", version: 7, key: "case-test|main_airway|7" }),
    renderFormalPortVisual: () => {},
    $: (id) => nodes[id],
    document: { createElement: () => element() },
  });
  vm.runInContext(functionSource("renderFormalPorts"), ctx);
  ctx.renderFormalPorts();
  return { nodes, ctx };
}

function formalPorts() {
  return {
    status: "ready", case_id: "case-test", scope: "main_airway",
    mask_version: 7, mesh_hash: "mesh-test", quality_verdict: "pass",
    ports: ["left", "right", "posterior"].map((side, index) => ({
      side, port_id: "case-test-port-" + side,
      cap_face_ids: [index * 2, index * 2 + 1], area_mm2: 31 + index,
    })),
  };
}

test("the experimental mesh markup is collapsed after the formal save controls", () => {
  const save = html.indexOf('id="endplane-save-bar"');
  const saveFallback = html.indexOf('class="endplane-save-bar"');
  const experiment = html.indexOf('<details id="endplane-opening-markup"');
  assert.ok(experiment > Math.max(save, saveFallback));
  assert.match(html.slice(experiment, experiment + 210), /实验工具：三维点选与画圈（草稿，不参与正式裁切）/);
  assert.match(html, /id="endplane-formal-ports-status"/);
  assert.match(html, /id="endplane-local-save-button"/);
  assert.match(html, /id="endplane-preview-model"/);
});

test("the formal status never treats an experimental draft as a generated CFD port", () => {
  const { nodes } = renderHarness({ status: "not_generated", ports: [], reason: "尚未生成正式网格" });
  assert.equal(nodes["endplane-formal-ports-list"].hidden, true);
  assert.match(nodes["endplane-formal-ports-status"].textContent, /尚未生成正式网格/);
});

test("current three-cap data is read-only and shows each backend port ID", () => {
  const { nodes } = renderHarness(formalPorts());
  const list = nodes["endplane-formal-ports-list"];
  assert.equal(list.hidden, false);
  assert.equal(list.children.length, 3);
  assert.deepEqual(list.children.map((item) => item.children[1].textContent), [
    "case-test-port-left", "case-test-port-right", "case-test-port-posterior",
  ]);
  assert.match(nodes["endplane-formal-ports-status"].textContent, /CFD 数值质量仍需另行检查/);
  const dirty = renderHarness(formalPorts(), { dirty: true });
  assert.match(dirty.nodes["endplane-formal-ports-status"].textContent, /上次保存/);
});

test("stale or duplicate formal ports do not appear as a valid three-port set", () => {
  const stale = formalPorts();
  stale.mask_version = 6;
  const staleUi = renderHarness(stale);
  assert.equal(staleUi.nodes["endplane-formal-ports-list"].hidden, true);
  assert.match(staleUi.nodes["endplane-formal-ports-status"].textContent, /旧掩膜版本/);
  const duplicate = formalPorts();
  duplicate.ports[2].side = "left";
  const duplicateUi = renderHarness(duplicate);
  assert.equal(duplicateUi.nodes["endplane-formal-ports-list"].hidden, true);
  assert.match(duplicateUi.nodes["endplane-formal-ports-status"].textContent, /记录不完整/);
});

test("formal display rejects mesh data without a passing geometry verdict", () => {
  const missing = formalPorts();
  missing.mesh_hash = "";
  const missingUi = renderHarness(missing);
  assert.equal(missingUi.nodes["endplane-formal-ports-list"].hidden, true);
  assert.match(missingUi.nodes["endplane-formal-ports-status"].textContent, /几何质检/);
  const failed = formalPorts();
  failed.quality_verdict = "fail";
  assert.equal(renderHarness(failed).nodes["endplane-formal-ports-list"].hidden, true);
});

test("formal endpoint failures stay visible with a retry action", () => {
  const { nodes } = renderHarness(null, { status: "error", error: "网格版本不匹配" });
  assert.match(nodes["endplane-formal-ports-status"].textContent, /网格版本不匹配/);
  assert.equal(nodes["endplane-formal-ports-refresh"].disabled, false);
  assert.equal(nodes["endplane-formal-ports-list"].hidden, true);
});

test("formal data loads from the case-scoped read-only endpoint", async () => {
  const calls = [];
  const context = { caseId: "case-test", scope: "main_airway", version: 7, key: "case-test|main_airway|7" };
  const formal = { status: "idle", data: null, error: null, requestId: 0 };
  const ctx = vm.createContext({
    state: { endplanes: { formalPorts: formal } },
    endplaneContext: () => context,
    renderFormalPorts: () => {},
    resetFormalPortVisual: () => {},
    api: async (url) => { calls.push(url); return formalPorts(); },
  });
  vm.runInContext(functionSource("loadFormalPorts"), ctx);
  await ctx.loadFormalPorts(context);
  assert.deepEqual(calls, ["/api/cases/case-test/segment/ports/formal?scope=main_airway"]);
  assert.equal(formal.status, "ready");
  assert.equal(formal.data.ports.length, 3);
});

test("blocked formal geometry is not silently shown as successful", async () => {
  const context = { caseId: "case-test", scope: "main_airway", version: 7, key: "case-test|main_airway|7" };
  const formal = { status: "idle", data: null, error: null, requestId: 0 };
  const ctx = vm.createContext({
    state: { endplanes: { formalPorts: formal } },
    endplaneContext: () => context,
    renderFormalPorts: () => {},
    resetFormalPortVisual: () => {},
    api: async () => { const error = new Error("409"); error.detail = { reason: "封盖面未通过质检" }; throw error; },
  });
  vm.runInContext(functionSource("loadFormalPorts"), ctx);
  await ctx.loadFormalPorts(context);
  assert.equal(formal.status, "error");
  assert.equal(formal.data, null);
  assert.equal(formal.error, "封盖面未通过质检");
});

test("formal cap triangles use exact saved STL face indices and reject bad indices", () => {
  const ctx = vm.createContext({ Float32Array, Set, Error, Number, Array });
  vm.runInContext(functionSource("formalPortCapPositions"), ctx);
  const positions = Float32Array.from({ length: 27 }, (_, index) => index);
  assert.deepEqual([...ctx.formalPortCapPositions(positions, [2, 0])],
    [...positions.slice(18, 27), ...positions.slice(0, 9)]);
  assert.throws(() => ctx.formalPortCapPositions(positions, [3]), /不一致/);
  assert.throws(() => ctx.formalPortCapPositions(positions, [1, 1]), /不一致/);
});

test("formal CT boundary projects in the server LPS reslice basis", () => {
  const ctx = vm.createContext({});
  vm.runInContext(functionSource("formalPortProjectionPoints"), ctx);
  const points = ctx.formalPortProjectionPoints(
    [[-10, 0, 0], [0, 0, 10], [10, 0, 0]], [0, 0, 0], [0, 1, 0], 384, 120);
  assert.ok(Math.abs(points[0][0] - 223.5) < 1e-6);
  assert.ok(Math.abs(points[1][1] - 159.5) < 1e-6);
  assert.throws(() => ctx.formalPortProjectionPoints(
    [[0, 0, 0], [0, 1, 1], [1, 0, 0]], [0, 0, 0], [0, 1, 0]), /偏离/);
});

test("formal preview labels unsaved edits as a saved result", () => {
  const ids = ["endplane-formal-preview", "endplane-formal-preview-status", "endplane-formal-preview-grid"];
  const nodes = Object.fromEntries(ids.map(id => [id, element()]));
  let resized = 0;
  const ctx = vm.createContext({ $: id => nodes[id], state: { endplanes: {
    dirty: true, formalPorts: { status: "ready", data: formalPorts(),
      visual: { status: "ready", side: "left", contextKey: "case-test|main_airway|7",
        viewer: { resize: () => resized++ } } },
  } }, endplaneContext: () => ({ caseId: "case-test", scope: "main_airway", version: 7,
    key: "case-test|main_airway|7" }) });
  vm.runInContext(functionSource("renderFormalPortVisual"), ctx);
  ctx.renderFormalPortVisual();
  assert.equal(nodes["endplane-formal-preview"].hidden, false);
  assert.equal(nodes["endplane-formal-preview-grid"].hidden, false);
  assert.match(nodes["endplane-formal-preview-status"].textContent, /上次保存结果/);
  assert.equal(resized, 1);
});

test("formal preview hides immediately when the case or mask changes", () => {
  const ids = ["endplane-formal-preview", "endplane-formal-preview-status", "endplane-formal-preview-grid"];
  const nodes = Object.fromEntries(ids.map(id => [id, element()]));
  const data = formalPorts();
  let current = { caseId: "case-test", scope: "main_airway", version: 7,
    key: "case-test|main_airway|7" };
  const ctx = vm.createContext({ $: id => nodes[id], endplaneContext: () => current,
    state: { endplanes: { formalPorts: { status: "ready", data,
      visual: { status: "ready", side: "left", contextKey: current.key } } } } });
  vm.runInContext(functionSource("renderFormalPortVisual"), ctx);
  ctx.renderFormalPortVisual();
  assert.equal(nodes["endplane-formal-preview"].hidden, false);
  current = { ...current, caseId: "case-next", key: "case-next|main_airway|7" };
  ctx.renderFormalPortVisual();
  assert.equal(nodes["endplane-formal-preview"].hidden, true);
  current = { caseId: "case-test", scope: "main_airway", version: 8,
    key: "case-test|main_airway|8" };
  ctx.renderFormalPortVisual();
  assert.equal(nodes["endplane-formal-preview"].hidden, true);
});

function formalViewHarness({ returnedHash, claimedHash, sectionVersion = "7" } = {}) {
  const buffer = Uint8Array.from([1, 2, 3, 4, 5]).buffer;
  const meshHash = claimedHash || createHash("sha256").update(Buffer.from(buffer)).digest("hex");
  const manifest = { ...formalPorts(), case_id: "case-test", scope: "main_airway", mesh_hash: meshHash };
  manifest.ports[0] = { ...manifest.ports[0], cap_face_ids: [1],
    boundary_loop_lps_mm: [[-1, 0, 0], [0, 0, 1], [1, 0, 0]],
    plane_point_lps_mm: [0, 0, 0], normal_lps: [0, 1, 0] };
  const formal = { status: "ready", data: structuredClone(manifest), visual: { status: "idle" } };
  const state = { endplanes: { formalPorts: formal } };
  const calls = [], shown = [];
  const image = { onload: null, onerror: null, set src(value) {
    this._src = value; queueMicrotask(() => this.onload?.());
  } };
  let context = { caseId: "case-test", scope: "main_airway", version: 7,
    key: "case-test|main_airway|7" };
  const position = Float32Array.from({ length: 18 }, (_, index) => index);
  const geometry = { getAttribute: () => ({ array: position }), dispose() {} };
  const ctx = vm.createContext({
    state, endplaneContext: () => context, renderFormalPorts: () => {},
    resetFormalPortVisual: () => { state.endplanes.formalPorts.visual = { status: "idle", requestId: 0 }; },
    api: async url => { calls.push(url); return { ...manifest, mesh_hash: returnedHash || meshHash }; },
    fetch: async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith("/model")) return { ok: true, arrayBuffer: async () => buffer };
      return { ok: true, headers: { get: key => ({ "X-Mask-Version": sectionVersion,
        "X-Model-Scope": "main_airway" })[key] }, blob: async () => ({}) };
    },
    crypto: webcrypto, Uint8Array, AbortController, URL: { createObjectURL: () => "blob:ct" },
    THREE: { STLLoader: class { parse() { return geometry; } } },
    $: id => id === "endplane-formal-ct-image" ? image : { id },
    createFormalPortViewer: (host, mesh, cap, side) => {
      shown.push({ host, mesh, cap, side }); return { dispose() {} };
    },
    drawFormalPortProjection: (canvas, port) => { shown.push({ canvas, port }); },
    encodeURIComponent, Error, Array, Number, Set, Float32Array,
  });
  for (const name of ["formalPortCapPositions", "formalPortProjectionPoints", "loadFormalPortVisual"])
    vm.runInContext(functionSource(name), ctx);
  return { ctx, state, calls, shown, manifest, setContext: value => { context = value; } };
}

test("formal preview reads verified STL, projects the same cap and fetches CT", async () => {
  const { ctx, state, calls, shown, manifest } = formalViewHarness();
  await ctx.loadFormalPortVisual("left");
  assert.equal(state.endplanes.formalPorts.visual.status, "ready");
  assert.equal(calls[0], "/api/cases/case-test/segment/ports/formal?scope=main_airway");
  assert.equal(calls[1].url, "/api/cases/case-test/model");
  assert.equal(calls[2].url, "/api/cases/case-test/segment/endplanes/section");
  assert.deepEqual(JSON.parse(calls[2].options.body).plane, {
    point_lps_mm: [0, 0, 0], normal_lps: [0, 1, 0],
  });
  assert.deepEqual([...shown[1].cap], [...Float32Array.from({ length: 9 }, (_, index) => index + 9)]);
  assert.equal(shown[1].side, "left");
  assert.equal(shown[0].port.port_id, manifest.ports[0].port_id);
});

test("formal preview rejects changed mesh and CT version before display", async () => {
  const changed = formalViewHarness({ returnedHash: "f".repeat(64) });
  await changed.ctx.loadFormalPortVisual("left");
  assert.equal(changed.state.endplanes.formalPorts.visual.status, "error");
  assert.match(changed.state.endplanes.formalPorts.visual.error, /网格版本已变更/);
  assert.equal(changed.calls.length, 1);
  const tampered = formalViewHarness({ claimedHash: "a".repeat(64) });
  await tampered.ctx.loadFormalPortVisual("left");
  assert.equal(tampered.state.endplanes.formalPorts.visual.status, "error");
  assert.match(tampered.state.endplanes.formalPorts.visual.error, /网格指纹不一致/);
  assert.equal(tampered.calls.length, 2);
  assert.equal(tampered.shown.length, 0);
  const wrongSection = formalViewHarness({ sectionVersion: "6" });
  await wrongSection.ctx.loadFormalPortVisual("left");
  assert.equal(wrongSection.state.endplanes.formalPorts.visual.status, "error");
  assert.match(wrongSection.state.endplanes.formalPorts.visual.error, /掩膜版本/);
  assert.equal(wrongSection.shown.length, 0);
});

test("a late formal response cannot paint the previous case", async () => {
  const harness = formalViewHarness();
  const { ctx, state, calls, shown, manifest, setContext } = harness;
  let release;
  ctx.api = async url => {
    calls.push(url);
    return new Promise(resolve => { release = () => resolve(manifest); });
  };
  const pending = ctx.loadFormalPortVisual("left");
  setContext({ caseId: "case-new", scope: "main_airway", version: 1,
    key: "case-new|main_airway|1" });
  release();
  await pending;
  assert.equal(state.endplanes.formalPorts.visual.status, "loading");
  assert.equal(calls.length, 1);
  assert.equal(shown.length, 0);
});

test("collapsed experimental annotation cannot leave a CT draft projection visible", () => {
  const canvas = element(), status = element(), experiment = element();
  experiment.open = false;
  const nodes = {
    "endplane-port-projection": canvas,
    "endplane-port-projection-status": status,
    "endplane-opening-markup": experiment,
  };
  const ctx = vm.createContext({ $: (id) => nodes[id], state: { endplanes: {} } });
  vm.runInContext(functionSource("renderPortProjection"), ctx);
  ctx.renderPortProjection();
  assert.equal(canvas.hidden, true);
  assert.equal(status.hidden, true);
});

test("automatic three-opening action loads only a review draft", async () => {
  assert.match(html, /id="endplane-auto-candidate-button"[^>]*>自动找三个开口/);
  assert.match(source, /addEventListener\("click", autoProposeBilateralOpenings\)/);
  const context = { caseId: "case-test", scope: "main_airway", version: 7, key: "case-test|main_airway|7" };
  const autoCandidate = { status: "idle", reason: null, requestId: 0, controller: null };
  const state = { endplanes: { key: context.key, status: "ready", open: true, saving: false,
    profile: { can_save: true, supported_formats: ["oblique_bilateral_local_v3"] }, autoCandidate } };
  const plan = { format: "oblique_bilateral_local_v3", scope: context.scope, mask_version: context.version };
  const calls = [], applied = [];
  const ctx = vm.createContext({
    state, endplaneContext: () => context,
    renderAutoCandidate: () => {},
    api: async (url, options) => {
      calls.push({ url, options });
      return { status: "available", proposal_id: "proposal-test", human_review_required: true,
        committed: false, proposal: { proposal_id: "proposal-test", case_id: context.caseId,
          scope: context.scope, mask_version: context.version, plan } };
    },
    applyEndplaneLearningDraft: (draft) => { applied.push(draft); return true; },
    AbortController,
  });
  vm.runInContext(functionSource("autoProposeBilateralOpenings"), ctx);
  await ctx.autoProposeBilateralOpenings();
  assert.deepEqual(calls.map((call) => call.url),
    ["/api/cases/case-test/segment/endplanes/learning/auto-propose-bilateral"]);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.body.mask_version, 7);
  assert.deepEqual(applied, [plan]);
  assert.equal(autoCandidate.status, "loaded");
});

test("unavailable or unreviewed automatic results cannot enter the draft", async () => {
  const context = { caseId: "case-test", scope: "main_airway", version: 7, key: "case-test|main_airway|7" };
  const autoCandidate = { status: "idle", reason: null, requestId: 0, controller: null };
  const state = { endplanes: { key: context.key, status: "ready", open: true, saving: false,
    profile: { can_save: true, supported_formats: ["oblique_bilateral_local_v3"] }, autoCandidate } };
  let result = { status: "unavailable", reason: "three_cap_preflight_unavailable",
    human_review_required: true, committed: false };
  let applied = 0;
  const ctx = vm.createContext({ state, endplaneContext: () => context,
    renderAutoCandidate: () => {}, api: async () => result,
    applyEndplaneLearningDraft: () => { applied += 1; return true; }, AbortController });
  vm.runInContext(functionSource("autoProposeBilateralOpenings"), ctx);
  await ctx.autoProposeBilateralOpenings();
  assert.equal(autoCandidate.status, "unavailable");
  assert.match(autoCandidate.reason, /三处封盖预检/);
  assert.equal(applied, 0);
  result = { status: "available", proposal_id: "proposal-test", human_review_required: false,
    committed: false, proposal: { proposal_id: "proposal-test", case_id: context.caseId,
      scope: context.scope, mask_version: context.version,
      plan: { format: "oblique_bilateral_local_v3" } } };
  await ctx.autoProposeBilateralOpenings();
  assert.equal(autoCandidate.status, "error");
  assert.equal(applied, 0);
});
