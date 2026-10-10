"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "activity-monitor.js"), "utf8");

function response(status = 200, body = {}) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function element(tagName = "DIV", options = {}) {
  const value = {
    tagName, id: "", type: "", value: "", checked: false, textContent: "", hidden: false,
    dataset: {}, children: [],
    closest() { return this; },
    addEventListener() {},
    append(...items) { this.children.push(...items); },
    appendChild(item) { this.children.push(item); },
    replaceChildren(...items) { this.children = items; },
    ...options,
  };
  return value;
}

function harness(nativeFetch, options = {}) {
  const listeners = new Map();
  const timers = new Map();
  let clock = 1791586800000;
  let timerId = 0;
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock])); }
    static now() { return clock; }
  }
  const nodes = options.nodes || new Map();
  const document = {
    readyState: "loading",
    addEventListener(name, handler) { listeners.set(name, handler); },
    getElementById(id) { return nodes.get(id) || null; },
    createElement(tag) { return element(tag.toUpperCase()); },
  };
  const window = {
    location: { hash: options.hash || "#work", origin: "http://127.0.0.1:8765", href: "http://127.0.0.1:8765/#work" },
    crypto: { randomUUID: () => "00000000-0000-4000-8000-000000000001" },
    fetch: nativeFetch,
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, at: clock + delay }); return id; },
    clearTimeout: (id) => timers.delete(id),
    queueMicrotask: (fn) => fn(),
    addEventListener(name, handler) { listeners.set(`window:${name}`, handler); },
  };
  const context = vm.createContext({ window, document, URL, Headers, Request, Uint8Array, Array, Date: FakeDate, Promise, JSON, Math, Number, String, Boolean, Error, Set, Map, Object, RegExp, console });
  vm.runInContext(source, context, { filename: "activity-monitor.js" });
  async function settle() { for (let pass = 0; pass < 8; pass++) await Promise.resolve(); }
  async function advance(ms) {
    const end = clock + ms;
    for (let pass = 0; pass < 100; pass++) {
      await settle();
      const entry = Array.from(timers.entries()).filter(([, item]) => item.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!entry) { clock = end; await settle(); return; }
      timers.delete(entry[0]);
      clock = entry[1].at;
      entry[1].fn();
    }
    throw new Error("Timer storm");
  }
  return { window, document, listeners, timers, nodes, advance, settle, monitor: window.WindcatcherMonitor };
}

test("record strips raw values and sends only a bounded event batch", async () => {
  const calls = [];
  const h = harness(async (input, options = {}) => {
    calls.push({ input, options });
    return response(201, { accepted: options.body ? JSON.parse(options.body).events.length : 0 });
  });
  h.monitor.setContextProvider(() => ({ route: "cases", case_id: "case-20261007-120000-abcd" }));
  h.monitor.record("ui", "change", { value: "E:\\secret\\scan.dcm", checked: true, controls: [1.333333, 2, 3], api_key: "do-not-send" }, "newcase-path");
  await h.monitor.flush();
  assert.equal(calls.length, 1);
  const events = JSON.parse(calls[0].options.body).events;
  assert.equal(events.length, 1);
  assert.equal(events[0].details.checked, true);
  assert.deepEqual(events[0].details.controls, [1.333, 2, 3]);
  assert.equal(Object.prototype.hasOwnProperty.call(events[0].details, "api_key"), false);
  assert.equal(JSON.stringify(events).includes("secret"), false);
  assert.equal(events[0].case_id, "case-20261007-120000-abcd");
  assert.ok(events[0].details.client_time_ms > 1700000000000);
  assert.equal(events[0].details.sequence, 1);
});

test("monitor endpoint is isolated while same-origin API requests get correlation headers", async () => {
  const calls = [];
  const h = harness(async (input, options = {}) => {
    calls.push({ input: String(input), options });
    return response(200, {});
  });
  await h.window.fetch("/api/monitor/status");
  await h.window.fetch("/api/cases");
  assert.ok(calls.length >= 2);
  const monitorCall = calls.find((call) => call.input.includes("/api/monitor/status"));
  const apiCall = calls.find((call) => call.input === "/api/cases");
  assert.ok(monitorCall && apiCall);
  assert.equal(new Headers(monitorCall.options.headers).has("X-Windcatcher-Session"), false);
  const headers = new Headers(apiCall.options.headers);
  assert.equal(headers.has("X-Windcatcher-Session"), true);
  assert.equal(headers.has("X-Windcatcher-Request-Id"), true);
  assert.equal(JSON.stringify(apiCall.options).includes("/api/cases"), false);
});

