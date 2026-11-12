"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static", "harness-advice.js"), "utf8");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "static", "app.css"), "utf8");
const app = fs.readFileSync(path.join(root, "static", "app.js"), "utf8");

function element() {
  const classes = new Set();
  return { hidden: true, disabled: false, textContent: "", listeners: {}, attributes: {},
    classList: { toggle(name, on) { if (on) classes.add(name); else classes.delete(name); }, contains(name) { return classes.has(name); } },
    addEventListener(type, listener) { this.listeners[type] = listener; },
    setAttribute(name, value) { this.attributes[name] = value; },
  };
}

function reply(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function advice(caseId, overrides = {}) {
  return { status: "advisory_only", source: "deepseek-harness", case_id: caseId,
    human_review_required: true, automated_action: false, input_sha256: "a".repeat(64),
    next_step: { code: "review_numeric_gate", stage: "cfd_run" },
    failure_recap: ["numeric_gate_failed"],
    metering: { source: "deepseek-harness-sdk-events", status: "reported",
      input_tokens: 12, output_tokens: 8, total_tokens: 20, cache_read_tokens: null,
      cost_usd: null },
    skill_draft: { trigger_condition: "manual", suggested_steps: ["review"], allowed_tools: [],
      arbitrary: "secret text must not render" },
    ...overrides };
}

function harness({ multiuser = true, pilotEnabled = true, fetchImpl = async () => reply(503, {}) } = {}) {
  const ids = [...new Set([...index.matchAll(/id="([^"]+)"/g)].map(match => match[1]))];
  const nodes = Object.fromEntries(ids.map(id => [id, element()]));
  const calls = [];
  const fetch = (...args) => { calls.push(args); return fetchImpl(...args); };
  const window = { WindcatcherMultiuser: multiuser, WindcatcherHarnessEnabled: pilotEnabled };
  vm.runInNewContext(source, { window, document: { getElementById: id => nodes[id] }, fetch, Object });
  return { nodes, calls, ui: window.WindcatcherHarnessAdvice };
}

test("advice is a collapsed optional tool on the agent page", () => {
  const agent = index.slice(index.indexOf('id="page-agent"'), index.indexOf('id="page-cases"'));
  assert.match(agent, /<details id="harness-advice-panel"[^>]*hidden/);
  assert.doesNotMatch(agent, /<details id="harness-advice-panel"[^>]* open/);
  assert.ok(index.indexOf('id="case-select"') < index.indexOf('id="harness-advice-panel"'));
  assert.match(agent, /id="harness-advice-case"/);
  assert.match(index, /id="harness-advice-status"[^>]*aria-live="polite"/);
  assert.ok(index.indexOf("/static/harness-advice.js") < index.indexOf("/static/app.js"));
  assert.ok(app.includes("window.WindcatcherHarnessAdvice?.setCase(null);"));
  assert.ok(app.includes("window.WindcatcherHarnessAdvice?.setCase(caseId);"));
  assert.ok(css.includes("@media (max-width: 700px)"));
  assert.ok(css.includes(".harness-advice-toolbar .button { width: 100%"));
  assert.ok(css.includes(".harness-advice-result[hidden] { display: none; }"));
  assert.match(css, /:focus-visible/);
  const local = harness({ multiuser: false });
  local.ui.setCase("case-a");
  assert.equal(local.nodes["harness-advice-panel"].hidden, true);
  assert.equal(local.nodes["harness-advice-generate"].disabled, true);
  assert.equal(local.calls.length, 0);
  const closed = harness({ pilotEnabled: false });
  closed.ui.setCase("case-a");
  assert.equal(closed.nodes["harness-advice-generate"].disabled, true);
  assert.match(closed.nodes["harness-advice-status"].textContent, /尚未启用/);
});

test("manual request renders only approved codes and hides free text", async () => {
  const h = harness({ fetchImpl: async () => reply(200, advice("case-a", {
    arbitrary_text: "untrusted model text",
    next_step: { code: "review_numeric_gate", stage: "cfd_run", explanation: "untrusted explanation" },
  })) });
  h.ui.setCase("case-a");
  assert.equal(h.calls.length, 0);
  await h.nodes["harness-advice-generate"].listeners.click();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0][0], "/api/cases/case-a/harness-advice");
  assert.equal(h.calls[0][1].method, "POST");
  assert.equal(h.calls[0][1].body, undefined);
  assert.match(h.nodes["harness-advice-next"].textContent, /CFD 数值闸门/);
  assert.match(h.nodes["harness-advice-recap"].textContent, /数值检查未通过/);
  assert.match(h.nodes["harness-advice-draft"].textContent, /人工复核/);
  assert.equal(h.nodes["harness-advice-result"].hidden, false);
  assert.match(h.nodes["harness-advice-metering"].textContent, /合计 20 token/);
  assert.match(h.nodes["harness-advice-metering"].textContent, /费用待账单核对/);
  assert.equal(h.nodes["harness-advice-generate"].disabled, false);
  assert.doesNotMatch(Object.values(h.nodes).map(node => node.textContent).join(" "), /untrusted|secret text/);
});

test("partial metering is shown as unknown instead of zero", async () => {
  const h = harness({ fetchImpl: async () => reply(200, advice("case-a", {
    metering: { source: "deepseek-harness-sdk-events", status: "partial",
      input_tokens: null, output_tokens: null, total_tokens: null, cost_usd: null },
  })) });
  h.ui.setCase("case-a");
  await h.nodes["harness-advice-generate"].listeners.click();
  assert.match(h.nodes["harness-advice-metering"].textContent, /不能按 0 计算/);
  h.ui.setCase("case-b");
  assert.equal(h.nodes["harness-advice-metering"].textContent, "");
});

test("disabled service and invalid contract show explicit error, never a result", async () => {
  const h = harness({ fetchImpl: async () => reply(503, { detail: "secret" }) });
  h.ui.setCase("case-a");
  await h.nodes["harness-advice-generate"].listeners.click();
  assert.match(h.nodes["harness-advice-status"].textContent, /服务未启用/);
  assert.equal(h.nodes["harness-advice-result"].hidden, true);
  const bad = harness({ fetchImpl: async () => reply(200, advice("case-a", { next_step: { code: "free_form", stage: "report" } })) });
  bad.ui.setCase("case-a");
  await bad.nodes["harness-advice-generate"].listeners.click();
  assert.match(bad.nodes["harness-advice-status"].textContent, /不符合只读契约/);
  assert.equal(bad.nodes["harness-advice-result"].hidden, true);
});

test("incomplete response and wrong case are both rejected", async () => {
  const minimal = harness({ fetchImpl: async () => reply(200, advice("case-a", {
    case_id: undefined, human_review_required: undefined, automated_action: undefined,
  })) });
  minimal.ui.setCase("case-a");
  await minimal.nodes["harness-advice-generate"].listeners.click();
  assert.equal(minimal.nodes["harness-advice-result"].hidden, true);
  const mismatch = harness({ fetchImpl: async () => reply(200, advice("case-b")) });
  mismatch.ui.setCase("case-a");
  await mismatch.nodes["harness-advice-generate"].listeners.click();
  assert.equal(mismatch.nodes["harness-advice-result"].hidden, true);
});

test("pending request blocks repeat click and case switch discards stale result", async () => {
  let resolve;
  const waiting = new Promise(done => { resolve = done; });
  const h = harness({ fetchImpl: async () => waiting });
  h.ui.setCase("case-a");
  const first = h.nodes["harness-advice-generate"].listeners.click();
  await h.nodes["harness-advice-generate"].listeners.click();
  assert.equal(h.calls.length, 1);
  assert.equal(h.nodes["harness-advice-generate"].attributes["aria-busy"], "true");
  h.ui.setCase("case-b");
  assert.equal(h.nodes["harness-advice-result"].hidden, true);
  resolve(reply(200, advice("case-a")));
  await first;
  assert.equal(h.nodes["harness-advice-result"].hidden, true);
  assert.equal(h.nodes["harness-advice-generate"].disabled, false);
  assert.match(h.nodes["harness-advice-status"].textContent, /当前病例/);
  h.ui.setCase(null);
  assert.equal(h.nodes["harness-advice-generate"].disabled, true);
});
