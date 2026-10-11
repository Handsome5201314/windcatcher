"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
const start = source.match(/^async function refreshAfterMaskReview\(/m).index;
const functionSource = source.slice(start, source.indexOf("\n}", start) + 2);

function harness({ synthetic = false, path = "/segment/confirm", fail = false, switchCase = false } = {}) {
  const state = { caseId: "case-a", case: { case_id: "case-a", synthetic }, endplanes: { open: false } };
  const messages = [];
  const requests = [];
  const button = { textContent: "确认气道范围", disabled: false };
  const context = vm.createContext({
    state,
    guardEndplaneDraft: () => true,
    externalAirCleanupState: () => ({ pending: false }),
    busy: (node, value) => { node.disabled = value; },
    api: async (url) => {
      requests.push(url);
      if (switchCase) state.caseId = "case-b";
      if (fail) throw new Error("确认失败");
      return { message: "气道范围已确认" };
    },
    selectCase: async (id) => { state.caseId = id; return true; },
    endplaneContext: () => state.case.synthetic ? null : { caseId: state.caseId },
    $: (id) => {
      assert.equal(id, "btn-endplanes");
      return { click: () => { state.endplanes.open = !state.endplanes.open; } };
    },
    toast: (message, failed) => messages.push({ message, failed }),
  });
  vm.runInContext(functionSource, context);
  return { state, messages, requests, button, run: () => context.refreshAfterMaskReview(button, path, "已确认") };
}

test("confirming a real CT opens manual endplane selection before STL generation", async () => {
  const h = harness();
  await h.run();
  assert.equal(h.state.endplanes.open, true);
  assert.deepEqual(h.requests, ["/api/cases/case-a/segment/confirm"]);
  assert.equal(h.button.disabled, false);
  assert.equal(h.button.textContent, "确认气道范围");
});

test("synthetic confirmation does not open anatomical selection", async () => {
  const h = harness({ synthetic: true });
  await h.run();
  assert.equal(h.state.endplanes.open, false);
  assert.equal(h.messages[0].failed, undefined);
});

test("an already open endplane panel remains open after confirmation", async () => {
  const h = harness();
  h.state.endplanes.open = true;
  await h.run();
  assert.equal(h.state.endplanes.open, true);
});

test("failed confirmation leaves manual endplanes closed and reports the failure", async () => {
  const h = harness({ fail: true });
  await h.run();
  assert.equal(h.state.endplanes.open, false);
  assert.deepEqual(h.messages, [{ message: "确认失败", failed: true }]);
  assert.equal(h.button.disabled, false);
});

test("other mask operations do not open endplane selection", async () => {
  const h = harness({ path: "/segment/reset" });
  await h.run();
  assert.equal(h.state.endplanes.open, false);
});

test("late confirmation does not switch back or open the newly selected case", async () => {
  const h = harness({ switchCase: true });
  await h.run();
  assert.equal(h.state.caseId, "case-b");
  assert.equal(h.state.endplanes.open, false);
  assert.deepEqual(h.messages, []);
  assert.equal(h.button.disabled, false);
});
