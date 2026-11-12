"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");
const source = fs.readFileSync(path.join(root, "static", "cfd-review-admin.js"), "utf8");
const appSource = fs.readFileSync(path.join(root, "static", "app.js"), "utf8");
const caseId = "case-20261031-120000-abcd";
const signature = "a".repeat(64);

function node(id = "") {
  return { id, value: "", textContent: "", hidden: true, disabled: false, children: [], listeners: {},
    classList: { toggle() {} },
    addEventListener(type, callback) { this.listeners[type] = callback; },
    replaceChildren(...items) { this.children = items; if (id === "cfd-review-case-id") this.value = ""; },
    querySelector() { return this.button ||= { disabled: false }; },
    querySelectorAll() { return []; },
  };
}

function harness({ ready = true, gateFails = false, postResponse = null } = {}) {
  const ids = ["cfd-review-case-id", "cfd-review-case-form", "cfd-review-content", "cfd-review-run",
    "cfd-review-numeric-status", "cfd-review-checks", "cfd-review-copy", "cfd-review-mesh-form",
    "cfd-review-engineer-form", "cfd-review-history", "cfd-review-status",
    "cfd-review-mesh-reviewer", "cfd-review-mesh-evidence", "cfd-review-mesh-note",
    "cfd-review-engineer-name", "cfd-review-engineer-decision", "cfd-review-engineer-evidence",
    "cfd-review-engineer-note", ...["coarse", "medium", "fine"].flatMap(role =>
      ["cfd-review-" + role + "-id", "cfd-review-" + role + "-cells", "cfd-review-" + role + "-dp"]),
  ];
  const nodes = Object.fromEntries(ids.map(id => [id, node(id)]));
  const calls = [];
  let clipboard = "";
  const context = vm.createContext({
    window: {}, navigator: { clipboard: { async writeText(value) { clipboard = value; } } },
    document: { getElementById: id => nodes[id], createElement: () => node() },
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      if (gateFails && url.endsWith("/gate")) return { ok: false, status: 409,
        json: async () => ({ detail: { reason: "掩膜未确认" } }) };
      if (url.endsWith("/gate")) return { ok: true, status: 200, json: async () => ({
        eligible: false, numeric_ok: false, checks: [
          { name: "残差收敛", status: "pending", detail: { reason: "未稳态收敛" } },
          { name: "网格敏感性", status: "pending" },
        ],
      }) };
      if (options.method === "POST") return postResponse ? postResponse() : { ok: true, status: 200,
        json: async () => ({ id: 1, status: "recorded_unverified", release_effect: "none" }) };
      return { ok: true, status: 200, json: async () => ({
        run: ready ? { status: "ready", run_id: "run-001", signature } :
          { status: "missing", reason: "尚无可复核的求解记录" },
        records: [],
      }) };
    },
  });
  vm.runInContext(source, context);
  return { nodes, calls, review: context.window.WindcatcherCfdReview, getClipboard: () => clipboard };
}

test("sidebar and sharing page expose a distinct CFD review destination", () => {
  assert.match(index, /data-route="cfd-review"[^>]*hidden/);
  assert.match(index, /id="page-cfd-review"/);
  assert.match(index, /id="share-open-cfd-review"/);
  assert.match(index, /id="cfd-review-mesh-form"/);
  assert.match(index, /id="cfd-review-engineer-form"/);
});

test("hosted sidebar opens CFD review while local workspace hides it", () => {
  const start = appSource.indexOf("const routes = [");
  const end = appSource.indexOf("function focusStageTarget", start);
  assert.ok(start >= 0 && end > start);
  for (const hosted of [true, false]) {
    const opened = {};
    const reviewButton = { dataset: { route: "cfd-review" }, hidden: true, classList: { toggle() {} }, addEventListener() {}, setAttribute() {}, removeAttribute() {} };
    const shareButton = { dataset: { route: "share" }, hidden: true, classList: { toggle() {} }, addEventListener() {}, setAttribute() {}, removeAttribute() {} };
    const workButton = { dataset: { route: "work" }, hidden: false, classList: { toggle() {} }, addEventListener() {}, setAttribute() {}, removeAttribute() {} };
    const pages = {};
    const context = vm.createContext({
      window: { WindcatcherCloudAccess: hosted, location: { hash: "" },
        WindcatcherCfdReview: { activate: () => { opened.review = true; } }, addEventListener() {} },
      state: { route: "work", cases: [], caseId: null },
      document: { querySelector: selector => selector.includes("cfd-review") ? reviewButton : shareButton,
        querySelectorAll: () => [workButton, reviewButton, shareButton] },
      $: id => pages[id] ||= { classList: { toggle: (_name, enabled) => { opened[id] = enabled; } } },
      loadEnv() {}, loadCfdResults() {}, renderCfdResultsError() {}, disposeCfdResults() {}, toast() {},
    });
    vm.runInContext(appSource.slice(start, end), context);
    vm.runInContext('showRoute("cfd-review")', context);
    assert.equal(reviewButton.hidden, !hosted);
    assert.equal(opened["page-cfd-review"], hosted ? true : undefined);
    assert.equal(opened["page-work"], !hosted);
    assert.equal(Boolean(opened.review), hosted);
  }
});

