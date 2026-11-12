"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");

function functionSource(name) {
  const start = source.match(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  assert.ok(start, `production function ${name} exists`);
  const end = source.indexOf("\n}", start.index);
  assert.notEqual(end, -1, `production function ${name} is complete`);
  return source.slice(start.index, end + 2);
}

function stateSource() {
  const start = source.indexOf("const state = {");
  const end = source.indexOf("\n};", start);
  assert.ok(start >= 0 && end > start, "production state exists");
  return source.slice(start, end + 3);
}

function plane(y, normalY = -1) {
  return { point_lps_mm: [1, y, 3], normal_lps: [0, normalY, 0] };
}

function assertJsonEqual(actual, expected) {
  assert.equal(JSON.stringify(actual), JSON.stringify(expected));
}

test("actual clip preview distinguishes geometry errors from gateway failures", () => {
  const ctx = vm.createContext({});
  vm.runInContext(functionSource("actualEndplanePreviewHttpError"), ctx);
  const response = (status) => ({ status, headers: { get: () => "req-123" } });
  assert.equal(ctx.actualEndplanePreviewHttpError(response(400), {
    detail: { reason: "鼻咽端形成两个闭合环", remediation: ["移动鼻咽切面后重试"] },
  }), "鼻咽端形成两个闭合环；移动鼻咽切面后重试");
  assert.match(ctx.actualEndplanePreviewHttpError(response(502), null), /HTTP 502.*当前选区仍保留.*req-123/);
  assert.match(ctx.actualEndplanePreviewHttpError(response(403), null), /新标签登录.*草稿不要刷新/);
});

test("synthetic acceptance can exercise the same endplane editor after mask confirmation", () => {
  const ctx = vm.createContext({
    requiresMaskReview: () => false,
    modelScopeParams: () => ({ params: { model_scope: "main_airway" } }),
  });
  vm.runInContext(functionSource("endplaneContext"), ctx);
  const data = { case_id: "case-synthetic", synthetic: true,
    stages: { segment: { status: "ok", data: { mask_version: 1 } } } };
  assert.equal(ctx.endplaneContext(data)?.caseId, "case-synthetic");
  data.stages.segment.status = "warn";
  assert.equal(ctx.endplaneContext(data), null);
});

function harness() {
  const requests = [];
  const messages = [];
  const context = vm.createContext({
    window: {},
    endplaneContext: () => ({ caseId: "case-a", scope: "main_airway", version: 3, key: "case-a|main_airway|3" }),
    endplaneValidation: () => null,
    clearEndplanePreview: () => {},
    scheduleEndplanePreview: () => {},
    scheduleEndplaneSection: () => {},
    renderEndplaneUi: () => {},
    clearEndplaneSection: () => {},
    renderCase: () => {},
    toast: (message, failed) => messages.push({ message, failed }),
    api: async (url, options) => {
      requests.push({ url, options });
      if (url === "/api/cases/case-a") return context.__state.case;
      const side = url.endsWith("/anterior") ? "anterior" : url.endsWith("/posterior") ? "posterior" : null;
      assert.ok(side, `unexpected API URL: ${url}`);
      const saved = context.__state.endplanes.baselinePlanes;
      return {
        scope: "main_airway",
        mask_version: 3,
        format: "oblique_planes_v1",
        anterior_plane: side === "anterior" ? context.__state.endplanes.planes.anterior : saved.anterior,
        posterior_plane: side === "posterior" ? context.__state.endplanes.planes.posterior : saved.posterior,
        complete: true,
      };
    },
  });
  vm.runInContext([
    stateSource(),
    functionSource("validLpsVector"),
    functionSource("normalizedEndplane"),
    functionSource("cloneEndplane"),
    functionSource("cloneEndplanePair"),
    functionSource("cloneBilateralDraft"),
    functionSource("emptyOpeningMarkup"),
    functionSource("resetOpeningMarkup"),
    functionSource("syncActiveBilateralDraft"),
    functionSource("currentEndplaneSeed"),
    functionSource("setCurrentEndplaneSeed"),
    functionSource("ctIndexFromPlane"),
    functionSource("endplanePairChanged"),
    functionSource("endplaneSideChanged"),
    functionSource("sameEndplaneSeed"),
    functionSource("rememberEndplaneEdit"),
    functionSource("updateEndplaneDirty"),
    functionSource("endplaneBody"),
    functionSource("endplaneSignature"),
    functionSource("resetSinglePortSuggestionState"),
    functionSource("singlePortSuggestionEligible"),
    functionSource("sameSuggestedPlane"),
    functionSource("validateSinglePortSuggestion"),
    functionSource("renderSinglePortSuggestion"),
    functionSource("requestSinglePortSuggestion"),
    functionSource("applySinglePortSuggestion"),
    functionSource("endplaneValidation"),
    functionSource("endplaneRegionProgress"),
    functionSource("updateEndplaneDraft"),
    functionSource("applyEndplaneRegions"),
    functionSource("loadEndplaneTargets"),
    functionSource("fetchEndplaneTargets"),
    functionSource("coincidentEndplanes"),
    functionSource("setEndplaneInletLayout"),
    functionSource("commitEndplaneEdit"),
    functionSource("undoEndplaneEdit"),
    functionSource("selectEndplaneSide"),
    functionSource("selectBilateralInlet"),
    functionSource("selectEndplaneOpening"),
    functionSource("nextEndplaneOpening"),
    functionSource("renderEndplaneOpeningSteps"),
    functionSource("guardEndplaneDraft"),
    functionSource("saveSingleEndplane"),
    functionSource("saveBilateralEndplanes"),
    functionSource("saveLocalEndplanes"),
    functionSource("discardLocalEndplanes"),
    functionSource("discardSingleEndplane"),
    "globalThis.__realEndplaneValidation = endplaneValidation;",
    "globalThis.endplaneValidation = () => null;",
    "globalThis.__state = state;",
  ].join("\n"), context, { filename: "static/app.js" });

  const state = context.__state;
  const anteriorBaseline = plane(-1, -1);
  const posteriorBaseline = plane(2, 1);
  const anteriorDraft = plane(-2, -2);
  const posteriorDraft = plane(3, 1);
  state.caseId = "case-a";
  state.case = { case_id: "case-a" };
  state.endplanes = {
    ...state.endplanes,
    key: "case-a|main_airway|3",
    status: "ready",
    profile: {
      can_save: true,
      count: 10,
      ap_origin_lps_mm: [0, 0, 0],
      ap_direction_lps: [0, 1, 0],
      ap_spacing_mm: 1,
      saved: { format: "oblique_planes_v1", anterior_plane: anteriorBaseline, posterior_plane: posteriorBaseline },
    },
    baseline: { anterior: 1, posterior: 2 },
    baselinePlanes: { anterior: anteriorBaseline, posterior: posteriorBaseline },
    values: { anterior: 2, posterior: 3 },
    planes: { anterior: anteriorDraft, posterior: posteriorDraft },
    dirty: true,
    saving: false,
  };
  context.updateEndplaneDirty();
  return { context, state, requests, messages, anteriorBaseline, posteriorBaseline, anteriorDraft, posteriorDraft };
}

for (const side of ["anterior", "posterior"]) {
  test(`saving ${side} submits only that endplane and preserves the other draft`, async () => {
    const h = harness();
    await h.context.saveSingleEndplane(side);

    const post = h.requests[0];
    assert.equal(post.url, `/api/cases/case-a/segment/endplanes/${side}`);
    assert.equal(post.options.method, "POST");
    assert.deepEqual(Object.keys(post.options.body).sort(), ["mask_version", "plane", "scope"]);
    assert.equal(post.options.body.scope, "main_airway");
    assert.equal(post.options.body.mask_version, 3);
    assertJsonEqual(post.options.body.plane.point_lps_mm, h.state.endplanes.planes[side].point_lps_mm);
    assert.equal(h.requests[1].url, "/api/cases/case-a");
    assertJsonEqual(h.state.endplanes.planes[side], h.state.endplanes.baselinePlanes[side]);
    const other = side === "anterior" ? "posterior" : "anterior";
    assertJsonEqual(h.state.endplanes.planes[other], h[`${other}Draft`]);
    assert.equal(h.state.endplanes.dirtySides[side], false);
    assert.equal(h.state.endplanes.dirtySides[other], true);
    assert.equal(h.state.endplanes.error, null);
    if (side === "anterior") assert.equal(h.state.endplanes.mode, "posterior", "saving the nostril advances to the nasopharynx");
  });
}

test("discarding one endplane keeps the opposite unsaved draft", () => {
  const h = harness();
  h.context.discardSingleEndplane("posterior");
  assertJsonEqual(h.state.endplanes.planes.posterior, h.posteriorBaseline);
  assertJsonEqual(h.state.endplanes.planes.anterior, h.anteriorDraft);
  assert.equal(h.state.endplanes.dirtySides.posterior, false);
  assert.equal(h.state.endplanes.dirtySides.anterior, true);
});

test("a late save response cannot alter another case or end its pending save", async () => {
  const h = harness();
  let resolve;
  h.context.api = () => new Promise((done) => { resolve = done; });
  const saving = h.context.saveSingleEndplane("anterior");
  h.state.caseId = "case-b";
  h.state.endplanes.key = "case-b|main_airway|4";
  const before = JSON.stringify(h.state.endplanes.baselinePlanes);
  resolve({ scope: "main_airway", mask_version: 3, format: "oblique_planes_v1", anterior_plane: h.anteriorDraft });
  await saving;
  assert.equal(JSON.stringify(h.state.endplanes.baselinePlanes), before);
  assert.equal(h.state.endplanes.saving, true);
  assert.equal(h.messages.length, 0);
});

function localDraft(h) {
  const e = h.state.endplanes;
  e.profile.supported_formats = ["oblique_local_inlet_v2"];
  e.format = "oblique_local_inlet_v2";
  e.anteriorSeed = [1, -9, 3];
  e.selectionStatus = "ready";
  h.context.updateEndplaneDirty();
  e.previewStatus = "ok";
  e.previewData = { format: e.format, can_preview_mesh: true };
  e.previewSignature = h.context.endplaneSignature();
  e.actualPreviewStatus = "ok";
  e.actualPreviewSignature = e.previewSignature;
  return e;
}

test("local inlet save submits both planes and seed only after matching mesh preview", async () => {
  const h = harness();
  const e = localDraft(h);
  h.context.api = async (url, options) => {
    h.requests.push({ url, options });
    if (url === "/api/cases/case-a") return h.state.case;
    assert.equal(url, "/api/cases/case-a/segment/endplanes");
    return {
      scope: "main_airway", mask_version: 3, format: "oblique_local_inlet_v2", complete: true,
      anterior_plane: h.anteriorDraft, posterior_plane: h.posteriorDraft,
      anterior_seed_lps_mm: [1, -9, 3],
    };
  };
  await h.context.saveLocalEndplanes();
  assert.equal(h.requests.length, 2);
  assert.deepEqual(Object.keys(h.requests[0].options.body).sort(), [
    "anterior_plane", "anterior_seed_lps_mm", "format", "mask_version", "posterior_plane", "scope",
  ]);
  assert.equal(h.requests[0].options.body.format, "oblique_local_inlet_v2");
  assertJsonEqual(h.requests[0].options.body.anterior_seed_lps_mm, [1, -9, 3]);
  assert.equal(e.baselineFormat, "oblique_local_inlet_v2");
  assert.equal(e.dirty, false);
});

test("local inlet save refuses missing seed, stale mesh preview and failed voxel preview", async () => {
  const h = harness();
  const e = localDraft(h);
  e.anteriorSeed = null;
  e.previewSignature = h.context.endplaneSignature();
  e.actualPreviewSignature = e.previewSignature;
  await h.context.saveLocalEndplanes();
  assert.equal(h.requests.length, 0);
  e.anteriorSeed = [1, -9, 3];
  e.previewSignature = h.context.endplaneSignature();
  e.actualPreviewSignature = "stale";
  await h.context.saveLocalEndplanes();
  assert.equal(h.requests.length, 0);
  e.actualPreviewSignature = e.previewSignature;
  e.previewStatus = "blocked";
  await h.context.saveLocalEndplanes();
  assert.equal(h.requests.length, 0);
});

test("legacy plan stays read-only and discarding local draft restores its baseline", async () => {
  const h = harness();
  const e = h.state.endplanes;
  e.profile.supported_formats = ["oblique_local_inlet_v2"];
  await h.context.saveSingleEndplane("anterior");
  assert.equal(h.requests.length, 0);
  localDraft(h);
  h.context.discardLocalEndplanes();
  assert.equal(e.format, "oblique_planes_v1");
  assert.equal(e.anteriorSeed, null);
  assertJsonEqual(e.planes.anterior, h.anteriorBaseline);
  assert.equal(e.dirty, false);
});

test("undo restores the previous target branch seed and invalidates its preview", () => {
  const h = harness();
  const e = localDraft(h);
  e.editHistory.push({ kind: "seed", seed: null });
  h.context.undoEndplaneEdit();
  assert.equal(e.anteriorSeed, null);
  assert.equal(e.placement, "seed");
  assert.equal(h.context.endplaneBody().anterior_seed_lps_mm, null);
});

function bilateralDraft(h) {
  const e = h.state.endplanes;
  h.context.endplaneValidation = h.context.__realEndplaneValidation;
  const leftPlane = { point_lps_mm: [-5, -6, 3], normal_lps: [0, -1, 0] };
  const rightPlane = { point_lps_mm: [5, -6, 3], normal_lps: [0, -1, 0] };
  const posteriorPlane = { point_lps_mm: [0, 6, 3], normal_lps: [0, 1, 0] };
  e.profile = { ...e.profile, count: 40, ap_origin_lps_mm: [0, -20, 0], anterior_sign: -1,
    supported_formats: ["oblique_bilateral_local_v3"], saved: null };
  e.format = e.baselineFormat = "oblique_bilateral_local_v3";
  e.activeInlet = "left";
  e.sharedInletPlane = false;
  e.baselineBilateral = h.context.cloneBilateralDraft(null);
  e.bilateral = h.context.cloneBilateralDraft({
    left: { plane: leftPlane, seed: [-5, -9, 3] },
    right: { plane: rightPlane, seed: [5, -9, 3] },
  });
  e.baselinePlanes = { anterior: null, posterior: null };
  e.planes = { anterior: h.context.cloneEndplane(leftPlane), posterior: posteriorPlane };
  e.baselineAnteriorSeed = e.baselinePosteriorSeed = null;
  e.anteriorSeed = [-5, -9, 3];
  e.posteriorSeed = [0, 9, 3];
  e.mode = "anterior";
  e.selectionStatus = "ready";
  h.context.updateEndplaneDirty();
  return e;
}

function singlePortHarness() {
  const h = harness();
  const e = bilateralDraft(h);
  e.open = true;
  const nodes = new Map();
  h.context.$ = id => {
    if (!nodes.has(id)) nodes.set(id, { hidden: false, disabled: false, textContent: "", checked: false,
      classList: { toggle() {} } });
    return nodes.get(id);
  };
  h.context.AbortController = AbortController;
  e.viewer = { setPlanes() {}, showSeedMarker() {}, clearActualPreview() {}, setActiveSide() {}, focusActive() {} };
  vm.runInContext(functionSource("clearEndplanePreview"), h.context);
  const original = JSON.parse(JSON.stringify(h.context.endplaneBody()));
  const proposal = JSON.parse(JSON.stringify(original));
  proposal.left_inlet_plane.point_lps_mm[1] -= 1.5;
  proposal.left_inlet_seed_lps_mm[1] -= 1.5;
  const result = { status: "proposed", reason: null, side: "left", scope: original.scope,
    mask_version: original.mask_version, shift_mm: { left: 1.5, right: 0, outlet: 0 },
    plan: proposal, retained_voxels: 1000, tested_offsets_mm: [2, 1.5], mesh_attempts: 1,
    mesh_preflight: { closed_caps: true, left_inlet_loops: 1, right_inlet_loops: 1, outlet_loops: 1 } };
  return { ...h, e, nodes, original, result };
}

test("single-port suggestion is read-only, pending cannot duplicate, and adopt requires an explicit click", async () => {
  const h = singlePortHarness();
  let resolve;
  h.context.api = (_url, options) => {
    h.requests.push(options);
    return new Promise(done => { resolve = done; });
  };
  const pending = h.context.requestSinglePortSuggestion();
  assert.equal(h.e.singlePortSuggestion.status, "loading");
  assert.equal(h.nodes.get("endplane-single-port-find").disabled, true);
  await h.context.requestSinglePortSuggestion();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].body.side, "left");
  assertJsonEqual(h.context.endplaneBody(), h.original);
  resolve(h.result);
  await pending;
  assert.equal(h.e.singlePortSuggestion.status, "proposed");
  assert.equal(h.nodes.get("endplane-single-port-apply").hidden, false);
  assertJsonEqual(h.context.endplaneBody(), h.original);
});

