"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "access-context.js"), "utf8");
const monitorSource = fs.readFileSync(path.join(__dirname, "..", "static", "activity-monitor.js"), "utf8");

function page(hostname) {
  const badge = { textContent: "仅本机 · 127.0.0.1" };
  const description = { textContent: "影像只在你的电脑上处理" };
  const footer = { textContent: "本地处理 · 数据不上传" };
  const help = { textContent: "请选择文件夹" };
  const picker = { disabled: false, title: "" };
  const monitor = { hidden: false };
  const selectors = new Map([
    [".local-led", badge], ["#page-cases .page-heading p", description],
    ["#viewer-card .viewer-foot", footer],
  ]);
  const ids = new Map([
    ["source-help", help], ["btn-pick-directory", picker],
    ["monitor-title", { closest: () => monitor }],
  ]);
  const window = { location: { hostname }, fetch: async () => ({ ok: true }) };
  const document = { querySelector: (selector) => selectors.get(selector), getElementById: (id) => ids.get(id) };
  const context = vm.createContext({ window, document });
  vm.runInContext(source, context);
  return { window, context, badge, description, footer, help, picker, monitor };
}

test("loopback access preserves the local workflow and its data-location statements", () => {
  for (const hostname of ["localhost", "127.0.0.1", "[::1]"]) {
    const p = page(hostname);
    assert.equal(p.window.WindcatcherSharedAccess, false);
    assert.equal(p.picker.disabled, false);
    assert.equal(p.monitor.hidden, false);
    assert.equal(p.footer.textContent, "本地处理 · 数据不上传");
  }
});

test("shared access switches the directory picker to browser upload", () => {
  const p = page("windcatcher.tongyimoheai.top");
  assert.equal(p.window.WindcatcherSharedAccess, true);
  assert.match(p.badge.textContent, /熟人共享/);
  assert.match(p.description.textContent, /可上传已去标识 DICOM/);
  assert.match(p.footer.textContent, /DICOM 通过 HTTPS 上传/);
  assert.match(p.help.textContent, /通过 HTTPS 上传到服务主机/);
  assert.equal(p.picker.disabled, false);
  assert.match(p.picker.textContent, /上传/);
  assert.equal(p.monitor.hidden, true);
});

test("the shared page does not instrument visitor fetches or query private operation history", () => {
  const p = page("windcatcher.tongyimoheai.top");
  const nativeFetch = p.window.fetch;
  vm.runInContext(monitorSource, p.context);
  assert.equal(p.window.fetch, nativeFetch);
  assert.equal(p.window.WindcatcherMonitor, undefined);
});
