"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static", "operator-preferences.js"), "utf8");
const appSource = fs.readFileSync(path.join(root, "static", "app.js"), "utf8");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");
const stepIds = ["qc", "segment", "endplanes", "surface", "cfd_prep", "cfd_run",
  "cfd_results", "report", "review", "share"];

function element(id = "") {
  const classes = new Set();
  return {
    id, value: "", checked: false, disabled: false, hidden: id === "operator-preferences-panel",
    textContent: "", children: [], listeners: {}, labels: [{ textContent: id }],
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

function preferences() {
  return { view_pose: { azimuth: 0.3, elevation: -0.2, zoom: 1.25 },
    display_ranges: { velocity: { min: 0, max: 12.5, unit: "m/s" },
      pressure: { min: -120, max: 80, unit: "Pa" } },
    favorite_steps: ["segment", "cfd_results"] };
}

function row(version = 1, data = preferences()) {
  return { user_id: "a".repeat(32), version, schema_version: 1, preferences: data };
}

function harness({ multiuser = true, replies = [response(200, row())] } = {}) {
  const ids = [...new Set([...index.matchAll(/id="([^"]+)"/g)].map(match => match[1]))];
  const nodes = Object.fromEntries(ids.map(id => [id, element(id)]));
  const calls = [];
  const opened = [];
  const window = { WindcatcherMultiuser: multiuser, WindcatcherOpenFavoriteStep: step => opened.push(step) };
  const document = { getElementById: id => nodes[id], createElement: () => element() };
  const fetch = async (url, options) => {
    calls.push({ url, options });
    const result = replies.shift();
    if (!result) throw new Error("unexpected request");
    return result;
  };
  vm.runInNewContext(source, { window, document, fetch });
  return { nodes, calls, opened, ui: window.WindcatcherOperatorPreferences };
}

test("personal settings are mounted only for authenticated multiuser mode", async () => {
  assert.match(index, /id="operator-preferences-panel"[^>]*hidden/);
  assert.match(index, /static\/operator-preferences\.js/);
  assert.match(appSource, /WindcatcherOperatorPreferences\?\.activate\(\)/);
  assert.match(appSource, /WindcatcherOperatorPreferences\?\.applyToViewer/);
  const local = harness({ multiuser: false });
  await local.ui.activate();
  assert.equal(local.nodes["operator-preferences-panel"].hidden, true);
  assert.equal(local.calls.length, 0);
});

test("load, edit and save use optimistic version and explicit display units", async () => {
  const h = harness({ replies: [response(200, row()), response(200, row(2, {
    view_pose: { azimuth: Math.PI / 2, elevation: 0, zoom: 1.5 },
    display_ranges: { velocity: { min: 1, max: 18, unit: "m/s" },
      pressure: { min: -20, max: 40, unit: "Pa" } },
    favorite_steps: ["segment", "cfd_results"],
  }))] });
  await h.ui.activate();
  assert.equal(h.nodes["operator-preferences-panel"].hidden, false);
  assert.equal(h.nodes["pref-velocity-max"].value, "12.5");
  assert.equal(h.nodes["operator-preferences-save"].disabled, false);
  h.nodes["pref-azimuth"].value = "90";
  h.nodes["pref-elevation"].value = "0";
  h.nodes["pref-zoom"].value = "1.5";
  h.nodes["pref-velocity-min"].value = "1";
  h.nodes["pref-velocity-max"].value = "18";
  h.nodes["pref-pressure-min"].value = "-20";
  h.nodes["pref-pressure-max"].value = "40";
  await h.nodes["operator-preferences-form"].listeners.submit({ preventDefault() {} });
  const write = h.calls[1];
  assert.equal(write.url, "/api/operator/preferences");
  assert.equal(write.options.method, "PUT");
  assert.equal(write.options.credentials, "same-origin");
  const body = JSON.parse(write.options.body);
  assert.equal(body.expected_version, 1);
  assert.ok(Math.abs(body.preferences.view_pose.azimuth - Math.PI / 2) < 1e-12);
  assert.deepEqual(body.preferences.display_ranges, {
    velocity: { min: 1, max: 18, unit: "m/s" },
    pressure: { min: -20, max: 40, unit: "Pa" },
  });
  assert.equal(h.nodes["operator-favorite-links"].children.length, 2);
  h.nodes["operator-favorite-links"].children[1].listeners.click();
  assert.deepEqual(h.opened, ["cfd_results"]);
  assert.match(h.nodes["operator-preferences-status"].textContent, /版本 2/);
});

test("version conflict blocks another save until reloaded without losing the visible draft", async () => {
  const h = harness({ replies: [response(200, row()), response(409, { detail: { reason: "stale" } }),
    response(200, row(2))] });
  await h.ui.activate();
  h.nodes["pref-velocity-max"].value = "20";
  await h.nodes["operator-preferences-form"].listeners.submit({ preventDefault() {} });
  assert.equal(h.nodes["pref-velocity-max"].value, "20");
  assert.equal(h.nodes["operator-preferences-save"].disabled, true);
  assert.match(h.nodes["operator-preferences-status"].textContent, /重新加载/);
  await h.nodes["operator-preferences-refresh"].listeners.click();
  assert.equal(h.nodes["pref-velocity-max"].value, "12.5");
  assert.equal(h.nodes["operator-preferences-save"].disabled, false);
});

test("saved pose and exact-unit color ranges affect only the active CFD viewer", async () => {
  const h = harness();
  await h.ui.activate();
  const poses = [], calls = [];
  const viewer = { disposed: false, ranges: { velocity: [0, 30], pressure: [-200, 200] },
    applyOperatorPose(pose) { poses.push(pose); },
    setColorRanges(ranges) {
      if (!ranges) this.ranges = { velocity: [0, 30], pressure: [-200, 200] };
      else { this.ranges = ranges; calls.push(ranges); }
    },
  };
  h.ui.applyToViewer(viewer, { units: { velocity: "m/s", pressure: "m2/s2" } });
  assert.equal(poses.length, 1);
  assert.deepEqual(Array.from(calls[0].velocity), [0, 12.5]);
  assert.deepEqual(Array.from(calls[0].pressure), [-200, 200]);
  viewer.disposed = true;
  h.ui.applyToViewer(viewer, { units: { velocity: "m/s", pressure: "Pa" } });
  assert.equal(calls.length, 1);
});

test("turning off a previously applied pose restores the current viewer default", async () => {
  const noPose = preferences();
  noPose.view_pose = null;
  const h = harness({ replies: [response(200, row()), response(200, row(2, noPose))] });
  await h.ui.activate();
  let applied = 0, reset = 0;
  const viewer = { disposed: false, ranges: { velocity: [0, 30], pressure: [-200, 200] },
    applyOperatorPose() { applied += 1; }, reset() { reset += 1; },
    setColorRanges(ranges) { if (!ranges) this.ranges = { velocity: [0, 30], pressure: [-200, 200] }; },
  };
  h.ui.applyToViewer(viewer, { units: { velocity: "m/s", pressure: "Pa" } });
  assert.equal(applied, 1);
  h.nodes["pref-pose-enabled"].checked = false;
  await h.nodes["operator-preferences-form"].listeners.submit({ preventDefault() {} });
  assert.equal(reset, 1);
  assert.equal(applied, 1);
});

test("invalid numeric range and ninth favorite never send updates", async () => {
  const h = harness();
  await h.ui.activate();
  for (const step of stepIds.slice(0, 8)) h.nodes["pref-step-" + step].checked = true;
  h.nodes["pref-step-review"].checked = true;
  h.nodes["pref-step-review"].listeners.change({ target: h.nodes["pref-step-review"] });
  assert.equal(h.nodes["pref-step-review"].checked, false);
  h.nodes["pref-velocity-min"].value = "20";
  h.nodes["pref-velocity-max"].value = "10";
  await h.nodes["operator-preferences-form"].listeners.submit({ preventDefault() {} });
  assert.equal(h.calls.length, 1);
  assert.equal(h.nodes["pref-velocity-max"].focused, true);
});