test("queue is bounded and flush keeps insertion order in batches of 50", async () => {
  const batches = [];
  const h = harness(async (_input, options = {}) => {
    const events = JSON.parse(options.body).events;
    batches.push(events);
    return response(201, {});
  });
  for (let index = 0; index < 260; index++) h.monitor.record("ui", "click", { button: index }, `button-${index}`);
  for (let count = 0; count < 6; count++) await h.monitor.flush();
  assert.ok(batches.length >= 4 && batches.length <= 5);
  assert.ok(batches.every((batch) => batch.length <= 50));
  const sequence = batches.flat().map((event) => event.details.sequence);
  assert.ok(sequence.every((value, index) => index === 0 || value > sequence[index - 1]));
  assert.ok(sequence.length <= 201);
  const summary = batches.flat().find((event) => event.action === "queue_dropped");
  assert.equal(summary.details.count, 60);
});

test("failed delivery backs off instead of issuing an immediate retry", async () => {
  let calls = 0;
  const h = harness(async () => { calls++; return response(503, {}); });
  h.monitor.record("ui", "click", {}, "broken-button");
  assert.equal(await h.monitor.flush(), false);
  assert.equal(await h.monitor.flush(), false);
  assert.equal(calls, 1);
  await h.advance(5000);
  assert.equal(calls, 2);
  await h.advance(10000);
  assert.equal(calls, 3);
  await h.advance(60000);
  assert.equal(calls, 3);
});

test("text, file, configuration and case selector values never enter operation logs", async () => {
  const delivered = [];
  const h = harness(async (_input, options) => { delivered.push(...JSON.parse(options.body).events); return response(201); });
  const controls = [
    element("INPUT", { id: "ai-report-api-key", type: "password", value: "secret-key-do-not-send" }),
    element("INPUT", { id: "ai-report-base-url", type: "text", value: "https://secret-provider.example/private" }),
    element("INPUT", { id: "newcase-path", type: "text", value: "E:\\Sensitive\\CT" }),
    element("INPUT", { id: "newcase-file", type: "file", value: "C:\\private\\image.dcm" }),
    element("SELECT", { id: "case-select", value: "private-patient-name" }),
    element("SELECT", { id: "ai-report-model", value: "secret-model" }),
  ];
  controls.forEach((control) => {
    h.listeners.get("input")({ target: control });
    h.listeners.get("change")({ target: control });
  });
  await h.monitor.flush();
  assert.equal(delivered.length, controls.length);
  const payload = JSON.stringify(delivered);
  ["secret-key", "secret-provider", "Sensitive", "private-patient", "secret-model", "image.dcm"].forEach((value) => assert.equal(payload.includes(value), false));
  assert.ok(delivered.every((event) => !Object.prototype.hasOwnProperty.call(event.details, "value")));
});

test("rapid numeric changes coalesce, preserve their last value and precede the next click", async () => {
  const delivered = [];
  const h = harness(async (_input, options) => { delivered.push(...JSON.parse(options.body).events); return response(201); });
  const range = element("INPUT", { id: "slice-range-axial", type: "range", value: "1" });
  for (let index = 1; index <= 100; index++) {
    range.value = String(index);
    h.listeners.get("input")({ target: range });
  }
  h.listeners.get("click")({ target: element("BUTTON", { id: "mask-save" }) });
  await h.monitor.flush();
  assert.equal(delivered.length, 2);
  assert.equal(delivered[0].action, "input");
  assert.equal(delivered[0].details.value, 100);
  assert.equal(delivered[0].details.count, 100);
  assert.equal(delivered[1].target, "mask-save");
  assert.ok(delivered[0].details.sequence < delivered[1].details.sequence);
});

test("external requests retain their options and sensitive API URLs and body remain outside logs", async () => {
  const calls = [];
  const payloads = [];
  const originalResponse = response(200);
  const h = harness(async (input, options) => {
    calls.push({ input, options });
    if (input === "/api/monitor/events") payloads.push(options.body);
    return originalResponse;
  });
  const externalOptions = { headers: { Authorization: "private" }, body: "sensitive" };
  assert.equal(await h.window.fetch("https://outside.example/api/cases", externalOptions), originalResponse);
  assert.equal(calls[0].options, externalOptions);
  const apiOptions = { method: "POST", headers: { "X-Existing": "retained" }, body: "api-key-private", signal: new AbortController().signal };
  assert.equal(await h.window.fetch("/api/cases?path=private-path", apiOptions), originalResponse);
  const sent = calls.find((call) => String(call.input).includes("path=private-path"));
  assert.equal(sent.options.body, apiOptions.body);
  assert.equal(sent.options.signal, apiOptions.signal);
  assert.equal(sent.options.headers.get("X-Existing"), "retained");
  await h.monitor.flush();
  assert.equal(payloads.join("").includes("private"), false);
  const event = JSON.parse(payloads[0]).events[0];
  assert.equal(event.kind, "request");
  assert.equal(event.details.method, "POST");
  assert.equal(event.details.status_code, 200);
});

