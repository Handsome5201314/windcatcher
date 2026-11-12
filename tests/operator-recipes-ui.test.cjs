"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static", "operator-recipes.js"), "utf8");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "static", "app.css"), "utf8");
const recipeId = "recipe-" + "1".repeat(32);
const ownerId = "a".repeat(32);

function spec() {
  return { suggested_steps: ["qc", "endplanes"], allowed_tools: ["service_status"],
    trigger_condition: "manual" };
}

function row(version = 1, revoked = false, draft = spec()) {
  return { recipe_id: recipeId, owner_id: ownerId, version, schema_version: 1,
    spec: draft, revoked };
}

function element(id = "") {
  const classes = new Set();
  return {
    id, value: "", checked: false, disabled: false, hidden: id === "operator-recipes-panel",
    textContent: "", children: [], listeners: {},
    classList: { toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
      contains(name) { return classes.has(name); } },
    addEventListener(type, listener) { this.listeners[type] = listener; },
    setAttribute(name, value) { this[name] = value; },
    replaceChildren(...children) { this.children = children; },
    append(...children) { this.children.push(...children); },
    focus() { this.focused = true; },
  };
}

function response(status, data) {
  return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(data) };
}

function harness({ multiuser = true, hash = "#work", replies = [] } = {}) {
  const ids = [...new Set([...index.matchAll(/id="([^"]+)"/g)].map(match => match[1]))];
  const nodes = Object.fromEntries(ids.map(id => [id, element(id)]));
  const calls = [];
  const listeners = {};
  const window = { WindcatcherMultiuser: multiuser, location: { hash },
    addEventListener(type, listener) { listeners[type] = listener; } };
  const document = { getElementById: id => nodes[id], createElement: () => element() };
  const fetch = async (url, options) => {
    calls.push({ url, options });
    const result = replies.shift();
    if (!result) throw new Error("unexpected request");
    return result;
  };
  vm.runInNewContext(source, { window, document, fetch });
  return { nodes, calls, listeners, ui: window.WindcatcherOperatorRecipes, window };
}

function findButton(node, label) {
  if (node.type === "button" && node.textContent === label) return node;
  for (const child of node.children || []) {
    const match = findButton(child, label);
    if (match) return match;
  }
  return null;
}

test("personal recipe panel is hidden outside multiuser mode and fits existing environment page", async () => {
  assert.match(index, /id="operator-recipes-panel"[^>]*hidden/);
  assert.match(index, /static\/operator-recipes\.js/);
  assert.match(index, /id="page-env"[\s\S]*id="operator-recipes-panel"/);
  assert.match(index, /id="operator-recipes-status"[^>]*aria-live="polite"/);
  assert.match(index, /id="operator-recipe-candidates-title"/);
  assert.match(index, /id="operator-recipe-candidates-status"[^>]*aria-live="polite"/);
  assert.match(css, /@media \(max-width: 700px\).*recipe-row-actions/);
  const local = harness({ multiuser: false });
  await local.ui.activate();
  assert.equal(local.nodes["operator-recipes-panel"].hidden, true);
  assert.equal(local.calls.length, 0);
});

test("candidate analysis is opt-in and only prefills steps before explicit create", async () => {
  const suggested_steps = ["segment", "qc"];
  const saved = { suggested_steps, allowed_tools: ["service_status"], trigger_condition: "manual" };
  const h = harness({ replies: [
    response(200, { recipes: [] }),
    response(200, { candidates: [{ suggested_steps, case_count: 3 }] }),
    response(201, row(1, false, saved)),
  ] });
  await h.ui.activate();
  assert.equal(h.calls.length, 1);
  assert.equal(h.nodes["operator-recipe-candidates-refresh"].disabled, false);
  await h.nodes["operator-recipe-candidates-refresh"].listeners.click();
  assert.equal(h.calls[1].url, "/api/operator/recipe-candidates");
  assert.equal(h.calls[1].options.method, "GET");
  assert.match(h.nodes["operator-recipe-candidates-status"].textContent, /使用习惯线索/);
  assert.ok(findButton(h.nodes["operator-recipe-candidates-list"], "填入草案"));
  findButton(h.nodes["operator-recipe-candidates-list"], "填入草案").listeners.click();
  assert.equal(h.calls.length, 2);
  assert.equal(h.nodes["recipe-step-segment"].checked, true);
  assert.equal(h.nodes["recipe-step-qc"].checked, true);
  assert.equal(h.nodes["recipe-tool-service_status"].checked, false);
  assert.equal(h.nodes["recipe-tool-service_status"].focused, true);
  await h.nodes["operator-recipes-form"].listeners.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 2);
  h.nodes["recipe-tool-service_status"].checked = true;
  await h.nodes["operator-recipes-form"].listeners.submit({ preventDefault() {} });
  assert.equal(h.calls[2].url, "/api/operator/recipes");
  assert.deepEqual(JSON.parse(h.calls[2].options.body), { spec: saved });
});

test("no repeated cross-case sequence leaves the candidate list empty", async () => {
  const h = harness({ replies: [response(200, { recipes: [] }), response(200, { candidates: [] })] });
  await h.ui.activate();
  await h.nodes["operator-recipe-candidates-refresh"].listeners.click();
  assert.match(h.nodes["operator-recipe-candidates-status"].textContent, /还没有跨至少 3 个病例/);
  assert.equal(h.nodes["operator-recipe-candidates-list"].children.length, 0);
  assert.equal(h.calls.length, 2);
});

test("candidate loading disables repeat requests and a network error can be retried", async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const h = harness({ replies: [response(200, { recipes: [] }), pending,
    response(200, { candidates: [{ suggested_steps: ["qc", "segment"], case_count: 4 }] })] });
  await h.ui.activate();
  const loading = h.nodes["operator-recipe-candidates-refresh"].listeners.click();
  assert.equal(h.nodes["operator-recipe-candidates-refresh"].disabled, true);
  assert.match(h.nodes["operator-recipe-candidates-status"].textContent, /正在分析/);
  release(response(503, { detail: { reason: "服务暂时不可用" } }));
  assert.equal(await loading, false);
  assert.equal(h.nodes["operator-recipe-candidates-status"].role, "alert");
  assert.equal(h.nodes["operator-recipe-candidates-refresh"].disabled, false);
  assert.equal(await h.nodes["operator-recipe-candidates-refresh"].listeners.click(), true);
  assert.equal(h.nodes["operator-recipe-candidates-status"].role, "status");
  assert.ok(findButton(h.nodes["operator-recipe-candidates-list"], "填入草案"));
  assert.equal(h.calls.length, 3);
});

