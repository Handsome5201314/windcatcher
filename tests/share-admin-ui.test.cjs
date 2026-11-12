"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static", "share-admin.js"), "utf8");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");
const appSource = fs.readFileSync(path.join(root, "static", "app.js"), "utf8");
const firstCase = "case-20261031-120000-abcd";
const secondCase = "case-20261031-130000-ef01";

function element(id = "") {
  const node = { id, value: "", textContent: "", checked: false,
    hidden: id === "admin-content" || id === "issued" || id === "test-issued" || id === "share-gate",
    children: [], listeners: {}, disabled: false, className: "",
    classList: { toggle() {} },
    addEventListener(type, callback) { this.listeners[type] = callback; },
    replaceChildren(...children) { this.children = children; if (id === "case-id") this.value = ""; },
    append(...children) { this.children.push(...children); },
    querySelector() { return this.button ||= { disabled: false }; },
  };
  return node;
}

function harness({ failRead = false, failGate = false, gate = { eligible: true, numeric_ok: true, checks: [] } } = {}) {
  const ids = ["admin-status", "case-id", "case-form", "admin-content", "issued", "reviews", "shares",
    "review-form", "publish-form", "operator", "review-stage", "review-action", "review-reason",
    "reviewer", "review-note", "days", "issued-link", "issued-code",
    "test-publish-form", "test-operator", "test-reason", "test-recipient-confirmed",
    "test-issued", "test-issued-link", "test-issued-code",
    "share-gate", "share-gate-status", "share-gate-checks", "publish-submit", "share-open-cfd-review"];
  const nodes = Object.fromEntries(ids.map(id => [id, element(id)]));
  const calls = [];
  const window = {};
  const context = vm.createContext({
    window, location: { origin: "https://example.test" },
    document: { getElementById: id => nodes[id], createElement: () => element() },
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      if (failRead && url.endsWith("/review-events")) return { ok: false, status: 403, text: async () => JSON.stringify({ detail: { reason: "无权读取" } }) };
      if (failGate && url.endsWith("/gate")) return { ok: false, status: 409, text: async () => JSON.stringify({ detail: { reason: "掩膜尚未确认" } }) };
      if (url.endsWith("/gate")) return { ok: true, status: 200, text: async () => JSON.stringify(gate) };
      const body = options.method === "POST" && (url.endsWith("/shares") || url.endsWith("/shares/test"))
        ? { url: "/share/abc", access_code: "test-code" } : [];
      return { ok: true, status: 200, text: async () => JSON.stringify(body) };
    },
  });
  vm.runInContext(source, context);
  return { nodes, calls, admin: window.WindcatcherShareAdmin };
}

test("share management lives in the main shell and selects an existing case", async () => {
  assert.match(index, /data-route="share"[^>]*hidden/);
  assert.match(index, /id="page-share"/);
  assert.match(index, /id="case-id"[^>]*class="case-select"/);
  const h = harness();
  await h.admin.activate([{ case_id: firstCase }, { case_id: secondCase }], firstCase);
  assert.equal(h.nodes["case-id"].value, firstCase);
  assert.equal(h.nodes["admin-content"].hidden, false);
  assert.deepEqual(h.calls.map(call => call.url), [
    `/api/cases/${firstCase}/review-events`, `/api/cases/${firstCase}/shares`, `/api/cases/${firstCase}/gate`,
  ]);
  h.nodes["case-id"].value = secondCase;
  await h.nodes["case-id"].listeners.change();
  assert.equal(h.nodes["admin-content"].hidden, false);
  assert.equal(h.calls[3].url, `/api/cases/${secondCase}/review-events`);
});

test("failed CFD checks are visible and prevent publishing", async () => {
  assert.match(index, /id="share-gate"/);
  const h = harness({ gate: { eligible: false, numeric_ok: false, checks: [
    { name: "残差收敛", status: "pending" },
    { name: "网格敏感性", status: "pending" },
    { name: "CFD 工程师复核", status: "pending" },
  ] } });
  await h.admin.activate([{ case_id: firstCase }], firstCase);
  assert.equal(h.nodes["share-gate"].hidden, false);
  assert.match(h.nodes["share-gate-status"].textContent, /暂不能发布/);
  assert.equal(h.nodes["publish-submit"].disabled, true);
  assert.match(h.nodes["share-gate-checks"].children.map(item => item.textContent).join(" "), /残差收敛.*网格敏感性.*工程师复核/);
  await h.nodes["publish-form"].listeners.submit({ preventDefault() {}, currentTarget: h.nodes["publish-form"] });
  assert.equal(h.calls.some(call => call.url.endsWith("/shares") && call.options.method === "POST"), false);
});

