"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
function productionFunction(name) {
  const start = source.indexOf("function " + name + "(");
  assert.ok(start >= 0, name + " exists");
  return source.slice(start, source.indexOf(String.fromCharCode(10) + "}", start) + 2);
}

function caseData(endplaneSource) {
  const stages = Object.fromEntries(
    ["import", "qc", "anonymize", "segment", "surface", "cfd_prep", "cfd_run", "report"]
      .map((name) => [name, { status: "pending", data: {} }]),
  );
  stages.surface = { status: "ok", data: {
    model_scope: "main_airway", verdict: "pass", endplanes: { source: endplaneSource },
  } };
  return { case_id: "anonymous-case", synthetic: false, stages };
}

test("single-branch geometry points to CFD limitation instead of offering preparation", () => {
  const box = { innerHTML: "", querySelectorAll: () => [], querySelector: () => null };
  const listeners = new Map();
  const primaryButton = { addEventListener: (name, fn) => listeners.set(name, fn) };
  const reportButton = { addEventListener() {} };
  let focused = false;
  const context = vm.createContext({
    STAGE_ORDER: ["import", "qc", "anonymize", "segment", "surface", "cfd_prep", "cfd_run", "report"],
    state: { clientRunningStage: null },
    $: (id) => id === "case-actions" ? box : id === "detail-surface" ? {}
      : id === "act-primary" ? primaryButton : reportButton,
    requiresMaskReview: () => false,
    modelScopeParams: () => ({ params: { model_scope: "main_airway" } }),
    cfdResourceCard: () => "resource settings",
    focusStageTarget: () => { focused = true; },
  });
  vm.runInContext(productionFunction("renderActions"), context);

  context.renderActions(caseData("user_confirmed_local_oblique_planes"));
  assert.match(box.innerHTML, /单支段模型：查看 CFD 限制/);
  assert.doesNotMatch(box.innerHTML, /resource settings|生成 CFD 算例/);
  listeners.get("click")();
  assert.equal(focused, true);

  context.renderActions(caseData("user_confirmed_oblique_planes"));
  assert.match(box.innerHTML, /生成 CFD 算例|resource settings/);
});

test("confirmed case without ports starts the real agent flow from the primary action", () => {
  const box = { innerHTML: "", querySelectorAll: () => [], querySelector: () => null };
  const listeners = new Map();
  const primaryButton = { addEventListener: (name, fn) => listeners.set(name, fn) };
  let launch = null;
  let route = null;
  let draftSafe = true;
  let retrySync = null;
  const context = vm.createContext({
    STAGE_ORDER: ["import", "qc", "anonymize", "segment", "surface", "cfd_prep", "cfd_run", "report"],
    state: { clientRunningStage: null, endplanes: { status: "ready" },
      maskEditor: { dirty: false, saving: false, autoCleaning: false } },
    $: id => id === "case-actions" ? box : id === "act-primary" ? primaryButton : { addEventListener() {} },
    requiresMaskReview: () => false,
    endplaneSaved: () => false,
    guardEndplaneDraft: () => draftSafe,
    modelScopeParams: () => ({ params: { model_scope: "main_airway" } }),
    window: { location: { hash: "work" }, WindcatcherOrchestration: { startCurrentCase: id => { launch = id; return Promise.resolve(); } } },
    showRoute: value => { route = value; },
    refreshCaseAfterAgent: id => { retrySync = id; },
    toast: () => {},
  });
  vm.runInContext(productionFunction("renderActions"), context);
  const data = caseData("");
  data.case_id = "case-20261102-120001-aaaa";
  data.stages.import.status = "ok";
  data.stages.qc.status = "ok";
  data.stages.segment.status = "ok";
  data.stages.surface.status = "pending";
  context.renderActions(data);
  assert.match(box.innerHTML, /自动处理当前病例/);
  assert.match(box.innerHTML, /data-action="start-agent"/);
  listeners.get("click")();
  assert.equal(route, "agent");
  assert.equal(launch, data.case_id);
  context.state.agentReturnRefreshingCaseId = data.case_id;
  context.renderActions(data);
  assert.match(box.innerHTML, /正在同步病例与端口状态…/);
  assert.match(box.innerHTML, /id="act-primary"[^>]*disabled/);
  context.state.agentReturnRefreshingCaseId = null;
  context.state.agentReturnRefreshErrorCaseId = data.case_id;
  context.renderActions(data);
  assert.match(box.innerHTML, /重试同步病例与端口状态/);
  launch = null;
  listeners.get("click")();
  assert.equal(retrySync, data.case_id);
  assert.equal(launch, null);
  context.state.agentReturnRefreshErrorCaseId = null;
  context.renderActions(data);
  launch = null; draftSafe = false;
  listeners.get("click")();
  assert.equal(launch, null, "an unsaved endplane draft must block automation");
  draftSafe = true; context.state.maskEditor.dirty = true;
  listeners.get("click")();
  assert.equal(launch, null, "an unsaved mask edit must block automation");
});

