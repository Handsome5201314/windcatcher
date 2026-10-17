"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
const handlerStart = source.indexOf('$("newcase-form").addEventListener("submit", async (event) => {');
const handlerEnd = source.indexOf("\n});", handlerStart);
const handlerSource = source.slice(handlerStart, handlerEnd + 4);

function harness({ fail = false } = {}) {
  let submit;
  const calls = [];
  const files = [{ name: "001.dcm", size: 10 }, { name: "002.dcm", size: 11 }];
  const state = { uploadFiles: files, cases: [], case: null, caseId: null };
  const sourceClasses = new Set(["is-selected"]);
  const nodes = {
    "newcase-form": { addEventListener: (_type, callback) => { submit = callback; } },
    "newcase-path": { value: "" },
    "newcase-synthetic": { checked: false },
    "newcase-anonymize": { checked: true },
    "btn-newcase": { disabled: false },
    "btn-pick-directory": { focus() {} },
    "newcase-upload": { value: "" },
    "selected-source": { textContent: "2 个文件 · 0 MB，提交时上传", classList: { remove: (x) => sourceClasses.delete(x) } },
  };
  const context = vm.createContext({
    window: { WindcatcherSharedAccess: true }, state,
    $: (id) => nodes[id], guardEndplaneDraft: () => true,
    busy: (button, value) => { button.disabled = value; },
    apiUploadCase: async (uploaded, synthetic, anonymize) => {
      calls.push({ uploaded, synthetic, anonymize });
      if (fail) throw new Error("网络中断");
      return { case_id: "case-upload-1" };
    },
    api: async () => { throw new Error("public mode must not use path API"); },
    loadCases: async () => { state.cases = [{ case_id: "case-upload-1" }]; },
    selectCase: async (id) => { state.caseId = id; state.case = { case_id: id }; },
    showRoute: () => {}, toast: () => {},
  });
  vm.runInContext(handlerSource, context);
  return { nodes, state, calls, sourceClasses, run: () => submit({ preventDefault() {} }) };
}

test("public import submits selected browser files instead of a local path", async () => {
  const h = harness();
  await h.run();
  assert.equal(h.calls.length, 1);
  assert.deepEqual(Array.from(h.calls[0].uploaded, (file) => file.name), ["001.dcm", "002.dcm"]);
  assert.equal(h.calls[0].synthetic, false);
  assert.equal(h.calls[0].anonymize, true);
  assert.equal(h.state.uploadFiles.length, 0);
  assert.equal(h.nodes["newcase-upload"].value, "");
});

test("failed public upload keeps selected files for retry", async () => {
  const h = harness({ fail: true });
  await h.run();
  assert.deepEqual(h.state.uploadFiles.map((file) => file.name), ["001.dcm", "002.dcm"]);
  assert.equal(h.nodes["btn-newcase"].disabled, false);
});