test("single-port unavailable preserves draft and explains what to adjust", async () => {
  const h = singlePortHarness();
  h.context.api = async () => ({ status: "unavailable", side: "left", scope: h.original.scope,
    mask_version: h.original.mask_version, reason: "original_port_not_selectable" });
  await h.context.requestSinglePortSuggestion();
  assert.equal(h.e.singlePortSuggestion.status, "unavailable");
  assert.match(h.nodes.get("endplane-single-port-status").textContent, /调整该侧切面或检查鼻孔前段掩膜/);
  assert.doesNotMatch(h.nodes.get("endplane-single-port-status").textContent, /original_port_not_selectable/);
  assert.equal(h.nodes.get("endplane-single-port-apply").hidden, true);
  assertJsonEqual(h.context.endplaneBody(), h.original);
});

test("single-port suggestion discards a late response after draft changes", async () => {
  const h = singlePortHarness();
  let resolve;
  h.context.api = () => new Promise(done => { resolve = done; });
  const pending = h.context.requestSinglePortSuggestion();
  h.e.planes.anterior = { point_lps_mm: [-5, -6.5, 3], normal_lps: [0, -1, 0] };
  h.context.updateEndplaneDraft(true);
  resolve(h.result);
  await pending;
  assert.equal(h.e.singlePortSuggestion.status, "idle");
  assert.equal(h.e.bilateral.left.plane.point_lps_mm[1], -6.5);
  assert.equal(h.nodes.get("endplane-single-port-apply").hidden, true);
});

