"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");

function productionFunction(name) {
  const start = source.match(new RegExp("^(?:async )?function " + name + "\\(", "m")).index;
  return source.slice(start, source.indexOf("\n}", start) + 2);
}

function fixture({ available = false, reason = "GPU 求解器未就绪", target = "cpu" } = {}) {
  const key = "windcatcher:cfd-compute-target:gpu-case";
  const store = new Map([[key, target]]);
  const ui = vm.createContext({
    state: { caseId: "gpu-case", clientRunningStage: null, computeCapabilities: {
      cpu: { physical_cores: 16, logical_cores: 32 },
      parallel: { strongest: 16, balanced: 12, custom_max: 32 },
      gpu: { detected: true, name: "NVIDIA GeForce RTX 3090", memory_gb: 24 },
      gpu_solver: { available, reason, device_name: "NVIDIA GeForce RTX 3090" },
    } },
    localStorage: { getItem: (item) => store.get(item) ?? null, setItem: (item, value) => store.set(item, value) },
  });
  vm.runInContext([
    "escapeHtml", "cfdParallelStorageKey", "getCfdParallelConfig", "cfdParallelLabel",
    "cfdEffectiveParallelPlan", "cfdComputeTargetStorageKey", "getCfdComputeTarget",
    "setCfdComputeTarget", "cfdGpuAvailability", "cfdResourceCard", "cfdSolverExecutionSummary",
  ].map(productionFunction).join("\n"), ui);
  const stages = Object.fromEntries(
    ["import", "qc", "anonymize", "segment", "surface", "cfd_prep", "cfd_run", "report"]
      .map((name) => [name, { status: "ok", data: {} }]),
  );
  stages.surface.data.model_scope = "main_airway";
  stages.cfd_prep.data.parallel = { profile: "strongest", requested_subdomains: 16 };
  stages.cfd_run.status = "pending";
  stages.report.status = "pending";
  return { ui, store, key, data: { case_id: "gpu-case", synthetic: false, stages } };
}

function bindMainAction(ui, data) {
  const listeners = new Map();
  const box = { innerHTML: "", querySelectorAll: () => [], querySelector: () => null };
  const primary = { addEventListener: (name, fn) => listeners.set(name, fn) };
  const requests = [];
  Object.assign(ui, {
    STAGE_ORDER: Object.keys(data.stages),
    $: (id) => id === "case-actions" ? box : id === "act-primary" ? primary : { addEventListener() {} },
    requiresMaskReview: () => false, endplaneSaved: () => true,
    modelScopeParams: () => ({ params: { model_scope: "main_airway" } }),
    runAction: (_button, action) => action(),
    toast() {},
    api: (route, options) => { requests.push({ route, options }); },
  });
  vm.runInContext(productionFunction("renderActions"), ui);
  ui.renderActions(data);
  return { box, primary, listeners, requests };
}

test("unavailable GPU stays selected but is disabled with a concrete reason", () => {
  const { ui, data } = fixture({ target: "gpu", reason: "GPU 镜像尚未通过预检" });
  const html = ui.cfdResourceCard(data);
  assert.match(html, /name="cfd-compute-target" value="gpu"[^>]*checked[^>]*disabled/);
  assert.match(html, /GPU 镜像尚未通过预检/);
  assert.equal(ui.getCfdComputeTarget("gpu-case"), "gpu");
});

test("unavailable GPU blocks the main action without CPU fallback", () => {
  const { ui, data } = fixture({ target: "gpu", reason: "GPU 镜像尚未通过预检" });
  const { box, primary, listeners, requests } = bindMainAction(ui, data);
  assert.match(box.innerHTML, /id="act-primary"[^>]*disabled[^>]*>GPU 求解未就绪/);
  listeners.get("click")({ currentTarget: primary });
  assert.equal(requests.length, 0);
});

test("GPU selection reaches cfd-run and a new case defaults to CPU", () => {
  const { ui, data, store, key } = fixture({ target: "gpu", available: true, reason: "" });
  const { primary, listeners, requests } = bindMainAction(ui, data);
  listeners.get("click")({ currentTarget: primary });
  assert.equal(requests[0].route, "/api/cases/gpu-case/cfd-run");
  assert.equal(requests[0].options.body.compute_target, "gpu");
  store.delete(key);
  ui.renderActions(data);
  listeners.get("click")({ currentTarget: primary });
  assert.equal(requests[1].options.body.compute_target, "cpu");
});

test("GPU capability loss before clicking does not start a stale CPU or GPU action", () => {
  const { ui, data } = fixture({ target: "gpu", available: true, reason: "" });
  const { primary, listeners, requests } = bindMainAction(ui, data);
  let starts = 0;
  let warning = "";
  ui.runAction = () => { starts += 1; };
  ui.toast = (message) => { warning = message; };
  ui.state.computeCapabilities.gpu_solver = { available: false, reason: "GPU 容器已停止" };
  listeners.get("click")({ currentTarget: primary });
  assert.equal(starts, 0);
  assert.equal(requests.length, 0);
  assert.match(warning, /GPU 容器已停止/);
});

test("GPU result distinguishes requested, successful and unverified execution", () => {
  const { ui } = fixture();
  const prep = { solver_gpu_note: "标准 simpleFoam 只使用 CPU" };
  const runStatus = { compute_target: "gpu", execution: {
    compute_target: "gpu", mode: "gpu-single", gpu_used: false, gpu: { device_name: "RTX 3090" },
  } };
  const pending = ui.cfdSolverExecutionSummary(runStatus, {}, prep, false);
  assert.equal(pending.executionLabel, "GPU 单进程（本次求解目标）");
  assert.match(pending.resourceLabel, /求解目标为 GPU 单进程/);
  assert.match(pending.gpuLabel, /尚未确认成功使用/);
  assert.doesNotMatch(pending.gpuLabel, /已使用 GPU|只使用 CPU/);
  runStatus.execution.gpu_used = true;
  const success = ui.cfdSolverExecutionSummary(runStatus, {}, prep, true);
  assert.equal(success.executionLabel, "GPU 单进程求解");
  assert.match(success.gpuLabel, /已使用 GPU · RTX 3090/);
  const incomplete = ui.cfdSolverExecutionSummary(runStatus, {}, prep, false);
  assert.doesNotMatch(incomplete.gpuLabel, /已使用 GPU/);
});

test("GPU resource settings describe MPI as mesh-only", () => {
  const { ui, data } = fixture({ target: "gpu", available: true });
  const html = ui.cfdResourceCard(data);
  assert.match(html, /GPU 气流求解使用下方设备选择/);
  assert.match(html, /16 个 MPI 子域 · 网格生成/);
  assert.doesNotMatch(html, /标准 simpleFoam 当前只使用 CPU/);
});

test("legacy CPU result retains its actual MPI execution", () => {
  const { ui } = fixture();
  const summary = ui.cfdSolverExecutionSummary({ execution: { mode: "mpi", requested_subdomains: 8 } }, {
    run_status: { parallel: { requested_subdomains: 8 } },
  }, { parallel: { profile: "strongest", requested_subdomains: 8 } }, true);
  assert.equal(summary.executionLabel, "MPI 并行（8 个子域）");
  assert.match(summary.resourceLabel, /最强 · 8 个 MPI 子域/);
});
