"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const handlerStart = source.indexOf('$("newcase-form").addEventListener("submit", async (event) => {');
assert.notEqual(handlerStart, -1, "the production import submit handler must exist");
const handlerEnd = source.indexOf("\n});", handlerStart);
assert.notEqual(handlerEnd, -1, "the production import submit handler must be complete");
const handlerSource = source.slice(handlerStart, handlerEnd + 4);

function inputInitiallyChecked(id) {
  const input = html.match(new RegExp(`<input\\b[^>]*\\bid="${id}"[^>]*>`));
  assert.ok(input, `the ${id} input must exist`);
  return /\schecked(?:\s|=|\/?>)/.test(input[0]);
}

function harness({ failFirst = false } = {}) {
  let submit;
  let attempts = 0;
  let prevented = 0;
  const imports = [];
  const requests = [];
  const messages = [];
  const routes = [];
  const sourceClasses = new Set(["is-selected"]);
  const state = { cases: [], case: null, caseId: null };
  const nodes = {
    "newcase-form": {
      addEventListener(type, callback) {
        assert.equal(type, "submit");
        submit = callback;
      },
    },
    "newcase-path": { value: "" },
    "newcase-synthetic": { checked: inputInitiallyChecked("newcase-synthetic") },
    "newcase-anonymize": { checked: inputInitiallyChecked("newcase-anonymize") },
    "btn-newcase": { disabled: false },
    "selected-source": {
      textContent: "已选择 DICOM 文件夹",
      classList: { remove: (name) => sourceClasses.delete(name) },
    },
  };
  const context = vm.createContext({
    $: (id) => {
      assert.ok(nodes[id], `unexpected element ${id}`);
      return nodes[id];
    },
    guardEndplaneDraft: () => true,
    busy: (button, value) => { button.disabled = value; },
    api: async (url, options) => {
      requests.push({ url, ...JSON.parse(JSON.stringify(options)) });
      attempts += 1;
      if (failFirst && attempts === 1) {
        throw Object.assign(new Error("import failed"), { detail: { reason: "DICOM 目录读取失败" } });
      }
      const imported = { case_id: `case-${imports.length + 1}`, ...options.body };
      imports.push(imported);
      return imported;
    },
    loadCases: async () => { state.cases = imports.map((item) => ({ ...item })); },
    selectCase: async (id) => {
      const selected = state.cases.find((item) => item.case_id === id);
      assert.ok(selected, "the imported case must be loaded before it is selected");
      state.caseId = selected.case_id;
      state.case = selected;
      return true;
    },
    showRoute: (route) => routes.push(route),
    toast: (message, failed) => messages.push({ message, failed }),
  });
  vm.runInContext(handlerSource, context);
  assert.equal(typeof submit, "function");
  return {
    nodes, state, imports, requests, messages, routes, sourceClasses,
    get prevented() { return prevented; },
    run: () => submit({ preventDefault: () => { prevented += 1; } }),
  };
}

test("a successful synthetic import resets the checkbox before the next real CT import", async () => {
  const h = harness();
  h.nodes["newcase-path"].value = "  E:\\CT\\phantom  ";
  h.nodes["newcase-synthetic"].checked = true;
  await h.run();

  assert.deepEqual(h.requests[0], {
    url: "/api/cases", method: "POST",
    body: { path: "E:\\CT\\phantom", synthetic: true, anonymize: true },
  });
  assert.equal(h.state.caseId, "case-1");
  assert.equal(h.state.case.synthetic, true);
  assert.equal(h.nodes["newcase-path"].value, "");
  assert.equal(h.nodes["newcase-synthetic"].checked, false);
  assert.equal(h.nodes["newcase-anonymize"].checked, true);
  assert.equal(h.sourceClasses.has("is-selected"), false);
  assert.equal(h.nodes["btn-newcase"].disabled, false);

  h.nodes["newcase-path"].value = "E:\\CT\\public-human-ct";
  await h.run();

  assert.deepEqual(h.requests[1], {
    url: "/api/cases", method: "POST",
    body: { path: "E:\\CT\\public-human-ct", synthetic: false, anonymize: true },
  });
  assert.deepEqual(h.state.cases.map((item) => item.synthetic), [true, false]);
  assert.equal(h.state.caseId, "case-2");
  assert.equal(h.state.case.synthetic, false);
  assert.equal(h.nodes["newcase-path"].value, "");
  assert.equal(h.nodes["newcase-synthetic"].checked, false);
  assert.equal(h.nodes["btn-newcase"].disabled, false);
  assert.deepEqual(h.routes, ["work", "work"]);
  assert.equal(h.prevented, 2);
  assert.deepEqual(h.messages, [
    { message: "已登记病例，正在处理：case-1", failed: undefined },
    { message: "已登记病例，正在处理：case-2", failed: undefined },
  ]);
});