test("adopting a verified suggestion changes only one inlet, invalidates preview, and undo restores it", async () => {
  const h = singlePortHarness();
  h.context.api = async () => h.result;
  await h.context.requestSinglePortSuggestion();
  h.e.previewStatus = "ok";
  h.e.actualPreviewStatus = "ok";
  h.e.previewSignature = h.e.actualPreviewSignature = h.context.endplaneSignature();
  assert.equal(h.context.applySinglePortSuggestion(), true);
  const adopted = h.context.endplaneBody();
  assert.equal(adopted.left_inlet_plane.point_lps_mm[1], -7.5);
  assertJsonEqual(adopted.left_inlet_seed_lps_mm, h.result.plan.left_inlet_seed_lps_mm);
  assertJsonEqual(adopted.right_inlet_plane, h.original.right_inlet_plane);
  assertJsonEqual(adopted.posterior_plane, h.original.posterior_plane);
  assert.equal(h.e.previewStatus, "idle");
  assert.equal(h.e.actualPreviewStatus, "idle");
  assert.equal(h.e.singlePortSuggestion.status, "idle");
  h.context.undoEndplaneEdit();
  assertJsonEqual(h.context.endplaneBody(), h.original);
});

test("single-port suggestion rejects any change to the other inlet", async () => {
  const h = singlePortHarness();
  h.result.plan.right_inlet_plane.point_lps_mm[1] -= 0.5;
  h.context.api = async () => h.result;
  await h.context.requestSinglePortSuggestion();
  assert.equal(h.e.singlePortSuggestion.status, "error");
  assert.equal(h.nodes.get("endplane-single-port-apply").hidden, true);
  assertJsonEqual(h.context.endplaneBody(), h.original);
});

test("right-side suggestion preserves the left inlet and the outlet", async () => {
  const h = singlePortHarness();
  h.context.selectBilateralInlet("right");
  const original = JSON.parse(JSON.stringify(h.context.endplaneBody()));
  const plan = JSON.parse(JSON.stringify(original));
  plan.right_inlet_plane.point_lps_mm[1] -= 1;
  plan.right_inlet_seed_lps_mm[1] -= 1;
  const result = { ...h.result, side: "right", shift_mm: { left: 0, right: 1, outlet: 0 }, plan };
  h.context.api = async () => result;
  await h.context.requestSinglePortSuggestion();
  assert.equal(h.context.applySinglePortSuggestion(), true);
  const adopted = h.context.endplaneBody();
  assertJsonEqual(adopted.left_inlet_plane, original.left_inlet_plane);
  assertJsonEqual(adopted.left_inlet_seed_lps_mm, original.left_inlet_seed_lps_mm);
  assertJsonEqual(adopted.posterior_plane, original.posterior_plane);
  assert.equal(adopted.right_inlet_plane.point_lps_mm[1], -7);
});

test("bilateral editor retains independent left and right planes and seeds", () => {
  const h = harness();
  const e = bilateralDraft(h);
  const left = h.context.cloneEndplane(e.planes.anterior);
  h.context.selectBilateralInlet("right");
  assertJsonEqual(e.planes.anterior, e.bilateral.right.plane);
  assertJsonEqual(e.anteriorSeed, e.bilateral.right.seed);
  const changedRight = { point_lps_mm: [5, -7, 3], normal_lps: [0, -1, 0] };
  h.context.commitEndplaneEdit(changedRight);
  e.anteriorSeed = [5, -10, 3];
  h.context.updateEndplaneDirty();
  h.context.selectBilateralInlet("left");
  assertJsonEqual(e.planes.anterior, left);
  assertJsonEqual(e.anteriorSeed, [-5, -9, 3]);
  assertJsonEqual(e.bilateral.right.plane, changedRight);
  assertJsonEqual(e.bilateral.right.seed, [5, -10, 3]);
  assert.equal(h.context.endplaneValidation(), null);
});

test("common nostril plane updates both sides and invalidates both region choices", () => {
  const h = harness();
  const e = bilateralDraft(h);
  e.sharedInletPlane = true;
  const changed = { point_lps_mm: [0, -7, 3], normal_lps: [0, -1, 0] };
  h.context.commitEndplaneEdit(changed);
  assertJsonEqual(e.bilateral.left.plane, changed);
  assertJsonEqual(e.bilateral.right.plane, changed);
  assert.equal(e.bilateral.left.seed, null);
  assert.equal(e.bilateral.right.seed, null);
  assertJsonEqual(e.posteriorSeed, [0, 9, 3]);
});

test("opening step separates a shared plane without losing regions or invalidating current preview", () => {
  const h = harness();
  const e = bilateralDraft(h);
  e.bilateral.right.plane = h.context.cloneEndplane(e.bilateral.left.plane);
  e.sharedInletPlane = true;
  e.previewStatus = "ok";
  const before = JSON.stringify(h.context.endplaneBody());
  e.previewSignature = h.context.endplaneSignature();
  e.actualPreviewStatus = "ok";
  e.actualPreviewSignature = e.previewSignature;
  h.context.selectEndplaneOpening("right");
  assert.equal(e.sharedInletPlane, false);
  assert.equal(e.activeInlet, "right");
  assert.equal(JSON.stringify(h.context.endplaneBody()), before);
  assert.equal(e.actualPreviewSignature, h.context.endplaneSignature());
  assert.equal(e.actualPreviewStatus, "ok");
  h.context.selectEndplaneOpening("posterior");
  h.context.selectEndplaneOpening("left");
  assert.equal(JSON.stringify(h.context.endplaneBody()), before);
  assert.equal(h.requests.length, 0, "navigating steps never persists or confirms a case");
  e.saving = true;
  h.context.selectEndplaneOpening("right");
  assert.equal(e.activeInlet, "left", "pending save locks step navigation");
});

