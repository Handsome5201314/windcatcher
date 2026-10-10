"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
function productionFunction(name) {
  const start = source.match(new RegExp(`^(?:async )?function ${name}\\(`, "m")).index;
  return source.slice(start, source.indexOf("\n}", start) + 2);
}
const context = vm.createContext({
  state: { caseId: "legacy-case", computeCapabilities: {
    cpu: { physical_cores: 16, logical_cores: 32 },
    parallel: { strongest: 16, balanced: 12, custom_max: 32 },
    gpu: { detected: true, name: "NVIDIA GeForce RTX 3090", memory_gb: 24 },
    solver_gpu_note: "标准 simpleFoam 当前只使用 CPU；GPU 不会自动参与。",
  } },
  localStorage: { getItem() { return null; }, setItem() {} },
});
vm.runInContext(["escapeHtml", "renderCfdProgress", "cfdParallelStorageKey", "getCfdParallelConfig", "cfdParallelLabel", "cfdEffectiveParallelPlan", "cfdRuntimeRows", "cfdMeshRows", "cfdResourceCard"].map(productionFunction).join("\n"), context);

test("unknown iteration percentage is indeterminate, never zero percent", () => {
  const html = context.renderCfdProgress({ current_step: "snappyHexMesh", progress: { completed: 1, total: 7, current_step_percent: null }, elapsed_seconds: 65 }, {}, "running");
  assert.match(html, /当前步骤正在处理，百分比未知/);
  assert.match(html, /1\/7 个步骤已完成/);
  assert.match(html, /总耗时 1 分 5 秒/);
  assert.doesNotMatch(html, /value="0"|<b>0\.0%/);
});

test("real outer iteration ratio is explicitly distinguished from convergence", () => {
  const html = context.renderCfdProgress({ current_step: "simpleFoam", solver: { iteration_percent: 12.3 }, progress: { completed: 4, total: 7 } }, {}, "running");
  assert.match(html, /value="12\.3"/);
  assert.match(html, /是否收敛需看残差/);
  assert.match(html, /步骤完成比例不代表剩余时间/);
});

test("legacy failed records do not invent total steps, duration or animation", () => {
  const html = context.renderCfdProgress({ failed_step: "simpleFoam", steps: [{ returncode: 0 }, { returncode: 0 }, { returncode: 0 }, { returncode: 124 }] }, {}, "failed");
  assert.match(html, /总步数未记录/);
  assert.match(html, /总耗时 未记录/);
  assert.doesNotMatch(html, /75|0 分 0 秒|<progress/);
});

test("failed CFD action preserves the detailed diagnosis rendered from the backend", async () => {
  let replaced = false;
  const state = { caseId: "test-case", case: { stages: {} } };
  const actionContext = vm.createContext({
    state, window: {}, guardEndplaneDraft: () => true, busy() {}, startCasePolling() {}, stopCasePolling() {},
    renderCase() {}, renderSteps() {}, toast() {}, hasRunningStages: () => false,
    api: async () => ({ stages: { cfd_prep: { status: "ok" }, cfd_run: { status: "failed", data: { run_status: { status: "failed", returncode: 124 } } } } }),
    renderErrorPanel: () => { replaced = true; },
  });
  vm.runInContext(productionFunction("runAction"), actionContext);
  await actionContext.runAction({}, async () => ({ status: "failed", message: "timeout" }), "cfd_run");
  assert.equal(replaced, false);
});

test("resource card reads legacy MPI plan from run status and marks it as historical", () => {
  const html = context.cfdResourceCard({
    case_id: "legacy-case",
    stages: {
      cfd_prep: { status: "ok", data: {} },
      cfd_run: { status: "failed", data: { run_status: { parallel: { enabled: true, requested_subdomains: 8 } } } },
    },
  });
  assert.match(html, /当前算例：历史配置 · 8 个 MPI 子域/);
  assert.match(html, /已选择 最强（16 个子域），需要重新生成 CFD 算例后才会生效/);
  assert.match(html, /按当前选择重新生成 CFD 算例/);
});

test("actual run plan inherits prepared profile only when the MPI counts match", () => {
  const data = {
    case_id: "prepared-case",
    stages: {
      cfd_prep: { status: "ok", data: { parallel: { profile: "strongest", requested_subdomains: 16 } } },
      cfd_run: { status: "failed", data: { run_status: { parallel: { enabled: true, requested_subdomains: 16 } } } },
    },
  };
  const before = JSON.stringify(data);
  assert.equal(context.cfdEffectiveParallelPlan(data).profile, "strongest");
  assert.match(context.cfdResourceCard(data), /当前算例：最强 · 16 个 MPI 子域/);
  data.stages.cfd_run.data.run_status.parallel.requested_subdomains = 8;
  assert.equal(context.cfdEffectiveParallelPlan(data).profile, "legacy");
  assert.match(context.cfdResourceCard(data), /当前算例：历史配置 · 8 个 MPI 子域/);
  data.stages.cfd_run.data.run_status.parallel.requested_subdomains = 16;
  assert.equal(JSON.stringify(data), before);
});

