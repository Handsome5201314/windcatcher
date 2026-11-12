"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static", "patient-view.js"), "utf8");
const html = fs.readFileSync(path.join(root, "deploy", "patient-view.html"), "utf8");
const css = fs.readFileSync(path.join(root, "static", "patient-view.css"), "utf8");

async function harness(reducedMotion = false, pressureUnit = "Pa") {
  const nodes = new Map();
  const frames = new Map();
  const documentListeners = {};
  const windowListeners = {};
  const calls = [];
  let frameId = 0;
  function element(id) {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: id === "model-panel", value: id === "field" ? "velocity" : "",
      textContent: "", disabled: false, attributes: { "aria-pressed": id === "motion" ? "true" : "false" },
      listeners: {}, style: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      getAttribute(name) { return this.attributes[name] ?? null; },
      addEventListener(name, callback) { this.listeners[name] = callback; },
    });
    return nodes.get(id);
  }
  class FakeViewer {
    constructor(container, data, options) {
      calls.push(["create", options.patientPresentation]);
      this.container = container;
      this.options = options;
    }
    resize() { calls.push(["resize"]); }
    setPresentationMode(value) { calls.push(["mode", value]); }
    setPresentationPose(azimuth, elevation) { calls.push(["pose", azimuth, elevation]); }
    setField(value) {
      calls.push(["field", value]);
      this.options.onLegend({ field: value, range: value === "pressure" ? [-1.58, 23.14] : [0, 4.2],
        unit: value === "pressure" ? pressureUnit : "m/s", gradient: "linear-gradient(to right, blue, red)" });
    }
    setPlaying(value) { calls.push(["particles", value]); }
    reset() { calls.push(["reset"]); }
    dispose() { calls.push(["dispose"]); }
  }
  const document = {
    hidden: false,
    getElementById: element,
    addEventListener(name, callback) { documentListeners[name] = callback; },
  };
  const context = vm.createContext({
    document, CfdViewer: FakeViewer, location: { pathname: "/share/synthetic" },
    window: {
      matchMedia: () => ({ matches: reducedMotion }),
      addEventListener(name, callback) { windowListeners[name] = callback; },
    },
    fetch: async () => ({ status: 200, ok: true, json: async () => ({}) }),
    requestAnimationFrame(callback) { frames.set(++frameId, callback); return frameId; },
    cancelAnimationFrame(id) { frames.delete(id); },
  });
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  return {
    nodes, calls, frames, document, documentListeners, windowListeners,
    frame(now) {
      const next = frames.entries().next().value;
      assert.ok(next, "guided tour must schedule a frame");
      frames.delete(next[0]);
      next[1](now);
    },
    click(id) { element(id).listeners.click({ currentTarget: element(id) }); },
  };
}

test("patient page offers guided playback, reset and an accessible progress caption", () => {
  for (const id of ["tour-toggle", "view-reset", "tour-progress", "tour-caption"]) {
    assert.match(html, new RegExp('id="' + id + '"'));
  }
  assert.match(html, /粒子运动是稳态计算的可视化/);
  assert.doesNotMatch(html, /正常人.*患者对比/);
  assert.match(html, /patient-view\.css\?v=20261101-reference1/);
  assert.match(html, /patient-view\.js\?v=20261101-reference1/);
  assert.match(css, /\.model canvas\s*\{[^}]*width:\s*100%;[^}]*height:\s*100%/);
  assert.match(source, /pageshow/);
  assert.match(source, /event\.persisted/);
});

test("tour starts automatically, changes from anatomy to flow and finishes with replay", async () => {
  const h = await harness();
  assert.deepEqual(h.calls[0], ["create", true]);
  assert.equal(h.nodes.get("tour-toggle").textContent, "暂停导览");
  assert.ok(h.calls.some(call => call[0] === "mode" && call[1] === "anatomy"));
  assert.ok(h.calls.some(call => call[0] === "particles" && call[1] === true));
  h.frame(0);
  h.frame(20000);
  assert.ok(h.calls.some(call => call[0] === "mode" && call[1] === "flow"));
  assert.ok(h.calls.some(call => call[0] === "particles" && call[1] === true));
  assert.match(h.nodes.get("tour-caption").textContent, /不是实时呼吸/);
  h.frame(60000);
  assert.equal(h.nodes.get("tour-toggle").textContent, "重播导览");
  assert.equal(h.nodes.get("tour-progress").value, 60);
  h.click("tour-toggle");
  assert.equal(h.nodes.get("tour-progress").value, 0);
  assert.equal(h.nodes.get("tour-toggle").textContent, "暂停导览");
});

test("touch pauses the tour and reset restores free exploration", async () => {
  const h = await harness();
  h.nodes.get("model").listeners.pointerdown();
  assert.equal(h.frames.size, 0);
  assert.equal(h.nodes.get("tour-toggle").textContent, "继续导览");
  h.click("view-reset");
  assert.ok(h.calls.some(call => call[0] === "reset"));
  assert.equal(h.nodes.get("tour-toggle").attributes["aria-pressed"], "false");
  h.click("tour-toggle");
  assert.equal(h.frames.size, 1);
  h.windowListeners.pagehide();
  assert.equal(h.frames.size, 0);
  assert.ok(h.calls.some(call => call[0] === "dispose"));
});

test("reduced-motion users start the tour deliberately", async () => {
  const h = await harness(true);
  assert.equal(h.frames.size, 0);
  assert.equal(h.nodes.get("tour-toggle").textContent, "播放导览");
  assert.equal(h.nodes.get("motion").textContent, "播放粒子");
  h.click("tour-toggle");
  assert.equal(h.frames.size, 1);
});

test("legend explains the active field and labels both color endpoints", async () => {
  for (const id of ["legend-title", "legend-scale", "legend-colorbar", "legend-min", "legend-max", "legend-description", "legend-unit-note"]) {
    assert.match(html, new RegExp('id="' + id + '"'));
  }
  const h = await harness();
  assert.equal(h.nodes.get("legend-title").textContent, "速度颜色说明");
  assert.match(h.nodes.get("legend-min").textContent, /低速度.*0\.00.*m\/s/);
  assert.match(h.nodes.get("legend-max").textContent, /高速度.*4\.20.*m\/s/);
  assert.match(h.nodes.get("legend-description").textContent, /蓝色.*较低.*红色.*较高/);
  assert.match(h.nodes.get("legend-description").textContent, /不代表正常或异常/);
  assert.match(h.nodes.get("legend-unit-note").textContent, /米.*秒/);
  assert.equal(h.nodes.get("legend-colorbar").style.background, "linear-gradient(to right, blue, red)");
  h.nodes.get("field").listeners.change({ target: { value: "pressure" } });
  assert.equal(h.nodes.get("legend-title").textContent, "压力颜色说明");
  assert.match(h.nodes.get("legend-min").textContent, /低压力.*-1\.58.*Pa/);
  assert.match(h.nodes.get("legend-max").textContent, /高压力.*23\.14.*Pa/);
  assert.match(h.nodes.get("legend-unit-note").textContent, /帕/);
  h.click("tour-toggle");
  assert.equal(h.nodes.get("legend-title").textContent, "速度颜色说明");
});

test("legacy pressure explicitly explains why it is not displayed in Pa", async () => {
  for (const unit of ["m2/s2", "m²/s²"]) {
    const h = await harness(false, unit);
    h.nodes.get("field").listeners.change({ target: { value: "pressure" } });
    assert.match(h.nodes.get("legend-min").textContent, /m²\/s²/);
    assert.match(h.nodes.get("legend-unit-note").textContent, /运动学压力.*空气密度.*未换算为 Pa/);
  }
});