test("opening step highlights the current target and distinguishes placement, selection, and saved data", () => {
  const h = harness();
  const e = bilateralDraft(h);
  const nodes = new Map();
  h.context.$ = (id) => {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: false, disabled: false, textContent: "", attrs: {}, classes: new Set(),
      setAttribute(key, value) { this.attrs[key] = value; },
      classList: { toggle(key, value) { const set = nodes.get(id).classes; value ? set.add(key) : set.delete(key); } },
    });
    return nodes.get(id);
  };
  h.context.renderEndplaneOpeningSteps();
  assert.equal(nodes.get("endplane-step-left").attrs["aria-pressed"], "true");
  assert.equal(nodes.get("endplane-step-right").attrs["aria-pressed"], "false");
  assert.equal(nodes.get("endplane-step-left-status").textContent, "已选 · 未保存");
  e.bilateral.right.seed = [5, 0, 3];
  h.context.renderEndplaneOpeningSteps();
  assert.equal(nodes.get("endplane-step-right-status").textContent, "待选区域", "seed on retained side is not a completed region");
  e.bilateral.right.plane = null;
  e.baselineBilateral.left = h.context.cloneBilateralDraft(e.bilateral).left;
  h.context.renderEndplaneOpeningSteps();
  assert.equal(nodes.get("endplane-step-right-status").textContent, "待定位");
  assert.equal(nodes.get("endplane-step-left-status").textContent, "已保存");
  h.context.selectEndplaneOpening("posterior");
  h.context.renderEndplaneOpeningSteps();
  assert.equal(nodes.get("endplane-step-posterior").attrs["aria-pressed"], "true");
  assert.equal(nodes.get("endplane-ct-active").textContent, "鼻咽端");
  assert.equal(nodes.get("endplane-active-badge").textContent, "鼻咽端 · 当前选中");
  e.stale = true;
  h.context.renderEndplaneOpeningSteps();
  assert.notEqual(nodes.get("endplane-step-left-status").textContent, "已保存");
  e.saving = true;
  h.context.renderEndplaneOpeningSteps();
  assert.equal(nodes.get("endplane-step-posterior").disabled, true);
});

test("next opening finishes the current region before directing users to another step", () => {
  const h = harness();
  const e = bilateralDraft(h);
  assert.equal(h.context.nextEndplaneOpening(), null);
  e.bilateral.left.seed = null;
  e.anteriorSeed = null;
  e.posteriorSeed = null;
  h.context.selectEndplaneOpening("posterior");
  assert.equal(h.context.nextEndplaneOpening(), "posterior");
  e.posteriorSeed = [0, 9, 3];
  assert.equal(h.context.nextEndplaneOpening(), "left");
});

test("discarding unsaved opening choices returns to point placement without saving a case", () => {
  const h = harness();
  const e = bilateralDraft(h);
  e.placement = null;
  h.context.discardLocalEndplanes();
  assert.equal(e.placement, "point");
  assert.equal(e.dirty, false);
  assert.equal(e.planes.anterior, null);
  assert.equal(e.bilateral.left.seed, null);
  assert.equal(e.bilateral.right.seed, null);
  assert.equal(h.requests.length, 0);
});

test("moving outlet clears only its region and keeps both nostril targets", () => {
  const h = harness();
  const e = bilateralDraft(h);
  h.context.selectEndplaneSide("posterior");
  h.context.commitEndplaneEdit({ point_lps_mm: [0, 7, 3], normal_lps: [0, 1, 0] });
  assert.equal(e.posteriorSeed, null);
  assertJsonEqual(e.bilateral.left.seed, [-5, -9, 3]);
  assertJsonEqual(e.bilateral.right.seed, [5, -9, 3]);
});

test("CT selection assigns left/right by patient coordinates and never saves automatically", () => {
  const h = harness();
  const e = bilateralDraft(h);
  e.sharedInletPlane = true;
  h.context.window.EndplanePicker = require("../static/endplane-picker.js");
  h.context.renderEndplaneRegions = () => {};
  e.sectionKey = "current";
  e.sectionTargets = { key: "current", role: "anterior", expected: 2, data: { candidates: [
    { id: 1, valid: true, seed_lps_mm: [-5, -9, 3], runs: [[1, 1, 2]] },
    { id: 2, valid: true, seed_lps_mm: [5, -9, 3], runs: [[2, 1, 2]] },
  ] } };
  h.context.applyEndplaneRegions([1, 2]);
  assertJsonEqual(e.bilateral.left.seed, [5, -9, 3]);
  assertJsonEqual(e.bilateral.right.seed, [-5, -9, 3]);
  assert.equal(h.requests.length, 0);
  h.context.applyEndplaneRegions([1]);
  assertJsonEqual(e.bilateral.left.seed, [-5, -9, 3]);
  assert.equal(e.bilateral.right.seed, null);
  assert.match(h.context.endplaneValidation(), /CT/);
  assert.equal(h.context.endplaneRegionProgress().inlets, 1);
  h.context.selectEndplaneSide("posterior");
  assert.equal(h.context.endplaneRegionProgress().missingSide, "anterior");
  assert.equal(h.context.endplaneRegionProgress().inlets, 1, "switching tabs must retain a partial inlet selection");
});

test("a CT region from an obsolete section cannot alter current draft", () => {
  const h = harness();
  const e = bilateralDraft(h);
  const before = h.context.endplaneSignature();
  e.sectionKey = "new";
  e.sectionTargets = { key: "old" };
  h.context.applyEndplaneRegions([1, 2]);
  assert.equal(h.context.endplaneSignature(), before);
});

test("rapid plane edits coalesce expensive CT reads into the latest request", async () => {
  const h = harness();
  const e = bilateralDraft(h);
  let resolve, started = 0, scheduled = 0;
  h.context.fetchEndplaneTargets = () => { started++; return new Promise(done => { resolve = done; }); };
  h.context.scheduleEndplaneSection = force => { assert.equal(force, true); scheduled++; };
  const first = h.context.loadEndplaneTargets({}, "first", 1);
  await h.context.loadEndplaneTargets({}, "second", 2);
  await h.context.loadEndplaneTargets({}, "third", 3);
  assert.equal(started, 1);
  assert.equal(e.targetsQueued, true);
  resolve(); await first;
  assert.equal(e.targetsBusy, false);
  assert.equal(scheduled, 1);
});

test("late CT response from a previous mask cannot overwrite targets", async () => {
  const h = harness();
  const e = bilateralDraft(h);
  e.open = true; e.sectionRequestId = 1; e.sectionKey = "first";
  let resolve;
  h.context.api = () => new Promise(done => { resolve = done; });
  const first = h.context.fetchEndplaneTargets({}, "first", 1, h.context.endplaneContext());
  e.sectionRequestId = 2; e.sectionKey = "second";
  e.sectionTargets = { key: "second", ids: [100] };
  resolve({}); await first;
  assertJsonEqual(e.sectionTargets.ids, [100]);
});

test("3D inlet reselection refreshes CT targets for the same plane", () => {
  const h = harness();
  const e = bilateralDraft(h);
  e.open = true;
  e.viewer = { sectionPlane: () => ({ plane: e.planes.anterior, span_mm: 60 }) };
  h.context.$ = () => ({ hidden: false, textContent: "" });
  let scheduled = 0;
  h.context.window.setTimeout = () => ++scheduled;
  vm.runInContext(functionSource("scheduleEndplaneSection"), h.context);
  h.context.scheduleEndplaneSection();
  const before = e.sectionKey;
  e.bilateral.left.seed = [-5, -10, 3];
  e.anteriorSeed = [-5, -10, 3];
  h.context.scheduleEndplaneSection();
  assert.notEqual(e.sectionKey, before, "CT key must include the selected 3D seed");
  assert.equal(scheduled, 2, "the changed inlet must cause a new CT target request");
});

test("coplanar saved nostril planes share a plane even with different centre points", () => {
  const h = harness();
  assert.equal(h.context.coincidentEndplanes(plane(-6), { point_lps_mm: [7, -6, 9], normal_lps: [0, -1, 0] }), true);
  assert.equal(h.context.coincidentEndplanes(plane(-6), plane(-7)), false);
});

test("switching bilateral sides preserves a matching actual-cut preview", () => {
  const h = harness();
  const e = bilateralDraft(h);
  const signature = h.context.endplaneSignature();
  e.previewStatus = e.actualPreviewStatus = "ok";
  e.previewSignature = e.actualPreviewSignature = signature;
  let cleared = 0;
  h.context.clearEndplanePreview = () => { cleared += 1; };
  h.context.selectBilateralInlet("right");
  h.context.selectEndplaneSide("posterior");
  h.context.selectBilateralInlet("left");
  assert.equal(cleared, 0);
  assert.equal(e.actualPreviewStatus, "ok");
  assert.equal(h.context.endplaneSignature(), signature);
});

test("bilateral preview requires three independent outside seeds", () => {
  const h = harness();
  const e = bilateralDraft(h);
  assert.equal(h.context.endplaneValidation(), null);
  e.posteriorSeed = null;
  assert.match(h.context.endplaneValidation(), /三维模型.*鼻咽开口.*CT/);
  e.posteriorSeed = [0, 9, 3];
  e.bilateral.right.seed = [-5, -9, 3];
  assert.match(h.context.endplaneValidation(), /定位点过近/);
  e.bilateral.right.seed = [5, -5, 3];
  assert.match(h.context.endplaneValidation(), /定位点必须位于切面的删除侧/);
  e.bilateral.right.seed = [5, -9, 3];
  assert.equal(h.context.endplaneValidation(), null);
});