test("runtime CPU evidence distinguishes container capacity from host hardware", () => {
  assert.equal(context.cfdRuntimeRows({}).length, 0);
  const rows = context.cfdRuntimeRows({ runtime: {
    backend: "docker", cpu: { logical_cores: 24, physical_cores: 12 },
    allocation_mode: "hwthread", requested_subdomains: 16,
  } });
  assert.match(JSON.stringify(rows), /容器可用 24 个逻辑核/);
  assert.match(JSON.stringify(rows), /12 个物理核/);
  assert.match(JSON.stringify(rows), /CPU 线程/);
  assert.doesNotMatch(JSON.stringify(rows), /32|GPU/);
});

test("MPI preflight has a clear Chinese progress label", () => {
  const html = context.renderCfdProgress({ current_step: "mpiPreflight" }, {}, "running");
  assert.match(html, /检查 MPI 并行启动/);
  assert.doesNotMatch(html, /当前步骤：mpiPreflight/);
});

test("mesh generation progress reports the actual mesh execution plan", () => {
  const html = context.renderCfdProgress({
    current_step: "snappyHexMesh",
    execution: { mode: "mpi", requested_subdomains: 16, mesh: { action: "rebuilt", mode: "parallel", requested_subdomains: 16, cache: { status: "checking" } } },
    progress: { completed: 3, total: 10, current_step_percent: null },
  }, {}, "running");
  assert.match(html, /并行生成气道网格（16 个进程）/);
  assert.match(html, /当前步骤正在处理，百分比未知/);
  assert.doesNotMatch(html, /value="0"|<b>0\.0%/);
});

test("mesh stage labels and percentages do not inherit an old solver iteration", () => {
  for (const [current_step, label] of [
    ["decomposeMesh", "分配网格到 CPU 子域"],
    ["reconstructMesh", "合并并行网格"],
    ["meshCacheCheck", "核对已有网格"],
    ["prepareRun", "归档旧结果，准备本次计算"],
  ]) {
    const html = context.renderCfdProgress({ current_step, solver: { iteration_percent: 85 } }, {}, "running");
    assert.match(html, new RegExp(label));
    assert.match(html, /当前步骤正在处理，百分比未知/);
    assert.doesNotMatch(html, /85\.0%|value="85"|迭代数相对上限/);
  }
  const serial = context.renderCfdProgress({ current_step: "snappyHexMesh", execution: { mesh: { action: "rebuilt", mode: "serial", requested_subdomains: 1 } } }, {}, "running");
  assert.match(serial, /单进程生成气道网格/);
  const legacy = context.renderCfdProgress({ current_step: "snappyHexMesh", execution: { mode: "mpi", requested_subdomains: 16 } }, {}, "running");
  assert.match(legacy, /生成气道网格（执行方式未记录）/);
  assert.doesNotMatch(legacy, /并行生成|16 个进程/);
});

test("resource settings explain that both mesh generation and solving use the selected plan", () => {
  const html = context.cfdResourceCard({ case_id: "new-case", stages: {} });
  assert.match(html, /Docker 网格生成与气流求解使用该设置；其他后端以运行记录为准/);
  assert.match(html, /16 个 MPI 子域 · 网格与求解/);
});

test("pending mesh execution is a plan, never proof of completed generation", () => {
  const rows = context.cfdMeshRows({ mesh: { action: "rebuilt", status: "pending", mode: "parallel", requested_subdomains: 16, cache: { status: "checking" } } });
  assert.equal(rows[0][1], "计划：并行生成气道网格（16 个进程）");
  assert.match(JSON.stringify(rows), /等待本次网格检查/);
});

test("mesh cache availability is separate from the decision to reuse", () => {
  const mesh = { action: "rebuilt", status: "checked", mode: "parallel", requested_subdomains: 16, reason: "旧网格没有可靠复用记录", cache: { status: "unavailable", reason: "缓存未记录：缺少网格依赖" } };
  const rows = context.cfdMeshRows({ mesh });
  assert.equal(rows.find(([key]) => key === "复用判定")[1], "旧网格没有可靠复用记录");
  assert.equal(rows.find(([key]) => key === "下次复用")[1], "缓存未记录：缺少网格依赖");
  mesh.cache = { status: "ready", reason: "已记录本次网格内容及检查证据" };
  assert.equal(context.cfdMeshRows({ mesh }).find(([key]) => key === "下次复用")[1], "检查证据已保存");
});