test("AbortError remains the original rejection and is distinguished from an HTTP failure", async () => {
  const failure = Object.assign(new Error("private-path from upstream"), { name: "AbortError" });
  const events = [];
  const h = harness(async (input, options) => {
    if (input === "/api/monitor/events") { events.push(...JSON.parse(options.body).events); return response(201); }
    if (input === "/api/failure") return response(422);
    throw failure;
  });
  await assert.rejects(h.window.fetch("/api/cancelled"), (error) => error === failure);
  assert.equal((await h.window.fetch("/api/failure")).status, 422);
  await h.monitor.flush();
  assert.equal(events[0].action, "network_error");
  assert.equal(events[0].details.outcome, "cancelled");
  assert.equal(events[1].details.outcome, "error");
  assert.equal(events[1].details.status_code, 422);
  assert.equal(JSON.stringify(events).includes("private-path"), false);
});

test("concurrent flush sends one batch and does not remove events added while sending", async () => {
  let resolveSend;
  const deliveries = [];
  const h = harness((input, options) => {
    deliveries.push(JSON.parse(options.body).events);
    return new Promise((resolve) => { resolveSend = resolve; });
  });
  h.monitor.record("ui", "click", {}, "first");
  const first = h.monitor.flush();
  assert.equal(h.monitor.flush(), first);
  await h.settle();
  h.monitor.record("ui", "click", {}, "second");
  assert.equal(deliveries.length, 1);
  resolveSend(response(201));
  await first;
  const second = h.monitor.flush();
  await h.settle();
  assert.equal(deliveries[1][0].target, "second");
  resolveSend(response(201));
  await second;
});

test("canvas pointer and wheel summaries have no image, absolute coordinates or per-frame events", async () => {
  const events = [];
  const h = harness(async (_input, options) => { events.push(...JSON.parse(options.body).events); return response(201); });
  const canvas = element("CANVAS", { id: "slice-canvas-axial" });
  h.listeners.get("pointerdown")({ target: canvas, pointerId: 1, clientX: 10, clientY: 20, button: 0 });
  h.listeners.get("pointerup")({ target: canvas, pointerId: 1, clientX: 30, clientY: 55, button: 0 });
  for (let index = 0; index < 80; index++) h.listeners.get("wheel")({ target: canvas, deltaY: 5 });
  await h.monitor.flush();
  assert.equal(events.length, 2);
  assert.equal(events[0].details.delta_x, 20);
  assert.equal(events[0].details.delta_y, 35);
  assert.equal(events[1].details.count, 80);
  assert.equal(events[1].details.delta, 400);
  assert.ok(events.every((event) => event.kind === "viewer"));
  assert.equal(JSON.stringify(events).includes("clientX"), false);
});

test("JavaScript failures retain type and known source location but remove the error text", async () => {
  const events = [];
  const h = harness(async (_input, options) => { events.push(...JSON.parse(options.body).events); return response(201); });
  h.listeners.get("window:error")({ error: new TypeError("patient-secret"), filename: "http://127.0.0.1:8765/static/app.js?private-key=x", lineno: 9, colno: 4, message: "patient-secret" });
  h.listeners.get("window:unhandledrejection")({ reason: { name: "SecretTokenIsNotErrorType", message: "patient-secret" } });
  await h.monitor.flush();
  assert.equal(events[0].details.error_type, "TypeError");
  assert.equal(events[0].details.source, "app.js");
  assert.equal(events[0].details.line, 9);
  assert.equal(events[1].details.error_type, "UnknownError");
  assert.equal(JSON.stringify(events).includes("secret"), false);
});