test("bilateral caudal nasopharynx outlet reaches service preview without an AP-only rejection", () => {
  const h = harness();
  const e = bilateralDraft(h);
  e.planes.posterior.normal_lps = [-0.002, -0.0067, -0.99998];
  e.posteriorSeed = [0, 6, 1];
  e.posteriorSeedPlane = h.context.cloneEndplane(e.planes.posterior);
  assert.equal(h.context.endplaneValidation(), null, "a caudal outlet with a deletion-side seed can be prechecked");

  e.posteriorSeed = [0, 6, 4];
  assert.match(h.context.endplaneValidation(), /鼻咽.*定位点必须位于切面的删除侧/);
  e.posteriorSeed = [0, 6, 1];

  e.planes.anterior.normal_lps = [0, 1, 0];
  e.bilateral.left.plane.normal_lps = [0, 1, 0];
  assert.match(h.context.endplaneValidation(), /左鼻孔端切除方向朝向气道内部/);
  e.planes.anterior.normal_lps = [0, -1, 0];
  e.bilateral.left.plane.normal_lps = [0, -1, 0];
  e.anteriorSeed = [-5, -9, 3];

  e.planes.posterior.point_lps_mm[1] = -7;
  e.posteriorSeedPlane = h.context.cloneEndplane(e.planes.posterior);
  assert.match(h.context.endplaneValidation(), /鼻孔端都必须位于鼻咽端前方/);
  e.planes.posterior.point_lps_mm[1] = 50;
  assert.match(h.context.endplaneValidation(), /鼻咽端切面超出 CT 扫描范围/);
});

test("a service-side bilateral preview rejection remains visible and cannot be saved", async () => {
  const h = harness();
  const e = bilateralDraft(h);
  e.planes.posterior.normal_lps = [-0.002, -0.0067, -0.99998];
  e.posteriorSeed = [0, 6, 1];
  e.posteriorSeedPlane = h.context.cloneEndplane(e.planes.posterior);
  h.context.AbortController = AbortController;
  h.context.api = async (url) => {
    h.requests.push({ url });
    throw { detail: { reason: "额外前端支段无法与左右鼻孔可靠区分", remediation: ["修正该支段后重试"] } };
  };
  vm.runInContext(functionSource("runEndplanePreview"), h.context);
  await h.context.runEndplanePreview();
  assert.equal(e.previewStatus, "blocked");
  assert.match(e.previewError, /额外前端支段无法与左右鼻孔可靠区分/);
  assert.equal(e.previewSignature, null);
  await h.context.saveBilateralEndplanes();
  assert.equal(h.requests.length, 1, "the blocked preview must not submit a save request");
});

test("explicit bilateral save requires matching voxel and mesh previews without a hidden checkbox", async () => {
  const h = harness();
  const e = bilateralDraft(h);
  const confirm = { checked: false };
  h.context.$ = (id) => id === "endplane-bilateral-confirm" ? confirm : null;
  const body = h.context.endplaneBody();
  const signature = h.context.endplaneSignature();
  e.previewStatus = "ok";
  e.previewSignature = signature;
  e.previewData = { format: body.format, can_preview_mesh: true };
  e.actualPreviewStatus = "ok";
  e.actualPreviewSignature = signature;
  e.previewStatus = "loading";
  await h.context.saveBilateralEndplanes();
  assert.equal(h.requests.length, 0, "automatic checks must finish before save");
  e.previewStatus = "ok";
  e.actualPreviewSignature = "stale";
  await h.context.saveBilateralEndplanes();
  assert.equal(h.requests.length, 0, "the actual mesh must match the draft");
  e.actualPreviewSignature = signature;
  h.context.api = async (url, options) => {
    h.requests.push({ url, options });
    return url === "/api/cases/case-a" ? h.state.case : { ...options.body, complete: true };
  };
  await h.context.saveBilateralEndplanes();
  assert.equal(h.requests[0].url, "/api/cases/case-a/segment/endplanes");
  assert.deepEqual(Object.keys(h.requests[0].options.body).sort(), [
    "format", "left_inlet_plane", "left_inlet_seed_lps_mm", "mask_version", "posterior_plane",
    "posterior_seed_lps_mm", "right_inlet_plane", "right_inlet_seed_lps_mm", "scope",
  ]);
  assert.equal(h.requests[1].url, "/api/cases/case-a");
  assert.equal(e.dirty, false);
  assert.equal(e.baselineFormat, "oblique_bilateral_local_v3");
});

test("discarding an upgraded bilateral draft restores the original single-branch baseline", () => {
  const h = harness();
  const e = bilateralDraft(h);
  e.baselineFormat = "oblique_local_inlet_v2";
  e.baselinePlanes = { anterior: h.anteriorBaseline, posterior: h.posteriorBaseline };
  e.baselineAnteriorSeed = [1, -9, 3];
  e.baseline = { anterior: 1, posterior: 2 };
  h.context.selectBilateralInlet("right");
  assertJsonEqual(e.baselinePlanes.anterior, h.anteriorBaseline);
  h.context.discardLocalEndplanes();
  assert.equal(e.format, "oblique_local_inlet_v2");
  assertJsonEqual(e.planes.anterior, h.anteriorBaseline);
  assertJsonEqual(e.planes.posterior, h.posteriorBaseline);
  assertJsonEqual(e.anteriorSeed, [1, -9, 3]);
});

test("closing an upgraded bilateral draft restores its visual format and saved baseline", () => {
  const h = harness();
  const e = bilateralDraft(h);
  e.baselineFormat = "oblique_local_inlet_v2";
  e.baselinePlanes = { anterior: h.anteriorBaseline, posterior: h.posteriorBaseline };
  e.baselineAnteriorSeed = [1, -9, 3];
  e.baseline = { anterior: 1, posterior: 2 };
  const viewerCalls = [];
  e.viewer = { setFormat: value => viewerCalls.push(["format", value]),
    setPlanes: value => viewerCalls.push(["planes", value]), focusActive() {} };
  h.context.window.confirm = () => true;
  h.context.selectBilateralInlet("right");
  assert.equal(h.context.guardEndplaneDraft(), true);
  assert.equal(e.format, "oblique_local_inlet_v2");
  assertJsonEqual(e.planes.anterior, h.anteriorBaseline);
  assert.deepEqual(viewerCalls.at(-2)[0], "format");
  assert.equal(viewerCalls.at(-2)[1], "oblique_local_inlet_v2");
});

test("undoing the posterior seed does not erase either nostril seed", () => {
  const h = harness();
  const e = bilateralDraft(h);
  h.context.selectEndplaneSide("posterior");
  e.editHistory.push({ kind: "seed", side: "posterior", seed: null });
  h.context.undoEndplaneEdit();
  assert.equal(e.posteriorSeed, null);
  assertJsonEqual(e.bilateral.left.seed, [-5, -9, 3]);
  assertJsonEqual(e.bilateral.right.seed, [5, -9, 3]);
});

