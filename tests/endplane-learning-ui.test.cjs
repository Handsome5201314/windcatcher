"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static", "endplane-learning.js"), "utf8");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");
const app = fs.readFileSync(path.join(root, "static", "app.js"), "utf8");

function element() {
  const classes = new Set();
  return { hidden: false, disabled: false, checked: false, value: "", textContent: "", children: [],
    attributes: {}, listeners: {}, classList: { toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute(name, value) { this.attributes[name] = value; },
    appendChild(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
  };
}
function reply(status, body) { return { ok: status >= 200 && status < 300, status, json: async () => body }; }
function plan(version = 3, fmt = "oblique_planes_v1") {
  return { scope: "main_airway", mask_version: version, format: fmt,
    anterior_plane: { point_lps_mm: [1, 2, 3], normal_lps: [0, -1, 0] },
    posterior_plane: { point_lps_mm: [1, 30, 3], normal_lps: [0, 1, 0] },
    ...(fmt === "oblique_local_inlet_v2" ? { anterior_seed_lps_mm: [1, 1, 3] } : {}) };
}
function bilateralPlan() {
  const plane = y => ({ point_lps_mm: [0, y, 0], normal_lps: [0, -1, 0] });
  return { scope: "main_airway", mask_version: 3, format: "oblique_bilateral_local_v3",
    left_inlet_plane: plane(1), right_inlet_plane: plane(2), posterior_plane: plane(30),
    left_inlet_seed_lps_mm: [-5, 0, 0], right_inlet_seed_lps_mm: [5, 0, 0],
    posterior_seed_lps_mm: [0, 31, 0] };
}
function proposal(overrides = {}) {
  return { proposal_id: "proposal-" + "a".repeat(32), case_id: "case-a", scope: "main_airway",
    mask_version: 3, plan: plan(), source: "explicit-model", source_version: "v1",
    status: "proposed", feedback: [], ...overrides };
}
function catalog(items = [proposal()], caseId = "case-a") {
  return { case_id: caseId, scope: "main_airway", proposals: items };
}
function registered(item = proposal({ source: "operator-manual", source_version: "ui-v1" })) {
  return { status: "available", proposal_id: item.proposal_id, proposal: item,
    human_review_required: true, committed: false };
}
function reviewed(status, overrides = {}) { return proposal({ status, previous_status: "proposed", ...overrides }); }
function tick() { return new Promise(resolve => setImmediate(resolve)); }
function harness(fetchImpl = async () => reply(200, catalog())) {
  const ids = ["panel", "status", "refresh", "select", "details", "load", "auto", "register", "confirm", "accept", "adjust", "reject"];
  const nodes = Object.fromEntries(ids.map(id => ["endplane-learning-" + id, element()]));
  const calls = [], applied = [];
  let draft = { plan: plan(), error: null };
  const window = {};
  const context = vm.createContext({ window, document: { getElementById: id => nodes[id], createElement: element },
    fetch: (...args) => { calls.push(args); return fetchImpl(...args); }, AbortController, Number, Object });
  vm.runInContext(source, context, { filename: "static/endplane-learning.js" });
  const config = { caseId: "case-a", scope: "main_airway", maskVersion: 3, isOpen: true, canEdit: true,
    getDraft: () => draft,
    applyDraft: value => { applied.push(value); draft = { plan: value, error: null }; },
  };
  return { nodes, calls, applied, ui: window.WindcatcherEndplaneLearning, config,
    setDraft(value) { draft = value; },
    setContext(changes = {}) { Object.assign(config, changes); window.WindcatcherEndplaneLearning.setContext(config); },
    click(id) { return nodes["endplane-learning-" + id].listeners.click(); },
    check() { nodes["endplane-learning-confirm"].checked = true; nodes["endplane-learning-confirm"].listeners.change(); },
  };
}

test("learning UI is inside endplane panel and binds to existing editor", () => {
  assert.match(index, /id="endplane-panel"[\s\S]*id="endplane-learning-panel"[\s\S]*id="viewer-canvas"/);
  assert.match(index, /id="endplane-learning-status"[^>]*aria-live="polite"/);
  assert.ok(index.indexOf("/static/endplane-learning.js") < index.indexOf("/static/app.js"));
  assert.match(app, /if \(window\.WindcatcherEndplaneLearning\)\s*\{[\s\S]*WindcatcherEndplaneLearning\.setContext\(/);
  assert.match(app, /function applyEndplaneLearningDraft\(/);
  assert.match(index, /id="endplane-learning-register"[^>]*type="button"[^>]*disabled>登记当前草稿/);
  assert.match(index, /id="endplane-learning-auto"[^>]*type="button"[^>]*disabled>本机生成候选/);
});

test("local generation registers multiple candidates for manual review without applying a draft", async () => {
  const first = proposal({ proposal_id: "proposal-" + "c".repeat(32), source: "local-geometry",
    plan: plan(3, "oblique_local_inlet_v2") });
  const second = proposal({ proposal_id: "proposal-" + "d".repeat(32), source: "local-geometry",
    plan: plan(3, "oblique_local_inlet_v2") });
  let generated = false;
  const h = harness(async (url, options) => {
    if (url.endsWith("/auto-propose")) { generated = true; return reply(200, { ...registered(first), proposals: [first, second] }); }
    return reply(200, catalog(generated ? [first, second] : []));
  });
  h.setContext(); await tick();
  assert.equal(h.nodes["endplane-learning-auto"].disabled, false);
  await h.click("auto");
  const posts = h.calls.filter(([, options]) => options?.method === "POST");
  assert.equal(posts.length, 1);
  assert.equal(posts[0][0], "/api/cases/case-a/segment/endplanes/learning/auto-propose");
  assert.deepEqual(JSON.parse(posts[0][1].body), { scope: "main_airway", mask_version: 3 });
  assert.equal(h.nodes["endplane-learning-select"].children.length, 2);
  assert.equal(h.nodes["endplane-learning-select"].value, first.proposal_id);
  assert.match(h.nodes["endplane-learning-status"].textContent, /2 条单支局部入口候选.*不能用于双侧鼻呼吸 CFD/);
  assert.match(h.nodes["endplane-learning-details"].textContent, /单支局部入口候选仅供几何预览.*不能用于双侧鼻呼吸 CFD/);
  assert.match(h.nodes["endplane-learning-auto"].title, /不能直接用于双侧鼻呼吸 CFD/);
  assert.equal(h.applied.length, 0);
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  assert.equal(h.nodes["endplane-learning-accept"].disabled, true);
});

test("local generation unavailable preserves existing candidates and never applies a draft", async () => {
  const h = harness(async (url) => url.endsWith("/auto-propose")
    ? reply(200, { status: "unavailable", reason: "geometry_or_preflight_unavailable", proposal_id: null })
    : reply(200, catalog()));
  h.setContext(); await tick();
  await h.click("auto");
  assert.match(h.nodes["endplane-learning-status"].textContent, /未生成候选.*未通过局部入口技术预检/);
  assert.equal(h.nodes["endplane-learning-select"].children.length, 1);
  assert.equal(h.applied.length, 0);
  assert.equal(h.calls.length, 2);
  assert.equal(h.nodes["endplane-learning-auto"].disabled, false);
});

test("posterior outlet ambiguity explains why no automatic plane was saved", async () => {
  const h = harness(async (url) => url.endsWith("/auto-propose")
    ? reply(200, { status: "unavailable", reason: "posterior_single_section_missing", candidates: [] })
    : reply(200, catalog([])));
  h.setContext(); await tick();
  await h.click("auto");
  assert.match(h.nodes["endplane-learning-status"].textContent, /未找到单一.*鼻咽出口截面.*核对覆盖范围与掩膜连通性/);
  assert.equal(h.applied.length, 0);
  assert.equal(h.nodes["endplane-learning-accept"].disabled, true);
});

test("local generation requires edit access and a single in-flight request", async () => {
  let resolve;
  const h = harness(async (url) => url.endsWith("/auto-propose")
    ? new Promise(done => { resolve = done; }) : reply(200, catalog([])));
  h.setContext({ canEdit: false }); await tick();
  await h.click("auto");
  assert.equal(h.calls.length, 1);
  h.setContext({ canEdit: true });
  const pending = h.click("auto");
  await h.click("auto");
  assert.equal(h.calls.filter(([, options]) => options?.method === "POST").length, 1);
  assert.equal(h.nodes["endplane-learning-auto"].textContent, "生成中…");
  resolve(reply(200, { status: "unavailable", reason: "no_reliable_candidate" }));
  await pending;
  assert.equal(h.nodes["endplane-learning-auto"].disabled, false);
});

test("local generation reports missing refreshed proposals instead of claiming success", async () => {
  const first = proposal({ proposal_id: "proposal-" + "c".repeat(32) });
  const second = proposal({ proposal_id: "proposal-" + "d".repeat(32) });
  const h = harness(async (url) => url.endsWith("/auto-propose")
    ? reply(200, { ...registered(first), proposals: [first, second] })
    : reply(200, catalog([first])));
  h.setContext(); await tick(); await h.click("auto");
  assert.match(h.nodes["endplane-learning-status"].textContent, /未找到全部记录.*刷新核对/);
  assert.doesNotMatch(h.nodes["endplane-learning-status"].textContent, /已返回 2 条候选记录/);
  assert.equal(h.applied.length, 0);
});

test("late local generation cannot alter a different case, scope or mask version", async () => {
  for (const change of [{ caseId: "case-b" }, { scope: "full_candidate" }, { maskVersion: 4 }]) {
    let resolve;
    const h = harness(async (url) => {
      if (url.endsWith("/auto-propose")) return new Promise(done => { resolve = done; });
      return reply(200, { case_id: url.includes("case-b/") ? "case-b" : "case-a",
        scope: url.includes("scope=full_candidate") ? "full_candidate" : "main_airway", proposals: [] });
    });
    h.setContext(); await tick();
    const pending = h.click("auto");
    h.setContext(change); await tick();
    const before = h.nodes["endplane-learning-status"].textContent, count = h.calls.length;
    resolve(reply(200, { ...registered(), proposals: [proposal()] })); await pending;
    assert.equal(h.calls.length, count);
    assert.equal(h.nodes["endplane-learning-status"].textContent, before);
    assert.equal(h.nodes["endplane-learning-select"].children.length, 0);
    assert.equal(h.applied.length, 0);
  }
});

test("invalid local candidate response and failed request remain visible, not accepted", async () => {
  for (const result of [reply(403, { detail: { reason: "forbidden" } }),
    reply(200, { ...registered(proposal({ case_id: "case-b" })), proposals: [proposal({ case_id: "case-b" })] }),
    reply(200, { ...registered(), proposals: [proposal()], committed: true })]) {
    const h = harness(async (url) => url.endsWith("/auto-propose") ? result : reply(200, catalog([])));
    h.setContext(); await tick(); await h.click("auto");
    assert.match(h.nodes["endplane-learning-status"].textContent, /本机候选未确认/);
    assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
    assert.equal(h.nodes["endplane-learning-accept"].disabled, true);
    assert.equal(h.applied.length, 0);
  }
});

test("empty catalog can register a complete current draft and select it without loading, feedback or formal save", async () => {
  const item = proposal({ proposal_id: "proposal-" + "b".repeat(32), source: "operator-manual", source_version: "ui-v1",
    plan: plan(3, "oblique_local_inlet_v2") });
  let created = false;
  const h = harness(async (_url, options) => {
    if (options?.method === "POST") { created = true; return reply(200, registered(item)); }
    return reply(200, catalog(created ? [proposal(), item] : []));
  });
  h.setDraft({ plan: item.plan, error: null }); h.setContext(); await tick();
  assert.match(h.nodes["endplane-learning-status"].textContent, /手动选好两端.*登记当前草稿.*积累人工示例/);
  assert.equal(h.nodes["endplane-learning-register"].disabled, false);
  await h.click("register");
  const posts = h.calls.filter(([, options]) => options?.method === "POST");
  assert.equal(posts.length, 1);
  assert.equal(posts[0][0], "/api/cases/case-a/segment/endplanes/learning/propose");
  assert.deepEqual(JSON.parse(posts[0][1].body), { scope: "main_airway", mask_version: 3,
    plan: item.plan, source: "operator-manual", source_version: "ui-v1" });
  assert.equal(h.nodes["endplane-learning-select"].value, item.proposal_id);
  assert.equal(h.applied.length, 0);
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  h.check(); await h.click("accept");
  assert.equal(h.nodes["endplane-learning-accept"].disabled, true);
  assert.equal(h.calls.filter(([, options]) => options?.method === "POST").length, 1);
  assert.match(h.nodes["endplane-learning-status"].textContent, /先加载/);
});

test("draft registration requires editor permission, matching complete geometry and no validation error", async () => {
  const h = harness(); h.setContext(); await tick();
  const invalid = [
    { plan: plan(), error: "当前端面预览无效" },
    { plan: { ...plan(), posterior_plane: null }, error: null },
    { plan: plan(4), error: null },
    { plan: { ...plan(3, "oblique_local_inlet_v2"), anterior_seed_lps_mm: null }, error: null },
  ];
  for (const value of invalid) {
    h.setDraft(value); h.setContext();
    assert.equal(h.nodes["endplane-learning-register"].disabled, true);
    await h.click("register");
  }
  h.setDraft({ plan: plan(), error: null }); h.setContext({ canEdit: false });
  assert.equal(h.nodes["endplane-learning-register"].disabled, true);
  await h.click("register");
  assert.equal(h.calls.length, 1);
});

test("three-opening draft registers only as a candidate with all three seeds", async () => {
  const item = proposal({ proposal_id: "proposal-" + "e".repeat(32),
    source: "operator-manual", source_version: "ui-v1", plan: bilateralPlan() });
  let created = false;
  const h = harness(async (_url, options) => {
    if (options?.method === "POST") { created = true; return reply(200, registered(item)); }
    return reply(200, catalog(created ? [item] : []));
  });
  h.setDraft({ plan: item.plan, error: null }); h.setContext(); await tick();
  assert.equal(h.nodes["endplane-learning-register"].disabled, false);
  await h.click("register");
  const post = h.calls.find(([, options]) => options?.method === "POST");
  assert.deepEqual(JSON.parse(post[1].body).plan, item.plan);
  assert.equal(h.applied.length, 0);
  assert.match(h.nodes["endplane-learning-details"].textContent, /三端局部候选需人工核对/);
  h.setDraft({ plan: { ...item.plan, posterior_seed_lps_mm: null }, error: null }); h.setContext();
  assert.equal(h.nodes["endplane-learning-register"].disabled, true);
});

test("duplicate registration clicks send one request and an existing candidate stays unreviewed", async () => {
  let resolve;
  const h = harness(async (_url, options) => options?.method === "POST"
    ? new Promise(done => { resolve = done; }) : reply(200, catalog()));
  h.setContext(); await tick(); await h.click("load"); h.check();
  const first = h.click("register"); await h.click("register");
  assert.equal(h.calls.filter(([, options]) => options?.method === "POST").length, 1);
  assert.equal(h.nodes["endplane-learning-register"].textContent, "登记中…");
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  resolve(reply(200, registered())); await first;
  assert.equal(h.nodes["endplane-learning-select"].children.length, 1);
  assert.equal(h.nodes["endplane-learning-accept"].disabled, true);
  assert.equal(h.nodes["endplane-learning-register"].disabled, false);
});

test("late registration cannot affect a different case, scope or mask version", async () => {
  for (const change of [{ caseId: "case-b" }, { scope: "full_candidate" }, { maskVersion: 4 }]) {
    let resolve;
    const h = harness(async (url, options) => {
      if (options?.method === "POST") return new Promise(done => { resolve = done; });
      return reply(200, { case_id: url.includes("case-b/") ? "case-b" : "case-a",
        scope: url.includes("scope=full_candidate") ? "full_candidate" : "main_airway", proposals: [] });
    });
    h.setContext(); await tick(); const pending = h.click("register");
    h.setContext(change); await tick();
    const before = h.nodes["endplane-learning-status"].textContent, count = h.calls.length;
    resolve(reply(200, registered())); await pending;
    assert.equal(h.calls.length, count);
    assert.equal(h.nodes["endplane-learning-status"].textContent, before);
    assert.equal(h.nodes["endplane-learning-select"].children.length, 0);
    assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  }
});

test("late registration cannot finish a newer request after switching away and back", async () => {
  const resolvers = [];
  const h = harness(async (url, options) => {
    if (options?.method === "POST") return new Promise(done => resolvers.push(done));
    const caseId = url.includes("case-b/") ? "case-b" : "case-a";
    return reply(200, catalog([proposal({ case_id: caseId })], caseId));
  });
  h.setContext(); await tick(); const first = h.click("register");
  h.setContext({ caseId: "case-b" }); await tick();
  h.setContext({ caseId: "case-a" }); await tick(); const second = h.click("register");
  resolvers[0](reply(200, registered())); await first;
  assert.equal(h.nodes["endplane-learning-panel"].attributes["aria-busy"], "true");
  assert.equal(h.nodes["endplane-learning-register"].disabled, true);
  resolvers[1](reply(200, registered())); await second;
  assert.equal(h.nodes["endplane-learning-panel"].attributes["aria-busy"], "false");
});

test("registration 401, 403, 409 and repeated failures stay visible, retryable and never report success", async () => {
  for (const status of [401, 403, 409]) {
    const h = harness(async (_url, options) => options?.method === "POST"
      ? reply(status, {}) : reply(200, catalog([])));
    h.setContext(); await tick();
    for (let attempt = 0; attempt < 2; attempt++) {
      await h.click("register");
      assert.match(h.nodes["endplane-learning-status"].textContent, new RegExp(`登记未确认.*HTTP ${status}`));
      assert.doesNotMatch(h.nodes["endplane-learning-status"].textContent, /当前草稿已登记/);
      assert.equal(h.nodes["endplane-learning-register"].disabled, false);
      assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
    }
    assert.equal(h.calls.length, 3);
    assert.equal(h.applied.length, 0);
  }
});

test("malformed registration and a missing refreshed candidate require record verification", async () => {
  for (const data of [null, registered(proposal({ case_id: "case-b" })), { ...registered(), committed: true }]) {
    const h = harness(async (_url, options) => options?.method === "POST" ? reply(200, data) : reply(200, catalog([])));
    h.setContext(); await tick(); await h.click("register");
    assert.match(h.nodes["endplane-learning-status"].textContent, /登记未确认.*不一致|登记未确认.*无效/);
    assert.equal(h.calls.length, 2);
  }
  const missing = harness(async (_url, options) => options?.method === "POST"
    ? reply(200, registered()) : reply(200, catalog([])));
  missing.setContext(); await tick(); await missing.click("register");
  assert.match(missing.nodes["endplane-learning-status"].textContent, /刷新后未找到.*刷新核对记录/);
  assert.equal(missing.nodes["endplane-learning-accept"].disabled, true);
});

test("opening reads once, repeated renders do not refetch, and manual refresh works", async () => {
  const h = harness();
  h.setContext(); await tick();
  h.setContext(); h.setContext();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0][0], "/api/cases/case-a/segment/endplanes/learning?scope=main_airway");
  assert.match(h.nodes["endplane-learning-details"].textContent, /explicit-model/);
  assert.match(h.nodes["endplane-learning-details"].textContent, /v1/);
  assert.match(h.nodes["endplane-learning-details"].textContent, /3/);
  await h.click("refresh");
  assert.equal(h.calls.length, 2);
});

test("late response from another case cannot appear or restore confirmation", async () => {
  let resolve;
  const waiting = new Promise(done => { resolve = done; });
  const h = harness(url => url.includes("case-a/") ? waiting : Promise.resolve(reply(200, catalog([], "case-b"))));
  h.setContext();
  h.setContext({ caseId: "case-b" }); await tick();
  resolve(reply(200, catalog())); await tick();
  assert.match(h.nodes["endplane-learning-status"].textContent, /暂无候选/);
  assert.equal(h.nodes["endplane-learning-select"].children.length, 0);
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
});

test("case, scope, mask and draft changes revoke human confirmation", async () => {
  const h = harness();
  h.setContext(); await tick(); await h.click("load"); h.check();
  assert.equal(h.nodes["endplane-learning-accept"].disabled, false);
  h.setDraft({ plan: { ...plan(), anterior_plane: { point_lps_mm: [2, 2, 3], normal_lps: [0, -1, 0] } }, error: null });
  h.setContext();
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  h.check(); h.setContext({ maskVersion: 4 });
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  await tick();
  assert.match(h.nodes["endplane-learning-details"].textContent, /陈旧|旧掩膜/);
  assert.equal(h.nodes["endplane-learning-load"].disabled, true);
  h.setContext({ scope: "full_candidate" });
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  h.setContext({ caseId: "case-b" });
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
});

test("unchecked confirmation sends no feedback even when click is invoked directly", async () => {
  const h = harness(); h.setContext(); await tick();
  for (const action of ["accept", "adjust", "reject"]) await h.click(action);
  assert.equal(h.calls.filter(([, options]) => options?.method === "POST").length, 0);
});

test("loading a candidate only fills draft and invokes no save or feedback request", async () => {
  const h = harness(); h.setContext(); await tick(); h.check();
  await h.click("load");
  assert.equal(h.applied.length, 1);
  assert.equal(JSON.stringify(h.applied[0]), JSON.stringify(plan()));
  assert.equal(h.calls.length, 1);
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  assert.match(h.nodes["endplane-learning-status"].textContent, /草稿/);
});

test("accepted feedback is explicit and records no formal endplane save", async () => {
  let recorded = false;
  const h = harness(async (_url, options) => {
    if (options?.method === "POST") { recorded = true; return reply(200, reviewed("accepted")); }
    return reply(200, catalog([recorded ? reviewed("accepted") : proposal()]));
  });
  h.setContext(); await tick(); await h.click("load"); h.check(); await h.click("accept");
  const posts = h.calls.filter(([, options]) => options?.method === "POST");
  assert.equal(posts.length, 1);
  assert.equal(posts[0][0], "/api/cases/case-a/segment/endplanes/learning/feedback");
  assert.equal(JSON.parse(posts[0][1].body).status, "accepted");
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  assert.match(h.nodes["endplane-learning-status"].textContent, /已记录“已接受”/);
  assert.equal(h.nodes["endplane-learning-reject"].disabled, true);
});

test("malformed or mismatched feedback success responses never confirm a recorded review", async () => {
  for (const data of [null, {}, { status: "rejected" }, reviewed("accepted"),
    reviewed("rejected", { proposal_id: "proposal-" + "b".repeat(32) }),
    reviewed("rejected", { case_id: "case-b" }), reviewed("rejected", { scope: "full_candidate" }),
    reviewed("rejected", { mask_version: 4 }), reviewed("rejected", { plan: null })]) {
    const h = harness(async (_url, options) => options?.method === "POST" ? reply(200, data) : reply(200, catalog()));
    h.setContext(); await tick(); h.check(); await h.click("reject");
    assert.match(h.nodes["endplane-learning-status"].textContent, /反馈未确认记录.*不一致|反馈未确认记录.*无效/);
    assert.doesNotMatch(h.nodes["endplane-learning-status"].textContent, /已记录“/);
    assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
    assert.equal(h.calls.length, 2);
  }
});

test("feedback is unconfirmed when the refreshed target is still proposed, missing or has a different terminal status", async () => {
  for (const items of [[proposal()], [], [reviewed("rejected")], [reviewed("accepted", { mask_version: 4, plan: plan(4) })]]) {
    let recorded = false;
    const h = harness(async (_url, options) => {
      if (options?.method === "POST") { recorded = true; return reply(200, reviewed("accepted")); }
      return reply(200, catalog(recorded ? items : [proposal()]));
    });
    h.setContext(); await tick(); await h.click("load"); h.check(); await h.click("accept");
    assert.match(h.nodes["endplane-learning-status"].textContent, /反馈未确认记录.*目标候选.*刷新核对/);
    assert.doesNotMatch(h.nodes["endplane-learning-status"].textContent, /已记录“/);
    assert.equal(h.calls.length, 3);
    assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  }
});

test("feedback remains unconfirmed when verification refresh fails", async () => {
  let recorded = false;
  const h = harness(async (_url, options) => {
    if (options?.method === "POST") { recorded = true; return reply(200, reviewed("rejected")); }
    return recorded ? reply(500, {}) : reply(200, catalog());
  });
  h.setContext(); await tick(); h.check(); await h.click("reject");
  assert.match(h.nodes["endplane-learning-status"].textContent, /反馈未确认记录.*HTTP 500.*刷新核对/);
  assert.doesNotMatch(h.nodes["endplane-learning-status"].textContent, /已记录“/);
});

test("accept requires the selected candidate to be loaded and unchanged", async () => {
  const h = harness(); h.setContext(); await tick(); h.check();
  await h.click("accept"); assert.equal(h.calls.length, 1);
  await h.click("load"); h.check();
  h.setDraft({ plan: { ...plan(), posterior_plane: { point_lps_mm: [2, 30, 3], normal_lps: [0, 1, 0] } }, error: null });
  h.setContext(); h.check(); await h.click("accept");
  assert.equal(h.calls.length, 1);
  assert.equal(h.nodes["endplane-learning-accept"].disabled, true);
});

test("late feedback cannot finish a newer request after switching away and back", async () => {
  const resolvers = [];
  let recorded = false;
  const h = harness(async (url, options) => {
    if (options?.method === "POST") return new Promise(done => resolvers.push(done));
    const caseId = url.includes("case-b/") ? "case-b" : "case-a";
    return reply(200, catalog([proposal({ case_id: caseId, status: recorded ? "accepted" : "proposed" })], caseId));
  });
  h.setContext(); await tick(); await h.click("load"); h.check();
  const first = h.click("accept");
  h.setContext({ caseId: "case-b" }); await tick();
  h.setContext({ caseId: "case-a" }); await tick();
  await h.click("load"); h.check(); const second = h.click("accept");
  assert.equal(resolvers.length, 2);
  resolvers[0](reply(200, reviewed("accepted"))); await first;
  assert.equal(h.nodes["endplane-learning-panel"].attributes["aria-busy"], "true");
  recorded = true;
  resolvers[1](reply(200, reviewed("accepted"))); await second;
  assert.equal(h.nodes["endplane-learning-panel"].attributes["aria-busy"], "false");
  assert.match(h.nodes["endplane-learning-status"].textContent, /已记录“已接受”/);
});

test("switching candidates revokes the prior candidate review", async () => {
  const h = harness(async () => reply(200, catalog([proposal(), proposal({ proposal_id: "proposal-" + "b".repeat(32) })])));
  h.setContext(); await tick(); await h.click("load"); h.check();
  h.nodes["endplane-learning-select"].value = "proposal-" + "b".repeat(32);
  h.nodes["endplane-learning-select"].listeners.change();
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
  assert.equal(h.nodes["endplane-learning-accept"].disabled, true);
});

test("adjusted feedback requires complete validated current draft", async () => {
  let recorded = false;
  const h = harness(async (_url, options) => {
    if (options?.method === "POST") { recorded = true; return reply(200, reviewed("adjusted")); }
    return reply(200, catalog([recorded ? reviewed("adjusted") : proposal()]));
  });
  h.setContext(); await tick();
  h.setDraft({ plan: plan(), error: "鼻孔端方向无效" }); h.setContext(); h.check();
  await h.click("adjust");
  assert.equal(h.calls.length, 1);
  h.setDraft({ plan: { ...plan(), posterior_plane: null }, error: null }); h.setContext(); h.check();
  await h.click("adjust"); assert.equal(h.calls.length, 1);
  h.setDraft({ plan: plan(3, "oblique_local_inlet_v2"), error: null }); h.setContext(); h.check();
  await h.click("adjust");
  const post = h.calls.find(([, options]) => options?.method === "POST");
  assert.equal(JSON.stringify(JSON.parse(post[1].body).feedback.adjusted_plan), JSON.stringify(plan(3, "oblique_local_inlet_v2")));
  assert.match(h.nodes["endplane-learning-status"].textContent, /已记录“已修正”/);
});

test("editor readiness cannot grant review permission and backend 403 is shown without success", async () => {
  const h = harness(async (_url, options) => options?.method === "POST"
    ? reply(403, { detail: { reason: "需要此病例的 review 权限" } }) : reply(200, catalog()));
  h.setContext({ canEdit: false }); await tick(); h.check();
  await h.click("reject"); assert.equal(h.calls.length, 1);
  assert.equal(h.nodes["endplane-learning-load"].disabled, true);
  h.setContext({ canEdit: true }); h.check(); await h.click("reject");
  assert.equal(h.calls.length, 2);
  assert.match(h.nodes["endplane-learning-status"].textContent, /HTTP 403.*review/);
  assert.doesNotMatch(h.nodes["endplane-learning-status"].textContent, /已记录/);
  assert.equal(h.nodes["endplane-learning-confirm"].checked, false);
});

test("HTTP errors, empty state and malformed contracts remain visible and non-actionable", async () => {
  for (const status of [403, 409, 500]) {
    const h = harness(async () => reply(status, { detail: { reason: "权限或状态变化" } }));
    h.setContext(); await tick();
    assert.match(h.nodes["endplane-learning-status"].textContent, new RegExp(String(status)));
    assert.equal(h.nodes["endplane-learning-load"].disabled, true);
  }
  const empty = harness(async () => reply(200, catalog([])));
  empty.setContext(); await tick();
  assert.match(empty.nodes["endplane-learning-status"].textContent, /暂无候选/);
  const invalid = harness(async () => reply(200, catalog([proposal({ case_id: "case-b" })])));
  invalid.setContext(); await tick();
  assert.match(invalid.nodes["endplane-learning-status"].textContent, /不一致|无效/);
  assert.equal(invalid.nodes["endplane-learning-confirm"].disabled, true);
});

function functionSource(name) {
  const start = app.match(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  assert.ok(start, `app function ${name} exists`);
  const end = app.indexOf("\n}", start.index);
  return app.slice(start.index, end + 2);
}

test("closed editor updates an installed learning module but has no learning dependencies when absent", () => {
  for (const installed of [false, true]) {
    let paused = 0, reads = 0;
    const updates = [], nodes = new Map();
    const state = { caseId: "case-a", case: { stages: { segment: { status: "ok" } } },
      endplanes: { open: false, status: "ready", profile: { can_save: true },
        viewer: { pause: () => paused++ } } };
    const context = vm.createContext({ state,
      window: installed ? { WindcatcherEndplaneLearning: { setContext: value => updates.push(value) } } : {},
      $: id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); },
      ...(installed ? { endplaneContext: () => { reads++; return { caseId: "case-a", scope: "main_airway", version: 3 }; },
        getEndplaneLearningDraft: () => {}, applyEndplaneLearningDraft: () => {} } : {}),
    });
    vm.runInContext(functionSource("renderEndplaneUi"), context);
    context.renderEndplaneUi();
    assert.equal(paused, 1);
    assert.equal(nodes.get("endplane-panel").hidden, true);
    assert.equal(reads, installed ? 1 : 0);
    assert.equal(updates.length, installed ? 1 : 0);
    if (installed) {
      assert.equal(updates[0].isOpen, false);
      assert.equal(updates[0].caseId, "case-a");
      assert.equal(updates[0].maskVersion, 3);
    }
  }
});

test("production apply callback asks before replacing dirty draft, updates preview, and preserves saved baseline", () => {
  let allow = false;
  const previews = [], viewerCalls = [];
  const baseline = { anterior: { ...plan().anterior_plane }, posterior: { ...plan().posterior_plane } };
  const state = { endplanes: { key: "case-a|main_airway|3", status: "ready", saving: false, dirty: true,
    profile: { can_save: true, supported_formats: ["oblique_planes_v1", "oblique_local_inlet_v2"] },
    planes: baseline, baselinePlanes: baseline, format: "oblique_planes_v1", anteriorSeed: null,
    viewer: { setFormat: value => viewerCalls.push(["format", value]), setPlanes: value => viewerCalls.push(["planes", value]),
      showSeedMarker: value => viewerCalls.push(["seed", value]) } } };
  const context = vm.createContext({ state, window: { confirm: () => allow },
    endplaneContext: () => ({ caseId: "case-a", scope: "main_airway", version: 3, key: "case-a|main_airway|3" }),
    clearEndplaneSection: () => {}, updateEndplaneDraft: value => previews.push(value),
  });
  vm.runInContext(["validLpsVector", "normalizedEndplane", "cloneEndplane", "currentEndplaneSeed", "applyEndplaneLearningDraft"].map(functionSource).join("\n"), context);
  const original = JSON.stringify(state.endplanes);
  assert.equal(context.applyEndplaneLearningDraft(plan(3, "oblique_local_inlet_v2")), false);
  assert.equal(JSON.stringify(state.endplanes), original);
  assert.equal(previews.length, 0);
  allow = true;
  assert.equal(context.applyEndplaneLearningDraft(plan(3, "oblique_local_inlet_v2")), true);
  assert.equal(state.endplanes.format, "oblique_local_inlet_v2");
  assert.equal(JSON.stringify(state.endplanes.anteriorSeed), "[1,1,3]");
  assert.equal(state.endplanes.baselinePlanes, baseline);
  assert.deepEqual(previews, [true]);
  assert.deepEqual(viewerCalls.map(call => call[0]), ["format", "planes", "seed"]);
  assert.throws(() => context.applyEndplaneLearningDraft(plan(4)), /不一致/);
});