test("reloading private drafts clears candidate evidence from the previous session", async () => {
  const h = harness({ replies: [response(200, { recipes: [] }),
    response(200, { candidates: [{ suggested_steps: ["qc", "segment"], case_count: 3 }] }),
    response(200, { recipes: [] })] });
  await h.ui.activate();
  await h.nodes["operator-recipe-candidates-refresh"].listeners.click();
  assert.ok(findButton(h.nodes["operator-recipe-candidates-list"], "填入草案"));
  await h.nodes["operator-recipes-refresh"].listeners.click();
  assert.equal(h.nodes["operator-recipe-candidates-list"].children.length, 0);
  assert.match(h.nodes["operator-recipe-candidates-status"].textContent, /点击.*分析/);
  assert.equal(h.calls.length, 3);
});

test("candidate response cannot inject case data and an expired session clears private drafts", async () => {
  const h = harness({ replies: [
    response(200, { recipes: [row()] }),
    response(200, { candidates: [{ suggested_steps: ["qc", "segment"], case_count: 3,
      case_id: "case-20261102-000000-0001" }] }),
    response(401, { detail: { reason: "请先登录" } }),
  ] });
  await h.ui.activate();
  assert.equal(await h.nodes["operator-recipe-candidates-refresh"].listeners.click(), false);
  assert.equal(h.nodes["operator-recipe-candidates-list"].children.length, 0);
  assert.match(h.nodes["operator-recipe-candidates-status"].textContent, /响应格式异常/);
  assert.equal(h.nodes["operator-recipe-candidates-status"].role, "alert");
  assert.ok(findButton(h.nodes["operator-recipes-list"], "编辑草案"));
  assert.equal(await h.nodes["operator-recipe-candidates-refresh"].listeners.click(), false);
  assert.equal(findButton(h.nodes["operator-recipes-list"], "编辑草案"), null);
  assert.equal(h.nodes["operator-recipes-submit"].disabled, true);
  assert.equal(h.nodes["operator-recipe-candidates-refresh"].disabled, true);
  assert.equal(h.calls.length, 3);
});