function viewerHarness() {
  const three = require("../static/vendor/three.min.js");
  const listeners = {};
  const lassoListeners = {};
  const pending = [];
  const frames = {};
  const canvas = {
    addEventListener: (name, handler) => { listeners[name] = handler; },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 640, height: 420 }),
  };
  const lassoCanvas = {
    hidden: true,
    width: 640, height: 420,
    addEventListener: (name, handler) => { lassoListeners[name] = handler; },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 640, height: 420 }),
    getContext: () => ({ scale() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, clearRect() {}, setLineDash() {} }),
    setPointerCapture() {}, releasePointerCapture() {}, hasPointerCapture: () => false,
  };
  const context = vm.createContext({
    $: (id) => id === "endplane-port-lasso" ? lassoCanvas : null,
    THREE: {
      ...three,
      WebGLRenderer: class {
        constructor() { this.domElement = canvas; }
        setPixelRatio() {}
        setSize() {}
        render(scene, camera) { frames.scene = scene; frames.camera = camera; scene.updateMatrixWorld(true); }
      },
      OrbitControls: class {
        constructor(camera) { this.camera = camera; this.target = new three.Vector3(); frames.orbit = this; }
        update() { this.camera.lookAt(this.target); this.camera.updateMatrixWorld(true); }
      },
      TransformControls: class extends three.Object3D {
        attach(object) { this.object = object; }
        detach() { this.object = null; }
        setMode(mode) { this.mode = mode; }
      },
    },
    window: { EndplaneTools: require("../static/endplane-tools.js")(three),
      EndplaneOpeningMarkup: require("../static/endplane-opening-markup.js"),
      addEventListener() {}, setTimeout: (handler) => pending.push(handler) },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {},
    updateEndplaneDraft() {},
    renderEndplaneUi() {},
  });
  vm.runInContext([
    stateSource(),
    ...["validLpsVector", "normalizedEndplane", "endplaneFlowDirection", "planeFromCtIndex", "ctIndexFromPlane", "isAxialEndplane",
      "cloneEndplane", "cloneBilateralDraft", "emptyOpeningMarkup", "openingMarkupSide", "currentOpeningMarkup",
      "setOpeningMarkup", "syncActiveBilateralDraft", "currentEndplaneSeed", "setCurrentEndplaneSeed",
      "rememberEndplaneEdit", "commitEndplaneEdit", "normalizedProfileDirection", "planeNormalFromObject", "endplaneClippingPlanes", "preparePreviewShading", "applyPreviewShading", "createEndplaneViewer"].map(functionSource),
    "globalThis.__state = state;",
  ].join("\n"), context);
  const host = { clientWidth: 640, clientHeight: 420, replaceChildren() {} };
  const viewer = context.createEndplaneViewer(host);
  const profile = {
    count: 40, ap_origin_lps_mm: [-100, -20, -100], ap_direction_lps: [0, 1, 0],
    ap_spacing_mm: 1, anterior_sign: -1, areas_mm2: Array(40).fill(100),
  };
  viewer.setProfile(profile);
  viewer.loadGeometry(new three.BoxGeometry(10, 10, 10, 10, 10, 10));
  frames.orbit.update();
  const markers = () => frames.scene.children.find((child) => child.children.some((object) => object.userData.kind === "endplane-marker"))?.children || [];
  return {
    three, context, viewer, profile, state: context.__state, markers, frames, host,
    click() {
      const event = { clientX: 320, clientY: 210 };
      listeners.pointerdown(event);
      listeners.pointerup(event);
      pending.splice(0).forEach((handler) => handler());
    },
    setPlanes(planes) {
      context.__state.endplanes.planes = planes;
      viewer.setPlanes(planes, "anterior");
    },
  };
}

test("model click skips clipped foreground and selects the visible surface behind it", () => {
  const h = viewerHarness();
  h.setPlanes({ anterior: null, posterior: { point_lps_mm: [0, 0, 0], normal_lps: [0, 1, 0] } });
  h.click();
  assert.ok(h.state.endplanes.planes.anterior.point_lps_mm[1] < 0, "the clipped front surface must be ignored");
});

test("model click cannot place a plane when every intersection is clipped", () => {
  const h = viewerHarness();
  h.setPlanes({ anterior: null, posterior: { point_lps_mm: [0, -20, 0], normal_lps: [0, 1, 0] } });
  h.click();
  assert.equal(h.state.endplanes.planes.anterior, null);
});

test("CT-index markers stay near the airway without changing the plane or its outward direction", () => {
  const h = viewerHarness();
  const plane = { point_lps_mm: [-100, -2, -100], normal_lps: [0, -1, 0] };
  h.setPlanes({ anterior: plane, posterior: null });
  const marker = h.markers()[0];
  assert.ok(Math.abs(marker.position.x) < 1 && Math.abs(marker.position.z) < 1);
  assert.equal(marker.position.y, -2);
  assert.ok(h.context.planeNormalFromObject(marker).distanceTo(new h.three.Vector3(0, -1, 0)) < 1e-6);
  assert.ok(marker.userData.radius_mm <= 1.1, "large local areas must not produce a full-frame marker");
  assert.equal(plane.point_lps_mm[0], -100, "display recentering must not mutate saved coordinates");
});

test("unselected nostril shows the exact filled cross-section from the model mesh", () => {
  const h = viewerHarness();
  h.viewer.setFormat("oblique_bilateral_local_v3");
  const plane = { point_lps_mm: [0, 0, 0], normal_lps: [0, 1, 0] };
  h.setPlanes({ anterior: plane, posterior: null });
  const marker = h.markers()[0];
  assert.equal(marker.getObjectByName("endplane-orientation"), undefined, "no synthetic disk is drawn");
  const sections = [];
  h.frames.scene.traverse((object) => { if (object.userData.kind === "endplane-section") sections.push(object); });
  assert.equal(sections.length, 1, "the active opening has one real section surface");
  const section = sections[0];
  const positions = section.geometry.getAttribute("position");
  let area = 0;
  for (let i = 0; i < positions.count; i += 3) {
    const a = new h.three.Vector3().fromBufferAttribute(positions, i);
    const b = new h.three.Vector3().fromBufferAttribute(positions, i + 1);
    const c = new h.three.Vector3().fromBufferAttribute(positions, i + 2);
    assert.ok(Math.abs(a.y) < 1e-6 && Math.abs(b.y) < 1e-6 && Math.abs(c.y) < 1e-6);
    assert.ok([a, b, c].every((point) => Math.abs(point.x) <= 5 + 1e-6 && Math.abs(point.z) <= 5 + 1e-6));
    area += b.clone().sub(a).cross(c.clone().sub(a)).length() / 2;
  }
  assert.ok(Math.abs(area - 100) < 1e-4, "the filled face follows the 10 x 10 mesh intersection");
  assert.equal(marker.children.find((child) => child.type === "ArrowHelper")?.type, "ArrowHelper");
  assert.equal(h.state.endplanes.anteriorSeed, null, "display must not choose an opening");
  assertJsonEqual(h.state.endplanes.planes.anterior, plane);
  const model = h.frames.scene.children.flatMap((group) => group.children)
    .find((child) => child.material?.isMeshPhysicalMaterial);
  assert.equal(model.material.clippingPlanes.length, 0);
  h.setPlanes({ anterior: null, posterior: null });
  const remainingSections = [];
  h.frames.scene.traverse((object) => { if (object.userData.kind === "endplane-section") remainingSections.push(object); });
  assert.equal(remainingSections.length, 0, "removing a draft also removes its preview section");
  assert.equal(h.markers().length, 0, "no plane is invented before placement");
});

test("switching openings recolors the real section for the active nostril", () => {
  const h = viewerHarness();
  h.state.endplanes.activeInlet = "right";
  h.viewer.setFormat("oblique_bilateral_local_v3");
  h.setPlanes({
    anterior: { point_lps_mm: [0, -2, 0], normal_lps: [0, -1, 0] },
    posterior: { point_lps_mm: [0, 2, 0], normal_lps: [0, 1, 0] },
  });
  const sections = [];
  h.frames.scene.traverse((object) => { if (object.userData.kind === "endplane-section") sections.push(object); });
  const bySide = Object.fromEntries(sections.map((section) => [section.userData.side, section]));
  assert.equal(bySide.right.material.color.getHex(), 0x53d7c4);
  assert.equal(bySide.posterior.material.color.getHex(), 0xffb675);
  assert.equal(bySide.right.material.opacity, 0.24, "the active right nostril is emphasized");
});

test("oblique CT and marker stay on the actual cut plane for an asymmetric section", () => {
  const h = viewerHarness();
  const normal = new h.three.Vector3(1, 1, 1).normalize();
  const plane = { point_lps_mm: [4, 4, 4], normal_lps: normal.toArray() };
  h.setPlanes({ anterior: plane, posterior: null });
  const actual = new h.three.Plane().setFromNormalAndCoplanarPoint(normal, new h.three.Vector3(4, 4, 4));
  assert.ok(Math.abs(actual.distanceToPoint(h.markers()[0].position)) < 1e-6, "the bounding-box centre must be projected onto the cut plane");
  assert.ok(Math.abs(actual.distanceToPoint(new h.three.Vector3(...h.viewer.sectionPlane().plane.point_lps_mm))) < 1e-6, "CT candidates and save validation must use the same geometric plane");
  assertJsonEqual(h.state.endplanes.planes.anterior, plane);
});

test("flow arrows point into the airway at the nostril and toward the lung at the nasopharynx", () => {
  const context = vm.createContext({});
  vm.runInContext(functionSource("endplaneFlowDirection"), context);
  assert.deepEqual(context.endplaneFlowDirection("anterior", [0, -1, 0]), [0, 1, 0]);
  assert.deepEqual(context.endplaneFlowDirection("posterior", [0, 1, 0]), [0, 1, 0]);
});

