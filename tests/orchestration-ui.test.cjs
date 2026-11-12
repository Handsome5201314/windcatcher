"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static", "orchestration-ui.js"), "utf8");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");

function element() {
  const classes = new Set();
  return { children: [], listeners: {}, value: "", textContent: "", disabled: false,
    hidden: false, checked: false, classList: { toggle(name, value) {
      if (value) classes.add(name); else classes.delete(name);
    } }, addEventListener(name, fn) { this.listeners[name] = fn; },
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this[name] = value; },
  };
}

function reply(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function harness(fetchImpl, { stored = [], openCase = async () => true } = {}) {
  const ids = [...new Set([...index.matchAll(/id="([^"]+)"/g)].map(match => match[1]))];
  const nodes = Object.fromEntries(ids.map(id => [id, element()]));
  nodes["orchestration-use-harness"].checked = true;
  const calls = [];
  const storage = new Map(stored);
  const timers = new Map();
  let timerId = 0;
  const context = vm.createContext({ document: { getElementById: id => nodes[id], createElement: element },
    fetch: (...args) => { calls.push(args); return fetchImpl(...args); },
    sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    crypto: { randomUUID: () => "aaaa-bbbb" },
    AbortController,
    WindcatcherWorkbench: { openCase },
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: id => timers.delete(id),
    setInterval: () => ++timerId, clearInterval: () => {},
  });
  vm.runInContext(source, context, { filename: "static/orchestration-ui.js" });
  return { nodes, calls, storage, timers, ui: context.WindcatcherOrchestration,
    click: id => nodes["orchestration-" + id].listeners.click() };
}

function textTree(node) {
  return node.textContent + node.children.map(textTree).join(" ");
}

function findNode(node, predicate) {
  if (predicate(node)) return node;
  for (const child of node.children) {
    const found = findNode(child, predicate);
    if (found) return found;
  }
}

test("entering the agent page loads once and preserves selection when revisited", async () => {
  const caseId = "case-20261102-120001-aaaa";
  const h = harness(async () => reply(200, [{ case_id: caseId }]));
  await h.ui.activate();
  assert.equal(h.calls.length, 1);
  assert.equal(h.nodes["orchestration-start"].disabled, true);
  await h.click("select-all");
  await h.ui.activate();
  assert.equal(h.calls.length, 1, "route activation must not restart or resubmit work");
  assert.equal(h.nodes["orchestration-selected-count"].textContent, "已选 1 例");
  assert.equal(h.nodes["orchestration-start"].textContent, "开始处理 1 例");
  await h.click("clear");
  assert.equal(h.nodes["orchestration-start"].disabled, true);
  assert.equal(h.nodes["orchestration-selected-count"].textContent, "已选 0 例");
});

test("workbench starts only its current accessible case after the case list loads", async () => {
  const first = "case-20261102-120001-aaaa";
  const second = "case-20261102-120002-bbbb";
  const h = harness(async url => {
    if (url === "/api/cases") return reply(200, [{ case_id: second }, { case_id: first }]);
    if (url === "/api/orchestration/batches") return reply(202, { batch_id: "batch-current" });
    return reply(200, { batch_id: "batch-current", status: "completed", counts: { blocked: 1 }, items: [
      { input: { case_id: second }, status: "blocked", result: { reason: "局部修正三端口" } },
    ] });
  });
  const routeLoad = h.ui.activate();
  const launched = h.ui.startCurrentCase(second);
  await Promise.all([routeLoad, launched]);
  assert.equal(h.calls.filter(([url]) => url === "/api/cases").length, 1);
  const posts = h.calls.filter(([url]) => url === "/api/orchestration/batches");
  assert.equal(posts.length, 1);
  assert.deepEqual(JSON.parse(posts[0][1].body).case_ids, [second]);
  assert.equal(h.nodes["orchestration-selected-count"].textContent, "已选 1 例");
  assert.match(textTree(h.nodes["orchestration-results"]), /局部修正三端口/);
});

test("workbench does not submit inaccessible or already running cases", async () => {
  const caseId = "case-20261102-120001-aaaa";
  const other = "case-20261102-120002-bbbb";
  const denied = harness(async () => reply(200, [{ case_id: other }]));
  assert.equal(await denied.ui.startCurrentCase(caseId), false);
  assert.match(denied.nodes["orchestration-status"].textContent, /访问权限/);
  assert.equal(denied.calls.some(([, options]) => options?.method === "POST"), false);

  const running = harness(async url => url === "/api/cases" ? reply(200, [{ case_id: caseId }])
    : reply(200, { batch_id: "batch-active", status: "running", counts: { running: 1 }, items: [
      { input: { case_id: caseId }, status: "running", result: { current_action: "preflight" } },
    ] }), { stored: [["windcatcher:orchestration-batch", "batch-active"]] });
  await running.ui.activate();
  assert.equal(await running.ui.startCurrentCase(caseId), false);
  assert.equal(running.calls.some(([, options]) => options?.method === "POST"), false);
});

test("empty cases and failed requests have distinct recovery states", async () => {
  const empty = harness(async () => reply(200, []));
  await empty.ui.activate();
  assert.equal(empty.nodes["orchestration-empty"].hidden, false);
  assert.equal(empty.nodes["orchestration-start"].disabled, true);
  let failed = true;
  const h = harness(async () => failed ? reply(403, {}) : reply(200, []));
  await h.ui.activate();
  assert.equal(h.nodes["orchestration-empty"].hidden, true);
  assert.match(h.nodes["orchestration-cases-status"].textContent, /403.*刷新列表/);
  failed = false;
  await h.click("load");
  assert.equal(h.nodes["orchestration-empty"].hidden, false);
  assert.doesNotMatch(h.nodes["orchestration-status"].textContent, /读取失败/);
});

test("review opens the exact case and expanded execution details survive refresh", async () => {
  const caseId = "case-20261102-120001-aaaa";
  const opened = [];
  const h = harness(async url => url === "/api/cases" ? reply(200, [{ case_id: caseId }])
    : reply(200, { batch_id: "batch-review", status: "completed", counts: { blocked: 1 }, items: [
      { input: { case_id: caseId }, status: "blocked", result: { reason: "端面待复核" } },
    ] }), { openCase: async id => { opened.push(id); return true; } });
  await h.ui.activate(); await h.click("select-all"); await h.click("start");
  const results = h.nodes["orchestration-results"];
  const open = findNode(results, n => n.id === "orchestration-open-" + caseId);
  await open.listeners.click();
  assert.deepEqual(opened, [caseId]);
  const details = findNode(results, n => n.className === "agent-run-details");
  assert.equal(details.open, false);
  details.open = true; details.listeners.toggle();
  await h.click("refresh");
  assert.equal(findNode(results, n => n.className === "agent-run-details").open, true);
  assert.equal(h.calls.filter(([, options]) => options?.method === "POST").length, 1);
});

test("a restored active batch keeps selection controls locked without duplicate submission", async () => {
  const caseId = "case-20261102-120001-aaaa";
  const h = harness(async url => url === "/api/cases" ? reply(200, [{ case_id: caseId }])
    : reply(200, { batch_id: "batch-restored", status: "running", counts: { running: 1 }, items: [
      { input: { case_id: caseId }, status: "running", result: { current_action: "preflight" } },
    ] }), { stored: [["windcatcher:orchestration-batch", "batch-restored"]] });
  await h.ui.activate();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.nodes["orchestration-cases"].children.length, 1, "restored batches still load visible cases");
  assert.equal(h.nodes["orchestration-start"].disabled, true);
  assert.equal(h.nodes["orchestration-select-all"].disabled, true);
  assert.equal(h.nodes["orchestration-current"].disabled, true);
  assert.equal(h.nodes["orchestration-cancel"].hidden, false);
  assert.equal(h.calls.some(([, options]) => options?.method === "POST"), false);
});

test("agent control has its own route without crowding the workbench", () => {
  const work = index.slice(index.indexOf('id="page-work"'), index.indexOf('id="page-agent"'));
  const agent = index.slice(index.indexOf('id="page-agent"'), index.indexOf('id="page-cases"'));
  assert.match(index, /data-route="agent"[^>]*aria-label="Agent 自动处理"/);
  assert.doesNotMatch(work, /id="(?:orchestration-panel|harness-advice-panel)"/);
  assert.match(work, /id="viewer-card"/);
  assert.match(agent, /id="orchestration-panel"/);
  assert.match(agent, /<details[^>]*id="orchestration-settings"[^>]*>/);
  assert.doesNotMatch(agent, /<details[^>]*id="orchestration-settings"[^>]* open/);
  assert.equal([...index.matchAll(/id="orchestration-start"/g)].length, 1);
  assert.match(index, /id="orchestration-status"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.ok(index.indexOf("/static/app.js") < index.indexOf("/static/orchestration-ui.js"));
});

test("batch sends only sorted anonymous real-case IDs and renders each blocker", async () => {
  const first = "case-20261102-120001-aaaa";
  const second = "case-20261102-120002-bbbb";
  const h = harness(async (url) => {
    if (url === "/api/cases") return reply(200, [
      { case_id: second, synthetic: false, patient_name: "must not send" },
      { case_id: "case-20261102-120003-cccc", synthetic: true },
      { case_id: "bad-id", synthetic: false },
      { case_id: first, synthetic: false },
    ]);
    if (url === "/api/orchestration/batches") return reply(202, { batch_id: "batch-aaaa" });
    return reply(200, { batch_id: "batch-aaaa", status: "completed", counts: { blocked: 2 }, items: [
      { input: { case_id: first }, status: "blocked", result: { reason: "掩膜待复核", remediation: ["核对 CT 并确认掩膜"] } },
      { input: { case_id: second }, status: "blocked", result: { reason: "三端面待复核", remediation: ["核对左右鼻孔与鼻咽"] } },
    ] });
  });
  await h.click("load");
  assert.equal(h.nodes["orchestration-cases"].children.length, 2);
  await h.click("select-all");
  await h.click("start");
  const post = h.calls.find(([url]) => url === "/api/orchestration/batches");
  assert.deepEqual(JSON.parse(post[1].body).case_ids, [first, second]);
  assert.equal(JSON.parse(post[1].body).use_harness, true);
  assert.equal(JSON.parse(post[1].body).retry_failed, false);
  assert.doesNotMatch(post[1].body, /patient_name|must not send/);
  assert.match(textTree(h.nodes["orchestration-results"]), /掩膜待复核/);
  assert.match(textTree(h.nodes["orchestration-results"]), /核对左右鼻孔与鼻咽/);
  assert.equal(h.nodes["orchestration-cancel"].hidden, true);
  assert.equal(h.storage.get("windcatcher:orchestration-batch"), "batch-aaaa");
});

test("a failed submission keeps its idempotency key for retry", async () => {
  let attempts = 0;
  const h = harness(async url => {
    if (url === "/api/cases") return reply(200, [{ case_id: "case-20261102-120001-aaaa", synthetic: false }]);
    if (url === "/api/orchestration/batches") return ++attempts === 1
      ? reply(503, { detail: { reason: "队列未运行" } }) : reply(202, { batch_id: "batch-bbbb" });
    return reply(200, { batch_id: "batch-bbbb", status: "completed", items: [], counts: {} });
  });
  await h.click("load");
  await h.click("select-all");
  await h.click("start");
  assert.match(h.nodes["orchestration-status"].textContent, /提交未确认.*队列未运行/);
  await h.click("start");
  const posts = h.calls.filter(([url]) => url === "/api/orchestration/batches");
  assert.equal(JSON.parse(posts[0][1].body).request_key, JSON.parse(posts[1][1].body).request_key);
});

test("unknown submission locks its payload and current-case launch retries only the same request", async () => {
  const first = "case-20261102-120001-aaaa";
  const second = "case-20261102-120002-bbbb";
  let attempts = 0;
  const h = harness(async url => {
    if (url === "/api/cases") return reply(200, [{ case_id: first }, { case_id: second }]);
    if (url === "/api/orchestration/batches") return ++attempts === 1
      ? reply(503, { detail: { reason: "queue unavailable" } })
      : reply(202, { batch_id: "batch-retry" });
    return reply(200, { batch_id: "batch-retry", status: "completed", items: [], counts: {} });
  });
  await h.ui.activate();
  assert.equal(await h.ui.startCurrentCase(first), false);
  assert.equal(h.nodes["orchestration-start"].textContent, "重试确认提交");
  for (const id of ["load", "current", "select-all", "clear", "use-harness", "retry-failed"]) {
    assert.equal(h.nodes["orchestration-" + id].disabled, true, id + " must be locked");
  }
  await h.click("select-all");
  assert.equal(h.nodes["orchestration-selected-count"].textContent, "已选 1 例");
  assert.equal(await h.ui.startCurrentCase(second), false);
  assert.equal(h.calls.filter(([url]) => url === "/api/orchestration/batches").length, 1);
  assert.equal(await h.ui.startCurrentCase(first), true);
  const posts = h.calls.filter(([url]) => url === "/api/orchestration/batches");
  assert.equal(posts.length, 2);
  assert.equal(posts[0][1].body, posts[1][1].body);
});

test("stalled submission times out visibly and unlocks safe retry", async () => {
  const h = harness(async (url, options) => {
    if (url === "/api/cases") return reply(200, [{ case_id: "case-20261102-120001-aaaa" }]);
    return new Promise((_, reject) => options.signal?.addEventListener("abort", () => {
      const error = new Error("aborted"); error.name = "AbortError"; reject(error);
    }));
  });
  await h.click("load"); await h.click("select-all");
  const pending = h.click("start");
  const timeout = [...h.timers.values()].find(t => t.ms === 20000);
  assert.ok(timeout, "pending requests need a bounded timeout");
  timeout.fn(); await pending;
  assert.match(h.nodes["orchestration-status"].textContent, /超时/);
  assert.equal(h.nodes["orchestration-start"].disabled, false);
});

test("poll failure retains running task and automatically retries without allowing duplicate start", async () => {
  const caseId = "case-20261102-120001-aaaa";
  const h = harness(async url => {
    if (url === "/api/cases") return reply(200, [{ case_id: caseId }]);
    if (url === "/api/orchestration/batches") return reply(202, {
      batch_id: "batch-live", status: "running", counts: { running: 1 }, created_at: new Date().toISOString(),
      items: [{ input: { case_id: caseId }, status: "running", result: { current_action: "segment_candidate" } }],
    });
    throw new Error("network offline");
  });
  await h.click("load"); await h.click("select-all"); await h.click("start");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.nodes["orchestration-start"].disabled, true);
  assert.equal(h.nodes["orchestration-cancel"].hidden, false);
  assert.match(textTree(h.nodes["orchestration-results"]), /生成掩膜候选/);
  assert.match(h.nodes["orchestration-status"].textContent, /自动重连/);
  assert.ok([...h.timers.values()].some(t => t.ms === 4000));
});

test("usage distinguishes server-only work, an unmetered model call, and absent action evidence", async () => {
  const caseId = "case-20261102-120001-aaaa";
  for (const [actions, usage] of [
    [[{ source: "server_rules", action: "wait_for_review", result: "paused" }], "server"],
    [[{ source: "deepseek-harness", action: "wait_for_review", result: "paused" }], "model"],
    [[], "unknown"],
  ]) {
    const h = harness(async url => {
      if (url === "/api/cases") return reply(200, [{ case_id: caseId, synthetic: false }]);
      if (url === "/api/orchestration/batches") return reply(202, { batch_id: "batch-cccc" });
      return reply(200, { batch_id: "batch-cccc", status: "completed", counts: { blocked: 1 }, items: [
        { input: { case_id: caseId }, status: "blocked", result: { actions,
          llm_usage: { status: "unknown", total_tokens: null }, reason: "待复核" } },
      ] });
    });
    await h.click("load");
    await h.click("select-all");
    await h.click("start");
    const rendered = textTree(h.nodes["orchestration-results"]);
    assert.equal(rendered.includes("本次未调用模型"), usage === "server");
    assert.equal(rendered.includes("模型 token 用量未完整返回"), usage === "model");
  }
});

test("batch distinguishes an expected pause from an execution failure", async () => {
  const first = "case-20261102-120001-aaaa";
  const second = "case-20261102-120002-bbbb";
  const h = harness(async url => {
    if (url === "/api/cases") return reply(200, [{ case_id: first }, { case_id: second }]);
    if (url === "/api/orchestration/batches") return reply(202, { batch_id: "batch-paused" });
    return reply(200, { batch_id: "batch-paused", status: "completed",
      counts: { blocked: 1, failed: 1 }, items: [
        { input: { case_id: first }, status: "blocked", result: { reason: "掩膜等待人工确认" } },
        { input: { case_id: second }, status: "failed", result: { reason: "求解器退出" } },
      ] });
  });
  await h.click("load"); await h.click("select-all"); await h.click("start");
  const result = h.nodes["orchestration-results"];
  assert.match(textTree(result), /已暂停.*暂停原因：掩膜等待人工确认/);
  assert.match(textTree(result), /处理失败.*失败原因：求解器退出/);
  assert.match(h.nodes["orchestration-status"].textContent, /暂停 1 例，失败 1 例/);
  assert.equal(findNode(result, n => n.id === "orchestration-open-" + first).textContent, "打开病例处理");
  assert.equal(findNode(result, n => n.id === "orchestration-open-" + second).textContent, "打开病例排查");
});

test("local preflight shows its current step without premature model-usage warnings", async () => {
  const caseId = "case-20261102-120001-aaaa";
  const h = harness(async url => {
    if (url === "/api/cases") return reply(200, [{ case_id: caseId }]);
    return reply(200, { batch_id: "batch-checking", status: "running", counts: { running: 1 }, items: [
      { input: { case_id: caseId }, status: "running", result: {
        current_action: "preflight", actions: [], next_action: "",
        llm_usage: { status: "unknown", total_tokens: null }
      } },
    ] });
  });
  await h.click("load"); await h.click("select-all"); await h.click("start");
  const rendered = textTree(h.nodes["orchestration-results"]);
  assert.match(rendered, /当前步骤：检查病例版本与处理条件/);
  assert.doesNotMatch(rendered, /模型 token 用量未完整返回|下一步：查看病例详情/);
});

test("automatic three-port step and local correction are named in the batch", async () => {
  const caseId = "case-20261102-120001-aaaa";
  const h = harness(async url => {
    if (url === "/api/cases") return reply(200, [{ case_id: caseId }]);
    if (url === "/api/orchestration/batches") return reply(202, { batch_id: "batch-ports" });
    return reply(200, { batch_id: "batch-ports", status: "completed", counts: { blocked: 1 }, items: [
      { input: { case_id: caseId }, status: "blocked", result: {
        reason: "三端网格有多环", next_action: "review_endplanes",
        actions: [{ source: "server_rules", action: "propose_endplanes", result: "blocked" }],
      } },
    ] });
  });
  await h.click("load"); await h.click("select-all"); await h.click("start");
  const rendered = textTree(h.nodes["orchestration-results"]);
  assert.match(rendered, /自动生成并验证双侧三端口/);
  assert.match(rendered, /下一步：局部修正三端口/);
});