test("loading and empty state, then explicit create sends only enum draft", async () => {
  const h = harness({ replies: [response(200, { recipes: [] }), response(201, row())] });
  assert.equal(h.nodes["operator-recipes-submit"].disabled, true);
  await h.ui.activate();
  assert.equal(h.nodes["operator-recipes-panel"].hidden, false);
  assert.match(h.nodes["operator-recipes-status"].textContent, /没有配方草案/);
  assert.match(h.nodes["operator-recipes-list"].children[0].textContent, /还没有配方草案/);
  assert.equal(h.nodes["operator-recipes-submit"].disabled, false);

  h.nodes["recipe-step-qc"].checked = true;
  h.nodes["recipe-step-endplanes"].checked = true;
  h.nodes["recipe-tool-service_status"].checked = true;
  await h.nodes["operator-recipes-form"].listeners.submit({ preventDefault() {} });
  assert.equal(h.calls[1].url, "/api/operator/recipes");
  assert.equal(h.calls[1].options.method, "POST");
  assert.equal(h.calls[1].options.credentials, "same-origin");
  assert.deepEqual(JSON.parse(h.calls[1].options.body), { spec: spec() });
  assert.match(h.nodes["operator-recipes-status"].textContent, /已创建/);
  assert.ok(findButton(h.nodes["operator-recipes-list"], "编辑草案"));
  assert.equal(h.nodes["recipe-step-qc"].checked, false);
});

test("loading disables edits until the list arrives", async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const h = harness({ replies: [pending] });
  const loading = h.ui.activate();
  assert.match(h.nodes["operator-recipes-status"].textContent, /正在读取/);
  assert.equal(h.nodes["operator-recipes-submit"].disabled, true);
  release(response(200, { recipes: [] }));
  await loading;
  assert.equal(h.nodes["operator-recipes-submit"].disabled, false);
});

test("editing uses expected version and offers explicit cancel", async () => {
  const changed = { suggested_steps: ["review"], allowed_tools: ["get_cfd_batch"],
    trigger_condition: "batch_finished" };
  const h = harness({ replies: [response(200, { recipes: [row()] }), response(200, row(2, false, changed))] });
  await h.ui.activate();
  findButton(h.nodes["operator-recipes-list"], "编辑草案").listeners.click();
  assert.equal(h.nodes["recipe-step-qc"].checked, true);
  assert.equal(h.nodes["operator-recipes-cancel"].hidden, false);
  assert.equal(h.nodes["recipe-step-qc"].focused, true);
  h.nodes["recipe-step-qc"].checked = false;
  h.nodes["recipe-step-endplanes"].checked = false;
  h.nodes["recipe-step-review"].checked = true;
  h.nodes["recipe-tool-service_status"].checked = false;
  h.nodes["recipe-tool-get_cfd_batch"].checked = true;
  h.nodes["operator-recipes-trigger"].value = "batch_finished";
  await h.nodes["operator-recipes-form"].listeners.submit({ preventDefault() {} });
  assert.equal(h.calls[1].url, "/api/operator/recipes/" + recipeId);
  assert.equal(h.calls[1].options.method, "PUT");
  assert.deepEqual(JSON.parse(h.calls[1].options.body), { expected_version: 1, spec: changed });
  assert.match(h.nodes["operator-recipes-status"].textContent, /版本 2/);
  assert.equal(h.nodes["operator-recipes-cancel"].hidden, true);
});