test("a failed import preserves the directory and options so the same submission can be retried", async () => {
  const h = harness({ failFirst: true });
  const selectedPath = "  E:\\CT\\phantom  ";
  h.nodes["newcase-path"].value = selectedPath;
  h.nodes["newcase-synthetic"].checked = true;
  h.nodes["newcase-anonymize"].checked = false;
  await h.run();

  assert.deepEqual(h.requests[0], {
    url: "/api/cases", method: "POST",
    body: { path: "E:\\CT\\phantom", synthetic: true, anonymize: false },
  });
  assert.equal(h.nodes["newcase-path"].value, selectedPath);
  assert.equal(h.nodes["newcase-synthetic"].checked, true);
  assert.equal(h.nodes["newcase-anonymize"].checked, false);
  assert.equal(h.nodes["selected-source"].textContent, "已选择 DICOM 文件夹");
  assert.equal(h.sourceClasses.has("is-selected"), true);
  assert.equal(h.nodes["btn-newcase"].disabled, false);
  assert.equal(h.state.case, null);
  assert.deepEqual(h.state.cases, []);
  assert.deepEqual(h.imports, []);
  assert.deepEqual(h.routes, []);
  assert.deepEqual(h.messages, [{ message: "DICOM 目录读取失败", failed: true }]);

  await h.run();

  assert.deepEqual(h.requests[1], h.requests[0]);
  assert.equal(h.state.caseId, "case-1");
  assert.equal(h.state.case.synthetic, true);
  assert.equal(h.state.case.anonymize, false);
  assert.equal(h.nodes["newcase-path"].value, "");
  assert.equal(h.nodes["newcase-synthetic"].checked, false);
  assert.equal(h.nodes["newcase-anonymize"].checked, false);
  assert.equal(h.nodes["btn-newcase"].disabled, false);
  assert.deepEqual(h.routes, ["work"]);
  assert.equal(h.prevented, 2);
});

test("a normal CT import uses the unchecked synthetic default from the actual form", async () => {
  const h = harness();
  assert.equal(h.nodes["newcase-synthetic"].checked, false);
  h.nodes["newcase-path"].value = "E:\\CT\\human-ct";
  await h.run();

  assert.deepEqual(h.requests, [{
    url: "/api/cases", method: "POST",
    body: { path: "E:\\CT\\human-ct", synthetic: false, anonymize: true },
  }]);
  assert.equal(h.state.case.path, "E:\\CT\\human-ct");
  assert.equal(h.state.case.synthetic, false);
  assert.equal(h.state.case.anonymize, true);
  assert.equal(h.nodes["btn-newcase"].disabled, false);
  assert.deepEqual(h.routes, ["work"]);
  assert.equal(h.messages[0].failed, undefined);
});

test("the form reserves synthetic mode for artificial CT and excludes public human CT", () => {
  const label = html.match(/<label\b[^>]*>\s*<input\b[^>]*\bid="newcase-synthetic"[^>]*>([\s\S]*?)<\/label>/);
  const help = html.match(/<p\b[^>]*\bid="synthetic-help"[^>]*>([\s\S]*?)<\/p>/);
  assert.ok(label, "the synthetic option must have visible explanatory text");
  assert.ok(help, "the synthetic option must explain when anatomical selection is skipped");
  assert.match(label[1], /仅用于人工生成的合成测试 CT/);
  assert.doesNotMatch(label[1], /公开|脱敏/);
  assert.match(help[1], /合成测试模式会跳过人工端面选择/);
  assert.match(help[1], /真实人体 CT，包括公开或已脱敏的数据，请不要勾选/);
});