function renderCfdDetailFixture(runStatus) {
  const panel = { innerHTML: "", querySelector() { return null; } };
  const detailContext = vm.createContext({
    $: () => panel,
    state: {},
    effectiveStageStatus: (_stage, status) => status,
    fmt: (value, suffix = "") => value == null ? null : `${value}${suffix}`,
  });
  vm.runInContext(["escapeHtml", "kvList", "renderCfdProgress", "cfdParallelLabel", "cfdEffectiveParallelPlan", "cfdRuntimeRows", "cfdMeshRows", "renderDetails"].map(productionFunction).join("\n"), detailContext);
  const stages = Object.fromEntries(["qc", "anonymize", "segment", "surface", "report"].map((key) => [key, { status: "pending" }]));
  stages.cfd_prep = { status: "ok", data: { parallel: { profile: "balanced", requested_subdomains: 12 } } };
  stages.cfd_run = { status: "running", data: { run_status: runStatus } };
  detailContext.renderDetails({ case_id: "mesh-case", stages });
  return panel.innerHTML;
}

test("CFD details show mesh execution evidence without deriving it from solver resources", () => {
  const runtime = { backend: "docker", cpu: { logical_cores: 24, physical_cores: 12 } };
  const parallel = renderCfdDetailFixture({ execution: { mode: "mpi", requested_subdomains: 12, runtime, mesh: {
    action: "rebuilt", mode: "parallel", requested_subdomains: 12, reason: "几何已变更，重新生成网格", cache: { status: "unavailable" },
  } }, mesh: { cells: 3082110 } });
  assert.match(parallel, /<dt>网格执行<\/dt><dd>并行生成气道网格（12 个进程）/);
  assert.match(parallel, /几何已变更，重新生成网格/);
  assert.match(parallel, /容器可用 24 个逻辑核 \/ 12 个物理核/);
  assert.match(parallel, /3082110 个单元/);

  const serial = renderCfdDetailFixture({ execution: { mode: "serial", requested_subdomains: 1, mesh: {
    action: "rebuilt", mode: "serial", requested_subdomains: 1,
  } } });
  assert.match(serial, /<dt>网格执行<\/dt><dd>单进程生成气道网格/);

  const reused = renderCfdDetailFixture({ execution: { mode: "mpi", requested_subdomains: 12, mesh: {
    action: "reused", mode: "parallel", requested_subdomains: 16, source_run_id: "run-&-source", reason: "几何与网格设置一致", cache: { status: "ready" },
  } } });
  assert.match(reused, /MPI 并行（12 个子域）/);
  assert.match(reused, /复用已生成网格 · 本次重新检查质量/);
  assert.match(reused, /仅复用网格；本次重新求解气流/);
  assert.match(reused, /<dt>原网格生成<\/dt><dd>并行生成气道网格（16 个进程）/);
  assert.match(reused, /<dt>网格来源<\/dt><dd>run-&amp;-source/);

  const legacy = renderCfdDetailFixture({ execution: { mode: "mpi", requested_subdomains: 16 } });
  assert.match(legacy, /执行方式未记录；不能据求解子域数判断网格并行/);
  assert.doesNotMatch(legacy, /并行生成气道网格|复用已生成网格/);
});

test("CFD details use the same prepared profile and show the log-based failure and runtime", () => {
  const panel = { innerHTML: "", querySelector() { return null; } };
  const detailContext = vm.createContext({
    $: () => panel,
    state: {},
    effectiveStageStatus: (_stage, status) => status,
    fmt: (value, suffix = "") => value == null ? null : `${value}${suffix}`,
  });
  vm.runInContext(["escapeHtml", "kvList", "renderCfdProgress", "cfdParallelLabel", "cfdEffectiveParallelPlan", "cfdRuntimeRows", "cfdMeshRows", "renderDetails"].map(productionFunction).join("\n"), detailContext);
  const stages = Object.fromEntries(["qc", "anonymize", "segment", "surface", "report"].map((key) => [key, { status: "pending" }]));
  stages.cfd_prep = { status: "ok", data: { parallel: { profile: "strongest", requested_subdomains: 16 } } };
  stages.cfd_run = { status: "failed", data: { run_status: {
    status: "failed", failed_step: "simpleFoam", returncode: 1,
    message: "MPI 槽位不足，未开始气流迭代", remediation: ["重新检测求解环境 CPU 并执行并行预检"],
    parallel: { requested_subdomains: 16 },
    execution: { mode: "mpi", requested_subdomains: 16, runtime: {
      backend: "docker", cpu: { logical_cores: 24, physical_cores: 12 }, allocation_mode: "hwthread",
    } },
  } } };

  detailContext.renderDetails({ case_id: "prepared-case", stages });

  assert.match(panel.innerHTML, /最强 · 16 个 MPI 子域/);
  assert.doesNotMatch(panel.innerHTML, /历史配置/);
  assert.match(panel.innerHTML, /容器可用 24 个逻辑核/);
  assert.match(panel.innerHTML, /MPI 槽位不足，未开始气流迭代/);
  assert.match(panel.innerHTML, /重新检测求解环境 CPU 并执行并行预检/);
});