test("editing an existing draft preserves its previously chosen step order", async () => {
  const reversed = { ...spec(), suggested_steps: ["endplanes", "qc"] };
  const h = harness({ replies: [response(200, { recipes: [row(1, false, reversed)] }),
    response(200, row(2, false, reversed))] });
  await h.ui.activate();
  findButton(h.nodes["operator-recipes-list"], "编辑草案").listeners.click();
  await h.nodes["operator-recipes-form"].listeners.submit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(h.calls[1].options.body).spec.suggested_steps, ["endplanes", "qc"]);
});

test("revoke requires a second explicit click and sends current version", async () => {
  const h = harness({ replies: [response(200, { recipes: [row()] }), response(200, row(2, true))] });
  await h.ui.activate();
  findButton(h.nodes["operator-recipes-list"], "撤销草案").listeners.click();
  assert.equal(h.calls.length, 1);
  assert.ok(findButton(h.nodes["operator-recipes-list"], "确认撤销"));
  await findButton(h.nodes["operator-recipes-list"], "确认撤销").listeners.click();
  assert.equal(h.calls[1].url, "/api/operator/recipes/" + recipeId + "/revoke");
  assert.equal(h.calls[1].options.method, "POST");
  assert.deepEqual(JSON.parse(h.calls[1].options.body), { expected_version: 1 });
  assert.match(h.nodes["operator-recipes-status"].textContent, /已撤销/);
  assert.equal(findButton(h.nodes["operator-recipes-list"], "编辑草案"), null);
});

test("409 conflict preserves visible draft and blocks writes until reload", async () => {
  const h = harness({ replies: [response(200, { recipes: [row()] }),
    response(409, { detail: { reason: "stale" } }), response(200, { recipes: [row(2)] })] });
  await h.ui.activate();
  findButton(h.nodes["operator-recipes-list"], "编辑草案").listeners.click();
  h.nodes["recipe-step-review"].checked = true;
  await h.nodes["operator-recipes-form"].listeners.submit({ preventDefault() {} });
  assert.equal(h.nodes["recipe-step-review"].checked, true);
  assert.equal(h.nodes["operator-recipes-submit"].disabled, true);
  assert.match(h.nodes["operator-recipes-status"].textContent, /版本冲突/);
  await h.nodes["operator-recipes-refresh"].listeners.click();
  assert.equal(h.nodes["recipe-step-review"].checked, false);
  assert.equal(h.nodes["operator-recipes-submit"].disabled, false);
});

test("invalid choices and load errors never send a write", async () => {
  const h = harness({ replies: [response(200, { recipes: [] })] });
  await h.ui.activate();
  await h.nodes["operator-recipes-form"].listeners.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 1);
  assert.equal(h.nodes["recipe-step-qc"].focused, true);
  h.nodes["recipe-step-qc"].checked = true;
  await h.nodes["operator-recipes-form"].listeners.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 1);
  assert.equal(h.nodes["recipe-tool-service_status"].focused, true);

  const failed = harness({ replies: [response(500, { detail: { reason: "offline" } })] });
  assert.equal(await failed.ui.activate(), false);
  assert.equal(failed.nodes["operator-recipes-submit"].disabled, true);
  assert.match(failed.nodes["operator-recipes-status"].textContent, /读取失败/);
});

test("expired session clears the previously loaded private recipe list", async () => {
  const h = harness({ replies: [response(200, { recipes: [row()] }),
    response(401, { detail: { reason: "请先登录" } })] });
  await h.ui.activate();
  assert.ok(findButton(h.nodes["operator-recipes-list"], "编辑草案"));
  await h.nodes["operator-recipes-refresh"].listeners.click();
  assert.equal(findButton(h.nodes["operator-recipes-list"], "编辑草案"), null);
  assert.equal(h.nodes["operator-recipes-submit"].disabled, true);
  assert.match(h.nodes["operator-recipes-status"].textContent, /读取失败/);
});