test("latest log panel uses safe text nodes and local event time without recursive activity queries", async () => {
  const nodes = new Map(["monitor-events", "monitor-status", "monitor-summary", "monitor-error", "monitor-refresh", "monitor-errors-only"].map((id) => [id, element("DIV", { id })]));
  const calls = [];
  const h = harness(async (input) => {
    calls.push(input);
    if (input === "/api/monitor/status") return response(200, { status: "ok", total_events: 3, retention_days: 14 });
    return response(200, { events: [{ kind: "ui", action: "click", target: "<script>private</script>", route: "work", details: { client_time_ms: 1791586800000, value: "secret-text" } }] });
  }, { nodes });
  await h.monitor.refreshPanel();
  assert.equal(calls.length, 2);
  assert.ok(calls[1].includes("latest=true"));
  assert.equal(nodes.get("monitor-events").children.length, 1);
  const item = nodes.get("monitor-events").children[0];
  assert.equal(item.children[0].dateTime, "2026-10-09T23:00:00.000Z");
  assert.equal(item.children[1].textContent.includes("<script>"), false);
  assert.equal(item.children[1].textContent.includes("secret-text"), false);
  assert.equal(item.children[1].textContent.includes("client_time_ms"), false);
  assert.ok(item.children[1].textContent.includes("操作：点击"));
  assert.equal(await h.monitor.flush(), false);
});

test("all processing stage names survive event sanitization and session startup is recorded once", async () => {
  const events = [];
  const h = harness(async (_input, options) => { events.push(...JSON.parse(options.body).events); return response(201); });
  h.listeners.get("DOMContentLoaded")();
  h.listeners.get("DOMContentLoaded")();
  ["qc", "anonymize", "cfd_prep", "cfd_run"].forEach((stage) => h.monitor.record("stage", "action_started", { stage, status: "running" }, "stage"));
  await h.monitor.flush();
  assert.equal(events.filter((event) => event.action === "session_started").length, 1);
  assert.deepEqual(events.filter((event) => event.kind === "stage").map((event) => event.details.stage), ["qc", "anonymize", "cfd_prep", "cfd_run"]);
});

test("only known viewer selects expose a fixed choice and hash changes use the new route", async () => {
  const events = [];
  const h = harness(async (_input, options) => { events.push(...JSON.parse(options.body).events); return response(201); });
  h.monitor.setContextProvider(() => ({ route: "work", case_id: "case-20261007-120000-abcd" }));
  h.listeners.get("change")({ target: element("SELECT", { id: "mask-edit-view", value: "sagittal" }) });
  h.listeners.get("change")({ target: element("SELECT", { id: "cfd-slice-axis", value: "x" }) });
  h.listeners.get("change")({ target: element("SELECT", { id: "ai-report-model", value: "sagittal" }) });
  h.window.location.hash = "#results";
  h.listeners.get("window:hashchange")();
  await h.monitor.flush();
  assert.equal(events[0].details.view, "sagittal");
  assert.equal(events[1].details.view, "x");
  assert.equal(Object.prototype.hasOwnProperty.call(events[2].details, "view"), false);
  assert.equal(events[3].route, "results");
});

test("panel recognizes recording and presents an unavailable service as a recoverable query error", async () => {
  const nodes = new Map(["monitor-events", "monitor-status", "monitor-summary", "monitor-error", "monitor-refresh"].map((id) => [id, element("DIV", { id })]));
  let healthStatus = "recording";
  const h = harness(async (input) => response(200, input === "/api/monitor/status" ? { status: healthStatus, total_events: 1, retention_days: 14 } : { events: [] }), { nodes });
  await h.monitor.refreshPanel();
  assert.equal(nodes.get("monitor-status").textContent, "正在记录");
  assert.equal(nodes.get("monitor-error").hidden, true);
  healthStatus = "unavailable";
  await h.monitor.refreshPanel();
  assert.equal(nodes.get("monitor-status").textContent, "记录服务暂不可查询");
  assert.equal(nodes.get("monitor-error").hidden, false);
  assert.ok(nodes.get("monitor-error").textContent.includes("刷新记录"));
});

test("successful retry clears delivery errors without clearing an independent query failure", async () => {
  const nodes = new Map(["monitor-events", "monitor-status", "monitor-summary", "monitor-error", "monitor-refresh"].map((id) => [id, element("DIV", { id })]));
  let failDelivery = true;
  let failQuery = false;
  const h = harness(async (input) => {
    if (input === "/api/monitor/events") return response(failDelivery ? 503 : 201);
    if (failQuery) return response(503);
    return response(200, input === "/api/monitor/status" ? { status: "recording", total_events: 1, retention_days: 14 } : { events: [] });
  }, { nodes });
  h.monitor.record("ui", "click", {}, "first");
  await h.monitor.flush();
  assert.equal(nodes.get("monitor-error").hidden, false);
  failDelivery = false;
  await h.advance(5000);
  assert.equal(nodes.get("monitor-error").hidden, true);
  failQuery = true;
  await h.monitor.refreshPanel();
  h.monitor.record("ui", "click", {}, "second");
  await h.monitor.flush();
  assert.equal(nodes.get("monitor-error").hidden, false);
  assert.ok(nodes.get("monitor-error").textContent.includes("读取操作记录失败"));
});