test("marker keeps the clipping normal while exposing the flow direction separately", () => {
  const h = viewerHarness();
  h.setPlanes({
    anterior: { point_lps_mm: [0, -2, 0], normal_lps: [0, -1, 0] },
    posterior: { point_lps_mm: [0, 2, 0], normal_lps: [0, 1, 0] },
  });
  const markers = h.markers().filter((marker) => marker.userData.kind === "endplane-marker");
  assert.equal(markers.length, 2);
  const bySide = Object.fromEntries(markers.map((marker) => [marker.userData.side, marker]));
  assert.deepEqual(bySide.anterior.userData.flow_direction_lps, [0, 1, 0]);
  assert.deepEqual(bySide.posterior.userData.flow_direction_lps, [0, 1, 0]);
  assert.ok(h.context.planeNormalFromObject(bySide.anterior).distanceTo(new h.three.Vector3(0, -1, 0)) < 1e-6);
  assert.ok(h.context.planeNormalFromObject(bySide.posterior).distanceTo(new h.three.Vector3(0, 1, 0)) < 1e-6);
  for (const marker of markers) {
    const arrow = marker.children.find((child) => child.type === "ArrowHelper");
    assert.ok(arrow, "visible flow arrow is present");
    const actual = new h.three.Vector3(0, 1, 0)
      .applyQuaternion(arrow.quaternion)
      .applyQuaternion(marker.quaternion)
      .normalize();
    const expected = new h.three.Vector3(...marker.userData.flow_direction_lps).normalize();
    assert.ok(actual.distanceTo(expected) < 1e-6, `${marker.userData.side} arrow matches its flow vector`);
  }
  const anterior = bySide.anterior;
  anterior.quaternion.premultiply(new h.three.Quaternion().setFromAxisAngle(new h.three.Vector3(1, 0, 0), 0.35));
  const rotatedNormal = h.context.planeNormalFromObject(anterior);
  const rotatedFlow = new h.three.Vector3(...h.context.endplaneFlowDirection("anterior", rotatedNormal.toArray()));
  const rotatedArrow = anterior.children.find((child) => child.type === "ArrowHelper");
  const rotatedActual = new h.three.Vector3(0, 1, 0)
    .applyQuaternion(rotatedArrow.quaternion)
    .applyQuaternion(anterior.quaternion)
    .normalize();
  assert.ok(rotatedActual.distanceTo(rotatedFlow.normalize()) < 1e-6, "the arrow follows a rotated plane normal");
});

test("replacing markers releases their geometry and materials and preserves the peer marker", () => {
  const h = viewerHarness();
  const planes = {
    anterior: { point_lps_mm: [0, -2, 0], normal_lps: [0, -1, 0] },
    posterior: { point_lps_mm: [0, 2, 0], normal_lps: [0, 1, 0] },
  };
  h.setPlanes(planes);
  let resources = 0;
  let released = 0;
  h.markers().forEach((marker) => marker.traverse((child) => {
    for (const resource of [child.geometry, child.material].filter(Boolean)) {
      resources += 1;
      resource.addEventListener("dispose", () => { released += 1; });
    }
  }));
  h.setPlanes(planes);
  assert.equal(released, resources);
  assert.equal(h.markers().length, 2);
  h.viewer.clear();
  assert.equal(h.markers().length, 0);
});

test("ordinary inspection clicks do not move an already placed plane and the gizmo is opt-in", () => {
  const h = viewerHarness();
  const original = { point_lps_mm: [0, -2, 0], normal_lps: [0, -1, 0] };
  h.setPlanes({ anterior: original, posterior: null });
  h.state.endplanes.placement = null;
  h.click();
  assertJsonEqual(h.state.endplanes.planes.anterior, original);
  const transform = h.frames.scene.children.find((child) => typeof child.setMode === "function");
  assert.equal(transform.visible, false);
  h.viewer.setGizmo(true);
  assert.equal(transform.visible, true);
  h.viewer.setGizmo(false);
  assert.equal(transform.visible, false);
});

test("point placement starts parallel to the view and exits placement mode", () => {
  const h = viewerHarness();
  h.click();
  const normal = new h.three.Vector3(...h.state.endplanes.planes.anterior.normal_lps);
  const view = h.frames.camera.getWorldDirection(new h.three.Vector3()).negate();
  assert.ok(normal.dot(view) > 0.999);
  assert.equal(h.state.endplanes.placement, null);
  assert.equal(h.state.endplanes.history.anterior.length, 1);
  assert.equal(h.state.endplanes.history.anterior[0], null);
});

test("three-point plane rejects degenerate picks and orients toward the selected anatomical side", () => {
  const tools = require("../static/endplane-tools.js")(require("../static/vendor/three.min.js"));
  assert.equal(tools.throughPoints([[0, 0, 0], [1, 0, 0], [2, 0, 0]], [0, -1, 0]), null);
  assert.equal(tools.throughPoints([[0, 0, 0], [0.1, 0, 0], [0, 0, 1]], [0, -1, 0]), null);
  const plane = tools.throughPoints([[0, -2, 0], [3, -2, 0], [0, -2, 3]], [0, -1, 0]);
  assert.deepEqual(plane.point_lps_mm, [1, -2, 1]);
  assert.deepEqual(plane.normal_lps, [0, -1, 0]);
});

test("sliders rotate around the anchored opening and offset in millimetres along the resulting normal", () => {
  const three = require("../static/vendor/three.min.js"), tools = require("../static/endplane-tools.js")(three);
  const anchor = { point_lps_mm: [2, 3, 4], normal_lps: [0, -1, 0] };
  const tilted = tools.adjustedPlane(anchor, 0, 25, -15);
  assert.deepEqual(tilted.point_lps_mm, anchor.point_lps_mm);
  const moved = tools.adjustedPlane(anchor, 3.4, 25, -15);
  const delta = new three.Vector3(...moved.point_lps_mm).sub(new three.Vector3(...anchor.point_lps_mm));
  assert.ok(Math.abs(delta.length() - 3.4) < 1e-8);
  assert.ok(delta.normalize().dot(new three.Vector3(...moved.normal_lps)) > 0.99999);
  assert.deepEqual(tools.adjustedPlane(anchor, 0, 0, 0), anchor);
});

test("intersection follows the real mesh, clips against the peer, and reports no section outside it", () => {
  const three = require("../static/vendor/three.min.js"), tools = require("../static/endplane-tools.js")(three);
  const geometry = new three.BoxGeometry(10, 10, 10);
  const plane = { point_lps_mm: [0, 0, 0], normal_lps: [0, -1, 0] };
  const peer = { point_lps_mm: [0, 0, 0], normal_lps: [1, 0, 0] };
  const segments = tools.intersectionSegments(geometry, plane, peer);
  assert.ok(segments.length > 0);
  for (let i = 0; i < segments.length; i += 3) {
    assert.ok(Math.abs(segments[i + 1]) < 1e-6);
    assert.ok(segments[i] <= 1e-6);
    assert.ok(Math.abs(Math.abs(segments[i]) - 5) < 1e-6 || Math.abs(Math.abs(segments[i + 2]) - 5) < 1e-6);
  }
  assert.deepEqual(tools.intersectionSegments(geometry, { ...plane, point_lps_mm: [0, 30, 0] }), []);
});

test("section triangulation fills the real mesh boundary without extending beyond it", () => {
  const three = require("../static/vendor/three.min.js"), tools = require("../static/endplane-tools.js")(three);
  const geometry = new three.BoxGeometry(10, 10, 10, 2, 2, 2);
  const plane = { point_lps_mm: [0, 0, 0], normal_lps: [0, 1, 0] };
  const segments = tools.intersectionSegments(geometry, plane);
  const triangles = tools.sectionTriangles(segments, plane.normal_lps, 1e-3);
  assert.ok(triangles.length >= 18, "a mesh-derived section is triangulated");
  let area = 0;
  for (let i = 0; i < triangles.length; i += 9) {
    const a = new three.Vector3(...triangles.slice(i, i + 3));
    const b = new three.Vector3(...triangles.slice(i + 3, i + 6));
    const c = new three.Vector3(...triangles.slice(i + 6, i + 9));
    assert.ok(Math.abs(a.y) < 1e-6 && Math.abs(b.y) < 1e-6 && Math.abs(c.y) < 1e-6);
    assert.ok([a, b, c].every((point) => Math.abs(point.x) <= 5 + 1e-6 && Math.abs(point.z) <= 5 + 1e-6));
    area += b.clone().sub(a).cross(c.clone().sub(a)).length() / 2;
  }
  assert.ok(Math.abs(area - 100) < 1e-4);
  assert.deepEqual(tools.sectionTriangles([], plane.normal_lps), []);
});