test("unverified test share is separate from formal approval and requires recipient consent", async () => {
  assert.match(index, /id="test-publish-form"/);
  const h = harness({ gate: { eligible: false, numeric_ok: false, checks: [
    { name: "残差收敛", status: "pending" },
  ] } });
  await h.admin.activate([{ case_id: firstCase }], firstCase);
  h.nodes["test-operator"].value = "测试操作员";
  h.nodes["test-reason"].value = "仅核对链接和访问码是否正常";
  const submit = () => h.nodes["test-publish-form"].listeners.submit({
    preventDefault() {}, currentTarget: h.nodes["test-publish-form"],
  });
  await submit();
  assert.equal(h.calls.some(call => call.url.endsWith("/shares/test") && call.options.method === "POST"), false);
  h.nodes["test-recipient-confirmed"].checked = true;
  await submit();
  const posted = h.calls.find(call => call.url === "/api/cases/" + firstCase + "/shares/test" && call.options.method === "POST");
  assert.ok(posted);
  assert.equal(JSON.parse(posted.options.body).recipient_confirmed, true);
  assert.equal(h.nodes["publish-submit"].disabled, true);
  assert.equal(h.nodes["test-issued"].hidden, false);
  assert.equal(h.nodes["test-issued-link"].value, "https://example.test/share/abc");
  assert.equal(h.nodes["test-issued-code"].value, "test-code");
  assert.equal(h.nodes["test-recipient-confirmed"].checked, false);
});

test("publishing keeps the selected case and shows the one-time link and code", async () => {
  const h = harness();
  await h.admin.activate([{ case_id: firstCase }], firstCase);
  h.nodes.reviewer.value = "测试审核员";
  h.nodes["review-note"].value = "已完成指定版本的人工复核";
  h.nodes.days.value = "7";
  await h.nodes["publish-form"].listeners.submit({ preventDefault() {}, currentTarget: h.nodes["publish-form"] });
  const publish = h.calls.find(call => call.url === `/api/cases/${firstCase}/shares` && call.options.method === "POST");
  assert.ok(publish);
  assert.equal(JSON.parse(publish.options.body).days, 7);
  assert.equal(h.nodes.issued.hidden, false);
  assert.equal(h.nodes["issued-link"].value, "https://example.test/share/abc");
  assert.equal(h.nodes["issued-code"].value, "test-code");
});

test("failed case read leaves publishing controls hidden with a visible error", async () => {
  const h = harness({ failRead: true });
  await h.admin.activate([{ case_id: firstCase }], firstCase);
  assert.equal(h.nodes["admin-content"].hidden, true);
  assert.equal(h.nodes["admin-status"].textContent, "无权读取");
});

test("unavailable gate keeps review records accessible but publishing blocked", async () => {
  const h = harness({ failGate: true });
  await h.admin.activate([{ case_id: firstCase }], firstCase);
  assert.equal(h.nodes["admin-content"].hidden, false);
  assert.equal(h.nodes["share-gate"].hidden, false);
  assert.match(h.nodes["share-gate-status"].textContent, /掩膜尚未确认/);
  assert.equal(h.nodes["publish-submit"].disabled, true);
  await h.nodes["publish-form"].listeners.submit({ preventDefault() {}, currentTarget: h.nodes["publish-form"] });
  assert.equal(h.calls.some(call => call.url.endsWith("/shares") && call.options.method === "POST"), false);
});

test("hosted sidebar opens sharing while the local workspace keeps it hidden", () => {
  const start = appSource.indexOf("const routes = [");
  const end = appSource.indexOf("function focusStageTarget", start);
  assert.ok(start >= 0 && end > start);
  for (const hosted of [true, false]) {
    const active = {};
    const shareButton = { dataset: { route: "share" }, hidden: true, classList: { toggle() {} }, addEventListener() {}, setAttribute() {}, removeAttribute() {} };
    const workButton = { dataset: { route: "work" }, classList: { toggle() {} }, addEventListener() {}, setAttribute() {}, removeAttribute() {} };
    const windows = { WindcatcherCloudAccess: hosted, location: { hash: "" },
      WindcatcherShareAdmin: { activate: () => { active.opened = true; } }, addEventListener() {} };
    const pages = {};
    const context = vm.createContext({
      window: windows, state: { route: "work", cases: [], caseId: null },
      document: { querySelector: () => shareButton, querySelectorAll: () => [workButton, shareButton] },
      $: id => pages[id] ||= { classList: { toggle: (_name, enabled) => { active[id] = enabled; } } },
      loadEnv() {}, loadCfdResults() {}, renderCfdResultsError() {}, disposeCfdResults() {}, toast() {},
    });
    vm.runInContext(appSource.slice(start, end), context);
    vm.runInContext('showRoute("share")', context);
    assert.equal(shareButton.hidden, !hosted);
    assert.equal(active["page-share"], hosted ? true : undefined);
    assert.equal(active["page-work"], !hosted);
    assert.equal(Boolean(active.opened), hosted);
  }
});
