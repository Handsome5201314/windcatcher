/* 风见工作台 · 对齐风格1概念图版式：步骤卡条 → 大数字指标 → 大视窗。全部数值来自本机真实产物。 */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  cases: [],
  caseId: null,
  case: null,
  modelScope: "main_nasal_airway",
  previewMode: "smooth",
  viewer3d: null,
  mode: "3d",
  pollTimer: null,
  clientRunningStage: null,
  uploadFiles: [],
  viewerAssetCaseId: null,
  viewerAssetMode: null,
  viewer3dCaseId: null,
  viewer3dKey: null,
  scenarioViewers: new Set(),
  scenarioPolls: new Map(),
  ctViewer: {
    caseId: null,
    maskKey: null,
    meta: null,
    metaPromise: null,
    metaRequestId: 0,
    level: -500,
    width: 2000,
    overlay: true,
    views: {
      axial: { index: 0, zoom: 1, panX: 0, panY: 0, image: null, controller: null, cursor: null },
      coronal: { index: 0, zoom: 1, panX: 0, panY: 0, image: null, controller: null, cursor: null },
      sagittal: { index: 0, zoom: 1, panX: 0, panY: 0, image: null, controller: null, cursor: null },
    },
  },
  maskEditor: {
    active: false,
    view: "axial",
    tool: "paint",
    radius: 8,
    stroke: null,
    cursor: null,
    preview: [],
    pending: [],
    saving: false,
    autoCleaning: false,
    externalAirAction: null,
    externalAirFeedback: null,
    dirty: false,
    history: [],
  },
  endplanes: {
    open: false,
    key: null,
    profile: null,
    status: "idle",
    error: null,
    pairError: null,
    stale: false,
    values: { anterior: null, posterior: null },
    baseline: { anterior: null, posterior: null },
    dirtySides: { anterior: false, posterior: false },
    planes: { anterior: null, posterior: null },
    baselinePlanes: { anterior: null, posterior: null },
    format: "oblique_planes_v1",
    baselineFormat: "oblique_planes_v1",
    anteriorSeed: null,
    baselineAnteriorSeed: null,
    activeInlet: "left",
    // New bilateral drafts start with two independent inlet selections. A
    // shared plane remains an explicit advanced option for compatible cases.
    sharedInletPlane: false,
    sectionTargets: null,
    bilateral: { left: { plane: null, seed: null }, right: { plane: null, seed: null } },
    baselineBilateral: { left: { plane: null, seed: null }, right: { plane: null, seed: null } },
    posteriorSeed: null,
    baselinePosteriorSeed: null,
    mode: "anterior",
    tool: "translate",
    placement: "point",
    pickedPoints: [],
    showGizmo: false,
    adjustments: { anterior: null, posterior: null },
    history: { anterior: [], posterior: [] },
    editHistory: [],
    sectionKey: null,
    sectionTimer: null,
    sectionAbort: null,
    sectionRequestId: 0,
    sectionUrl: null,
    viewer: null,
    selectionKey: null,
    selectionStatus: "idle",
    selectionError: null,
    selectionRequestId: 0,
    selectionAbort: null,
    openingMarkup: {
      left: { opening_id: "left_nostril_opening", side: "left", seed_lps_mm: null, outline_lps_mm: [], status: "empty", source: "three_d_model_draft", persisted: false },
      right: { opening_id: "right_nostril_opening", side: "right", seed_lps_mm: null, outline_lps_mm: [], status: "empty", source: "three_d_model_draft", persisted: false },
      posterior: { opening_id: "posterior_nostril_opening", side: "posterior", seed_lps_mm: null, outline_lps_mm: [], status: "empty", source: "three_d_model_draft", persisted: false },
      anterior: { opening_id: "anterior_nostril_opening", side: "anterior", seed_lps_mm: null, outline_lps_mm: [], status: "empty", source: "three_d_model_draft", persisted: false },
    },
    openingStroke: null,
    formalPorts: { status: "idle", data: null, error: null, requestId: 0,
      visual: { status: "idle", side: null, error: null, requestId: 0, controller: null, viewer: null, ctUrl: null } },
    autoCandidate: { status: "idle", reason: null, requestId: 0, controller: null },
    singlePortSuggestion: { status: "idle", reason: null, requestId: 0, controller: null, result: null, signature: null, contextKey: null, side: null, originalBody: null },
    previewStatus: "idle",
    previewData: null,
    previewError: null,
    previewSignature: null,
    previewTimer: null,
    previewRequestId: 0,
    previewAbort: null,
    actualPreviewStatus: "idle",
    actualPreviewError: null,
    actualPreviewSignature: null,
    actualPreviewRequestId: 0,
    actualPreviewAbort: null,
    dirty: false,
    saving: false,
    requestId: 0,
  },
  installJobs: new Map(),
  computeCapabilities: null,
  cfdResults: { viewer: null, sections: null, caseId: null, requestId: 0, controller: null, pollTimer: null, status: null, result: null, payloadKey: null, actionPending: false },
};
window.WindcatcherMonitor?.setContextProvider(() => ({
  case_id: window.location.hash === "#results" ? state.cfdResults.caseId : state.caseId,
  route: state.route || window.location.hash.slice(1) || "work",
}));

// 与后端实际执行顺序一致：先质控，再按选项生成匿名化副本。
const STAGE_ORDER = ["import", "qc", "anonymize", "segment", "surface", "cfd_prep", "cfd_run", "report"];
const STAGE_LABELS = {
  import: "导入",
  anonymize: "本地去标识",
  qc: "输入质控",
  segment: "气道分割",
  surface: "STL 与几何",
  cfd_prep: "CFD 算例",
  cfd_run: "气流求解",
  report: "报告",
};
const CT_VIEWS = ["axial", "coronal", "sagittal"];
const CT_VIEW_LABELS = { axial: "轴位", coronal: "冠状", sagittal: "矢状" };
const MODEL_SCOPE_OPTIONS = {
  main_nasal_airway: "主鼻腔气流模型",
  complete_candidate: "完整候选范围",
};

function modelScopeStorageKey(caseId) {
  return `windcatcher:model-scope:${caseId}`;
}

function getModelScope(caseId = state.caseId) {
  if (!caseId) return "main_nasal_airway";
  if (state.caseId === caseId && MODEL_SCOPE_OPTIONS[state.modelScope]) return state.modelScope;
  try {
    const saved = localStorage.getItem(modelScopeStorageKey(caseId));
    return MODEL_SCOPE_OPTIONS[saved] ? saved : "main_nasal_airway";
  } catch {
    return "main_nasal_airway";
  }
}

function setModelScope(caseId, scope) {
  if (!MODEL_SCOPE_OPTIONS[scope]) return;
  if (state.caseId === caseId) state.modelScope = scope;
  try { localStorage.setItem(modelScopeStorageKey(caseId), scope); } catch { /* 浏览器禁用存储时仍可继续当前病例 */ }
}

function modelScopeParams(caseId) {
  const modelScope = getModelScope(caseId);
  const segmentData = state.case?.stages?.segment?.data || {};
  // candidate_metrics 可能来自安全回退（主气道副本），只有能力标记为 true
  // 才允许把完整候选提交给 surface。
  const candidateAvailable = segmentData.candidate_mask_available === true;
  const effectiveScope = modelScope === "complete_candidate" && !candidateAvailable ? "main_nasal_airway" : modelScope;
  return {
    params: {
      // UI labels stay readable; surface API uses its explicit domain names.
      model_scope: effectiveScope === "complete_candidate" ? "full_candidate" : "main_airway",
      // 兼容旧版 surface 参数；新版后端应按 model_scope 选择候选范围。
      keep_largest_component: effectiveScope === "main_nasal_airway",
    },
  };
}

function cfdParallelStorageKey(caseId) {
  return `windcatcher:cfd-parallel:${caseId}`;
}

function getCfdParallelConfig(caseId = state.caseId) {
  const fallback = { profile: "strongest", custom_subdomains: null };
  if (!caseId) return fallback;
  try {
    const saved = JSON.parse(localStorage.getItem(cfdParallelStorageKey(caseId)) || "null");
    if (!saved || !["strongest", "balanced", "custom"].includes(saved.profile)) return fallback;
    const custom = Number.isInteger(Number(saved.custom_subdomains)) ? Number(saved.custom_subdomains) : null;
    return { profile: saved.profile, custom_subdomains: custom };
  } catch {
    return fallback;
  }
}

function setCfdParallelConfig(caseId, patch) {
  const next = { ...getCfdParallelConfig(caseId), ...patch };
  try { localStorage.setItem(cfdParallelStorageKey(caseId), JSON.stringify(next)); } catch { /* 当前页面仍可继续提交 */ }
  return next;
}

function cfdComputeTargetStorageKey(caseId) {
  return "windcatcher:cfd-compute-target:" + caseId;
}

function getCfdComputeTarget(caseId = state.caseId) {
  if (!caseId) return "cpu";
  try { return localStorage.getItem(cfdComputeTargetStorageKey(caseId)) === "gpu" ? "gpu" : "cpu"; }
  catch { return "cpu"; }
}

function setCfdComputeTarget(caseId, target) {
  if (!caseId || !["cpu", "gpu"].includes(target)) return getCfdComputeTarget(caseId);
  try { localStorage.setItem(cfdComputeTargetStorageKey(caseId), target); } catch { /* 当前页面仍可继续操作 */ }
  return target;
}

function cfdGpuAvailability() {
  const solver = state.computeCapabilities?.gpu_solver;
  if (solver?.available === true) return { available: true, reason: "", device_name: solver.device_name || null };
  const reason = typeof solver?.reason === "string" && solver.reason.trim()
    ? solver.reason.trim() : "GPU 求解后端尚未通过预检，请先使用 CPU。";
  return { available: false, reason, device_name: solver?.device_name || null };
}

function cfdParallelLabel(profile) {
  return ({ strongest: "最强", balanced: "适度", custom: "自定义", legacy: "历史配置", env: "环境变量" })[profile] || "最强";
}

function cfdEffectiveParallelPlan(data) {
  const prepared = data.stages?.cfd_prep?.data?.parallel;
  const runData = data.stages?.cfd_run?.data || {};
  const actual = runData.run_status?.parallel || runData.parallel || prepared;
  if (!actual) return null;
  // 运行记录是实际子域数的依据；仅在数量一致时沿用生成算例时的模式标签。
  const matchesPrepared = prepared && Number(actual.requested_subdomains) > 0
    && Number(actual.requested_subdomains) === Number(prepared.requested_subdomains);
  return { ...actual, profile: actual.profile || (matchesPrepared && prepared.profile) || "legacy" };
}

function cfdRuntimeRows(execution) {
  const runtime = execution.runtime || {};
  const cpu = runtime.cpu || {};
  const rows = [];
  if (Number.isInteger(cpu.logical_cores) && cpu.logical_cores > 0) {
    const physical = Number.isInteger(cpu.physical_cores) && cpu.physical_cores > 0 ? ` / ${cpu.physical_cores} 个物理核` : "";
    rows.push(["求解环境 CPU", `${runtime.backend === "docker" ? "容器" : "运行环境"}可用 ${cpu.logical_cores} 个逻辑核${physical}`]);
  }
  if (runtime.allocation_mode === "hwthread") {
    rows.push(["MPI 分配", "按可用 CPU 线程分配"]);
  }
  return rows;
}

function cfdSolverExecutionSummary(runStatus, cfdRunData, prepData, solved) {
  const execution = runStatus.execution || cfdRunData.execution || {};
  const computeTarget = runStatus.compute_target || execution.compute_target || cfdRunData.compute_target;
  if (computeTarget === "gpu" || execution.mode === "gpu-single") {
    const device = execution.gpu?.device_name;
    const gpuVerified = solved && execution.gpu_used === true;
    return {
      executionLabel: gpuVerified ? "GPU 单进程求解" : "GPU 单进程（本次求解目标）",
      resourceLabel: `气道网格按上方 MPI 设置生成；${gpuVerified ? "求解使用 GPU 单进程" : "求解目标为 GPU 单进程"}`,
      gpuLabel: gpuVerified
        ? `已使用 GPU${device ? ` · ${escapeHtml(device)}` : ""}`
        : "已选择 GPU；尚未确认成功使用（查看运行状态和日志）",
    };
  }
  const executionLabel = execution.mode === "mpi" ? `MPI 并行（${execution.requested_subdomains || "?"} 个子域）`
    : execution.mode === "serial" ? "单进程 CPU" : "—";
  const plan = cfdEffectiveParallelPlan({ stages: { cfd_prep: { data: prepData }, cfd_run: { data: cfdRunData } } }) || {};
  return {
    executionLabel,
    resourceLabel: `${escapeHtml(cfdParallelLabel(plan.profile || "legacy"))} · ${plan.requested_subdomains || "?"} 个 MPI 子域`,
    gpuLabel: escapeHtml(cfdRunData.solver_gpu_note || prepData.solver_gpu_note || "标准 simpleFoam 只使用 CPU"),
  };
}

function cfdMeshRows(execution) {
  const mesh = execution.mesh || {};
  const count = Number.isInteger(mesh.requested_subdomains) && mesh.requested_subdomains > 0 ? mesh.requested_subdomains : null;
  const generation = mesh.mode === "parallel" ? `并行生成气道网格${count ? `（${count} 个进程）` : "（进程数未记录）"}`
    : mesh.mode === "serial" ? "单进程生成气道网格" : "执行方式未记录；不能据求解子域数判断网格并行";
  const planned = mesh.action === "reused" ? "复用已生成网格 · 本次重新检查质量" : generation;
  const label = mesh.status === "pending" ? `计划：${planned}` : mesh.status === "failed" ? `未完成：${planned}` : planned;
  const rows = [["网格执行", label]];
  if (mesh.action === "reused") rows.push(["复用范围", "仅复用网格；本次重新求解气流"], ["原网格生成", generation]);
  if (mesh.reason) rows.push(["复用判定", escapeHtml(mesh.reason)]);
  const cache = mesh.cache || {};
  const cacheLabel = cache.status === "ready" ? "检查证据已保存"
    : cache.status === "unavailable" ? cache.reason || "缓存不可用，原因未记录"
      : cache.status === "checking" ? "等待本次网格检查" : cache.reason;
  if (cacheLabel) rows.push(["下次复用", escapeHtml(cacheLabel)]);
  if (mesh.action === "reused" && mesh.source_run_id) rows.push(["网格来源", escapeHtml(mesh.source_run_id)]);
  return rows;
}

function cfdResourceCard(data, { locked = false } = {}) {
  const config = getCfdParallelConfig(data.case_id);
  const compute = state.computeCapabilities || {};
  const cpu = compute.cpu || {};
  const parallel = compute.parallel || {};
  const gpu = compute.gpu || {};
  const prepared = data.stages?.cfd_prep?.status === "ok" || data.stages?.cfd_prep?.status === "warn";
  const prepUpdated = Date.parse(data.stages?.cfd_prep?.updated_at || "");
  const runUpdated = Date.parse(data.stages?.cfd_run?.updated_at || "");
  const prepIsNewer = Number.isFinite(prepUpdated) && Number.isFinite(runUpdated) && prepUpdated > runUpdated;
  const preparedPlan = prepIsNewer
    ? (data.stages?.cfd_prep?.data?.parallel || cfdEffectiveParallelPlan(data))
    : cfdEffectiveParallelPlan(data);
  const strongest = parallel.strongest ?? "自动";
  const balanced = parallel.balanced ?? "自动";
  const customMax = parallel.custom_max ?? cpu.logical_cores ?? 1;
  const customValue = config.custom_subdomains || strongest;
  const gpuText = gpu.detected ? `${gpu.name || "NVIDIA GPU"}${gpu.memory_gb ? ` · ${gpu.memory_gb} GB` : ""}` : "未检测到 NVIDIA GPU";
  const gpuSolver = cfdGpuAvailability();
  const note = gpuSolver.available ? "GPU 求解器已通过预检，可在下方选择；实际运行以求解记录为准。"
    : compute.solver_gpu_note || "标准 simpleFoam 当前只使用 CPU；GPU 不会自动参与。";
  const computeTarget = getCfdComputeTarget(data.case_id);
  const gpuDevice = gpuSolver.device_name || gpu.name || "NVIDIA GPU";
  const selectedSubdomains = config.profile === "custom" ? config.custom_subdomains : parallel[config.profile] || strongest;
  const preparedSubdomains = Number(preparedPlan?.requested_subdomains);
  const planChanged = prepared && Number.isFinite(preparedSubdomains) && Number(selectedSubdomains) !== preparedSubdomains;
  const failedRun = prepared && data.stages?.cfd_run?.status === "failed"
    && !prepIsNewer;
  const preparedText = preparedPlan
    ? `${cfdParallelLabel(preparedPlan.profile)} · ${preparedSubdomains || "?"} 个 MPI 子域`
    : "尚未生成 CFD 算例";
  return `<div class="cfd-resource-card" aria-label="CFD 计算资源设置">
    <div class="cfd-resource-head"><div><strong>计算资源</strong><p>${computeTarget === "gpu" ? "默认按本机物理核心选择网格生成子域数；GPU 气流求解使用下方设备选择。" : "默认按本机物理核心选择子域数；Docker 网格生成与气流求解使用该设置；其他后端以运行记录为准。"}</p></div><span class="pill pill-mute">当前选择：${cfdParallelLabel(config.profile)}</span></div>
    ${prepared ? `<div class="cfd-resource-current">当前算例：${escapeHtml(preparedText)}</div>` : ""}
    <div class="cfd-resource-options" role="radiogroup" aria-label="选择计算资源模式">
      <label class="cfd-resource-option"><input type="radio" name="cfd-parallel-profile" value="strongest" ${config.profile === "strongest" ? "checked" : ""} ${locked ? "disabled" : ""}><span><b>最强</b><small>${strongest} 个 MPI 子域 · ${computeTarget === "gpu" ? "网格生成" : "网格与求解"}</small></span></label>
      <label class="cfd-resource-option"><input type="radio" name="cfd-parallel-profile" value="balanced" ${config.profile === "balanced" ? "checked" : ""} ${locked ? "disabled" : ""}><span><b>适度</b><small>${balanced} 个 MPI 子域 · 留出约 25% 余量</small></span></label>
      <label class="cfd-resource-option"><input type="radio" name="cfd-parallel-profile" value="custom" ${config.profile === "custom" ? "checked" : ""} ${locked ? "disabled" : ""}><span><b>自定义</b><small>按逻辑核心数范围选择</small></span></label>
    </div>
    <label class="cfd-resource-custom">自定义 MPI 子域数<input id="cfd-custom-subdomains" type="number" min="1" max="${customMax}" step="1" value="${customValue}" ${config.profile === "custom" && !locked ? "" : "disabled"} aria-describedby="cfd-resource-note"></label>
    <div id="cfd-resource-note" class="cfd-resource-note">检测到 CPU：${cpu.physical_cores ?? "?"} 个物理核 / ${cpu.logical_cores ?? "?"} 个逻辑核；GPU：${escapeHtml(gpuText)}。${escapeHtml(note)}</div>
    <div class="cfd-compute-target" aria-label="气流求解设备">
      <strong>气流求解设备</strong><p>选择用于气流求解的设备；网格仍按上方 MPI 设置生成。</p>
      <div class="cfd-compute-options" role="radiogroup" aria-label="选择气流求解设备">
        <label class="cfd-resource-option"><input type="radio" name="cfd-compute-target" value="cpu" ${computeTarget === "cpu" ? "checked" : ""} ${locked ? "disabled" : ""}><span><b>CPU</b><small>当前稳定求解路径</small></span></label>
        <label class="cfd-resource-option"><input type="radio" name="cfd-compute-target" value="gpu" ${computeTarget === "gpu" ? "checked" : ""} ${locked || !gpuSolver.available ? "disabled" : ""}><span><b>GPU 试验</b><small>${escapeHtml(gpuDevice)}</small></span></label>
      </div>
      <p class="cfd-resource-note">${gpuSolver.available ? "GPU 求解器已通过预检；是否实际使用 GPU 以本次运行记录为准。" : escapeHtml(gpuSolver.reason)}</p>
      <p class="cfd-resource-change" role="alert" ${computeTarget === "gpu" && !gpuSolver.available ? "" : "hidden"}>当前选择的 GPU 求解器不可用，不能开始求解；不会自动改用 CPU。</p>
    </div>
    ${planChanged || failedRun ? `<div class="cfd-resource-change">${planChanged ? `已选择 ${escapeHtml(cfdParallelLabel(config.profile))}（${selectedSubdomains} 个子域），需要重新生成 CFD 算例后才会生效。` : "上次气流求解失败。可按当前模型、端面和最新数值设置重新生成算例，再启动求解；这不会重新裁切模型。"}</div><button id="act-cfd-rebuild" type="button" class="button button-quiet" ${locked ? "disabled" : ""}>${planChanged ? "按当前选择重新生成 CFD 算例" : "重新生成 CFD 算例"}</button>` : ""}
  </div>`;
}

function endplaneContext(data = state.case) {
  if (!data || data.stages?.segment?.status !== "ok" || requiresMaskReview(data.stages)) return null;
  const rawVersion = data.stages.segment.data?.mask_version;
  const version = Number.isInteger(rawVersion) && rawVersion >= 0 ? rawVersion : null;
  const scope = modelScopeParams(data.case_id).params.model_scope;
  return { caseId: data.case_id, scope, version, key: `${data.case_id}|${scope}|${version}` };
}

function validLpsVector(value) {
  return Array.isArray(value) && value.length === 3 && value.every((part) => Number.isFinite(part));
}

function cloneEndplane(plane) {
  return plane ? { point_lps_mm: [...plane.point_lps_mm], normal_lps: [...plane.normal_lps] } : null;
}

function cloneEndplanePair(planes) {
  return { anterior: cloneEndplane(planes.anterior), posterior: cloneEndplane(planes.posterior) };
}

function cloneBilateralDraft(value) {
  return Object.fromEntries(["left", "right"].map(side => [side, {
    plane: cloneEndplane(value?.[side]?.plane),
    seed: validLpsVector(value?.[side]?.seed) ? [...value[side].seed] : null,
  }]));
}

function emptyOpeningMarkup(side) {
  if (window.EndplaneOpeningMarkup?.empty) return window.EndplaneOpeningMarkup.empty(side);
  const safeSide = ["left", "right", "posterior", "anterior"].includes(side) ? side : "left";
  return {
    opening_id: `${safeSide}_nostril_opening`, side: safeSide,
    seed_lps_mm: null, outline_lps_mm: [], status: "empty",
    source: "three_d_model_draft", persisted: false,
  };
}

function openingMarkupSide() {
  const e = state.endplanes;
  return e.format === "oblique_bilateral_local_v3"
    ? e.mode === "posterior" ? "posterior" : e.activeInlet
    : e.mode === "posterior" ? "posterior" : "anterior";
}

function currentOpeningMarkup(side = openingMarkupSide()) {
  const entry = state.endplanes.openingMarkup?.[side];
  return entry || emptyOpeningMarkup(side);
}

function setOpeningMarkup(side, value) {
  const e = state.endplanes;
  if (!e.openingMarkup) e.openingMarkup = {};
  const normalized = window.EndplaneOpeningMarkup?.normalize
    ? window.EndplaneOpeningMarkup.normalize(side, value)
    : { ...emptyOpeningMarkup(side), ...value, side };
  e.openingMarkup[side] = normalized;
  return normalized;
}

function resetOpeningMarkup() {
  const e = state.endplanes;
  e.openingMarkup = Object.fromEntries(["left", "right", "posterior", "anterior"]
    .map((side) => [side, emptyOpeningMarkup(side)]));
}

function renderPortProjection(view = state.endplanes.portSectionView) {
  const canvas = $("endplane-port-projection");
  const status = $("endplane-port-projection-status");
  if (!canvas || !status) return;
  const experiment = $("endplane-opening-markup");
  if (!experiment?.open || experiment.hidden) {
    canvas.hidden = status.hidden = true;
    return;
  }
  const e = state.endplanes;
  const markup = currentOpeningMarkup();
  const boundary = markup.boundary_loop_lps_mm;
  const section = e.viewer?.sectionPlane();
  const image = $("endplane-section-image");
  if (!Array.isArray(boundary) || boundary.length < 4 || !section || !image?.complete || !image.src) {
    canvas.hidden = status.hidden = true;
    return;
  }
  const n = new THREE.Vector3(...section.plane.normal_lps).normalize();
  const up = Math.abs(n.z) < 0.9 ? new THREE.Vector3(0, 0, 1) : new THREE.Vector3(0, 1, 0);
  const u = up.clone().cross(n).normalize(), v = n.clone().cross(u).normalize();
  const origin = new THREE.Vector3(...section.plane.point_lps_mm);
  const size = e.sectionTargets?.data?.size || 384;
  const currentView = view || { x: 0, y: 0, span: size };
  canvas.width = canvas.height = size;
  canvas.hidden = status.hidden = false;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, size, size);
  ctx.beginPath();
  let maxDistance = 0;
  for (let i = 0; i < boundary.length; i += 1) {
    const offset = new THREE.Vector3(...boundary[i]).sub(origin);
    maxDistance = Math.max(maxDistance, Math.abs(offset.dot(n)));
    const px = offset.dot(u) * size / section.span_mm + (size - 1) / 2;
    const py = -offset.dot(v) * size / section.span_mm + (size - 1) / 2;
    const x = (px - currentView.x) * size / currentView.span;
    const y = (py - currentView.y) * size / currentView.span;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.closePath();
  ctx.strokeStyle = "#ffdc84";
  ctx.lineWidth = 2.5;
  ctx.setLineDash([5, 4]);
  ctx.stroke();
  status.textContent = markup.opening_id + " · 三维面选区边界在当前 CT 视角的投影（非 CT 分割或实际裁切）；距当前切面最远 " + maxDistance.toFixed(1) + " mm";
  if (maxDistance > 1) status.textContent += " · 边界不在此切面，不能据此确认入口";
  status.classList.toggle("is-warning", maxDistance > 1);
}

function renderOpeningMarkup() {
  const panel = $("endplane-opening-markup");
  if (!panel) return;
  const e = state.endplanes;
  const local = ["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(e.format);
  panel.hidden = !local;
  if (!local) {
    panel.open = false;
    e.viewer?.showOpeningMarkup(null);
    renderPortProjection();
    return;
  }
  const side = openingMarkupSide();
  const markup = currentOpeningMarkup(side);
  const summary = window.EndplaneOpeningMarkup?.summary
    ? window.EndplaneOpeningMarkup.summary(markup)
    : { id: markup.opening_id, boundary: markup.outline_lps_mm?.length ? `${markup.outline_lps_mm.length} 个三维点` : "未生成", status: markup.status };
  const seedButton = $("endplane-markup-seed");
  const outlineButton = $("endplane-markup-outline");
  const clearButton = $("endplane-markup-clear");
  const busy = e.saving || e.selectionStatus !== "ready";
  seedButton.disabled = busy;
  outlineButton.disabled = busy || !Number.isInteger(markup.seed_face_id);
  clearButton.disabled = busy || (!markup.seed_lps_mm && !markup.selected_face_ids?.length);
  seedButton.classList.toggle("is-on", e.placement === "opening-seed");
  outlineButton.classList.toggle("is-on", e.placement === "opening-outline");
  seedButton.setAttribute("aria-pressed", String(e.placement === "opening-seed"));
  outlineButton.setAttribute("aria-pressed", String(e.placement === "opening-outline"));
  $("endplane-markup-id").textContent = summary.id;
  $("endplane-markup-faces").textContent = markup.selected_face_ids?.length
    ? String(markup.selected_face_ids.length) + " 个 STL 原始三角面" : "未生成";
  $("endplane-markup-boundary").textContent = summary.boundary;
  $("endplane-markup-area").textContent = Number.isFinite(markup.area_mm2)
    ? markup.area_mm2.toFixed(1) + " mm²" : "—";
  $("endplane-markup-status").textContent = `${summary.status} · ${side === "posterior" ? "鼻咽端" : side === "left" ? "左鼻孔" : side === "right" ? "右鼻孔" : "鼻孔端"}`;
  const issue = markup.quality_errors?.[0]?.message || markup.warnings?.[0]?.message;
  if (issue) $("endplane-markup-status").textContent += " · " + issue;
  $("endplane-markup-status").classList.toggle("is-warning", Boolean(markup.quality_errors?.length));
  e.viewer?.showOpeningMarkup(panel.open ? markup : null);
  e.viewer?.setOpeningMode(panel.open && e.placement === "opening-outline");
  renderPortProjection();
}

function resetAutoCandidateState() {
  const e = state.endplanes;
  e.autoCandidate.controller?.abort();
  e.autoCandidate = { status: "idle", reason: null, requestId: 0, controller: null };
}

function resetFormalPortVisual() {
  const formal = state.endplanes.formalPorts;
  if (!formal) return;
  const visual = formal.visual;
  visual?.controller?.abort();
  visual?.viewer?.dispose();
  if (visual?.ctUrl) URL.revokeObjectURL(visual.ctUrl);
  const image = $("endplane-formal-ct-image");
  if (image) { image.onload = image.onerror = null; image.removeAttribute("src"); }
  const canvas = $("endplane-formal-ct-boundary");
  canvas?.getContext("2d")?.clearRect(0, 0, canvas.width, canvas.height);
  formal.visual = { status: "idle", side: null, error: null, requestId: 0,
    controller: null, viewer: null, ctUrl: null };
}

function resetFormalPorts() {
  resetFormalPortVisual();
  state.endplanes.formalPorts = { status: "idle", data: null, error: null, requestId: 0,
    visual: { status: "idle", side: null, error: null, requestId: 0,
      controller: null, viewer: null, ctUrl: null } };
}

function renderFormalPortVisual() {
  const container = $("endplane-formal-preview");
  const status = $("endplane-formal-preview-status");
  const grid = $("endplane-formal-preview-grid");
  if (!container || !status || !grid) return;
  const e = state.endplanes;
  const visual = e.formalPorts.visual;
  const context = endplaneContext();
  const data = e.formalPorts.data;
  const usable = e.formalPorts.status === "ready" && data?.status === "ready" &&
    context && data.case_id === context.caseId && data.scope === context.scope &&
    data.mask_version === context.version && visual?.contextKey === context.key;
  container.hidden = !usable || !visual || visual.status === "idle";
  if (container.hidden) return;
  grid.hidden = visual.status !== "ready";
  status.classList.toggle("is-error", visual.status === "error");
  const side = { left: "左鼻孔", right: "右鼻孔", posterior: "鼻咽端" }[visual.side] || "端口";
  status.textContent = visual.status === "loading" ? `正在核验并读取${side}的已保存 STL 与 CT…` :
    visual.status === "error" ? `${side}真实视图读取失败：${visual.error}。可重新点击该端口。` :
    `${side} · 已保存网格的真实封盖与同一边界在 CT 的投影。${e.dirty ? "当前编辑草稿尚未保存，图中仍为上次保存结果。" : "这不是未保存草稿。"}`;
  if (visual.status === "ready") visual.viewer?.resize();
}

function renderFormalPorts() {
  const status = $("endplane-formal-ports-status");
  const list = $("endplane-formal-ports-list");
  const refresh = $("endplane-formal-ports-refresh");
  if (!status || !list || !refresh) return;
  const e = state.endplanes;
  const formal = e.formalPorts;
  const context = endplaneContext();
  refresh.disabled = !context || formal.status === "loading";
  list.replaceChildren();
  list.hidden = true;
  renderFormalPortVisual();
  status.classList.toggle("is-error", formal.status === "error");
  if (!context) { status.textContent = "确认当前气道范围后，才能读取正式入口。"; return; }
  if (formal.status === "loading") { status.textContent = "正在读取已保存网格的入口数据…"; return; }
  if (formal.status === "error") {
    status.textContent = "正式入口读取失败：" + (formal.error || "未知错误") + "。可点击刷新状态重试。";
    return;
  }
  const result = formal.data;
  if (result?.status !== "ready") {
    status.textContent = result?.reason || "当前尚无通过几何质检的正式三端入口；实验草稿不算已保存。";
    return;
  }
  if (result.case_id !== context.caseId || result.scope !== context.scope ||
      result.mask_version !== context.version) {
    status.textContent = "正式入口属于其他病例、建模范围或旧掩膜版本，不能用于当前病例。请刷新状态。";
    status.classList.add("is-error");
    return;
  }
  if (typeof result.mesh_hash !== "string" || !result.mesh_hash ||
      result.quality_verdict !== "pass") {
    status.textContent = "正式入口缺少有效网格标识或几何质检结论，不能用于当前病例。";
    status.classList.add("is-error");
    return;
  }
  const labels = { left: "左鼻孔", right: "右鼻孔", posterior: "鼻咽端" };
  const ports = Array.isArray(result.ports) ? result.ports : [];
  if (ports.length !== 3 || new Set(ports.map((port) => port.side)).size !== 3 ||
      ports.some((port) => !labels[port.side] ||
      typeof port.port_id !== "string" || !port.port_id ||
      !Array.isArray(port.cap_face_ids) || !port.cap_face_ids.length)) {
    status.textContent = "正式端口记录不完整，不能当作三端 CFD 入口。";
    status.classList.add("is-error");
    return;
  }
  status.textContent = e.dirty
    ? "下方是上次保存的真实网格端口；当前切面草稿尚未保存，不能把两者混用。"
    : "三处端口来自已保存且通过几何质检的实际封盖网格；CFD 数值质量仍需另行检查。";
  for (const side of ["left", "right", "posterior"]) {
    const port = ports.find((item) => item.side === side);
    const item = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = labels[side];
    const id = document.createElement("code");
    id.textContent = port.port_id;
    const area = document.createElement("span");
    area.textContent = Number.isFinite(Number(port.area_mm2))
      ? Number(port.area_mm2).toFixed(1) + " mm² · " + port.cap_face_ids.length + " 个封盖面"
      : port.cap_face_ids.length + " 个封盖面";
    const inspect = document.createElement("button");
    inspect.type = "button";
    inspect.className = "vt-btn";
    inspect.textContent = formal.visual?.side === side && formal.visual.status === "ready" ? "正在查看真实入口" : "查看真实入口";
    inspect.setAttribute("aria-pressed", String(formal.visual?.side === side && formal.visual.status === "ready"));
    inspect.addEventListener("click", () => loadFormalPortVisual(side));
    item.append(name, id, area, inspect);
    list.append(item);
  }
  list.hidden = false;
}

async function loadFormalPorts(context = endplaneContext()) {
  if (!context) return;
  const e = state.endplanes;
  const formal = e.formalPorts;
  const requestId = ++formal.requestId;
  formal.status = "loading";
  formal.error = null;
  renderFormalPorts();
  try {
    const url = "/api/cases/" + encodeURIComponent(context.caseId) + "/segment/ports/formal?scope=" + encodeURIComponent(context.scope);
    const result = await api(url);
    if (e.formalPorts !== formal || formal.requestId !== requestId || endplaneContext()?.key !== context.key) return;
    if (!result || !["ready", "not_generated"].includes(result.status)) throw new Error("正式端口接口返回未知状态");
    if (formal.data?.mesh_hash !== result.mesh_hash || formal.data?.mask_version !== result.mask_version ||
        formal.data?.case_id !== result.case_id || formal.data?.scope !== result.scope ||
        result.status !== "ready") resetFormalPortVisual();
    formal.data = result;
    formal.status = "ready";
  } catch (error) {
    if (e.formalPorts !== formal || formal.requestId !== requestId || endplaneContext()?.key !== context.key) return;
    formal.data = null;
    formal.status = "error";
    formal.error = error.detail?.reason || error.message || "网络错误";
  }
  renderFormalPorts();
}

function formalPortCapPositions(position, faceIds) {
  if (!position || !Number.isInteger(position.length / 9) || !Array.isArray(faceIds) || !faceIds.length)
    throw new Error("已保存 STL 或端口封盖面索引无效");
  const faceCount = position.length / 9;
  if (faceIds.some(id => !Number.isInteger(id) || id < 0 || id >= faceCount) ||
      new Set(faceIds).size !== faceIds.length) throw new Error("端口封盖面与已保存 STL 不一致");
  const cap = new Float32Array(faceIds.length * 9);
  faceIds.forEach((id, index) => cap.set(position.subarray(id * 9, id * 9 + 9), index * 9));
  return cap;
}

function formalPortProjectionPoints(boundary, point, normal, size = 384, spanMm = 120) {
  const vector = value => Array.isArray(value) && value.length === 3 && value.every(Number.isFinite);
  if (!Array.isArray(boundary) || boundary.length < 3 || !boundary.every(vector) ||
      !vector(point) || !vector(normal) || !Number.isFinite(spanMm) || spanMm <= 0)
    throw new Error("正式端口边界或截面数据不完整");
  const dot = (a, b) => a.reduce((sum, value, i) => sum + value * b[i], 0);
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const unit = a => { const length = Math.hypot(...a); if (length < 1e-8) throw new Error("正式端口法向无效"); return a.map(v => v / length); };
  const n = unit(normal);
  const up = Math.abs(n[2]) < 0.9 ? [0, 0, 1] : [0, 1, 0];
  const u = unit(cross(up, n)), v = cross(n, u);
  const pixels = boundary.map(world => {
    const offset = world.map((coordinate, i) => coordinate - point[i]);
    if (Math.abs(dot(offset, n)) > 0.01) throw new Error("正式封盖边界偏离 CT 截面");
    return [dot(offset, u) * size / spanMm + (size - 1) / 2,
      -dot(offset, v) * size / spanMm + (size - 1) / 2];
  });
  return pixels;
}

function drawFormalPortProjection(canvas, port, size = 384) {
  const points = formalPortProjectionPoints(
    port.boundary_loop_lps_mm, port.plane_point_lps_mm, port.normal_lps, size);
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, size, size);
  ctx.beginPath();
  points.forEach(([x, y], index) => index ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
  ctx.closePath();
  ctx.strokeStyle = port.side === "posterior" ? "#ffb675" : port.side === "left" ? "#69caff" : "#53d7c4";
  ctx.lineWidth = 3;
  ctx.stroke();
}

function createFormalPortViewer(host, geometry, capPositions, side) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d1519);
  const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 10000);
  camera.up.set(0, 0, 1);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  host.replaceChildren(renderer.domElement);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x173036, 1.5));
  const light = new THREE.DirectionalLight(0xffffff, 0.9);
  light.position.set(1.4, 1.6, 1.2);
  scene.add(light);
  const modelMaterial = new THREE.MeshPhongMaterial({ color: 0x669b9a, transparent: true,
    opacity: 0.64, side: THREE.DoubleSide, depthWrite: false });
  const model = new THREE.Mesh(geometry, modelMaterial);
  scene.add(model);
  const capGeometry = new THREE.BufferGeometry();
  capGeometry.setAttribute("position", new THREE.BufferAttribute(capPositions, 3));
  capGeometry.computeVertexNormals();
  const capMaterial = new THREE.MeshBasicMaterial({
    color: side === "posterior" ? 0xffb675 : side === "left" ? 0x69caff : 0x53d7c4,
    side: THREE.DoubleSide, depthTest: false, transparent: true, opacity: 0.94,
  });
  const cap = new THREE.Mesh(capGeometry, capMaterial);
  cap.renderOrder = 2;
  scene.add(cap);
  const bounds = new THREE.Box3().setFromObject(model);
  const center = bounds.getCenter(new THREE.Vector3());
  const radius = Math.max(bounds.getBoundingSphere(new THREE.Sphere()).radius, 1);
  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.target.copy(center);
  camera.position.copy(center).addScaledVector(new THREE.Vector3(1.05, -1.55, -0.55).normalize(), radius * 3.6);
  camera.near = Math.max(0.01, radius / 1000);
  camera.far = radius * 30;
  camera.updateProjectionMatrix();
  const paint = () => renderer.render(scene, camera);
  const resize = () => {
    const width = host.clientWidth || 350, height = host.clientHeight || 300;
    camera.aspect = width / height; camera.updateProjectionMatrix();
    renderer.setSize(width, height); controls.update(); paint();
  };
  controls.addEventListener("change", paint);
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(resize) : null;
  observer?.observe(host);
  window.addEventListener("resize", resize);
  resize();
  return { resize, dispose() {
    observer?.disconnect(); window.removeEventListener("resize", resize);
    controls.dispose(); scene.remove(model, cap);
    geometry.dispose(); capGeometry.dispose(); modelMaterial.dispose(); capMaterial.dispose();
    renderer.dispose(); host.replaceChildren();
  } };
}

async function loadFormalPortVisual(side) {
  const e = state.endplanes;
  const context = endplaneContext();
  const formal = e.formalPorts;
  const saved = formal?.data;
  if (!context || formal.status !== "ready" || saved?.status !== "ready" ||
      !["left", "right", "posterior"].includes(side)) return;
  resetFormalPortVisual();
  const visual = formal.visual;
  const controller = new AbortController();
  visual.status = "loading";
  visual.side = side;
  visual.contextKey = context.key;
  visual.controller = controller;
  const requestId = ++visual.requestId;
  const isCurrent = () => e.formalPorts === formal && formal.visual === visual &&
    visual.requestId === requestId && endplaneContext()?.key === context.key &&
    !controller.signal.aborted;
  let geometry = null;
  renderFormalPorts();
  try {
    const base = "/api/cases/" + encodeURIComponent(context.caseId);
    const manifest = await api(base + "/segment/ports/formal?scope=" + encodeURIComponent(context.scope),
      { signal: controller.signal });
    if (!isCurrent()) return;
    if (manifest?.status !== "ready" || manifest.case_id !== context.caseId ||
        manifest.scope !== context.scope || manifest.mask_version !== context.version ||
        manifest.quality_verdict !== "pass" ||
        !/^[a-f0-9]{64}$/i.test(manifest.mesh_hash || "") ||
        manifest.mesh_hash !== saved.mesh_hash || manifest.mask_version !== saved.mask_version)
      throw new Error("正式端口或网格版本已变更，请刷新状态后重新查看");
    const port = manifest.ports?.find(item => item.side === side);
    const savedPort = saved.ports?.find(item => item.side === side);
    if (!port || !savedPort || port.port_id !== savedPort.port_id ||
        !Array.isArray(port.cap_face_ids) || !port.cap_face_ids.length)
      throw new Error("正式端口标识或封盖面不一致，请刷新状态");

    const modelResponse = await fetch(base + "/model", { signal: controller.signal });
    if (!modelResponse.ok) {
      const payload = await modelResponse.json().catch(() => null);
      throw new Error(payload?.detail?.reason || `已保存 STL 读取失败（HTTP ${modelResponse.status}）`);
    }
    const buffer = await modelResponse.arrayBuffer();
    if (!isCurrent()) return;
    if (typeof crypto === "undefined" || !crypto.subtle?.digest)
      throw new Error("此浏览器无法核验 STL 网格指纹");
    const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", buffer));
    if (!isCurrent()) return;
    const hash = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
    if (hash !== manifest.mesh_hash.toLowerCase())
      throw new Error("已保存 STL 与正式端口的网格指纹不一致，不能展示封盖");
    geometry = new THREE.STLLoader().parse(buffer);
    const position = geometry.getAttribute("position")?.array;
    const capPositions = formalPortCapPositions(position, port.cap_face_ids);
    // The boundary is projected with the same LPS basis as the server CT reslice.
    formalPortProjectionPoints(port.boundary_loop_lps_mm,
      port.plane_point_lps_mm, port.normal_lps);

    const sectionResponse = await fetch(base + "/segment/endplanes/section", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: context.scope, mask_version: context.version,
        plane: { point_lps_mm: port.plane_point_lps_mm, normal_lps: port.normal_lps },
        span_mm: 120 }), signal: controller.signal,
    });
    if (!sectionResponse.ok) {
      const payload = await sectionResponse.json().catch(() => null);
      throw new Error(payload?.detail?.reason || `CT 截面读取失败（HTTP ${sectionResponse.status}）`);
    }
    if (sectionResponse.headers.get("X-Mask-Version") !== String(context.version) ||
        sectionResponse.headers.get("X-Model-Scope") !== context.scope)
      throw new Error("CT 截面与正式端口的掩膜版本或范围不一致");
    const blob = await sectionResponse.blob();
    if (!isCurrent()) return;
    const image = $("endplane-formal-ct-image");
    visual.ctUrl = URL.createObjectURL(blob);
    await new Promise((resolve, reject) => {
      const cleanup = () => {
        image.onload = image.onerror = null;
        controller.signal.removeEventListener("abort", onAbort);
      };
      const onAbort = () => { cleanup(); reject(Object.assign(new Error("已取消"), { name: "AbortError" })); };
      image.onload = () => { cleanup(); resolve(); };
      image.onerror = () => { cleanup(); reject(new Error("正式端口 CT 图像无法显示")); };
      controller.signal.addEventListener("abort", onAbort, { once: true });
      image.src = visual.ctUrl;
    });
    if (!isCurrent()) return;
    drawFormalPortProjection($("endplane-formal-ct-boundary"), port);
    visual.viewer = createFormalPortViewer($("endplane-formal-model"), geometry, capPositions, side);
    geometry = null; // The viewer owns this geometry from here onward.
    visual.status = "ready";
  } catch (error) {
    if (!isCurrent() || error.name === "AbortError") return;
    visual.status = "error";
    visual.error = error.detail?.reason || error.message || "读取失败";
  } finally {
    geometry?.dispose();
    if (isCurrent()) {
      visual.controller = null;
      renderFormalPorts();
    }
  }
}

function syncActiveBilateralDraft() {
  const e = state.endplanes;
  if (e.format !== "oblique_bilateral_local_v3") return;
  if (e.posteriorSeedPlane && endplaneSideChanged(e.planes, { posterior: e.posteriorSeedPlane }, "posterior")) e.posteriorSeed = null;
  e.posteriorSeedPlane = cloneEndplane(e.planes.posterior);
  const changed = endplaneSideChanged({ anterior: e.planes.anterior },
    { anterior: e.bilateral[e.activeInlet].plane }, "anterior");
  if (changed) e.anteriorSeed = null;
  if (e.sharedInletPlane) {
    const other = e.activeInlet === "left" ? "right" : "left";
    e.bilateral[other].plane = cloneEndplane(e.planes.anterior);
    if (changed) e.bilateral[other].seed = null;
  }
  e.bilateral[e.activeInlet] = {
    plane: cloneEndplane(e.planes.anterior),
    seed: validLpsVector(e.anteriorSeed) ? [...e.anteriorSeed] : null,
  };
}

function currentEndplaneSeed() {
  const e = state.endplanes;
  return e.format === "oblique_bilateral_local_v3" && e.mode === "posterior"
    ? e.posteriorSeed : e.anteriorSeed;
}

function setCurrentEndplaneSeed(value) {
  const e = state.endplanes;
  const seed = validLpsVector(value) ? [...value] : null;
  if (e.format === "oblique_bilateral_local_v3" && e.mode === "posterior") e.posteriorSeed = seed;
  else e.anteriorSeed = seed;
  syncActiveBilateralDraft();
}

function normalizedEndplane(plane) {
  if (!plane || !validLpsVector(plane.point_lps_mm) || !validLpsVector(plane.normal_lps)) return null;
  const length = Math.hypot(...plane.normal_lps);
  if (length < 1e-8) return null;
  return { point_lps_mm: [...plane.point_lps_mm], normal_lps: plane.normal_lps.map((value) => value / length) };
}

function endplaneFlowDirection(side, normal) {
  // The saved normal points toward the anatomical outside that gets clipped.
  // Inlet air enters from the nostril, so its visible arrow is opposite that
  // clipping normal; the nasopharyngeal outlet points toward the lung side.
  const flowSign = side === "anterior" ? -1 : 1;
  return normal.map((value) => value === 0 ? 0 : value * flowSign);
}

function planeFromCtIndex(profile, side, index) {
  if (!Number.isInteger(index) || index < 0 || index >= profile.count) return null;
  const direction = profile.ap_direction_lps;
  const origin = profile.ap_origin_lps_mm;
  const step = profile.ap_spacing_mm * index;
  const outward = side === "anterior" ? profile.anterior_sign : -profile.anterior_sign;
  return {
    point_lps_mm: origin.map((coordinate, axis) => coordinate + step * direction[axis]),
    normal_lps: direction.map((coordinate) => coordinate * outward),
  };
}

function ctIndexFromPlane(profile, plane) {
  if (!plane) return null;
  const distance = plane.point_lps_mm.reduce((sum, coordinate, axis) =>
    sum + (coordinate - profile.ap_origin_lps_mm[axis]) * profile.ap_direction_lps[axis], 0);
  const index = Math.round(distance / profile.ap_spacing_mm);
  return index >= 0 && index < profile.count ? index : null;
}

function isAxialEndplane(profile, side, plane) {
  if (!plane) return false;
  const expected = planeFromCtIndex(profile, side, ctIndexFromPlane(profile, plane));
  if (!expected) return false;
  const normalDot = plane.normal_lps.reduce((sum, value, axis) => sum + value * expected.normal_lps[axis], 0);
  const planeOffset = plane.point_lps_mm.reduce((sum, value, axis) =>
    sum + (value - expected.point_lps_mm[axis]) * profile.ap_direction_lps[axis], 0);
  return normalDot > 0.999 && Math.abs(planeOffset) < Math.max(0.01, profile.ap_spacing_mm * 0.02);
}

function endplanePairChanged(a, b) {
  return ["anterior", "posterior"].some((side) => {
    if (!a[side] || !b[side]) return a[side] !== b[side];
    return ["point_lps_mm", "normal_lps"].some((field) =>
      a[side][field].some((value, axis) => Math.abs(value - b[side][field][axis]) > 1e-5));
  });
}

function endplaneSideChanged(planes, baselinePlanes, side) {
  return endplanePairChanged(
    { anterior: planes[side], posterior: null },
    { anterior: baselinePlanes[side], posterior: null },
  );
}

function sameEndplaneSeed(a, b) {
  if (!a || !b) return a === b;
  return validLpsVector(a) && validLpsVector(b) &&
    a.every((value, axis) => Math.abs(value - b[axis]) <= 1e-5);
}

function updateEndplaneDirty() {
  const e = state.endplanes;
  syncActiveBilateralDraft();
  if (e.format === "oblique_bilateral_local_v3" || e.baselineFormat === "oblique_bilateral_local_v3") {
    e.dirtySides = {
      anterior: e.format !== e.baselineFormat || ["left", "right"].some(side =>
        endplaneSideChanged({ anterior: e.bilateral[side].plane },
          { anterior: e.baselineBilateral[side].plane }, "anterior") ||
        !sameEndplaneSeed(e.bilateral[side].seed, e.baselineBilateral[side].seed)),
      posterior: endplaneSideChanged(e.planes, e.baselinePlanes, "posterior") ||
        !sameEndplaneSeed(e.posteriorSeed, e.baselinePosteriorSeed),
    };
    e.dirty = e.dirtySides.anterior || e.dirtySides.posterior;
    return;
  }
  e.dirtySides = {
    anterior: endplaneSideChanged(e.planes, e.baselinePlanes, "anterior") ||
      e.format !== e.baselineFormat || !sameEndplaneSeed(e.anteriorSeed, e.baselineAnteriorSeed),
    posterior: endplaneSideChanged(e.planes, e.baselinePlanes, "posterior"),
  };
  e.dirty = e.dirtySides.anterior || e.dirtySides.posterior;
}

function endplaneBody(context = endplaneContext()) {
  const e = state.endplanes;
  if (!context || !e.planes.posterior) return null;
  if (e.format === "oblique_bilateral_local_v3") {
    syncActiveBilateralDraft();
    if (!e.bilateral.left.plane || !e.bilateral.right.plane) return null;
    return {
      scope: context.scope, mask_version: context.version, format: e.format,
      left_inlet_plane: cloneEndplane(e.bilateral.left.plane),
      right_inlet_plane: cloneEndplane(e.bilateral.right.plane),
      posterior_plane: cloneEndplane(e.planes.posterior),
      left_inlet_seed_lps_mm: validLpsVector(e.bilateral.left.seed) ? [...e.bilateral.left.seed] : null,
      right_inlet_seed_lps_mm: validLpsVector(e.bilateral.right.seed) ? [...e.bilateral.right.seed] : null,
      posterior_seed_lps_mm: validLpsVector(e.posteriorSeed) ? [...e.posteriorSeed] : null,
    };
  }
  if (!e.planes.anterior) return null;
  const body = {
    scope: context.scope,
    mask_version: context.version,
    anterior_plane: cloneEndplane(e.planes.anterior),
    posterior_plane: cloneEndplane(e.planes.posterior),
  };
  if (e.format === "oblique_local_inlet_v2") {
    body.format = e.format;
    body.anterior_seed_lps_mm = validLpsVector(e.anteriorSeed) ? [...e.anteriorSeed] : null;
  }
  return body;
}

function endplaneSignature() {
  const body = endplaneBody();
  return body ? JSON.stringify(body) : null;
}

function resetSinglePortSuggestionState() {
  const e = state.endplanes;
  const previous = e.singlePortSuggestion;
  previous?.controller?.abort();
  e.singlePortSuggestion = { status: "idle", reason: null, requestId: (previous?.requestId || 0) + 1,
    controller: null, result: null, signature: null, contextKey: null, side: null, originalBody: null };
}

function singlePortSuggestionEligible() {
  const e = state.endplanes, context = endplaneContext();
  const body = endplaneBody(context);
  return Boolean(context && e.open && e.key === context.key && e.status === "ready" &&
    e.profile?.can_save !== false && !e.saving && e.selectionStatus === "ready" &&
    e.format === "oblique_bilateral_local_v3" && !e.sharedInletPlane &&
    e.mode === "anterior" && ["left", "right"].includes(e.activeInlet) && body &&
    validLpsVector(body.left_inlet_seed_lps_mm) && validLpsVector(body.right_inlet_seed_lps_mm) &&
    validLpsVector(body.posterior_seed_lps_mm) && endplaneValidation() === null);
}

function sameSuggestedPlane(a, b) {
  const first = normalizedEndplane(a), second = normalizedEndplane(b);
  return Boolean(first && second &&
    first.point_lps_mm.every((value, axis) => Math.abs(value - second.point_lps_mm[axis]) <= 1e-4) &&
    first.normal_lps.every((value, axis) => Math.abs(value - second.normal_lps[axis]) <= 1e-4));
}

function validateSinglePortSuggestion(result, original, side) {
  if (result?.status !== "proposed" || result.side !== side ||
      result.scope !== original.scope || result.mask_version !== original.mask_version ||
      result.plan?.format !== original.format || result.plan?.scope !== original.scope ||
      result.plan?.mask_version !== original.mask_version ||
      result.mesh_preflight?.closed_caps !== true ||
      ["left_inlet_loops", "right_inlet_loops", "outlet_loops"].some(key => result.mesh_preflight[key] !== 1)) {
    throw new Error("建议未通过当前病例与三端网格单环校验，原草稿保持不变");
  }
  const other = side === "left" ? "right" : "left";
  const movedKey = `${side}_inlet_plane`;
  const otherKey = `${other}_inlet_plane`;
  const movedSeedKey = `${side}_inlet_seed_lps_mm`;
  const otherSeedKey = `${other}_inlet_seed_lps_mm`;
  const oldPlane = normalizedEndplane(original[movedKey]);
  const newPlane = normalizedEndplane(result.plan[movedKey]);
  const shift = result.shift_mm?.[side];
  const delta = oldPlane && newPlane ? newPlane.point_lps_mm.map((value, axis) => value - oldPlane.point_lps_mm[axis]) : null;
  const projected = delta?.reduce((sum, value, axis) => sum + value * oldPlane.normal_lps[axis], 0);
  const offAxis = delta && Number.isFinite(projected) ? Math.hypot(...delta.map((value, axis) =>
    value - projected * oldPlane.normal_lps[axis])) : Infinity;
  if (!oldPlane || !newPlane || !Number.isFinite(shift) || shift <= 0 || shift > 2 ||
      result.shift_mm[other] !== 0 || result.shift_mm.outlet !== 0 ||
      !validLpsVector(result.plan[movedSeedKey]) ||
      !sameSuggestedPlane(result.plan[otherKey], original[otherKey]) ||
      !sameSuggestedPlane(result.plan.posterior_plane, original.posterior_plane) ||
      !sameEndplaneSeed(result.plan[otherSeedKey], original[otherSeedKey]) ||
      !sameEndplaneSeed(result.plan.posterior_seed_lps_mm, original.posterior_seed_lps_mm) ||
      oldPlane.normal_lps.some((value, axis) => Math.abs(value - newPlane.normal_lps[axis]) > 1e-4) ||
      Math.abs(projected - shift) > 1e-3 || offAxis > 1e-3) {
    throw new Error("建议改变了角度、另一处开口或位移范围，原草稿保持不变");
  }
  return shift;
}

function clearEndplanePreview() {
  const e = state.endplanes;
  resetSinglePortSuggestionState();
  const confirmation = $("endplane-bilateral-confirm");
  if (confirmation) confirmation.checked = false;
  if (e.previewTimer) clearTimeout(e.previewTimer);
  e.previewTimer = null;
  e.previewRequestId += 1;
  e.previewAbort?.abort();
  e.previewAbort = null;
  e.previewStatus = "idle";
  e.previewData = null;
  e.previewError = null;
  e.previewSignature = null;
  e.actualPreviewRequestId += 1;
  e.actualPreviewAbort?.abort();
  e.actualPreviewAbort = null;
  e.actualPreviewStatus = "idle";
  e.actualPreviewError = null;
  e.actualPreviewSignature = null;
  e.viewer?.clearActualPreview();
}

function bilateralRetentionMetrics(preview) {
  const keys = ["retained_voxels", "left_deleted_voxels", "right_deleted_voxels", "posterior_deleted_voxels"];
  if (!preview || keys.some((key) => !Number.isSafeInteger(preview[key]) || preview[key] < 0)) return null;
  const deleted = preview.left_deleted_voxels + preview.right_deleted_voxels + preview.posterior_deleted_voxels;
  const working = preview.retained_voxels + deleted;
  if (!working || !Number.isSafeInteger(working) ||
      (preview.source_voxels !== undefined &&
        (!Number.isSafeInteger(preview.source_voxels) || preview.source_voxels < working))) return null;
  const percent = (value) => (100 * value / working).toFixed(1);
  return { retained: percent(preview.retained_voxels), deleted: percent(deleted),
    left: percent(preview.left_deleted_voxels), right: percent(preview.right_deleted_voxels),
    posterior: percent(preview.posterior_deleted_voxels),
    excluded: preview.source_voxels === undefined ? 0 : preview.source_voxels - working };
}

function scheduleEndplanePreview() {
  const e = state.endplanes;
  if (!e.profile || e.profile.can_save === false || !endplaneBody()) return;
  const localError = endplaneValidation();
  if (localError) {
    e.previewStatus = "blocked";
    e.previewError = localError;
    renderEndplaneUi();
    return;
  }
  e.previewStatus = "queued";
  e.previewTimer = setTimeout(runEndplanePreview, 450);
  renderEndplaneUi();
}

async function runEndplanePreview() {
  const e = state.endplanes;
  e.previewTimer = null;
  const body = endplaneBody();
  const signature = endplaneSignature();
  if (!body || !signature) return;
  const requestId = ++e.previewRequestId;
  const controller = new AbortController();
  e.previewAbort = controller;
  e.previewStatus = "loading";
  renderEndplaneUi();
  try {
    const result = await api(`/api/cases/${encodeURIComponent(state.caseId)}/segment/endplanes/preview`, {
      method: "POST", body, signal: controller.signal,
    });
    if (e.previewRequestId !== requestId || endplaneSignature() !== signature) return;
    const bilateral = body.format === "oblique_bilateral_local_v3";
    if (result.scope !== body.scope || result.mask_version !== body.mask_version ||
        result.format !== (body.format || "oblique_planes_v1") ||
        (body.format === "oblique_local_inlet_v2" || bilateral ? result.can_preview_mesh !== true : result.can_save !== true) ||
        !Number.isFinite(result.posterior_area_mm2) ||
        (bilateral
          ? !Number.isFinite(result.left_inlet_area_mm2) || !Number.isFinite(result.right_inlet_area_mm2)
          : !Number.isFinite(result.anterior_area_mm2))) {
      throw new Error("端面预检没有返回可核对的开口面积，请重新检查位置");
    }
    if (body.format === "oblique_local_inlet_v2" &&
        (!Number.isInteger(result.anterior_deleted_voxels) || result.anterior_deleted_voxels <= 0 ||
         result.anterior_opening_components !== 1 || result.unselected_region_changed_voxels !== 0)) {
      throw new Error("局部鼻孔裁切缺少可核对的单支段/未选区域统计，不能保存");
    }
    if (bilateral && !bilateralRetentionMetrics(result)) {
      throw new Error("三端预检缺少可核对的删除量或保留量，不能保存");
    }
    e.previewStatus = "ok";
    e.previewData = result;
    e.previewError = null;
    e.previewSignature = signature;
    if (bilateral) void loadActualEndplanePreview();
  } catch (error) {
    if (error.name === "AbortError" || e.previewRequestId !== requestId || endplaneSignature() !== signature) return;
    e.previewStatus = "blocked";
    e.previewData = null;
    e.previewError = [error.detail?.reason || error.message || "几何预检失败", ...(error.detail?.remediation || []).slice(0, 2)].join("；");
    e.previewSignature = null;
    e.actualPreviewRequestId += 1;
    e.actualPreviewAbort?.abort();
    e.actualPreviewAbort = null;
    e.actualPreviewStatus = "idle";
    e.actualPreviewSignature = null;
    e.viewer?.clearActualPreview();
  } finally {
    if (e.previewRequestId === requestId) {
      e.previewAbort = null;
      renderEndplaneUi();
    }
  }
}

function actualEndplanePreviewHttpError(response, payload) {
  const detail = payload?.detail;
  const reason = typeof detail === "object" && detail !== null ? detail.reason :
    typeof detail === "string" ? detail : null;
  if (typeof reason === "string" && reason.trim()) {
    const next = Array.isArray(detail?.remediation) ? detail.remediation.find(item => typeof item === "string" && item.trim()) : null;
    return reason.trim() + (next ? `；${next.trim()}` : "");
  }
  const requestId = response.headers?.get("X-Windcatcher-Request-Id");
  const suffix = requestId ? `；请求编号 ${requestId}` : "";
  if (response.status === 401 || response.status === 403) {
    return `实际裁切预览无访问权限（HTTP ${response.status}）。请在新标签登录，当前页面草稿不要刷新${suffix}`;
  }
  if (response.status >= 500) {
    return `实际裁切服务未能返回网格（HTTP ${response.status}）。当前选区仍保留，可重试${suffix}`;
  }
  return `实际裁切预览请求失败（HTTP ${response.status}）。当前选区仍保留${suffix}`;
}

async function loadActualEndplanePreview() {
  const e = state.endplanes;
  const context = endplaneContext();
  const body = endplaneBody(context);
  const signature = endplaneSignature();
  if (!context || !["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(body?.format) || e.saving ||
      e.selectionStatus !== "ready" || e.previewStatus !== "ok" ||
      e.previewSignature !== signature || e.previewData?.can_preview_mesh !== true) return;
  const requestId = ++e.actualPreviewRequestId;
  e.actualPreviewAbort?.abort();
  const controller = new AbortController();
  e.actualPreviewAbort = controller;
  e.actualPreviewStatus = "loading";
  e.actualPreviewError = null;
  renderEndplaneUi();
  try {
    let response;
    try {
      response = await fetch("/api/cases/" + encodeURIComponent(context.caseId) +
        "/segment/endplanes/preview-model", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body), signal: controller.signal,
      });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new Error("实际裁切服务连接中断。当前选区仍保留，请重试");
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(actualEndplanePreviewHttpError(response, payload));
    }
    const returnedScope = response.headers.get("X-Model-Scope");
    const returnedVersion = response.headers.get("X-Mask-Version");
    if ((returnedScope && returnedScope !== context.scope) ||
        (returnedVersion && Number(returnedVersion) !== context.version)) {
      throw new Error("实际裁切网格与当前掩膜版本不一致");
    }
    let geometry;
    try {
      geometry = new THREE.STLLoader().parse(await response.arrayBuffer());
    } catch (_) {
      throw new Error("服务端返回的实际裁切网格无法解析。当前选区仍保留，请重试");
    }
    if (!geometry.getAttribute("position")?.count) {
      geometry.dispose();
      throw new Error("实际裁切网格为空，不能用于核对");
    }
    if (e.actualPreviewRequestId !== requestId || state.caseId !== context.caseId ||
        endplaneContext()?.key !== context.key || endplaneSignature() !== signature) {
      geometry.dispose();
      return;
    }
    e.viewer?.setActualPreviewGeometry(geometry);
    e.actualPreviewStatus = "ok";
    e.actualPreviewSignature = signature;
  } catch (error) {
    if (error.name === "AbortError" || e.actualPreviewRequestId !== requestId ||
        endplaneSignature() !== signature) return;
    e.actualPreviewStatus = "error";
    e.actualPreviewError = error.message || "实际裁切预览失败，当前选区仍保留";
    e.actualPreviewSignature = null;
    e.viewer?.clearActualPreview();
  } finally {
    if (e.actualPreviewRequestId === requestId) {
      e.actualPreviewAbort = null;
      renderEndplaneUi();
    }
  }
}

function updateEndplaneDraft(schedulePreview = true) {
  const e = state.endplanes;
  updateEndplaneDirty();
  if (e.profile) {
    for (const side of ["anterior", "posterior"]) e.values[side] = ctIndexFromPlane(e.profile, e.planes[side]);
  }
  e.error = null;
  e.pairError = null;
  clearEndplanePreview();
  if (schedulePreview && e.planes.anterior && e.planes.posterior) scheduleEndplanePreview();
  renderEndplaneUi();
}

function endplaneSaved() {
  const context = endplaneContext();
  const e = state.endplanes;
  const saved = e.profile?.saved;
  const validNew = saved?.format === "oblique_planes_v1" &&
    normalizedEndplane(saved.anterior_plane) && normalizedEndplane(saved.posterior_plane);
  const validLocal = saved?.format === "oblique_local_inlet_v2" &&
    normalizedEndplane(saved.anterior_plane) && normalizedEndplane(saved.posterior_plane) &&
    validLpsVector(saved.anterior_seed_lps_mm);
  const validBilateral = saved?.format === "oblique_bilateral_local_v3" &&
    normalizedEndplane(saved.left_inlet_plane) && normalizedEndplane(saved.right_inlet_plane) &&
    normalizedEndplane(saved.posterior_plane) &&
    validLpsVector(saved.left_inlet_seed_lps_mm) &&
    validLpsVector(saved.right_inlet_seed_lps_mm) && validLpsVector(saved.posterior_seed_lps_mm);
  const validOld = Number.isInteger(saved?.anterior_index) && Number.isInteger(saved?.posterior_index);
  return Boolean(context && Number.isInteger(context.version) && e.key === context.key && e.status === "ready" &&
    e.profile?.can_save !== false && !e.stale && saved && saved.complete !== false && !saved.validation_error &&
    !(saved.format === "oblique_planes_v1" && e.profile?.supported_formats?.includes("oblique_local_inlet_v2")) &&
    !(saved.format === "oblique_local_inlet_v2" && e.profile?.supported_formats?.includes("oblique_bilateral_local_v3")) &&
    (saved.format === e.format || (!saved.format && e.format === "oblique_planes_v1")) &&
    (validNew || validLocal || validBilateral || validOld));
}

function guardEndplaneDraft() {
  const e = state.endplanes;
  if (e.saving) {
    toast("端面位置正在保存，请等待完成", true);
    return false;
  }
  if (!e.dirty) return true;
  if (!window.confirm(e.format === "oblique_bilateral_local_v3"
    ? "左右鼻孔或鼻咽端面尚未保存。确定放弃这次选择吗？"
    : "鼻孔侧或鼻咽侧层面尚未保存。确定放弃这次选择吗？")) return false;
  e.values = { ...e.baseline };
  e.planes = cloneEndplanePair(e.baselinePlanes);
  e.format = e.baselineFormat;
  e.bilateral = cloneBilateralDraft(e.baselineBilateral);
  e.sharedInletPlane = e.baselineFormat === "oblique_bilateral_local_v3" &&
    Boolean(e.bilateral.left.plane && e.bilateral.right.plane) &&
    coincidentEndplanes(e.bilateral.left.plane, e.bilateral.right.plane);
  e.posteriorSeedPlane = null;
  e.posteriorSeed = e.baselinePosteriorSeed ? [...e.baselinePosteriorSeed] : null;
  e.anteriorSeed = e.baselineAnteriorSeed ? [...e.baselineAnteriorSeed] : null;
  resetOpeningMarkup();
  e.editHistory = [];
  e.history = { anterior: [], posterior: [] };
  updateEndplaneDirty();
  e.error = null;
  e.pairError = e.profile?.saved?.validation_error?.reason || null;
  clearEndplanePreview();
  e.viewer?.setFormat(e.format);
  e.viewer?.setPlanes(e.planes, e.mode);
  renderEndplaneUi();
  return true;
}

function resetEndplaneState() {
  const e = state.endplanes;
  resetAutoCandidateState();
  clearEndplaneSection();
  e.placement = "point";
  e.pickedPoints = [];
  e.openingStroke = null;
  resetFormalPorts();
  e.showGizmo = false;
  e.adjustments = { anterior: null, posterior: null };
  e.history = { anterior: [], posterior: [] };
  e.editHistory = [];
  clearEndplanePreview();
  e.requestId += 1;
  e.selectionRequestId += 1;
  e.selectionAbort?.abort();
  e.selectionAbort = null;
  e.viewer?.clear();
  e.viewer?.pause();
  e.open = false;
  e.key = null;
  e.profile = null;
  e.status = "idle";
  e.error = null;
  e.pairError = null;
  e.stale = false;
  e.values = { anterior: null, posterior: null };
  e.baseline = { anterior: null, posterior: null };
  e.dirtySides = { anterior: false, posterior: false };
  e.planes = { anterior: null, posterior: null };
  e.baselinePlanes = { anterior: null, posterior: null };
  e.format = "oblique_planes_v1";
  e.baselineFormat = "oblique_planes_v1";
  e.anteriorSeed = null;
  e.baselineAnteriorSeed = null;
  e.activeInlet = "left";
  e.sharedInletPlane = false;
  e.posteriorSeedPlane = null;
  e.bilateral = cloneBilateralDraft(null);
  e.baselineBilateral = cloneBilateralDraft(null);
  e.posteriorSeed = null;
  e.baselinePosteriorSeed = null;
  resetOpeningMarkup();
  e.mode = "anterior";
  e.tool = "translate";
  e.selectionKey = null;
  e.selectionStatus = "idle";
  e.selectionError = null;
  e.dirty = false;
  e.saving = false;
  renderEndplaneUi();
}

function syncEndplaneContext(data = state.case) {
  const context = endplaneContext(data);
  const e = state.endplanes;
  if (!context) {
    if (e.key) {
      clearEndplanePreview();
      e.requestId += 1;
      e.key = null;
      e.profile = null;
      e.status = "idle";
      resetAutoCandidateState();
      resetFormalPorts();
      e.values = { anterior: null, posterior: null };
      e.baseline = { anterior: null, posterior: null };
      e.dirtySides = { anterior: false, posterior: false };
      e.planes = { anterior: null, posterior: null };
      e.baselinePlanes = { anterior: null, posterior: null };
      e.format = "oblique_planes_v1";
      e.baselineFormat = "oblique_planes_v1";
      e.anteriorSeed = null;
      e.baselineAnteriorSeed = null;
      e.activeInlet = "left";
      e.bilateral = cloneBilateralDraft(null);
      e.baselineBilateral = cloneBilateralDraft(null);
      e.posteriorSeed = null;
      e.baselinePosteriorSeed = null;
      resetOpeningMarkup();
      e.dirty = false;
      e.selectionRequestId += 1;
      e.selectionAbort?.abort();
      e.selectionAbort = null;
      e.selectionKey = null;
      e.selectionStatus = "idle";
      e.viewer?.clear();
    }
    renderEndplaneUi();
    return;
  }
  if (e.key !== context.key) loadEndplaneProfile(context);
  else renderEndplaneUi();
}

async function loadEndplaneProfile(context = endplaneContext(), force = false) {
  if (!context) return;
  const e = state.endplanes;
  if (!force && e.key === context.key && ["loading", "ready"].includes(e.status)) return;
  clearEndplaneSection();
  e.adjustments = { anterior: null, posterior: null };
  e.history = { anterior: [], posterior: [] };
  e.editHistory = [];
  e.placement = "point";
  e.pickedPoints = [];
  e.openingStroke = null;
  resetAutoCandidateState();
  resetFormalPorts();
  e.showGizmo = false;
  clearEndplanePreview();
  const requestId = ++e.requestId;
  e.key = context.key;
  e.profile = null;
  e.status = "loading";
  e.error = null;
  e.pairError = null;
  e.stale = false;
  e.values = { anterior: null, posterior: null };
  e.baseline = { anterior: null, posterior: null };
  e.dirtySides = { anterior: false, posterior: false };
  e.planes = { anterior: null, posterior: null };
  e.baselinePlanes = { anterior: null, posterior: null };
  e.format = "oblique_planes_v1";
  e.baselineFormat = "oblique_planes_v1";
  e.anteriorSeed = null;
  e.baselineAnteriorSeed = null;
  e.activeInlet = "left";
  e.sharedInletPlane = false;
  e.posteriorSeedPlane = null;
  e.bilateral = cloneBilateralDraft(null);
  e.baselineBilateral = cloneBilateralDraft(null);
  e.posteriorSeed = null;
  e.baselinePosteriorSeed = null;
  resetOpeningMarkup();
  e.dirty = false;
  e.selectionRequestId += 1;
  e.selectionAbort?.abort();
  e.selectionAbort = null;
  e.selectionKey = null;
  e.selectionStatus = "idle";
  e.viewer?.clear();
  renderEndplaneUi();
  renderActions(state.case);
  try {
    const profile = await api(`/api/cases/${encodeURIComponent(context.caseId)}/segment/endplanes?scope=${encodeURIComponent(context.scope)}`);
    if (e.requestId !== requestId || endplaneContext()?.key !== context.key) return;
    if (profile.scope !== context.scope || !CT_VIEWS.includes(profile.view) ||
        !Number.isInteger(profile.count) || profile.count < 2 ||
        !Array.isArray(profile.areas_mm2) || profile.areas_mm2.length !== profile.count ||
        ![-1, 1].includes(Number(profile.anterior_sign)) || profile.mask_version !== context.version ||
        !validLpsVector(profile.ap_origin_lps_mm) || !validLpsVector(profile.ap_direction_lps) ||
        !Number.isFinite(profile.ap_spacing_mm) || profile.ap_spacing_mm <= 0) {
      throw new Error("端面层面数据与当前 CT 或掩膜版本不一致，请重新读取病例");
    }
    e.profile = profile;
    e.status = "ready";
    const saved = profile.saved;
    e.stale = Boolean(profile.stale || saved?.stale ||
      (saved?.mask_version !== undefined && Number(saved.mask_version) !== context.version));
    const supportsLocal = Array.isArray(profile.supported_formats) &&
      profile.supported_formats.includes("oblique_local_inlet_v2");
    const supportsBilateral = Array.isArray(profile.supported_formats) &&
      profile.supported_formats.includes("oblique_bilateral_local_v3");
    e.format = !e.stale && ["oblique_planes_v1", "oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(saved?.format)
      ? saved.format : supportsBilateral ? "oblique_bilateral_local_v3" : supportsLocal ? "oblique_local_inlet_v2" : "oblique_planes_v1";
    e.baselineFormat = e.format;
    if (!e.stale && saved?.format === "oblique_bilateral_local_v3") {
      e.baselineBilateral = cloneBilateralDraft({
        left: { plane: saved.left_inlet_plane, seed: saved.left_inlet_seed_lps_mm },
        right: { plane: saved.right_inlet_plane, seed: saved.right_inlet_seed_lps_mm },
      });
    }
    e.bilateral = cloneBilateralDraft(e.baselineBilateral);
    e.posteriorSeedPlane = null;
    e.sharedInletPlane = !e.stale && saved?.format === "oblique_bilateral_local_v3" &&
      Boolean(e.bilateral.left.plane && e.bilateral.right.plane) &&
      coincidentEndplanes(e.bilateral.left.plane, e.bilateral.right.plane);
    e.baselinePosteriorSeed = !e.stale && saved?.format === "oblique_bilateral_local_v3" &&
      validLpsVector(saved.posterior_seed_lps_mm) ? [...saved.posterior_seed_lps_mm] : null;
    e.posteriorSeed = e.baselinePosteriorSeed ? [...e.baselinePosteriorSeed] : null;
    e.baselineAnteriorSeed = !e.stale && saved?.format === "oblique_local_inlet_v2" &&
      validLpsVector(saved.anterior_seed_lps_mm) ? [...saved.anterior_seed_lps_mm] :
      e.format === "oblique_bilateral_local_v3" ? e.baselineBilateral.left.seed : null;
    e.anteriorSeed = e.baselineAnteriorSeed ? [...e.baselineAnteriorSeed] : null;
    e.pairError = saved?.validation_error?.reason || null;
    if (!e.stale && saved) {
      if (saved.format === "oblique_planes_v1" || saved.format === "oblique_local_inlet_v2") {
        e.baselinePlanes = {
          anterior: normalizedEndplane(saved.anterior_plane),
          posterior: normalizedEndplane(saved.posterior_plane),
        };
      } else if (saved.format === "oblique_bilateral_local_v3") {
        e.baselinePlanes = {
          anterior: cloneEndplane(e.baselineBilateral.left.plane),
          posterior: normalizedEndplane(saved.posterior_plane),
        };
      } else if (Number.isInteger(saved.anterior_index) && Number.isInteger(saved.posterior_index)) {
        e.baselinePlanes = {
          anterior: planeFromCtIndex(profile, "anterior", saved.anterior_index),
          posterior: planeFromCtIndex(profile, "posterior", saved.posterior_index),
        };
      }
      e.planes = cloneEndplanePair(e.baselinePlanes);
      e.placement = e.planes[e.mode] ? null : "point";
      for (const side of ["anterior", "posterior"]) e.values[side] = ctIndexFromPlane(profile, e.planes[side]);
      e.baseline = { ...e.values };
    }
    if (e.format === "oblique_bilateral_local_v3" && !e.stale && !saved) {
      e.planes.anterior = cloneEndplane(e.bilateral.left.plane);
      e.anteriorSeed = e.bilateral.left.seed ? [...e.bilateral.left.seed] : null;
    }
    updateEndplaneDirty();
    if (e.open) loadEndplaneSelectionModel(context);
    loadFormalPorts(context);
  } catch (error) {
    if (e.requestId !== requestId || endplaneContext()?.key !== context.key) return;
    e.status = "error";
    e.error = error.detail?.reason || error.message || "端面数据读取失败";
  } finally {
    if (e.requestId === requestId && endplaneContext()?.key === context.key) {
      renderEndplaneUi();
      renderActions(state.case);
    }
  }
}

function endplaneArea(index) {
  if (index === null) return null;
  const raw = state.endplanes.profile?.areas_mm2?.[index];
  const area = raw === null || raw === undefined ? NaN : Number(raw);
  return Number.isFinite(area) && area >= 0 ? area : null;
}

function endplaneValidation(side = null) {
  const e = state.endplanes;
  if (!e.profile) return "当前 CT 层面资料尚未加载";
  if (e.format === "oblique_bilateral_local_v3") {
    syncActiveBilateralDraft();
    if (endplaneContext()?.scope !== "main_airway") return "双鼻孔局部裁切只适用于主鼻腔气流模型";
    const targets = side === "anterior" ? [e.activeInlet] : side === "posterior" ? ["posterior"] : ["left", "right", "posterior"];
    const indices = {};
    for (const target of targets) {
      const inlet = target !== "posterior";
      const label = target === "left" ? "左鼻孔" : target === "right" ? "右鼻孔" : "鼻咽";
      const candidate = inlet ? e.bilateral[target] : { plane: e.planes.posterior, seed: e.posteriorSeed };
      const plane = normalizedEndplane(candidate.plane);
      if (!plane) return "请先放置" + label + "端切面";
      const index = ctIndexFromPlane(e.profile, plane);
      if (index === null) return label + "端切面超出 CT 扫描范围";
      indices[target] = index;
      if (inlet) {
        const projection = plane.normal_lps.reduce((sum, value, axis) =>
          sum + value * e.profile.ap_direction_lps[axis] * Number(e.profile.anterior_sign), 0);
        if (projection <= 0) return label + "端切除方向朝向气道内部，请翻转法向或调整角度";
      }
      if (!validLpsVector(candidate.seed)) return "请在三维模型点选" + label + "开口，或在右侧 CT 选择可用入口；正在读取时请稍候";
      const distance = candidate.seed.reduce((sum, value, axis) =>
        sum + (value - plane.point_lps_mm[axis]) * plane.normal_lps[axis], 0);
      if (distance <= 0.1) return label + "定位点必须位于切面的删除侧";
    }
    if (side) return null;
    if ((indices.left - indices.posterior) * Number(e.profile.anterior_sign) <= 0 ||
        (indices.right - indices.posterior) * Number(e.profile.anterior_sign) <= 0) {
      return "左右鼻孔端都必须位于鼻咽端前方";
    }
    if (Math.hypot(...e.bilateral.left.seed.map((value, axis) => value - e.bilateral.right.seed[axis])) < 1) {
      return "左右鼻孔定位点过近，请核对是否重复选择同一支段";
    }
    return null;
  }
  const sides = side ? [side] : ["anterior", "posterior"];
  const planes = {};
  const indices = {};
  for (const current of sides) {
    const plane = normalizedEndplane(e.planes[current]);
    const label = current === "anterior" ? "鼻孔端" : "鼻咽端";
    if (!plane) return `请先放置${label}裁切面`;
    const index = ctIndexFromPlane(e.profile, plane);
    if (index === null) return `${label}超出 CT 前后扫描范围，请移回气道区域`;
    planes[current] = plane;
    indices[current] = index;
    const outward = (current === "anterior" ? 1 : -1) * Number(e.profile.anterior_sign);
    const projection = plane.normal_lps.reduce((sum, value, axis) =>
      sum + value * e.profile.ap_direction_lps[axis] * outward, 0);
    if (projection <= 0) {
      return `${label}的切除方向朝向气道内部。请点击“切除另一侧”，或调整切面角度。`;
    }
    if (isAxialEndplane(e.profile, current, plane)) {
      const area = endplaneArea(index);
      if (area === null || area < 1) return `${label}掩膜估算面积低于 1 mm² 技术下限，请重新放置`;
    }
  }
  if (e.format === "oblique_local_inlet_v2" && (!side || side === "anterior")) {
    if (!validLpsVector(e.anteriorSeed)) return "请在鼻孔切面的外侧，单独点选准备移除的鼻孔支段";
    const anterior = normalizedEndplane(e.planes.anterior);
    if (!anterior) return "请先放置鼻孔端裁切面";
    const distance = e.anteriorSeed.reduce((sum, value, axis) =>
      sum + (value - anterior.point_lps_mm[axis]) * anterior.normal_lps[axis], 0);
    if (distance <= 0.1) return "目标支段选点必须位于鼻孔切面的删除侧，请重新选点或调整切面";
  }
  if (side) return null;
  const anterior = planes.anterior;
  const posterior = planes.posterior;
  if ((indices.anterior - indices.posterior) * Number(e.profile.anterior_sign) <= 0) return "鼻孔端必须位于鼻咽端前方，请分别核实并保存两端";
  return null;
}

function getEndplaneLearningDraft() {
  const context = endplaneContext();
  const body = endplaneBody(context);
  if (!context || !body) return { plan: null, error: state.endplanes.format === "oblique_bilateral_local_v3"
    ? "请先完成左右鼻孔及鼻咽三端草稿" : "请先完成鼻孔端和鼻咽端草稿" };
  return { plan: { ...body, format: state.endplanes.format }, error: endplaneValidation() };
}

function applyEndplaneLearningDraft(plan) {
  const context = endplaneContext(), e = state.endplanes;
  if (!context || e.key !== context.key || e.status !== "ready" || e.saving || e.profile?.can_save === false) {
    throw new Error("当前编辑器尚未就绪，请等待读取完成后重试");
  }
  const bilateral = plan.format === "oblique_bilateral_local_v3";
  if (plan.scope !== context.scope || plan.mask_version !== context.version ||
      !["oblique_planes_v1", "oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(plan.format) ||
      !normalizedEndplane(plan.posterior_plane) ||
      (bilateral
        ? context.scope !== "main_airway" || !normalizedEndplane(plan.left_inlet_plane) ||
          !normalizedEndplane(plan.right_inlet_plane) ||
          !validLpsVector(plan.left_inlet_seed_lps_mm) ||
          !validLpsVector(plan.right_inlet_seed_lps_mm) ||
          !validLpsVector(plan.posterior_seed_lps_mm)
        : !normalizedEndplane(plan.anterior_plane)) ||
      (plan.format === "oblique_local_inlet_v2" &&
        (context.scope !== "main_airway" || !validLpsVector(plan.anterior_seed_lps_mm)))) {
    throw new Error("候选与当前病例、掩膜或端面格式不一致");
  }
  if (!e.profile.supported_formats?.includes(plan.format)) throw new Error("当前编辑器尚不支持该候选格式");
    if (e.dirty && !window.confirm("当前端面草稿尚未保存。加载候选将替换端面草稿，确定继续吗？")) return false;
  if (bilateral) {
    e.activeInlet = "left";
    if (e.baselineFormat === "oblique_bilateral_local_v3") {
      e.baselinePlanes.anterior = cloneEndplane(e.baselineBilateral.left.plane);
      e.baselineAnteriorSeed = e.baselineBilateral.left.seed ? [...e.baselineBilateral.left.seed] : null;
      e.baseline.anterior = ctIndexFromPlane(e.profile, e.baselinePlanes.anterior);
    }
    e.bilateral = cloneBilateralDraft({
      left: { plane: plan.left_inlet_plane, seed: plan.left_inlet_seed_lps_mm },
      right: { plane: plan.right_inlet_plane, seed: plan.right_inlet_seed_lps_mm },
    });
    e.sharedInletPlane = coincidentEndplanes(plan.left_inlet_plane, plan.right_inlet_plane);
    e.posteriorSeedPlane = null;
    e.posteriorSeed = [...plan.posterior_seed_lps_mm];
  }
  e.planes = { anterior: cloneEndplane(bilateral ? plan.left_inlet_plane : plan.anterior_plane),
    posterior: cloneEndplane(plan.posterior_plane) };
  e.format = plan.format;
  e.anteriorSeed = bilateral ? [...plan.left_inlet_seed_lps_mm] :
    plan.format === "oblique_local_inlet_v2" ? [...plan.anterior_seed_lps_mm] : null;
  e.adjustments = { anterior: null, posterior: null };
  e.history = { anterior: [], posterior: [] };
  e.editHistory = [];
  e.pickedPoints = [];
  e.placement = null;
  clearEndplaneSection();
  e.viewer?.setFormat(e.format);
  e.viewer?.setPlanes(e.planes, e.mode);
  e.viewer?.showSeedMarker(currentEndplaneSeed());
  updateEndplaneDraft(true);
  return true;
}

function renderAutoCandidate() {
  const e = state.endplanes;
  const panel = $("endplane-auto-candidate");
  const button = $("endplane-auto-candidate-button");
  const status = $("endplane-auto-candidate-status");
  if (!panel || !button || !status) return;
  const context = endplaneContext();
  panel.hidden = context?.scope !== "main_airway" ||
    !e.profile?.supported_formats?.includes("oblique_bilateral_local_v3");
  if (panel.hidden) return;
  const auto = e.autoCandidate;
  button.disabled = e.saving || e.profile.can_save === false || auto.status === "loading";
  button.textContent = auto.status === "loading" ? "正在找三个开口…" : "自动找三个开口";
  status.classList.toggle("is-error", ["error", "unavailable"].includes(auto.status));
  status.textContent = auto.status === "loading"
    ? "正在从已确认掩膜定位左右鼻孔与鼻咽，并检查三处封盖几何…"
    : auto.status === "loaded"
      ? "候选已载入未保存草稿。请核对三维模型和 CT；自动预检通过后，查看实际裁切，再决定是否保存。"
      : auto.status === "generated"
        ? "候选已生成，但你保留了原草稿；正式入口未改变。"
        : auto.status === "unavailable" || auto.status === "error"
          ? auto.reason
          : "根据已确认的气道范围生成待复核草稿；不会自动保存端口或运行 CFD。";
}

async function autoProposeBilateralOpenings() {
  const context = endplaneContext(), e = state.endplanes;
  if (!context || context.scope !== "main_airway" || e.key !== context.key ||
      e.status !== "ready" || !e.open || e.saving || e.profile?.can_save === false ||
      !e.profile?.supported_formats?.includes("oblique_bilateral_local_v3")) return;
  const auto = e.autoCandidate;
  if (auto.status === "loading") return;
  const requestId = ++auto.requestId;
  const controller = new AbortController();
  auto.controller = controller;
  auto.status = "loading";
  auto.reason = null;
  renderAutoCandidate();
  try {
    const result = await api("/api/cases/" + encodeURIComponent(context.caseId) +
      "/segment/endplanes/learning/auto-propose-bilateral", {
      method: "POST", body: { scope: context.scope, mask_version: context.version },
      signal: controller.signal,
    });
    if (e.autoCandidate !== auto || auto.requestId !== requestId || !e.open ||
        e.key !== context.key || endplaneContext()?.key !== context.key) return;
    if (result?.status === "unavailable" && result.human_review_required === true &&
        result.committed === false && typeof result.reason === "string") {
      const reasons = {
        unsupported_scope: "当前气道范围不支持自动寻找三个开口。",
        bilateral_local_unavailable: "本机三端局部几何依赖尚未就绪。",
        mask_not_ready_or_stale: "气道掩膜尚未确认，或版本已经变化。",
        case_context_unavailable: "当前病例几何暂时无法读取。",
        mask_changed_during_generation: "生成期间气道掩膜发生变化，请重新读取病例。",
        excessive_cut_required: "要形成完整的三个开口，当前自动方案需要切除过多气道，已停止。请检查原始分割是否保留鼻孔前段；必要时进入专家流程核对。",
        three_cap_preflight_unavailable: "未找到能同时通过三处封盖预检的开口；请手动选择并检查掩膜。",
      };
      auto.status = "unavailable";
      auto.reason = reasons[result.reason] || "当前几何条件不满足自动提案要求；请手动选择。";
      return;
    }
    if (result?.status !== "available" || result.human_review_required !== true ||
        result.committed !== false || !result.proposal_id ||
        result.proposal?.proposal_id !== result.proposal_id ||
        result.proposal?.case_id !== context.caseId || result.proposal?.scope !== context.scope ||
        result.proposal?.mask_version !== context.version ||
        result.proposal?.plan?.format !== "oblique_bilateral_local_v3") {
      throw new Error("自动候选与当前病例或掩膜版本不一致，未载入草稿");
    }
    const applied = applyEndplaneLearningDraft(result.proposal.plan);
    auto.status = applied ? "loaded" : "generated";
  } catch (error) {
    if (error.name === "AbortError" || e.autoCandidate !== auto ||
        auto.requestId !== requestId || e.key !== context.key) return;
    auto.status = "error";
    auto.reason = "自动找开口失败：" + (error.detail?.reason || error.message || "未知错误") + "。可重试或手动选择。";
  } finally {
    if (e.autoCandidate === auto && auto.requestId === requestId) {
      auto.controller = null;
      renderAutoCandidate();
    }
  }
}

function renderSinglePortSuggestion() {
  const e = state.endplanes, suggestion = e.singlePortSuggestion;
  const panel = $("endplane-single-port");
  if (!panel) return;
  panel.hidden = e.format !== "oblique_bilateral_local_v3" || e.mode !== "anterior" || e.sharedInletPlane;
  if (panel.hidden) return;
  const find = $("endplane-single-port-find");
  const apply = $("endplane-single-port-apply");
  const status = $("endplane-single-port-status");
  const eligible = singlePortSuggestionEligible();
  const current = eligible && suggestion.status === "proposed" &&
    suggestion.contextKey === endplaneContext()?.key &&
    suggestion.signature === endplaneSignature() && suggestion.side === e.activeInlet;
  find.disabled = !eligible || suggestion.status === "loading";
  find.textContent = suggestion.status === "loading" ? "正在检查实际开口…" : "尝试找更完整的单一入口";
  apply.hidden = !current;
  apply.disabled = !current || e.saving;
  status.classList.toggle("is-error", ["error", "unavailable"].includes(suggestion.status));
  const sideLabel = e.activeInlet === "left" ? "左鼻孔" : "右鼻孔";
  status.textContent = suggestion.status === "loading" ? `正在试算${sideLabel}切面向外微移，并检查左右鼻孔与鼻咽三处真实网格封盖；原草稿未变。` :
    suggestion.status === "proposed" && current
      ? `找到建议：${sideLabel}切面沿原角度向外移动 ${suggestion.result.shift_mm[e.activeInlet]} mm，三处网格封盖均为单环。尚未采用；采用后仍需查看实际裁切网格才能保存。` :
    suggestion.status === "proposed" ? "端面草稿已变化，此建议已过期，请重新查找。" :
    suggestion.status === "unavailable" || suggestion.status === "error" ? suggestion.reason :
    !eligible ? "先完成左右鼻孔与鼻咽三处切面和定位点，才可试算当前鼻孔入口。" :
    `只尝试将${sideLabel}切面向外微移，保留更多已分割的气道；不会自动修改或保存。`;
}

async function requestSinglePortSuggestion() {
  const e = state.endplanes, context = endplaneContext();
  if (!singlePortSuggestionEligible() || e.singlePortSuggestion.status === "loading") return;
  const body = endplaneBody(context);
  const signature = endplaneSignature();
  const side = e.activeInlet;
  resetSinglePortSuggestionState();
  const suggestion = e.singlePortSuggestion;
  const requestId = ++suggestion.requestId;
  const controller = new AbortController();
  suggestion.status = "loading";
  suggestion.controller = controller;
  suggestion.signature = signature;
  suggestion.contextKey = context.key;
  suggestion.side = side;
  suggestion.originalBody = JSON.parse(JSON.stringify(body));
  const isCurrent = () => e.singlePortSuggestion === suggestion && suggestion.requestId === requestId &&
    e.key === context.key && endplaneContext()?.key === context.key && state.caseId === context.caseId &&
    e.activeInlet === side && endplaneSignature() === signature && singlePortSuggestionEligible();
  renderSinglePortSuggestion();
  try {
    const result = await api(`/api/cases/${encodeURIComponent(context.caseId)}/segment/endplanes/suggest-single-port`, {
      method: "POST", body: { ...body, side }, signal: controller.signal,
    });
    if (!isCurrent()) return;
    if (result?.scope !== context.scope || result.mask_version !== context.version || result.side !== side) {
      throw new Error("建议所属病例或掩膜版本不一致，原草稿保持不变");
    }
    if (result.status === "unavailable") {
      const reasons = {
        original_port_not_selectable: "当前鼻孔入口无法形成可选的单一连通开口。请调整该侧切面或检查鼻孔前段掩膜；原草稿未变。",
        three_cap_preflight_unavailable: "向外微移后仍找不到左右鼻孔和鼻咽三处均可封盖的单环方案。请调整该侧切面或检查掩膜；原草稿未变。",
        mesh_preflight_budget_exhausted: "实际网格试算达到次数上限，尚无可确认建议。请调整该侧切面或检查掩膜后重试；原草稿未变。",
      };
      suggestion.status = "unavailable";
      suggestion.reason = reasons[result.reason] || "当前未找到可通过三处网格检查的方案。请调整该侧切面或检查掩膜；原草稿未变。";
      return;
    }
    validateSinglePortSuggestion(result, suggestion.originalBody, side);
    suggestion.result = result;
    suggestion.status = "proposed";
  } catch (error) {
    if (error.name === "AbortError" || !isCurrent()) return;
    suggestion.status = "error";
    suggestion.reason = "入口建议失败：" + (error.detail?.reason || error.message || "未知错误") + "。原草稿未变，请调整切面或检查掩膜后重试。";
  } finally {
    if (e.singlePortSuggestion === suggestion && suggestion.requestId === requestId) {
      suggestion.controller = null;
      if (!isCurrent() && suggestion.status === "loading") resetSinglePortSuggestionState();
      renderSinglePortSuggestion();
    }
  }
}

function applySinglePortSuggestion() {
  const e = state.endplanes, suggestion = e.singlePortSuggestion;
  const context = endplaneContext();
  if (suggestion.status !== "proposed" || !singlePortSuggestionEligible() ||
      suggestion.contextKey !== context?.key || suggestion.signature !== endplaneSignature() ||
      suggestion.side !== e.activeInlet) {
    toast("当前端面已变化，请重新查找入口建议", true);
    resetSinglePortSuggestionState();
    renderSinglePortSuggestion();
    return false;
  }
  let shift;
  try {
    shift = validateSinglePortSuggestion(suggestion.result, endplaneBody(context), suggestion.side);
  } catch (error) {
    suggestion.status = "error";
    suggestion.reason = error.message;
    renderSinglePortSuggestion();
    return false;
  }
  const side = suggestion.side;
  const previous = { kind: "single-port-suggestion", side,
    plane: cloneEndplane(e.bilateral[side].plane), seed: [...e.bilateral[side].seed] };
  e.editHistory.push(previous);
  if (e.editHistory.length > 40) e.editHistory.shift();
  e.planes.anterior = cloneEndplane(suggestion.result.plan[`${side}_inlet_plane`]);
  e.anteriorSeed = [...suggestion.result.plan[`${side}_inlet_seed_lps_mm`]];
  e.bilateral[side] = { plane: cloneEndplane(e.planes.anterior), seed: [...e.anteriorSeed] };
  e.adjustments.anterior = null;
  e.placement = null;
  e.pickedPoints = [];
  e.viewer?.setPlanes(e.planes, "anterior");
  e.viewer?.showSeedMarker(e.anteriorSeed);
  clearEndplaneSection();
  updateEndplaneDraft(true);
  scheduleEndplaneSection(true);
  toast(`已采用${side === "left" ? "左鼻孔" : "右鼻孔"}向外 ${shift} mm 的建议；请核对实际裁切后保存`);
  return true;
}

function renderEndplaneUi(preserveNumber = null) {
  const e = state.endplanes;
  const panel = $("endplane-panel");
  const button = $("btn-endplanes");
  if (!panel || !button) return;
  const hasMask = ["ok", "warn"].includes(state.case?.stages?.segment?.status);
  button.disabled = !state.caseId || !hasMask;
  button.classList.toggle("is-on", e.open);
  button.setAttribute("aria-expanded", String(e.open));
  panel.hidden = !e.open;
  if (window.WindcatcherEndplaneLearning) {
    const learningContext = endplaneContext();
    window.WindcatcherEndplaneLearning.setContext({
      caseId: learningContext?.caseId || null, scope: learningContext?.scope || null,
      maskVersion: learningContext?.version ?? null, isOpen: e.open,
      canEdit: e.status === "ready" && e.profile?.can_save !== false && !e.saving,
      getDraft: getEndplaneLearningDraft, applyDraft: applyEndplaneLearningDraft,
    });
  }
  if (!e.open) { e.viewer?.pause(); if (e.sectionKey) clearEndplaneSection(); return; }
  e.viewer?.resume();
  const context = endplaneContext();
  const feedback = $("endplane-feedback");
  const status = $("endplane-status");
  const controls = $("endplane-controls");
  let statusText = "待复核";
  let message = "请先去除体外空气、逐层复核并确认气道掩膜，然后再选择两端裁切层面。";
  if (state.case?.synthetic) message = "合成测试病例可选择端面以验收完整流程；测试结果不能作为患者结果。";
  else if (context && e.status === "loading") { statusText = "读取中"; message = "正在读取当前模型范围每一层的掩膜截面积…"; }
  else if (context && e.status === "error") { statusText = "读取失败"; message = e.error || "端面数据读取失败"; }
  else if (context && e.status === "ready") {
    const saved = e.profile?.saved;
    const savedAnterior = Boolean(saved?.anterior_plane || saved?.left_inlet_plane || Number.isInteger(saved?.anterior_index));
    const savedPosterior = Boolean(saved?.posterior_plane || Number.isInteger(saved?.posterior_index));
    statusText = e.saving ? "保存中" : e.profile?.can_save === false ? "当前不可保存" : e.dirty ? "未保存" : e.stale ? "旧设置已失效" : e.pairError ? "端面有冲突" : endplaneSaved() ? "已保存" : savedAnterior || savedPosterior ? "已有端面" : "待选择";
    if (saved?.format === "oblique_planes_v1" && e.format === "oblique_planes_v1" &&
        e.profile?.supported_formats?.includes("oblique_local_inlet_v2")) statusText = "旧版已保存，待升级";
    if (saved?.format === "oblique_local_inlet_v2" && e.format === "oblique_local_inlet_v2" &&
        e.profile?.supported_formats?.includes("oblique_bilateral_local_v3")) statusText = "单支方案，待升级";
    message = e.error || e.selectionError || (e.profile?.can_save === false
      ? e.profile.message || "当前掩膜尚未满足端面保存条件，请先重新分割、清理体外空气并确认掩膜。"
      : e.stale
      ? "之前保存的端面属于旧掩膜版本，已失效；请在当前 CT 中重新选择两端。"
      : e.pairError
      ? `${e.pairError}；请只调整并保存冲突的那一端。`
      : e.dirty ? "所选层面尚未保存。请在 CT 中核实两端后保存。"
      : endplaneSaved() ? "当前范围的端面位置已保存；可以重新生成 STL 与几何质检。"
      : savedAnterior !== savedPosterior ? "已保存一侧端面；请继续选择并保存另一侧，完成后才会生成模型。"
      : "先选择鼻孔端，在模型上点位置；用滑块微调并核对 CT 后保存，再继续鼻咽端。");
  }
  feedback.textContent = message;
  if (e.format === "oblique_planes_v1" && e.profile?.supported_formats?.includes("oblique_local_inlet_v2")) {
    feedback.textContent = "旧版端面采用贯穿全模型的平面，可能误切相邻鼻孔。旧方案只读；请显式改用局部鼻孔裁切，重新选目标支段并完成预检。";
  }
  if (e.format === "oblique_local_inlet_v2" && e.profile?.supported_formats?.includes("oblique_bilateral_local_v3")) {
    feedback.textContent = "当前仅有单支鼻孔入口，不能进入双侧鼻呼吸 CFD。请改用双侧局部裁切，分别核对左右鼻孔和鼻咽。";
  }
  if (e.format === "oblique_bilateral_local_v3" && e.status === "ready" && !e.error) {
    feedback.textContent = !e.dirty && endplaneSaved()
      ? "双侧三端面已保存；仍需检查 STL 开口及 CFD 数值质量。"
      : "";
  }
  feedback.classList.toggle("is-error", e.status === "error" || Boolean(e.error && e.status === "ready"));
  $("endplane-retry").hidden = !context || e.status !== "error";
  status.className = `pill ${e.status === "error" ? "pill-fail" : endplaneSaved() && !e.dirty ? "pill-ok" : "pill-warn"}`;
  status.innerHTML = `<i></i>${statusText}`;
  controls.hidden = !context || e.status !== "ready" || e.profile?.can_save === false;
  if (controls.hidden) return;
  renderAutoCandidate();
  $("endplane-preview-mode").value = state.previewMode;
  const profile = e.profile;
  const bilateral = e.format === "oblique_bilateral_local_v3";
  const local = bilateral || e.format === "oblique_local_inlet_v2";
  const legacyReadOnly = !bilateral && (
    e.format === "oblique_planes_v1" && profile.supported_formats?.includes("oblique_local_inlet_v2") ||
    e.format === "oblique_local_inlet_v2" && profile.supported_formats?.includes("oblique_bilateral_local_v3"));
  $("endplane-actions-help").textContent = bilateral
    ? "三端面须一次保存；任何修改都会使旧 STL / CFD 算例失效。保存不等于 CFD 数值质量通过。"
    : "端面保存会使旧 STL / CFD 算例失效；请先核对预检和实际裁切。";
  $("endplane-switch-local").hidden = !legacyReadOnly || e.saving;
  $("endplane-switch-local").textContent = profile.supported_formats?.includes("oblique_bilateral_local_v3")
    ? "选择双鼻孔与鼻咽切面" : "改用局部鼻孔裁切（需重新选支段）";
  $("endplane-local-mode").hidden = !local;
  $("endplane-legacy-mode-bar").hidden = bilateral;
  renderEndplaneOpeningSteps();
  $("endplane-local-title").textContent = bilateral ? "三维模型点选真实开口 · 左鼻孔、右鼻孔、鼻咽端" : "局部鼻孔端 · 单支段入口";
  $("endplane-local-help").textContent = bilateral
    ? "先选左鼻孔、右鼻孔或鼻咽端，再点下方按钮、在三维模型的目标开口外侧点一次；也可在 CT 选入口。橙色是已有掩膜空气但未形成可选开口；CT 黑色但没有青绿掩膜边界的部分可能漏分割，选入口不会自动补齐。保存前须看真实网格预览。"
    : "先定鼻孔端切面，再点选该切面外侧准备移除的鼻孔支段。另一鼻孔不会自动成为入口。";
  $("endplane-local-save").hidden = !local;
  $("endplane-preview-model").hidden = !local;
  $("endplane-preview-model").disabled = true;
  $("endplane-preview-model").title = "体素预检通过后加载实际裁切网格；草稿变化会撤回预览";
  $("endplane-model-legend").textContent = bilateral ? "蓝/绿/橙填色：当前切面与模型的局部交线，尚非最终开口；实际结果以裁切预览 STL 为准" : local ? "填色：模型与切面的真实局部截面；黄色点：当前切面外侧待切支段" : "青色：全局平面裁切示意；彩色边界来自模型网格";
  $("endplane-show-removed-label").hidden = local;
  $("endplane-3d-help").textContent = local
    ? bilateral
      ? "着色显示切面附近的网格交线，仅用于定位。三维点选和 CT 高亮使用同一待切入口；三端选好后仍须核对实际裁切 STL 和封盖。"
      : "当前显示完整候选模型、切面交线和选点；实际删除范围须由服务端预检与最终网格核对。该方案只代表所选单支段入口，不代表双侧鼻呼吸 CFD。"
    : "旧版平面会贯穿模型；灰色仅为未裁切模型叠影，不是实际删除网格。请核对 CT 与 STL。";
  e.viewer?.setFormat(e.format);
  const seedButton = $("endplane-seed-place");
  const currentSeed = currentEndplaneSeed();
  seedButton.parentElement.hidden = false;
  $("endplane-independent-wrap").hidden = !bilateral;
  $("endplane-independent").checked = !e.sharedInletPlane;
  $("endplane-independent").disabled = e.saving;
  seedButton.disabled = e.saving || e.selectionStatus !== "ready" || !e.planes[e.mode] ||
    (!bilateral && e.mode !== "anterior");
  seedButton.classList.toggle("is-on", e.placement === "seed");
  seedButton.setAttribute("aria-pressed", String(e.placement === "seed"));
  const openingLabel = e.mode === "posterior" ? "鼻咽出口" : bilateral ? (e.activeInlet === "right" ? "右鼻孔入口" : "左鼻孔入口") : "鼻孔入口";
  seedButton.textContent = e.placement === "seed" ? "取消三维点选" : currentSeed ? `在三维模型重选${openingLabel}` : `在三维模型点选${openingLabel}`;
  $("endplane-seed-clear").disabled = e.saving || !validLpsVector(currentSeed);
  $("endplane-seed-status").textContent = validLpsVector(currentSeed)
    ? "此入口已定位；请查看实际裁切网格预览" : "此入口尚未定位；不能保存";
  renderSinglePortSuggestion();
  const viewLabel = CT_VIEW_LABELS[profile.view];
  $("endplane-orientation").textContent = `当前范围：${context.scope === "full_candidate" ? MODEL_SCOPE_OPTIONS.complete_candidate : MODEL_SCOPE_OPTIONS.main_nasal_airway} · ${viewLabel}共 ${profile.count} 层；层号越${Number(profile.anterior_sign) > 0 ? "大" : "小"}越靠鼻孔侧。先用 CT 看解剖位置，再用面积排除毛刺。`;
  for (const side of ["anterior", "posterior"]) {
    const button = $(`endplane-mode-${side}`);
    if (button) {
      button.hidden = bilateral && !e.sharedInletPlane && side === "anterior";
      button.classList.toggle("is-on", e.mode === side);
      button.setAttribute("aria-pressed", String(e.mode === side));
      const label = side === "anterior" ? bilateral ? "1 双鼻孔" : "1 鼻孔端" : bilateral && !e.sharedInletPlane ? "3 鼻咽端" : "2 鼻咽端";
      button.textContent = `${label}${e.dirtySides[side] ? " · 未保存" : e.baselinePlanes[side] ? " · 已保存" : ""}`;
      button.disabled = e.saving;
    }
  }
  for (const side of ["left", "right"]) {
    const button = $("endplane-mode-" + side);
    button.hidden = !bilateral || e.sharedInletPlane;
    button.disabled = e.saving;
    button.classList.toggle("is-on", bilateral && e.mode === "anterior" && e.activeInlet === side);
    button.setAttribute("aria-pressed", String(bilateral && e.mode === "anterior" && e.activeInlet === side));
    const entry = e.bilateral[side];
    const savedEntry = e.baselineBilateral[side];
    const dirty = endplaneSideChanged({ anterior: entry.plane }, { anterior: savedEntry.plane }, "anterior") ||
      !sameEndplaneSeed(entry.seed, savedEntry.seed);
    button.textContent = (side === "left" ? "1 左鼻孔" : "2 右鼻孔") +
      (dirty ? " · 未保存" : savedEntry.plane ? " · 已保存" : entry.plane && entry.seed ? " · 已选" : "");
  }
  for (const tool of ["translate", "rotate"]) {
    const button = $(`endplane-tool-${tool}`);
    if (button) {
      button.classList.toggle("is-on", e.tool === tool);
      button.setAttribute("aria-pressed", String(e.tool === tool));
    }
  }
  const flipButton = $("endplane-flip-normal");
  if (flipButton) flipButton.disabled = e.saving || !e.planes[e.mode];
  renderEndplaneAdjustments();
  renderFormalPorts();
  renderOpeningMarkup();
  for (const side of ["anterior", "posterior"]) {
    const index = e.values[side];
    const range = $(`endplane-${side}-range`);
    const number = $(`endplane-${side}-number`);
    const jump = $(`endplane-${side}-jump`);
    const areaNode = $(`endplane-${side}-area`);
    range.max = number.max = String(profile.count);
    range.value = String(index === null ? 1 : index + 1);
    range.disabled = number.disabled = e.saving;
    if (preserveNumber !== side) number.value = index === null ? "" : String(index + 1);
    jump.disabled = e.saving || index === null;
    const area = endplaneArea(index);
    areaNode.textContent = index === null ? "尚未选择层面；滑块当前位置不会自动保存" :
      area === null ? `第 ${index + 1} 层：无法读取掩膜截面积` :
      `第 ${index + 1} 层 · 掩膜估算截面积 ${fmt(area, " mm²")}${area < 1 ? " · 低于 1 mm² 技术下限" : " · 仍需核对实际开口"}`;
    areaNode.classList.toggle("is-warning", index !== null && (area === null || area < 1));
  }
  const validation = e.planes.anterior && e.planes.posterior ? endplaneValidation() : e.planes[e.mode] ? endplaneValidation(e.mode) : null;
  if (e.dirty && validation && !e.error) {
    const selectingRegion = bilateral && Boolean(endplaneRegionProgress().missingSide);
    feedback.textContent = selectingRegion ? "" : validation;
    feedback.classList.toggle("is-error", !selectingRegion);
  }
  const previewStatus = $("endplane-preview-status");
  previewStatus.classList.toggle("is-error", e.previewStatus === "blocked");
  previewStatus.textContent = e.previewStatus === "blocked" ? e.previewError :
    ["queued", "loading"].includes(e.previewStatus) ? "正在预检两端裁切后的截面积、保留体积与连通性…" :
    e.previewStatus === "ok" ? `两端技术预检通过 · 鼻孔端约 ${fmt(e.previewData.anterior_area_mm2, " mm²")} · 鼻咽端约 ${fmt(e.previewData.posterior_area_mm2, " mm²")}。仍需人工核对解剖位置，生成 STL 后检查实际开口。` :
    !e.planes.anterior || !e.planes.posterior ? "两端可以分别保存；放置另一端后，会自动预检两端组合。" : "两端位置已读取；请核对 CT 截面与解剖位置。";
  if (local && !bilateral) {
    previewStatus.textContent = e.previewStatus === "blocked" ? e.previewError :
      ["queued", "loading"].includes(e.previewStatus) ? "正在核对目标鼻孔支段、未选区域变化和单入口拓扑…" :
      e.previewStatus === "ok" ? "局部体素预检通过：目标支段删除 " +
        e.previewData.anterior_deleted_voxels + " 个体素，未选区域体素变化 0。请点击查看实际裁切；这一步尚未核对精确网格或双侧鼻呼吸 CFD。" :
      !e.planes.anterior || !e.planes.posterior ? "先放置两端切面，再选择鼻孔切面外侧的目标支段。" :
      "请选择目标鼻孔支段；服务端预检通过后才可保存。";
  }
  if (bilateral) {
    previewStatus.textContent = e.previewStatus === "blocked" ? e.previewError :
      ["queued", "loading"].includes(e.previewStatus) ? "正在预检左右鼻孔、鼻咽三处局部裁切与连通性…" :
      e.previewStatus === "ok" ? "三端技术预检通过 · 左 " + fmt(e.previewData.left_inlet_area_mm2, " mm²") +
        " · 右 " + fmt(e.previewData.right_inlet_area_mm2, " mm²") +
        " · 鼻咽 " + fmt(e.previewData.posterior_area_mm2, " mm²") +
        "。请查看实际裁切并人工核对三处开口。" :
      "先选左鼻孔切面，再选右鼻孔切面，最后选鼻咽切面；右侧 CT 会显示对应开口选区。";
  }
  const retention = $("endplane-retention");
  const retentionReady = bilateral && e.previewStatus === "ok" &&
    e.previewSignature === endplaneSignature();
  retention.hidden = !retentionReady;
  if (retentionReady) {
    const metrics = bilateralRetentionMetrics(e.previewData);
    $("endplane-retention-summary").textContent = metrics
      ? `当前主连通气道保留 ${metrics.retained}% · 三端共切除 ${metrics.deleted}%`
      : "预检未返回可核对的裁切比例，请不要仅凭预览保存";
    $("endplane-retention-fill").style.width = metrics ? `${metrics.retained}%` : "0%";
    $("endplane-retention-fill").parentElement.setAttribute("aria-label", metrics
      ? `主连通气道保留 ${metrics.retained}%，三端切除 ${metrics.deleted}%` : "裁切比例无法核对");
    $("endplane-retention-detail").textContent = metrics
      ? `其中左鼻孔 ${metrics.left}%、右鼻孔 ${metrics.right}%、鼻咽 ${metrics.posterior}%。` +
        (metrics.excluded ? `另有 ${metrics.excluded} 个不属于主连通气道的掩膜体素，不计入三端裁切比例。` : "") +
        "这是相对当前已分割掩膜的体素统计；还需对照 CT 和实际裁切网格判断位置，不能证明气流结果真实。"
      : "缺少服务端删除量或保留量数据。";
  }
  const previewReady = e.previewStatus === "ok" && e.previewSignature === endplaneSignature() &&
    e.previewData?.format === e.format && e.previewData?.can_preview_mesh === true;
  const actualReady = e.actualPreviewStatus === "ok" && e.actualPreviewSignature === endplaneSignature();
  $("endplane-show-removed-label").hidden = local && !actualReady;
  $("endplane-show-removed-label").lastChild.textContent = local && actualReady
    ? " 显示灰色裁切前模型对照" : " 显示灰色未裁切模型对照";
  if (local && actualReady) {
    $("endplane-3d-help").textContent = "青绿色是服务端生成的实际裁切网格；灰色是裁切前主气道的半透明叠影，不是删除部分的独立网格。切换叠影、旋转模型并核对 CT，确认没有多切。" +
      (bilateral ? " 橙色箭头沿鼻咽端切面法向，指向出口待删除侧。" : "");
  } else if (bilateral) {
    $("endplane-3d-help").textContent = "橙色箭头沿鼻咽端切面法向，指向出口待删除侧；当前是定位模型，尚未执行裁切。须通过服务端体素预检并查看实际裁切网格后才能保存。";
  }
  $("endplane-view-kind").textContent = actualReady ? "实际裁切网格" : "定位模型 · 未执行裁切";
  const actualButton = $("endplane-preview-model");
  actualButton.disabled = !local || !previewReady || e.saving || e.selectionStatus !== "ready" ||
    e.actualPreviewStatus === "loading" || e.actualPreviewStatus === "ok";
  actualButton.textContent = e.actualPreviewStatus === "loading" ? "正在加载实际裁切…" :
    e.actualPreviewStatus === "ok" ? "实际裁切已加载" :
    e.actualPreviewStatus === "error" ? "重新检查实际裁切" : "查看实际裁切";
  if (local && actualReady) {
    previewStatus.textContent += bilateral
      ? " 服务端精确网格已加载；请旋转核对左右鼻孔与鼻咽三个开口、周围结构和 CT。"
      : " 服务端精确网格已加载；请旋转核对开口和相邻结构。这仍是单支段入口。";
  } else if (local && e.actualPreviewStatus === "error") {
    previewStatus.textContent += " 实际裁切预览失败：" + e.actualPreviewError;
    previewStatus.classList.add("is-error");
  }
  $("endplane-local-save-button").disabled = !local || e.profile?.can_save === false || e.saving || !e.dirty || !previewReady || !actualReady || Boolean(endplaneValidation());
  $("endplane-local-save-button").textContent = e.saving ? "正在保存…" : bilateral ? "确认范围并保存开口" : "预检通过后保存两端";
  $("endplane-bilateral-confirm-wrap").hidden = true;
  const saveReason = $("endplane-save-reason");
  saveReason.hidden = !bilateral;
  saveReason.textContent = e.saving ? "正在保存当前切面…" : e.error ? e.error :
    e.profile?.can_save === false ? "当前气道掩膜还未确认，请先完成 CT 气道范围复核。" :
    !e.dirty ? "当前没有未保存的修改。" : endplaneValidation() ||
    (e.previewStatus === "blocked" ? e.previewError : !previewReady ? "正在自动检查开口范围…" :
    e.actualPreviewStatus === "error" ? "暂不能保存：" + e.actualPreviewError : !actualReady ? "正在生成实际裁切，请稍候…" :
    "可以保存。请核对右侧 CT 和实际裁切；点击保存即确认这三处开口的范围。");
  saveReason.classList.toggle("is-error", Boolean(e.error) || e.previewStatus === "blocked" || e.actualPreviewStatus === "error");
  const regionProgress = endplaneRegionProgress();
  const missingOpening = nextEndplaneOpening();
  const missingLabel = { left: "左鼻孔", right: "右鼻孔", posterior: "鼻咽端" }[missingOpening];
  $("endplane-save-summary").textContent = e.saving ? "正在保存三个开口…" :
    bilateral && !e.dirty && endplaneSaved() ? "三个开口已保存" :
    bilateral ? `${regionProgress.inlets + regionProgress.outlet} / 3 个开口已选定${regionProgress.missingSide ? "" : " · 尚未保存"}` : "保存局部裁切设置";
  const progressText = $("endplane-selection-progress");
  progressText.hidden = !bilateral;
  progressText.textContent = `${regionProgress.inlets + regionProgress.outlet} / 3 个开口已选定` +
    (missingOpening ? ` · 下一步：选择${missingLabel}，在三维模型点开口或在 CT 上选择对应入口` :
      !e.dirty && endplaneSaved() ? " · 已保存，可继续生成模型" : " · 选区已齐，继续核对实际裁切");
  const goMissing = $("endplane-go-missing");
  goMissing.hidden = !bilateral || !regionProgress.missingSide;
  goMissing.disabled = e.saving;
  goMissing.textContent = missingOpening ? `继续选择${missingLabel}` : "查看开口";
  if (bilateral && !e.saving && regionProgress.missingSide && !e.error) {
    saveReason.textContent = `${missingLabel}尚未完成：定位切面后，可在三维模型点目标开口，或在 CT 上点击可用候选；橙色未覆盖选区不能纳入裁切。其他开口草稿会保留。`;
  }
  $("endplane-local-discard").disabled = !local || e.saving || !e.dirty;
  for (const side of ["anterior", "posterior"]) {
    const sideValidation = endplaneValidation(side);
    const saveButton = $(`endplane-${side}-save`);
    const discardButton = $(`endplane-${side}-discard`);
    if (saveButton) saveButton.parentElement.hidden = local || e.mode !== side;
    if (saveButton) saveButton.disabled = e.saving || legacyReadOnly || e.profile?.can_save === false || !e.dirtySides[side] || Boolean(sideValidation);
    if (discardButton) discardButton.disabled = e.saving || !e.dirtySides[side];
  }
}

function endplaneRegionProgress() {
  const e = state.endplanes;
  const ready = (plane,seed) => {
    plane=normalizedEndplane(plane);
    return Boolean(plane && validLpsVector(seed) && seed.reduce((sum,v,i)=>sum+(v-plane.point_lps_mm[i])*plane.normal_lps[i],0)>0.1);
  };
  const selected = {
    left: ready(e.bilateral.left?.plane, e.bilateral.left?.seed),
    right: ready(e.bilateral.right?.plane, e.bilateral.right?.seed),
    posterior: ready(e.planes.posterior, e.posteriorSeed),
  };
  const inlets = Number(selected.left) + Number(selected.right);
  const outlet = Number(selected.posterior);
  return {inlets,outlet,missingSide:inlets<2?"anterior":outlet<1?"posterior":null,ready:selected};
}

function renderEndplaneOpeningSteps() {
  const e = state.endplanes;
  const bilateral = e.format === "oblique_bilateral_local_v3";
  $("endplane-opening-steps").hidden = !bilateral;
  $("endplane-active-badge").hidden = !bilateral;
  if (!bilateral) { $("endplane-ct-active").textContent = e.mode === "posterior" ? "鼻咽端" : "鼻孔端"; return; }
  const progress = endplaneRegionProgress();
  const active = e.mode === "posterior" ? "posterior" : e.activeInlet;
  const labels = { left: "左鼻孔", right: "右鼻孔", posterior: "鼻咽端" };
  $("endplane-active-badge").textContent = `${labels[active]} · 当前选中`;
  $("endplane-ct-active").textContent = labels[active];
  for (const side of ["left", "right", "posterior"]) {
    const button = $("endplane-step-" + side);
    const entry = side === "posterior" ? { plane: e.planes.posterior, seed: e.posteriorSeed } : e.bilateral[side];
    const baseline = side === "posterior" ? { plane: e.baselinePlanes.posterior, seed: e.baselinePosteriorSeed } : e.baselineBilateral[side];
    const unchanged = !endplaneSideChanged({ anterior: entry.plane }, { anterior: baseline.plane }, "anterior") && sameEndplaneSeed(entry.seed, baseline.seed);
    const saved = progress.ready[side] && unchanged && !e.stale && e.baselineFormat === e.format;
    const status = !entry.plane ? "待定位" : !progress.ready[side] ? "待选区域" : saved ? "已保存" : "已选 · 未保存";
    button.disabled = e.saving;
    button.classList.toggle("is-current", active === side);
    button.classList.toggle("is-complete", progress.ready[side]);
    button.setAttribute("aria-pressed", String(active === side));
    $("endplane-step-" + side + "-status").textContent = e.saving ? "保存中…" : status;
    $("endplane-step-" + side + "-hint").textContent = `${side === "posterior" ? "出气口" : "进气口"} · ${active === side ? "当前正在调整" : "点击切换"}`;
  }
}

function nextEndplaneOpening() {
  const e = state.endplanes;
  const ready = endplaneRegionProgress().ready;
  const current = e.mode === "posterior" ? "posterior" : e.activeInlet;
  return !ready[current] ? current : ["left", "right", "posterior"].find(side => !ready[side]) || null;
}

function selectEndplaneOpening(side) {
  const e = state.endplanes;
  if (e.saving || e.format !== "oblique_bilateral_local_v3") return;
  if (side === "posterior") { selectEndplaneSide("posterior"); return; }
  if (!["left", "right"].includes(side)) return;
  // Selecting a single nostril separates the controls, without changing either
  // plane/region or invalidating an otherwise current preview.
  syncActiveBilateralDraft();
  e.sharedInletPlane = false;
  selectBilateralInlet(side);
}

function coincidentEndplanes(a, b) {
  a = normalizedEndplane(a); b = normalizedEndplane(b);
  return Boolean(a && b && a.normal_lps.every((v, i) => Math.abs(v - b.normal_lps[i]) < 1e-5) &&
    Math.abs(a.point_lps_mm.reduce((sum, v, i) => sum + (v - b.point_lps_mm[i]) * a.normal_lps[i], 0)) < 0.01);
}

function setEndplaneInletLayout(shared) {
  const e = state.endplanes;
  if (e.saving || e.format !== "oblique_bilateral_local_v3") return;
  syncActiveBilateralDraft();
  e.sharedInletPlane = shared;
  if (shared) {
    for (const side of ["left", "right"]) e.bilateral[side] = { plane: cloneEndplane(e.planes.anterior), seed: null };
    e.anteriorSeed = null;
  }
  e.history = { anterior: [], posterior: [] }; e.editHistory = [];
  clearEndplaneSection();
  updateEndplaneDraft(true);
}

function applyEndplaneRegions(ids) {
  const e = state.endplanes, selection = e.sectionTargets;
  if (!selection || e.saving || selection.key !== e.sectionKey) return;
  const chosen = ids.length && ids.length <= selection.expected ? window.EndplanePicker.selected(selection.data, ids, ids.length) : null;
  selection.ids = [...ids];
  if (selection.role === "posterior") e.posteriorSeed = chosen ? [...chosen[0].seed_lps_mm] : null;
  else if (selection.expected === 2) {
    for (const [i, side] of ["left", "right"].entries()) e.bilateral[side].seed = chosen?.[i] ? [...chosen[i].seed_lps_mm] : null;
    e.anteriorSeed = e.bilateral[e.activeInlet].seed;
  } else {
    e.anteriorSeed = chosen ? [...chosen[0].seed_lps_mm] : null;
  }
  e.placement = null;
  e.viewer?.showSeedMarker(currentEndplaneSeed());
  updateEndplaneDraft(true);
  renderEndplaneRegions();
}

function renderEndplaneRegions() {
  const e = state.endplanes, selection = e.sectionTargets;
  $("endplane-clear-region").disabled = e.saving || !selection || selection.key !== e.sectionKey || !selection.ids.length;
  $("endplane-clear-region").textContent = e.sharedInletPlane && e.mode === "anterior" ? "重选双鼻孔入口" : "重选当前开口";
  if (!selection || selection.key !== e.sectionKey) return;
  window.EndplanePicker.render(selection.data, selection.ids, selection.expected, id => {
    applyEndplaneRegions(window.EndplanePicker.toggle(selection.ids, id, selection.expected));
  }, e.saving, view => {
    e.portSectionView = view;
    renderPortProjection(view);
  });
}

async function loadEndplaneTargets(body, key, requestId) {
  const e = state.endplanes, context = endplaneContext();
  // Keep at most one expensive volume read in flight; aborting fetch cannot
  // cancel a server thread. Rapid slider changes coalesce to the latest draft.
  if (e.targetsBusy) { e.targetsQueued = true; return; }
  e.targetsBusy = true;
  try {
    await fetchEndplaneTargets(body, key, requestId, context);
  } finally {
    e.targetsBusy = false;
    if (e.targetsQueued) { e.targetsQueued = false; scheduleEndplaneSection(true); }
  }
}

async function fetchEndplaneTargets(body, key, requestId, context) {
  const e = state.endplanes;
  const role = e.mode, expected = role === "anterior" && e.sharedInletPlane ? 2 : 1;
  const seeds = (expected === 2 ? [e.bilateral.left.seed, e.bilateral.right.seed] : [currentEndplaneSeed()]).filter(validLpsVector);
  const data = await api(`/api/cases/${encodeURIComponent(context.caseId)}/segment/endplanes/section-targets`, {
    method: "POST", body: { ...body, role, seeds },
  });
  if (e.sectionRequestId !== requestId || e.sectionKey !== key || endplaneContext()?.key !== context.key || !e.open) return;
  if (data.scope !== context.scope || data.mask_version !== context.version || !Array.isArray(data.candidates) || data.role !== role) {
    throw new Error("CT 选区与当前病例或掩膜版本不一致，请重试");
  }
  const ids = seeds.length ? data.selected_ids : expected === (role === "anterior" ? 2 : 1) ? data.automatic_ids : [];
  e.sectionTargets = { data, ids, expected, key, role };
  const image = $("endplane-section-image"), status = $("endplane-section-status");
  image.onload = () => {
    if (e.sectionRequestId !== requestId) return;
    image.hidden = false; status.hidden = true;
    renderEndplaneRegions();
  };
  image.onerror = () => {
    if (e.sectionRequestId !== requestId) return;
    status.hidden = false; status.textContent = "CT 图像无法显示，请重试";
    $("endplane-section-retry").hidden = false;
    clearEndplanePreview(); renderEndplaneUi();
  };
  image.src = "data:image/png;base64," + data.image_png;
  if ((!seeds.length && ids.length) || (seeds.length && ids.length !== seeds.length)) applyEndplaneRegions(ids);
  else if (endplaneBody() && !endplaneValidation() && e.previewStatus === "idle") scheduleEndplanePreview();
}

function clearEndplaneSection() {
  const e = state.endplanes;
  if (e.sectionTimer) window.clearTimeout(e.sectionTimer);
  e.sectionTimer = null;
  e.sectionAbort?.abort();
  e.sectionAbort = null;
  e.sectionRequestId += 1;
  e.sectionKey = null;
  e.sectionTargets = null;
  e.portSectionView = null;
  for (const id of ["endplane-section-fit", "endplane-section-full", "endplane-section-plus", "endplane-section-minus", "endplane-clear-region"]) {
    const control = $(id);
    if (control) control.disabled = true;
  }
  const zoom = $("endplane-section-zoom");
  if (zoom) zoom.textContent = "放置切面后可聚焦开口，或切回完整 CT。";
  const regions = $("endplane-section-regions");
  if (regions) regions.hidden = true;
  $("endplane-region-choices")?.replaceChildren();
  const regionStatus = $("endplane-region-status");
  if (regionStatus) regionStatus.textContent = "";
  if (e.sectionUrl) URL.revokeObjectURL(e.sectionUrl);
  e.sectionUrl = null;
  const image = $("endplane-section-image");
  if (image) { image.hidden = true; image.removeAttribute("src"); }
  $("endplane-port-projection").hidden = true;
  $("endplane-port-projection-status").hidden = true;
}

function scheduleEndplaneSection(force = false) {
  const e = state.endplanes, context = endplaneContext();
  const data = e.viewer?.sectionPlane();
  if (!context || !e.open || !data || e.selectionStatus !== "ready") return;
  const body = { scope: context.scope, mask_version: context.version, ...data };
  const expected = e.mode === "anterior" && e.sharedInletPlane ? 2 : 1;
  const seeds = (expected === 2 ? [e.bilateral.left.seed, e.bilateral.right.seed] : [currentEndplaneSeed()]).filter(validLpsVector);
  const key = `${context.key}|${e.mode}|${JSON.stringify(body)}|${JSON.stringify(seeds)}`;
  if (!force && key === e.sectionKey) return;
  clearEndplaneSection();
  e.sectionKey = key;
  const status = $("endplane-section-status");
  status.hidden = false;
  status.textContent = "正在读取当前角度的 CT 截面…";
  $("endplane-section-retry").hidden = true;
  const requestId = e.sectionRequestId;
  e.sectionTimer = window.setTimeout(async () => {
    e.sectionTimer = null;
    const controller = new AbortController();
    e.sectionAbort = controller;
    try {
      if (e.format === "oblique_bilateral_local_v3") {
        await loadEndplaneTargets(body, key, requestId, controller);
        return;
      }
      const response = await fetch(`/api/cases/${encodeURIComponent(context.caseId)}/segment/endplanes/section`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: controller.signal,
      });
      if (!response.ok) {
        const payload = await response.json();
        throw new Error(payload.detail?.reason || `CT 截面读取失败（HTTP ${response.status}）`);
      }
      if (response.headers.get("X-Mask-Version") !== String(context.version) || response.headers.get("X-Model-Scope") !== context.scope) {
        throw new Error("CT 截面与当前掩膜版本或范围不一致，请重新读取");
      }
      const blob = await response.blob();
      if (e.sectionRequestId !== requestId || endplaneContext()?.key !== context.key || !e.open) return;
      e.sectionUrl = URL.createObjectURL(blob);
      const image = $("endplane-section-image");
      image.onload = () => {
        if (e.sectionRequestId !== requestId) return;
        status.hidden = true;
        image.hidden = false;
        renderPortProjection();
      };
      image.onerror = () => {
        if (e.sectionRequestId !== requestId) return;
        status.hidden = false;
        status.textContent = "CT 截面图像无法显示，请重试";
        $("endplane-section-retry").hidden = false;
      };
      image.src = e.sectionUrl;
    } catch (error) {
      if (error.name === "AbortError" || e.sectionRequestId !== requestId) return;
      status.hidden = false;
      status.textContent = error.message || "CT 截面读取失败，请重试";
      $("endplane-section-retry").hidden = false;
    } finally {
      if (e.sectionRequestId === requestId) e.sectionAbort = null;
    }
  }, 300);
}

function rememberEndplaneEdit() {
  const e = state.endplanes, history = e.history[e.mode];
  const current = cloneEndplane(e.planes[e.mode]);
  if (!history.length || JSON.stringify(history[history.length - 1]) !== JSON.stringify(current)) {
    history.push(current);
    e.editHistory.push({ kind: "plane", side: e.mode, plane: current });
  }
  if (history.length > 20) history.shift();
  if (e.editHistory.length > 40) e.editHistory.shift();
}

function commitEndplaneEdit(plane, remember = true) {
  const e = state.endplanes;
  if (e.saving || (e.format === "oblique_planes_v1" && e.profile?.supported_formats?.includes("oblique_local_inlet_v2")) ||
      (e.format === "oblique_local_inlet_v2" && e.profile?.supported_formats?.includes("oblique_bilateral_local_v3"))) return;
  if (remember) rememberEndplaneEdit();
  e.planes[e.mode] = cloneEndplane(plane);
  e.adjustments[e.mode] = null;
  e.placement = null;
  e.pickedPoints = [];
  e.viewer?.setPlanes(e.planes, e.mode);
  updateEndplaneDraft(true);
}

function selectEndplaneSide(side, afterSave = false) {
  const e = state.endplanes;
  if (e.saving && !afterSave) return;
  if (e.mode !== side) resetSinglePortSuggestionState();
  e.mode = side;
  e.placement = e.planes[side] ? null : "point";
  e.pickedPoints = [];
  e.viewer?.setActiveSide(side);
  e.viewer?.focusActive();
  clearEndplaneSection();
  renderEndplaneUi();
}

function selectBilateralInlet(side) {
  const e = state.endplanes;
  if (e.saving || e.format !== "oblique_bilateral_local_v3" || !["left", "right"].includes(side)) return;
  if (e.mode !== "anterior" || e.activeInlet !== side) resetSinglePortSuggestionState();
  syncActiveBilateralDraft();
  e.activeInlet = side;
  e.mode = "anterior";
  e.planes.anterior = cloneEndplane(e.bilateral[side].plane);
  e.anteriorSeed = e.bilateral[side].seed ? [...e.bilateral[side].seed] : null;
  if (e.baselineFormat === "oblique_bilateral_local_v3") {
    e.baselinePlanes.anterior = cloneEndplane(e.baselineBilateral[side].plane);
    e.baselineAnteriorSeed = e.baselineBilateral[side].seed ? [...e.baselineBilateral[side].seed] : null;
  }
  e.values.anterior = e.profile ? ctIndexFromPlane(e.profile, e.planes.anterior) : null;
  e.baseline.anterior = e.profile ? ctIndexFromPlane(e.profile, e.baselinePlanes.anterior) : null;
  e.adjustments.anterior = null;
  e.history.anterior = [];
  e.editHistory = [];
  e.pickedPoints = [];
  e.placement = e.planes.anterior ? null : "point";
  clearEndplaneSection();
  updateEndplaneDirty();
  e.viewer?.setPlanes(e.planes, "anterior");
  e.viewer?.focusActive();
  if (e.previewStatus === "idle" && endplaneBody() && !endplaneValidation()) scheduleEndplanePreview();
  renderEndplaneUi();
}

function renderEndplaneAdjustments() {
  const e = state.endplanes, plane = e.planes[e.mode];
  const label = e.mode === "posterior" ? "鼻咽端" : e.format === "oblique_bilateral_local_v3"
    ? e.sharedInletPlane ? "双鼻孔端" : e.activeInlet === "left" ? "左鼻孔端" : "右鼻孔端" : "鼻孔端";
  const legacyReadOnly = e.format === "oblique_planes_v1" && e.profile?.supported_formats?.includes("oblique_local_inlet_v2") ||
    e.format === "oblique_local_inlet_v2" && e.profile?.supported_formats?.includes("oblique_bilateral_local_v3");
  const busy = e.saving || e.selectionStatus !== "ready" || legacyReadOnly;
  const status = e.placement === "opening-seed" ? "点击三维模型上入口内部的一个面；只是端口标注草稿"
    : e.placement === "opening-outline" ? "按住鼠标沿完整开口画圈；松开后显示实际选中的 STL 三角面"
    : e.placement === "seed" ? "点击" + label + "切面外侧待移除的支段；拖动可旋转模型"
    : e.placement === "three"
    ? `在开口周围选第 ${e.pickedPoints.length + 1} 个点（共 3 个）；拖动可旋转模型`
    : e.placement === "point" ? `在模型上点击${label}的位置；切面先平行当前屏幕`
    : e.format === "oblique_bilateral_local_v3" && !endplaneRegionProgress().ready[e.mode === "posterior" ? "posterior" : e.activeInlet]
    ? `请在右侧 CT 点击${label}的开口区域；灰色区域需调整切面`
    : `正在调整${label} · 核对截面后保存`;
  $("endplane-placement-status").textContent = status;
  $("endplane-adjust-title").textContent = `微调${label}`;
  const intersects = e.viewer?.hasIntersection();
  $("endplane-adjust-hint").textContent = !plane ? "先点选位置，也可展开下方 CT 层号选面。" :
    intersects === false ? "当前切面未穿过保留的模型，请前后移动或重新点选位置。" : "前后移动沿切面垂直方向，倾斜围绕当前开口中心。";
  $("endplane-adjust-hint").classList.toggle("is-warning", Boolean(plane) && intersects === false);
  if (plane && !e.adjustments[e.mode]) {
    const anchor = e.viewer?.sectionPlane()?.plane || plane;
    e.adjustments[e.mode] = { anchor: cloneEndplane(anchor), offset: 0, tiltU: 0, tiltV: 0 };
  }
  const adjustment = e.adjustments[e.mode];
  for (const [id, key, unit] of [["offset", "offset", " mm"], ["tilt-u", "tiltU", "°"], ["tilt-v", "tiltV", "°"]]) {
    const input = $(`endplane-${id}`), value = adjustment?.[key] || 0;
    input.value = String(value);
    input.disabled = busy || !plane || Boolean(e.placement);
    $(`endplane-${id}-value`).textContent = `${value > 0 ? "+" : ""}${value}${unit}`;
  }
  for (const id of ["place", "focus", "reset-camera", "three-points", "gizmo", "show-removed"]) $("endplane-" + id).disabled = busy;
  $("endplane-align-view").disabled = busy || !plane;
  $("endplane-flip-normal").disabled = busy || !plane;
  $("endplane-undo").disabled = busy || (!e.editHistory.length && !e.pickedPoints.length);
  $("endplane-place").classList.toggle("is-on", e.placement === "point");
  $("endplane-place").textContent = e.placement === "point" && plane ? "取消点选" : "重新点选位置";
  $("endplane-place").setAttribute("aria-pressed", String(e.placement === "point"));
  $("endplane-three-points").classList.toggle("is-on", e.placement === "three");
  $("endplane-three-points").setAttribute("aria-pressed", String(e.placement === "three"));
  $("endplane-gizmo").checked = e.showGizmo;
  for (const tool of ["translate", "rotate"]) $("endplane-tool-" + tool).disabled = busy || !e.showGizmo || !plane;
  e.viewer?.setInteraction(!e.saving && e.selectionStatus === "ready");
  e.viewer?.setGizmo(e.showGizmo && $("endplane-advanced").open);
  e.viewer?.showPickedPoints(e.pickedPoints);
  if (plane) scheduleEndplaneSection();
  else {
    clearEndplaneSection();
    $("endplane-section-status").hidden = false;
    $("endplane-section-status").textContent = "放置切面后显示对应 CT";
    $("endplane-section-retry").hidden = true;
  }
}

function normalizedProfileDirection(profile) {
  const direction = new THREE.Vector3(...(profile?.ap_direction_lps || []));
  return direction.lengthSq() > 1e-10 ? direction.normalize() : new THREE.Vector3(1, 0, 0);
}

function planeNormalFromObject(object) {
  // The marker lies in XY with its front normal on local +Z. Three.js
  // getWorldDirection() reports the camera-facing -Z axis, which would flip
  // a user-rotated endplane and make the backend retain the wrong half-space.
  return new THREE.Vector3(0, 0, 1).applyQuaternion(object.getWorldQuaternion(new THREE.Quaternion())).normalize();
}

function endplaneClippingPlanes(planes) {
  return ["anterior", "posterior"].flatMap((side) => {
    const data = normalizedEndplane(planes?.[side]);
    if (!data) return [];
    // Three.js clips the negative side; the backend retains outward dot (x - point) <= 0.
    return [new THREE.Plane().setFromNormalAndCoplanarPoint(
      new THREE.Vector3(...data.normal_lps).negate(), new THREE.Vector3(...data.point_lps_mm),
    )];
  });
}

// STLLoader gives each triangle separate vertices. Share only their lighting normals,
// never their positions: picking, clipping intersections and exported geometry stay exact.
function preparePreviewShading(geometry) {
  geometry.computeVertexNormals();
  const position = geometry.getAttribute?.("position");
  const normal = geometry.getAttribute?.("normal");
  if (!position?.array || !normal?.array || geometry.index ||
      position.count !== normal.count || position.count % 3 !== 0) return null;
  const raw = normal.array.slice();
  const smooth = new Float32Array(raw.length);
  const groups = new Map();
  const coords = position.array;
  // Sub-voxel tolerance only joins vertices that represent the same STL corner.
  const key = (index) => {
    const offset = index * 3;
    return Math.round(coords[offset] * 10000) + "," +
      Math.round(coords[offset + 1] * 10000) + "," +
      Math.round(coords[offset + 2] * 10000);
  };
  for (let index = 0; index < position.count; index += 1) {
    const id = key(index);
    let sum = groups.get(id);
    if (!sum) { sum = [0, 0, 0]; groups.set(id, sum); }
    const offset = index * 3;
    sum[0] += raw[offset];
    sum[1] += raw[offset + 1];
    sum[2] += raw[offset + 2];
  }
  for (let index = 0; index < position.count; index += 1) {
    const offset = index * 3;
    const sum = groups.get(key(index));
    const length = Math.hypot(sum[0], sum[1], sum[2]);
    for (let axis = 0; axis < 3; axis += 1) {
      smooth[offset + axis] = length > 1e-8 ? sum[axis] / length : raw[offset + axis];
    }
  }
  return { raw, smooth, mode: "raw" };
}

function applyPreviewShading(geometry, shading, mode) {
  if (!shading || shading.mode === mode) return;
  const normal = geometry.getAttribute("normal");
  normal.array.set(mode === "smooth" ? shading.smooth : shading.raw);
  normal.needsUpdate = true;
  shading.mode = mode;
}

function setSurfacePreviewMode(mode) {
  state.previewMode = mode === "raw" ? "raw" : "smooth";
  for (const id of ["viewer-preview-mode", "endplane-preview-mode"]) {
    const input = $(id);
    if (input) input.value = state.previewMode;
  }
  state.viewer3d?.setPreviewMode(state.previewMode);
  state.endplanes.viewer?.setPreviewMode(state.previewMode);
}

function createEndplaneViewer(host) {
  const width = () => host.clientWidth || 640;
  const height = () => host.clientHeight || 420;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d1519);
  const camera = new THREE.PerspectiveCamera(40, width() / height(), 0.01, 10000);
  camera.up.set(0, 0, 1);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.localClippingEnabled = true;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(width(), height());
  host.replaceChildren(renderer.domElement);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x173036, 1.25));
  const key = new THREE.DirectionalLight(0xffffff, 1.05);
  key.position.set(1.4, 1.6, 1.2);
  scene.add(key);
  scene.add(new THREE.DirectionalLight(0x7ac7be, 0.42));
  const headlight = new THREE.DirectionalLight(0xffffff, 0.95);
  scene.add(headlight);

  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = true;
  const modelGroup = new THREE.Group();
  const planeGroup = new THREE.Group();
  const contourGroup = new THREE.Group();
  const pickedGroup = new THREE.Group();
  const seedGroup = new THREE.Group();
  const openingMarkupGroup = new THREE.Group();
  const lassoCanvas = $("endplane-port-lasso");
  let lassoPoints = [];
  let lassoPointerId = null;
  const contours = { anterior: [], posterior: [] };
  const planeIntersections = { anterior: false, posterior: false };
  scene.add(modelGroup, planeGroup, contourGroup, pickedGroup, seedGroup, openingMarkupGroup);
  const transform = typeof THREE.TransformControls === "function"
    ? new THREE.TransformControls(camera, renderer.domElement) : null;
  if (transform) scene.add(transform);
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const planeObjects = { anterior: null, posterior: null };
  let modelMesh = null;
  let removedMesh = null;
  let actualMesh = null;
  let modelShading = null;
  let actualShading = null;
  let animationId = null;
  let profile = null;
  let activeSide = "anterior";
  let pointerStart = null;
  let transformDrag = false;
  let frameCenter = new THREE.Vector3();
  let frameRadius = 100;
  let interactionEnabled = true;
  let gizmoEnabled = false;
  let showRemoved = true;
  let visualFormat = "oblique_planes_v1";

  function updatePointer(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / Math.max(rect.width, 1)) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / Math.max(rect.height, 1)) * 2 + 1;
  }

  function frame() {
    const box = new THREE.Box3().setFromObject(modelGroup);
    if (box.isEmpty()) return;
    frameCenter = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    frameRadius = Math.max(size.x, size.y, size.z) || 100;
    placeCamera(frameCenter, new THREE.Vector3(1.05, -1.55, -0.55).normalize());
    camera.near = Math.max(0.01, frameRadius / 10000);
    camera.far = Math.max(1000, frameRadius * 30);
    camera.updateProjectionMatrix();
  }

  function placeCamera(target, direction) {
    const bounds = new THREE.Box3().setFromObject(modelGroup);
    const u = camera.up.clone().cross(direction).normalize();
    const v = direction.clone().cross(u).normalize();
    const tangent = Math.tan(camera.fov * Math.PI / 360);
    let distance = frameRadius * 0.65;
    for (const x of [bounds.min.x, bounds.max.x]) for (const y of [bounds.min.y, bounds.max.y]) for (const z of [bounds.min.z, bounds.max.z]) {
      const offset = new THREE.Vector3(x, y, z).sub(target);
      distance = Math.max(distance, offset.dot(direction) + Math.max(Math.abs(offset.dot(u)) / (tangent * camera.aspect), Math.abs(offset.dot(v)) / tangent));
    }
    controls.target.copy(target);
    camera.position.copy(target).addScaledVector(direction, distance * 1.12);
  }

  function syncPlaneFromObject(side) {
    const object = planeObjects[side];
    if (!object || !object.visible) return;
    const position = object.getWorldPosition(new THREE.Vector3());
    const normal = planeNormalFromObject(object);
    object.userData.flow_direction_lps = endplaneFlowDirection(side, normal.toArray());
    state.endplanes.planes[side] = { point_lps_mm: position.toArray(), normal_lps: normal.toArray() };
    updateClipping();
  }

  function updateContours() {
    for (const child of [...contourGroup.children]) { contourGroup.remove(child); disposeObject(child); }
    const e = state.endplanes;
    const bilateralLocal = visualFormat === "oblique_bilateral_local_v3";
    planeIntersections.anterior = false;
    planeIntersections.posterior = false;
    const addContour = (segments, color, opacity = 0.95, side = null) => {
      if (!segments.length) return null;
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(segments, 3));
      const line = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({
        color, transparent: opacity < 1, opacity, depthTest: false,
      }));
      line.renderOrder = 8;
      line.userData = { kind: "endplane-section-outline", side };
      contourGroup.add(line);
      return segments;
    };
    const addSection = (segments, plane, side, color, opacity) => {
      if (!segments.length || !plane) return null;
      const triangles = window.EndplaneTools.sectionTriangles(segments, plane.normal_lps);
      if (!triangles.length) return null;
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(triangles, 3));
      geometry.computeVertexNormals();
      const surface = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
        color, transparent: true, opacity, side: THREE.DoubleSide, depthTest: false, depthWrite: false,
      }));
      surface.renderOrder = 7;
      surface.userData = { kind: "endplane-section", side, local: true };
      contourGroup.add(surface);
      return surface;
    };
    if (bilateralLocal) {
      const inletSegments = {};
      for (const side of ["left", "right"]) {
        const entry = e.bilateral[side];
        const active = e.mode === "anterior" && e.activeInlet === side;
        const plane = active ? e.planes.anterior : entry?.plane;
        const seed = active ? e.anteriorSeed : entry?.seed;
        const intersections = modelMesh && plane
          ? window.EndplaneTools.intersectionSegments(modelMesh.geometry, plane) : [];
        if (side === e.activeInlet) planeIntersections.anterior = intersections.length > 0;
        const target = validLpsVector(seed) ? seed : plane?.point_lps_mm;
        const local = validLpsVector(target)
          ? window.EndplaneTools.nearestIntersectionComponent(intersections, target) : [];
        inletSegments[side] = local;
        const color = side === "left" ? 0x69caff : 0x53d7c4;
        addSection(local, plane, side, color, active ? 0.24 : 0.12);
        addContour(local, color, active ? 0.98 : 0.56, side);
      }
      const activeSide = e.activeInlet === "right" ? "right" : "left";
      contours.anterior = inletSegments[activeSide] || [];
      const outletPlane = e.planes.posterior;
      const outletIntersections = modelMesh && outletPlane
        ? window.EndplaneTools.intersectionSegments(modelMesh.geometry, outletPlane) : [];
      planeIntersections.posterior = outletIntersections.length > 0;
      const outletTarget = validLpsVector(e.posteriorSeed) ? e.posteriorSeed : outletPlane?.point_lps_mm;
      contours.posterior = validLpsVector(outletTarget)
        ? window.EndplaneTools.nearestIntersectionComponent(outletIntersections, outletTarget) : [];
      addSection(contours.posterior, outletPlane, "posterior", 0xffb675, 0.24);
      addContour(contours.posterior, 0xffb675, 0.98, "posterior");
      return;
    }
    for (const side of ["anterior", "posterior"]) {
      const plane = state.endplanes.planes[side];
      let segments = modelMesh && plane ? window.EndplaneTools.intersectionSegments(
        modelMesh.geometry, plane, state.endplanes.planes[side === "anterior" ? "posterior" : "anterior"],
      ) : [];
      planeIntersections[side] = segments.length > 0;
      if (visualFormat === "oblique_local_inlet_v2") {
        const seed = side === "anterior" ? e.anteriorSeed : e.posteriorSeed;
        const target = validLpsVector(seed) ? seed : plane?.point_lps_mm;
        segments = validLpsVector(target)
          ? window.EndplaneTools.nearestIntersectionComponent(segments, target) : [];
      }
      contours[side] = segments;
      const color = side === "anterior" ? 0x69caff : 0xffb675;
      addSection(segments, plane, side, color, 0.24);
      addContour(segments, color, 0.98, side);
    }
  }

  function updateClipping(refreshContours = true) {
    if (!modelMesh) return;
    // v2 只在服务端删除选中支段；GPU 的无限平面不能充当局部裁切预览。
    const local = ["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(visualFormat);
    const planes = local ? [] : endplaneClippingPlanes(state.endplanes.planes);
    modelMesh.material.clippingPlanes = planes;
    modelMesh.material.needsUpdate = true;
    if (removedMesh) removedMesh.visible = !local && showRemoved && planes.length > 0;
    if (refreshContours) updateContours();
  }

  function syncActivePlane() {
    if (!planeObjects[activeSide]) return;
    syncPlaneFromObject(activeSide);
    state.endplanes.adjustments[activeSide] = null;
    updateEndplaneDraft(true);
  }

  function attachActivePlane() {
    if (!transform) return;
    const object = planeObjects[activeSide];
    if (object && gizmoEnabled && interactionEnabled && !state.endplanes.placement) {
      transform.attach(object);
      transform.setMode(state.endplanes.tool === "rotate" ? "rotate" : "translate");
      transform.visible = true;
    } else {
      transform.detach();
      transform.visible = false;
    }
  }

  function markerRadius(data) {
    const index = profile ? ctIndexFromPlane(profile, data) : null;
    const rawArea = index === null ? NaN : Number(profile?.areas_mm2?.[index]);
    const estimated = Number.isFinite(rawArea) && rawArea > 0
      ? Math.sqrt(rawArea / Math.PI)
      : frameRadius * 0.055;
    // The marker represents the local opening scale; it is deliberately capped so
    // it cannot hide the airway or recreate the old full-frame square.
    return Math.min(Math.max(estimated, frameRadius * 0.018), frameRadius * 0.11);
  }

  function markerPosition(side, data) {
    const position = new THREE.Vector3(...data.point_lps_mm);
    if (!modelMesh || !profile) return position;
    // A CT-index plane uses the CT origin as its reference point, often far
    // outside the airway. Moving its marker within the plane keeps the cut intact.
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(new THREE.Vector3(...data.normal_lps), position);
    const bounds = new THREE.Box3();
    const vertex = new THREE.Vector3();
    const segments = contours[side];
    for (let i = 0; i < segments.length; i += 3) bounds.expandByPoint(vertex.fromArray(segments, i));
    // An oblique polygon's axis-aligned bounding-box centre need not lie on
    // its plane. Project it back before using it for markers or CT sampling.
    if (!bounds.isEmpty()) return plane.projectPoint(bounds.getCenter(vertex), position);
    bounds.setFromObject(modelMesh);
    // Keep an explicitly picked point at the user's click. Only CT-origin
    // reference points outside the model need a display-only recentering.
    if (bounds.containsPoint(position)) return position;
    if (!bounds.isEmpty() && plane.intersectsBox(bounds)) plane.projectPoint(bounds.getCenter(vertex), position);
    return position;
  }

  function makePlane(side, data) {
    const color = side === "posterior" ? 0xff9c55
      : visualFormat === "oblique_bilateral_local_v3" && state.endplanes.activeInlet === "right" ? 0x53d7c4 : 0x36a9ff;
    const radius = markerRadius(data);
    const group = new THREE.Group();
    group.quaternion.setFromUnitVectors(
      new THREE.Vector3(0, 0, 1), new THREE.Vector3(...data.normal_lps).normalize(),
    );
    const anchor = new THREE.Mesh(new THREE.SphereGeometry(radius * 0.1, 12, 8), new THREE.MeshBasicMaterial({ color, depthTest: false }));
    anchor.renderOrder = 6;

    const arrowLength = Math.max(radius * 1.8, frameRadius * 0.045);
    const flowDirection = endplaneFlowDirection(side, data.normal_lps);
    const localFlowDirection = new THREE.Vector3(...flowDirection)
      .applyQuaternion(group.quaternion.clone().invert())
      .normalize();
    const arrow = new THREE.ArrowHelper(
      localFlowDirection, new THREE.Vector3(0, 0, 0),
      arrowLength, color, Math.max(radius * 0.48, frameRadius * 0.012), Math.max(radius * 0.32, frameRadius * 0.009),
    );
    // ArrowHelper shares its default geometries; these clones belong to this marker.
    arrow.line.geometry = arrow.line.geometry.clone();
    arrow.cone.geometry = arrow.cone.geometry.clone();
    arrow.renderOrder = 7;
    arrow.line.material.transparent = true;
    arrow.line.material.opacity = 0.92;
    arrow.line.material.depthTest = false;
    arrow.cone.material.transparent = true;
    arrow.cone.material.opacity = 0.98;
    arrow.cone.material.depthTest = false;

    group.add(anchor, arrow);
    group.renderOrder = 5;
    group.userData = { side, radius_mm: radius, kind: "endplane-marker", flow_direction_lps: flowDirection };
    group.position.copy(markerPosition(side, data));
    planeGroup.add(group);
    return group;
  }

  function disposeObject(object) {
    object.traverse((child) => {
      child.geometry?.dispose();
      if (child.material) {
        const materials = Array.isArray(child.material) ? child.material : [child.material];
        materials.forEach((material) => material.dispose());
      }
    });
  }

  function setPlanes(planes, side = activeSide) {
    activeSide = side === "posterior" ? "posterior" : "anterior";
    updateContours();
    showPickedPoints(state.endplanes.pickedPoints);
    showSeedMarker(currentEndplaneSeed());
    if (transform) {
      transform.detach();
      transform.visible = false;
    }
    for (const key of ["anterior", "posterior"]) {
      const existing = planeObjects[key];
      const data = planes?.[key];
      if (existing) {
        planeGroup.remove(existing);
        disposeObject(existing);
        planeObjects[key] = null;
      }
      if (data && validLpsVector(data.point_lps_mm) && validLpsVector(data.normal_lps)) {
        planeObjects[key] = makePlane(key, normalizedEndplane(data));
      }
    }
    attachActivePlane();
    updateClipping(false);
  }

  function setActiveSide(side) {
    activeSide = side === "posterior" ? "posterior" : "anterior";
    showPickedPoints([]);
    showSeedMarker(currentEndplaneSeed());
    attachActivePlane();
  }

  function outwardNormal() {
    const direction = normalizedProfileDirection(profile);
    const outward = (activeSide === "anterior" ? 1 : -1) * Number(profile?.anterior_sign || 1);
    return direction.multiplyScalar(outward > 0 ? 1 : -1);
  }

  function viewNormal() {
    const normal = camera.getWorldDirection(new THREE.Vector3()).negate().normalize();
    const outward = outwardNormal();
    // An edge-on view cannot define a usable inlet/outlet direction.
    if (Math.abs(normal.dot(outward)) < 0.15) return outward;
    return normal.dot(outward) < 0 ? normal.negate() : normal;
  }

  function focusActive() {
    if (!modelMesh) return;
    const object = planeObjects[activeSide];
    const normal = object ? planeNormalFromObject(object) : outwardNormal().add(new THREE.Vector3(0, 0, -0.45)).normalize();
    placeCamera(object?.position || frameCenter, normal);
    controls.update();
  }

  function sectionPlane() {
    const object = planeObjects[activeSide];
    const plane = state.endplanes.planes[activeSide];
    if (!object || !plane) return null;
    const actual = new THREE.Plane().setFromNormalAndCoplanarPoint(
      new THREE.Vector3(...plane.normal_lps).normalize(), new THREE.Vector3(...plane.point_lps_mm));
    const center = actual.projectPoint(object.position, new THREE.Vector3());
    return { plane: { point_lps_mm: center.toArray(), normal_lps: [...plane.normal_lps] }, span_mm: Math.min(400, Math.max(10, frameRadius * 2.2)) };
  }

  function showPickedPoints(points = []) {
    for (const child of [...pickedGroup.children]) { pickedGroup.remove(child); disposeObject(child); }
    for (const point of points) {
      const marker = new THREE.Mesh(new THREE.SphereGeometry(frameRadius * 0.012, 12, 8), new THREE.MeshBasicMaterial({ color: 0xffffff, depthTest: false }));
      marker.position.fromArray(point);
      marker.renderOrder = 10;
      pickedGroup.add(marker);
    }
  }

  function showSeedMarker(point) {
    for (const child of [...seedGroup.children]) { seedGroup.remove(child); disposeObject(child); }
    if (["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(visualFormat) && validLpsVector(point)) {
      const marker = new THREE.Mesh(
        new THREE.SphereGeometry(Math.max(frameRadius * 0.018, 0.5), 16, 12),
        new THREE.MeshBasicMaterial({ color: 0xffd166, depthTest: false }),
      );
      marker.position.fromArray(point);
      marker.renderOrder = 11;
      seedGroup.add(marker);
    }
    updateContours();
  }

  function showOpeningMarkup(markup) {
    for (const child of [...openingMarkupGroup.children]) {
      openingMarkupGroup.remove(child);
      disposeObject(child);
    }
    const positions = modelMesh?.geometry.getAttribute("position")?.array;
    const faceIds = markup?.selected_face_ids;
    if (positions && Array.isArray(faceIds) && faceIds.length) {
      const selected = new Float32Array(faceIds.length * 9);
      let count = 0;
      for (const id of faceIds) {
        if (!Number.isInteger(id) || id < 0 || id * 9 + 8 >= positions.length) continue;
        selected.set(positions.subarray(id * 9, id * 9 + 9), count++ * 9);
      }
      if (count) {
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute("position", new THREE.BufferAttribute(selected.slice(0, count * 9), 3));
        geometry.computeVertexNormals();
        const patch = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
          color: markup.side === "right" ? 0x53d7c4 : markup.side === "posterior" ? 0xffb675 : 0x69caff,
          side: THREE.DoubleSide, transparent: true, opacity: 0.72, depthWrite: false,
          polygonOffset: true, polygonOffsetFactor: -2,
        }));
        patch.renderOrder = 6;
        patch.userData = { kind: "opening-draft-patch", opening_id: markup.opening_id, face_ids: faceIds };
        openingMarkupGroup.add(patch);
      }
    }
    const seed = validLpsVector(markup?.seed_lps_mm) ? markup.seed_lps_mm : null;
    if (seed) {
      const marker = new THREE.Mesh(
        new THREE.SphereGeometry(Math.max(frameRadius * 0.022, 0.6), 16, 12),
        new THREE.MeshBasicMaterial({ color: 0xffa94d, depthTest: false }),
      );
      marker.position.fromArray(seed);
      marker.renderOrder = 12;
      marker.userData = { kind: "opening-markup-seed", opening_id: markup.opening_id };
      openingMarkupGroup.add(marker);
    }
    const outline = Array.isArray(markup?.boundary_loop_lps_mm)
      ? markup.boundary_loop_lps_mm.filter(validLpsVector) : [];
    if (outline.length >= 2) {
      const geometry = new THREE.BufferGeometry();
      const points = outline.map((point) => new THREE.Vector3(...point));
      if (outline.length >= 3 && !points[0].equals(points[points.length - 1])) points.push(points[0].clone());
      geometry.setFromPoints(points);
      const line = new THREE.Line(geometry, new THREE.LineBasicMaterial({
        color: 0xffa94d, transparent: true, opacity: 0.95, depthTest: false,
      }));
      line.renderOrder = 12;
      line.userData = { kind: "opening-markup-outline", opening_id: markup.opening_id };
      openingMarkupGroup.add(line);
    }
  }

  function drawOpeningLasso() {
    const rect = lassoCanvas.getBoundingClientRect();
    const scale = Math.min(window.devicePixelRatio || 1, 2);
    lassoCanvas.width = Math.max(1, Math.round(rect.width * scale));
    lassoCanvas.height = Math.max(1, Math.round(rect.height * scale));
    const ctx = lassoCanvas.getContext("2d");
    ctx.scale(scale, scale);
    if (lassoPoints.length < 2) return;
    ctx.beginPath();
    ctx.moveTo(...lassoPoints[0]);
    for (const point of lassoPoints.slice(1)) ctx.lineTo(...point);
    ctx.strokeStyle = "#ffdc84";
    ctx.lineWidth = 2.5;
    ctx.setLineDash([6, 4]);
    ctx.stroke();
  }

  function setOpeningMode(enabled) {
    lassoCanvas.hidden = !(enabled && interactionEnabled && modelMesh);
    controls.enabled = interactionEnabled && !transformDrag && lassoCanvas.hidden;
    if (lassoCanvas.hidden) { lassoPoints = []; lassoPointerId = null; }
    drawOpeningLasso();
  }

  function captureOpeningPatch(polygon) {
    const markup = currentOpeningMarkup(openingMarkupSide());
    const positions = modelMesh?.geometry.getAttribute("position")?.array;
    if (!positions || !Number.isInteger(markup.seed_face_id)) return;
    if (modelMesh.geometry.index) {
      toast("当前选择模型不是原始非索引 STL，无法可靠定位面 ID", true);
      return;
    }
    const rect = lassoCanvas.getBoundingClientRect();
    const faceCount = positions.length / 9;
    const projectedPoints = new Float32Array(faceCount * 2);
    const projectedTriangles = new Float32Array(faceCount * 9);
    const nearbyFaceIds = new Set();
    const seed = new THREE.Vector3(...markup.seed_lps_mm);
    const distance = camera.position.distanceTo(seed);
    const pixelMm = 2 * distance * Math.tan(camera.fov * Math.PI / 360) / Math.max(rect.height, 1);
    const xs = polygon.map(point => point[0]), ys = polygon.map(point => point[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const radiusMm = Math.max(3, Math.min(frameRadius * 0.22, Math.hypot(x1-x0, y1-y0) * pixelMm * 0.8 + 2));
    const center = new THREE.Vector3(), vertex = new THREE.Vector3();
    camera.updateMatrixWorld(true);
    modelMesh.updateMatrixWorld(true);
    for (let id = 0; id < faceCount; id += 1) {
      const offset = id * 9;
      center.set((positions[offset] + positions[offset+3] + positions[offset+6]) / 3,
        (positions[offset+1] + positions[offset+4] + positions[offset+7]) / 3,
        (positions[offset+2] + positions[offset+5] + positions[offset+8]) / 3)
        .applyMatrix4(modelMesh.matrixWorld);
      if (center.distanceTo(seed) <= radiusMm || id === markup.seed_face_id) nearbyFaceIds.add(id);
      vertex.copy(center).project(camera);
      const x = (vertex.x + 1) * rect.width / 2, y = (1 - vertex.y) * rect.height / 2;
      projectedPoints[id * 2] = x;
      projectedPoints[id * 2 + 1] = y;
      for (let corner = 0; corner < 3; corner += 1) {
        const start = offset + corner * 3;
        vertex.set(positions[start], positions[start + 1], positions[start + 2])
          .applyMatrix4(modelMesh.matrixWorld).project(camera);
        const target = offset + corner * 3;
        projectedTriangles[target] = (vertex.x + 1) * rect.width / 2;
        projectedTriangles[target + 1] = (1 - vertex.y) * rect.height / 2;
        projectedTriangles[target + 2] = vertex.z;
      }
    }
    const visible = window.EndplaneOpeningMarkup.visibleFaceIds({
      projectedTriangles, projectedPoints, polygon,
    });
    const eligibleFaceIds = visible.eligibleFaceIds.filter(id => nearbyFaceIds.has(id));
    const patch = visible.quality_errors.length ? {
      ok: false, selected_face_ids: [], boundary_loop_lps_mm: [],
      area_mm2: null, centroid_lps_mm: null, normal_lps: null,
      plane: null, max_plane_deviation_mm: null,
      quality_errors: visible.quality_errors,
    } : window.EndplaneOpeningMarkup.buildPortPatch({
      positions, projectedPoints, polygon, seedFaceId: markup.seed_face_id, eligibleFaceIds,
    });
    state.endplanes.editHistory.push({ kind: "opening-markup", side: markup.side, markup });
    if (state.endplanes.editHistory.length > 40) state.endplanes.editHistory.shift();
    setOpeningMarkup(markup.side, {
      ...markup, ...patch, seed: markup.seed_lps_mm, seed_face_id: markup.seed_face_id,
      outline_lps_mm: [], mask_version: endplaneContext()?.version,
    });
    state.endplanes.placement = null;
    renderEndplaneUi();
  }

  const lassoPoint = (event) => {
    const rect = lassoCanvas.getBoundingClientRect();
    return [event.clientX - rect.left, event.clientY - rect.top];
  };
  lassoCanvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || lassoCanvas.hidden) return;
    lassoPointerId = event.pointerId;
    lassoPoints = [lassoPoint(event)];
    lassoCanvas.setPointerCapture(event.pointerId);
    drawOpeningLasso();
  });
  lassoCanvas.addEventListener("pointermove", (event) => {
    if (event.pointerId !== lassoPointerId) return;
    const point = lassoPoint(event), last = lassoPoints.at(-1);
    if (Math.hypot(point[0] - last[0], point[1] - last[1]) < 3) return;
    lassoPoints.push(point);
    drawOpeningLasso();
  });
  lassoCanvas.addEventListener("pointerup", (event) => {
    if (event.pointerId !== lassoPointerId) return;
    if (lassoCanvas.hasPointerCapture(event.pointerId)) lassoCanvas.releasePointerCapture(event.pointerId);
    lassoPointerId = null;
    const polygon = lassoPoints.slice();
    lassoPoints = [];
    drawOpeningLasso();
    if (polygon.length < 3) { toast("请沿完整入口边界拖动画圈", true); return; }
    captureOpeningPatch(polygon);
  });
  lassoCanvas.addEventListener("pointercancel", () => { lassoPoints = []; lassoPointerId = null; drawOpeningLasso(); });

  function hitModel(event) {
    updatePointer(event);
    camera.updateMatrixWorld(true);
    modelMesh.updateMatrixWorld(true);
    raycaster.setFromCamera(pointer, camera);
    const clippingPlanes = ["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(visualFormat)
      ? [] : endplaneClippingPlanes(state.endplanes.planes);
    const tolerance = Math.max(frameRadius * 1e-5, 0.01);
    return raycaster.intersectObject(modelMesh, false).find(({ point }) =>
      clippingPlanes.every((plane) => plane.distanceToPoint(point) >= -tolerance)) || null;
  }

  function handleModelClick(event) {
    if (!modelMesh || !profile || !interactionEnabled || !state.endplanes.placement || transformDrag || !pointerStart) return;
    if (visualFormat === "oblique_planes_v1" && profile.supported_formats?.includes("oblique_local_inlet_v2")) return;
    const distance = Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y);
    if (distance > 5 || pointerStart.gizmo) return;
    updatePointer(event);
    // OrbitControls 更新相机后，点击事件可能先于下一帧渲染触发；射线检测必须使用最新世界矩阵。
    camera.updateMatrixWorld(true);
    modelMesh.updateMatrixWorld(true);
    raycaster.setFromCamera(pointer, camera);
    if (state.endplanes.placement === "seed") {
      const plane = normalizedEndplane(state.endplanes.planes[activeSide]);
      if (!plane || !["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(visualFormat) ||
          (visualFormat === "oblique_local_inlet_v2" && activeSide !== "anterior")) return;
      const hit = raycaster.intersectObject(modelMesh, false)[0];
      const distanceFromPlane = hit && plane.normal_lps.reduce((sum, normal, axis) =>
        sum + normal * (hit.point.getComponent(axis) - plane.point_lps_mm[axis]), 0);
      if (!hit || distanceFromPlane <= 0.1) {
        toast("可见表面不在当前切面外侧；请转动模型后点目标开口", true);
        return;
      }
      state.endplanes.editHistory.push({
        kind: "seed", side: activeSide, seed: currentEndplaneSeed() ? [...currentEndplaneSeed()] : null,
      });
      if (state.endplanes.editHistory.length > 40) state.endplanes.editHistory.shift();
      setCurrentEndplaneSeed(hit.point.toArray());
      state.endplanes.placement = null;
      showSeedMarker(currentEndplaneSeed());
      updateEndplaneDraft(true);
      return;
    }
    if (state.endplanes.placement === "opening-seed") {
      const hit = hitModel(event);
      if (!hit?.point) return;
      const side = openingMarkupSide();
      state.endplanes.editHistory.push({ kind: "opening-markup", side, markup: currentOpeningMarkup(side) });
      if (state.endplanes.editHistory.length > 40) state.endplanes.editHistory.shift();
      setOpeningMarkup(side, { seed: hit.point.toArray(), seed_face_id: hit.faceIndex });
      state.endplanes.placement = null;
      showOpeningMarkup(currentOpeningMarkup(side));
      renderEndplaneUi();
      return;
    }
    if (state.endplanes.placement === "opening-outline") return;
    const hit = hitModel(event);
    if (!hit || !hit.point) return;
    if (state.endplanes.placement === "three") {
      state.endplanes.pickedPoints.push(hit.point.toArray());
      showPickedPoints(state.endplanes.pickedPoints);
      if (state.endplanes.pickedPoints.length === 3) {
        const plane = window.EndplaneTools.throughPoints(state.endplanes.pickedPoints, outwardNormal().toArray());
        if (!plane) {
          state.endplanes.pickedPoints.pop();
          toast("这三点太近或接近一条直线，请换一个更分散的第三点", true);
        } else commitEndplaneEdit(plane);
      }
    } else commitEndplaneEdit({ point_lps_mm: hit.point.toArray(), normal_lps: viewNormal().toArray() });
    renderEndplaneUi();
    window.WindcatcherMonitor?.record("viewer", "plane_selected", { mode: activeSide, dirty: true }, "endplane-model");
  }

  renderer.domElement.addEventListener("pointerdown", (event) => {
    pointerStart = { x: event.clientX, y: event.clientY, gizmo: Boolean(transform?.dragging) };
  });
  renderer.domElement.addEventListener("pointerup", (event) => {
    const start = pointerStart;
    window.setTimeout(() => {
      if (!start || start !== pointerStart) return;
      handleModelClick(event);
      pointerStart = null;
    }, 0);
  });
  renderer.domElement.addEventListener("pointercancel", () => {
    pointerStart = null;
    controls.enabled = interactionEnabled && lassoCanvas.hidden;
  });
  if (transform) {
    transform.addEventListener("dragging-changed", (event) => {
      transformDrag = Boolean(event.value);
      controls.enabled = interactionEnabled && !transformDrag && lassoCanvas.hidden;
      if (transformDrag) rememberEndplaneEdit();
      if (!transformDrag) {
        syncActivePlane();
        window.WindcatcherMonitor?.record("viewer", "plane_adjusted", {
          mode: activeSide, tool: transform.getMode(), dirty: state.endplanes.dirty,
          controls: state.endplanes.planes[activeSide]?.normal_lps,
        }, "endplane-model");
      }
    });
    transform.addEventListener("objectChange", () => {
      if (transformDrag) syncPlaneFromObject(activeSide);
    });
  }

  function clear() {
    if (transform) { transform.detach(); transform.visible = false; }
    clearActualPreview();
    if (removedMesh) {
      modelGroup.remove(removedMesh);
      removedMesh.material.dispose();
      removedMesh = null;
    }
    if (modelMesh) {
      modelGroup.remove(modelMesh);
      modelMesh.geometry.dispose();
      modelMesh.material.dispose();
      modelMesh = null;
    }
    modelShading = null;
    setPlanes({});
    showPickedPoints([]);
    showSeedMarker(null);
    showOpeningMarkup(null);
    setOpeningMode(false);
  }

  function loadGeometry(geometry) {
    clear();
    modelShading = preparePreviewShading(geometry);
    applyPreviewShading(geometry, modelShading, state.previewMode);
    const material = new THREE.MeshPhysicalMaterial({
      color: 0x68c8ba, metalness: 0, roughness: state.previewMode === "smooth" ? 0.55 : 0.3,
      transmission: 0.1, transparent: true, opacity: 0.82,
      side: THREE.DoubleSide,
    });
    modelMesh = new THREE.Mesh(geometry, material);
    modelMesh.renderOrder = 1;
    modelGroup.add(modelMesh);
    removedMesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
      color: 0xa8afb2, transparent: true, opacity: 0.12,
      side: THREE.DoubleSide, depthWrite: false,
    }));
    removedMesh.renderOrder = 0;
    modelGroup.add(removedMesh);
    frame();
    setPlanes(state.endplanes.planes, activeSide);
    focusActive();
    showOpeningMarkup(currentOpeningMarkup());
  }

  function clearActualPreview() {
    if (actualMesh) {
      modelGroup.remove(actualMesh);
      disposeObject(actualMesh);
      actualMesh = null;
    }
    actualShading = null;
    if (modelMesh) {
      modelMesh.visible = true;
      modelMesh.material.color.setHex(0x68c8ba);
      modelMesh.material.opacity = 0.82;
    }
  }

  function setActualPreviewGeometry(geometry) {
    clearActualPreview();
    actualShading = preparePreviewShading(geometry);
    applyPreviewShading(geometry, actualShading, state.previewMode);
    actualMesh = new THREE.Mesh(geometry, new THREE.MeshPhysicalMaterial({
      color: 0x68c8ba, metalness: 0, roughness: state.previewMode === "smooth" ? 0.55 : 0.3,
      transparent: true, opacity: 0.9, side: THREE.DoubleSide,
    }));
    actualMesh.renderOrder = 3;
    modelGroup.add(actualMesh);
    if (modelMesh) {
      modelMesh.visible = showRemoved;
      modelMesh.material.color.setHex(0xa8afb2);
      modelMesh.material.opacity = 0.12;
    }
  }

  function resize() {
    camera.aspect = width() / Math.max(height(), 1);
    camera.updateProjectionMatrix();
    renderer.setSize(width(), height());
    if (!lassoCanvas.hidden) drawOpeningLasso();
    if (modelMesh) {
      placeCamera(controls.target.clone(), camera.position.clone().sub(controls.target).normalize());
      controls.update();
    }
  }

  function resetCamera() { frame(); controls.update(); }

  function animate() {
    animationId = requestAnimationFrame(animate);
    controls.update();
    headlight.position.copy(camera.position);
    headlight.target.position.copy(controls.target);
    headlight.target.updateMatrixWorld();
    renderer.render(scene, camera);
  }
  function pause() {
    if (animationId !== null) cancelAnimationFrame(animationId);
    animationId = null;
  }
  function resume() { if (animationId === null) animate(); }
  resume();
  window.addEventListener("resize", resize);
  if (typeof ResizeObserver === "function") new ResizeObserver(resize).observe(host);

  return {
    clear,
    loadGeometry,
    resize,
    pause,
    resume,
    resetCamera,
    focusActive,
    sectionPlane,
    showPickedPoints,
    showOpeningMarkup,
    setOpeningMode,
    hasIntersection() { return planeIntersections[activeSide]; },
    setInteraction(enabled) {
      interactionEnabled = enabled;
      controls.enabled = enabled && !transformDrag && lassoCanvas.hidden;
    },
    setGizmo(enabled) { gizmoEnabled = enabled; attachActivePlane(); },
    setShowRemoved(enabled) {
      showRemoved = enabled;
      if (actualMesh && modelMesh) modelMesh.visible = enabled;
      updateClipping(false);
    },
    clearActualPreview,
    setActualPreviewGeometry,
    setPreviewMode(mode) {
      if (modelMesh) {
        applyPreviewShading(modelMesh.geometry, modelShading, mode);
        modelMesh.material.roughness = mode === "smooth" ? 0.55 : 0.3;
      }
      if (actualMesh) {
        applyPreviewShading(actualMesh.geometry, actualShading, mode);
        actualMesh.material.roughness = mode === "smooth" ? 0.55 : 0.3;
      }
    },
    setFormat(value) {
      const next = ["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(value) ? value : "oblique_planes_v1";
      if (visualFormat === next) return;
      clearActualPreview();
      visualFormat = next;
      updateClipping();
      showSeedMarker(currentEndplaneSeed());
    },
    showSeedMarker,
    alignToView() {
      const plane = state.endplanes.planes[activeSide];
      if (plane) commitEndplaneEdit({ point_lps_mm: planeObjects[activeSide].position.toArray(), normal_lps: viewNormal().toArray() });
    },
    setActiveSide,
    setPlanes,
    setProfile(value) { profile = value; },
    setTool(mode) { if (transform) transform.setMode(mode === "rotate" ? "rotate" : "translate"); },
  };
}

async function loadEndplaneSelectionModel(context = endplaneContext()) {
  const e = state.endplanes;
  if (!context || !e.open || e.status !== "ready") return;
  if (e.selectionKey === context.key && e.selectionStatus === "ready") return;
  const requestId = ++e.selectionRequestId;
  e.selectionAbort?.abort();
  const controller = new AbortController();
  e.selectionAbort = controller;
  e.selectionKey = context.key;
  e.selectionStatus = "loading";
  e.selectionError = null;
  if (!e.viewer) e.viewer = createEndplaneViewer($("endplane-3d"));
  e.viewer.setProfile(e.profile);
  e.viewer.clear();
  const overlay = $("endplane-3d-overlay");
  if (overlay) { overlay.hidden = false; overlay.textContent = "正在按原始体素分辨率重建 3D 气道，请稍候…"; }
  renderEndplaneUi();
  try {
    const query = new URLSearchParams({ scope: context.scope, mask_version: String(context.version) });
    const response = await fetch(`/api/cases/${encodeURIComponent(context.caseId)}/segment/selection-model?${query}`, { signal: controller.signal });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      const detail = payload?.detail || {};
      throw Object.assign(new Error(detail.reason || "3D 选择模型读取失败"), { detail });
    }
    const buffer = await response.arrayBuffer();
    if (e.selectionRequestId !== requestId || endplaneContext()?.key !== context.key) return;
    const geometry = new THREE.STLLoader().parse(buffer);
    e.viewer.loadGeometry(geometry);
    e.viewer.setPlanes(e.planes, e.mode);
    e.selectionStatus = "ready";
    if (e.planes.anterior && e.planes.posterior) scheduleEndplanePreview();
    if (overlay) overlay.hidden = true;
  } catch (error) {
    if (error.name === "AbortError" || e.selectionRequestId !== requestId) return;
    e.selectionStatus = "error";
    e.selectionError = error.detail?.reason || error.message || "3D 选择模型读取失败";
    if (overlay) { overlay.hidden = false; overlay.textContent = e.selectionError; }
  } finally {
    if (e.selectionRequestId === requestId) {
      e.selectionAbort = null;
      renderEndplaneUi();
    }
  }
}

function setEndplaneIndex(side, index, preserveNumber = null) {
  const e = state.endplanes;
  if (e.status !== "ready" || e.saving ||
      (e.format === "oblique_planes_v1" && e.profile?.supported_formats?.includes("oblique_local_inlet_v2")) ||
      (e.format === "oblique_local_inlet_v2" && e.profile?.supported_formats?.includes("oblique_bilateral_local_v3"))) return;
  const previousMode = e.mode;
  e.mode = side;
  rememberEndplaneEdit();
  e.adjustments[side] = null;
  e.placement = null;
  e.pickedPoints = [];
  e.values[side] = index;
  // CT 精确调整仍然使用同一组平面数据；这样从滑块改动后保存的方案
  // 与 3D 视图、预检和后端裁切保持一致。
  e.planes[side] = e.profile ? planeFromCtIndex(e.profile, side, index) : null;
  updateEndplaneDirty();
  e.error = null;
  e.pairError = null;
  clearEndplanePreview();
  e.viewer?.setPlanes(e.planes, e.mode);
  if (e.planes.anterior && e.planes.posterior) scheduleEndplanePreview();
  renderEndplaneUi(preserveNumber);
  if (previousMode !== side) e.viewer?.focusActive();
}

async function jumpToEndplane(side) {
  const e = state.endplanes;
  const profile = e.profile;
  const index = e.values[side];
  const caseId = state.caseId;
  if (!profile || index === null || e.saving) return;
  state.mode = "slices";
  refreshViewer();
  if (state.ctViewer.metaPromise) await state.ctViewer.metaPromise;
  if (state.caseId !== caseId || !state.ctViewer.meta?.views?.[profile.view]) {
    toast("CT 层面尚未加载，请稍后重试", true);
    return;
  }
  loadCtSlice(profile.view, index);
  focusStageTarget($("viewer-card"), $(`slice-range-${profile.view}`));
}

async function saveSingleEndplane(side) {
  const e = state.endplanes;
  const context = endplaneContext();
  if (!context || !["anterior", "posterior"].includes(side) || e.status !== "ready" ||
      e.profile?.can_save === false || e.saving || !e.dirtySides[side] || e.format !== "oblique_planes_v1" ||
      e.profile?.supported_formats?.includes("oblique_local_inlet_v2")) return;
  const validation = endplaneValidation(side);
  if (validation) { e.error = validation; renderEndplaneUi(); return; }
  const plane = normalizedEndplane(e.planes[side]);
  if (!plane) { e.error = `${side === "anterior" ? "鼻孔端" : "鼻咽端"}尚未放置`; renderEndplaneUi(); return; }
  e.saving = true;
  e.error = null;
  renderEndplaneUi();
  let committed = false;
  try {
    const result = await api(`/api/cases/${encodeURIComponent(context.caseId)}/segment/endplanes/${side}`, {
      method: "POST",
      body: {
        scope: context.scope,
        mask_version: context.version,
        plane,
      },
    });
    if (result.scope !== context.scope || result.mask_version !== context.version ||
        result.format !== "oblique_planes_v1" || !normalizedEndplane(result[`${side}_plane`])) {
      throw new Error("服务端没有返回可核对的当前端面保存结果");
    }
    committed = true;
    if (state.caseId !== context.caseId || endplaneContext()?.key !== context.key) return;
    const savedPlane = normalizedEndplane(result[`${side}_plane`]);
    e.profile = { ...e.profile, saved: result };
    e.baselinePlanes[side] = savedPlane;
    e.planes[side] = cloneEndplane(savedPlane);
    e.values[side] = ctIndexFromPlane(e.profile, savedPlane);
    e.baseline[side] = e.values[side];
    e.pairError = result.validation_error?.reason || null;
    e.stale = false;
    updateEndplaneDirty();
    clearEndplanePreview();
    e.viewer?.setPlanes(e.planes, e.mode);
    if (e.planes.anterior && e.planes.posterior) scheduleEndplanePreview();
    state.viewer3dCaseId = null;
    state.viewer3dKey = null;
    state.viewer3d?.clear();
    const refreshed = await api(`/api/cases/${encodeURIComponent(context.caseId)}`);
    if (state.caseId !== context.caseId || endplaneContext()?.key !== context.key) return;
    state.case = refreshed;
    renderCase();
    const label = side === "anterior" ? "鼻孔端" : "鼻咽端";
    const other = side === "anterior" ? "posterior" : "anterior";
    if (e.dirtySides[other]) {
      toast(`${label}已保存；另一端仍有未保存更改，请单独保存另一端`);
    } else if (result.complete === true && !result.validation_error) {
      toast(`${label}已保存；两端联合预检通过，可以生成 STL`);
    } else if (result.validation_error) {
      toast(`${label}已保存，但两端存在冲突，请调整并保存冲突的一端`, true);
    } else {
      toast(`${label}已保存；请继续选择并保存另一端`);
    }
    e.history[side] = [];
    e.editHistory = e.editHistory.filter((item) => item.side !== side);
    e.adjustments[side] = null;
    if (side === "anterior") selectEndplaneSide("posterior", true);
  } catch (error) {
    if (state.caseId !== context.caseId || e.key !== context.key) return;
    e.error = committed ? `位置已保存，但刷新状态失败：${error.detail?.reason || error.message}` :
      error.detail?.reason || error.message || "保存端面位置失败";
    renderEndplaneUi();
    toast(e.error, true);
  } finally {
    if (state.caseId === context.caseId && e.key === context.key) {
      e.saving = false;
      renderEndplaneUi();
    }
  }
}

async function saveBilateralEndplanes() {
  const e = state.endplanes;
  const context = endplaneContext();
  const body = endplaneBody(context);
  const signature = endplaneSignature();
  if (!context || body?.format !== "oblique_bilateral_local_v3" || e.status !== "ready" ||
      e.profile?.can_save === false || e.saving || !e.dirty || endplaneValidation() ||
      e.previewStatus !== "ok" || e.previewSignature !== signature ||
      e.previewData?.format !== body.format || e.previewData?.can_preview_mesh !== true ||
      e.actualPreviewStatus !== "ok" || e.actualPreviewSignature !== signature) return;
  e.saving = true;
  e.error = null;
  renderEndplaneUi();
  let committed = false;
  try {
    const result = await api("/api/cases/" + encodeURIComponent(context.caseId) + "/segment/endplanes", {
      method: "POST", body,
    });
    if (result.scope !== context.scope || result.mask_version !== context.version ||
        result.format !== body.format || result.complete !== true || result.validation_error ||
        !normalizedEndplane(result.left_inlet_plane) || !normalizedEndplane(result.right_inlet_plane) ||
        !normalizedEndplane(result.posterior_plane) ||
        !validLpsVector(result.left_inlet_seed_lps_mm) ||
        !validLpsVector(result.right_inlet_seed_lps_mm) ||
        !validLpsVector(result.posterior_seed_lps_mm)) {
      throw new Error("服务端没有返回可核对的双侧三端面保存结果");
    }
    committed = true;
    if (state.caseId !== context.caseId || endplaneContext()?.key !== context.key ||
        endplaneSignature() !== signature) return;
    e.profile = { ...e.profile, saved: result };
    e.baselineFormat = e.format = result.format;
    e.baselineBilateral = cloneBilateralDraft({
      left: { plane: result.left_inlet_plane, seed: result.left_inlet_seed_lps_mm },
      right: { plane: result.right_inlet_plane, seed: result.right_inlet_seed_lps_mm },
    });
    e.bilateral = cloneBilateralDraft(e.baselineBilateral);
    e.posteriorSeedPlane = null;
    e.baselinePosteriorSeed = [...result.posterior_seed_lps_mm];
    e.posteriorSeed = [...e.baselinePosteriorSeed];
    e.baselinePlanes = {
      anterior: cloneEndplane(e.baselineBilateral[e.activeInlet].plane),
      posterior: normalizedEndplane(result.posterior_plane),
    };
    e.planes = cloneEndplanePair(e.baselinePlanes);
    e.baselineAnteriorSeed = [...e.baselineBilateral[e.activeInlet].seed];
    e.anteriorSeed = [...e.baselineAnteriorSeed];
    for (const side of ["anterior", "posterior"]) {
      e.values[side] = ctIndexFromPlane(e.profile, e.planes[side]);
      e.baseline[side] = e.values[side];
    }
    e.pairError = null;
    e.stale = false;
    updateEndplaneDirty();
    e.history = { anterior: [], posterior: [] };
    e.editHistory = [];
    clearEndplanePreview();
    e.viewer?.setPlanes(e.planes, e.mode);
    state.viewer3dCaseId = null;
    state.viewer3dKey = null;
    state.viewer3d?.clear();
    const refreshed = await api("/api/cases/" + encodeURIComponent(context.caseId));
    if (state.caseId !== context.caseId || endplaneContext()?.key !== context.key) return;
    state.case = refreshed;
    renderCase();
    loadFormalPorts(context);
    toast("双侧三端面已保存；请检查 STL 三处开口及 CFD 数值闸门");
  } catch (error) {
    if (state.caseId !== context.caseId || e.key !== context.key) return;
    e.error = committed ? "三端面已保存，但刷新状态失败：" + (error.detail?.reason || error.message) :
      error.detail?.reason || error.message || "双侧端面保存失败";
    renderEndplaneUi();
    toast(e.error, true);
  } finally {
    if (state.caseId === context.caseId && e.key === context.key) {
      e.saving = false;
      renderEndplaneUi();
    }
  }
}

async function saveLocalEndplanes() {
  const e = state.endplanes;
  const context = endplaneContext();
  const body = endplaneBody(context);
  const signature = endplaneSignature();
  if (!context || body?.format !== "oblique_local_inlet_v2" || !validLpsVector(body.anterior_seed_lps_mm) ||
      e.status !== "ready" ||
      e.profile?.can_save === false || e.saving || !e.dirty || endplaneValidation() ||
      e.previewStatus !== "ok" || e.previewSignature !== signature ||
      e.previewData?.format !== body.format || e.previewData?.can_preview_mesh !== true ||
      e.actualPreviewStatus !== "ok" || e.actualPreviewSignature !== signature) return;
  e.saving = true;
  e.error = null;
  renderEndplaneUi();
  let committed = false;
  try {
    const result = await api("/api/cases/" + encodeURIComponent(context.caseId) + "/segment/endplanes", {
      method: "POST", body,
    });
    if (result.scope !== context.scope || result.mask_version !== context.version ||
        result.format !== "oblique_local_inlet_v2" || result.complete !== true ||
        result.validation_error || !normalizedEndplane(result.anterior_plane) ||
        !normalizedEndplane(result.posterior_plane) || !validLpsVector(result.anterior_seed_lps_mm)) {
      throw new Error("服务端没有返回可核对的局部端面保存结果");
    }
    committed = true;
    if (state.caseId !== context.caseId || endplaneContext()?.key !== context.key ||
        endplaneSignature() !== signature) return;
    e.profile = { ...e.profile, saved: result };
    e.baselineFormat = e.format = result.format;
    e.baselineAnteriorSeed = [...result.anterior_seed_lps_mm];
    e.anteriorSeed = [...result.anterior_seed_lps_mm];
    for (const side of ["anterior", "posterior"]) {
      e.baselinePlanes[side] = normalizedEndplane(result[side + "_plane"]);
      e.planes[side] = cloneEndplane(e.baselinePlanes[side]);
      e.values[side] = ctIndexFromPlane(e.profile, e.planes[side]);
      e.baseline[side] = e.values[side];
    }
    e.pairError = null;
    e.stale = false;
    updateEndplaneDirty();
    e.history = { anterior: [], posterior: [] };
    e.editHistory = [];
    clearEndplanePreview();
    e.viewer?.setPlanes(e.planes, e.mode);
    state.viewer3dCaseId = null;
    state.viewer3dKey = null;
    state.viewer3d?.clear();
    const refreshed = await api("/api/cases/" + encodeURIComponent(context.caseId));
    if (state.caseId !== context.caseId || endplaneContext()?.key !== context.key) return;
    state.case = refreshed;
    renderCase();
    loadFormalPorts(context);
    toast("局部鼻孔端与鼻咽端已保存；这是单支段入口，生成 STL 后仍需核对实际开口");
  } catch (error) {
    if (state.caseId !== context.caseId || e.key !== context.key) return;
    e.error = committed ? "端面已保存，但刷新状态失败：" + (error.detail?.reason || error.message) :
      error.detail?.reason || error.message || "局部端面保存失败";
    renderEndplaneUi();
    toast(e.error, true);
  } finally {
    if (state.caseId === context.caseId && e.key === context.key) {
      e.saving = false;
      renderEndplaneUi();
    }
  }
}

function discardLocalEndplanes() {
  const e = state.endplanes;
  if (e.saving) return;
  e.format = e.baselineFormat;
  e.bilateral = cloneBilateralDraft(e.baselineBilateral);
  e.sharedInletPlane = e.baselineFormat === "oblique_bilateral_local_v3" &&
    Boolean(e.bilateral.left.plane && e.bilateral.right.plane) &&
    coincidentEndplanes(e.bilateral.left.plane, e.bilateral.right.plane);
  e.posteriorSeedPlane = null;
  e.posteriorSeed = e.baselinePosteriorSeed ? [...e.baselinePosteriorSeed] : null;
  e.anteriorSeed = e.baselineAnteriorSeed ? [...e.baselineAnteriorSeed] : null;
  e.planes = cloneEndplanePair(e.baselinePlanes);
  e.values = { ...e.baseline };
  e.adjustments = { anterior: null, posterior: null };
  e.history = { anterior: [], posterior: [] };
  e.editHistory = [];
  e.pickedPoints = [];
  e.placement = e.planes[e.mode] ? null : "point";
  e.error = null;
  e.pairError = e.profile?.saved?.validation_error?.reason || null;
  updateEndplaneDirty();
  clearEndplanePreview();
  e.viewer?.setFormat(e.format);
  e.viewer?.setPlanes(e.planes, e.mode);
  renderEndplaneUi();
}

async function saveEndplanes() {
  if (state.endplanes.format === "oblique_bilateral_local_v3") return saveBilateralEndplanes();
  if (state.endplanes.format === "oblique_local_inlet_v2") return saveLocalEndplanes();
  // 旧版接口仅留给不支持 v2 的部署；已保存旧方案在新版服务端只读。
  const side = state.endplanes.mode === "posterior" ? "posterior" : "anterior";
  return saveSingleEndplane(side);
}

function discardSingleEndplane(side) {
  const e = state.endplanes;
  if (!["anterior", "posterior"].includes(side) || e.saving) return;
  e.values[side] = e.baseline[side];
  e.planes[side] = cloneEndplane(e.baselinePlanes[side]);
  e.history[side] = [];
  e.editHistory = e.editHistory.filter((item) => item.side !== side);
  e.adjustments[side] = null;
  e.pickedPoints = [];
  e.placement = e.planes[e.mode] ? null : "point";
  e.error = null;
  e.pairError = e.profile?.saved?.validation_error?.reason || null;
  updateEndplaneDirty();
  clearEndplanePreview();
  e.viewer?.setPlanes(e.planes, e.mode);
  if (e.planes.anterior && e.planes.posterior) scheduleEndplanePreview();
  renderEndplaneUi();
}

function undoEndplaneEdit() {
  const e = state.endplanes;
  if (e.saving) return;
  if (e.pickedPoints.length) {
    e.pickedPoints.pop();
    renderEndplaneUi();
    return;
  }
  const entry = e.editHistory.pop();
  if (!entry) return;
  if (entry.kind === "single-port-suggestion") {
    e.mode = "anterior";
    e.activeInlet = entry.side;
    e.planes.anterior = cloneEndplane(entry.plane);
    e.anteriorSeed = [...entry.seed];
    e.bilateral[entry.side] = { plane: cloneEndplane(entry.plane), seed: [...entry.seed] };
    e.adjustments.anterior = null;
    e.placement = null;
    e.viewer?.setActiveSide("anterior");
    e.viewer?.setPlanes(e.planes, "anterior");
    e.viewer?.showSeedMarker(e.anteriorSeed);
    clearEndplaneSection();
    updateEndplaneDraft(true);
    scheduleEndplaneSection(true);
    return;
  }
  if (entry.kind === "opening-markup") {
    setOpeningMarkup(entry.side, entry.markup);
    e.placement = null;
    renderEndplaneUi();
    return;
  }
  if (entry.kind === "seed") {
    e.mode = entry.side || "anterior";
    setCurrentEndplaneSeed(entry.seed);
    e.placement = currentEndplaneSeed() ? null : "seed";
    e.viewer?.setActiveSide(e.mode);
    e.viewer?.showSeedMarker(currentEndplaneSeed());
    updateEndplaneDraft(true);
    return;
  }
  e.mode = entry.side;
  e.history[entry.side].pop();
  e.viewer?.setActiveSide(entry.side);
  commitEndplaneEdit(entry.plane, false);
  e.placement = e.planes[entry.side] ? null : "point";
  renderEndplaneUi();
}

function bindEndplaneControls() {
  $("btn-endplanes").addEventListener("click", () => {
    const e = state.endplanes;
    if (e.open) {
      if (!guardEndplaneDraft()) return;
      resetAutoCandidateState();
      e.open = false;
      clearEndplaneSection();
      e.values = { ...e.baseline };
      e.dirty = false;
    } else {
      if (state.maskEditor.dirty || state.maskEditor.saving || state.maskEditor.autoCleaning) {
        toast("请先完成当前掩膜编辑或清理，再设置端面", true);
        return;
      }
      state.maskEditor.active = false;
      e.open = true;
      state.mode = "slices";
      refreshViewer();
      const context = endplaneContext();
      if (context && (e.key !== context.key || e.status === "error")) loadEndplaneProfile(context, true);
      else if (context && e.status === "ready") loadEndplaneSelectionModel(context);
      focusStageTarget($("endplane-panel"));
    }
    renderEndplaneUi();
  });
  for (const side of ["anterior", "posterior"]) {
    $(`endplane-${side}-range`).addEventListener("input", (event) => setEndplaneIndex(side, Number(event.target.value) - 1));
    $(`endplane-${side}-number`).addEventListener("input", (event) => {
      const text = event.target.value.trim();
      const value = Number(text);
      const count = state.endplanes.profile?.count || 0;
      const index = text && Number.isInteger(value) && value >= 1 && value <= count ? value - 1 : null;
      setEndplaneIndex(side, index, side);
    });
    $(`endplane-${side}-jump`).addEventListener("click", () => jumpToEndplane(side));
    $(`endplane-${side}-discard`).addEventListener("click", () => discardSingleEndplane(side));
    $(`endplane-${side}-save`).addEventListener("click", () => saveSingleEndplane(side));
  }
  $("endplane-retry").addEventListener("click", () => loadEndplaneProfile(endplaneContext(), true));
  $("endplane-switch-local").addEventListener("click", () => {
    const e = state.endplanes;
    if (e.saving || e.status !== "ready" ||
        !e.profile?.supported_formats?.some(format => ["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(format))) return;
    const bilateral = e.profile.supported_formats.includes("oblique_bilateral_local_v3");
    e.format = bilateral ? "oblique_bilateral_local_v3" : "oblique_local_inlet_v2";
    if (bilateral) {
      e.activeInlet = "left";
      e.bilateral = cloneBilateralDraft(null);
      e.posteriorSeed = null;
      e.planes.anterior = null;
      e.planes.posterior = null;
      e.values = { anterior: null, posterior: null };
    }
    e.anteriorSeed = null;
    e.mode = "anterior";
    e.placement = e.planes.anterior ? "seed" : "point";
    e.pickedPoints = [];
    clearEndplanePreview();
    updateEndplaneDirty();
    e.viewer?.setFormat(e.format);
    e.viewer?.setPlanes(e.planes, e.mode);
    e.viewer?.focusActive();
    renderEndplaneUi();
  });
  $("endplane-seed-place").addEventListener("click", () => {
    const e = state.endplanes;
    if (e.saving || !["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(e.format) ||
        (e.format === "oblique_local_inlet_v2" && e.mode !== "anterior") || !e.planes[e.mode]) return;
    e.placement = e.placement === "seed" ? null : "seed";
    e.pickedPoints = [];
    renderEndplaneUi();
  });
  $("endplane-seed-clear").addEventListener("click", () => {
    const e = state.endplanes;
    const seed = currentEndplaneSeed();
    if (e.saving || !["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(e.format) || !seed) return;
    e.editHistory.push({ kind: "seed", side: e.mode, seed: [...seed] });
    if (e.editHistory.length > 40) e.editHistory.shift();
    setCurrentEndplaneSeed(null);
    e.placement = "seed";
    e.viewer?.showSeedMarker(null);
    updateEndplaneDraft(true);
  });
  $("endplane-markup-seed").addEventListener("click", () => {
    const e = state.endplanes;
    if (e.saving || e.selectionStatus !== "ready" || !["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(e.format)) return;
    e.openingStroke = null;
    e.placement = e.placement === "opening-seed" ? null : "opening-seed";
    renderEndplaneUi();
  });
  $("endplane-markup-outline").addEventListener("click", () => {
    const e = state.endplanes;
    if (e.saving || e.selectionStatus !== "ready" || !["oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(e.format)) return;
    e.openingStroke = [];
    e.placement = e.placement === "opening-outline" ? null : "opening-outline";
    renderEndplaneUi();
  });
  $("endplane-markup-clear").addEventListener("click", () => {
    const e = state.endplanes;
    if (e.saving) return;
    const side = openingMarkupSide();
    e.editHistory.push({ kind: "opening-markup", side, markup: currentOpeningMarkup(side) });
    if (e.editHistory.length > 40) e.editHistory.shift();
    setOpeningMarkup(side, emptyOpeningMarkup(side));
    e.openingStroke = null;
    e.placement = null;
    e.viewer?.showOpeningMarkup(currentOpeningMarkup(side));
    renderEndplaneUi();
  });
  $("endplane-opening-markup").addEventListener("toggle", () => {
    const panel = $("endplane-opening-markup");
    const e = state.endplanes;
    if (!panel.open && ["opening-seed", "opening-outline"].includes(e.placement)) {
      e.placement = null;
      e.openingStroke = null;
    }
    renderOpeningMarkup();
  });
  $("endplane-formal-ports-refresh").addEventListener("click", () => loadFormalPorts());
  $("endplane-auto-candidate-button").addEventListener("click", autoProposeBilateralOpenings);
  $("endplane-single-port-find").addEventListener("click", requestSinglePortSuggestion);
  $("endplane-single-port-apply").addEventListener("click", applySinglePortSuggestion);
  $("endplane-preview-model").addEventListener("click", loadActualEndplanePreview);
  $("endplane-local-save-button").addEventListener("click", saveEndplanes);
  $("endplane-local-discard").addEventListener("click", discardLocalEndplanes);
  $("endplane-independent").addEventListener("change", event => setEndplaneInletLayout(!event.target.checked));
  $("endplane-mode-anterior").addEventListener("click", () => selectEndplaneSide("anterior"));
  $("endplane-mode-left").addEventListener("click", () => selectBilateralInlet("left"));
  $("endplane-mode-right").addEventListener("click", () => selectBilateralInlet("right"));
  $("endplane-mode-posterior").addEventListener("click", () => selectEndplaneSide("posterior"));
  for (const side of ["left", "right", "posterior"]) {
    $("endplane-step-" + side).addEventListener("click", () => selectEndplaneOpening(side));
  }
  $("endplane-clear-region").addEventListener("click", () => applyEndplaneRegions([]));
  $("endplane-go-missing").addEventListener("click", () => {
    const side = nextEndplaneOpening();
    if (!side) return;
    selectEndplaneOpening(side);
    $("endplane-region-status").scrollIntoView({behavior:"smooth",block:"center"});
  });
  $("endplane-place").addEventListener("click", () => {
    const e = state.endplanes;
    e.placement = e.placement === "point" && e.planes[e.mode] ? null : "point";
    e.pickedPoints = [];
    renderEndplaneUi();
  });
  $("endplane-three-points").addEventListener("click", () => {
    const e = state.endplanes;
    e.placement = e.placement === "three" ? e.planes[e.mode] ? null : "point" : "three";
    e.pickedPoints = [];
    renderEndplaneUi();
  });
  $("endplane-gizmo").addEventListener("change", (event) => {
    state.endplanes.showGizmo = event.target.checked;
    state.endplanes.placement = state.endplanes.planes[state.endplanes.mode] ? null : "point";
    state.endplanes.pickedPoints = [];
    renderEndplaneUi();
  });
  $("endplane-advanced").addEventListener("toggle", () => renderEndplaneUi());
  $("endplane-show-removed").addEventListener("change", (event) => state.endplanes.viewer?.setShowRemoved(event.target.checked));
  $("endplane-preview-mode").addEventListener("change", (event) => setSurfacePreviewMode(event.target.value));
  $("endplane-align-view").addEventListener("click", () => state.endplanes.viewer?.alignToView());
  $("endplane-focus").addEventListener("click", () => state.endplanes.viewer?.focusActive());
  $("endplane-section-retry").addEventListener("click", () => scheduleEndplaneSection(true));
  $("endplane-undo").addEventListener("click", undoEndplaneEdit);
  for (const [id, key] of [["offset", "offset"], ["tilt-u", "tiltU"], ["tilt-v", "tiltV"]]) {
    const input = $("endplane-" + id);
    input.addEventListener("pointerdown", () => rememberEndplaneEdit());
    input.addEventListener("keydown", (event) => { if (!event.repeat && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) rememberEndplaneEdit(); });
    input.addEventListener("input", (event) => {
      const e = state.endplanes, adjustment = e.adjustments[e.mode];
      if (e.saving || !adjustment || e.placement) return;
      adjustment[key] = Number(event.target.value);
      e.planes[e.mode] = window.EndplaneTools.adjustedPlane(adjustment.anchor, adjustment.offset, adjustment.tiltU, adjustment.tiltV);
      e.viewer?.setPlanes(e.planes, e.mode);
      updateEndplaneDraft(true);
    });
  }
  $("endplane-tool-translate").addEventListener("click", () => {
    state.endplanes.tool = "translate";
    state.endplanes.viewer?.setTool("translate");
    renderEndplaneUi();
  });
  $("endplane-tool-rotate").addEventListener("click", () => {
    state.endplanes.tool = "rotate";
    state.endplanes.viewer?.setTool("rotate");
    renderEndplaneUi();
  });
  $("endplane-flip-normal").addEventListener("click", () => {
    const e = state.endplanes;
    const side = e.mode;
    const plane = normalizedEndplane(e.planes[side]);
    if (e.saving || !plane) return;
    commitEndplaneEdit({ ...plane, normal_lps: plane.normal_lps.map((value) => -value) });
  });
  $("endplane-reset-camera").addEventListener("click", () => state.endplanes.viewer?.resetCamera());
  window.addEventListener("beforeunload", (event) => {
    if (!state.endplanes.dirty || state.endplanes.saving) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

/* ---------- 基础设施 ---------- */
function toast(message, isError = false) {
  window.WindcatcherMonitor?.record(isError ? "error" : "ui", "feedback", { outcome: isError ? "error" : "ok" });
  const node = document.createElement("div");
  node.className = "toast" + (isError ? " is-error" : "");
  node.textContent = message;
  $("toasts").appendChild(node);
  setTimeout(() => node.remove(), isError ? 7000 : 3200);
}

async function api(path, options = {}) {
  if (window.WindcatcherCloudAccess && options.method === "DELETE") {
    options = { ...options, method: "POST", headers: { ...(options.headers || {}), "X-Windcatcher-Method": "DELETE" } };
  }
  const formData = typeof FormData !== "undefined" && options.body instanceof FormData;
  const binary = typeof Blob !== "undefined" && options.body instanceof Blob;
  const response = await fetch(path, {
    ...options,
    headers: formData ? (options.headers || {}) : { "Content-Type": binary ? "application/octet-stream" : "application/json", ...(options.headers || {}) },
    body: formData || binary ? options.body : options.body ? JSON.stringify(options.body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  const status = payload?.status || payload?.detail?.status;
  if (["failed", "missing", "blocked"].includes(status)) {
    window.WindcatcherMonitor?.record("error", "api_outcome", { status }, "api", response.headers.get("X-Windcatcher-Request-Id"));
  }
  if (!response.ok) {
    const detail = (payload && typeof payload.detail === "object" && payload.detail)
      || (payload && typeof payload === "object" ? payload : {});
    const remediation = Array.isArray(detail.remediation) ? detail.remediation : [];
    throw Object.assign(new Error([detail.reason || `请求失败（HTTP ${response.status}）`, ...remediation].join("\n")), { detail, status: response.status });
  }
  return payload;
}

async function apiUploadCase(files, synthetic, anonymize, onProgress = () => {}) {
  if (window.WindcatcherCloudAccess) return apiUploadCloudCase(files, synthetic, anonymize, onProgress);
  const form = new FormData();
  form.append("synthetic", synthetic ? "true" : "false");
  form.append("anonymize", anonymize ? "true" : "false");
  const relativePaths = [];
  for (const file of files) {
    const raw = file.webkitRelativePath || file.name || "";
    const parts = raw.replace(/\\/g, "/").split("/").filter(Boolean);
    // webkitdirectory includes the selected folder as the first component;
    // strip it so the server receives the actual sequence directory itself.
    const relative = file.webkitRelativePath && parts.length > 1 ? parts.slice(1).join("/") : parts.join("/");
    relativePaths.push(relative || file.name);
    form.append("files", file, file.name || "dicom");
  }
  form.append("relative_paths", JSON.stringify(relativePaths));
  return api("/api/cases/upload", { method: "POST", body: form });
}

async function apiUploadCloudCase(files, synthetic, anonymize, onProgress) {
  async function send(path, options) {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try { return await api(path, options); }
      catch (error) {
        if (attempt === 2 || (error.status && error.status < 500 && error.status !== 429)) throw error;
        await new Promise((resolve) => setTimeout(resolve, 500 * (attempt + 1)));
      }
    }
  }
  const manifest = files.map((file) => {
    const parts = (file.webkitRelativePath || file.name).replace(/\\/g, "/").split("/").filter(Boolean);
    return { path: file.webkitRelativePath && parts.length > 1 ? parts.slice(1).join("/") : parts.join("/"), size: file.size };
  });
  let uploadId;
  try {
    const upload = await api("/api/uploads", { method: "POST", body: { files: manifest, synthetic, anonymize } });
    uploadId = upload.upload_id;
    const limit = Math.min(upload.chunk_size, 4 * 1024 * 1024);
    if (!(limit > 0)) throw new Error("服务器返回的上传分块大小无效");
    const total = files.reduce((sum, file) => sum + file.size, 0);
    let offset = 0;
    let fileIndex = 0;
    let fileOffset = 0;
    while (offset < total) {
      const parts = [];
      let size = 0;
      while (fileIndex < files.length && size < limit) {
        const file = files[fileIndex];
        const length = Math.min(file.size - fileOffset, limit - size);
        parts.push(file.slice(fileOffset, fileOffset + length));
        size += length;
        fileOffset += length;
        if (fileOffset === file.size) { fileIndex += 1; fileOffset = 0; }
      }
      const body = new Blob(parts, { type: "application/octet-stream" });
      const acknowledged = await send(`/api/uploads/${uploadId}/chunks?offset=${offset}`, { method: "POST", body });
      if (acknowledged.received_bytes !== offset + size) throw new Error("服务器确认的上传位置不一致，请重新上传");
      offset += size;
      onProgress(offset, total);
    }
    return await send(`/api/uploads/${uploadId}/complete`, { method: "POST" });
  } catch (error) {
    let reason = error.detail?.reason || error.message;
    if (!error.status && (error instanceof TypeError || /fetch|network/i.test(reason))) {
      reason = "上传连接中断，文件选择已保留；请检查网络后重试";
    }
    if (uploadId) {
      try { await api(`/api/uploads/${uploadId}/cancel`, { method: "POST" }); }
      catch { reason += "；暂存清理尚未完成，请稍后重试"; }
    }
    throw Object.assign(new Error(reason), { detail: { reason } });
  }
}

function busy(button, isBusy) {
  if (!button) return;
  button.classList.toggle("is-busy", isBusy);
  button.disabled = isBusy;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}
function localizeMaskInstruction(text) {
  return String(text)
    .replace(/高级复核/g, "编辑掩膜")
    .replace(/在\s*[^\s，。]+\s*中编辑气道标签/g, "在三平面 CT 中编辑气道掩膜")
    .replace(/在\s*[^\s，。]+\s*中导出为病例目录下的/g, "在编辑工具中导出为病例目录下的")
    .replace(/回到网页再次点击“读取修正掩膜”/g, "回到网页继续确认气道范围");
}
function round(value) {
  const abs = Math.abs(value);
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 1) return value.toFixed(2).replace(/\.?0+$/, "");
  return value.toFixed(3).replace(/\.?0+$/, "");
}
function fmt(value, unit = "") {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") return `${round(value)}${unit}`;
  return String(value) + unit;
}
function verdictText(verdict) {
  return { pass: "通过", warn: "有警告", fail: "未通过" }[verdict] || verdict;
}
function statusIsDone(status) {
  return ["ok", "warn", "skipped"].includes(status);
}
const MASK_DEPENDENT_STAGES = new Set(["surface", "cfd_prep", "cfd_run", "report"]);
function externalAirCleanupState(stages = state.case?.stages) {
  const segmentData = stages?.segment?.data || {};
  const meta = state.ctViewer.caseId === state.caseId &&
    (segmentData.mask_version == null || state.ctViewer.meta?.mask_version === segmentData.mask_version)
    ? state.ctViewer.meta : null;
  const review = meta?.mask_review || {};
  const status = review.candidate_cleaning_status ?? segmentData.candidate_cleaning?.status ?? null;
  const pending = typeof meta?.mask_external_air_cleanup_pending === "boolean"
    ? meta.mask_external_air_cleanup_pending
    : status === "pending_manual";
  const cleanup = review.candidate_external_air_cleanup ?? segmentData.candidate_external_air_cleanup ?? null;
  return { pending, status, cleanup };
}
function externalAirCleanupSummary(cleanup) {
  if (cleanup?.removed_voxels === null || cleanup?.removed_voxels === undefined) return null;
  const voxels = Number(cleanup.removed_voxels);
  if (!Number.isFinite(voxels) || voxels < 0) return null;
  const volume = cleanup.removed_volume_ml;
  const volumeText = volume !== null && volume !== undefined && Number.isFinite(Number(volume))
    ? `（${fmt(Number(volume), " mL")}）` : "";
  return `移除 ${voxels.toLocaleString()} 个体素${volumeText}`;
}
function maskNeedsConfirmation(stages = {}) {
  const segment = stages.segment || {};
  return Boolean(
    ["warn", "blocked"].includes(segment.status) ||
    externalAirCleanupState(stages).pending ||
    segment.data?.need_human_confirm ||
    segment.data?.external_air_suspected ||
    state.ctViewer.meta?.mask_review?.need_human_confirm
  );
}
function maskBlocksStage(name, stages = {}) {
  return MASK_DEPENDENT_STAGES.has(name) && maskNeedsConfirmation(stages);
}
function effectiveStageStatus(name, status, stages) {
  // 旧版本可能已经落盘了 STL/CFD/报告，但这些产物来自未确认掩膜，不能继续展示为可用。
  if (maskBlocksStage(name, stages) && status !== "failed") return "blocked";
  if (state.clientRunningStage === name && !statusIsDone(status) && status !== "failed") return "running";
  if (name === "anonymize" && (
    (status === "pending" && statusIsDone(stages.qc?.status)) ||
    (status === "warn" && stages[name]?.data?.skipped)
  )) return "skipped";
  return status;
}
function requiresMaskReview(stages = state.case?.stages) {
  const segment = stages?.segment || {};
  return Boolean(
    ["warn", "blocked"].includes(segment.status) ||
    externalAirCleanupState(stages).pending ||
    segment.data?.need_human_confirm ||
    segment.data?.external_air_suspected ||
    state.ctViewer.meta?.mask_review?.need_human_confirm ||
    state.ctViewer.meta?.mask_external_air_suspected
  );
}
function stagePillText(name, status) {
  if (name === "import" && status === "ok") return "已导入";
  if (name === "anonymize" && status === "skipped") return "已跳过";
  return {
    ok: "已完成",
    warn: name === "segment" ? "待人工复核" : "有警告",
    failed: "未通过",
    blocked: "已阻断",
    running: "进行中",
    skipped: "已跳过",
    pending: "等待",
  }[status] || "等待";
}
function hasRunningStages(data) {
  return Boolean(data && (
    data.job?.status === "running" ||
    Object.values(data.stages || {}).some((stage) => stage.status === "running")
  ));
}

/* ---------- 当前病例真实 CFD 结果 ---------- */
function cfdResultsCaseRecord(caseId = state.cfdResults.caseId || state.caseId) {
  return (state.case?.case_id === caseId ? state.case : null) || state.cases.find((item) => item.case_id === caseId) || null;
}

function cfdCanPostprocess(result) {
  return Boolean(result?.generation?.can_generate === true &&
    !["running", "queued"].includes(result?.status) &&
    !["running", "queued"].includes(result?.task?.status));
}

function renderCfdResultsError(message, title = "真实病例结果读取失败", canRegenerate = false) {
  const box = $("cfd-results-state");
  if (!box) return;
  box.className = "cfd-results-state is-failed";
  box.innerHTML = `<h2>${escapeHtml(title)}</h2><p>${escapeHtml(message || "服务没有返回结果")}</p>`;
  if (canRegenerate) box.innerHTML += "<p>当前求解场已核对，可点击“生成可视化”重新生成可视化，无需重新求解。</p>";
  $("cfd-results-ready").hidden = true;
  $("btn-cfd-postprocess").disabled = !canRegenerate;
}

function renderCfdValidation(validation) {
  const box = $("cfd-results-validation");
  if (!box) return;
  const checks = Array.isArray(validation?.checks) ? validation.checks : [];
  const statusText = { ok: "通过", pending: "待复核", failed: "失败", invalid: "未通过", warn: "有警告" };
  const rows = checks.map((check) => {
    const status = check.status || check.detail?.status || "pending";
    const detail = typeof check.detail === "string" ? check.detail : (check.detail?.reason || "");
    return `<li><b>${escapeHtml(check.name || "检查")}</b><span>${escapeHtml(statusText[status] || status)}${detail ? ` · ${escapeHtml(detail)}` : ""}</span></li>`;
  }).join("");
  const metrics = validation?.metrics || {};
  const conservationError = reportNumber(metrics.mass_conservation_error_percent);
  const flow = conservationError !== null
    ? `质量守恒误差 ${conservationError.toFixed(4)}%` : "质量守恒尚无记录";
  const pressure = metrics.pressure_unit ? `复核统计压力单位为 ${metrics.pressure_unit === "m2/s2" ? "m²/s²（运动学压力）" : metrics.pressure_unit}；流场色标单位见图例` : "复核统计压力单位尚未确认；流场色标单位见图例";
  const numericText = validation?.numeric_ok ? "已通过" : "未通过；当前仅用于真实场可视化";
  const publishText = validation?.eligible ? "已满足发布条件" : "未满足发布条件";
  box.innerHTML = `<div class="kv"><dt>数值检查</dt><dd>${numericText}</dd><dt>患者分享</dt><dd>${publishText}</dd><dt>复核说明</dt><dd>${escapeHtml(flow)}；${escapeHtml(pressure)}</dd></div>${rows ? `<ul class="cfd-check-list">${rows}</ul>` : ""}`;
  const convergence = validation?.convergence || {};
  const residuals = convergence.initial_residual || {};
  const controls = convergence.residual_control || {};
  const comparisons = [];
  for (const field of ["Ux", "Uy", "Uz", "p"]) {
    const measured = residuals[field];
    const threshold = controls[field === "p" ? "p" : "U"];
    if (Number.isFinite(measured) && Number.isFinite(threshold)) {
      comparisons.push(field + " " + measured.toExponential(2) + " / " + threshold.toExponential(2));
    }
  }
  if (comparisons.length) {
    const count = Number.isInteger(convergence.iterations) ? String(convergence.iterations) : "—";
    const reason = convergence.reason ? "；" + convergence.reason : "";
    box.innerHTML += '<p class="cfd-validation-residual"><strong>第 ' + escapeHtml(count)
      + ' 次外层迭代，初始残差 / 阈值</strong>：' + escapeHtml(comparisons.join("；"))
      + escapeHtml(reason) + '</p>';
  }
}

function disposeCfdResults() {
  const result = state.cfdResults;
  if (result.pollTimer) clearTimeout(result.pollTimer);
  result.pollTimer = null;
  result.controller?.abort();
  result.controller = null;
  result.sections?.clear();
  result.viewer?.dispose();
  result.viewer = null;
  result.payloadKey = null;
  result.result = null;
  $("cfd-results-ready")?.setAttribute("hidden", "");
}

function updateCfdLegend(info) {
  if (!info?.range) return;
  const name = info.field === "pressure" ? "压力" : "速度";
  const unit = info.unit === "m2/s2" ? "m²/s²" : (info.unit || (info.field === "pressure" ? "m²/s²" : "m/s"));
  $("cfd-legend-name").textContent = `${name} (${unit})`;
  $("cfd-legend-min").textContent = Number(info.range[0]).toPrecision(4);
  $("cfd-legend-max").textContent = Number(info.range[1]).toPrecision(4);
}

function syncCfdResultsControls() {
  const viewer = state.cfdResults.viewer;
  if (!viewer) return;
  $("cfd-field-velocity").setAttribute("aria-pressed", String(viewer.field === "velocity"));
  $("cfd-field-pressure").setAttribute("aria-pressed", String(viewer.field === "pressure"));
  const play = $("cfd-play");
  const label = viewer.playing ? "暂停粒子" : "播放粒子";
  play.setAttribute("aria-pressed", String(viewer.playing));
  play.setAttribute("aria-label", label);
  play.title = label;
  play.querySelector("svg").innerHTML = viewer.playing
    ? '<path d="M7 4v12M13 4v12" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>'
    : '<path d="M6 3.5 15 10 6 16.5Z" fill="currentColor"/>';
  $("cfd-slice-axis").value = viewer.sliceAxis;
  const opacity = Math.round(viewer.opacity * 100);
  $("cfd-opacity").value = String(opacity);
  $("cfd-opacity-value").textContent = `${opacity}%`;
  $("cfd-show-streamlines").checked = viewer.showLines;
  $("cfd-show-patches").checked = viewer.showPatches;
  $("cfd-speed").value = String(viewer.speed);
  $("cfd-speed-value").textContent = `${viewer.speed.toFixed(2)}×`;
  $("cfd-density").value = String(viewer.density);
  $("cfd-density-value").textContent = `${viewer.density} / 流线`;
}

function renderCfdResultState(result, record) {
  const box = $("cfd-results-state");
  const ready = $("cfd-results-ready");
  const generate = $("btn-cfd-postprocess");
  const cancel = $("btn-cfd-postprocess-cancel");
  const task = result?.task;
  const status = result?.status || "missing";
  const source = result?.source || result?.manifest?.source;
  const isRunning = ["running", "queued"].includes(status) || ["running", "queued"].includes(task?.status);
  $("cfd-results-source").textContent = record ? `当前病例：${record.case_id}` : "尚未选择病例";
  generate.disabled = !cfdCanPostprocess(result) || state.cfdResults.actionPending;
  generate.title = result?.generation?.reason || "先刷新并核对当前病例的求解场";
  generate.hidden = isRunning;
  cancel.hidden = !isRunning;
  ready.hidden = status !== "ready";
  box.className = "cfd-results-state";
  if (status === "ready") {
    const counts = result?.counts || result?.manifest?.counts;
    const streamlineInfo = result?.metadata?.streamlines || result?.manifest?.metadata?.streamlines;
    const lineCount = Number.isInteger(counts?.streamlines) ? `${counts.streamlines} 条入口采样轨迹` : "入口采样轨迹";
    const terminationLabels = { out_of_steps: "达到积分步数上限", stagnation: "速度过低停止", out_of_length: "达到积分长度上限", out_of_domain: "离开计算域", not_initialized: "积分未初始化", unexpected_value: "积分异常", unknown: "终止原因未记录" };
    const terminations = Object.entries(streamlineInfo?.termination_counts || {}).filter(([, count]) => Number.isInteger(count) && count > 0).map(([name, count]) => `${terminationLabels[name] || "其他终止原因"} ${count} 条`).join("；");
    const sampling = `<p>${escapeHtml(lineCount)}；不代表全域覆盖或完整流量。${terminations ? `轨迹终止：${escapeHtml(terminations)}。` : ""}${streamlineInfo?.status === "empty" && streamlineInfo.reason ? escapeHtml(streamlineInfo.reason) : ""}</p>`;
    const interruptedAttempt = ["failed", "cancelled"].includes(task?.status);
    const attemptNotice = interruptedAttempt ? `<p><strong>本次后处理${task.status === "cancelled" ? "已取消" : "失败"}</strong>：${escapeHtml(task.message || "请查看后处理日志后重试")}。当前显示上次完整结果。</p>` : "";
    box.innerHTML = `<h2>${interruptedAttempt ? "上次完整流场可用" : "真实流场已生成"}</h2>${attemptNotice}<p>读取病例 ${escapeHtml(source?.case_id || record?.case_id || "")} 的最终时间步 ${escapeHtml(String(source?.timestep ?? ""))}。下方显示本病例的速度、压力、流线和动态粒子；它不是参考样本。</p>${sampling}`;
    renderCfdValidation(result.validation);
  } else if (isRunning) {
    const stages = { queued: "准备读取", read_fields: "读取求解场", wall: "提取气道壁面与端面", streamlines: "生成流线", slices: "生成速度与压力切片", publish: "保存可视化结果" };
    const startedAt = Date.parse(task?.created_at);
    const seconds = Number.isFinite(startedAt) ? Math.max(0, Math.floor((Date.now() - startedAt) / 1000)) : null;
    const duration = seconds === null ? "" : (seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`);
    const progressInfo = [stages[task?.stage] ? `当前阶段：${stages[task.stage]}` : "", duration ? `已耗时 ${duration}` : ""].filter(Boolean).join(" · ");
    box.innerHTML = `<h2>正在生成真实流场</h2><p>${escapeHtml(task?.message || "正在读取已有 OpenFOAM 求解场，请稍候")}</p>${progressInfo ? `<p>${escapeHtml(progressInfo)}</p>` : ""}<progress class="cfd-result-progress" aria-label="正在生成真实流场"></progress>`;
  } else if (status === "stale") {
    box.innerHTML = `<h2>结果已过期</h2><p>原始 CFD 场发生变化，旧流场不会继续显示。请重新生成可视化。</p>`;
  } else if (status === "failed" || status === "cancelled") {
    box.className = "cfd-results-state is-failed";
    box.innerHTML = `<h2>后处理${status === "cancelled" ? "已取消" : "失败"}</h2><p>${escapeHtml(task?.message || result?.reason || "请查看后处理日志后重试")}</p>`;
  } else {
    box.innerHTML = cfdCanPostprocess(result)
      ? `<h2>求解场已存在，尚未生成浏览器流场</h2><p>${escapeHtml(result.generation.reason)}。点击“生成可视化”读取最终时间步的速度、压力和流线。</p><p>当前可生成真实场观察结果；数值与工程复核状态将单独显示。</p>`
      : `<h2>暂不能生成流场</h2><p>${escapeHtml(result?.generation?.reason || (record ? "尚未取得当前病例的求解场状态，请点击刷新重试" : "请先选择病例"))}</p>${result?.generation?.remediation?.length ? `<p>${escapeHtml(result.generation.remediation.join("；"))}</p>` : ""}`;
  }
}

async function loadCfdResults(caseId = null) {
  const resultState = state.cfdResults;
  if (resultState.pollTimer) clearTimeout(resultState.pollTimer);
  resultState.pollTimer = null;
  const selected = caseId || resultState.caseId || state.caseId || state.cases.find((item) => !item.synthetic)?.case_id || state.cases[0]?.case_id;
  const select = $("cfd-results-case");
  if (select) {
    select.innerHTML = `<option value="">选择病例…</option>` + state.cases.slice().reverse().map((item) => `<option value="${escapeHtml(item.case_id)}">${escapeHtml(item.case_id)}${item.synthetic ? " · 合成" : ""}</option>`).join("");
    select.value = selected || "";
  }
  if (!selected) {
    disposeCfdResults();
    renderCfdResultState({ status: "missing" }, null);
    return;
  }
  if (resultState.caseId !== selected) disposeCfdResults();
  resultState.caseId = selected;
  resultState.controller?.abort();
  const controller = new AbortController();
  resultState.controller = controller;
  const isCurrentRequest = () => resultState.caseId === selected && resultState.controller === controller && !controller.signal.aborted;
  let verifiedResult = null;
  try {
    const result = await api(`/api/cases/${encodeURIComponent(selected)}/cfd-results`, { signal: controller.signal });
    if (!isCurrentRequest()) return;
    verifiedResult = result;
    resultState.result = result;
    const record = cfdResultsCaseRecord(selected);
    renderCfdResultState(result, record);
    if (result.status === "ready") {
      const payloadKey = `${selected}:${result.source_signature || result.manifest?.source_signature || ""}:${result.payload || result.manifest?.payload || ""}`;
      if (resultState.payloadKey !== payloadKey) {
        const payload = await api(result.viewer_url || `/api/cases/${encodeURIComponent(selected)}/cfd-results/viewer`, { signal: controller.signal });
        if (!isCurrentRequest()) return;
        resultState.sections?.clear();
        resultState.viewer?.dispose();
        resultState.viewer = new CfdViewer($("cfd-flow-canvas"), payload, { onLegend: updateCfdLegend, onError: (message) => renderCfdResultsError(message, undefined, cfdCanPostprocess(result) && !resultState.actionPending) });
        window.WindcatcherOperatorPreferences?.applyToViewer(resultState.viewer, payload);
        syncCfdResultsControls();
        resultState.payloadKey = payloadKey;
        $("cfd-results-ready").hidden = false;
      }
      if (window.CfdSections) {
        resultState.sections ||= new window.CfdSections();
        await resultState.sections.load(selected, resultState.viewer);
      }
    }
    if (result.status === "running" || result.task?.status === "running" || result.task?.status === "queued") {
      resultState.pollTimer = setTimeout(() => loadCfdResults(selected), 1200);
    }
  } catch (error) {
    if (error.name === "AbortError" || !isCurrentRequest()) return;
    renderCfdResultsError(error.detail?.reason || error.message, undefined, cfdCanPostprocess(verifiedResult) && !resultState.actionPending);
  } finally {
    if (resultState.controller === controller) resultState.controller = null;
  }
}

function bindCfdResultsControls() {
  $("btn-cfd-results-refresh").addEventListener("click", () => loadCfdResults().catch((error) => renderCfdResultsError(error.message)));
  $("cfd-results-case").addEventListener("change", (event) => loadCfdResults(event.target.value));
  $("btn-cfd-postprocess").addEventListener("click", async () => {
    const id = state.cfdResults.caseId;
    if (!id || state.cfdResults.actionPending) return;
    state.cfdResults.actionPending = true;
    $("btn-cfd-postprocess").disabled = true;
    try { await api(`/api/cases/${encodeURIComponent(id)}/cfd-postprocess`, { method: "POST", body: {} }); await loadCfdResults(id); }
    catch (error) { renderCfdResultsError(error.detail?.reason || error.message, "后处理启动失败"); }
    finally {
      state.cfdResults.actionPending = false;
      if (state.cfdResults.caseId === id) $("btn-cfd-postprocess").disabled = !cfdCanPostprocess(state.cfdResults.result);
    }
  });
  $("btn-cfd-postprocess-cancel").addEventListener("click", async () => {
    const id = state.cfdResults.caseId;
    if (!id) return;
    try { await api(`/api/cases/${encodeURIComponent(id)}/cfd-postprocess/cancel`, { method: "POST", body: {} }); await loadCfdResults(id); }
    catch (error) { renderCfdResultsError(error.detail?.reason || error.message); }
  });
  $("cfd-field-velocity").addEventListener("click", () => { state.cfdResults.viewer?.setField("velocity"); syncCfdResultsControls(); });
  $("cfd-field-pressure").addEventListener("click", () => { state.cfdResults.viewer?.setField("pressure"); syncCfdResultsControls(); });
  $("cfd-play").addEventListener("click", () => { const viewer = state.cfdResults.viewer; if (!viewer) return; viewer.setPlaying(!viewer.playing); syncCfdResultsControls(); });
  $("cfd-reset").addEventListener("click", () => {
    const viewer = state.cfdResults.viewer;
    if (!viewer) return;
    viewer.reset();
    window.WindcatcherOperatorPreferences?.applyToViewer(viewer, viewer.data);
  });
  $("cfd-slice-axis").addEventListener("change", (event) => state.cfdResults.viewer?.setSlice(event.target.value));
  $("cfd-opacity").addEventListener("input", (event) => { const value = Number(event.target.value) / 100; state.cfdResults.viewer?.setOpacity(value); $("cfd-opacity-value").textContent = `${event.target.value}%`; });
  $("cfd-show-streamlines").addEventListener("change", (event) => state.cfdResults.viewer?.setLines(event.target.checked));
  $("cfd-show-patches").addEventListener("change", (event) => state.cfdResults.viewer?.setPatches(event.target.checked));
  $("cfd-speed").addEventListener("input", (event) => { const value = Number(event.target.value); state.cfdResults.viewer?.setSpeed(value); $("cfd-speed-value").textContent = `${value.toFixed(2)}×`; });
  $("cfd-density").addEventListener("input", (event) => { const value = Number(event.target.value); state.cfdResults.viewer?.setDensity(value); $("cfd-density-value").textContent = `${value} / 流线`; });
}

/* ---------- 路由 ---------- */
const routes = ["work", "agent", "cases", "env", "method", "sample", "results"];
if (window.WindcatcherActivityEnabled) routes.push("activity");
if (window.WindcatcherMultiuser === true) routes.push("accounts");
if (window.WindcatcherCloudAccess) {
  routes.push("cfd-review", "share");
  document.querySelector('.side-nav button[data-route="cfd-review"]').hidden = false;
  document.querySelector('.side-nav button[data-route="share"]').hidden = false;
}
function refreshCaseAfterAgent(caseId) {
  state.agentReturnRefreshErrorCaseId = null;
  state.agentReturnRefreshingCaseId = caseId;
  if (state.case) {
    renderActions(state.case);
    syncQuickPreview(state.case);
  }
  void api("/api/cases/" + caseId).then(async (fresh) => {
    if (state.caseId !== caseId) return;
    const previousEndplaneKey = state.endplanes.key;
    state.case = fresh;
    renderCase();
    const context = endplaneContext(fresh);
    // The batch can save ports without changing the mask version. Refresh the
    // same-version cache, but never discard an unsaved local draft.
    if (context && previousEndplaneKey === context.key &&
        state.endplanes.key === context.key &&
        !state.endplanes.dirty && !state.endplanes.saving) {
      await loadEndplaneProfile(context, true);
    }
    if (hasRunningStages(fresh)) startCasePolling();
  }).catch((error) => {
    if (state.caseId !== caseId) return;
    state.agentReturnRefreshErrorCaseId = caseId;
    toast(error.detail?.reason || "病例进度刷新失败，请重试同步", true);
  }).finally(() => {
    if (state.agentReturnRefreshingCaseId !== caseId) return;
    state.agentReturnRefreshingCaseId = null;
    if (state.caseId === caseId && state.case) {
      renderActions(state.case);
      syncQuickPreview(state.case);
    }
  });
}

function showRoute(route) {
  if (!routes.includes(route)) route = "work";
  const previousRoute = state.route;
  if (state.route !== route) {
    state.route = route;
    window.WindcatcherMonitor?.record("navigation", "page_opened", {}, route);
  }
  for (const name of routes) $("page-" + name).classList.toggle("is-visible", name === route);
  for (const button of document.querySelectorAll(".side-nav button")) {
    button.classList.toggle("is-active", button.dataset.route === route);
    if (button.dataset.route === route) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  if (route === "agent") window.WindcatcherOrchestration?.activate();
  if (route === "env") {
    loadEnv().catch((e) => toast(e.message, true));
    window.WindcatcherOperatorPreferences?.activate();
  }
  if (route === "accounts") window.WindcatcherAccessAdmin?.activateAccounts();
  if (route === "cases") window.WindcatcherAccessAdmin?.activateCaseGrants(state.cases, state.caseId);
  if (route === "cfd-review") window.WindcatcherCfdReview?.activate(state.cases, state.caseId);
  if (route === "share") window.WindcatcherShareAdmin?.activate(state.cases, state.caseId);
  if (route === "sample") window.WindcatcherReferenceAdmin?.activate(state.cases, state.caseId);
  if (route === "work") {
    state.viewer3d?.resize();
    if (previousRoute === "agent" && state.caseId) refreshCaseAfterAgent(state.caseId);
  }
  if (route === "results") loadCfdResults().catch((e) => renderCfdResultsError(e.message));
  else disposeCfdResults();
}
document.querySelectorAll(".side-nav button").forEach((button) => {
  button.addEventListener("click", () => {
    window.location.hash = button.dataset.route;
    showRoute(button.dataset.route);
  });
});
window.addEventListener("hashchange", () => showRoute(window.location.hash.replace("#", "")));

// Reuse case selection and its draft guard; opening the current case keeps drafts intact.
window.WindcatcherWorkbench = {
  async openCase(caseId) {
    if (state.caseId !== caseId && await selectCase(caseId) === false) return false;
    window.location.hash = "work";
    showRoute("work");
    window.WindcatcherQuickPreview?.openAdvanced?.();
    focusStageTarget($("viewer-card"));
    return true;
  },
};

function focusStageTarget(target, focusTarget = target) {
  if (!target) return;
  window.setTimeout(() => {
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    if (focusTarget && typeof focusTarget.focus === "function") {
      if (!focusTarget.hasAttribute("tabindex")) focusTarget.setAttribute("tabindex", "-1");
      focusTarget.focus({ preventScroll: true });
    }
  }, 0);
}

function navigateToStage(stageName) {
  window.WindcatcherQuickPreview?.openAdvanced?.();
  const hasAnonymizeDetail = Boolean($("detail-anonymize"));
  const route = stageName === "import" || (stageName === "anonymize" && !hasAnonymizeDetail) ? "cases" : "work";
  window.location.hash = route;
  showRoute(route);
  const jump = (target, focusTarget = target) => focusStageTarget(target, focusTarget);
  switch (stageName) {
    case "import":
      jump($("newcase-form"), $("btn-pick-directory"));
      break;
    case "qc":
      jump($("detail-qc") || $("case-message-wrap") || $("viewer-card"));
      break;
    case "anonymize":
      if (hasAnonymizeDetail) jump($("detail-anonymize"));
      else jump($("newcase-form"), $("newcase-anonymize"));
      break;
    case "segment":
      state.mode = "slices";
      refreshViewer();
      jump($("viewer-card"), $("viewer-card"));
      break;
    case "surface": {
      const maskNeedsReview = requiresMaskReview(state.case?.stages);
      const surfaceReady = hasSurfaceModel() && !maskNeedsReview;
      if (surfaceReady) {
        if (!activateStlPreview()) return;
        jump($("viewer-card"), $("viewer-card"));
      } else {
        jump($("detail-surface") || $("viewer-card"));
      }
      break;
    }
    case "cfd_prep":
    case "cfd_run": {
      const cfdTarget = $("detail-cfd") || $("case-actions");
      jump(cfdTarget, cfdTarget?.id === "case-actions" ? $("act-primary") : cfdTarget);
      break;
    }
    case "report": {
      const reportTarget = $("detail-report") || $("case-actions");
      jump(reportTarget, reportTarget?.id === "case-actions" ? $("act-primary") : reportTarget);
      break;
    }
    default:
      jump($("case-message-wrap") || $("viewer-card"));
  }
}
window.WindcatcherOpenFavoriteStep = (step) => {
  if (step === "cfd_results") { window.location.hash = "results"; showRoute("results"); return; }
  if (step === "review" || step === "share") {
    if (!window.WindcatcherCloudAccess) return;
    const route = step === "review" ? "cfd-review" : "share";
    window.location.hash = route;
    showRoute(route);
    return;
  }
  if (step === "endplanes") {
    window.location.hash = "work";
    showRoute("work");
    if (!$('btn-endplanes').disabled && !state.endplanes.open) $('btn-endplanes').click();
    focusStageTarget($("endplane-panel")?.hidden ? $("viewer-card") : $("endplane-panel"));
    return;
  }
  if (["qc", "segment", "surface", "cfd_prep", "cfd_run", "report"].includes(step)) navigateToStage(step);
};
/* ---------- 病例列表 / 选择 ---------- */
async function loadCases() {
  state.cases = await api("/api/cases");
  window.WindcatcherShareAdmin?.syncCases(state.cases, state.caseId);
  window.WindcatcherCfdReview?.syncCases(state.cases, state.caseId);
  window.WindcatcherAccessAdmin?.syncCases(state.cases, state.caseId);
  if (state.route === "cases") window.WindcatcherAccessAdmin?.activateCaseGrants(state.cases, state.caseId);
  const select = $("case-select");
  const current = state.caseId;
  select.innerHTML = `<option value="">选择病例…</option>` +
    state.cases.slice().reverse().map((item) =>
      `<option value="${item.case_id}" ${item.case_id === current ? "selected" : ""}>${item.case_id}${item.synthetic ? " · 合成" : ""}</option>`
    ).join("");
  const list = $("case-list");
  renderHistory();
  if (!state.cases.length) {
    list.innerHTML = `<p style="margin:0;color:var(--ink-3);font-size:12.5px;">还没有病例。在右侧选择一套 DICOM 序列开始。</p>`;
    return;
  }
  list.innerHTML = "";
  for (const item of state.cases.slice().reverse()) {
    const row = document.createElement("div");
    row.className = "case-item-row";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "case-item" + (item.case_id === state.caseId ? " is-active" : "");
    button.innerHTML = `<strong>${escapeHtml(item.case_id)}</strong><small>${escapeHtml(caseRecordMeta(item))}</small>`;
    button.addEventListener("click", () => selectCase(item.case_id).then((selected) => {
      if (selected === false) return;
      window.location.hash = "work";
      showRoute("work");
    }));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "case-delete";
    remove.textContent = "删除";
    remove.title = "删除病例及其本地产物；服务会核对当前处理状态";
    remove.setAttribute("aria-label", `删除病例 ${item.case_id}`);
    remove.addEventListener("click", async (event) => {
      event.stopPropagation();
      remove.disabled = true;
      try {
        await deleteCase(item.case_id);
      } catch (error) {
        toast(error.message, true);
      } finally {
        remove.disabled = false;
      }
    });
    row.append(button, remove);
    list.appendChild(row);
  }
}

function isCaseRunning(item) {
  return Boolean(item?.job?.status === "running" || Object.values(item?.stages || {}).some((status) => status === "running"));
}

function effectiveCaseListStages(item) {
  const raw = item?.stages || {};
  const stageObjects = Object.fromEntries(
    Object.entries(raw).map(([name, status]) => [name, { status }])
  );
  return Object.fromEntries(
    Object.entries(raw).map(([name, status]) => [name, effectiveStageStatus(name, status, stageObjects)])
  );
}

function caseRecordMeta(item) {
  const statuses = Object.values(effectiveCaseListStages(item));
  const done = statuses.filter((status) => ["ok", "skipped"].includes(status)).length;
  const review = statuses.filter((status) => status === "warn").length;
  const blocked = statuses.some((status) => status === "blocked");
  const failed = statuses.some((status) => status === "failed");
  const date = item.created_at ? item.created_at.slice(0, 16).replace("T", " ") : "时间未知";
  const source = item.synthetic ? "合成测试" : "本地 CT";
  const progress = `${done}/8 步完成${review ? ` · ${review} 项待复核` : ""}${blocked ? " · 下游已阻断" : ""}`;
  if (isCaseRunning(item)) return `${source} · ${date} · 当前：${STAGE_LABELS[item.job?.stage] || item.job?.stage || "处理中"} · ${progress}`;
  if (failed || item.job?.status === "failed") return `${source} · ${date} · 有阶段未通过 · ${progress}`;
  return `${source} · ${date} · ${progress}`;
}

function caseRecordStatus(item) {
  if (isCaseRunning(item)) return `进行中：${STAGE_LABELS[item.job?.stage] || item.job?.stage || "处理中"}`;
  const effectiveStages = effectiveCaseListStages(item);
  if (Object.values(effectiveStages).some((status) => status === "failed")) return "有阶段未通过";
  const statuses = Object.values(effectiveStages);
  const done = statuses.filter((status) => ["ok", "skipped"].includes(status)).length;
  const review = statuses.filter((status) => status === "warn").length;
  if (statuses.some((status) => status === "blocked")) return `${done}/${STAGE_ORDER.length} 步已完成 · 上游掩膜待确认`;
  if (done === STAGE_ORDER.length) return "流程已完成";
  return `${done}/${STAGE_ORDER.length} 步已完成${review ? ` · ${review} 项待复核` : ""}`;
}

function renderHistory() {
  const list = $("history-list");
  if (!list) return;
  const count = $("history-count");
  if (count) count.textContent = state.cases.length ? `${state.cases.length} 条` : "暂无";
  if (!state.cases.length) {
    list.innerHTML = `<div class="history-empty">还没有既往分析记录。先到“病例”页导入一套 DICOM 序列。</div>`;
    return;
  }
  list.innerHTML = "";
  for (const item of state.cases.slice().reverse()) {
    const row = document.createElement("div");
    row.className = "history-row" + (item.case_id === state.caseId ? " is-active" : "");
    const open = document.createElement("button");
    open.type = "button";
    open.className = "history-select";
    open.innerHTML = `<strong>${escapeHtml(item.case_id)}</strong><small>${escapeHtml(item.synthetic ? "合成测试数据" : "本地 CT")} · ${escapeHtml(item.created_at?.slice(0, 16).replace("T", " ") || "时间未知")}</small>`;
    open.addEventListener("click", () => selectCase(item.case_id).then((selected) => {
      if (selected === false) return;
      window.location.hash = "work";
      showRoute("work");
    }).catch((error) => toast(error.detail?.reason || error.message, true)));
    const status = document.createElement("span");
    status.className = "history-status";
    status.textContent = caseRecordStatus(item);
    const manage = document.createElement("button");
    manage.type = "button";
    manage.className = "button button-quiet history-open";
    manage.textContent = item.case_id === state.caseId ? "当前病例" : "查看";
    manage.disabled = item.case_id === state.caseId;
    manage.addEventListener("click", () => open.click());
    row.append(open, status, manage);
    list.appendChild(row);
  }
}

const HISTORY_DRAWER_STORAGE_KEY = "windcatcher:history-drawer";
function setHistoryDrawer(collapsed, persist = true) {
  const panel = $("history-panel");
  const list = $("history-list");
  const toggle = $("btn-history-toggle");
  const label = toggle?.querySelector(".history-toggle-label");
  if (!panel || !list || !toggle) return;
  panel.classList.toggle("is-collapsed", collapsed);
  list.hidden = collapsed;
  toggle.setAttribute("aria-expanded", String(!collapsed));
  toggle.setAttribute("aria-label", collapsed ? "展开病例记录" : "收起病例记录");
  if (label) label.textContent = collapsed ? "展开病例" : "收起病例";
  if (persist) {
    try { localStorage.setItem(HISTORY_DRAWER_STORAGE_KEY, collapsed ? "collapsed" : "open"); } catch { /* 浏览器禁用存储时不影响折叠 */ }
  }
}

function initHistoryDrawer() {
  const toggle = $("btn-history-toggle");
  if (!toggle) return;
  let saved = null;
  try { saved = localStorage.getItem(HISTORY_DRAWER_STORAGE_KEY); } catch { /* 使用响应式默认值 */ }
  const collapsed = saved === "collapsed"
    || (saved !== "open" && window.matchMedia?.("(max-width: 700px)").matches === true);
  setHistoryDrawer(collapsed, false);
  toggle.addEventListener("click", () => setHistoryDrawer(!$("history-panel").classList.contains("is-collapsed")));
}

async function deleteCase(caseId) {
  if (state.caseId === caseId && !guardEndplaneDraft()) return;
  if (!window.confirm(`确定删除病例 ${caseId} 及其本地产物吗？此操作无法撤销。`)) return;
  await api(`/api/cases/${encodeURIComponent(caseId)}`, { method: "DELETE" });
  if (state.caseId === caseId) {
    stopCasePolling();
    state.caseId = null;
    state.case = null;
    state.clientRunningStage = null;
    try { localStorage.removeItem("windcatcher:last-case"); } catch { /* ignore storage policy */ }
    renderEmptyCase();
  }
  await loadCases();
  toast("病例已删除");
}

function renderEmptyCase() {
  window.WindcatcherHarnessAdvice?.setCase(null);
  window.WindcatcherQuickPreview?.render(null);
  const advanced = $("advanced-workflow-panel");
  if (advanced) advanced.open = false;
  $("case-select").value = "";
  $("case-sub").textContent = "尚未选择病例";
  $("case-synthetic").hidden = true;
  $("progress-summary").innerHTML = `<div class="progress-copy"><strong>处理进度</strong><span>请从“既往分析记录”选择一个病例</span><b>0/8</b></div><div class="progress-track"><span style="width:0%"></span></div>`;
  $("step-cards").innerHTML = "";
  $("case-actions").innerHTML = "";
  renderMetrics(null);
  $("detail-panels").innerHTML = "";
  $("viewer-empty").hidden = false;
  $("viewer-views").hidden = true;
  $("viewer-controls").hidden = true;
  $("viewer-3d").hidden = true;
  $("viewer-preview-controls").hidden = true;
  $("btn-download-stl").disabled = true;
  $("btn-view-3d").disabled = true;
  $("viewer-note").textContent = "";
  $("viewer-sub").textContent = "浏览器内 CT 复核与掩膜编辑 · 等待导入体数据";
  $("review-guide").hidden = true;
  $("btn-confirm-mask").hidden = true;
  $("btn-export-mask").hidden = true;
  $("mask-editor-toolbar").hidden = true;
  state.maskEditor.active = false;
  state.maskEditor.stroke = null;
  state.maskEditor.preview = [];
  state.maskEditor.pending = [];
  state.maskEditor.saving = false;
  state.maskEditor.autoCleaning = false;
  state.maskEditor.externalAirAction = null;
  state.maskEditor.externalAirFeedback = null;
  state.maskEditor.dirty = false;
  state.maskEditor.history = [];
  state.viewerAssetCaseId = null;
  state.viewerAssetMode = null;
  state.viewer3dCaseId = null;
  state.viewer3dKey = null;
  state.viewer3d?.clear();
  resetCtViewer(null);
  resetEndplaneState();
}

async function selectCase(caseId) {
  if (state.caseId && !guardEndplaneDraft()) return false;
  stopCasePolling();
  destroyScenarioViewers();
  state.scenarioPolls.forEach((timer) => clearInterval(timer));
  state.scenarioPolls.clear();
  state.clientRunningStage = null;
  state.maskEditor.active = false;
  state.maskEditor.stroke = null;
  state.maskEditor.preview = [];
  state.maskEditor.pending = [];
  state.maskEditor.saving = false;
  state.maskEditor.autoCleaning = false;
  state.maskEditor.externalAirAction = null;
  state.maskEditor.externalAirFeedback = null;
  state.maskEditor.dirty = false;
  state.maskEditor.history = [];
  state.caseId = caseId;
  window.WindcatcherHarnessAdvice?.setCase(caseId);
  state.viewer3d?.clear();
  state.viewer3dCaseId = null;
  state.viewer3dKey = null;
  resetEndplaneState();
  state.modelScope = "main_nasal_airway";
  try {
    const savedScope = localStorage.getItem(modelScopeStorageKey(caseId));
    if (MODEL_SCOPE_OPTIONS[savedScope]) state.modelScope = savedScope;
  } catch { /* 浏览器禁用存储时使用当前病例的默认范围 */ }
  try { localStorage.setItem("windcatcher:last-case", caseId); } catch { /* 浏览器禁用存储时不影响工作台 */ }
  state.case = await api("/api/cases/" + caseId);
  if (!state.computeCapabilities) {
    try { state.computeCapabilities = (await api("/api/capabilities")).compute || null; } catch { /* 环境探测失败不阻断病例操作 */ }
  }
  // 进入病例先打开 CT 复核，STL 只有用户明确点击后才加载，避免错误或超大网格阻塞页面。
  state.mode = "slices";
  // 先清除上一病例的 CT 元数据，避免异步加载当前病例时沿用旧的边界警告。
  resetCtViewer(caseId);
  await loadCases();
  renderCase();
  if (hasRunningStages(state.case)) startCasePolling();
  return true;
}

function stopCasePolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function startCasePolling() {
  stopCasePolling();
  const capturedCaseId = state.caseId;
  let consecutiveFailures = 0;
  const tick = async () => {
    if (!state.caseId || state.caseId !== capturedCaseId) {
      stopCasePolling();
      return;
    }
    try {
      const fresh = await api("/api/cases/" + capturedCaseId);
      if (state.caseId !== capturedCaseId) return;
      consecutiveFailures = 0;
      state.case = fresh;
      renderCase();
      if (!hasRunningStages(fresh) && !state.clientRunningStage) stopCasePolling();
    } catch (error) {
      // 原子落盘后理论上不会读到半截 JSON；网络/服务瞬时抖动仍重试几次，避免进度卡死。
      consecutiveFailures += 1;
      if (consecutiveFailures >= 5) {
        stopCasePolling();
        toast(error.detail?.reason || "暂时无法读取病例进度，请稍后刷新", true);
      }
    }
    if (consecutiveFailures < 5 && state.caseId === capturedCaseId && state.case && (hasRunningStages(state.case) || state.clientRunningStage)) {
      state.pollTimer = setTimeout(tick, 1200);
    } else if (state.caseId === capturedCaseId) {
      state.pollTimer = null;
    }
  };
  tick();
}

/* ---------- 步骤卡条（阶段导航 + 状态胶囊） ---------- */
const ICON_CHECK = `<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 6.5l2.4 2.4L9.7 3.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const ICON_WARN = `<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2.2v4.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="6" cy="9.4" r="1" fill="currentColor"/></svg>`;

function worstStatus(statuses) {
  if (statuses.includes("failed")) return "failed";
  if (statuses.includes("blocked")) return "warn";
  if (statuses.includes("warn")) return "warn";
  if (statuses.every((s) => s === "ok")) return "ok";
  return "pending";
}

function renderSteps(data) {
  const s = data.stages;
  const cleanupPending = externalAirCleanupState(s).pending;
  const steps = STAGE_ORDER.map((name) => {
    const status = effectiveStageStatus(name, s[name].status, s);
    const blockedByMask = maskBlocksStage(name, s);
    return {
      name,
      label: STAGE_LABELS[name],
      status,
      pill: name === "segment" && status === "warn" && cleanupPending ? "待清理" : stagePillText(name, status),
      tip: blockedByMask
        ? "上游气道掩膜尚未确认，旧产物不可用；确认后需要重新生成"
        : s[name].message || STAGE_LABELS[name],
    };
  });
  const completed = steps.filter((step) => ["ok", "skipped"].includes(step.status)).length;
  const reviewSteps = steps.filter((step) => step.status === "warn");
  const processed = completed + reviewSteps.length;
  const active = steps.find((step) => step.status === "running");
  const next = steps.find((step) => step.status === "pending");
  const blocked = steps.find((step) => ["failed", "blocked"].includes(step.status));
  const job = data.job || {};
  const current = job.status === "running" && job.stage === "preview"
    ? `当前：切面预览 · ${job.message || "正在生成预览"}`
    : active
    ? `当前：${active.label} · ${active.tip}`
    : reviewSteps.length
      ? `当前${cleanupPending && reviewSteps[0].name === "segment" ? "待清理" : "待复核"}：${reviewSteps[0].label} · ${reviewSteps[0].tip}`
    : job.status === "failed"
      ? `导入未完成 · ${job.message || "后台任务失败"}`
    : blocked
      ? `${blocked.label}未通过 · ${blocked.tip}`
      : next
      ? `下一步：${next.label} · 等待开始`
      : completed === steps.length ? "流程已完成，可查看报告" : `已完成 ${completed}/${steps.length} 个阶段`;
  const summary = $("progress-summary");
  if (summary) {
    const progressText = `${completed}/${steps.length} 已完成${reviewSteps.length ? ` · ${reviewSteps.length} 项${cleanupPending ? "待清理/复核" : "待复核"}` : ""}`;
    summary.innerHTML = `<div class="progress-copy"><strong>处理进度</strong><span>${escapeHtml(current)}</span><b>${progressText}</b></div>
      <div class="progress-track" role="progressbar" aria-label="病例处理进度" aria-valuemin="0" aria-valuemax="${steps.length}" aria-valuenow="${completed}"><span style="width:${Math.round(processed / steps.length * 100)}%"></span></div>`;
  }
  const box = $("step-cards");
  box.innerHTML = "";
  steps.forEach((step, index) => {
    const card = document.createElement("div");
    card.className = "step-card " + (step.status === "ok" ? "is-ok" : step.status === "warn" || step.status === "blocked" ? "is-warn" : step.status === "failed" ? "is-failed" : step.status === "running" ? "is-running" : "");
    card.dataset.stage = step.name;
    card.setAttribute("role", "button");
    card.tabIndex = 0;
    card.setAttribute("aria-label", "查看" + step.label + "阶段，当前状态：" + step.pill);
    if (step.status === "running") card.setAttribute("aria-current", "step");
    const icon = step.status === "ok" ? ICON_CHECK : (step.status === "warn" || step.status === "blocked" || step.status === "failed") ? ICON_WARN : step.status === "running" ? ICON_RUNNING : "";
    card.title = step.tip || step.label;
    const pillClass = step.status === "ok" ? "ok" : (step.status === "warn" || step.status === "blocked") ? "warn" : step.status === "failed" ? "failed" : step.status === "running" ? "running" : "pending";
    card.innerHTML = `<div class="step-line"><span class="step-icon">${icon}</span><span class="step-name">${step.label}</span></div>
      <span class="step-pill is-${pillClass}">${step.pill}</span>`;
    box.appendChild(card);
    const openStage = () => navigateToStage(step.name);
    card.addEventListener("click", openStage);
    card.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openStage();
    });
  });
}

const ICON_RUNNING = `<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="3.2" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="5 3"/></svg>`;

function statusPillText(status) {
  return { ok: "已完成", warn: "有警告", failed: "未通过", blocked: "受阻", running: "进行中" }[status] || "未开始";
}

/* ---------- 主操作（概念图右侧按钮） ---------- */
function renderActions(data) {
  const box = $("case-actions");
  const s = data.stages;
  let primary = null, primaryFn = null, primaryUiFn = null, primaryStage = null, primaryOpensAgent = false;
  const runningStage = STAGE_ORDER.find((name) => s[name]?.status === "running");
  const maskNeedsReview = requiresMaskReview(s);
  const requestedSurfaceScope = modelScopeParams(data.case_id).params.model_scope;
  const existingSurfaceScope = s.surface?.data?.model_scope || s.surface?.data?.scope || (s.surface?.status === "ok" ? "main_airway" : null);
  const surfaceScopeChanged = ["ok", "warn"].includes(s.surface?.status) && existingSurfaceScope && requestedSurfaceScope !== existingSurfaceScope;
  const singleBranchSurface = s.surface?.data?.endplanes?.source === "user_confirmed_local_oblique_planes";
  if (data.job?.status === "failed" || s.import.status === "failed" || s.qc.status === "failed") {
    primary = "导入未完成，请重新导入";
    primaryStage = "import";
  } else if (data.job?.status === "running" || runningStage) {
    const runningName = data.job?.stage && data.job.stage !== "preview" ? data.job.stage : runningStage;
    primary = runningName ? `正在${STAGE_LABELS[runningName] || runningName}` : "正在处理导入";
    primaryStage = runningName || "import";
  } else if (state.agentReturnRefreshingCaseId === data.case_id) {
    primary = "正在同步病例与端口状态…";
    primaryStage = "surface";
  } else if (state.agentReturnRefreshErrorCaseId === data.case_id) {
    primary = "重试同步病例与端口状态";
    primaryStage = "surface";
    primaryUiFn = () => refreshCaseAfterAgent(data.case_id);
  } else if (maskNeedsReview) {
    primary = "下一步：打开 CT 复核";
    primaryStage = "segment";
    primaryUiFn = () => {
      state.mode = "slices";
      refreshViewer();
      $("viewer-card").scrollIntoView({ behavior: "smooth", block: "start" });
      window.setTimeout(() => $("btn-edit-mask")?.focus?.(), 250);
    };
  } else if (["ok", "warn"].includes(s.qc.status) && ["pending", "failed"].includes(s.segment.status)) {
    primary = "开始分割"; primaryStage = "segment"; primaryFn = () => api(`/api/cases/${data.case_id}/segment`, { method: "POST", body: {} });
  } else if (!data.synthetic && s.segment.status === "ok" && !endplaneSaved()) {
    primaryStage = "surface";
    const downstreamUntouched = ["surface", "cfd_prep", "cfd_run", "report"]
      .every((name) => s[name]?.status === "pending");
    if (!downstreamUntouched) {
      primary = "检查已有模型与端口";
      primaryUiFn = () => {
        window.WindcatcherQuickPreview?.openAdvanced?.();
        focusStageTarget($("detail-surface") || $("viewer-card"));
      };
    } else if (state.endplanes?.status === "loading") {
      primary = "正在核对已有端口…";
    } else if (state.endplanes?.status === "error") {
      primary = "重试读取端口状态";
      primaryUiFn = () => loadEndplaneProfile(endplaneContext(), true);
    } else {
      primary = "自动处理当前病例";
      primaryOpensAgent = true;
      primaryUiFn = () => {
        if (state.maskEditor.dirty || state.maskEditor.saving || state.maskEditor.autoCleaning) {
          toast("请先保存当前掩膜编辑或完成清理，再开始自动处理", true);
          return;
        }
        if (!guardEndplaneDraft()) return;
        window.location.hash = "agent";
        showRoute("agent");
        if (window.WindcatcherOrchestration?.startCurrentCase) {
          void window.WindcatcherOrchestration.startCurrentCase(data.case_id)
            .catch((error) => toast(error.message || "自动处理未能启动", true));
        } else toast("自动处理页尚未就绪，请在该页选择当前病例后启动", true);
      };
    }
  } else if (["ok", "warn"].includes(s.segment.status) && (["pending", "failed"].includes(s.surface.status) || surfaceScopeChanged)) {
    primary = surfaceScopeChanged ? `按${MODEL_SCOPE_OPTIONS[getModelScope(data.case_id)] || "所选范围"}重新生成 STL` : "生成 STL 与质检";
    primaryStage = "surface"; primaryFn = () => api(`/api/cases/${data.case_id}/surface`, { method: "POST", body: modelScopeParams(data.case_id) });
  } else if (["ok", "warn"].includes(s.surface.status) && ["pending", "failed", "blocked"].includes(s.cfd_prep.status)) {
    const disconnectedFullCandidate = requestedSurfaceScope === "full_candidate" && Number(s.surface.data?.connected_components) > 1;
    if (singleBranchSurface || disconnectedFullCandidate) {
      primary = singleBranchSurface ? "单支段模型：查看 CFD 限制" : "复核断开的鼻窦与旁支";
      primaryStage = "surface";
      primaryUiFn = () => focusStageTarget($("detail-surface") || $("viewer-card"));
    } else {
      primary = s.surface.status === "warn" ? "几何有警告，检查后生成 CFD 算例" : "生成 CFD 算例";
      primaryStage = "cfd_prep";
      primaryFn = () => {
        const config = getCfdParallelConfig(data.case_id);
        return api(`/api/cases/${data.case_id}/cfd-prep`, { method: "POST", body: { params: { parallel_profile: config.profile, custom_subdomains: config.custom_subdomains } } });
      };
    }
  } else if (s.cfd_prep.status === "ok" && ["pending", "failed", "blocked"].includes(s.cfd_run.status)) {
    primaryStage = "cfd_run";
    const target = getCfdComputeTarget(data.case_id);
    if (target === "gpu" && !cfdGpuAvailability().available) {
      primary = "GPU 求解未就绪";
    } else {
      primary = target === "gpu" ? "开始 GPU 气流求解" : "开始气流求解";
      primaryFn = () => {
        const selectedTarget = getCfdComputeTarget(data.case_id);
        if (selectedTarget === "gpu" && !cfdGpuAvailability().available) {
          toast(cfdGpuAvailability().reason, true);
          return null;
        }
        return api(`/api/cases/${data.case_id}/cfd-run`, { method: "POST", body: { compute_target: selectedTarget } });
      };
    }
  } else if (s.report.status !== "ok") {
    primary = "生成报告"; primaryStage = "report"; primaryFn = () => api(`/api/cases/${data.case_id}/report`);
  } else {
    primary = "重新生成报告"; primaryStage = "report"; primaryFn = () => api(`/api/cases/${data.case_id}/report`);
  }
  const reportReady = s.report.status === "ok" && !maskNeedsReview;
  const showResourceCard = !singleBranchSurface && ["ok", "warn"].includes(s.surface.status) && ["pending", "failed", "blocked", "ok", "warn"].includes(s.cfd_prep.status);
  const resourceLocked = s.cfd_prep.status === "running" || s.cfd_run.status === "running";
  box.innerHTML = `${showResourceCard ? cfdResourceCard(data, { locked: resourceLocked }) : ""}
    <button id="act-primary" class="button ${primaryFn || primaryOpensAgent ? "button-primary" : "button-quiet"}" ${primaryOpensAgent ? 'data-action="start-agent" ' : ""}${(!primaryFn && !primaryUiFn) || state.clientRunningStage === primaryStage || s[primaryStage]?.status === "running" ? "disabled" : ""}>${primary}</button>
    <button id="act-report-dl" class="button button-quiet" ${reportReady ? "" : "disabled"}>导出报告</button>`;
  box.querySelectorAll("input[name='cfd-parallel-profile']").forEach((input) => input.addEventListener("change", () => {
    const profile = input.value;
    setCfdParallelConfig(data.case_id, { profile });
    renderActions(state.case);
  }));
  box.querySelectorAll("input[name='cfd-compute-target']").forEach((input) => input.addEventListener("change", () => {
    if (input.value === "gpu" && !cfdGpuAvailability().available) {
      toast(cfdGpuAvailability().reason, true);
      renderActions(data);
      return;
    }
    setCfdComputeTarget(data.case_id, input.value);
    renderActions(data);
  }));
  box.querySelector("#cfd-custom-subdomains")?.addEventListener("change", (event) => {
    const max = Number(event.currentTarget.max) || 1;
    const value = Math.max(1, Math.min(max, Math.round(Number(event.currentTarget.value) || 1)));
    event.currentTarget.value = String(value);
    setCfdParallelConfig(data.case_id, { custom_subdomains: value, profile: "custom" });
    renderActions(state.case);
  });
  box.querySelector("#act-cfd-rebuild")?.addEventListener("click", (event) => {
    const config = getCfdParallelConfig(data.case_id);
    runAction(event.currentTarget, () => api(`/api/cases/${data.case_id}/cfd-prep`, {
      method: "POST",
      body: { params: { parallel_profile: config.profile, custom_subdomains: config.custom_subdomains } },
    }), "cfd_prep");
  });
  $("act-primary").addEventListener("click", (event) => {
    if (primaryStage === "cfd_run" && getCfdComputeTarget(data.case_id) === "gpu") {
      const availability = cfdGpuAvailability();
      if (!availability.available) {
        toast(availability.reason, true);
        renderActions(data);
        return;
      }
    }
    if (primaryUiFn) primaryUiFn();
    else if (primaryFn) runAction(event.currentTarget, primaryFn, primaryStage);
  });
  $("act-report-dl").addEventListener("click", () => window.location.href = `/api/cases/${data.case_id}/report-file`);
}

async function runAction(button, fn, stageName) {
  if (!guardEndplaneDraft()) return;
  window.WindcatcherMonitor?.record("stage", "action_started", { stage: stageName, status: "running" }, button?.id);
  const capturedCaseId = state.caseId;
  state.clientRunningStage = stageName;
  busy(button, true);
  renderSteps(state.case);
  startCasePolling();
  try {
    const result = await fn();
    if (result && ["failed", "missing", "blocked"].includes(result.status)) {
      const detail = {
        status: result.status === "missing" ? "blocked" : result.status,
        reason: result.message || "该阶段未完成",
        remediation: result.remediation || [],
      };
      throw Object.assign(new Error(detail.reason), { detail });
    }
    if (result?.geom_qc?.verdict === "fail") {
      const reasons = Array.isArray(result.geom_qc.reasons) ? result.geom_qc.reasons : [];
      const detail = {
        status: "failed",
        reason: "几何质检未通过，暂不能进入 CFD 算例",
        remediation: [
          ...reasons,
          "回到三平面 CT 视图修正气道掩膜，确认鼻孔端和鼻咽端都保留开口",
          "修正并确认后，重新生成 STL 与几何质检",
        ],
        geom_qc: result.geom_qc,
      };
      throw Object.assign(new Error(detail.reason), { detail });
    }
    const fresh = await api("/api/cases/" + capturedCaseId);
    if (state.caseId !== capturedCaseId) return;
    state.case = fresh;
    state.clientRunningStage = null;
    renderCase();
    if (stageName === "surface" && hasSurfaceModel()) {
      activateStlPreview({ reload: true });
      focusStageTarget($("viewer-card"));
    }
    toast("完成");
    window.WindcatcherMonitor?.record("stage", "action_finished", { stage: stageName, status: "ok" }, button?.id);
  } catch (error) {
    window.WindcatcherMonitor?.record("stage", "action_failed", { stage: stageName, status: error.detail?.status || "failed" }, button?.id);
    if (state.caseId !== capturedCaseId) return;
    state.clientRunningStage = null;
    // 后端会把 failed/blocked 落盘；失败时重新取一次，步骤卡也要同步到最终状态。
    let stageDiagnosisRendered = false;
    try {
      state.case = await api("/api/cases/" + capturedCaseId);
      renderCase();
      stageDiagnosisRendered = stageName === "cfd_run" && state.case.stages?.cfd_prep?.status === "ok" && state.case.stages?.cfd_run?.data?.run_status?.status === "failed";
    } catch { /* 保留下面的错误面板，避免二次读取失败遮住原始原因 */ }
    if (!error.detail?.geom_qc && !stageDiagnosisRendered) {
      renderErrorPanel(error.detail || { reason: String(error.message), remediation: [] });
    }
    toast(error.detail?.reason || "操作失败", true);
  } finally {
    busy(button, false);
    if (state.caseId === capturedCaseId && state.case && !hasRunningStages(state.case)) stopCasePolling();
  }
}

function renderErrorPanel(detail) {
  const cls = detail.status === "failed" ? "is-fail" : "";
  $("detail-panels").innerHTML = `
    <div class="card"><div class="card-head"><h2>这一步没有执行</h2><span class="pill ${detail.status === "failed" ? "pill-fail" : "pill-warn"}"><i></i>${detail.status || "blocked"}</span></div>
    <div class="card-body">
      <p>${escapeHtml(localizeMaskInstruction(detail.reason || ""))}</p>
      <div class="remedy ${cls}"><p>下一步建议</p><ol>${(detail.remediation || []).map((item) => `<li>${escapeHtml(localizeMaskInstruction(item))}</li>`).join("")}</ol></div>
    </div></div>`;
}

/* ---------- 指标卡（大数字） ---------- */

function configureMetricCard(valueId, metric) {
  const valueNode = $(valueId);
  const card = valueNode?.closest(".metric-card");
  if (!valueNode || !card) return;
  const copy = valueNode.parentElement;
  const labelNode = copy?.querySelector(".metric-label");
  if (labelNode) labelNode.textContent = metric.label;
  let stateNode = copy?.querySelector(".metric-state");
  if (!stateNode && copy) {
    stateNode = document.createElement("div");
    stateNode.className = "metric-state";
    copy.appendChild(stateNode);
  }
  if (stateNode) {
    stateNode.className = "metric-state" + (metric.level ? " is-" + metric.level : "");
    stateNode.textContent = metric.status || "";
  }
  valueNode.replaceChildren();
  if (metric.value === null || metric.value === undefined) {
    const empty = document.createElement("span");
    empty.className = "metric-none";
    empty.textContent = metric.empty || "未生成";
    valueNode.appendChild(empty);
  } else {
    valueNode.appendChild(document.createTextNode(String(metric.value)));
    if (metric.unit) {
      const unit = document.createElement("small");
      unit.textContent = metric.unit;
      valueNode.appendChild(unit);
    }
  }
  const actionable = Boolean(metric.actionable && metric.stage);
  card.classList.toggle("is-actionable", actionable);
  card.classList.toggle("is-warning", metric.level === "warn");
  card.dataset.metric = metric.key || "";
  card.dataset.stage = metric.stage || "";
  card.setAttribute("role", "button");
  card.tabIndex = actionable ? 0 : -1;
  card.setAttribute("aria-disabled", actionable ? "false" : "true");
  card.setAttribute("aria-label", metric.ariaLabel || metric.label + "：" + (metric.status || metric.empty || ""));
  card.title = metric.hint || "";
  card.onclick = actionable ? () => navigateToStage(metric.stage) : null;
  card.onkeydown = actionable ? (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    navigateToStage(metric.stage);
  } : null;
}

function renderMetrics(data) {
  const s = data?.stages || {};
  const hasCase = Boolean(data);
  const segment = s.segment || {};
  const surface = s.surface || {};
  const cfdPrep = s.cfd_prep || {};
  const cfdRun = s.cfd_run || {};
  const segmentStatus = segment.status || "pending";
  const surfaceStatus = surface.status || "pending";
  const cfdPrepStatus = cfdPrep.status || "pending";
  const cfdRunStatus = cfdRun.status || "pending";
  const cleanupPending = hasCase && externalAirCleanupState(s).pending;
  const maskNeedsReview = hasCase && requiresMaskReview(s);
  const segmentReady = ["ok", "warn"].includes(segmentStatus);
  const surfaceReady = ["ok", "warn"].includes(surfaceStatus);
  const cfdPrepReady = ["ok", "warn"].includes(cfdPrepStatus);
  const pressureDriven = cfdPrep.data?.inlet_mode === "pressure";
  const boundaryLabel = pressureDriven ? "压力边界 · 速度由求解获得"
    : Number.isFinite(cfdPrep.data?.total_flow_lpm) ? `${fmt(cfdPrep.data.total_flow_lpm, " L/min")} 工况 · 边界设定`
    : "入口工况未记录";
  const volume = segmentReady && Number.isFinite(segment.data?.volume_ml) ? round(segment.data.volume_ml) : null;
  const velocity = cfdPrepReady && Number.isFinite(cfdPrep.data?.mean_inlet_velocity_mps) ? round(cfdPrep.data.mean_inlet_velocity_mps) : null;
  const deviation = surfaceReady && Number.isFinite(surface.data?.volume_deviation_percent) ? round(surface.data.volume_deviation_percent) : null;
  const volumeStatus = !hasCase ? "请选择病例"
    : segmentStatus === "running" ? "正在生成候选掩膜"
    : !segmentReady ? "先完成气道分割"
    : cleanupPending ? "含体外空气 · 不可作为气道体积"
    : maskNeedsReview ? "候选掩膜 · 待人工复核" : "分割结果已生成";
  const velocityStatus = !hasCase ? "请选择病例"
    : cfdPrepStatus === "running" ? "正在生成 CFD 算例"
    : !cfdPrepReady ? "先生成 CFD 算例"
    : cfdRunStatus === "running" ? `${boundaryLabel}，正在求解`
    : ["ok", "warn"].includes(cfdRunStatus) ? `${boundaryLabel}，求解完成待复核`
    : ["failed", "blocked"].includes(cfdRunStatus) ? `${boundaryLabel}，求解受阻` : `${boundaryLabel}，尚未求解`;
  const deviationStatus = !hasCase ? "请选择病例"
    : surfaceStatus === "running" ? "正在执行几何质检"
    : !surfaceReady ? "先生成 STL 与几何质检"
    : maskNeedsReview ? "候选结果 · 先确认分割" : surface.data?.verdict === "pass" ? "体积对照已完成" : verdictText(surface.data?.verdict);
  configureMetricCard("mt-volume", {
    key: "volume",
    label: cleanupPending ? "未清理阈值候选" : maskNeedsReview ? "候选气道体积" : "气道体积",
    value: cleanupPending ? null : volume,
    unit: "mL",
    empty: cleanupPending ? "待清理" : hasCase ? "未生成" : "未选择",
    status: volumeStatus,
    level: maskNeedsReview ? "warn" : segmentStatus === "ok" ? "ok" : "",
    stage: "segment",
    actionable: hasCase,
    hint: cleanupPending
      ? "原始 HU 候选含体外背景空气，体积暂不展示；点击进入三平面 CT 先清理"
      : "来源：segment/segmentation.json；点击查看三平面 CT 复核",
  });
  configureMetricCard("mt-velocity", {
    key: "velocity",
    label: "算例设定入口速度",
    value: velocity,
    unit: "m/s",
    empty: pressureDriven ? "未预设" : cfdPrepReady ? "未记录" : hasCase ? "未生成" : "未选择",
    status: velocityStatus,
    level: cfdPrepReady ? "warn" : "",
    stage: "cfd_prep",
    actionable: hasCase,
    hint: pressureDriven ? "压力边界不预设入口速度；实际速度请在求解流场中查看"
      : "来源：cfd/case_meta.json；按流量÷入口面积计算，不是患者测量值或求解场结果",
  });
  configureMetricCard("mt-deviation", {
    key: "deviation",
    label: "掩膜-模型体积差",
    value: deviation,
    unit: "%",
    empty: hasCase ? "未生成" : "未选择",
    status: deviationStatus,
    level: maskNeedsReview ? "warn" : surface.data?.verdict === "pass" ? "ok" : surfaceReady ? "warn" : "",
    stage: "surface",
    actionable: hasCase,
    hint: "来源：surface/geom_qc.json；仅表示网格与掩膜的体积对照",
  });
}

/* ---------- 病例头部信息 ---------- */
function renderCaseHead(data) {
  const sourceLabel = data.source_type === "synthetic"
    ? "合成测试数据"
    : data.source_type === "browser_upload" ? "浏览器上传 · 服务主机处理" : "本地目录";
  $("case-sub").textContent = `${sourceLabel} · ${data.created_at.slice(0, 16).replace("T", " ")}`;
  $("case-synthetic").hidden = !data.synthetic;
  const s = data.stages;
  let message = "等待开始。";
  const reviewStatusLoading = s.segment?.status === "ok" && !state.ctViewer.meta;
  if (data.job?.status === "running") {
    message = data.job.message || `正在处理：${STAGE_LABELS[data.job.stage] || data.job.stage || "导入"}`;
  } else if (data.job?.status === "failed") {
    message = data.job.message || "后台导入未完成，请查看对应阶段的错误说明。";
  } else if (externalAirCleanupState(s).pending) {
    message = "原始 HU 候选包含体外背景空气；请先在三平面视窗去除体外空气，再逐层复核气道。";
  } else if (maskNeedsConfirmation(s)) {
    message = "候选气道掩膜待人工确认；确认或导入修正后，需要重新生成 STL、CFD 算例和报告。";
  } else if (reviewStatusLoading) {
    message = "正在核对气道掩膜的复核状态。";
  }
  const order = ["report", "cfd_run", "cfd_prep", "surface", "segment", "qc"];
  if (!maskNeedsConfirmation(s) && !reviewStatusLoading && (!data.job?.status || data.job.status === "idle" || data.job.status === "completed")) {
    for (const name of order) {
      const stage = s[name];
      if (stage.status === "failed" || stage.status === "blocked") { message = `${stageName(name)}受阻：${stage.message || ""}`; break; }
      if (stage.status === "running") { message = `正在${stageName(name)}：${stage.message || "处理中"}`; break; }
      if (stage.status === "ok" || stage.status === "warn") {
        const segmentDone = stage.data?.need_human_confirm === false ? "已人工确认" : "待人工复核";
        const map = { qc: "质控完成，可以开始分割。", segment: `分割完成（${stage.data?.volume_ml ?? "?"} mL，${segmentDone}）。`, surface: "几何质检完成，可以生成 CFD 算例。", cfd_prep: "算例已生成；求解需要本机 OpenFOAM 后端。", cfd_run: stage.message, report: "报告已生成，可导出 Markdown。" };
        message = name === "surface" && s.surface?.data?.endplanes?.source === "user_confirmed_local_oblique_planes"
          ? "单支段几何质检完成；双侧 CFD 工况尚未定义，请先核对实际裁切范围。" : map[name];
        break;
      }
    }
  }
  $("case-message-wrap").innerHTML = `<p style="margin:10px 2px;color:var(--ink-2);font-size:13px;">${escapeHtml(message)}</p>`;
}
function stageName(name) {
  return { import: "导入", anonymize: "去标识", qc: "质控", segment: "分割", surface: "表面", cfd_prep: "算例", cfd_run: "求解", report: "报告" }[name] || name;
}

function syncQuickPreview(data) {
  const primary = $("act-primary");
  const rebuild = $("act-cfd-rebuild");
  window.WindcatcherQuickPreview?.bind({
    onPrimary: () => {
      if (primary?.dataset.action !== "start-agent") window.WindcatcherQuickPreview?.openAdvanced?.();
      primary?.click();
    },
    onAdvanced: () => window.WindcatcherQuickPreview?.openAdvanced?.(),
    onRebuild: () => {
      if (!rebuild || rebuild.disabled) return;
      window.WindcatcherQuickPreview?.openAdvanced?.();
      rebuild.click();
    },
    onResults: () => {
      window.location.hash = "results";
      showRoute("results");
    },
  });
  window.WindcatcherQuickPreview?.render(data, {
    action: primary ? {
      label: primary.textContent.trim(),
      disabled: primary.disabled,
      busy: primary.classList.contains("is-busy"),
    } : { label: "打开完整工程流程", disabled: true },
    rebuild: { visible: Boolean(rebuild), disabled: rebuild?.disabled ?? true },
  });
}

function renderCase() {
  const data = state.case;
  if (!data) return;
  renderSteps(data);
  renderActions(data);
  syncQuickPreview(data);
  renderMetrics(data);
  renderCaseHead(data);
  renderDetails(data);
  refreshViewer();
  syncEndplaneContext(data);
}

/* ---------- 详情面板 ---------- */
function kvList(rows) {
  return `<dl class="kv">${rows.map(([k, v, mono]) => `<dt>${k}</dt><dd${mono ? ' class="mono"' : ""}>${v}</dd>`).join("")}</dl>`;
}

function firstRecordedNumber(...values) {
  for (const value of values) {
    const number = typeof value === "number" ? value : Number(value);
    if (Number.isFinite(number)) return number;
  }
  return null;
}

function recordedStat(value, unit = "") {
  return value === null ? "未记录" : `${fmt(value, unit)}`;
}

function modelScopeStats(data) {
  const segment = data?.stages?.segment?.data || {};
  const metrics = segment.metrics || {};
  const candidateMetrics = segment.candidate_metrics || {};
  const candidates = segment.candidates || {};
  const surface = data?.stages?.surface?.data || {};
  const manifold = surface.manifold_prep || {};
  const volumeCheck = surface.volume_check || {};
  const sourceVoxels = firstRecordedNumber(segment.source_voxels, metrics.source_voxels, candidateMetrics.voxels, manifold.source_voxels);
  const retainedVoxels = firstRecordedNumber(segment.retained_voxels, metrics.retained_voxels, metrics.voxels);
  const candidateVolume = firstRecordedNumber(segment.candidate_volume_ml, metrics.candidate_volume_ml, candidateMetrics.volume_ml);
  const retainedVolume = firstRecordedNumber(segment.retained_volume_ml, metrics.retained_volume_ml, surface.retained_volume_ml, volumeCheck.mask_volume_ml, surface.mask_volume_ml, metrics.volume_ml);
  const explicitExcluded = firstRecordedNumber(segment.excluded_volume_ml, metrics.excluded_volume_ml, manifold.excluded_volume_ml);
  const excludedVolume = explicitExcluded !== null
    ? explicitExcluded
    : candidateVolume !== null && retainedVolume !== null ? Math.max(candidateVolume - retainedVolume, 0) : null;
  const candidateList = Array.isArray(segment.candidate_components) ? segment.candidate_components : [];
  const totalComponents = firstRecordedNumber(segment.components_total, metrics.components_total, candidateMetrics.components_total, candidates.total, candidateList.length || null);
  const retainedComponents = firstRecordedNumber(segment.retained_components, metrics.retained_components, surface.retained_components, surface.connected_components, candidates.retained);
  const excludedComponents = firstRecordedNumber(segment.excluded_components, metrics.excluded_components, surface.excluded_components, candidates.excluded);
  return {
    totalComponents,
    retainedComponents,
    excludedComponents,
    candidateVolume,
    retainedVolume,
    excludedVolume,
    sourceVoxels,
    retainedVoxels,
  };
}

function renderModelScopeCard(data) {
  const segment = data?.stages?.segment?.data || {};
  const cleanupPending = externalAirCleanupState(data?.stages).pending;
  const stats = modelScopeStats(data);
  const candidateAvailable = segment.candidate_mask_available === true;
  const surfaceStage = data?.stages?.surface || {};
  const surfaceScope = surfaceStage.data?.model_scope || surfaceStage.data?.scope
    || (surfaceStage.status === "ok" ? "main_airway" : null);
  const surfaceScopeLabel = surfaceScope === "full_candidate" ? MODEL_SCOPE_OPTIONS.complete_candidate
    : surfaceScope === "main_airway" ? MODEL_SCOPE_OPTIONS.main_nasal_airway : null;
  const selected = getModelScope(data.case_id) === "complete_candidate" && !candidateAvailable
    ? "main_nasal_airway"
    : getModelScope(data.case_id);
  const capabilities = segment.model_scope_capabilities || data?.stages?.segment?.model_scope_capabilities || {};
  const completeSupported = candidateAvailable && capabilities.complete_candidate !== false && segment.complete_candidate_available !== false;
  const mainSupported = capabilities.main_nasal_airway !== false && segment.main_nasal_airway_available !== false;
  const capabilityNote = !candidateAvailable
    ? "当前病例没有保存完整候选掩膜；请重新执行气道分割后再选择完整候选。"
    : Object.keys(capabilities).length
      ? "范围能力来自当前病例的分割产物。"
      : "当前病例未回传范围能力清单；生成后仍会以服务端回执为准。";
  const scopeCard = (scope, title, text, enabled) => `<label class="model-scope-option ${selected === scope && !cleanupPending ? "is-selected" : ""} ${enabled && !cleanupPending ? "" : "is-disabled"}">
      <input type="radio" name="model-scope" value="${scope}" ${selected === scope ? "checked" : ""} ${enabled && !cleanupPending ? "" : "disabled"} />
      <span class="model-scope-copy"><strong>${title}</strong><span>${text}</span></span>
      <span class="model-scope-state">${cleanupPending ? "待清理" : enabled ? (selected === scope ? "当前选择" : "可选择") : "当前病例不可用"}</span>
    </label>`;
  const scopeStatus = cleanupPending ? "体外空气待清理" : surfaceScopeLabel ? `已生成 · ${surfaceScopeLabel}` : "待范围确认";
  const scopePill = surfaceScopeLabel && !cleanupPending ? "pill-ok" : "pill-warn";
  const statsBlock = cleanupPending ? "" : `<div class="model-scope-stats" aria-label="分割范围统计">
        <div><span>候选连通组件</span><strong>${recordedStat(stats.totalComponents)}</strong></div>
        <div><span>当前保留组件</span><strong>${recordedStat(stats.retainedComponents)}</strong></div>
        <div><span>候选体积</span><strong>${recordedStat(stats.candidateVolume, " mL")}</strong></div>
        <div><span>当前保留体积</span><strong>${recordedStat(stats.retainedVolume, " mL")}</strong></div>
        <div><span>排除体积</span><strong>${recordedStat(stats.excludedVolume, " mL")}</strong></div>
        <div><span>排除组件</span><strong>${recordedStat(stats.excludedComponents)}</strong></div>
      </div>`;
  return `<div id="detail-model-scope" class="card model-scope-card ${cleanupPending ? "is-pending" : ""}" tabindex="-1">
    <div class="card-head"><div><h2>模型范围确认</h2><span class="hint">生成 STL 前选择保留范围</span></div><span class="pill ${scopePill}"><i></i>${scopeStatus}</span></div>
    <div class="card-body">
      <p class="model-scope-intro">${cleanupPending
        ? "当前是含体外背景空气的未清理阈值候选，不能据此解读气道体积或选择建模范围。请先在三平面 CT 去除体外空气，逐层复核并确认气道范围。"
        : "这里决定送入网格与 CFD 的气道范围。完整候选会保留 CT 阈值候选中落在复核范围内的鼻窦/旁支，但候选可能含误检，需逐层确认后才能用于 CFD；主鼻腔模型只用于主通道气流比较。鼻窦旁支不会自动设置为入口或出口，入口、出口仍在 SimScale / CFD 阶段确认。"}</p>
      <div class="model-scope-options" role="radiogroup" aria-label="${cleanupPending ? "体外空气清理后可选择模型范围" : "选择模型范围"}">
        ${scopeCard("main_nasal_airway", "主鼻腔气流模型", "仅提交主鼻腔通道，用于鼻塞或空鼻症主气流比较。", mainSupported)}
        ${scopeCard("complete_candidate", "完整候选（保留鼻窦/旁支）", "按需选择，逐层复核窦口、体外空气和不确定区域。", completeSupported)}
      </div>
      ${statsBlock}
      <p class="model-scope-note">${cleanupPending
        ? "未清理候选的范围统计暂不展示；去除体外空气并完成复核后，才能选择范围和生成 STL。"
        : `${capabilityNote} “未记录”表示当前产物没有保存这项统计，不代表数值为 0。${surfaceScopeLabel ? "如需更换范围，请重新生成 STL 与质检。" : "完成选择后，再点击“生成 STL 与质检”。"}`}</p>
    </div>
  </div>`;
}

function bindModelScopeCard(data) {
  document.querySelectorAll("input[name='model-scope']").forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked || input.disabled) return;
      if (input.value !== getModelScope(data.case_id) && !guardEndplaneDraft()) {
        const old = document.querySelector(`input[name='model-scope'][value='${getModelScope(data.case_id)}']`);
        if (old) old.checked = true;
        return;
      }
      setModelScope(data.case_id, input.value);
      renderDetails(data);
      syncEndplaneContext(data);
      renderActions(data);
      toast(`已选择：${MODEL_SCOPE_OPTIONS[input.value]}。生成 STL 时会提交此范围。`);
    });
  });
}

function renderCfdProgress(runStatus, stageData, status) {
  if (!["running", "failed"].includes(status)) return "";
  const number = (value) => value == null || value === "" || typeof value === "boolean" || !Number.isFinite(Number(value)) ? null : Number(value);
  const progress = runStatus.progress || stageData.progress || {};
  const solver = runStatus.solver || stageData.solver || {};
  const steps = Array.isArray(runStatus.steps) ? runStatus.steps : [];
  const completed = number(progress.completed) ?? steps.filter((step) => step.returncode === 0).length;
  const total = number(progress.total);
  // 旧记录只列出已执行的命令，不能把它们的数量当成整条流程的总步数。
  const percent = total > 0 ? Math.max(0, Math.min(100, number(progress.percent) ?? completed / total * 100)) : null;
  const elapsed = number(runStatus.elapsed_seconds ?? stageData.elapsed_seconds);
  const stepElapsed = number(runStatus.step_elapsed_seconds);
  const duration = (seconds) => seconds == null ? "未记录" : `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
  const name = runStatus.current_step || runStatus.failed_step || stageData.current_step;
  const stepPercent = number(progress.current_step_percent ?? (name === "simpleFoam" ? solver.iteration_percent : null));
  const mesh = (runStatus.execution || stageData.execution || {}).mesh || {};
  const meshCount = Number.isInteger(mesh.requested_subdomains) && mesh.requested_subdomains > 0 ? mesh.requested_subdomains : null;
  const meshLabel = mesh.mode === "parallel" ? `并行生成气道网格${meshCount ? `（${meshCount} 个进程）` : "（进程数未记录）"}`
    : mesh.mode === "serial" ? "单进程生成气道网格" : "生成气道网格（执行方式未记录）";
  const labels = { mpiPreflight: "检查 MPI 并行启动", meshCacheCheck: "核对已有网格", prepareRun: "归档旧结果，准备本次计算", blockMesh: "生成背景网格", decomposeMesh: "分配网格到 CPU 子域", snappyHexMesh: meshLabel, reconstructMesh: "合并并行网格", checkMesh: "检查网格质量", decomposePar: "分配到 CPU 子域", simpleFoam: "计算气流", reconstructPar: "合并并行结果", foamToVTK: "导出可视化文件" };
  const stepLabel = labels[name] || name || "准备执行";
  const currentPercent = stepPercent == null ? null : Math.max(0, Math.min(100, stepPercent));
  const active = status === "running";
  const stepCount = total > 0 ? `${completed}/${total} 个步骤已完成` : `${completed} 个步骤已完成，总步数未记录`;
  const currentProgress = currentPercent != null
    ? `<progress class="cfd-result-progress" max="100" value="${currentPercent}" aria-label="外层迭代相对上限的比例"></progress>`
    : active ? '<progress class="cfd-result-progress" aria-label="当前步骤正在处理，百分比未知"></progress>' : "";
  return `<div class="cfd-live-progress${active ? "" : " is-failed"}" aria-live="polite">
    <div class="progress-copy"><strong>${active ? "当前步骤" : "停止步骤"}：${escapeHtml(stepLabel)}</strong><b>${currentPercent == null ? (active ? "处理中" : "已停止") : `${currentPercent.toFixed(1)}%`}</b></div>
    ${currentProgress}
    ${percent != null ? `<div class="progress-copy cfd-step-count"><span>${escapeHtml(stepCount)}</span><b>${percent.toFixed(1)}%</b></div><progress class="cfd-result-progress" max="100" value="${percent}" aria-label="已完成的命令步骤比例"></progress>` : `<p>${escapeHtml(stepCount)}</p>`}
    <p>总耗时 ${escapeHtml(duration(elapsed))}${stepElapsed == null ? "" : ` · 本步骤 ${escapeHtml(duration(stepElapsed))}`}</p>
    <p>${currentPercent == null ? "当前步骤无法计算可靠的完成百分比。" : "当前百分比表示迭代数相对上限的比例，是否收敛需看残差。"}步骤完成比例不代表剩余时间。</p>
  </div>`;
}

function scenarioPill(scenario) {
  const cfd = scenario.cfd || {};
  if (cfd.status === "solved") return ["pill-warn", "已求解 · 待复核"];
  if (cfd.status === "prepared") return ["pill-warn", "算例已生成"];
  if (cfd.status === "running") return ["pill-warn", "正在求解"];
  if (cfd.status === "failed") return ["pill-fail", "求解失败"];
  if (scenario.review?.approved) return ["pill-ok", "已审核"];
  if (scenario.placement?.confirmed) return ["pill-warn", "待审核"];
  return ["pill-mute", "待摆位"];
}

function scenarioVectorInputs(name, value) {
  const values = Array.isArray(value) && value.length === 3 ? value : [0, 0, 0];
  return `<div class="scenario-vector" role="group" aria-label="${name}">${values.map((item, index) => `<label>${["X", "Y", "Z"][index]}<input name="${name}" type="number" step="any" value="${escapeHtml(String(item))}" inputmode="decimal" /></label>`).join("")}</div>`;
}

function createScenarioViewer3d(host) {
  if (!host || typeof THREE === "undefined" || !THREE.STLLoader || !THREE.OrbitControls) return null;
  const width = () => host.clientWidth || 420;
  const height = () => host.clientHeight || 300;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d1519);
  const camera = new THREE.PerspectiveCamera(36, width() / height(), 0.1, 1000000);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(width(), height());
  renderer.domElement.setAttribute("aria-label", "气道与鼻塞三维叠加预览");
  host.replaceChildren(renderer.domElement);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x162629, 1.15));
  const key = new THREE.DirectionalLight(0xffffff, 1.0);
  key.position.set(1.4, 1.5, 1.1);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x70cfc0, 0.5);
  rim.position.set(-1.2, -0.8, -1.4);
  scene.add(rim);
  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  const group = new THREE.Group();
  scene.add(group);
  let airway = null;
  let plug = null;
  let requestId = 0;
  let disposed = false;

  function disposeMesh(mesh) {
    if (!mesh) return;
    mesh.geometry?.dispose();
    if (Array.isArray(mesh.material)) mesh.material.forEach((material) => material.dispose());
    else mesh.material?.dispose();
  }

  function clearMeshes() {
    if (airway) group.remove(airway);
    if (plug) group.remove(plug);
    disposeMesh(airway);
    disposeMesh(plug);
    airway = null;
    plug = null;
  }

  function fit() {
    const box = new THREE.Box3().setFromObject(group);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.x, size.y, size.z) || 1;
    controls.target.copy(center);
    camera.position.copy(center.clone().add(new THREE.Vector3(radius * 0.9, radius * 0.55, radius * 1.15)));
    camera.near = Math.max(0.05, radius / 10000);
    camera.far = Math.max(100, radius * 30);
    camera.updateProjectionMatrix();
  }

  function resize() {
    camera.aspect = width() / height();
    camera.updateProjectionMatrix();
    renderer.setSize(width(), height());
  }

  function setPlacement(placement) {
    if (!plug) return;
    const translation = placement?.translation_mm || [0, 0, 0];
    const rotation = placement?.rotation_deg || [0, 0, 0];
    const scale = placement?.scale || [1, 1, 1];
    plug.position.set(...translation.map(Number));
    plug.rotation.order = "ZYX";
    plug.rotation.set(...rotation.map((value) => Number(value) * Math.PI / 180));
    plug.scale.set(...scale.map(Number));
  }

  function loadGeometry(url) {
    return new Promise((resolve, reject) => {
      const loader = new THREE.STLLoader();
      loader.load(url, resolve, undefined, reject);
    });
  }

  async function load(baseUrl, plugUrl, placement, onStatus) {
    const currentRequest = ++requestId;
    clearMeshes();
    onStatus?.("正在加载气道与鼻塞模型…", false);
    try {
      const loaded = await Promise.allSettled([loadGeometry(baseUrl), loadGeometry(plugUrl)]);
      const failure = loaded.find((item) => item.status === "rejected");
      if (failure || disposed || currentRequest !== requestId) {
        loaded.filter((item) => item.status === "fulfilled").forEach((item) => item.value.dispose());
        if (failure) throw failure.reason;
        return;
      }
      const airwayGeometry = loaded[0].value;
      const plugGeometry = loaded[1].value;
      airwayGeometry.computeVertexNormals();
      plugGeometry.computeVertexNormals();
      airway = new THREE.Mesh(airwayGeometry, new THREE.MeshPhysicalMaterial({
        color: 0x2bb3a6, roughness: 0.28, metalness: 0, transparent: true, opacity: 0.34,
        side: THREE.DoubleSide, depthWrite: false,
      }));
      plug = new THREE.Mesh(plugGeometry, new THREE.MeshStandardMaterial({
        color: 0xe59b54, roughness: 0.32, metalness: 0.02, transparent: true, opacity: 0.96,
      }));
      airway.renderOrder = 1;
      plug.renderOrder = 2;
      group.add(airway, plug);
      setPlacement(placement);
      fit();
      onStatus?.("可旋转、滚轮缩放；青色为气道，橙色为鼻塞。输入变化会实时预览。", false);
    } catch (error) {
      if (disposed || currentRequest !== requestId) return;
      onStatus?.("叠加预览加载失败：请先确认当前病例已生成气道 STL。", true);
    }
  }

  (function animate() {
    if (disposed) return;
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }());
  window.addEventListener("resize", resize);
  const viewer = {
    load,
    setPlacement,
    fit,
    resize,
    destroy() {
      disposed = true;
      requestId += 1;
      window.removeEventListener("resize", resize);
      clearMeshes();
      controls.dispose?.();
      renderer.dispose?.();
      renderer.domElement.remove();
    },
  };
  state.scenarioViewers.add(viewer);
  return viewer;
}

function destroyScenarioViewers() {
  state.scenarioViewers.forEach((viewer) => viewer.destroy?.());
  state.scenarioViewers.clear();
}

function renderScenarioList(items) {
  const target = $("scenario-list");
  if (!target) return;
  destroyScenarioViewers();
  if (!Array.isArray(items)) throw new Error("鼻塞方案接口返回格式无效");
  if (!items.length) {
    target.innerHTML = `<div class="scenario-empty"><strong>还没有鼻塞方案</strong><p>上传一个 STL 后，服务会保留原始文件、毫米规范化文件和每次摆位版本。当前病例的气道基准完成后，才能生成独立 CFD 对比。</p></div>`;
    return;
  }
  target.innerHTML = items.map((scenario) => {
    const [pillClass, pillText] = scenarioPill(scenario);
    const placement = scenario.placement || {};
    const version = Number(scenario.current_version) || 1;
    const reviewReady = Boolean(scenario.review?.approved && scenario.base_status === "current");
    const cfdReady = Boolean(["prepared", "solved"].includes(scenario.cfd?.status));
    const running = scenario.cfd?.status === "running";
    const disabledReview = !placement.confirmed || scenario.base_status !== "current" || running;
    const disabledCfd = !reviewReady || running;
    return `<article class="scenario-item" data-scenario-id="${escapeHtml(scenario.scenario_id)}" data-version="${version}">
      <div class="scenario-item-head"><div><strong>${escapeHtml(scenario.scenario_id)}</strong><span>当前版本 v${String(version).padStart(4, "0")} · ${escapeHtml(scenario.model?.unit_input || "mm")} 输入</span></div><span class="pill ${pillClass}"><i></i>${pillText}</span></div>
      <div class="scenario-item-meta"><span>几何：${escapeHtml(scenario.model?.qc?.verdict || "未记录")}</span><span>气道基准：${escapeHtml(scenario.base_status || "未记录")}</span><span>CFD：${escapeHtml(scenario.cfd?.status || "未开始")}</span></div>
      <div class="scenario-placement-layout"><div class="scenario-preview"><div class="scenario-preview-head"><strong>气道 × 鼻塞叠加预览</strong><span>当前版本 v${String(version).padStart(4, "0")}</span><button type="button" class="button button-quiet button-compact" data-scenario-open-preview>重置视角</button></div><div class="scenario-viewer" data-scenario-viewer></div><p class="scenario-viewer-status" data-scenario-viewer-status role="status" aria-live="polite">正在准备叠加预览…</p><div class="scenario-viewer-legend"><span><i class="scenario-airway-swatch"></i>气道</span><span><i class="scenario-plug-swatch"></i>鼻塞</span></div></div>
        <details class="scenario-placement" ${placement.confirmed ? "" : "open"}><summary>人工核对位置与尺寸</summary>
          <form class="scenario-placement-form" data-scenario-placement><fieldset ${running ? "disabled" : ""}>
            <label>平移（mm）${scenarioVectorInputs("translation_mm", placement.translation_mm)}</label>
            <label>旋转（度）${scenarioVectorInputs("rotation_deg", placement.rotation_deg)}</label>
            <label>缩放${scenarioVectorInputs("scale", placement.scale || [1, 1, 1])}</label>
            <div class="scenario-form-actions"><button type="submit" class="button button-primary button-compact">保存新版本</button><span>每次保存都会生成新版本，旧版本保留。</span></div>
          </fieldset></form>
        </details>
      </div>
      <div class="scenario-item-actions">
        <input class="scenario-review-role" type="text" value="engineer" maxlength="80" aria-label="审核角色" placeholder="审核角色" />
        <button type="button" class="button button-quiet button-compact" data-scenario-review ${disabledReview ? "disabled" : ""}>${scenario.review?.approved ? "重新审核" : "提交人工审核"}</button>
        <button type="button" class="button button-quiet button-compact" data-scenario-prep ${disabledCfd ? "disabled" : ""}>生成独立 CFD</button>
        <button type="button" class="button button-primary button-compact" data-scenario-run ${!cfdReady || disabledCfd ? "disabled" : ""}>求解并对比</button>
        <a class="button button-quiet button-compact" href="${escapeHtml(scenario.download_url || "#")}">下载资产</a>
        <button type="button" class="button button-quiet button-compact" data-scenario-comparison ${!cfdReady ? "disabled" : ""}>查看前后对比</button>
      </div>
      <div class="scenario-task-status" data-scenario-task-status role="status" aria-live="polite">${scenario.cfd?.status === "running" ? "鼻塞 CFD 正在后台执行，页面会自动更新状态。" : ""}</div><div class="scenario-comparison" data-scenario-comparison-output hidden aria-live="polite"></div>
    </article>`;
  }).join("");
}

async function loadScenarioList(caseId) {
  const target = $("scenario-list");
  if (!target || state.caseId !== caseId) return;
  destroyScenarioViewers();
  target.innerHTML = `<p class="scenario-loading" role="status">正在读取鼻塞方案记录…</p>`;
  try {
    const items = await api(`/api/cases/${encodeURIComponent(caseId)}/scenarios`);
    if (state.caseId !== caseId || !$("scenario-list")) return;
    renderScenarioList(items);
    bindScenarioItems(caseId);
  } catch (error) {
    if (state.caseId !== caseId) return;
    target.innerHTML = `<div class="scenario-error"><strong>鼻塞方案暂时无法读取</strong><p>${escapeHtml(error.detail?.reason || error.message || "请求失败")}</p><button type="button" class="button button-quiet button-compact" data-scenario-retry>重试</button></div>`;
    target.querySelector("[data-scenario-retry]")?.addEventListener("click", () => loadScenarioList(caseId));
  }
}

function scenarioFormVectors(form, name, fallback) {
  const inputs = [...form.querySelectorAll(`input[name="${name}"]`)];
  if (inputs.length !== 3) throw new Error(`${name} 需要填写 X、Y、Z 三个值`);
  const values = inputs.map((input) => input.value.trim() === "" ? NaN : Number(input.value));
  if (values.some((value) => !Number.isFinite(value))) throw new Error(`${name} 必须是有限数字，不能留空`);
  return values;
}

function scenarioPlacementSignature(form) {
  try {
    return JSON.stringify(["translation_mm", "rotation_deg", "scale"].map((name) => scenarioFormVectors(form, name)));
  } catch {
    return null;
  }
}

function syncScenarioCardActions(card, dirty) {
  card.querySelectorAll("[data-scenario-review], [data-scenario-prep], [data-scenario-run], [data-scenario-comparison]").forEach((control) => {
    control.disabled = control.dataset.scenarioBaseDisabled === "true" || dirty;
  });
}

function scenarioCardAction(card, button, action) {
  if (!card || card.dataset.busy === "true" || (button && button.disabled)) return;
  const caseId = state.caseId;
  const scenarioId = card.dataset.scenarioId;
  card.dataset.busy = "true";
  const previousDisabled = new Map([...card.querySelectorAll("button, input, select")].map((control) => [control, control.disabled]));
  card.querySelectorAll("button, input, select").forEach((control) => { control.disabled = true; });
  Promise.resolve().then(action)
    .then(async () => {
      if (state.caseId === caseId && card.isConnected && card.dataset.scenarioId === scenarioId) await loadScenarioList(caseId);
    })
    .catch((error) => toast(error.detail?.reason || error.message || "鼻塞方案操作失败", true))
    .finally(() => {
      if (card.isConnected) {
        card.dataset.busy = "false";
        card.querySelectorAll("button, input, select").forEach((control) => { control.disabled = previousDisabled.get(control) ?? false; });
      }
    });
}

function renderScenarioComparison(output, comparison) {
  if (!output) return;
  output.hidden = false;
  if (!comparison || comparison.status === "missing") {
    output.innerHTML = `<p>${escapeHtml(comparison?.reason || "尚未生成独立 CFD 结果。先点击‘求解并对比’。")}</p>`;
    return;
  }
  if (["blocked", "stale", "pending_validation"].includes(comparison.status) && !comparison.baseline && !comparison.scenario) {
    output.innerHTML = `<p>${escapeHtml(comparison.reason || "当前结果不能与基准直接比较，请重新生成并核对工况。")}</p>`;
    return;
  }
  const baseline = comparison.baseline || {};
  const scenario = comparison.scenario || {};
  const delta = comparison.delta || {};
  const pressureUnit = scenario.pressure_unit || baseline.pressure_unit || "";
  const gateText = comparison.eligibility?.numeric_ok ? "数值检查暂通过，仍需网格敏感性和 CFD 工程师复核" : "数值检查未完成，不能据此判断改善";
  output.innerHTML = `<div class="scenario-comparison-grid"><div><span>基准压降</span><strong>${fmt(baseline.delta_pressure, pressureUnit ? ` ${pressureUnit}` : "") || "未生成"}</strong></div><div><span>鼻塞压降</span><strong>${fmt(scenario.delta_pressure, pressureUnit ? ` ${pressureUnit}` : "") || "未生成"}</strong></div><div><span>压降变化</span><strong>${fmt(delta.pressure_percent, " %") || "未计算"}</strong></div><div><span>流量变化</span><strong>${fmt(delta.flow_percent, " %") || "未计算"}</strong></div></div><p>${escapeHtml(gateText)}</p><small>${escapeHtml((comparison.notes || ["未记录比较说明"])[0])}</small>`;
}

function bindScenarioItems(caseId) {
  document.querySelectorAll(".scenario-item").forEach((card) => {
    const scenarioId = card.dataset.scenarioId;
    const version = Number(card.dataset.version);
    const placementForm = card.querySelector("[data-scenario-placement]");
    const viewerStatus = card.querySelector("[data-scenario-viewer-status]");
    const viewerHost = card.querySelector("[data-scenario-viewer]");
    let viewer = null;
    try {
      viewer = createScenarioViewer3d(viewerHost);
    } catch {
      if (viewerStatus) {
        viewerStatus.textContent = "当前浏览器无法创建三维预览，请使用支持 WebGL 的浏览器；仍可保存数值摆位后审核。";
        viewerStatus.classList.add("is-error");
      }
    }
    const baseUrl = `/api/cases/${encodeURIComponent(caseId)}/model`;
    const plugUrl = `/api/cases/${encodeURIComponent(caseId)}/scenarios/${encodeURIComponent(scenarioId)}/download?artifact=normalized&version=${version}`;
    const currentPlacement = () => ({
      translation_mm: scenarioFormVectors(placementForm, "translation_mm", [0, 0, 0]),
      rotation_deg: scenarioFormVectors(placementForm, "rotation_deg", [0, 0, 0]),
      scale: scenarioFormVectors(placementForm, "scale", [1, 1, 1]),
    });
    const savedPlacementSignature = scenarioPlacementSignature(placementForm);
    card.dataset.scenarioDirty = "false";
    card.querySelectorAll("[data-scenario-review], [data-scenario-prep], [data-scenario-run], [data-scenario-comparison]").forEach((control) => {
      control.dataset.scenarioBaseDisabled = String(control.disabled);
    });
    syncScenarioCardActions(card, false);
    const updateDirtyState = () => {
      const dirty = scenarioPlacementSignature(placementForm) !== savedPlacementSignature;
      card.dataset.scenarioDirty = dirty ? "true" : "false";
      syncScenarioCardActions(card, dirty);
      return dirty;
    };
    card.querySelector("[data-scenario-open-preview]")?.addEventListener("click", () => {
      if (viewer) viewer.fit();
      else if (viewerStatus) viewerStatus.textContent = "三维预览不可用；请核对模型状态后重试。";
    });
    if (viewer) {
      viewer.load(baseUrl, plugUrl, {
        ...currentPlacement(),
      }, (message, failed) => {
        if (viewerStatus) {
          viewerStatus.textContent = message;
          viewerStatus.classList.toggle("is-error", Boolean(failed));
        }
      });
      placementForm?.querySelectorAll("input").forEach((input) => input.addEventListener("input", () => {
        const dirty = updateDirtyState();
        try {
          viewer.setPlacement(currentPlacement());
          if (viewerStatus) {
            viewerStatus.textContent = dirty ? "当前输入尚未保存；预览已更新，保存后才会生成新版本。" : "已恢复到已保存摆位。";
            viewerStatus.classList.remove("is-error");
          }
        } catch (error) {
          if (viewerStatus) {
            viewerStatus.textContent = error.message;
            viewerStatus.classList.add("is-error");
          }
        }
      }));
    }
    if (!viewer) placementForm?.querySelectorAll("input").forEach((input) => input.addEventListener("input", updateDirtyState));
    placementForm?.addEventListener("submit", (event) => {
      event.preventDefault();
      scenarioCardAction(card, event.submitter || placementForm.querySelector("button[type='submit']"), async () => {
        await api(`/api/cases/${encodeURIComponent(caseId)}/scenarios/${encodeURIComponent(scenarioId)}/placement`, { method: "PUT", body: {
          ...currentPlacement(),
          expected_version: version,
        }});
        toast("鼻塞摆位已保存为新版本，请继续人工审核");
      });
    });
    card.querySelector("[data-scenario-review]")?.addEventListener("click", (event) => scenarioCardAction(card, event.currentTarget, async () => {
      if (card.dataset.scenarioDirty === "true") throw new Error("当前摆位尚未保存，请先点击“保存新版本”再审核");
      const role = card.querySelector(".scenario-review-role")?.value.trim() || "engineer";
      await api(`/api/cases/${encodeURIComponent(caseId)}/scenarios/${encodeURIComponent(scenarioId)}/review`, { method: "POST", body: { approved: true, reviewer_role: role, notes: "已在工作台人工核对摆位", expected_version: version }});
      toast("鼻塞方案已记录人工审核");
    }));
    card.querySelector("[data-scenario-prep]")?.addEventListener("click", (event) => scenarioCardAction(card, event.currentTarget, async () => {
      if (card.dataset.scenarioDirty === "true") throw new Error("当前摆位尚未保存，请先点击“保存新版本”再生成 CFD");
      await api(`/api/cases/${encodeURIComponent(caseId)}/scenarios/${encodeURIComponent(scenarioId)}/cfd-prep`, { method: "POST", body: { version }});
      toast("鼻塞独立 CFD 算例已生成");
    }));
    card.querySelector("[data-scenario-run]")?.addEventListener("click", (event) => scenarioCardAction(card, event.currentTarget, async () => {
      if (card.dataset.scenarioDirty === "true") throw new Error("当前摆位尚未保存，请先点击“保存新版本”再求解");
      const result = await api(`/api/cases/${encodeURIComponent(caseId)}/scenarios/${encodeURIComponent(scenarioId)}/cfd-run`, { method: "POST", body: { version }});
      toast(`鼻塞 CFD 已提交（任务 ${result.task?.job_id || "已登记"}），计算会在后台继续`);
      startScenarioCfdPolling(caseId, scenarioId, version);
    }));
    card.querySelector("[data-scenario-comparison]")?.addEventListener("click", async (event) => {
      const output = card.querySelector("[data-scenario-comparison-output]");
      const button = event.currentTarget;
      busy(button, true);
      try {
        renderScenarioComparison(output, await api(`/api/cases/${encodeURIComponent(caseId)}/scenarios/${encodeURIComponent(scenarioId)}/comparison?version=${version}`));
      } catch (error) {
        renderScenarioComparison(output, { status: "blocked", reason: error.detail?.reason || error.message });
      } finally { busy(button, false); }
    });
    if (card.querySelector("[data-scenario-task-status]")?.textContent.trim()) startScenarioCfdPolling(caseId, scenarioId, version);
  });
}

function stopScenarioCfdPolling(key) {
  const timer = state.scenarioPolls.get(key);
  if (timer) clearInterval(timer);
  state.scenarioPolls.delete(key);
}

function startScenarioCfdPolling(caseId, scenarioId, version) {
  const key = `${caseId}/${scenarioId}/v${version}`;
  stopScenarioCfdPolling(key);
  const poll = async () => {
    if (state.caseId !== caseId) { stopScenarioCfdPolling(key); return; }
    const card = document.querySelector(`.scenario-item[data-scenario-id="${CSS.escape(scenarioId)}"]`);
    const statusNode = card?.querySelector("[data-scenario-task-status]");
    try {
      const status = await api(`/api/cases/${encodeURIComponent(caseId)}/scenarios/${encodeURIComponent(scenarioId)}/cfd-status?version=${version}`);
      const task = status.task || {};
      if (statusNode) statusNode.textContent = ["queued", "running"].includes(task.status) ? (task.message || "鼻塞 CFD 正在后台执行，页面会自动更新状态。") : (task.message || "");
      if (!["queued", "running"].includes(task.status) && ["solved", "failed", "blocked"].includes(task.status || status.status)) {
        stopScenarioCfdPolling(key);
        await loadScenarioList(caseId);
      }
    } catch (error) {
      if (statusNode) statusNode.textContent = `任务状态读取失败：${error.detail?.reason || error.message || "请求失败"}`;
    }
  };
  poll();
  state.scenarioPolls.set(key, setInterval(poll, 2500));
}

function renderScenarioPanel(data) {
  return `<div id="detail-scenarios" class="card scenario-panel" tabindex="-1"><div class="card-head"><div><h2>鼻塞方案闭环</h2><span class="hint">上传 → 人工摆位 → 审核 → 独立 CFD → 前后对比</span></div><span class="pill pill-mute"><i></i>研究预览</span></div><div class="card-body"><p class="scenario-intro">鼻塞实体会作为气道内独立 wall 障碍物加入网格，基准算例不会被覆盖。只有同一气道基准、同一入口工况且数值检查完成后，才会显示前后差异；工程师复核通过前不会给出医疗建议。</p><form id="scenario-upload-form" class="scenario-upload-form"><div class="scenario-upload-field"><label for="scenario-model">鼻塞 STL</label><input id="scenario-model" name="model" type="file" accept=".stl,model/stl" required /><small>仅上传患者方案资产；服务会保留原始文件、毫米规范化文件和版本记录。</small></div><div class="scenario-upload-field"><label for="scenario-unit">模型单位</label><select id="scenario-unit" name="unit"><option value="mm">mm</option><option value="cm">cm</option><option value="m">m</option><option value="inch">inch</option></select></div><button type="submit" class="button button-primary button-compact">上传鼻塞模型</button><span data-scenario-upload-status role="status" aria-live="polite"></span></form><div id="scenario-list" class="scenario-list" aria-live="polite"><p class="scenario-loading">正在读取鼻塞方案记录…</p></div></div></div>`;
}

function renderDetails(data) {
  const box = $("detail-panels");
  const parts = [];
  const s = data.stages;

  if (s.qc.status === "ok") {
    parts.push(`<div id="detail-qc" class="card" tabindex="-1"><div class="card-head"><h2>输入质控</h2><span class="hint">产物 volume/qc.json</span></div>
      <div class="card-body">${kvList([
        ["层厚", fmt(s.qc.data?.slice_thickness_mm, " mm") || "—"],
        ["像素间距", fmt(s.qc.data?.pixel_spacing_mm, " mm") || "—"],
        ["隐私标记", s.qc.data?.contains_phi ? "输入含识别性标签（仅记录是否，不记录取值）" : "未见识别性标签"],
        ["解剖覆盖", "需人工确认（自动无法判定鼻尖至鼻咽）"],
      ])}</div></div>`);
  }

 const anonymizeStatus = effectiveStageStatus("anonymize", s.anonymize?.status || "pending", s);
  if (anonymizeStatus !== "pending" || s.qc.status !== "pending") {
    const anonymizeData = s.anonymize?.data || {};
    const anonymizeLabel = anonymizeStatus === "ok" ? "已生成副本" : anonymizeStatus === "skipped" ? "已跳过" : anonymizeStatus === "running" ? "处理中" : anonymizeStatus === "failed" ? "未通过" : "等待导入";
    const anonymizePill = anonymizeStatus === "ok" ? "pill-ok" : anonymizeStatus === "failed" ? "pill-fail" : anonymizeStatus === "running" ? "pill-warn" : "pill-mute";
   const anonymizeMessage = escapeHtml(s.anonymize?.message || (anonymizeStatus === "skipped" ? "未生成匿名化副本，按导入选项继续使用本机数据。" : "导入时可生成只保存在本机的匿名化副本。"));
    const fileNote = anonymizeData.files ? `<p class="hint">已写入 ${escapeHtml(anonymizeData.files)} 个文件</p>` : "";
    parts.push(`<div id="detail-anonymize" class="card" tabindex="-1"><div class="card-head"><h2>本地去标识</h2><span class="pill ${anonymizePill}"><i></i>${anonymizeLabel}</span></div>`,
      `<div class="card-body"><p>${anonymizeMessage}</p>${fileNote}</div></div>`);
  }
  if (["ok", "warn"].includes(s.segment.status)) {
   const d = s.segment.data || {};
    const cleanupState = externalAirCleanupState(s);
    const cleanupSummary = externalAirCleanupSummary(cleanupState.cleanup);
    const externalAirSuspected = Boolean(d.external_air_suspected || state.ctViewer.meta?.mask_external_air_suspected);
    d.external_air_suspected = externalAirSuspected;
    const confirmed = Boolean(state.ctViewer.meta) && s.segment.status === "ok" && s.segment.data?.need_human_confirm === false &&
      state.ctViewer.meta?.mask_review?.need_human_confirm !== true;
    const reviewLabel = cleanupState.pending ? "体外空气待清理" : externalAirSuspected ? "疑似含体外空气" : confirmed ? "已人工确认" : "待人工复核";
    const reviewPill = cleanupState.pending || externalAirSuspected || !confirmed ? "pill-warn" : "pill-ok";
    const cleanupNote = cleanupState.pending
      ? `<p class="hint">当前是未经清理的 HU 阈值候选，青色可能包含体外背景空气。请先在三平面 CT 视窗点击“去除体外空气”。</p>`
      : cleanupState.status === "ok" && cleanupSummary
        ? `<p class="hint">最近一次体外空气清理：${cleanupSummary}；仍需逐层复核目标气道范围。</p>`
        : "";
    parts.push(`<div id="detail-segment" class="card" tabindex="-1"><div class="card-head"><h2>气道分割（SOP 阈值基线）</h2><span class="pill ${reviewPill}"><i></i>${reviewLabel}</span></div>
      <div class="card-body">${kvList([
        ["阈值区间", `${d.hu_range?.[0] ?? "?"} ~ ${d.hu_range?.[1] ?? "?"} HU`],
        ["前后长度", fmt(d.ap_length_mm, " mm") || "—"],
        ["复核项", "鼻孔端面 / 鼻咽端面 / 鼻窦旁路 / ENS 宽大腔隙"],
      ])}${cleanupNote}${d.external_air_suspected && !cleanupState.pending ? `<div class="remedy"><p>这份候选结果不能直接用于 STL 或气流求解。</p><ol><li>点击“编辑掩膜”，在三平面 CT 中用画笔或橡皮修正青色区域。</li><li>确认青色区域只覆盖目标气道空腔后，点击“确认气道范围”，再导出掩膜。</li></ol></div>` : ""}</div></div>`);
    parts.push(renderModelScopeCard(data));
  }

  if (["ok", "warn", "failed"].includes(s.surface.status) && s.surface.data) {
    const d = s.surface.data;
    const maskNeedsReview = requiresMaskReview(s);
    const surfaceFailed = s.surface.status === "failed" || d.verdict === "fail";
    const confirmedEndplaneSources = new Set(["user_confirmed_ct_planes", "user_confirmed_oblique_planes", "user_confirmed_bilateral_local_oblique_planes", "automated_bilateral_local_oblique_planes"]);
    const singleBranchSurface = d.endplanes?.source === "user_confirmed_local_oblique_planes";
    const legacySurface = !data.synthetic && !confirmedEndplaneSources.has(d.endplanes?.source) && !singleBranchSurface;
    const surfacePillClass = surfaceFailed ? "pill-fail" : singleBranchSurface || legacySurface || maskNeedsReview ? "pill-warn" : d.verdict === "pass" ? "pill-ok" : "pill-warn";
    const surfacePillText = surfaceFailed ? "未通过" : singleBranchSurface ? "单支段几何" : legacySurface ? "旧模型待重建" : maskNeedsReview ? "上游待复核" : verdictText(d.verdict);
    const volumeCheck = d.volume_check || {};
    const reasons = Array.isArray(d.reasons) ? d.reasons : [];
    let reasonBlock = reasons.length
      ? `<div class="remedy ${surfaceFailed ? "is-fail" : ""}"><p>${surfaceFailed ? "阻断原因" : "质检说明"}</p><ol>${reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ol></div>`
      : "";
    if (singleBranchSurface) reasonBlock += '<div class="remedy"><p>当前只形成一个鼻孔支段入口，另一鼻孔不会自动成为入口。此模型仅供核对局部裁切几何；双侧 CFD 工况尚未定义，不能生成算例或作为气流对照。</p></div>';
    const failedAction = surfaceFailed
      ? `<div class="remedy is-fail"><p>下一步</p><ol><li>在三平面 CT 中复核鼻孔侧与鼻咽侧裁切层面；如掩膜本身断开或含误检，再编辑掩膜。</li><li>保存修正后重新生成 STL 与几何质检。</li></ol><button type="button" class="button button-quiet button-compact" data-surface-endplanes>查看端面裁切</button> <button type="button" class="button button-quiet button-compact" data-surface-edit-mask>编辑掩膜</button></div>`
      : maskNeedsReview
        ? `<div class="remedy"><p>这项只完成了网格与掩膜的体积对照，不能替代气道范围复核。</p><ol><li>先在三平面 CT 中确认候选掩膜。</li><li>确认后再重新生成可信的 STL 与 CFD 算例。</li></ol></div>`
        : "";
    parts.push(`<div id="detail-surface" class="card" tabindex="-1"><div class="card-head"><h2>几何质检</h2>
       <span class="pill ${surfacePillClass}"><i></i>${surfacePillText}</span></div>
       <div class="card-body">${kvList([
         ["质检结论", legacySurface ? `历史记录：${verdictText(d.verdict || "warn")}，当前需重建` : verdictText(d.verdict || (s.surface.status === "failed" ? "fail" : "warn"))],
         ["面数", fmt(d.faces) || "—"],
         ["边界边", fmt(d.boundary_edges) || "—"],
         ["开口", `鼻孔端 ${d.opening_anterior ?? "?"} / 鼻咽端 ${d.opening_posterior ?? "?"} / 中段 ${d.opening_middle ?? "?"}`],
         ["网格体积", fmt(volumeCheck.mesh_volume_ml, " mL") || "—"],
         ["掩膜体积", fmt(volumeCheck.mask_volume_ml, " mL") || "—"],
         ["体积偏差", fmt(d.volume_deviation_percent ?? volumeCheck.deviation_percent, " %") || "—"],
       ])}${legacySurface ? '<div class="remedy"><p>这是端面工具升级前生成的模型。旧质检只统计开口数量，没有排除面积过小的毛刺；请先在 CT 中选择两端，再重新生成 STL 与几何质检。</p></div>' : ""}${reasonBlock}${failedAction}</div></div>`);
  }

  if (s.cfd_prep.status === "ok" && s.cfd_prep.data) {
    const cfdRunData = s.cfd_run.data || {};
    const runStatus = cfdRunData.run_status || {};
    const completed = ["ok", "warn"].includes(s.cfd_run.status);
    const steps = Array.isArray(runStatus.steps) ? runStatus.steps : [];
    const solverStep = steps.find((step) => step?.name === "simpleFoam");
    const exportStep = steps.find((step) => step?.name === "foamToVTK");
    const solved = completed && runStatus.status === "ran" && solverStep?.returncode === 0;
    const failedPostprocess = solved && Number.isInteger(exportStep?.returncode) && exportStep.returncode !== 0;
    const recordError = cfdRunData.run_status_error || (completed && !solved ? "求解阶段与运行记录不一致，请检查 cfd/run_status.json。" : null);
    const running = s.cfd_run.status === "running";
    const failed = s.cfd_run.status === "failed";
    const runLabel = solved ? "已完成" : recordError ? "运行记录异常"
      : ({ running: "正在执行", failed: "失败", blocked: "受阻" }[s.cfd_run.status] || "尚未求解");
    const solver = runStatus.solver || cfdRunData.solver || {};
    const residualText = Object.entries(solver.initial_residual || {}).map(([key, value]) => `${key}=${Number(value).toExponential(2)}`).join(", ");
    const solverDetail = solver.iteration
      ? `已解析外层迭代 ${solver.iteration}${solver.time != null && solver.end_time != null ? ` · 上限参考 ${solver.time}/${solver.end_time}（${solver.iteration_percent ?? "—"}%）` : ""}${residualText ? ` · 初始残差 ${residualText}` : ""}`
      : ((running || failed) ? "尚未收到可解析的外层迭代残差；不能据此判断是否收敛" : "—");
    const progressDetail = renderCfdProgress(runStatus, cfdRunData, s.cfd_run.status);
    const execution = runStatus.execution || cfdRunData.execution || {};
    const executionSummary = cfdSolverExecutionSummary(runStatus, cfdRunData, s.cfd_prep.data, solved);
    const mesh = runStatus.mesh || cfdRunData.mesh || {};
    const failureStep = runStatus.failed_step || s.cfd_run.data?.failed_step;
    const failureReturncode = runStatus.returncode ?? steps.find((step) => step?.name === failureStep)?.returncode;
    const failureMessage = runStatus.message || s.cfd_run.message || (runStatus.timed_out || failureReturncode === 124 ? "求解器超时并被终止" : null);
    const failureRemediation = Array.isArray(runStatus.remediation) ? runStatus.remediation : [];
    const failureLogUrl = failureStep ? `/api/cases/${encodeURIComponent(data.case_id)}/artifact?rel=${encodeURIComponent(`cfd/logs/${failureStep}.log`)}` : null;
    const failureDetail = failed ? `<div class="remedy is-fail"><p><strong>失败原因</strong>：${escapeHtml(failureMessage || "求解器退出，原因未记录")}</p><p>失败步骤：${escapeHtml(failureStep || "未记录")} · 退出码：${failureReturncode == null ? "—" : failureReturncode}${runStatus.timed_out ? " · 已确认超时" : ""}</p>${mesh.cells ? `<p>网格规模：${escapeHtml(fmt(mesh.cells, " 个单元"))}；这是网格检查后的真实数量。</p>` : ""}${failureRemediation.length ? `<ol>${failureRemediation.slice(0, 4).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol>` : ""}${failureLogUrl ? `<a href="${failureLogUrl}">查看 ${escapeHtml(failureStep || "失败步骤")} 日志</a>` : ""}</div>` : "";
    const exportLabel = solved ? (exportStep?.returncode === 0 ? "已完成" : failedPostprocess ? "失败" : exportStep ? "状态未记录" : "未执行")
      : running && cfdRunData.current_step === "foamToVTK" ? "正在导出" : "尚未完成";
    const backend = runStatus.backend || cfdRunData.backend;
    const backendLabel = ({ docker: "Docker", wsl: "WSL", native: "本机 OpenFOAM" })[backend] || (backend ? escapeHtml(backend) : "未选择");
    const pillLabel = solved ? (failedPostprocess ? "求解完成 · 导出失败" : "求解完成 · 待复核")
      : recordError ? "运行记录异常" : ({ running: "正在求解", failed: "求解失败", blocked: "求解受阻" }[s.cfd_run.status] || "尚未求解");
    const pillClass = recordError || s.cfd_run.status === "failed" ? "pill-fail" : solved || running || s.cfd_run.status === "blocked" ? "pill-warn" : "pill-mute";
    const exportLogUrl = `/api/cases/${encodeURIComponent(data.case_id)}/artifact?rel=cfd%2Flogs%2FfoamToVTK.log`;
    const viewResultsAction = completed
      ? `<button type="button" class="button button-primary button-compact" data-cfd-view-results>查看病例流场</button>`
      : "";
    parts.push(`<div id="detail-cfd" class="card" tabindex="-1"><div class="card-head"><h2>CFD</h2>
      <span class="pill ${pillClass}"><i></i>${pillLabel}</span></div>
      <div class="card-body">${kvList([
        ["求解器", `${escapeHtml(s.cfd_prep.data.solver || "simpleFoam")}（OpenFOAM）`],
        ["运行环境", backendLabel],
        ["执行方式", executionSummary.executionLabel],
        ["计算资源", executionSummary.resourceLabel],
        ...cfdRuntimeRows(execution),
        ["GPU", executionSummary.gpuLabel],
        ...cfdMeshRows(execution),
        ["网格规模", mesh.cells ? fmt(mesh.cells, " 个单元") : "—"],
        ["气流求解", runLabel],
        ["可视化导出", exportLabel],
        ["结果复核", solved ? "网格敏感性与工程师复核待完成" : "等待求解结果"],
        ["工况", s.cfd_prep.data.inlet_mode === "pressure" ? "双侧压力边界 · 流量由求解获得"
          : Number.isFinite(s.cfd_prep.data.total_flow_lpm) ? `双侧 ${fmt(s.cfd_prep.data.total_flow_lpm, " L/min")} 静息吸气` : "未记录入口工况"],
        ["入口面积", fmt(s.cfd_prep.data.inlet_area_mm2, " mm²") || "—"],
        ["设定平均入口速度", s.cfd_prep.data.inlet_mode === "pressure" ? "未预设（由求解获得）" : fmt(s.cfd_prep.data.mean_inlet_velocity_mps, " m/s") || "—"],
      ])}
      ${s.cfd_run.status === "blocked" ? remedyBlock(s.cfd_run.data || {}) : ""}
      ${progressDetail}
      ${running ? `<div class="remedy"><p>当前求解状态</p><p>${escapeHtml(solverDetail)}</p><p>如果超过上限，页面会标明“超时并终止”，不会显示为成功。</p></div>` : ""}
      ${failed ? `<div class="remedy"><p>最后可核对的求解状态</p><p>${escapeHtml(solverDetail)}</p></div>` : ""}
      ${recordError ? `<div class="remedy is-fail"><p>${escapeHtml(recordError)}</p></div>` : ""}
      ${failureDetail}
      ${failedPostprocess ? `<div class="remedy"><p>可视化文件导出失败（退出码 ${exportStep.returncode}）；气流求解结果已保留。</p><p>查看转换日志，排查导出失败后再执行可视化转换。结果仍待网格敏感性检查与工程师复核。</p><a href="${exportLogUrl}">下载转换日志</a></div>` : ""}
      ${s.segment.data?.external_air_suspected ? `<div class="remedy"><p>当前候选掩膜待复核，已有算例不能继续求解。</p><ol><li>先修正并确认气道掩膜。</li><li>确认后重新生成 STL、几何质检和 CFD 算例。</li></ol></div>` : ""}
      ${viewResultsAction ? `<div class="step-actions">${viewResultsAction}</div>` : ""}
      </div></div>`);
  }

  if (s.report.status === "ok") {
    parts.push(`<div id="detail-report" class="card" tabindex="-1"><div class="card-head"><h2>患者可读报告</h2><a class="button button-quiet button-compact" href="/api/cases/${data.case_id}/report-file">下载 Markdown</a></div>
      <div class="card-body"><div id="report-summary" class="report-summary" aria-live="polite">正在读取真实 CFD 摘要…</div><div id="report-body" class="report-md">加载中…</div>
      <section id="ai-report-panel" class="ai-report-panel" aria-labelledby="ai-report-title"><div class="ai-report-head"><div><h3 id="ai-report-title">AI 报告解读（可选）</h3><p>只解释事实报告里的脱敏统计，不修改原始报告，也不补全未生成的数据。</p></div><button id="ai-report-generate" type="button" class="button button-quiet button-compact">生成 AI 解读</button></div><div id="ai-report-body" class="ai-report-body" aria-live="polite">尚未生成 AI 解读。</div></section>
      </div></div>`);
  }

  // 鼻塞方案是独立于基准病例的研究闭环：即使基准尚未就绪，也要展示
  // 上传和阻断原因，让用户知道下一步缺什么；真正的 CFD 按钮由服务端状态解锁。
  parts.push(renderScenarioPanel(data));

  box.innerHTML = parts.join("");
  if (["ok", "warn"].includes(s.segment.status)) bindModelScopeCard(data);
  const scenarioUploadForm = box.querySelector("#scenario-upload-form");
  if (scenarioUploadForm) {
    const fileInput = scenarioUploadForm.querySelector("input[name='model']");
    const unitInput = scenarioUploadForm.querySelector("select[name='unit']");
    const submitButton = scenarioUploadForm.querySelector("button[type='submit']");
    const statusNode = scenarioUploadForm.querySelector("[data-scenario-upload-status]");
    fileInput?.addEventListener("change", () => {
      const file = fileInput.files?.[0];
      if (statusNode) statusNode.textContent = file ? `已选择：${file.name}` : "";
    });
    scenarioUploadForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const file = fileInput?.files?.[0];
      if (!file) {
        if (statusNode) statusNode.textContent = "请选择 STL 文件";
        fileInput?.focus();
        return;
      }
      const form = new FormData();
      form.append("model", file, file.name || "nasal-plug.stl");
      form.append("unit", unitInput?.value || "mm");
      busy(submitButton, true);
      if (statusNode) statusNode.textContent = "正在上传并检查 STL…";
      try {
        await api(`/api/cases/${encodeURIComponent(data.case_id)}/scenarios`, { method: "POST", body: form });
        if (fileInput) fileInput.value = "";
        if (statusNode) statusNode.textContent = "上传完成，已建立版本 v0001";
        toast("鼻塞模型已上传，请继续人工摆位");
        await loadScenarioList(data.case_id);
      } catch (error) {
        const reason = error.detail?.reason || error.message || "鼻塞模型上传失败";
        if (statusNode) statusNode.textContent = reason;
        toast(reason, true);
      } finally {
        busy(submitButton, false);
      }
    });
    loadScenarioList(data.case_id);
  }
  const surfaceEditButton = box.querySelector("[data-surface-edit-mask]");
  const surfaceEndplanesButton = box.querySelector("[data-surface-endplanes]");
  if (surfaceEndplanesButton) surfaceEndplanesButton.addEventListener("click", () => {
    if (!state.endplanes.open) $("btn-endplanes").click();
    else focusStageTarget($("endplane-panel"));
  });
  if (surfaceEditButton) {
    surfaceEditButton.addEventListener("click", () => {
      state.mode = "slices";
      const editButton = $("btn-edit-mask");
      if (editButton && !state.maskEditor.active) editButton.click();
      else {
        refreshViewer();
        $("viewer-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  }
  const viewResultsButton = box.querySelector("[data-cfd-view-results]");
  if (viewResultsButton) viewResultsButton.addEventListener("click", () => {
    state.cfdResults.caseId = data.case_id;
    window.location.hash = "results";
    showRoute("results");
  });
  if (s.report.status === "ok") {
    $("ai-report-generate")?.addEventListener("click", () => generateAIReport(data.case_id));
    loadReport(data.case_id);
    loadAIReport(data.case_id);
  }
}

function remedyBlock(data) {
  const remedy = data.remediation || [];
  if (!remedy.length) return "";
  return `<div class="remedy"><p>${escapeHtml(localizeMaskInstruction(data.message || "本机未检测到可用的 OpenFOAM 后端"))}</p>
    <ol>${remedy.map((item) => `<li>${escapeHtml(localizeMaskInstruction(item))}</li>`).join("")}</ol></div>`;
}

async function loadReport(caseId) {
  const target = $("report-body");
  const summary = $("report-summary");
  if (!target) return;
  try {
    const [markdownResponse, reportResponse] = await Promise.all([
      fetch(`/api/cases/${encodeURIComponent(caseId)}/report-file`),
      fetch(`/api/cases/${encodeURIComponent(caseId)}/artifact?rel=report%2Freport.json`),
    ]);
    if (!markdownResponse.ok) throw new Error("报告正文读取失败");
    if (!reportResponse.ok) throw new Error("报告数据读取失败");
    const report = await reportResponse.json();
    let flowPayload = null;
    let cfdResult = null;
    // 只有已有完整病例流场时才读取 payload；没有结果时不以估算值填充报告。
    if (report.cfd_run?.status === "ran") {
      try {
        cfdResult = await api(`/api/cases/${encodeURIComponent(caseId)}/cfd-results`);
        if (cfdResult.status === "ready") {
          flowPayload = await api(cfdResult.viewer_url || `/api/cases/${encodeURIComponent(caseId)}/cfd-results/viewer`);
        }
      } catch {
        // 报告正文和闸门数据仍可用；流场分布单独标注为未读取。
      }
    }
    if (summary) summary.innerHTML = renderReportSummary(report, flowPayload, cfdResult);
    target.innerHTML = miniMarkdown(await markdownResponse.text());
  } catch (error) {
    if (summary) summary.innerHTML = `<div class="report-status is-error">${escapeHtml(error.message || "报告数据读取失败")}</div>`;
    target.textContent = "报告加载失败";
  }
}

async function loadAIReport(caseId) {
  const target = $("ai-report-body");
  if (!target) return;
  try {
    const result = await api(`/api/cases/${encodeURIComponent(caseId)}/ai-report`);
    renderAIReport(result);
  } catch (error) {
    if (error.status === 404 || error.detail?.status === "missing") {
      target.innerHTML = `<p class="ai-report-muted">尚未生成。请先在“环境与依赖”页配置 API URL 和密钥。</p>`;
      return;
    }
    target.innerHTML = `<p class="ai-report-muted">暂时无法读取 AI 解读：${escapeHtml(error.detail?.reason || error.message || "未知错误")}</p>`;
  }
}

async function generateAIReport(caseId) {
  const button = $("ai-report-generate");
  const target = $("ai-report-body");
  if (!button || !target) return;
  busy(button, true);
  target.innerHTML = `<p class="ai-report-muted">正在请求 AI 解读，事实报告和 CT 数据不会被修改…</p>`;
  try {
    const result = await api(`/api/cases/${encodeURIComponent(caseId)}/ai-report`, { method: "POST", body: {} });
    renderAIReport(result);
    toast("AI 解读已生成，事实报告保持不变");
  } catch (error) {
    const detail = error.detail || {};
    target.innerHTML = `<div class="ai-report-error"><strong>${escapeHtml(detail.reason || error.message || "AI 解读生成失败")}</strong>${Array.isArray(detail.remediation) && detail.remediation.length ? `<ul>${detail.remediation.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}</div>`;
  } finally {
    busy(button, false);
  }
}

function renderAIReport(result) {
  const target = $("ai-report-body");
  if (!target) return;
  const interpretation = result?.interpretation || {};
  const design = interpretation.design_reference || {};
  const list = (items) => Array.isArray(items) && items.length ? `<ul>${items.map((item) => `<li>${escapeHtml(String(item))}</li>`).join("")}</ul>` : `<p class="ai-report-muted">未生成</p>`;
  const fields = result?.source?.fields || {};
  const pending = !fields.gate?.eligible;
  target.innerHTML = `<div class="ai-report-meta"><span>模型：${escapeHtml(result.model || "未记录")}</span><span>基于报告：${escapeHtml(result.source?.report_generated_at || "未记录")}</span>${pending ? `<span class="report-status is-warn">结果闸门待复核</span>` : `<span class="report-status is-ok">结果闸门已通过</span>`}</div>
    <p class="ai-report-summary">${escapeHtml(interpretation.summary || "未生成")}</p>
    <div class="ai-report-columns"><div><b>事实观察</b>${list(interpretation.observations)}</div><div><b>限制与待复核</b>${list(interpretation.limitations)}</div></div>
    <div class="ai-report-design"><b>设计参考</b><p>${escapeHtml(design.text || "未生成")}</p>${pending || design.recommendation_allowed === false ? `<small>当前仅作解释，不能据此确定鼻塞设计方案。</small>` : ""}</div>
    <small class="ai-report-disclaimer">${escapeHtml(result.disclaimer || "AI 解读不能替代 CFD 工程复核或医学判断。")}</small>`;
}

function reportNumber(value) {
  if (value === null || value === undefined || typeof value === "boolean" ||
      (typeof value === "string" && !value.trim()) ||
      !["number", "string"].includes(typeof value)) return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

function reportMetricValue(value, unit = "") {
  const number = reportNumber(value);
  return number === null ? "未记录" : escapeHtml(fmt(number, unit));
}

function reportRangeValue(range, unit = "") {
  if (!Array.isArray(range) || range.length < 2 || reportNumber(range[0]) === null || reportNumber(range[1]) === null) return "未生成";
  return `${escapeHtml(fmt(Number(range[0])))}–${escapeHtml(fmt(Number(range[1]), unit))}`;
}

function reportDistributionValue(summary, fallbackRange, unit = "") {
  if (summary && reportNumber(summary.min) !== null && reportNumber(summary.max) !== null) {
    return `${escapeHtml(fmt(Number(summary.min)))}–${escapeHtml(fmt(Number(summary.max), unit))}`;
  }
  return reportRangeValue(fallbackRange, unit);
}

function reportMetric(label, value, detail = "") {
  return `<div class="report-metric"><span>${escapeHtml(label)}</span><strong>${value}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</div>`;
}

function reportStatus(text, tone = "muted") {
  return `<span class="report-status is-${tone}">${escapeHtml(text)}</span>`;
}

function renderReportSummary(report, flowPayload, cfdResult) {
  // report.json 是报告摘要的唯一数据源。flowPayload/cfdResult 只用于旧页面
  // 的加载流程，不能把 viewer 的范围或算例请求值冒充已复核的报告指标。
  const baselineFlow = report?.baseline_flow || {};
  const comparisonData = report?.obstruction_comparison || {};
  const design = report?.design_reference || {};
  const cfd = report?.cfd_case || {};
  const run = report?.cfd_run || {};
  const gate = report?.gate || {};
  const velocity = baselineFlow.velocity_distribution?.velocity || {};
  const pressure = baselineFlow.velocity_distribution?.pressure || {};
  const leftRight = baselineFlow.left_right_flow || {};
  const narrow = baselineFlow.narrow_regions || {};
  // pending_validation 仍可能包含真实求解观测；只把字段缺失标为未生成，
  // 不因闸门未通过而把已有值隐藏掉。
  const baselineObserved = ["ready", "numeric_only", "pending_validation"].includes(baselineFlow.status);
  const statusText = {
    ready: "流场与摘要已生成",
    numeric_only: "数值已通过，流场可视化未读取",
    pending_validation: "待数值闸门复核",
    missing: "尚未生成",
  }[baselineFlow.status] || "未记录";
  const statusTone = baselineFlow.status === "ready" ? "ok" : baselineFlow.status === "numeric_only" ? "warn" : "warn";
  const baselineStatus = reportStatus(statusText, statusTone);
  const pressureUnit = baselineFlow.pressure_drop?.unit || null;
  const pressureLabel = pressureUnit === "m2/s2" || pressureUnit === "m²/s²" ? "m²/s²（运动学压力）" : pressureUnit || "未确认";
  const pressureDrop = baselineObserved
    ? reportMetricValue(baselineFlow.pressure_drop?.value, pressureUnit ? ` ${pressureUnit}` : "")
    : "未生成";
  const flowValue = baselineFlow.flow_lpm;
  // 压力入口没有“设定流量”；不要从旧病例元数据回填，避免把压力工况误标成流量工况。
  const requestedFlow = baselineFlow.requested_flow_lpm;
  const flowDetail = baselineFlow.flow_measurement?.reason || (reportNumber(flowValue) !== null
    ? "当前时间步入口净通量，吸气方向已核对"
    : "实际入口流量尚不可用");
  const resistance = baselineFlow.resistance || {};
  const regionFlow = baselineFlow.region_flow || {};
  const wholeSection = regionFlow.analysis_type === "whole_section";
  const solverState = run.status === "ran" ? "已执行" : run.status === "failed" ? "求解失败" : "尚未执行";
  const gateText = gate.eligible ? "已通过结果闸门，可进入工程复核。" : "尚未通过全部结果闸门；以下已显示的值仅作观测，需复核。";
  const metricSummary = (value, unit, digits = 3) => reportNumber(value) === null ? "未记录" : escapeHtml(fmt(value, unit));
  const distributionText = (summary, unit) => summary.status === "checked"
    ? `最小 ${metricSummary(summary.min, unit)} · 平均 ${metricSummary(summary.mean, unit)} · P95 ${metricSummary(summary.p95, unit)} · 最大 ${metricSummary(summary.max, unit)}${summary.sample_count ? ` · ${escapeHtml(String(summary.sample_count))} 个采样值` : ""}`
    : escapeHtml(summary.reason || "未生成");
  const narrowText = narrow.status === "checked" && Array.isArray(narrow.regions) && narrow.regions.length
    ? narrow.regions.map((region) => {
      const label = region.label || region.position || "区域";
      const area = metricSummary(region.area_mm2, " mm²");
      const speed = metricSummary(region.velocity_p95_mps, " m/s");
      return `${escapeHtml(label)}（截面积 ${area}，速度 P95 ${speed}）`;
    }).join("；")
    : escapeHtml(narrow.reason || "未生成");
  const splitAvailable = ["checked", "available_pending_validation"].includes(leftRight.status);
  const splitText = splitAvailable
    ? `左 ${metricSummary(leftRight.left_lpm, " L/min")}（${metricSummary(leftRight.left_fraction == null ? null : leftRight.left_fraction * 100, " %", 1)}），右 ${metricSummary(leftRight.right_lpm, " L/min")}（${metricSummary(leftRight.right_fraction == null ? null : leftRight.right_fraction * 100, " %", 1)}）${leftRight.status === "available_pending_validation" ? "（待复核）" : ""}`
    : escapeHtml(leftRight.reason || "未生成");
  const sectionFlowText = wholeSection
    ? `正向 ${metricSummary(regionFlow.totals?.forward_lpm, " L/min")}，反向 ${metricSummary(regionFlow.totals?.reverse_lpm, " L/min")}，净流量 ${metricSummary(regionFlow.totals?.net_lpm, " L/min")}`
    : splitText;
  const baseline = `<section class="report-section" aria-labelledby="report-baseline-title">
    <div class="report-section-head"><div><h3 id="report-baseline-title">本次气流计算</h3><p>稳态吸气；指标描述当前模型和呼吸条件，仍需数值与工程复核。</p></div>${baselineStatus}</div>
    <div class="report-metric-grid">
      ${reportMetric("设定入口流量", metricSummary(requestedFlow, " L/min"), "计算条件；与实际求得流量分开记录")}
      ${reportMetric("实际入口流量", baselineObserved ? metricSummary(flowValue, " L/min") : "未生成", flowDetail)}
      ${reportMetric("入口至出口压降", pressureDrop, baselineFlow.pressure_drop?.reason || pressureLabel)}
      ${reportMetric("模拟鼻阻力", resistance.status === "checked" ? metricSummary(resistance.value, " Pa·s/mL") : "未计算", resistance.reason || "当前模型压降 ÷ 实际吸气流量；数值小不代表一定更舒服")}
      ${reportMetric("速度采样摘要", baselineObserved ? distributionText(velocity, ` ${velocity.unit || "m/s"}`) : "未生成", "流线与切片采样，不能代表全鼻腔体积平均")}
      ${reportMetric("压力采样摘要", baselineObserved ? distributionText(pressure, ` ${pressure.unit || ""}`) : "未生成", "可视化采样摘要；单位独立于压降")}
      ${reportMetric(wholeSection ? "整截面流量" : "截面左右侧正向流量", baselineObserved ? sectionFlowText : "未生成", wholeSection ? "沿 CT 前→后方向，松手后从已有最终速度场重新积分" : (leftRight.description || leftRight.reason || "历史截面分配记录"))}
      ${reportMetric("狭窄区域", baselineObserved ? narrowText : "未生成", narrow.status === "not_available" ? "当前算例未输出沿程截面积曲线" : "")}
    </div>
    <div class="report-note"><b>当前计算状态：</b>${escapeHtml(solverState)}；${escapeHtml(gateText)}</div>
  </section>`;

  const regionalRecords = regionFlow.status === "ready" && Array.isArray(regionFlow.regions)
    ? [...regionFlow.regions, ...(regionFlow.unassigned ? [regionFlow.unassigned] : [])] : [];
  const regionalRows = regionalRecords.map((region) => {
    const sidePercent = region.id === "unassigned" ? "—" : reportNumber(region.percent_of_side) === null
      ? (regionFlow.coverage_complete === false ? "待补齐分区" : "占比不可用") : metricSummary(region.percent_of_side, " %");
    return `<tr><th scope="row">${escapeHtml(region.label || region.id)}</th><td>${metricSummary(region.forward_lpm, " L/min")}</td><td>${metricSummary(region.percent_of_section, " %")}</td><td>${sidePercent}</td></tr>`;
  }).join("");
  const regionalSection = wholeSection
    ? `<section class="report-section" aria-labelledby="report-region-title"><div class="report-section-head"><div><h3 id="report-region-title">整截面气流统计</h3><p>拖动三维模型上的截面后，在松手位置重新计算；速度统计按截面三角面面积加权。</p></div>${reportStatus(regionFlow.status === "ready" ? "已计算 · 待复核" : "未计算", "warn")}</div>
      ${regionFlow.status === "ready" ? `<div class="report-metric-grid"><div class="report-metric"><b>位置</b><span>${metricSummary(regionFlow.plane?.position_fraction == null ? null : regionFlow.plane.position_fraction * 100, " %", 1)}</span></div><div class="report-metric"><b>面积</b><span>${metricSummary(regionFlow.totals?.area_mm2, " mm²")}</span></div><div class="report-metric"><b>速度平均 / P95 / 最大</b><span>${metricSummary(regionFlow.velocity?.mean_mps, " m/s")} / ${metricSummary(regionFlow.velocity?.p95_mps, " m/s")} / ${metricSummary(regionFlow.velocity?.max_mps, " m/s")}</span></div></div><p>正向 ${metricSummary(regionFlow.totals?.forward_lpm, " L/min")}；反向 ${metricSummary(regionFlow.totals?.reverse_lpm, " L/min")}；净流量 ${metricSummary(regionFlow.totals?.net_lpm, " L/min")}。</p>` : `<p>${escapeHtml(regionFlow.reason || "在病例流场页选择截面并重新计算。")}</p>`}
      <p>${escapeHtml(typeof regionFlow.inlet_condition === "string" ? regionFlow.inlet_condition : regionFlow.inlet_condition?.description || "当前结果描述所选截面和当前入口工况，需结合工程复核解读。")}</p>
    </section>`
    : `<section class="report-section" aria-labelledby="report-region-title"><div class="report-section-head"><div><h3 id="report-region-title">历史人工分区结果</h3><p>这是旧版左右、上中下标注结果；新截面分析不再要求人工分区。</p></div>${reportStatus(regionFlow.status === "ready" ? "历史记录" : "未计算", "warn")}</div>
      ${regionalRows ? `<div class="report-region-scroll"><table class="report-region-table"><thead><tr><th scope="col">区域</th><th scope="col">正向流量</th><th scope="col">占整截面</th><th scope="col">占该侧</th></tr></thead><tbody>${regionalRows}</tbody></table></div><p>整截面正向流量 ${metricSummary(regionFlow.totals?.forward_lpm, " L/min")}；回流 ${metricSummary(regionFlow.totals?.reverse_lpm, " L/min")}；净流量 ${metricSummary(regionFlow.totals?.net_lpm, " L/min")}。</p>` : `<p>${escapeHtml(regionFlow.reason || "尚无历史人工分区结果。")}</p>`}
      <p>${escapeHtml(typeof regionFlow.inlet_condition === "string" ? regionFlow.inlet_condition : regionFlow.inlet_condition?.description || "历史人工分区结果仅供兼容读取。")}</p>
    </section>`;
  const interpretation = `<section class="report-section"><h3>这些结果说明到哪一步</h3><p>流线只追踪部分气流，没有画线的位置也可能有气流。${wholeSection ? "整截面流量和速度不能直接代表鼻甲感觉或黏膜功能。" : "历史区域比例不代表鼻甲感觉或黏膜功能。"}当前模型不能据此判断治疗效果。</p><p>${escapeHtml(baselineFlow.wall_shear_stress?.reason || "壁面剪切应力 WSS 尚未形成可验证的生产结果，本轮保持缺项。")}</p><p>${escapeHtml(gateText)}</p></section>`;

  const scenarios = Array.isArray(comparisonData.items) ? comparisonData.items : [];
  const comparison = scenarios.length
    ? `<div class="report-scenario-list">${scenarios.map((scenario, index) => {
      const flow = scenario.flow_report || {};
      const scenarioPressure = flow.pressure_drop || {};
      const delta = scenario.comparison || {};
      const label = scenario.label || scenario.scenario_id || `方案 ${index + 1}`;
      const scenarioVelocity = flow.velocity_distribution?.velocity || {};
      return `<article class="report-scenario"><b>${escapeHtml(label)}</b><span>位置：${escapeHtml(scenario.position || "未记录")}</span><span>尺寸：${metricSummary(scenario.size_mm, " mm")}</span><span>阻塞率：${metricSummary(scenario.occlusion_percent, " %")}</span><span>流量：${metricSummary(flow.flow_lpm, " L/min")}</span><span>压降：${metricSummary(scenarioPressure.value, scenarioPressure.unit ? ` ${scenarioPressure.unit}` : "")}</span><span>速度 P95：${metricSummary(scenarioVelocity.p95, scenarioVelocity.unit ? ` ${scenarioVelocity.unit}` : " m/s")}</span><span>压降变化：${metricSummary(delta.delta_pressure_percent, " %")}</span><span>流量变化：${metricSummary(delta.delta_flow_percent, " %")}</span></article>`;
    }).join("")}</div>`
    : `<div class="report-comparison-empty"><strong>尚未生成独立鼻塞方案</strong><p>${escapeHtml(comparisonData.reason || "每个鼻塞位置、尺寸和阻塞率都必须重新建模并求解，不能复制基准病例数值。")} </p></div>`;
  const comparisonLabel = comparisonData.status === "checked" ? `${scenarios.length} 个独立算例` : "待创建方案";
  const comparisonSection = `<section class="report-section" aria-labelledby="report-comparison-title">
    <div class="report-section-head"><div><h3 id="report-comparison-title">2. 鼻塞方案对比报告</h3><p>每个位置、尺寸和阻塞率都绑定独立几何与 CFD 结果。</p></div>${reportStatus(comparisonLabel, comparisonData.status === "checked" ? "ok" : "warn")}</div>
    ${comparison}
  </section>`;

  const conclusions = Array.isArray(design.conclusions) ? design.conclusions : [];
  const uncertainties = Array.isArray(design.uncertainty) ? design.uncertainty : [];
  const designStatusText = { checked: "可供复核", baseline_only: "仅有基准结果", pending_validation: "基准结果待复核", pending: "等待基准流场" }[design.status] || "未记录";
  const designTone = design.status === "checked" ? "ok" : "warn";
  const conclusionBody = conclusions.length
    ? `<ul class="report-conclusion-list">${conclusions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : `<p class="report-conclusion-lead">尚未形成设计结论。</p>`;
  const uncertaintyBody = uncertainties.length
    ? `<div class="report-uncertainty"><b>不确定性与边界</b><ul>${uncertainties.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`
    : "";
  const conclusionSection = `<section class="report-section report-conclusion" aria-labelledby="report-conclusion-title">
    <div class="report-section-head"><div><h3 id="report-conclusion-title">3. 设计参考结论</h3><p>只根据已完成的真实算例给出参考，未完成项目保持空缺。</p></div>${reportStatus(designStatusText, designTone)}</div>
    ${conclusionBody}
    ${uncertaintyBody}
  </section>`;
  const comparisonDetails = scenarios.length ? `${comparisonSection}${conclusionSection}` : `<details class="report-section"><summary>后续结构方案与设计参考</summary>${comparisonSection}${conclusionSection}</details>`;
  return `<div class="report-summary-head"><div><strong>报告摘要</strong><span>当前呼吸条件、截面气流及解释范围</span></div></div>${baseline}${regionalSection}${interpretation}${comparisonDetails}`;
}

function miniMarkdown(text) {
  return text.split(/\n{2,}/).map((block) => {
    const safe = escapeHtml(block);
    if (safe.startsWith("### ")) return `<h3>${safe.slice(4)}</h3>`;
    if (safe.startsWith("## ")) return `<h2>${safe.slice(3)}</h2>`;
    if (safe.startsWith("# ")) return `<h1>${safe.slice(2)}</h1>`;
    if (/^([-*]|\d+\.)\s/m.test(safe)) {
      return `<ul>${safe.split("\n").filter((line) => line.trim()).map((line) => `<li>${line.replace(/^([-*]|\d+\.)\s*/, "")}</li>`).join("")}</ul>`;
    }
    return `<p>${safe.replace(/\n/g, "<br/>")}</p>`;
  }).join("");
}

/* ---------- 视窗 ---------- */
function hasSurfaceModel() {
  return Boolean(state.caseId && ["ok", "warn"].includes(state.case?.stages?.surface?.status));
}

function surfaceModelKey() {
  const surface = state.case?.stages?.surface;
  return [state.caseId, surface?.updated_at || "", state.case?.stages?.segment?.data?.mask_version ?? ""].join("|");
}

function activateStlPreview({ reload = false } = {}) {
  if (!guardEndplaneDraft()) return false;
  state.endplanes.open = false;
  state.maskEditor.active = false;
  if (reload) {
    state.viewer3dCaseId = null;
    state.viewer3dKey = null;
    state.viewer3d?.clear();
  }
  state.mode = "3d";
  refreshViewer();
  return true;
}

function refreshViewer() {
  const hasCase = Boolean(state.caseId);
  const hasStl = hasSurfaceModel();
  const hasVolume = hasCase && ["ok", "warn"].includes(state.case?.stages?.import?.status);
  const maskNeedsReview = requiresMaskReview(state.case?.stages);
  const hasMask = hasCase && Boolean(state.ctViewer.meta?.mask_available || ["ok", "warn"].includes(state.case?.stages?.segment?.status));
  $("viewer-empty").hidden = hasVolume;
  $("btn-download-stl").disabled = !hasStl || maskNeedsReview;
  // 待清理阶段的阻断原因已在三平面复核提示区持续显示。
  $("btn-view-3d").disabled = !hasVolume || externalAirCleanupState().pending;
  $("btn-edit-mask").disabled = !hasVolume || !hasMask;
  renderEndplaneUi();
  if (!hasVolume) {
    $("viewer-views").hidden = true;
    $("viewer-controls").hidden = true;
    $("viewer-3d").hidden = true;
    $("viewer-preview-controls").hidden = true;
    $("mask-editor-toolbar").hidden = true;
    $("viewer-note").textContent = "";
    $("viewer-sub").textContent = "浏览器内 CT 复核与掩膜编辑 · 等待导入体数据";
    updateCtViewerGuide();
    updateMaskEditorUi();
    state.viewerAssetCaseId = null;
    state.viewerAssetMode = null;
    return;
  }
  updateCtViewerGuide();
  updateMaskEditorUi();
  if (state.mode === "3d") {
    if (maskNeedsReview) show3dBlocked("当前掩膜尚未确认，不能显示可用于建模的 3D 模型。");
    else if (!hasStl) show3dBlocked("当前还没有 STL。请先确认气道掩膜，再生成 STL 与几何质检。");
    else show3d();
  } else showSlices();
}

function resetCtViewer(caseId) {
  const ct = state.ctViewer;
  // A metadata request cannot be aborted through the generic api helper. Bump a
  // generation token so an older request cannot overwrite a newer mask state.
  ct.metaRequestId += 1;
  for (const view of CT_VIEWS) {
    const entry = ct.views[view];
    entry.controller?.abort();
    if (entry.image?.close) entry.image.close();
    Object.assign(entry, { index: 0, zoom: 1, panX: 0, panY: 0, image: null, controller: null, cursor: null });
    const canvas = $(`slice-canvas-${view}`);
    const context = canvas?.getContext("2d");
    if (context && canvas.width && canvas.height) context.clearRect(0, 0, canvas.width, canvas.height);
  }
  ct.caseId = caseId;
  ct.maskKey = null;
  ct.meta = null;
  ct.metaPromise = null;
  ct.level = -500;
  ct.width = 2000;
  ct.overlay = true;
  ct.requestTimer = null;
}

function ctMaskKey(caseData = state.case) {
  const segment = caseData?.stages?.segment || {};
  return [
    segment.status || "pending",
    segment.updated_at || "",
    segment.data?.mask_version ?? "",
    segment.data?.need_human_confirm ?? "",
    segment.data?.external_air_suspected ?? "",
  ].join("|");
}

function syncCtControls() {
  const ct = state.ctViewer;
  $("viewer-wl").value = String(ct.level);
  $("viewer-ww").value = String(ct.width);
  $("viewer-overlay").checked = ct.overlay;
}

function syncCtIndex(view, index) {
  const ct = state.ctViewer;
  const info = ct.meta?.views?.[view];
  if (!info) return;
  const entry = ct.views[view];
  entry.index = Math.max(0, Math.min(info.count - 1, Number(index) || 0));
  const range = $(`slice-range-${view}`);
  const label = $(`slice-label-${view}`);
  range.max = String(Math.max(0, info.count - 1));
  range.value = String(entry.index);
  label.textContent = `第 ${entry.index + 1} / ${info.count} 层`;
}

function ctImageTransform(view) {
  const entry = state.ctViewer.views[view];
  const canvas = $(`slice-canvas-${view}`);
  const info = state.ctViewer.meta?.views?.[view];
  if (!canvas || !entry?.image || !info) return null;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, rect.width);
  const height = Math.max(1, rect.height);
  const iw = entry.image.width || entry.image.naturalWidth;
  const ih = entry.image.height || entry.image.naturalHeight;
  if (!iw || !ih) return null;
  const fit = Math.min(width / iw, height / ih);
  return { entry, canvas, info, width, height, iw, ih, scale: fit * entry.zoom };
}

function maskPointFromEvent(view, event) {
  const transform = ctImageTransform(view);
  if (!transform || transform.scale <= 0) return null;
  const rect = transform.canvas.getBoundingClientRect();
  const localX = event.clientX - rect.left;
  const localY = event.clientY - rect.top;
  const imageX = (localX - transform.width / 2 - transform.entry.panX) / transform.scale + transform.iw / 2;
  const imageY = (localY - transform.height / 2 - transform.entry.panY) / transform.scale + transform.ih / 2;
  if (imageX < 0 || imageX >= transform.iw || imageY < 0 || imageY >= transform.ih) return null;
  const x = Math.max(0, Math.min(transform.info.width - 1, Math.round(imageX * transform.info.width / transform.iw)));
  const y = Math.max(0, Math.min(transform.info.height - 1, Math.round(imageY * transform.info.height / transform.ih)));
  return { x, y, localX, localY };
}

function maskPointToCanvas(view, x, y) {
  const transform = ctImageTransform(view);
  if (!transform) return null;
  const imageX = (Number(x) + 0.5) * transform.iw / transform.info.width;
  const imageY = (Number(y) + 0.5) * transform.ih / transform.info.height;
  return {
    x: transform.width / 2 + transform.entry.panX + (imageX - transform.iw / 2) * transform.scale,
    y: transform.height / 2 + transform.entry.panY + (imageY - transform.ih / 2) * transform.scale,
    transform,
  };
}

function drawMaskEditorOverlay(view, ctx, width, height) {
  const editor = state.maskEditor;
  if (!editor.active || editor.view !== view) return;
  const entry = state.ctViewer.views[view];
  const info = state.ctViewer.meta?.views?.[view];
  if (!entry || !info) return;
  const transform = ctImageTransform(view);
  if (!transform) return;
  const sourceScale = ((info.width / transform.iw) + (info.height / transform.ih)) / 2;
  const radius = Math.max(3, editor.radius * transform.scale / Math.max(sourceScale, 0.001));
  const preview = editor.preview.filter((op) => op.view === view && op.index === entry.index);
  if (preview.length) {
    ctx.save();
    for (const op of preview) {
      const point = maskPointToCanvas(view, op.x, op.y);
      if (!point) continue;
      ctx.beginPath();
      ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = op.mode === "erase" ? "rgba(255, 122, 104, .22)" : "rgba(55, 225, 199, .25)";
      ctx.strokeStyle = op.mode === "erase" ? "rgba(255, 153, 137, .9)" : "rgba(122, 255, 226, .92)";
      ctx.lineWidth = 1.4;
      ctx.fill();
      ctx.stroke();
    }
    ctx.restore();
  }
  const cursor = entry.cursor;
  if (!cursor) return;
  ctx.save();
  ctx.beginPath();
  ctx.arc(cursor.x, cursor.y, radius, 0, Math.PI * 2);
  ctx.strokeStyle = editor.tool === "erase" ? "#ff9f8f" : "#8affdd";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([5, 3]);
  ctx.stroke();
  ctx.restore();
}

function drawCtSlice(view) {
  const entry = state.ctViewer.views[view];
  const canvas = $(`slice-canvas-${view}`);
  if (!canvas || !entry.image) return;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(rect.width));
  const height = Math.max(1, Math.round(rect.height));
  const pixelWidth = Math.max(1, Math.round(width * dpr));
  const pixelHeight = Math.max(1, Math.round(height * dpr));
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#050a0c";
  ctx.fillRect(0, 0, width, height);
  const iw = entry.image.width || entry.image.naturalWidth;
  const ih = entry.image.height || entry.image.naturalHeight;
  const fit = Math.min(width / iw, height / ih);
  const scale = fit * entry.zoom;
  ctx.save();
  ctx.translate(width / 2 + entry.panX, height / 2 + entry.panY);
  ctx.scale(scale, scale);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(entry.image, -iw / 2, -ih / 2);
  ctx.restore();
  drawMaskEditorOverlay(view, ctx, width, height);
}

function loadCtSlice(view, requestedIndex = null) {
  const ct = state.ctViewer;
  const caseId = state.caseId;
  const info = ct.meta?.views?.[view];
  if (!caseId || ct.caseId !== caseId || !info) return;
  if (requestedIndex !== null) syncCtIndex(view, requestedIndex);
  const entry = ct.views[view];
  entry.controller?.abort();
  const controller = new AbortController();
  entry.controller = controller;
  const loading = $(`slice-loading-${view}`);
  loading.hidden = false;
  const params = new URLSearchParams({
    index: String(entry.index),
    wl: String(ct.level),
    ww: String(ct.width),
    overlay: String(ct.overlay),
  });
  fetch(window.WindcatcherSegmentationModels?.sliceUrl(view, params) || `/api/cases/${encodeURIComponent(caseId)}/slice/${view}?${params}`, { signal: controller.signal })
    .then((response) => {
      if (!response.ok) return response.json().then((payload) => { throw new Error(payload.detail?.reason || "切片读取失败"); });
      return response.blob();
    })
    .then((blob) => {
      if (typeof createImageBitmap === "function") return createImageBitmap(blob);
      return new Promise((resolve, reject) => {
        const image = new Image();
        const url = URL.createObjectURL(blob);
        image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
        image.onerror = () => { URL.revokeObjectURL(url); reject(new Error("切片图像解码失败")); };
        image.src = url;
      });
    })
    .then((image) => {
      if (state.caseId !== caseId || state.ctViewer.caseId !== caseId || entry.controller !== controller) {
        image.close?.();
        return;
      }
      entry.image?.close?.();
      entry.image = image;
      drawCtSlice(view);
    })
    .catch((error) => {
      if (error.name !== "AbortError" && state.caseId === caseId) toast(error.message || "切片读取失败", true);
    })
    .finally(() => {
      if (entry.controller === controller) {
        loading.hidden = true;
        entry.controller = null;
      }
    });
}

function loadCtViewer(caseId) {
  const ct = state.ctViewer;
  const maskKey = ctMaskKey();
  if (ct.caseId !== caseId || ct.maskKey !== maskKey) {
    resetCtViewer(caseId);
    ct.maskKey = maskKey;
  }
  if (ct.meta) {
    syncCtControls();
    for (const view of CT_VIEWS) {
      if (!ct.views[view].image) loadCtSlice(view);
    }
    return;
  }
  if (ct.metaPromise) return;
  const requestId = ++ct.metaRequestId;
  ct.metaPromise = api(`/api/cases/${encodeURIComponent(caseId)}/viewer`)
    .then((meta) => {
      if (
        state.caseId !== caseId ||
        state.ctViewer.caseId !== caseId ||
        ct.metaRequestId !== requestId ||
        ct.maskKey !== maskKey
      ) return;
      ct.meta = meta;
      for (const view of CT_VIEWS) {
        const rememberedIndex = ct.restoreIndices?.[view];
        syncCtIndex(view, Number.isFinite(rememberedIndex) ? rememberedIndex : meta.views[view].default_index);
      }
      ct.restoreIndices = null;
      syncCtControls();
      updateCtViewerNote();
      renderMetrics(state.case);
      renderActions(state.case);
      renderDetails(state.case);
      refreshViewer();
    })
    .catch((error) => {
      if (
        state.caseId === caseId &&
        ct.metaRequestId === requestId &&
        ct.maskKey === maskKey
      ) toast(error.detail?.reason || error.message || "CT 体数据读取失败", true);
    })
    .finally(() => {
      // An older request must not clear the promise belonging to a newer one.
      if (ct.metaRequestId === requestId) ct.metaPromise = null;
    });
}

function rerenderCtSlices() {
  for (const view of CT_VIEWS) loadCtSlice(view);
}

function updateCtViewerNote() {
  const hasMask = Boolean(state.ctViewer.meta?.mask_available || ["ok", "warn"].includes(state.case?.stages?.segment?.status));
  const cleanupPending = externalAirCleanupState().pending;
  const boundaryWarning = Boolean(state.ctViewer.meta?.mask_external_air_suspected);
  $("viewer-note").textContent = hasMask
    ? cleanupPending
      ? "原始 CT · 青绿色为未经清理的 HU 候选，包含体外背景空气 · 请先去除体外空气"
      : boundaryWarning
        ? "原始 CT · 青绿色为候选掩膜 · 头部外缘或体数据边界疑似残留体外空气"
        : "原始 CT · 青绿色为已清理候选掩膜 · 仍需逐层复核"
      : "原始 CT · 尚未生成气道掩膜";
}

function updateMaskFlow(current) {
  const order = ["candidate", "edit", "confirm", "export"];
  const currentIndex = order.indexOf(current);
  document.querySelectorAll("[data-mask-flow]").forEach((node) => {
    const index = order.indexOf(node.dataset.maskFlow);
    node.classList.toggle("is-current", index === currentIndex);
    node.classList.toggle("is-done", currentIndex >= 0 && index < currentIndex);
  });
}

function updateCtViewerGuide() {
  const guide = $("review-guide");
  if (!guide) return;
  const confirmButton = $("btn-confirm-mask");
  const exportButton = $("btn-export-mask");
  const cleanupButton = $("mask-remove-external");
  const feedback = $("review-guide-feedback");
  const hasVolume = Boolean(state.caseId && ["ok", "warn"].includes(state.case?.stages?.import?.status));
  if (!hasVolume) {
    guide.hidden = true;
    if (confirmButton) confirmButton.hidden = true;
    if (exportButton) exportButton.hidden = true;
    if (cleanupButton) cleanupButton.hidden = true;
    return;
  }
  const hasMask = Boolean(state.ctViewer.meta?.mask_available || ["ok", "warn"].includes(state.case?.stages?.segment?.status));
  const segment = state.case?.stages?.segment || {};
  const cleanupState = externalAirCleanupState();
  const needsHumanConfirm = Boolean(
    segment.data?.need_human_confirm || segment.status === "warn" ||
    state.ctViewer.meta?.mask_review?.need_human_confirm
  );
  const boundaryWarning = Boolean(
    state.ctViewer.meta?.mask_external_air_suspected || state.case?.stages?.segment?.data?.external_air_suspected
  );
  const title = $("review-guide-title");
  const text = $("review-guide-text");
  const status = $("review-guide-status");
  const cleanupSummary = cleanupState.status === "ok" ? externalAirCleanupSummary(cleanupState.cleanup) : null;
  const resultText = cleanupSummary
    ? `最近一次体外空气清理：${cleanupSummary}；${Number(cleanupState.cleanup.removed_voxels) === 0 ? "候选掩膜未变化，" : ""}仍需逐层复核。`
    : "";
  const localFeedback = state.maskEditor.externalAirFeedback;
  const localMessage = localFeedback?.kind === "error" || !resultText ? localFeedback?.message : null;
  feedback.setAttribute("role", localFeedback?.kind === "error" ? "alert" : "status");
  feedback.textContent = [resultText, localMessage].filter(Boolean).join(" ");
  feedback.hidden = !feedback.textContent;
  feedback.classList.toggle("is-error", localFeedback?.kind === "error");
  guide.hidden = false;
  if (confirmButton) confirmButton.hidden = true;
  if (exportButton) exportButton.hidden = true;
  if (cleanupButton) {
    cleanupButton.hidden = true;
    cleanupButton.classList.toggle("is-primary-clean", cleanupState.pending);
  }
  guide.classList.toggle("is-warning", cleanupState.pending || boundaryWarning);
  if (!hasMask) {
    title.textContent = "先完成气道分割，再检查青色候选区域";
    text.textContent = "当前只有原始 CT。完成“气道分割”后，青绿色区域会叠加在三平面图上；它只是候选结果，不是最终模型。";
    status.textContent = "等待分割";
    updateMaskFlow("candidate");
  } else if (state.maskEditor.autoCleaning && state.maskEditor.externalAirAction === "remove") {
    title.textContent = "正在去除可识别的体外空气";
    text.textContent = "请稍候。完成后会显示移除量，再逐层复核鼻孔、鼻咽和鼻窦。";
    status.textContent = "清理中";
    if (cleanupButton) cleanupButton.hidden = false;
    updateMaskFlow("candidate");
  } else if (cleanupState.pending) {
    title.textContent = "先去除体外空气，再复核气道";
    text.textContent = "青绿色是未经清理的 HU 阈值候选，包含头部周围的体外背景空气。点击“去除体外空气”生成待复核候选；之后仍须逐层检查鼻孔、鼻咽和鼻窦。";
    status.textContent = "待清理";
    if (cleanupButton) cleanupButton.hidden = false;
    updateMaskFlow("candidate");
  } else if (!state.ctViewer.meta) {
    title.textContent = "正在读取掩膜复核状态";
    text.textContent = "请稍候，正在核对当前掩膜和人工复核记录。";
    status.textContent = "检查中";
    updateMaskFlow("confirm");
  } else if (boundaryWarning) {
    title.textContent = "当前不能继续：候选掩膜需要修正";
    text.textContent = "清理后青色区域仍疑似触及体外边界。请逐层检查并用画笔或橡皮修正；若本次清理误删气道，可在编辑工具中撤销本次清理。";
    status.textContent = "需要修正";
    if (cleanupButton && state.maskEditor.active) cleanupButton.hidden = false;
    updateMaskFlow("edit");
  } else if (needsHumanConfirm) {
    title.textContent = "先确认青色区域只在鼻腔内";
    text.textContent = "体外空气清理已执行。拖动三平面层号，逐层检查目标气道、鼻孔、鼻咽和鼻窦；需要修正时点击“编辑掩膜”，完成后再确认气道范围。";
    status.textContent = "待人工复核";
    if (confirmButton) confirmButton.hidden = false;
    if (cleanupButton && state.maskEditor.active) cleanupButton.hidden = false;
    updateMaskFlow(state.maskEditor.dirty ? "edit" : "edit");
  } else {
    const needsEndplanes = Boolean(endplaneContext()) && !endplaneSaved();
    title.textContent = needsEndplanes ? "气道掩膜已确认，可以继续自动处理" : "气道掩膜已确认，可以导出或继续建模";
    text.textContent = needsEndplanes
      ? "点击工作台上方“自动处理当前病例”，系统将尝试生成并技术验证双侧三端口，再继续几何与 CFD；未通过时在 Agent 页说明局部修正原因。这里的手动端面工具仍可用于专家修正。"
      : "当前候选掩膜已通过人工确认。可以导出完整候选 NRRD 保存，也可以生成 STL 与几何质检进入下一步。";
    status.textContent = "已确认";
    if (exportButton) exportButton.hidden = false;
    updateMaskFlow("export");
  }
}

function updateMaskEditorUi() {
  const editor = state.maskEditor;
  window.WindcatcherSegmentationModels?.setContext({
    caseId: state.caseId, maskVersion: state.case?.stages?.segment?.data?.mask_version ?? state.ctViewer.meta?.mask_version,
    busy: Boolean(editor.saving || editor.autoCleaning || editor.dirty), editing: Boolean(editor.active),
    onViewChange(mode) {
      if (mode !== 'current' && (editor.dirty || editor.saving || editor.autoCleaning || editor.active)) return false;
      for (const view of Object.keys(state.ctViewer.views)) loadCtSlice(view);
      return true;
    },
    onJump(indices) { for (const [view, index] of Object.entries(indices)) loadCtSlice(view, index); },
    async onDraftAdopted(result) {
      await refreshMaskViewerState();
      state.mode = "slices";
      refreshViewer();
      focusStageTarget($("viewer-card"), $("btn-edit-mask"));
      toast(result?.message || "AI 候选已转为待复核草稿；下一步请打开 CT 复核");
    },
  });
  window.WindcatcherSegmentationLearning?.setContext({
    caseId: state.caseId, maskVersion: state.case?.stages?.segment?.data?.mask_version ?? state.ctViewer.meta?.mask_version,
    busy: Boolean(editor.saving || editor.autoCleaning),
  });
  const hasVolume = Boolean(state.caseId && ["ok", "warn"].includes(state.case?.stages?.import?.status));
  const hasMask = Boolean(state.ctViewer.meta?.mask_available || ["ok", "warn"].includes(state.case?.stages?.segment?.status));
  const toolbar = $("mask-editor-toolbar");
  if (toolbar) toolbar.hidden = !(editor.active && hasVolume && hasMask);
  const editButton = $("btn-edit-mask");
  if (editButton) editButton.classList.toggle("is-on", editor.active);
  const viewSelect = $("mask-edit-view");
  if (viewSelect) viewSelect.value = editor.view;
  const radiusRange = $("mask-brush-radius");
  const radiusNumber = $("mask-brush-radius-number");
  if (radiusRange) radiusRange.value = String(editor.radius);
  if (radiusNumber) radiusNumber.value = String(editor.radius);
  const paint = $("mask-tool-paint");
  const erase = $("mask-tool-erase");
  if (paint) { paint.classList.toggle("is-on", editor.tool === "paint"); paint.setAttribute("aria-pressed", String(editor.tool === "paint")); }
  if (erase) { erase.classList.toggle("is-on", editor.tool === "erase"); erase.setAttribute("aria-pressed", String(editor.tool === "erase")); }
  const status = $("mask-editor-status");
  if (status) {
    status.textContent = editor.autoCleaning
      ? (editor.externalAirAction === "undo" ? "正在撤销本次清理…" : "正在自动清理体外空气…")
      : editor.saving
      ? "正在保存笔画…"
      : editor.dirty
        ? "有未保存编辑"
        : editor.active
          ? `当前：${CT_VIEW_LABELS[editor.view]} · ${editor.tool === "erase" ? "橡皮" : "画笔"}`
          : "点击“编辑掩膜”开始";
  }
  const undo = $("mask-undo");
  const reset = $("mask-reset");
  const autoClean = $("mask-remove-external");
  const undoExternal = $("mask-undo-external");
  const save = $("mask-save");
  if (undo) undo.disabled = editor.saving || editor.autoCleaning || !editor.history.length;
  if (reset) reset.disabled = editor.saving || editor.autoCleaning || !hasMask;
  if (autoClean) {
    autoClean.disabled = editor.saving || editor.autoCleaning || !hasMask;
    const cleaning = editor.autoCleaning && editor.externalAirAction !== "undo";
    const pending = externalAirCleanupState().pending;
    autoClean.classList.toggle("is-busy", cleaning);
    autoClean.setAttribute("aria-busy", String(cleaning));
    autoClean.textContent = cleaning ? "正在清理…" : pending ? "去除体外空气" : "重新检查体外空气";
    autoClean.title = pending
      ? "根据 CT 体内包络去除可识别的体外空气；完成后仍需逐层复核"
      : "对编辑后的候选重新检查可识别的体外空气；若未新增残留，掩膜不会变化";
  }
  if (undoExternal) {
    const undoing = editor.autoCleaning && editor.externalAirAction === "undo";
    undoExternal.disabled = editor.saving || editor.autoCleaning || !hasMask || !state.ctViewer.meta?.can_undo_external_air;
    undoExternal.classList.toggle("is-busy", undoing);
    undoExternal.setAttribute("aria-busy", String(undoing));
    undoExternal.textContent = undoing ? "正在撤销…" : "撤销本次清理";
  }
  // 笔画会在抬指后自动提交；保留“保存编辑”作为显式重试/确认入口，避免用户以为编辑没有保存。
  if (save) save.disabled = editor.saving || editor.autoCleaning || !editor.active;
  CT_VIEWS.forEach((view) => {
    const stage = document.querySelector(`.slice-panel[data-view="${view}"] .slice-stage`);
    if (!stage) return;
    const editing = editor.active && editor.view === view && hasMask;
    stage.classList.toggle("is-mask-editing", editing);
    stage.classList.toggle("is-saving", editing && (editor.saving || editor.autoCleaning));
  });
}

function scheduleCtRerender() {
  const ct = state.ctViewer;
  clearTimeout(ct.requestTimer);
  ct.requestTimer = setTimeout(() => {
    const level = Number($("viewer-wl").value);
    const width = Number($("viewer-ww").value);
    if (Number.isFinite(level)) ct.level = level;
    if (Number.isFinite(width) && width > 0) ct.width = width;
    syncCtControls();
    rerenderCtSlices();
  }, 160);
}

function showSlices() {
  state.mode = "slices";
  $("btn-view-slices").classList.add("is-on");
  $("btn-view-3d").classList.remove("is-on");
  $("viewer-3d").hidden = true;
  $("viewer-preview-controls").hidden = true;
  $("viewer-views").hidden = false;
  $("viewer-controls").hidden = false;
  updateCtViewerNote();
  updateCtViewerGuide();
  $("viewer-sub").textContent = "浏览器内 CT 复核 · 原始层面 / 气道候选";
  updateMaskEditorUi();
  loadCtViewer(state.caseId);
}

function show3dBlocked(message) {
  state.mode = "slices";
  showSlices();
  const guide = $("review-guide");
  if (guide) {
    guide.hidden = false;
    guide.classList.add("is-warning");
    $("review-guide-title").textContent = "3D 预览暂时受阻";
    $("review-guide-text").textContent = message;
    $("review-guide-status").textContent = "请先完成上游步骤";
    guide.focus?.();
  }
  toast(message, true);
}

function show3d() {
  state.mode = "3d";
  $("btn-view-3d").classList.add("is-on");
  $("btn-view-slices").classList.remove("is-on");
  $("viewer-views").hidden = true;
  $("viewer-controls").hidden = true;
  $("review-guide").hidden = true;
  $("viewer-empty").hidden = true;
  const host = $("viewer-3d");
  host.hidden = false;
  $("viewer-preview-controls").hidden = false;
  $("viewer-preview-mode").value = state.previewMode;
  const warning = state.case?.stages?.surface?.status === "warn";
  $("viewer-note").textContent = warning ? "真实气道 STL · 几何质检有警告，详见下方结果" : "真实气道 STL · 拖拽旋转 / 滚轮缩放";
  $("viewer-sub").textContent = "3D 气道模型 · 本机生成的 airway.stl";
  if (!state.viewer3d) state.viewer3d = createViewer3d(host);
  state.viewer3d.resize();
  const caseId = state.caseId;
  const modelKey = surfaceModelKey();
  if (state.viewer3dCaseId !== caseId || state.viewer3dKey !== modelKey) {
    state.viewer3dCaseId = caseId;
    state.viewer3dKey = modelKey;
    $("viewer-sub").textContent = "正在加载最新裁切 STL…";
    state.viewer3d.load(`/api/cases/${encodeURIComponent(caseId)}/model?v=${encodeURIComponent(modelKey)}`, () => {
      if (state.caseId !== caseId || state.viewer3dKey !== modelKey) return;
      state.viewer3dCaseId = null;
      state.viewer3dKey = null;
      if (state.mode === "3d") show3dBlocked("STL 加载失败，请重试 STL 预览或检查本地产物。");
    }, () => {
      if (state.caseId === caseId && state.viewer3dKey === modelKey && state.mode === "3d") {
        $("viewer-sub").textContent = "3D 气道模型 · 本机生成的 airway.stl";
      }
    });
  }
}

function createViewer3d(host) {
  const width = () => host.clientWidth || 640;
  const height = () => host.clientHeight || 520;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d1519);
  const camera = new THREE.PerspectiveCamera(40, width() / height(), 0.1, 5000);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(width(), height());
  host.appendChild(renderer.domElement);

  scene.add(new THREE.HemisphereLight(0xffffff, 0xdde8e5, 1.15));
  const key = new THREE.DirectionalLight(0xffffff, 0.9);
  key.position.set(1.2, 1.5, 1.4);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x9fd8cf, 0.45);
  rim.position.set(-1.4, -0.5, -1.1);
  scene.add(rim);

  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;

  const group = new THREE.Group();
  scene.add(group);
  let currentMesh = null;
  let currentShading = null;
  let loadRequestId = 0;

  function clear() {
    loadRequestId += 1;
    if (!currentMesh) return;
    group.remove(currentMesh);
    currentMesh.geometry.dispose();
    currentMesh.material.dispose();
    currentMesh = null;
    currentShading = null;
  }

  function frame() {
    const box = new THREE.Box3().setFromObject(group);
    if (!box.isEmpty()) {
      const size = box.getSize(new THREE.Vector3());
      const center = box.getCenter(new THREE.Vector3());
      const radius = Math.max(size.x, size.y, size.z) || 1;
      controls.target.copy(center);
      camera.position.copy(center.clone().add(new THREE.Vector3(radius * 0.85, radius * 0.5, radius * 1.05)));
    }
  }

  (function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  })();

  window.addEventListener("resize", resize);
  function resize() {
    camera.aspect = width() / height();
    camera.updateProjectionMatrix();
    renderer.setSize(width(), height());
  }

  return {
    resize,
    clear,
    setPreviewMode(mode) {
      if (!currentMesh) return;
      applyPreviewShading(currentMesh.geometry, currentShading, mode);
      currentMesh.material.roughness = mode === "smooth" ? 0.48 : 0.2;
      currentMesh.material.clearcoat = mode === "smooth" ? 0.22 : 0.55;
    },
    load(url, onError, onLoad) {
      clear();
      const requestId = loadRequestId;
      const loader = new THREE.STLLoader();
      loader.load(url, (geometry) => {
        if (requestId !== loadRequestId) { geometry.dispose(); return; }
        currentShading = preparePreviewShading(geometry);
        applyPreviewShading(geometry, currentShading, state.previewMode);
        const material = new THREE.MeshPhysicalMaterial({
          color: 0x7cc6b9,
          metalness: 0.0,
          roughness: state.previewMode === "smooth" ? 0.48 : 0.2,
          transmission: 0.5,
          ior: 1.32,
          clearcoat: state.previewMode === "smooth" ? 0.22 : 0.55,
          clearcoatRoughness: 0.22,
          transparent: true,
          opacity: 0.96,
        });
        currentMesh = new THREE.Mesh(geometry, material);
        group.add(currentMesh);
        frame();
        if (onLoad) onLoad();
      }, undefined, () => {
        if (requestId !== loadRequestId) return;
        if (onError) onError();
      });
    },
  };
}

function maskEditorHasMask() {
  const segment = state.case?.stages?.segment;
  return Boolean(
    state.caseId &&
    segment &&
    ["ok", "warn"].includes(segment.status) &&
    (state.ctViewer.meta?.mask_available !== false)
  );
}

function rememberMaskPoint(view, event) {
  const editor = state.maskEditor;
  const stroke = editor.stroke;
  if (!stroke || editor.saving || editor.autoCleaning || stroke.view !== view) return;
  const point = maskPointFromEvent(view, event);
  if (!point) return;
  const index = state.ctViewer.views[view].index;
  const previous = stroke.operations[stroke.operations.length - 1];
  const minDistance = Math.max(1, editor.radius * 0.28);
  // 后端每笔最多接受 256 个原始切片点；长拖动按上限采样，避免一次笔画被拒绝。
  if (stroke.operations.length >= 240) return;
  if (previous && previous.index === index && Math.hypot(previous.x - point.x, previous.y - point.y) < minDistance) {
    state.ctViewer.views[view].cursor = { x: point.localX, y: point.localY };
    drawCtSlice(view);
    return;
  }
  const operation = { view, index, x: point.x, y: point.y, radius: editor.radius, mode: editor.tool === "erase" ? "erase" : "add" };
  stroke.operations.push(operation);
  editor.preview.push(operation);
  editor.dirty = true;
  state.ctViewer.views[view].cursor = { x: point.localX, y: point.localY };
  drawCtSlice(view);
}

async function refreshMaskViewerState(previousIndices = null) {
  const capturedCaseId = state.caseId;
  if (!capturedCaseId) return;
  const active = state.maskEditor.active;
  state.case = await api(`/api/cases/${encodeURIComponent(capturedCaseId)}`);
  if (state.caseId !== capturedCaseId) return;
  resetCtViewer(capturedCaseId);
  state.ctViewer.maskKey = ctMaskKey(state.case);
  state.ctViewer.restoreIndices = previousIndices;
  state.maskEditor.active = active;
  renderCase();
}

async function sendMaskOperations(operations, { recordHistory = true } = {}) {
  const editor = state.maskEditor;
  const capturedCaseId = state.caseId;
  if (!capturedCaseId || !operations?.length || editor.saving || editor.autoCleaning) return false;
  window.WindcatcherMonitor?.record("viewer", "mask_edit", {
    view: operations[0].view, index: operations[0].index, tool: editor.tool, radius: editor.radius,
    operation_count: operations.length, point_count: operations.reduce((count, operation) => count + (operation.points?.length || 0), 0),
  }, "mask-editor");
  const previousIndices = Object.fromEntries(CT_VIEWS.map((view) => [view, state.ctViewer.views[view].index]));
  editor.saving = true;
  editor.preview = operations.flatMap((operation) => (operation.points || []).map((point) => ({
    view: operation.view,
    index: operation.index,
    x: point.x,
    y: point.y,
    mode: operation.action === "erase" ? "erase" : "add",
  })));
  updateMaskEditorUi();
  CT_VIEWS.forEach(drawCtSlice);
  try {
    await api(`/api/cases/${encodeURIComponent(capturedCaseId)}/segment/edit-mask`, {
      method: "POST",
      body: { operations },
    });
    if (recordHistory) editor.history.push(operations.map((operation) => ({
      ...operation,
      points: (operation.points || []).map((point) => ({ ...point })),
    })));
    editor.pending = [];
    editor.preview = [];
    editor.dirty = false;
    editor.externalAirFeedback = null;
    await refreshMaskViewerState(previousIndices);
    toast("掩膜编辑已保存，切片已刷新");
    return true;
  } catch (error) {
    editor.pending = operations.map((operation) => ({
      ...operation,
      points: (operation.points || []).map((point) => ({ ...point })),
    }));
    editor.dirty = true;
    toast(error.detail?.reason || error.message || "掩膜编辑保存失败", true);
    return false;
  } finally {
    editor.saving = false;
    updateMaskEditorUi();
    CT_VIEWS.forEach(drawCtSlice);
  }
}

async function undoMaskEdit() {
  const editor = state.maskEditor;
  if (editor.saving || editor.autoCleaning || !editor.history.length) return;
  const last = editor.history[editor.history.length - 1];
  const inverse = last.slice().reverse().map((operation) => ({
    ...operation,
    action: operation.action === "erase" ? "paint" : "erase",
    points: (operation.points || []).map((point) => ({ ...point })),
  }));
  if (await sendMaskOperations(inverse, { recordHistory: false })) {
    editor.history.pop();
    updateMaskEditorUi();
    toast("已撤销上一笔编辑");
  }
}

async function resetMaskCandidate() {
  const editor = state.maskEditor;
  if (editor.saving || editor.autoCleaning || !state.caseId || !maskEditorHasMask()) return;
  const capturedCaseId = state.caseId;
  const previousIndices = Object.fromEntries(CT_VIEWS.map((view) => [view, state.ctViewer.views[view].index]));
  editor.saving = true;
  updateMaskEditorUi();
  try {
    await api(`/api/cases/${encodeURIComponent(capturedCaseId)}/segment/edit-mask`, { method: "POST", body: { reset: true } });
    editor.history = [];
    editor.preview = [];
    editor.pending = [];
    editor.dirty = false;
    editor.externalAirFeedback = null;
    await refreshMaskViewerState(previousIndices);
    toast("已重置为算法候选掩膜");
  } catch (error) {
    toast(error.detail?.reason || error.message || "重置候选失败", true);
  } finally {
    editor.saving = false;
    updateMaskEditorUi();
  }
}

async function removeExternalAir() {
  const editor = state.maskEditor;
  const capturedCaseId = state.caseId;
  if (editor.saving || editor.autoCleaning || !capturedCaseId || !maskEditorHasMask()) return;
  const previousIndices = Object.fromEntries(CT_VIEWS.map((view) => [view, state.ctViewer.views[view].index]));
  editor.autoCleaning = true;
  editor.externalAirAction = "remove";
  editor.externalAirFeedback = null;
  editor.stroke = null;
  editor.preview = [];
  editor.pending = [];
  editor.dirty = false;
  updateCtViewerGuide();
  updateMaskEditorUi();
  CT_VIEWS.forEach(drawCtSlice);
  let cleanupCompleted = false;
  try {
    const result = await api(`/api/cases/${encodeURIComponent(capturedCaseId)}/segment/remove-external-air`, {
      method: "POST",
      body: {},
    });
    if (state.caseId !== capturedCaseId) return;
    cleanupCompleted = true;
    // 自动清理不是一组可逆的画笔点，避免撤销按钮给出无法兑现的承诺。
    editor.history = [];
    // 三平面优先显示完整候选，提示也必须使用所显示掩膜的清理统计。
    const showingCandidate = Boolean(result.candidate_available);
    const cleanup = showingCandidate ? result.candidate_cleanup : result.cleanup;
    const removedValue = showingCandidate
      ? result.candidate_removed_voxels ?? cleanup?.removed_voxels
      : result.removed_voxels ?? cleanup?.removed_voxels;
    const removedVoxels = removedValue === null || removedValue === undefined ? NaN : Number(removedValue);
    const volumeValue = cleanup?.removed_volume_ml;
    const removedVolumeMl = volumeValue === null || volumeValue === undefined ? NaN : Number(volumeValue);
    const summary = externalAirCleanupSummary({ ...cleanup, removed_voxels: removedValue });
    if (summary) editor.externalAirFeedback = { kind: "info", message: `最近一次体外空气清理：${summary}；${removedVoxels === 0 ? "候选掩膜未变化，" : ""}仍需逐层复核。` };
    await refreshMaskViewerState(previousIndices);
    if (!Number.isFinite(removedVoxels)) {
      editor.externalAirFeedback = { kind: "error", message: "清理已执行，但未能读取当前显示掩膜的清理数量；请逐层复核。" };
      toast("清理已执行，但未能读取当前显示掩膜的清理数量；请逐层复核", true);
    } else if (removedVoxels === 0) {
      editor.externalAirFeedback = { kind: "info", message: "未识别到可移除的体外空气；当前显示的掩膜未变化，仍需逐层复核。" };
      toast("未识别到可移除的体外空气；当前显示的掩膜未变化，仍需逐层复核");
    } else {
      const cleanupStats = `，当前显示的掩膜移除约 ${removedVoxels.toLocaleString()} 个体素${Number.isFinite(removedVolumeMl) ? `（${removedVolumeMl} mL）` : ""}`;
      toast(`${result.message || "已清理可识别的体外空气，请继续逐层复核"}${cleanupStats}`);
    }
  } catch (error) {
    if (state.caseId !== capturedCaseId) return;
    const reason = error.detail?.reason || error.message || "未知错误";
    const message = cleanupCompleted
      ? `清理已执行，但视窗刷新失败：${reason}。请刷新病例并复核。`
      : `去除体外空气失败：${reason}`;
    editor.externalAirFeedback = { kind: "error", message };
    toast(message, true);
  } finally {
    editor.autoCleaning = false;
    editor.externalAirAction = null;
    updateCtViewerGuide();
    updateMaskEditorUi();
    CT_VIEWS.forEach(drawCtSlice);
  }
}

async function undoExternalAir() {
  const editor = state.maskEditor;
  const capturedCaseId = state.caseId;
  if (editor.saving || editor.autoCleaning || !capturedCaseId || !maskEditorHasMask() || !state.ctViewer.meta?.can_undo_external_air) return;
  const previousIndices = Object.fromEntries(CT_VIEWS.map((view) => [view, state.ctViewer.views[view].index]));
  editor.autoCleaning = true;
  editor.externalAirAction = "undo";
  editor.stroke = null;
  editor.preview = [];
  updateMaskEditorUi();
  CT_VIEWS.forEach(drawCtSlice);
  try {
    const result = await api(`/api/cases/${encodeURIComponent(capturedCaseId)}/segment/undo-external-air`, {
      method: "POST",
      body: {},
    });
    editor.history = [];
    editor.pending = [];
    editor.dirty = false;
    editor.externalAirFeedback = null;
    await refreshMaskViewerState(previousIndices);
    toast(result.message || "已撤销本次自动清理，请重新检查三平面掩膜");
  } catch (error) {
    toast(error.detail?.reason || error.message || "撤销本次清理失败", true);
  } finally {
    editor.autoCleaning = false;
    editor.externalAirAction = null;
    updateMaskEditorUi();
    CT_VIEWS.forEach(drawCtSlice);
  }
}

function bindMaskEditorControls() {
  const editor = state.maskEditor;
  $("btn-edit-mask").addEventListener("click", () => {
    if (!maskEditorHasMask()) {
      toast("请先完成气道分割，生成候选掩膜", true);
      return;
    }
    editor.active = !editor.active;
    if (!editor.active) {
      editor.stroke = null;
      editor.preview = [];
      CT_VIEWS.forEach((view) => { state.ctViewer.views[view].cursor = null; });
    }
    state.mode = "slices";
    refreshViewer();
    if (editor.active) $("mask-editor-toolbar")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  $("mask-edit-view").addEventListener("change", (event) => {
    editor.view = event.target.value;
    editor.preview = [];
    CT_VIEWS.forEach((view) => { state.ctViewer.views[view].cursor = null; drawCtSlice(view); });
    updateMaskEditorUi();
  });
  $("mask-tool-paint").addEventListener("click", () => { editor.tool = "paint"; updateMaskEditorUi(); });
  $("mask-tool-erase").addEventListener("click", () => { editor.tool = "erase"; updateMaskEditorUi(); });
  const radiusRange = $("mask-brush-radius");
  const radiusNumber = $("mask-brush-radius-number");
  const updateRadius = (value) => {
    const numeric = Math.max(1, Math.min(64, Number(value) || 1));
    editor.radius = numeric;
    updateMaskEditorUi();
    CT_VIEWS.forEach(drawCtSlice);
  };
  radiusRange.addEventListener("input", (event) => updateRadius(event.target.value));
  radiusNumber.addEventListener("input", (event) => updateRadius(event.target.value));
  radiusNumber.addEventListener("change", (event) => updateRadius(event.target.value));
  $("mask-undo").addEventListener("click", undoMaskEdit);
  $("mask-reset").addEventListener("click", resetMaskCandidate);
  $("mask-remove-external").addEventListener("click", removeExternalAir);
  $("mask-undo-external").addEventListener("click", undoExternalAir);
  $("mask-save").addEventListener("click", async () => {
    if (editor.pending?.length) {
      const pending = editor.pending.splice(0);
      await sendMaskOperations(pending);
    } else {
      toast(editor.dirty ? "当前笔画正在保存，请稍候" : "每笔操作完成后会自动保存");
    }
  });
}

function bindCtViewerControls() {
  for (const view of CT_VIEWS) {
    const range = $(`slice-range-${view}`);
    const stage = document.querySelector(`.slice-panel[data-view="${view}"] .slice-stage`);
    range.addEventListener("input", (event) => loadCtSlice(view, Number(event.target.value)));
    let drag = null;
    let monitorGestureTimer = null;
    const recordView = (gesture) => {
      const entry = state.ctViewer.views[view];
      window.WindcatcherMonitor?.record("viewer", "ct_view_changed", {
        view, gesture, index: entry.index, controls: [entry.zoom, entry.panX, entry.panY],
      }, `ct-${view}`);
    };
    stage.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      const editor = state.maskEditor;
      if (editor.active && editor.view === view && maskEditorHasMask() && !editor.saving && !editor.autoCleaning) {
        event.preventDefault();
        editor.stroke = { pointerId: event.pointerId, view, operations: [] };
        stage.setPointerCapture(event.pointerId);
        rememberMaskPoint(view, event);
        return;
      }
      drag = { pointerId: event.pointerId, x: event.clientX, y: event.clientY };
      stage.setPointerCapture(event.pointerId);
    });
    stage.addEventListener("pointermove", (event) => {
      const editor = state.maskEditor;
      if (editor.active && editor.view === view && maskEditorHasMask() && !editor.autoCleaning) {
        const point = maskPointFromEvent(view, event);
        const entry = state.ctViewer.views[view];
        if (point) {
          entry.cursor = { x: point.localX, y: point.localY };
          if (editor.stroke?.pointerId === event.pointerId) rememberMaskPoint(view, event);
          else drawCtSlice(view);
        } else {
          entry.cursor = null;
          drawCtSlice(view);
        }
        return;
      }
      if (!drag || drag.pointerId !== event.pointerId) return;
      const entry = state.ctViewer.views[view];
      entry.panX += event.clientX - drag.x;
      entry.panY += event.clientY - drag.y;
      drag.x = event.clientX;
      drag.y = event.clientY;
      drawCtSlice(view);
    });
    const stopDrag = (event) => {
      const editor = state.maskEditor;
      if (editor.stroke?.pointerId === event.pointerId && editor.stroke.view === view) {
        const points = editor.stroke.operations.map((operation) => ({ x: operation.x, y: operation.y }));
        const first = editor.stroke.operations[0];
        const operations = first ? [{
          view: first.view,
          index: first.index,
          action: first.mode === "erase" ? "erase" : "paint",
          points,
          radius: first.radius,
        }] : [];
        editor.stroke = null;
        editor.preview = [];
        if (operations.length) sendMaskOperations(operations);
        return;
      }
      if (drag?.pointerId === event.pointerId) {
        drag = null;
        recordView("pan");
      }
    };
    stage.addEventListener("pointerup", stopDrag);
    stage.addEventListener("pointercancel", stopDrag);
    stage.addEventListener("pointerleave", () => {
      if (!state.maskEditor.stroke || state.maskEditor.stroke.view !== view) {
        state.ctViewer.views[view].cursor = null;
        drawCtSlice(view);
      }
    });
    stage.addEventListener("dblclick", () => {
      if (state.maskEditor.active && state.maskEditor.view === view) return;
      const entry = state.ctViewer.views[view];
      entry.zoom = 1;
      entry.panX = 0;
      entry.panY = 0;
      drawCtSlice(view);
      recordView("double_click");
    });
    stage.addEventListener("wheel", (event) => {
      event.preventDefault();
      const entry = state.ctViewer.views[view];
      if (monitorGestureTimer !== null) window.clearTimeout(monitorGestureTimer);
      monitorGestureTimer = window.setTimeout(() => {
        monitorGestureTimer = null;
        recordView(event.ctrlKey || event.metaKey ? "zoom" : "wheel");
      }, 350);
      if (event.ctrlKey || event.metaKey) {
        entry.zoom = Math.max(.4, Math.min(6, entry.zoom * (event.deltaY > 0 ? .9 : 1.1)));
        drawCtSlice(view);
        return;
      }
      if (state.ctViewer.meta) {
        const delta = event.deltaY > 0 ? 1 : -1;
        loadCtSlice(view, entry.index + delta);
      }
    }, { passive: false });
  }
  $("viewer-wl").addEventListener("input", scheduleCtRerender);
  $("viewer-ww").addEventListener("input", scheduleCtRerender);
  $("viewer-overlay").addEventListener("change", (event) => {
    state.ctViewer.overlay = event.target.checked;
    rerenderCtSlices();
  });
  $("viewer-preset-air").addEventListener("click", () => {
    state.ctViewer.level = -500;
    state.ctViewer.width = 2000;
    syncCtControls();
    rerenderCtSlices();
  });
  $("viewer-preset-soft").addEventListener("click", () => {
    state.ctViewer.level = 40;
    state.ctViewer.width = 400;
    syncCtControls();
    rerenderCtSlices();
  });
  $("viewer-reset").addEventListener("click", () => {
    for (const view of CT_VIEWS) {
      const entry = state.ctViewer.views[view];
      entry.zoom = 1;
      entry.panX = 0;
      entry.panY = 0;
      drawCtSlice(view);
    }
  });
  window.addEventListener("resize", () => CT_VIEWS.forEach(drawCtSlice));
}

/* ---------- 环境页 ---------- */
async function loadEnv() {
  const box = $("env-body");
  box.innerHTML = `<div class="skeleton" style="height:180px;"></div>`;
  const caps = await api("/api/capabilities");
  state.computeCapabilities = caps.compute || state.computeCapabilities;
  const statusPill = (status) =>
    status === "available" ? `<span class="pill pill-ok"><i></i>可用</span>`
    : status === "unknown" ? `<span class="pill pill-warn"><i></i>未能探测</span>`
    : `<span class="pill pill-mute"><i></i>未安装</span>`;
  const renderCapability = (item) => `<div class="cap-row">
          <span class="cap-name">${escapeHtml(item.name)}</span>${statusPill(item.status)}
          <div class="cap-detail">${escapeHtml(item.detail || "")}
            ${item.remediation?.length ? `<ul class="cap-remedy">${item.remediation.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>` : ""}
          </div>
          <div class="cap-actions">${item.install && item.status !== "available" ? item.install.url
            ? `<a class="button button-quiet cap-install" href="${escapeHtml(item.install.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.install.label)}</a>`
            : `<button type="button" class="button button-quiet cap-install" data-install-target="${escapeHtml(item.install.target)}" data-install-label="${escapeHtml(item.install.label)}">${escapeHtml(item.install.label)}</button>`
            : ""}</div>
        </div>`;
  const groupByTitle = new Map((caps.groups || []).map((group) => [group.title, group.items || []]));
  const pythonItems = groupByTitle.get("Python 依赖") || [];
  const medicalItems = groupByTitle.get("医学影像与几何") || [];
  const cfdItems = groupByTitle.get("CFD 求解环境") || [];
  const findItem = (items, prefix) => items.find((item) => item.name?.startsWith(prefix));
  const openfoam = findItem(cfdItems, "OpenFOAM");
  const runtime = openfoam?.status === "available"
    ? ({ "OpenFOAM（Docker）": "Docker", "OpenFOAM（WSL）": "WSL" })[openfoam.name]
    : null;
  const currentCfdItems = cfdItems.filter((item) => item === openfoam || item.name === runtime);
  const fallbackCfdItems = cfdItems.filter((item) => !currentCfdItems.includes(item));
  const cfdCopy = openfoam?.status === "available"
    ? runtime ? `自动求解当前优先使用 ${runtime} 中的 OpenFOAM；下方显示对应运行环境。`
      : "自动求解当前优先使用本机 OpenFOAM。"
    : "尚未检测到可用的 OpenFOAM 求解后端，请查看检测结果和安装建议。";
  const optionalTools = medicalItems.filter((item) => ["3D Slicer", "ParaView", "Blender"].includes(item.name));
  const notIntegrated = medicalItems.filter((item) => item.name.startsWith("AI 分割模型"));
  const renderedKnown = new Set([...pythonItems, ...currentCfdItems, ...fallbackCfdItems, ...optionalTools, ...notIntegrated]);
  const unknownItems = (caps.groups || []).flatMap((group) => group.items || []).filter((item) => !renderedKnown.has(item));
  const availabilityCount = (items) => `${items.filter((item) => item.status === "available").length}/${items.length} 可用`;
  const renderSection = (title, copy, items, className = "") => items.length ? `<div class="card cap-section ${className}"><div class="card-body cap-group"><div class="cap-section-head"><div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(copy)}</p></div><span class="cap-count">${escapeHtml(availabilityCount(items))}</span></div>${items.map(renderCapability).join("")}</div></div>` : "";
  const renderOptionalSection = (title, copy, items, className = "") => items.length ? `<details class="card cap-section cap-collapsible ${className}"><summary><span><strong>${escapeHtml(title)}</strong><small>${escapeHtml(copy)}</small></span><span class="cap-count">${escapeHtml(availabilityCount(items))}</span></summary><div class="card-body cap-group">${items.map(renderCapability).join("")}</div></details>` : "";
  const ai = caps.ai_interpretation || {};
  box.innerHTML = `<div class="env-flow-note"><div><strong>当前流程在浏览器内完成</strong><p>CT 浏览、三平面复核、掩膜编辑、端面裁切和几何检查不依赖桌面版 3D Slicer、Blender 或 ParaView。</p></div><span class="pill pill-ok"><i></i>网页核心流程</span></div>` +
    renderSection("当前流程 · Python 服务依赖", "这些组件由风见当前网页和本地管线直接使用。", pythonItems, "cap-core") +
    renderSection("当前流程 · CFD 求解", cfdCopy, currentCfdItems, "cap-core") +
    renderOptionalSection("备用运行环境", "WSL 或 Docker 可作为其他运行环境；环境已安装不代表其中的 OpenFOAM 已可求解，以求解后端检测为准。", fallbackCfdItems, "cap-optional") +
    renderOptionalSection("外部工具（可选）", "仅用于高级人工复核或外部几何检查，缺少不阻断网页主流程。", optionalTools, "cap-optional") +
    renderOptionalSection("尚未接入能力", "AI 分割模型目前不参与当前分割流程，安装状态不会阻断病例处理。", notIntegrated, "cap-not-integrated") +
    renderOptionalSection("其他检测项", "检测到的附加能力。", unknownItems, "cap-optional") +
    `<div class="card ai-config-card"><div class="card-body"><div class="ai-config-head"><div><h2>AI 报告解读（可选）</h2><p>只发送脱敏后的 CFD 统计，不上传 CT、DICOM、STL 或病例原始目录。</p></div>${ai.status === "available" ? `<span class="pill pill-ok"><i></i>已配置</span>` : `<span class="pill pill-mute"><i></i>未配置</span>`}</div>
      <div class="ai-config-grid">
        <label class="field"><span>OpenAI 兼容 API URL</span><input id="ai-report-base-url" type="url" value="${escapeHtml(ai.base_url || "")}" placeholder="https://api.openai.com/v1" autocomplete="url" /></label>
        <label class="field"><span>API 密钥</span><input id="ai-report-api-key" type="password" value="" placeholder="粘贴后点击获取模型" autocomplete="new-password" /></label>
        <label class="field"><span>模型（先获取）</span><select id="ai-report-model" disabled><option value="${escapeHtml(ai.model || "gpt-4o-mini")}">${escapeHtml(ai.model || "gpt-4o-mini")}</option></select></label>
      </div>
      <div class="ai-config-status"><span>${escapeHtml(ai.reason || "未配置")}</span><small>密钥只保存在当前服务进程内存中，不写入页面、病例或配置文件。${ai.api_key_configured ? "当前账号已有可用密钥。" : ai.key_env ? "也可通过环境变量 WINDCATCHER_AI_API_KEY 提供。" : "请为当前账号填写 API 密钥；服务重启后需重新填写。"}</small></div>
      <div class="ai-config-actions"><button id="ai-report-models-fetch" type="button" class="button button-quiet button-compact">获取模型</button><button id="ai-report-config-save" type="button" class="button button-primary button-compact" disabled>保存连接配置</button></div>
    </div></div>` +
    `<p class="env-probe-note">探测时间 ${escapeHtml(caps.generated_at)} · ${escapeHtml(caps.notes.join("；"))}</p>`;
  updateInstallButtons();
  box.querySelectorAll("[data-install-target]").forEach((button) => {
    button.addEventListener("click", () => beginInstall(button.dataset.installTarget, button.dataset.installLabel));
  });
  $("ai-report-models-fetch")?.addEventListener("click", fetchAIModels);
  $("ai-report-config-save")?.addEventListener("click", saveAIReportConfig);
}

async function fetchAIModels() {
  const button = $("ai-report-models-fetch");
  const saveButton = $("ai-report-config-save");
  const baseUrl = $("ai-report-base-url")?.value.trim();
  const apiKey = $("ai-report-api-key")?.value.trim();
  const select = $("ai-report-model");
  if (!baseUrl) {
    toast("请先填写 AI API URL", true);
    return;
  }
  busy(button, true);
  try {
    const result = await api("/api/ai-report/models", { method: "POST", body: { base_url: baseUrl, api_key: apiKey } });
    const models = Array.isArray(result.models) ? result.models : [];
    if (!models.length) throw new Error("服务没有返回可用模型");
    select.innerHTML = models.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.id)}</option>`).join("");
    select.disabled = false;
    saveButton.disabled = false;
    toast(`已获取 ${models.length} 个模型，请选择后保存`);
  } catch (error) {
    toast(error.detail?.reason || error.message || "获取模型失败", true);
  } finally {
    busy(button, false);
  }
}

async function saveAIReportConfig() {
  const button = $("ai-report-config-save");
  const baseUrl = $("ai-report-base-url")?.value.trim();
  const model = $("ai-report-model")?.value.trim();
  const apiKey = $("ai-report-api-key")?.value.trim();
  if (!baseUrl) {
    toast("请先填写 AI API URL", true);
    return;
  }
  busy(button, true);
  try {
    await api("/api/ai-report/config", { method: "PUT", body: { base_url: baseUrl, model, api_key: apiKey } });
    toast("AI 连接配置已保存");
    await loadEnv();
  } catch (error) {
    toast(error.detail?.reason || error.message || "AI 连接配置保存失败", true);
  } finally {
    busy(button, false);
  }
}

function updateInstallButtons() {
  document.querySelectorAll("[data-install-target]").forEach((button) => {
    const job = state.installJobs.get(button.dataset.installTarget);
    if (!job) return;
    button.disabled = true;
    button.classList.toggle("is-busy", job.status === "queued" || job.status === "running");
    button.textContent = job.status === "queued" ? "等待安装…" : "安装中…";
  });
}

async function beginInstall(target, label) {
  if (state.installJobs.has(target)) return;
  const message = target === "openfoam-image"
    ? "将使用 Docker 拉取 OpenFOAM 镜像，可能下载数 GB 数据。确定继续吗？"
    : `${label || "安装"}会修改本机环境，可能需要几分钟。确定继续吗？`;
  if (!window.confirm(message)) return;
  try {
    const job = await api("/api/environment/install", { method: "POST", body: { target } });
    state.installJobs.set(target, job);
    updateInstallButtons();
    pollInstall(target);
  } catch (error) {
    toast(error.detail?.reason || error.message || "无法开始安装", true);
  }
}

async function pollInstall(target) {
  const current = state.installJobs.get(target);
  if (!current) return;
  try {
    const job = await api(`/api/environment/install/${encodeURIComponent(current.job_id)}`);
    state.installJobs.set(target, job);
    updateInstallButtons();
    if (["queued", "running"].includes(job.status)) {
      setTimeout(() => pollInstall(target), 1200);
      return;
    }
    state.installJobs.delete(target);
    if (job.status === "completed") {
      toast(`${job.message || "安装完成"}，正在重新探测环境`);
      await loadEnv();
    } else {
      toast(job.message || "安装失败，请查看环境说明", true);
      await loadEnv();
    }
  } catch (error) {
    state.installJobs.delete(target);
    updateInstallButtons();
    toast(error.detail?.reason || error.message || "无法读取安装进度", true);
  }
}

function renderMethod() {
  const intro = `<p class="method-intro">你只需要理解下面 5 个动作。括号里的内容是后台自动执行的技术子步骤，不需要你填写参数；只有“确认气道”需要你看 CT。</p>`;
  const steps = [
    ["01", "导入 CT", "在“病例”页选择收到的那一套 DICOM 文件夹。后台会自动读取体数据，并显示导入进度。"],
    ["02", "检查质量", "后台自动检查层厚、像素间距、序列完整性和隐私标记；扫描范围是否覆盖鼻尖到鼻咽，需要你根据质控提示判断。"],
    ["03", "编辑并确认气道", "后台先生成青绿色候选掩膜。你在三平面 CT 视窗逐层查看；需要修正时打开“编辑掩膜”，用画笔补充目标气道空腔、用橡皮去掉体外空气，保存后再确认范围。"],
    ["04", "选端面并生成模型", "确认候选范围后，在三平面 CT 中选择鼻孔侧和鼻咽侧裁切层面并保存，再生成 STL、检查几何。完整候选中断开的旁支仍需逐一复核。"],
    ["05", "求解与报告", "本机有 OpenFOAM 后才能开始气流求解。残差、质量守恒和场文件都通过后，才会生成真实报告；没有结果就显示“未生成”。"],
  ];
  $("method-body").innerHTML = intro + steps
    .map(([num, name, text]) => `<div class="method-step"><span class="method-name">${num}</span><div class="method-body"><h2>${name}</h2><p>${text}</p></div></div>`)
    .join("");
}

/* ---------- 事件绑定 ---------- */
$("btn-refresh-cases").addEventListener("click", () => loadCases().catch((e) => toast(e.message, true)));
$("btn-refresh-env").addEventListener("click", () => loadEnv().catch((e) => toast(e.message, true)));
$("btn-history-cases").addEventListener("click", () => {
  window.location.hash = "cases";
  showRoute("cases");
});
initHistoryDrawer();
$("btn-pick-directory").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (window.WindcatcherSharedAccess) {
    $("newcase-upload").click();
    return;
  }
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 60_000);
  busy(button, true);
  try {
    const result = await api("/api/pick-directory", { signal: controller.signal });
    if (result.status === "ok") {
      $("newcase-path").value = result.path;
      $("selected-source").textContent = "已选择本机文件夹";
      $("selected-source").classList.add("is-selected");
      toast("已选择 DICOM 文件夹");
    } else if (result.status !== "cancelled") {
      toast(result.message || "没有选择文件夹", true);
    }
  } catch (error) {
    if (error.name === "AbortError") {
      toast("文件夹选择器等待超时。请按 Alt+Tab 查找选择窗口，或使用“手动输入路径（高级）”", true);
      return;
    }
    toast(error.detail?.reason || error.message || "无法打开文件夹选择器", true);
  } finally {
    window.clearTimeout(timeout);
    busy(button, false);
  }
});
$("newcase-upload").addEventListener("change", (event) => {
  const files = Array.from(event.currentTarget.files || []);
  state.uploadFiles = files;
  const names = new Set(files.map((file) => {
    const parts = String(file.webkitRelativePath || file.name || "").replace(/\\/g, "/").split("/").filter(Boolean);
    return parts.length > 1 ? parts[0] : "";
  }).filter(Boolean));
  if (!files.length) {
    $("selected-source").textContent = "尚未选择";
    $("selected-source").classList.remove("is-selected");
    return;
  }
  if (names.size > 1) {
    state.uploadFiles = [];
    event.currentTarget.value = "";
    $("selected-source").textContent = "请选择单个序列文件夹";
    $("selected-source").classList.remove("is-selected");
    toast("一次只能上传一个 DICOM 序列文件夹", true);
    return;
  }
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  const sizeLabel = totalBytes >= 1024 ** 3
    ? `${(totalBytes / 1024 ** 3).toFixed(2)} GB`
    : `${(totalBytes / 1024 ** 2).toFixed(0)} MB`;
  $("selected-source").textContent = `${files.length} 个文件 · ${sizeLabel}，提交时上传`;
  $("selected-source").classList.add("is-selected");
});
$("btn-edit-path").addEventListener("click", (event) => {
  const input = $("newcase-path");
  const editing = input.readOnly;
  input.readOnly = !editing;
  event.currentTarget.setAttribute("aria-pressed", String(editing));
  event.currentTarget.textContent = editing ? "收起手动路径输入" : "手动输入路径（高级）";
  if (editing) input.focus();
});
$("btn-view-slices").addEventListener("click", () => { state.mode = "slices"; refreshViewer(); });
$("btn-view-3d").addEventListener("click", () => activateStlPreview());
async function refreshAfterMaskReview(button, path, successMessage) {
  const capturedCaseId = state.caseId;
  if (!capturedCaseId) return;
  if (!guardEndplaneDraft()) return;
  if (path === "/segment/confirm" && externalAirCleanupState().pending) {
    state.mode = "slices";
    refreshViewer();
    toast("请先去除体外空气，再逐层复核并确认气道范围", true);
    return;
  }
  const originalText = button?.textContent;
  state.maskEditor.externalAirFeedback = null;
  updateCtViewerGuide();
  busy(button, true);
  if (button && path === "/segment/confirm") button.textContent = "正在逐层核查 CT…";
  try {
    const result = await api(`/api/cases/${encodeURIComponent(capturedCaseId)}${path}`, { method: "POST" });
    if (state.caseId !== capturedCaseId) return;
    const selected = await selectCase(capturedCaseId);
    if (selected === false || state.caseId !== capturedCaseId) return;
    toast(result.message || successMessage);
    if (path === "/segment/confirm") {
      if (state.case?.synthetic && endplaneContext() && !state.endplanes.open) $("btn-endplanes").click();
      else if (!state.case?.synthetic) focusStageTarget($("quick-preview-panel"), $("quick-preview-primary"));
    }
  } catch (error) {
    if (state.caseId !== capturedCaseId) return;
    const reason = error.detail?.reason || error.message || "复核操作失败";
    const remediation = Array.isArray(error.detail?.remediation) ? error.detail.remediation.join("；") : "";
    state.maskEditor.externalAirFeedback = { kind: "error", message: [reason, remediation].filter(Boolean).join("。") };
    updateCtViewerGuide();
    toast(reason, true);
  } finally {
    if (button && originalText !== undefined) button.textContent = originalText;
    busy(button, false);
  }
}
$("btn-confirm-mask").addEventListener("click", (event) => {
  refreshAfterMaskReview(event.currentTarget, "/segment/confirm", "气道范围已确认，可从工作台启动自动处理");
});
$("btn-export-mask").addEventListener("click", () => {
  if (!state.caseId) return;
  if (!state.ctViewer.meta || externalAirCleanupState().pending || requiresMaskReview()) {
    toast(externalAirCleanupState().pending ? "请先去除体外空气，再确认并导出掩膜" : "请先确认气道范围，再导出掩膜", true);
    return;
  }
  window.location.href = `/api/cases/${encodeURIComponent(state.caseId)}/segment/mask-export?scope=full_candidate`;
});
$("btn-download-stl").addEventListener("click", () => { if (state.caseId) window.location.href = `/api/cases/${state.caseId}/model`; });
$("viewer-preview-mode").addEventListener("change", (event) => setSurfacePreviewMode(event.target.value));
$("case-select").addEventListener("change", (event) => {
  if (!event.target.value) return;
  selectCase(event.target.value).then((selected) => {
    if (selected === false) event.target.value = state.caseId || "";
  }).catch((e) => {
    event.target.value = state.caseId || "";
    toast(e.message, true);
  });
});

$("newcase-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!guardEndplaneDraft()) return;
  const sharedAccess = typeof window !== "undefined" && window.WindcatcherSharedAccess === true;
  const uploadFiles = sharedAccess && typeof state !== "undefined" && Array.isArray(state.uploadFiles) ? state.uploadFiles : [];
  const path = $("newcase-path").value.trim();
  if (sharedAccess && !uploadFiles.length) {
    toast("请先点击“选择 DICOM 文件夹并上传”", true);
    $("btn-pick-directory").focus();
    return;
  }
  if (!sharedAccess && !path) {
    toast("请先点击“选择 DICOM 文件夹”", true);
    $("btn-pick-directory").focus();
    return;
  }
  const button = $("btn-newcase");
  busy(button, true);
  const originalButtonText = button.textContent;
  if (sharedAccess) button.textContent = "上传中…";
  try {
    const synthetic = $("newcase-synthetic").checked;
    const anonymize = $("newcase-anonymize").checked;
    const result = sharedAccess
      ? await apiUploadCase(uploadFiles, synthetic, anonymize, (done, total) => {
        button.textContent = `上传中 ${Math.floor(done / total * 100)}%`;
        $("selected-source").textContent = `已上传 ${(done / 1024 / 1024).toFixed(1)} / ${(total / 1024 / 1024).toFixed(1)} MB`;
      })
      : await api("/api/cases", {
        method: "POST",
        body: { path, synthetic, anonymize },
      });
    $("newcase-path").value = "";
    if (sharedAccess) {
      if (typeof state !== "undefined") state.uploadFiles = [];
      $("newcase-upload").value = "";
    }
    $("newcase-synthetic").checked = false;
    $("selected-source").textContent = "正在准备病例…";
    $("selected-source").classList.remove("is-selected");
    await loadCases();
    await selectCase(result.case_id);
    showRoute("work");
    toast(`已登记病例，正在处理：${result.case_id}`);
  } catch (error) {
    toast(error.detail?.reason || error.message || "导入失败", true);
  } finally {
    if (sharedAccess) button.textContent = originalButtonText;
    busy(button, false);
  }
});

/* ---------- 启动 ---------- */
bindCtViewerControls();
bindMaskEditorControls();
bindEndplaneControls();
bindCfdResultsControls();
renderMethod();
showRoute(window.location.hash.replace("#", ""));
(async function boot() {
  try {
    window.WindcatcherAccessAdmin?.probeAdmin();
    await loadCases();
    let remembered = null;
    try { remembered = localStorage.getItem("windcatcher:last-case"); } catch { /* ignore storage policy */ }
    const active = state.cases.find((item) => item.job?.status === "running" || Object.values(item.stages || {}).includes("running"));
    const target = active?.case_id || (remembered && state.cases.some((item) => item.case_id === remembered) ? remembered : null);
    if (target) await selectCase(target);
    else renderEmptyCase();
    if (window.location.hash.replace("#", "") === "results") await loadCfdResults();
    if (window.location.hash.replace("#", "") === "cfd-review") window.WindcatcherCfdReview?.activate(state.cases, state.caseId);
    if (window.location.hash.replace("#", "") === "share") window.WindcatcherShareAdmin?.activate(state.cases, state.caseId);
  } catch (error) {
    $("case-list").innerHTML = `<p style="margin:0;color:var(--danger);font-size:12.5px;">服务连接失败：${escapeHtml(error.message)}</p>`;
  }
})();