test("a pending or failed port profile cannot silently start another batch", () => {
  const box = { innerHTML: "", querySelectorAll: () => [], querySelector: () => null };
  const listeners = new Map();
  const primaryButton = { addEventListener: (name, fn) => listeners.set(name, fn) };
  let launched = false, retried = false, openedExpert = false, focusedExpert = false;
  const state = { clientRunningStage: null, endplanes: { status: "loading" }, maskEditor: {} };
  const context = vm.createContext({
    STAGE_ORDER: ["import", "qc", "anonymize", "segment", "surface", "cfd_prep", "cfd_run", "report"],
    state,
    $: id => id === "case-actions" ? box : id === "act-primary" ? primaryButton : { addEventListener() {} },
    requiresMaskReview: () => false, endplaneSaved: () => false,
    modelScopeParams: () => ({ params: { model_scope: "main_airway" } }),
    cfdResourceCard: () => "",
    endplaneContext: () => ({ key: "current" }),
    loadEndplaneProfile: () => { retried = true; },
    window: { WindcatcherOrchestration: { startCurrentCase: () => { launched = true; } },
      WindcatcherQuickPreview: { openAdvanced: () => { openedExpert = true; } } },
    focusStageTarget: () => { focusedExpert = true; },
  });
  vm.runInContext(productionFunction("renderActions"), context);
  const data = caseData("");
  data.stages.import.status = "ok";
  data.stages.qc.status = "ok";
  data.stages.segment.status = "ok";
  data.stages.surface.status = "pending";
  context.renderActions(data);
  assert.match(box.innerHTML, /正在核对已有端口…/);
  assert.match(box.innerHTML, /id="act-primary"[^>]*disabled/);
  assert.equal(launched, false);
  state.endplanes.status = "error";
  context.renderActions(data);
  assert.match(box.innerHTML, /重试读取端口状态/);
  listeners.get("click")();
  assert.equal(retried, true);
  assert.equal(launched, false);
  state.endplanes.status = "ready";
  data.stages.surface.status = "ok";
  context.renderActions(data);
  assert.match(box.innerHTML, /检查已有模型与端口/);
  assert.doesNotMatch(box.innerHTML, /data-action="start-agent"/);
  listeners.get("click")();
  assert.equal(openedExpert, true);
  assert.equal(focusedExpert, true);
  assert.equal(launched, false, "existing geometry must not submit a new batch");
});

test("returning from Agent refreshes saved ports without discarding an unsaved draft", async () => {
  const contextKey = "case-20261102-120001-aaaa|main_airway|1";
  const fresh = { case_id: "case-20261102-120001-aaaa", stages: { segment: { status: "ok" } } };
  async function visit({ dirty = false, saving = false, changedKey = false, failFirst = false } = {}) {
    const refreshed = [];
    let failNext = failFirst;
    const state = { route: "agent", caseId: fresh.case_id, case: { case_id: fresh.case_id },
      endplanes: { key: contextKey, dirty, saving }, viewer3d: { resize() {} } };
    const renderedDuringSync = [];
    const context = vm.createContext({
      routes: ["work", "agent"], state,
      $: () => ({ classList: { toggle() {} } }),
      document: { querySelectorAll: () => [] },
      window: {},
      api: async () => {
        if (failNext) { failNext = false; throw new Error("offline"); }
        return fresh;
      },
      renderCase: () => { if (changedKey) state.endplanes.key = "new-mask-key"; },
      renderActions: () => renderedDuringSync.push(state.agentReturnRefreshingCaseId),
      syncQuickPreview: () => {},
      endplaneContext: () => ({ key: contextKey }),
      loadEndplaneProfile: (profile, force) => refreshed.push({ profile, force }),
      hasRunningStages: () => false,
      disposeCfdResults: () => {},
      toast: () => {},
    });
    vm.runInContext(productionFunction("refreshCaseAfterAgent") + "\n" + productionFunction("showRoute"), context);
    context.showRoute("work");
    assert.equal(state.agentReturnRefreshingCaseId, fresh.case_id);
    await new Promise(setImmediate);
    if (failFirst) {
      assert.equal(state.agentReturnRefreshErrorCaseId, fresh.case_id, "a failed read keeps the retry state");
      context.refreshCaseAfterAgent(fresh.case_id);
      await new Promise(setImmediate);
    }
    assert.equal(state.case, fresh, "the case snapshot is refreshed on return");
    assert.equal(state.agentReturnRefreshingCaseId, null);
    assert.equal(state.agentReturnRefreshErrorCaseId, null);
    assert.ok(renderedDuringSync.includes(fresh.case_id));
    return { refreshed, state };
  }

  const clean = await visit();
  assert.equal(clean.refreshed.length, 1);
  assert.equal(clean.refreshed[0].force, true);
  assert.equal(clean.refreshed[0].profile.key, contextKey);
  assert.equal((await visit({ dirty: true })).refreshed.length, 0);
  assert.equal((await visit({ saving: true })).refreshed.length, 0);
  assert.equal((await visit({ changedKey: true })).refreshed.length, 0,
    "a changed mask version already starts its own profile load");
  assert.equal((await visit({ failFirst: true })).refreshed.length, 1);
});

test("single-branch QC is labeled as geometry, not an obsolete model", () => {
  const box = { innerHTML: "", querySelector: () => null };
  const context = vm.createContext({
    state: {},
    $: () => box,
    effectiveStageStatus: (_name, status) => status,
    requiresMaskReview: () => false,
    verdictText: (value) => value,
    kvList: () => "",
    fmt: (value) => String(value ?? ""),
    renderScenarioPanel: () => "",
  });
  vm.runInContext(productionFunction("renderDetails"), context);
  context.renderDetails(caseData("user_confirmed_local_oblique_planes"));
  assert.match(box.innerHTML, /单支段几何/);
  assert.match(box.innerHTML, /双侧 CFD 工况尚未定义/);
  assert.doesNotMatch(box.innerHTML, /旧模型待重建/);

  context.renderDetails(caseData("user_confirmed_bilateral_local_oblique_planes"));
  assert.doesNotMatch(box.innerHTML, /旧模型待重建|单支段几何|升级前生成/);
  context.renderDetails(caseData("automated_bilateral_local_oblique_planes"));
  assert.doesNotMatch(box.innerHTML, /旧模型待重建|单支段几何|升级前生成/);
  context.renderDetails(caseData("legacy_unverified"));
  assert.match(box.innerHTML, /旧模型待重建/);
});