test("local section fills only the mesh component nearest the selected nostril", () => {
  const three = require("../static/vendor/three.min.js"), tools = require("../static/endplane-tools.js")(three);
  const left = new three.BoxGeometry(2, 2, 2).toNonIndexed();
  const right = new three.BoxGeometry(2, 2, 2).toNonIndexed();
  left.translate(-5, 0, 0);
  right.translate(5, 0, 0);
  const positions = [...left.attributes.position.array, ...right.attributes.position.array];
  const geometry = new three.BufferGeometry();
  geometry.setAttribute("position", new three.Float32BufferAttribute(positions, 3));
  const plane = { point_lps_mm: [-5, 0, 0], normal_lps: [0, 1, 0] };
  const intersections = tools.intersectionSegments(geometry, plane);
  const local = tools.nearestIntersectionComponent(intersections, plane.point_lps_mm);
  const triangles = tools.sectionTriangles(local, plane.normal_lps);
  let area = 0;
  for (let i = 0; i < triangles.length; i += 9) {
    const a = new three.Vector3(...triangles.slice(i, i + 3));
    const b = new three.Vector3(...triangles.slice(i + 3, i + 6));
    const c = new three.Vector3(...triangles.slice(i + 6, i + 9));
    assert.ok([a, b, c].every((point) => point.x < -3), "the other nostril is excluded from the preview");
    area += b.clone().sub(a).cross(c.clone().sub(a)).length() / 2;
  }
  assert.ok(Math.abs(area - 4) < 1e-4, "only the 2 x 2 selected opening is filled");
});

test("local contour selection returns only the intersection loop nearest to the selected opening", () => {
  const three = require("../static/vendor/three.min.js"), tools = require("../static/endplane-tools.js")(three);
  const left = new three.BoxGeometry(2, 2, 2).toNonIndexed();
  const right = new three.BoxGeometry(2, 2, 2).toNonIndexed();
  left.translate(-5, 0, 0);
  right.translate(5, 0, 0);
  const positions = [...left.attributes.position.array, ...right.attributes.position.array];
  const geometry = new three.BufferGeometry();
  geometry.setAttribute("position", new three.Float32BufferAttribute(positions, 3));
  const plane = { point_lps_mm: [0, 0, 0], normal_lps: [0, 1, 0] };
  const segments = tools.intersectionSegments(geometry, plane);
  const local = tools.nearestIntersectionComponent(segments, [-5, 1.5, 0]);
  assert.ok(segments.length > local.length);
  assert.ok(local.length > 0);
  for (let index = 0; index < local.length; index += 3) assert.ok(local[index] < -3);
  assert.deepEqual(tools.nearestIntersectionComponent(segments, null), []);
});

test("local contour keeps the whole opening when mesh segments arrive out of order", () => {
  const tools = require("../static/endplane-tools.js")(require("../static/vendor/three.min.js"));
  const opening = [
    0, 0, 0, 1, 0, 0,
    1, 1, 0, 0, 1, 0,
    1, 0, 0, 1, 1, 0,
    0, 1, 0, 0, 0, 0,
  ];
  const remote = [10, 0, 0, 11, 0, 0];
  assert.deepEqual(tools.nearestIntersectionComponent([...opening, ...remote], [0.5, 0.5, 0.2]), opening);
});

test("portrait resize and overall reset keep the actual model bounds inside the view", () => {
  const h = viewerHarness();
  h.host.clientWidth = 240;
  h.host.clientHeight = 320;
  h.viewer.resize();
  for (const reset of [() => {}, () => h.viewer.resetCamera()]) {
    reset();
    const camera = h.frames.camera;
    camera.updateMatrixWorld(true);
    for (const x of [-5, 5]) for (const y of [-5, 5]) for (const z of [-5, 5]) {
      const point = new h.three.Vector3(x, y, z).project(camera);
      assert.ok(Math.abs(point.x) < 1 && Math.abs(point.y) < 1, "every corner fits in portrait");
    }
  }
});

test("local inlet view keeps full model visible and picks only the visible target branch", () => {
  const h = viewerHarness();
  h.viewer.setFormat("oblique_local_inlet_v2");
  h.setPlanes({
    anterior: { point_lps_mm: [0, -20, 0], normal_lps: [0, 1, 0] },
    posterior: null,
  });
  const modelGroup = h.frames.scene.children.find((group) =>
    group.children.some((child) => child.material?.isMeshPhysicalMaterial));
  assert.ok(modelGroup);
  assert.equal(modelGroup.children[0].material.clippingPlanes.length, 0);
  assert.equal(modelGroup.children[1].visible, false);
  h.state.endplanes.placement = "seed";
  h.click();
  assert.equal(h.state.endplanes.anteriorSeed.length, 3);
  assert.equal(h.state.endplanes.placement, null);
  assert.ok(h.frames.scene.children.some((group) => group.children.some((child) =>
    child.material?.color?.getHex() === 0xffd166)));
});

test("local preview shows the nearest model section before CT region selection", () => {
  for (const format of ["oblique_local_inlet_v2", "oblique_bilateral_local_v3"]) {
    const h = viewerHarness();
    h.viewer.setFormat(format);
    h.setPlanes({ anterior: { point_lps_mm: [0, 0, 0], normal_lps: [0, 1, 0] }, posterior: null });
    assert.equal(h.viewer.hasIntersection(), true);
    const lines = [];
    h.frames.scene.traverse((object) => { if (object.userData.kind === "endplane-section") lines.push(object); });
    assert.equal(lines.length, 1, "the cut surface appears before CT region selection");
    h.setPlanes({ anterior: { point_lps_mm: [0, 20, 0], normal_lps: [0, 1, 0] }, posterior: null });
    assert.equal(h.viewer.hasIntersection(), false);
    const removed = [];
    h.frames.scene.traverse((object) => { if (object.userData.kind === "endplane-section") removed.push(object); });
    assert.equal(removed.length, 0, "a plane outside the model cannot invent a section");
  }
});

test("actual preview mesh can be withdrawn without altering the original selection model", () => {
  const h = viewerHarness();
  h.viewer.setFormat("oblique_local_inlet_v2");
  const modelGroup = h.frames.scene.children.find((group) =>
    group.children.some((child) => child.material?.isMeshPhysicalMaterial));
  const original = modelGroup.children[0];
  const originalGeometry = original.geometry;
  h.viewer.setActualPreviewGeometry(new h.three.BoxGeometry(5, 5, 5));
  assert.equal(modelGroup.children.length, 3);
  assert.equal(original.material.opacity, 0.12);
  h.viewer.clearActualPreview();
  assert.equal(modelGroup.children.length, 2);
  assert.equal(original.material.opacity, 0.82);
  assert.equal(original.geometry, originalGeometry);
});

test("endplane smooth preview changes lighting normals but keeps the exact cut mesh coordinates", () => {
  const h = viewerHarness();
  const geometry = new h.three.BufferGeometry();
  geometry.setAttribute("position", new h.three.Float32BufferAttribute([
    0, 0, 0, 1, 0, 0, 0, 1, 0,
    0, 0, 0, 0, 1, 0, 0, 0, 1,
  ], 3));
  geometry.computeVertexNormals();
  const positions = Array.from(geometry.getAttribute("position").array);
  const rawNormals = Array.from(geometry.getAttribute("normal").array);
  h.viewer.setActualPreviewGeometry(geometry);
  const smoothNormals = Array.from(geometry.getAttribute("normal").array);
  assert.notDeepEqual(smoothNormals, rawNormals);
  assert.deepEqual(Array.from(geometry.getAttribute("position").array), positions);
  h.viewer.setPreviewMode("raw");
  assert.deepEqual(Array.from(geometry.getAttribute("normal").array), rawNormals);
  h.viewer.setPreviewMode("smooth");
  assert.deepEqual(Array.from(geometry.getAttribute("normal").array), smoothNormals);
  assert.deepEqual(Array.from(geometry.getAttribute("position").array), positions);
});
