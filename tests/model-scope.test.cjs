"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
function fn(name) {
  const start = source.match(new RegExp(`^(?:async )?function ${name}\\(`, "m")).index;
  return source.slice(start, source.indexOf("\n}", start) + 2);
}
function harness(saved = null, unavailableStorage = false) {
  const start = source.indexOf("const state = {");
  const context = vm.createContext({
    window: {},
    localStorage: {
      getItem: (key) => { if (unavailableStorage) throw new Error("Storage disabled"); return key.endsWith(":case-a") ? saved : null; },
      setItem() {},
    },
    stopCasePolling() {}, destroyScenarioViewers() {}, resetEndplaneState() {}, resetCtViewer() {},
    loadCases: async () => {}, renderCase() {}, hasRunningStages: () => false, guardEndplaneDraft: () => true,
    api: async (url) => url === "/api/capabilities" ? { compute: {} } : { case_id: "case-a", stages: { segment: { data: { candidate_mask_available: true } } } },
  });
  vm.runInContext([
    source.slice(start, source.indexOf("\n};", start) + 3),
    'const MODEL_SCOPE_OPTIONS = {main_nasal_airway:"主鼻腔气流模型",complete_candidate:"完整候选范围"};',
    ...["modelScopeStorageKey", "getModelScope", "modelScopeParams", "selectCase"].map(fn),
    "globalThis.state = state;",
  ].join("\n"), context);
  return context;
}

test("fresh workspace and newly selected cases use the main nasal airway for the API", async () => {
  const h = harness();
  assert.equal(h.state.modelScope, "main_nasal_airway");
  assert.equal(h.getModelScope(), "main_nasal_airway");
  await h.selectCase("case-a");
  assert.equal(h.state.modelScope, "main_nasal_airway");
  assert.equal(h.modelScopeParams("case-a").params.model_scope, "main_airway");
  assert.equal(h.modelScopeParams("case-a").params.keep_largest_component, true);
});

test("an explicitly saved full-candidate selection stays available per case", async () => {
  const h = harness("complete_candidate");
  await h.selectCase("case-a");
  assert.equal(h.getModelScope(), "complete_candidate");
  assert.equal(h.modelScopeParams("case-a").params.model_scope, "full_candidate");
  await h.selectCase("case-b");
  assert.equal(h.getModelScope(), "main_nasal_airway", "another case does not inherit that selection");
});

for (const [name, saved, blocked] of [["invalid preference", "old-invalid-scope", false], ["storage unavailable", null, true]]) {
  test(`${name} falls back to the main airway`, async () => {
    const h = harness(saved, blocked);
    assert.equal(h.getModelScope("case-a"), "main_nasal_airway");
    await h.selectCase("case-a");
    assert.equal(h.getModelScope(), "main_nasal_airway");
  });
}