test("review page shows failed numeric gate and copies an anonymous summary", async () => {
  const h = harness();
  await h.review.activate([{ case_id: caseId }], caseId);
  assert.equal(h.nodes["cfd-review-content"].hidden, false);
  assert.match(h.nodes["cfd-review-numeric-status"].textContent, /数值检查未通过/);
  assert.match(h.nodes["cfd-review-checks"].children.map(item => item.textContent).join(" "), /残差收敛.*网格敏感性/);
  await h.nodes["cfd-review-copy"].listeners.click();
  assert.match(h.getClipboard(), /患者发布：禁止/);
  assert.match(h.getClipboard(), /run-001/);
  assert.ok(h.getClipboard().includes("\n"));
});

test("grid evidence posts current run signature without approving it", async () => {
  const h = harness();
  await h.review.activate([{ case_id: caseId }], caseId);
  h.nodes["cfd-review-mesh-reviewer"].value = "测试工程师";
  h.nodes["cfd-review-mesh-evidence"].value = "mesh-report-001";
  h.nodes["cfd-review-mesh-note"].value = "仍需核实独立网格算例。";
  for (const [role, cells] of [["coarse", 100], ["medium", 200], ["fine", 400]]) {
    h.nodes["cfd-review-" + role + "-id"].value = role + "-run";
    h.nodes["cfd-review-" + role + "-cells"].value = String(cells);
    h.nodes["cfd-review-" + role + "-dp"].value = "12.3";
  }
  await h.nodes["cfd-review-mesh-form"].listeners.submit({ preventDefault() {}, currentTarget: h.nodes["cfd-review-mesh-form"] });
  const post = h.calls.find(call => call.options.method === "POST");
  assert.ok(post);
  const body = JSON.parse(post.options.body);
  assert.equal(body.expected_signature, signature);
  assert.equal(body.kind, "mesh_sensitivity");
  assert.equal(body.grids.length, 3);
  assert.equal(body.grids[2].cells, 400);
  assert.equal(body.decision, undefined);
});

test("missing run or failed gate stays visible without an approval action", async () => {
  const h = harness({ ready: false, gateFails: true });
  await h.review.activate([{ case_id: caseId }], caseId);
  assert.equal(h.nodes["cfd-review-content"].hidden, false);
  assert.equal(h.nodes["cfd-review-copy"].disabled, true);
  assert.match(h.nodes["cfd-review-numeric-status"].textContent, /掩膜未确认/);
  assert.match(h.nodes["cfd-review-status"].textContent, /先完成求解/);
});

test("failed gate disables copying but still shows the evidence forms for a completed run", async () => {
  const h = harness({ gateFails: true });
  await h.review.activate([{ case_id: caseId }], caseId);
  assert.equal(h.nodes["cfd-review-content"].hidden, false);
  assert.equal(h.nodes["cfd-review-copy"].disabled, true);
  assert.match(h.nodes["cfd-review-numeric-status"].textContent, /掩膜未确认/);
});

test("a delayed save cannot overwrite the status of a newly selected case", async () => {
  let finishPost;
  const pending = new Promise(resolve => { finishPost = resolve; });
  const h = harness({ postResponse: () => pending });
  const otherCase = "case-20261031-120001-abcd";
  await h.review.activate([{ case_id: caseId }, { case_id: otherCase }], caseId);
  h.nodes["cfd-review-engineer-name"].value = "测试工程师";
  h.nodes["cfd-review-engineer-decision"].value = "reviewed";
  h.nodes["cfd-review-engineer-evidence"].value = "review-report-001";
  h.nodes["cfd-review-engineer-note"].value = "需要独立核实后再提交审核。";
  const saving = h.nodes["cfd-review-engineer-form"].listeners.submit({
    preventDefault() {}, currentTarget: h.nodes["cfd-review-engineer-form"],
  });
  h.nodes["cfd-review-case-id"].value = otherCase;
  await h.nodes["cfd-review-case-id"].listeners.change();
  const currentMessage = h.nodes["cfd-review-status"].textContent;
  finishPost({ ok: true, status: 200, json: async () => ({ release_effect: "none" }) });
  await saving;
  assert.equal(h.nodes["cfd-review-status"].textContent, currentMessage);
  assert.equal(h.calls.filter(call => call.options.method === "POST").length, 1);
});
