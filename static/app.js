/* 风见工作台 · 对齐风格1概念图版式：步骤卡条 → 大数字指标 → 大视窗。全部数值来自本机真实产物。 */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  cases: [],
  caseId: null,
  case: null,
  modelScope: "complete_candidate",
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
    mode: "anterior",
    tool: "translate",
    viewer: null,
    selectionKey: null,
    selectionStatus: "idle",
    selectionError: null,
    selectionRequestId: 0,
    selectionAbort: null,
    previewStatus: "idle",
    previewData: null,
    previewError: null,
    previewSignature: null,
    previewTimer: null,
    previewRequestId: 0,
    previewAbort: null,
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
  complete_candidate: "完整候选范围",
  main_nasal_airway: "主鼻腔气流模型",
};

function modelScopeStorageKey(caseId) {
  return `windcatcher:model-scope:${caseId}`;
}

function getModelScope(caseId = state.caseId) {
  if (!caseId) return "complete_candidate";
  if (state.caseId === caseId && MODEL_SCOPE_OPTIONS[state.modelScope]) return state.modelScope;
  try {
    const saved = localStorage.getItem(modelScopeStorageKey(caseId));
    return MODEL_SCOPE_OPTIONS[saved] ? saved : "complete_candidate";
  } catch {
    return "complete_candidate";
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
  const preparedPlan = cfdEffectiveParallelPlan(data);
  const strongest = parallel.strongest ?? "自动";
  const balanced = parallel.balanced ?? "自动";
  const customMax = parallel.custom_max ?? cpu.logical_cores ?? 1;
  const customValue = config.custom_subdomains || strongest;
  const gpuText = gpu.detected ? `${gpu.name || "NVIDIA GPU"}${gpu.memory_gb ? ` · ${gpu.memory_gb} GB` : ""}` : "未检测到 NVIDIA GPU";
  const note = compute.solver_gpu_note || "标准 simpleFoam 当前只使用 CPU；GPU 不会自动参与。";
  const selectedSubdomains = config.profile === "custom" ? config.custom_subdomains : parallel[config.profile] || strongest;
  const preparedSubdomains = Number(preparedPlan?.requested_subdomains);
  const planChanged = prepared && Number.isFinite(preparedSubdomains) && Number(selectedSubdomains) !== preparedSubdomains;
  const preparedText = preparedPlan
    ? `${cfdParallelLabel(preparedPlan.profile)} · ${preparedSubdomains || "?"} 个 MPI 子域`
    : "尚未生成 CFD 算例";
  return `<div class="cfd-resource-card" aria-label="CFD 计算资源设置">
    <div class="cfd-resource-head"><div><strong>计算资源</strong><p>默认按本机物理核心选择子域数；Docker 网格生成与气流求解使用该设置；其他后端以运行记录为准。</p></div><span class="pill pill-mute">当前选择：${cfdParallelLabel(config.profile)}</span></div>
    ${prepared ? `<div class="cfd-resource-current">当前算例：${escapeHtml(preparedText)}</div>` : ""}
    <div class="cfd-resource-options" role="radiogroup" aria-label="选择计算资源模式">
      <label class="cfd-resource-option"><input type="radio" name="cfd-parallel-profile" value="strongest" ${config.profile === "strongest" ? "checked" : ""} ${locked ? "disabled" : ""}><span><b>最强</b><small>${strongest} 个 MPI 子域 · 网格与求解</small></span></label>
      <label class="cfd-resource-option"><input type="radio" name="cfd-parallel-profile" value="balanced" ${config.profile === "balanced" ? "checked" : ""} ${locked ? "disabled" : ""}><span><b>适度</b><small>${balanced} 个 MPI 子域 · 留出约 25% 余量</small></span></label>
      <label class="cfd-resource-option"><input type="radio" name="cfd-parallel-profile" value="custom" ${config.profile === "custom" ? "checked" : ""} ${locked ? "disabled" : ""}><span><b>自定义</b><small>按逻辑核心数范围选择</small></span></label>
    </div>
    <label class="cfd-resource-custom">自定义 MPI 子域数<input id="cfd-custom-subdomains" type="number" min="1" max="${customMax}" step="1" value="${customValue}" ${config.profile === "custom" && !locked ? "" : "disabled"} aria-describedby="cfd-resource-note"></label>
    <div id="cfd-resource-note" class="cfd-resource-note">检测到 CPU：${cpu.physical_cores ?? "?"} 个物理核 / ${cpu.logical_cores ?? "?"} 个逻辑核；GPU：${escapeHtml(gpuText)}。${escapeHtml(note)}</div>
    ${planChanged && !locked ? `<div class="cfd-resource-change">已选择 ${escapeHtml(cfdParallelLabel(config.profile))}（${selectedSubdomains} 个子域），需要重新生成 CFD 算例后才会生效。</div><button id="act-cfd-rebuild" class="button button-quiet">按当前选择重新生成 CFD 算例</button>` : ""}
  </div>`;
}

function endplaneContext(data = state.case) {
  if (!data || data.synthetic || data.stages?.segment?.status !== "ok" || requiresMaskReview(data.stages)) return null;
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

function updateEndplaneDirty() {
  const e = state.endplanes;
  e.dirtySides = {
    anterior: endplaneSideChanged(e.planes, e.baselinePlanes, "anterior"),
    posterior: endplaneSideChanged(e.planes, e.baselinePlanes, "posterior"),
  };
  e.dirty = e.dirtySides.anterior || e.dirtySides.posterior;
}

function endplaneBody(context = endplaneContext()) {
  const e = state.endplanes;
  if (!context || !e.planes.anterior || !e.planes.posterior) return null;
  return {
    scope: context.scope,
    mask_version: context.version,
    anterior_plane: cloneEndplane(e.planes.anterior),
    posterior_plane: cloneEndplane(e.planes.posterior),
  };
}

function endplaneSignature() {
  const body = endplaneBody();
  return body ? JSON.stringify(body) : null;
}

function clearEndplanePreview() {
  const e = state.endplanes;
  if (e.previewTimer) clearTimeout(e.previewTimer);
  e.previewTimer = null;
  e.previewRequestId += 1;
  e.previewAbort?.abort();
  e.previewAbort = null;
  e.previewStatus = "idle";
  e.previewData = null;
  e.previewError = null;
  e.previewSignature = null;
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
    if (result.scope !== body.scope || result.mask_version !== body.mask_version ||
        result.format !== "oblique_planes_v1" || result.can_save !== true ||
        !Number.isFinite(result.anterior_area_mm2) || !Number.isFinite(result.posterior_area_mm2)) {
      throw new Error("端面预检没有返回可核对的两端面积，请重新检查位置");
    }
    e.previewStatus = "ok";
    e.previewData = result;
    e.previewError = null;
    e.previewSignature = signature;
  } catch (error) {
    if (error.name === "AbortError" || e.previewRequestId !== requestId || endplaneSignature() !== signature) return;
    e.previewStatus = "blocked";
    e.previewData = null;
    e.previewError = [error.detail?.reason || error.message || "几何预检失败", ...(error.detail?.remediation || []).slice(0, 2)].join("；");
    e.previewSignature = null;
  } finally {
    if (e.previewRequestId === requestId) {
      e.previewAbort = null;
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
  const validOld = Number.isInteger(saved?.anterior_index) && Number.isInteger(saved?.posterior_index);
  return Boolean(context && Number.isInteger(context.version) && e.key === context.key && e.status === "ready" &&
    e.profile?.can_save !== false && !e.stale && saved && saved.complete !== false && !saved.validation_error && (validNew || validOld));
}

function guardEndplaneDraft() {
  const e = state.endplanes;
  if (e.saving) {
    toast("端面位置正在保存，请等待完成", true);
    return false;
  }
  if (!e.dirty) return true;
  if (!window.confirm("鼻孔侧或鼻咽侧层面尚未保存。确定放弃这次选择吗？")) return false;
  e.values = { ...e.baseline };
  e.planes = cloneEndplanePair(e.baselinePlanes);
  updateEndplaneDirty();
  e.error = null;
  e.pairError = e.profile?.saved?.validation_error?.reason || null;
  clearEndplanePreview();
  e.viewer?.setPlanes(e.planes, e.mode);
  renderEndplaneUi();
  return true;
}

function resetEndplaneState() {
  const e = state.endplanes;
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
      e.values = { anterior: null, posterior: null };
      e.baseline = { anterior: null, posterior: null };
      e.dirtySides = { anterior: false, posterior: false };
      e.planes = { anterior: null, posterior: null };
      e.baselinePlanes = { anterior: null, posterior: null };
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
    e.pairError = saved?.validation_error?.reason || null;
    if (!e.stale && saved) {
      if (saved.format === "oblique_planes_v1") {
        e.baselinePlanes = {
          anterior: normalizedEndplane(saved.anterior_plane),
          posterior: normalizedEndplane(saved.posterior_plane),
        };
      } else if (Number.isInteger(saved.anterior_index) && Number.isInteger(saved.posterior_index)) {
        e.baselinePlanes = {
          anterior: planeFromCtIndex(profile, "anterior", saved.anterior_index),
          posterior: planeFromCtIndex(profile, "posterior", saved.posterior_index),
        };
      }
      e.planes = cloneEndplanePair(e.baselinePlanes);
      for (const side of ["anterior", "posterior"]) e.values[side] = ctIndexFromPlane(profile, e.planes[side]);
      e.baseline = { ...e.values };
    }
    updateEndplaneDirty();
    if (e.open) loadEndplaneSelectionModel(context);
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
      return `${label}法向朝向模型内部。请选中“选${label}”，点击“翻转裁切法向”后再检查。`;
    }
    if (isAxialEndplane(e.profile, current, plane)) {
      const area = endplaneArea(index);
      if (area === null || area < 1) return `${label}掩膜估算面积低于 1 mm² 技术下限，请重新放置`;
    }
  }
  if (side) return null;
  const anterior = planes.anterior;
  const posterior = planes.posterior;
  if ((indices.anterior - indices.posterior) * Number(e.profile.anterior_sign) <= 0) return "鼻孔端必须位于鼻咽端前方，请分别核实并保存两端";
  return null;
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
  if (!e.open) { e.viewer?.pause(); return; }
  e.viewer?.resume();
  const context = endplaneContext();
  const feedback = $("endplane-feedback");
  const status = $("endplane-status");
  const controls = $("endplane-controls");
  let statusText = "待复核";
  let message = "请先去除体外空气、逐层复核并确认气道掩膜，然后再选择两端裁切层面。";
  if (state.case?.synthetic) message = "合成测试病例沿用已有自动端面流程；真实 CT 病例需人工选择。";
  else if (context && e.status === "loading") { statusText = "读取中"; message = "正在读取当前模型范围每一层的掩膜截面积…"; }
  else if (context && e.status === "error") { statusText = "读取失败"; message = e.error || "端面数据读取失败"; }
  else if (context && e.status === "ready") {
    const saved = e.profile?.saved;
    const savedAnterior = Boolean(saved?.anterior_plane || Number.isInteger(saved?.anterior_index));
    const savedPosterior = Boolean(saved?.posterior_plane || Number.isInteger(saved?.posterior_index));
    statusText = e.saving ? "保存中" : e.profile?.can_save === false ? "当前不可保存" : e.dirty ? "未保存" : e.stale ? "旧设置已失效" : e.pairError ? "两端有冲突" : endplaneSaved() ? "已保存" : savedAnterior || savedPosterior ? "已保存一端" : "待选择";
    message = e.error || e.selectionError || (e.profile?.can_save === false
      ? e.profile.message || "当前掩膜尚未满足端面保存条件，请先重新分割、清理体外空气并确认掩膜。"
      : e.stale
      ? "之前保存的端面属于旧掩膜版本，已失效；请在当前 CT 中重新选择两端。"
      : e.pairError
      ? `${e.pairError}；请只调整并保存冲突的那一端。`
      : e.dirty ? "所选层面尚未保存。请在 CT 中核实两端后保存。"
      : endplaneSaved() ? "当前范围的端面位置已保存；可以重新生成 STL 与几何质检。"
      : savedAnterior !== savedPosterior ? "已保存一侧端面；请继续选择并保存另一侧，完成后才会生成模型。"
      : "尚未选择裁切位置。请先在 3D 模型上点击鼻孔端和鼻咽端；选中后可移动或旋转切面，再用 CT 核对。");
  }
  feedback.textContent = message;
  feedback.classList.toggle("is-error", e.status === "error" || Boolean(e.error && e.status === "ready"));
  $("endplane-retry").hidden = !context || e.status !== "error";
  status.className = `pill ${e.status === "error" ? "pill-fail" : endplaneSaved() && !e.dirty ? "pill-ok" : "pill-warn"}`;
  status.innerHTML = `<i></i>${statusText}`;
  controls.hidden = !context || e.status !== "ready" || e.profile?.can_save === false;
  if (controls.hidden) return;
  const profile = e.profile;
  const viewLabel = CT_VIEW_LABELS[profile.view];
  $("endplane-orientation").textContent = `当前范围：${context.scope === "full_candidate" ? MODEL_SCOPE_OPTIONS.complete_candidate : MODEL_SCOPE_OPTIONS.main_nasal_airway} · ${viewLabel}共 ${profile.count} 层；层号越${Number(profile.anterior_sign) > 0 ? "大" : "小"}越靠鼻孔侧。先用 CT 看解剖位置，再用面积排除毛刺。`;
  for (const side of ["anterior", "posterior"]) {
    const button = $(`endplane-mode-${side}`);
    if (button) {
      button.classList.toggle("is-on", e.mode === side);
      button.setAttribute("aria-pressed", String(e.mode === side));
    }
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
  const placementStatus = $("endplane-placement-status");
  if (placementStatus) {
    const sideLabel = e.mode === "anterior" ? "鼻孔端" : "鼻咽端";
    placementStatus.textContent = e.planes[e.mode]
      ? `当前编辑${sideLabel}；可拖动模型上的${e.tool === "rotate" ? "旋转" : "移动"}手柄`
      : `点击 3D 气道表面放置${sideLabel}切面`;
  }
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
  const validation = endplaneValidation();
  if (e.dirty && validation && !e.error) {
    feedback.textContent = validation;
    feedback.classList.add("is-error");
  }
  for (const side of ["anterior", "posterior"]) {
    const sideValidation = endplaneValidation(side);
    const saveButton = $(`endplane-${side}-save`);
    const discardButton = $(`endplane-${side}-discard`);
    if (saveButton) saveButton.disabled = e.saving || e.profile?.can_save === false || !e.dirtySides[side] || Boolean(sideValidation);
    if (discardButton) discardButton.disabled = e.saving || !e.dirtySides[side];
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

function createEndplaneViewer(host) {
  const width = () => host.clientWidth || 640;
  const height = () => host.clientHeight || 420;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d1519);
  const camera = new THREE.PerspectiveCamera(40, width() / height(), 0.01, 10000);
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

  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = true;
  const modelGroup = new THREE.Group();
  const planeGroup = new THREE.Group();
  scene.add(modelGroup, planeGroup);
  const transform = typeof THREE.TransformControls === "function"
    ? new THREE.TransformControls(camera, renderer.domElement) : null;
  if (transform) scene.add(transform);
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const planeObjects = { anterior: null, posterior: null };
  let modelMesh = null;
  let removedMesh = null;
  let animationId = null;
  let profile = null;
  let activeSide = "anterior";
  let pointerStart = null;
  let transformDrag = false;
  let frameCenter = new THREE.Vector3();
  let frameRadius = 100;

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
    controls.target.copy(frameCenter);
    camera.position.copy(frameCenter).add(new THREE.Vector3(frameRadius * 1.35, frameRadius * 0.8, frameRadius * 1.55));
    camera.near = Math.max(0.01, frameRadius / 10000);
    camera.far = Math.max(1000, frameRadius * 30);
    camera.updateProjectionMatrix();
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

  function updateClipping() {
    if (!modelMesh) return;
    const planes = endplaneClippingPlanes(state.endplanes.planes);
    modelMesh.material.clippingPlanes = planes;
    modelMesh.material.needsUpdate = true;
    if (removedMesh) removedMesh.visible = planes.length > 0;
  }

  function syncActivePlane() {
    if (!planeObjects[activeSide]) return;
    syncPlaneFromObject(activeSide);
    updateEndplaneDraft(true);
  }

  function attachActivePlane() {
    if (!transform) return;
    const object = planeObjects[activeSide];
    if (object) {
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
    if (!modelMesh || !profile || !isAxialEndplane(profile, side, data)) return position;
    // A CT-index plane uses the CT origin as its reference point, often far
    // outside the airway. Moving its marker within the plane keeps the cut intact.
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(new THREE.Vector3(...data.normal_lps), position);
    const bounds = new THREE.Box3().setFromObject(modelMesh);
    const vertex = new THREE.Vector3();
    if (!bounds.isEmpty() && plane.intersectsBox(bounds)) plane.projectPoint(bounds.getCenter(vertex), position);
    return position;
  }

  function makePlane(side, data) {
    const color = side === "anterior" ? 0x36a9ff : 0xff9c55;
    const radius = markerRadius(data);
    const group = new THREE.Group();
    group.quaternion.setFromUnitVectors(
      new THREE.Vector3(0, 0, 1), new THREE.Vector3(...data.normal_lps).normalize(),
    );
    const disk = new THREE.Mesh(new THREE.CircleGeometry(radius, 48), new THREE.MeshBasicMaterial({
      color, transparent: true, opacity: 0.14, side: THREE.DoubleSide,
      depthWrite: false,
    }));
    disk.renderOrder = 4;

    const ringPoints = [];
    for (let index = 0; index < 48; index += 1) {
      const angle = (index / 48) * Math.PI * 2;
      ringPoints.push(new THREE.Vector3(Math.cos(angle) * radius * 1.06, Math.sin(angle) * radius * 1.06, 0));
    }
    const ring = new THREE.LineLoop(
      new THREE.BufferGeometry().setFromPoints(ringPoints),
      new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.98, depthTest: false }),
    );
    ring.renderOrder = 6;

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

    group.add(disk, ring, arrow);
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
    updateClipping();
  }

  function setActiveSide(side) {
    activeSide = side === "posterior" ? "posterior" : "anterior";
    attachActivePlane();
  }

  function handleModelClick(event) {
    if (!modelMesh || !profile || transformDrag || !pointerStart) return;
    const distance = Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y);
    if (distance > 5 || pointerStart.gizmo) return;
    updatePointer(event);
    // OrbitControls 更新相机后，点击事件可能先于下一帧渲染触发；射线检测必须使用最新世界矩阵。
    camera.updateMatrixWorld(true);
    modelMesh.updateMatrixWorld(true);
    raycaster.setFromCamera(pointer, camera);
    const clippingPlanes = endplaneClippingPlanes(state.endplanes.planes);
    const tolerance = Math.max(frameRadius * 1e-5, 0.01);
    const hit = raycaster.intersectObject(modelMesh, false).find(({ point }) =>
      clippingPlanes.every((plane) => plane.distanceToPoint(point) >= -tolerance));
    if (!hit || !hit.point) return;
    const direction = normalizedProfileDirection(profile);
    const outward = (activeSide === "anterior" ? 1 : -1) * Number(profile.anterior_sign || 1);
    const normal = direction.clone().multiplyScalar(outward > 0 ? 1 : -1);
    state.endplanes.planes[activeSide] = { point_lps_mm: hit.point.toArray(), normal_lps: normal.toArray() };
    setPlanes(state.endplanes.planes, activeSide);
    updateEndplaneDraft(true);
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
  if (transform) {
    transform.addEventListener("dragging-changed", (event) => {
      transformDrag = Boolean(event.value);
      controls.enabled = !transformDrag;
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
    setPlanes({});
  }

  function loadGeometry(geometry) {
    clear();
    geometry.computeVertexNormals();
    const material = new THREE.MeshPhysicalMaterial({
      color: 0x68c8ba, metalness: 0, roughness: 0.3,
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
  }

  function resize() {
    camera.aspect = width() / Math.max(height(), 1);
    camera.updateProjectionMatrix();
    renderer.setSize(width(), height());
  }

  function resetCamera() { frame(); }

  function animate() {
    animationId = requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }
  function pause() {
    if (animationId !== null) cancelAnimationFrame(animationId);
    animationId = null;
  }
  function resume() { if (animationId === null) animate(); }
  resume();
  window.addEventListener("resize", resize);

  return {
    clear,
    loadGeometry,
    resize,
    pause,
    resume,
    resetCamera,
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
  if (e.status !== "ready" || e.saving) return;
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
      e.profile?.can_save === false || e.saving || !e.dirtySides[side]) return;
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
    if (state.caseId !== context.caseId) return;
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
    state.case = await api(`/api/cases/${encodeURIComponent(context.caseId)}`);
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
  } catch (error) {
    if (state.caseId !== context.caseId) return;
    e.error = committed ? `位置已保存，但刷新状态失败：${error.detail?.reason || error.message}` :
      error.detail?.reason || error.message || "保存端面位置失败";
    renderEndplaneUi();
    toast(e.error, true);
  } finally {
    e.saving = false;
    renderEndplaneUi();
  }
}

async function saveEndplanes() {
  // 保留函数名供旧调用方兼容；新界面始终按当前编辑端面单独保存。
  const side = state.endplanes.mode === "posterior" ? "posterior" : "anterior";
  return saveSingleEndplane(side);
}

function discardSingleEndplane(side) {
  const e = state.endplanes;
  if (!["anterior", "posterior"].includes(side) || e.saving) return;
  e.values[side] = e.baseline[side];
  e.planes[side] = cloneEndplane(e.baselinePlanes[side]);
  e.error = null;
  e.pairError = e.profile?.saved?.validation_error?.reason || null;
  updateEndplaneDirty();
  clearEndplanePreview();
  e.viewer?.setPlanes(e.planes, e.mode);
  if (e.planes.anterior && e.planes.posterior) scheduleEndplanePreview();
  renderEndplaneUi();
}

function bindEndplaneControls() {
  $("btn-endplanes").addEventListener("click", () => {
    const e = state.endplanes;
    if (e.open) {
      if (!guardEndplaneDraft()) return;
      e.open = false;
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
  $("endplane-mode-anterior").addEventListener("click", () => {
    state.endplanes.mode = "anterior";
    state.endplanes.viewer?.setActiveSide("anterior");
    renderEndplaneUi();
  });
  $("endplane-mode-posterior").addEventListener("click", () => {
    state.endplanes.mode = "posterior";
    state.endplanes.viewer?.setActiveSide("posterior");
    renderEndplaneUi();
  });
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
    e.planes[side] = { ...plane, normal_lps: plane.normal_lps.map((value) => -value) };
    e.viewer?.setPlanes(e.planes, side);
    updateEndplaneDraft(true);
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
  const formData = typeof FormData !== "undefined" && options.body instanceof FormData;
  const response = await fetch(path, {
    headers: formData ? (options.headers || {}) : { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
    body: formData ? options.body : options.body ? JSON.stringify(options.body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  const status = payload?.status || payload?.detail?.status;
  if (["failed", "missing", "blocked"].includes(status)) {
    window.WindcatcherMonitor?.record("error", "api_outcome", { status }, "api", response.headers.get("X-Windcatcher-Request-Id"));
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : {};
   throw Object.assign(new Error([detail.reason || "请求失败", ...(detail.remediation || [])].join("\n")), { detail });
  }
 return payload;
}

async function apiUploadCase(files, synthetic, anonymize) {
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

function renderCfdResultsError(message, title = "真实病例结果读取失败") {
  const box = $("cfd-results-state");
  if (!box) return;
  box.className = "cfd-results-state is-failed";
  box.innerHTML = `<h2>${escapeHtml(title)}</h2><p>${escapeHtml(message || "服务没有返回结果")}</p>`;
  $("cfd-results-ready").hidden = true;
  $("btn-cfd-postprocess").disabled = true;
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
  box.innerHTML = `<div class="kv"><dt>数值闸门</dt><dd>${validation?.eligible ? "可进入工程复核" : "尚未整体通过，当前仅用于真实场可视化"}</dd><dt>复核说明</dt><dd>${escapeHtml(flow)}；${escapeHtml(pressure)}</dd></div>${rows ? `<ul class="cfd-check-list">${rows}</ul>` : ""}`;
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
  try {
    const result = await api(`/api/cases/${encodeURIComponent(selected)}/cfd-results`, { signal: controller.signal });
    if (!isCurrentRequest()) return;
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
        resultState.viewer = new CfdViewer($("cfd-flow-canvas"), payload, { onLegend: updateCfdLegend, onError: (message) => renderCfdResultsError(message) });
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
    renderCfdResultsError(error.detail?.reason || error.message);
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
  $("cfd-reset").addEventListener("click", () => state.cfdResults.viewer?.reset());
  $("cfd-slice-axis").addEventListener("change", (event) => state.cfdResults.viewer?.setSlice(event.target.value));
  $("cfd-opacity").addEventListener("input", (event) => { const value = Number(event.target.value) / 100; state.cfdResults.viewer?.setOpacity(value); $("cfd-opacity-value").textContent = `${event.target.value}%`; });
  $("cfd-show-streamlines").addEventListener("change", (event) => state.cfdResults.viewer?.setLines(event.target.checked));
  $("cfd-show-patches").addEventListener("change", (event) => state.cfdResults.viewer?.setPatches(event.target.checked));
  $("cfd-speed").addEventListener("input", (event) => { const value = Number(event.target.value); state.cfdResults.viewer?.setSpeed(value); $("cfd-speed-value").textContent = `${value.toFixed(2)}×`; });
  $("cfd-density").addEventListener("input", (event) => { const value = Number(event.target.value); state.cfdResults.viewer?.setDensity(value); $("cfd-density-value").textContent = `${value} / 流线`; });
}

/* ---------- 路由 ---------- */
const routes = ["work", "cases", "env", "method", "sample", "results"];
function showRoute(route) {
  if (!routes.includes(route)) route = "work";
  if (state.route !== route) {
    state.route = route;
    window.WindcatcherMonitor?.record("navigation", "page_opened", {}, route);
  }
  for (const name of routes) $("page-" + name).classList.toggle("is-visible", name === route);
  for (const button of document.querySelectorAll(".side-nav button")) {
    button.classList.toggle("is-active", button.dataset.route === route);
  }
  if (route === "env") loadEnv().catch((e) => toast(e.message, true));
  if (route === "work" && state.viewer3d) state.viewer3d.resize();
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
/* ---------- 病例列表 / 选择 ---------- */
async function loadCases() {
  state.cases = await api("/api/cases");
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
  state.viewer3d?.clear();
  state.viewer3dCaseId = null;
  state.viewer3dKey = null;
  resetEndplaneState();
  state.modelScope = "complete_candidate";
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
  let primary = null, primaryFn = null, primaryUiFn = null, primaryStage = null;
  const runningStage = STAGE_ORDER.find((name) => s[name]?.status === "running");
  const maskNeedsReview = requiresMaskReview(s);
  const requestedSurfaceScope = modelScopeParams(data.case_id).params.model_scope;
  const existingSurfaceScope = s.surface?.data?.model_scope || s.surface?.data?.scope || (s.surface?.status === "ok" ? "main_airway" : null);
  const surfaceScopeChanged = ["ok", "warn"].includes(s.surface?.status) && existingSurfaceScope && requestedSurfaceScope !== existingSurfaceScope;
  if (data.job?.status === "failed" || s.import.status === "failed" || s.qc.status === "failed") {
    primary = "导入未完成，请重新导入";
    primaryStage = "import";
  } else if (data.job?.status === "running" || runningStage) {
    const runningName = data.job?.stage && data.job.stage !== "preview" ? data.job.stage : runningStage;
    primary = runningName ? `正在${STAGE_LABELS[runningName] || runningName}` : "正在处理导入";
    primaryStage = runningName || "import";
  } else if (maskNeedsReview) {
    primary = "查看掩膜复核说明";
    primaryStage = "segment";
    primaryUiFn = () => {
      state.mode = "slices";
      refreshViewer();
      $("viewer-card").scrollIntoView({ behavior: "smooth", block: "start" });
      window.setTimeout(() => $("review-guide")?.focus?.(), 250);
    };
  } else if (["ok", "warn"].includes(s.qc.status) && ["pending", "failed"].includes(s.segment.status)) {
    primary = "开始分割"; primaryStage = "segment"; primaryFn = () => api(`/api/cases/${data.case_id}/segment`, { method: "POST", body: {} });
  } else if (!data.synthetic && s.segment.status === "ok" && !endplaneSaved()) {
    primary = state.endplanes.status === "loading" ? "正在读取端面位置…" : state.endplanes.status === "error" ? "重试读取端面位置" : "选择鼻孔与鼻咽端面";
    primaryStage = "segment";
    primaryUiFn = () => {
      if (!state.endplanes.open) $("btn-endplanes").click();
      else {
        if (state.endplanes.status === "error") loadEndplaneProfile(endplaneContext(), true);
        focusStageTarget($("endplane-panel"));
      }
    };
  } else if (["ok", "warn"].includes(s.segment.status) && (["pending", "failed"].includes(s.surface.status) || surfaceScopeChanged)) {
    primary = surfaceScopeChanged ? `按${MODEL_SCOPE_OPTIONS[getModelScope(data.case_id)] || "所选范围"}重新生成 STL` : "生成 STL 与质检";
    primaryStage = "surface"; primaryFn = () => api(`/api/cases/${data.case_id}/surface`, { method: "POST", body: modelScopeParams(data.case_id) });
  } else if (["ok", "warn"].includes(s.surface.status) && ["pending", "failed", "blocked"].includes(s.cfd_prep.status)) {
    const disconnectedFullCandidate = requestedSurfaceScope === "full_candidate" && Number(s.surface.data?.connected_components) > 1;
    if (disconnectedFullCandidate) {
      primary = "复核断开的鼻窦与旁支";
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
    primary = "开始气流求解"; primaryStage = "cfd_run"; primaryFn = () => api(`/api/cases/${data.case_id}/cfd-run`, { method: "POST", body: {} });
  } else if (s.report.status !== "ok") {
    primary = "生成报告"; primaryStage = "report"; primaryFn = () => api(`/api/cases/${data.case_id}/report`);
  } else {
    primary = "重新生成报告"; primaryStage = "report"; primaryFn = () => api(`/api/cases/${data.case_id}/report`);
  }
  const reportReady = s.report.status === "ok" && !maskNeedsReview;
  const showResourceCard = ["ok", "warn"].includes(s.surface.status) && ["pending", "failed", "blocked", "ok", "warn"].includes(s.cfd_prep.status);
  const resourceLocked = s.cfd_prep.status === "running" || s.cfd_run.status === "running";
  box.innerHTML = `${showResourceCard ? cfdResourceCard(data, { locked: resourceLocked }) : ""}
    <button id="act-primary" class="button ${primaryFn ? "button-primary" : "button-quiet"}" ${(!primaryFn && !primaryUiFn) || state.clientRunningStage === primaryStage || s[primaryStage]?.status === "running" ? "disabled" : ""}>${primary}</button>
    <button id="act-report-dl" class="button button-quiet" ${reportReady ? "" : "disabled"}>导出报告</button>`;
  box.querySelectorAll("input[name='cfd-parallel-profile']").forEach((input) => input.addEventListener("change", () => {
    const profile = input.value;
    setCfdParallelConfig(data.case_id, { profile });
    renderActions(state.case);
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
    : cfdRunStatus === "running" ? "15 L/min 工况 · 边界设定，正在求解"
    : ["ok", "warn"].includes(cfdRunStatus) ? "15 L/min 工况 · 边界设定，求解完成待复核"
    : ["failed", "blocked"].includes(cfdRunStatus) ? "15 L/min 工况 · 边界设定，求解受阻" : "15 L/min 工况 · 边界设定，尚未求解";
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
    empty: hasCase ? "未生成" : "未选择",
    status: velocityStatus,
    level: cfdPrepReady ? "warn" : "",
    stage: "cfd_prep",
    actionable: hasCase,
    hint: "来源：cfd/case_meta.json；按流量÷入口面积计算，不是患者测量值或求解场结果",
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
        message = map[name]; break;
      }
    }
  }
  $("case-message-wrap").innerHTML = `<p style="margin:10px 2px;color:var(--ink-2);font-size:13px;">${escapeHtml(message)}</p>`;
}
function stageName(name) {
  return { import: "导入", anonymize: "去标识", qc: "质控", segment: "分割", surface: "表面", cfd_prep: "算例", cfd_run: "求解", report: "报告" }[name] || name;
}

function renderCase() {
  const data = state.case;
  if (!data) return;
  renderSteps(data);
  renderActions(data);
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
        ${scopeCard("complete_candidate", "完整候选（保留鼻窦/旁支）", "先保留候选旁支，逐层复核窦口、体外空气和不确定区域。", completeSupported)}
        ${scopeCard("main_nasal_airway", "主鼻腔气流模型", "仅提交主鼻腔通道，用于鼻塞或空鼻症主气流比较。", mainSupported)}
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
    const confirmedEndplaneSources = new Set(["user_confirmed_ct_planes", "user_confirmed_oblique_planes"]);
    const legacySurface = !data.synthetic && !confirmedEndplaneSources.has(d.endplanes?.source);
    const surfacePillClass = surfaceFailed ? "pill-fail" : legacySurface || maskNeedsReview ? "pill-warn" : d.verdict === "pass" ? "pill-ok" : "pill-warn";
    const surfacePillText = surfaceFailed ? "未通过" : legacySurface ? "旧模型待重建" : maskNeedsReview ? "上游待复核" : verdictText(d.verdict);
    const volumeCheck = d.volume_check || {};
    const reasons = Array.isArray(d.reasons) ? d.reasons : [];
    const reasonBlock = reasons.length
      ? `<div class="remedy ${surfaceFailed ? "is-fail" : ""}"><p>${surfaceFailed ? "阻断原因" : "质检说明"}</p><ol>${reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ol></div>`
      : "";
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
    const executionLabel = execution.mode === "mpi" ? `MPI 并行（${execution.requested_subdomains || "?"} 个子域）` : execution.mode === "serial" ? "单进程 CPU" : "—";
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
        ["执行方式", executionLabel],
        ["计算资源", (() => { const plan = cfdEffectiveParallelPlan(data) || {}; return `${escapeHtml(cfdParallelLabel(plan.profile || "legacy"))} · ${plan.requested_subdomains || "?"} 个 MPI 子域`; })()],
        ...cfdRuntimeRows(execution),
        ["GPU", (() => { const note = cfdRunData.solver_gpu_note || s.cfd_prep.data.solver_gpu_note; return escapeHtml(note || "标准 simpleFoam 只使用 CPU"); })()],
        ...cfdMeshRows(execution),
        ["网格规模", mesh.cells ? fmt(mesh.cells, " 个单元") : "—"],
        ["气流求解", runLabel],
        ["可视化导出", exportLabel],
        ["结果复核", solved ? "网格敏感性与工程师复核待完成" : "等待求解结果"],
        ["工况", `双侧 ${fmt(s.cfd_prep.data.total_flow_lpm, " L/min") || "—"} 静息吸气`],
        ["入口面积", fmt(s.cfd_prep.data.inlet_area_mm2, " mm²")],
        ["平均入口速度", fmt(s.cfd_prep.data.mean_inlet_velocity_mps, " m/s")],
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
  fetch(`/api/cases/${encodeURIComponent(caseId)}/slice/${view}?${params}`, { signal: controller.signal })
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
    title.textContent = needsEndplanes ? "气道掩膜已确认，下一步选择两端裁切面" : "气道掩膜已确认，可以导出或继续建模";
    text.textContent = needsEndplanes
      ? "下一步请人工选择并保存鼻孔端和鼻咽端裁切面，再生成 STL 与几何质检。软件不会自动识别解剖端面；也可先导出完整候选 NRRD 保存。"
      : "当前候选掩膜已通过人工确认。可以导出完整候选 NRRD 保存，也可以生成 STL 与几何质检进入下一步。";
    status.textContent = "已确认";
    if (exportButton) exportButton.hidden = false;
    updateMaskFlow("export");
  }
}

function updateMaskEditorUi() {
  const editor = state.maskEditor;
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
  let loadRequestId = 0;

  function clear() {
    loadRequestId += 1;
    if (!currentMesh) return;
    group.remove(currentMesh);
    currentMesh.geometry.dispose();
    currentMesh.material.dispose();
    currentMesh = null;
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
    load(url, onError, onLoad) {
      clear();
      const requestId = loadRequestId;
      const loader = new THREE.STLLoader();
      loader.load(url, (geometry) => {
        if (requestId !== loadRequestId) { geometry.dispose(); return; }
        geometry.computeVertexNormals();
        const material = new THREE.MeshPhysicalMaterial({
          color: 0x7cc6b9,
          metalness: 0.0,
          roughness: 0.2,
          transmission: 0.5,
          ior: 1.32,
          clearcoat: 0.55,
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
      <div class="ai-config-status"><span>${escapeHtml(ai.reason || "未配置")}</span><small>密钥只保存在当前服务进程内存中，不写入页面、病例或配置文件。${ai.api_key_configured ? "当前服务已有可用密钥。" : "也可通过环境变量 WINDCATCHER_AI_API_KEY 提供。"}</small></div>
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
  busy(button, true);
  if (button && path === "/segment/confirm") button.textContent = "正在逐层核查 CT…";
  try {
    const result = await api(`/api/cases/${encodeURIComponent(capturedCaseId)}${path}`, { method: "POST" });
    if (state.caseId !== capturedCaseId) return;
    const selected = await selectCase(capturedCaseId);
    if (selected === false || state.caseId !== capturedCaseId) return;
    toast(result.message || successMessage);
    if (path === "/segment/confirm" && endplaneContext() && !state.endplanes.open) {
      $("btn-endplanes").click();
    }
  } catch (error) {
    toast(error.detail?.reason || error.message || "复核操作失败", true);
  } finally {
    if (button && originalText !== undefined) button.textContent = originalText;
    busy(button, false);
  }
}
$("btn-confirm-mask").addEventListener("click", (event) => {
  refreshAfterMaskReview(event.currentTarget, "/segment/confirm", "气道范围已确认，下一步请人工选择鼻孔端和鼻咽端裁切面");
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
      ? await apiUploadCase(uploadFiles, synthetic, anonymize)
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
    toast(error.detail?.reason || "导入失败", true);
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
    await loadCases();
    let remembered = null;
    try { remembered = localStorage.getItem("windcatcher:last-case"); } catch { /* ignore storage policy */ }
    const active = state.cases.find((item) => item.job?.status === "running" || Object.values(item.stages || {}).includes("running"));
    const target = active?.case_id || (remembered && state.cases.some((item) => item.case_id === remembered) ? remembered : null);
    if (target) await selectCase(target);
    else renderEmptyCase();
    if (window.location.hash.replace("#", "") === "results") await loadCfdResults();
  } catch (error) {
    $("case-list").innerHTML = `<p style="margin:0;color:var(--danger);font-size:12.5px;">服务连接失败：${escapeHtml(error.message)}</p>`;
  }
})();
