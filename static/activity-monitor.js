/* 本机操作记录：只采集控件标识和数值摘要，不读取文本、路径、凭证或影像。 */
"use strict";

(function () {
  if (window.WindcatcherMonitor || typeof window.fetch !== "function") return;
  const nativeFetch = window.fetch.bind(window);
  const routes = new Set(["work", "cases", "results", "env", "method", "sample"]);
  const numericKeys = new Set(["status_code", "duration_ms", "index", "radius", "level", "width", "anterior_index", "posterior_index", "operation_count", "point_count", "delta_x", "delta_y", "delta", "button", "value", "line", "column", "count", "controls", "client_time_ms", "sequence"]);
  const booleanKeys = new Set(["overlay", "dirty", "checked", "value"]);
  const tokenValues = {
    method: new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
    outcome: new Set(["ok", "success", "completed", "failed", "error", "blocked", "cancelled", "aborted", "pending", "running", "warn", "unknown", "invalid", "network_error"]),
    stage: new Set(["import", "anonymize", "qc", "segment", "surface", "cfd_prep", "cfd_run", "cfd", "solve", "report", "preview", "cfd_postprocess", "environment", "install", "ai_report", "selection", "endplanes", "viewer"]),
    status: new Set(["pending", "running", "ok", "warn", "failed", "blocked", "idle", "missing", "ready", "loading", "error", "saving", "cancelled", "completed", "recording", "stopped", "stale", "unavailable", "available"]),
    view: new Set(["axial", "coronal", "sagittal", "3d", "none", "x", "y", "z"]),
    tool: new Set(["paint", "erase", "translate", "rotate", "orbit", "pan", "zoom", "select", "add", "none"]),
    mode: new Set(["3d", "slices", "anterior", "posterior", "translate", "rotate", "paint", "erase", "velocity", "pressure", "none", "mask", "model", "segment", "endplanes", "full_candidate", "main_airway", "complete_candidate", "main_nasal_airway"]),
    gesture: new Set(["click", "double_click", "dblclick", "drag", "wheel", "pointer", "pinch", "pan", "zoom", "rotate", "keyboard"]),
    error_type: new Set(["Error", "TypeError", "SyntaxError", "ReferenceError", "RangeError", "URIError", "EvalError", "NetworkError", "AbortError", "SecurityError", "QuotaExceededError", "DOMException", "UnhandledRejection", "UnknownError", "TimeoutError", "ConnectionError"]),
  };
  const scriptNames = new Set(["app.js", "activity-monitor.js", "cfd-viewer.js", "three.min.js", "STLLoader.js", "OrbitControls.js", "TransformControls.js", "unknown", "inline"]);
  const kinds = new Set(["lifecycle", "ui", "navigation", "viewer", "request", "error", "stage"]);
  const queue = [];
  const inputs = new Map();
  const wheels = new Map();
  const pointers = new Map();
  const sessionId = uuid();
  let contextProvider = null;
  let flushTimer = null;
  let inputTimer = null;
  let wheelTimer = null;
  let inFlight = null;
  let failures = 0;
  let retryAt = 0;
  let dropped = 0;
  let reportedDropped = 0;
  let panelGeneration = 0;
  let panelBound = false;
  let transportFailed = false;
  let panelQueryFailed = false;
  let sessionRecorded = false;
  let sequence = 0;

  const labels = {
    kind: { lifecycle: "记录服务", ui: "操作", navigation: "页面", viewer: "视图编辑", request: "接口请求", error: "错误", stage: "处理阶段" },
    action: { session_started: "开始记录", queue_dropped: "丢弃未送达记录", click: "点击", submit: "提交", change: "调整", input: "调整数值", pointer: "模型或切片操作", wheel: "滚轮缩放或切层", route: "切换页面", page_opened: "打开页面", feedback: "操作反馈", api_outcome: "接口业务结果", response: "响应", network_error: "网络错误", javascript: "脚本错误", unhandled_rejection: "未处理的异步错误", action_started: "开始处理", action_finished: "处理完成", action_failed: "处理失败", mask_edit: "编辑掩膜", plane_selected: "选择裁切端面", plane_adjusted: "调整裁切端面", ct_view_changed: "调整 CT 视图", started: "开始处理", finished: "处理完成", failed: "处理失败" },
    route: { work: "工作台", cases: "病例", results: "病例流场", env: "环境", method: "处理流程", sample: "参考样本" },
    stage: { import: "CT 导入", anonymize: "本地去标识", qc: "输入质控", segment: "气道分割", surface: "STL 与几何质检", cfd_prep: "CFD 算例生成", cfd_run: "气流求解", cfd: "CFD", solve: "气流求解", report: "报告", preview: "模型预览", cfd_postprocess: "流场可视化", environment: "环境检查", install: "环境安装", ai_report: "AI 报告解读", selection: "选择病例", endplanes: "端面裁切", viewer: "建模视窗" },
    status: { pending: "等待处理", running: "处理中", ok: "完成", warn: "有警告", failed: "失败", blocked: "被阻断", idle: "空闲", missing: "缺失", ready: "就绪", loading: "读取中", error: "错误", saving: "保存中", cancelled: "已取消", completed: "完成", recording: "正在记录", stopped: "已停止", stale: "已失效", unavailable: "不可用", available: "可用" },
    outcome: { ok: "成功", success: "成功", completed: "完成", failed: "失败", error: "错误", blocked: "被阻断", cancelled: "已取消", aborted: "已中止", pending: "等待处理", running: "处理中", warn: "有警告", unknown: "未知", invalid: "无效", network_error: "网络错误" },
    view: { axial: "轴位", coronal: "冠状", sagittal: "矢状", "3d": "3D", none: "隐藏", x: "X 切片", y: "Y 切片", z: "Z 切片" },
    tool: { paint: "画笔", erase: "橡皮", translate: "移动切面", rotate: "旋转", orbit: "旋转视角", pan: "平移", zoom: "缩放", select: "选择", add: "添加", none: "无" },
    mode: { "3d": "3D", slices: "CT 复核", anterior: "鼻孔端", posterior: "咽喉端", translate: "移动切面", rotate: "旋转切面", paint: "画笔", erase: "橡皮", velocity: "速度", pressure: "压力", none: "隐藏", mask: "掩膜", model: "模型", segment: "气道分割", endplanes: "端面裁切", full_candidate: "完整候选", main_airway: "主气道", complete_candidate: "完整候选", main_nasal_airway: "主鼻腔气道" },
    gesture: { click: "点击", double_click: "双击", dblclick: "双击", drag: "拖动", wheel: "滚轮", pointer: "指针", pinch: "捏合缩放", pan: "平移", zoom: "缩放", rotate: "旋转", keyboard: "键盘" },
    target: { "case-select": "选择病例", "cfd-results-case": "选择流场病例", "btn-history-cases": "管理病例", "btn-view-slices": "CT 复核", "btn-view-3d": "STL 预览", "btn-edit-mask": "编辑掩膜", "btn-endplanes": "端面裁切", "btn-download-stl": "下载 STL", "mask-remove-external": "去除体外空气", "btn-confirm-mask": "确认气道范围", "btn-export-mask": "导出候选掩膜", "mask-save": "保存掩膜编辑", "mask-reset": "重置候选", "mask-undo": "撤销编辑", "mask-undo-external": "撤销体外空气清理", "mask-tool-paint": "画笔", "mask-tool-erase": "橡皮", "mask-edit-view": "编辑平面", "mask-brush-radius": "画笔半径", "mask-brush-radius-number": "画笔半径", "mask-editor": "掩膜编辑器", "viewer-wl": "窗位", "viewer-ww": "窗宽", "viewer-overlay": "候选掩膜显示", "viewer-reset": "复位视图", "viewer-preset-air": "鼻腔窗", "viewer-preset-soft": "软组织窗", "endplane-save": "保存两个裁切面", "endplane-discard": "放弃切面更改", "endplane-flip-normal": "翻转端面保留方向", "endplane-mode-anterior": "选择鼻孔端", "endplane-mode-posterior": "选择咽喉端", "endplane-tool-translate": "移动切面", "endplane-tool-rotate": "旋转切面", "endplane-reset-camera": "复位端面视角", "endplane-3d": "3D 端面视图", "btn-newcase": "导入并质控", "newcase-form": "导入病例", "btn-pick-directory": "选择 CT 文件夹", "newcase-path": "CT 目录", "btn-edit-path": "手动输入目录", "btn-refresh-cases": "刷新病例", "btn-refresh-env": "重新检测环境", "ai-report-api-key": "API 密钥输入", "ai-report-base-url": "API URL 输入", "ai-report-model": "选择报告模型", "ai-report-models-fetch": "获取模型", "ai-report-config-save": "保存报告连接", "ai-report-generate": "生成 AI 解读", "btn-cfd-results-refresh": "刷新流场", "btn-cfd-postprocess": "生成流场可视化", "btn-cfd-postprocess-cancel": "取消流场后处理", "cfd-slice-axis": "流场切片方向", "cfd-flow-canvas": "3D 流场", "cfd-play": "播放或暂停流场", "cfd-reset": "复位流场视角", "cfd-opacity": "模型不透明度", "cfd-speed": "粒子播放倍率", "cfd-density": "粒子数量", "cfd-show-streamlines": "显示流线", "cfd-show-patches": "显示入口和出口", "cfd-field-velocity": "显示速度", "cfd-field-pressure": "显示压力", "monitor-refresh": "刷新操作记录", "monitor-errors-only": "过滤错误记录", api: "本机接口", monitor: "记录队列", window: "浏览器" },
  };

  function uuid() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function token(value, limit = 96) {
    return typeof value === "string" && value.length <= limit && /^[A-Za-z0-9_.:-]+$/.test(value) ? value : "";
  }

  function route() {
    const value = window.location.hash.slice(1);
    return routes.has(value) ? value : "work";
  }

  function context() {
    let current = {};
    if (contextProvider) {
      try { current = contextProvider() || {}; }
      catch (_) { current = {}; } // Monitoring must not interrupt the workbench if its context is unavailable.
    }
    return { route: routes.has(current.route) ? current.route : route(), case_id: token(current.case_id, 128) || null };
  }

  function sourceName(value) {
    if (typeof value !== "string") return "unknown";
    const name = value.split(/[\\/]/).pop().split(/[?#]/)[0];
    return scriptNames.has(name) ? name : "unknown";
  }

  function detailsOf(details) {
    const clean = {};
    if (!details || typeof details !== "object") return clean;
    Object.keys(details).slice(0, 32).forEach((key) => {
      const value = details[key];
      if (numericKeys.has(key) && typeof value === "number" && Number.isFinite(value)) clean[key] = Math.round(Math.max(-1e13, Math.min(1e13, value)) * 1000) / 1000;
      else if (booleanKeys.has(key) && typeof value === "boolean") clean[key] = value;
      else if (tokenValues[key] && tokenValues[key].has(value)) clean[key] = value;
      else if (key === "source") clean.source = sourceName(value);
      else if (key === "controls" && Array.isArray(value) && value.length <= 16 && value.every((part) => typeof part === "number" && Number.isFinite(part) && Math.abs(part) <= 1e9)) clean.controls = value.map((part) => Math.round(part * 1000) / 1000);
    });
    return clean;
  }

  function eventOf(kind, action, details, target, requestId, current = context()) {
    return { kind: kinds.has(kind) ? kind : "lifecycle", action: token(action, 48) || "unknown", ...current, session_id: sessionId, request_id: token(requestId, 64) || uuid(), target: token(target) || null, details: { ...detailsOf(details), client_time_ms: Date.now(), sequence: ++sequence } };
  }

  function queueDropSummary() {
    if (dropped <= reportedDropped || queue.length >= 199 || queue.some((event) => event.kind === "lifecycle" && event.action === "queue_dropped")) return;
    queue.push(eventOf("lifecycle", "queue_dropped", { count: dropped }, "monitor"));
  }

  function enqueue(event) {
    if (queue.length < 200) queue.push(event);
    else dropped++;
    if (!transportFailed) queueDropSummary();
    scheduleFlush();
    renderLocalState();
  }

  function record(kind, action, details = {}, target, requestId) {
    drainInputs();
    drainWheels();
    enqueue(eventOf(kind, action, details, target, requestId));
  }

  function scheduleFlush(delay = 2500) {
    if (flushTimer !== null || !queue.length || inFlight) return;
    flushTimer = window.setTimeout(() => { flushTimer = null; void flush(); }, Math.max(delay, retryAt - Date.now()));
  }

  function renderLocalState() {
    const summary = document.getElementById("monitor-summary");
    if (summary) {
      const base = summary.dataset.monitorBase || "操作记录仅保存在本机。";
      summary.textContent = `${base} 浏览器待发送 ${queue.length} 条；本次会话丢弃 ${dropped} 条。`;
    }
    const error = document.getElementById("monitor-error");
    if (error && panelQueryFailed) {
      error.hidden = false;
      error.textContent = "读取操作记录失败。请确认风见服务已启动，然后点击刷新记录重试。";
    } else if (error && transportFailed) {
      error.hidden = false;
      error.textContent = queue.length ? "操作记录暂未送达，正在有限次重试。工作台可以继续使用；恢复服务后点击刷新记录。" : "部分操作记录未送达，重试次数已用尽。工作台可以继续使用；恢复服务后点击刷新记录。";
    } else if (error) {
      error.hidden = true;
      error.textContent = "";
    }
  }

  function drainInputs() {
    if (inputTimer !== null) window.clearTimeout(inputTimer);
    inputTimer = null;
    const pending = Array.from(inputs.values()).sort((a, b) => a.details.sequence - b.details.sequence);
    inputs.clear();
    pending.forEach(enqueue);
  }

  function drainWheels() {
    if (wheelTimer !== null) window.clearTimeout(wheelTimer);
    wheelTimer = null;
    const pending = Array.from(wheels.values());
    wheels.clear();
    pending.forEach((item) => enqueue(eventOf("viewer", "wheel", { delta: item.delta, count: item.count, duration_ms: Date.now() - item.started }, item.target, null, item.context)));
  }

  function flush(options = {}) {
    drainInputs();
    drainWheels();
    if (inFlight) return inFlight;
    if (!queue.length) return Promise.resolve(false);
    if (!options.keepalive && Date.now() < retryAt) { scheduleFlush(retryAt - Date.now()); return Promise.resolve(false); }
    if (flushTimer !== null) window.clearTimeout(flushTimer);
    flushTimer = null;
    let batch = queue.slice(0, 50);
    let body = JSON.stringify({ events: batch });
    while (body.length > 48000 && batch.length > 1) {
      batch = batch.slice(0, batch.length - 1);
      body = JSON.stringify({ events: batch });
    }
    inFlight = Promise.resolve().then(async () => {
      try {
        const response = await nativeFetch("/api/monitor/events", { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: Boolean(options.keepalive) });
        if (!response.ok) throw new Error("MonitorDeliveryError");
        queue.splice(0, batch.length);
        batch.forEach((event) => { if (event.kind === "lifecycle" && event.action === "queue_dropped") reportedDropped = Math.max(reportedDropped, event.details.count); });
        failures = 0;
        retryAt = 0;
        transportFailed = false;
        queueDropSummary();
        return true;
      } catch (_) {
        transportFailed = true;
        failures++;
        retryAt = Date.now() + Math.min(30000, 5000 * (2 ** (failures - 1)));
        if (failures >= 3) {
          queue.splice(0, batch.length);
          dropped += batch.length;
          failures = 0;
        }
        return false;
      } finally {
        inFlight = null;
        renderLocalState();
        scheduleFlush();
      }
    });
    return inFlight;
  }

  function targetOf(element) {
    if (!element || typeof element.closest !== "function") return null;
    const control = element.closest("button, input, select, textarea, form, a, [data-route], [data-stage], [data-action]");
    if (!control) return null;
    const id = token(control.id);
    const target = id || (token(control.dataset.route, 48) ? `route:${control.dataset.route}` : "") || (token(control.dataset.stage, 48) ? `stage:${control.dataset.stage}` : "") || (token(control.dataset.action, 48) ? `action:${control.dataset.action}` : "") || token(control.tagName.toLowerCase());
    return { control, target };
  }

  function numericValue(control) {
    if (control.type === "checkbox") return { checked: Boolean(control.checked) };
    if (control.type !== "range" && control.type !== "number") return {};
    if (!String(control.value).trim()) return {};
    const value = Number(control.value);
    return Number.isFinite(value) ? { value } : {};
  }

  function safeControlValues(control) {
    if (control.tagName === "SELECT") {
      const key = { "mask-edit-view": "view", "cfd-slice-axis": "view" }[control.id];
      if (key && tokenValues[key].has(control.value)) return { [key]: control.value };
      return {};
    }
    return numericValue(control);
  }

  document.addEventListener("click", (event) => {
    const target = targetOf(event.target);
    if (target) record("ui", "click", {}, target.target);
  }, true);
  document.addEventListener("submit", (event) => {
    const target = targetOf(event.target);
    if (target) record("ui", "submit", {}, target.target);
  }, true);
  document.addEventListener("change", (event) => {
    const target = targetOf(event.target);
    if (!target) return;
    const values = safeControlValues(target.control);
    // Capture context after the app handles case selection; never copy the select's value.
    window.queueMicrotask(() => record("ui", "change", values, target.target));
  }, true);
  document.addEventListener("input", (event) => {
    const target = targetOf(event.target);
    if (!target || !["range", "number"].includes(target.control.type)) return;
    const values = numericValue(target.control);
    if (!Object.keys(values).length) return;
    drainWheels();
    if (inputs.size >= 32 && !inputs.has(target.target)) drainInputs();
    const prior = inputs.get(target.target);
    inputs.set(target.target, eventOf("ui", "input", { ...values, count: prior ? prior.details.count + 1 : 1 }, target.target));
    if (inputTimer === null) inputTimer = window.setTimeout(drainInputs, 350);
  }, true);

  function canvasOf(element) {
    if (!element || element.tagName !== "CANVAS") return null;
    const host = element.parentElement && element.parentElement.closest("[id]");
    return token(element.id) || token(host && host.id) || "canvas";
  }

  document.addEventListener("pointerdown", (event) => {
    const target = canvasOf(event.target);
    if (!target || pointers.size >= 8) return;
    drainInputs();
    drainWheels();
    pointers.set(event.pointerId, { target, x: event.clientX, y: event.clientY, button: event.button, started: Date.now(), context: context() });
  }, true);
  function finishPointer(event, cancelled) {
    const pointer = pointers.get(event.pointerId);
    if (!pointer) return;
    pointers.delete(event.pointerId);
    drainWheels();
    enqueue(eventOf("viewer", "pointer", { delta_x: event.clientX - pointer.x, delta_y: event.clientY - pointer.y, duration_ms: Date.now() - pointer.started, button: pointer.button, outcome: cancelled ? "cancelled" : "completed" }, pointer.target, null, pointer.context));
  }
  document.addEventListener("pointerup", (event) => finishPointer(event, false), true);
  document.addEventListener("pointercancel", (event) => finishPointer(event, true), true);
  document.addEventListener("wheel", (event) => {
    const target = canvasOf(event.target);
    if (!target) return;
    drainInputs();
    if (wheels.size >= 8 && !wheels.has(target)) drainWheels();
    const item = wheels.get(target) || { target, delta: 0, count: 0, started: Date.now(), context: context() };
    item.delta = Math.max(-1e9, Math.min(1e9, item.delta + event.deltaY));
    item.count++;
    wheels.set(target, item);
    if (wheelTimer === null) wheelTimer = window.setTimeout(drainWheels, 350);
  }, { capture: true, passive: true });

  function errorType(error) {
    return error && tokenValues.error_type.has(error.name) ? error.name : "UnknownError";
  }
  window.addEventListener("error", (event) => record("error", "javascript", { error_type: errorType(event.error), source: sourceName(event.filename), line: event.lineno, column: event.colno }, "window"));
  window.addEventListener("unhandledrejection", (event) => record("error", "unhandled_rejection", { error_type: errorType(event.reason) }, "window"));
  window.addEventListener("hashchange", () => {
    drainInputs();
    drainWheels();
    enqueue(eventOf("navigation", "route", {}, route(), null, { ...context(), route: route() }));
    if (route() === "env") void refreshPanel();
  });
  window.addEventListener("pagehide", () => { void flush({ keepalive: true }); });

  window.fetch = function (input, init) {
    let url;
    try {
      const address = typeof input === "string" ? input : input instanceof URL ? input.href : input instanceof Request ? input.url : null;
      if (!address) return nativeFetch(input, init);
      url = new URL(address, window.location.href);
    } catch (_) { return nativeFetch(input, init); }
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/") || /^\/api\/monitor(?:\/|$)/.test(url.pathname)) return nativeFetch(input, init);
    const requestId = uuid();
    const current = context();
    let options;
    let method;
    try {
      const headers = new Headers(init && init.headers !== undefined ? init.headers : input instanceof Request ? input.headers : undefined);
      headers.set("X-Windcatcher-Session", sessionId);
      headers.set("X-Windcatcher-Request-Id", requestId);
      options = { ...init, headers };
      method = token(String((init && init.method) || (input instanceof Request && input.method) || "GET").toUpperCase(), 16) || "OTHER";
    } catch (_) { return nativeFetch(input, init); }
    const started = Date.now();
    function failed(error) {
      drainInputs();
      drainWheels();
      enqueue(eventOf("request", "network_error", { method, duration_ms: Date.now() - started, error_type: errorType(error), outcome: error && error.name === "AbortError" ? "cancelled" : "error" }, "api", requestId, current));
      throw error;
    }
    let result;
    try { result = nativeFetch(input, options); }
    catch (error) { return failed(error); }
    return result.then((response) => {
      drainInputs();
      drainWheels();
      enqueue(eventOf("request", "response", { method, status_code: response.status, duration_ms: Date.now() - started, outcome: response.ok ? "success" : "error" }, "api", requestId, current));
      return response;
    }, failed);
  };

  async function refreshPanel() {
    const list = document.getElementById("monitor-events");
    const status = document.getElementById("monitor-status");
    const error = document.getElementById("monitor-error");
    const button = document.getElementById("monitor-refresh");
    if (!list || !status) return;
    const generation = ++panelGeneration;
    const errorsOnly = document.getElementById("monitor-errors-only");
    status.textContent = "正在读取记录…";
    if (button) button.disabled = true;
    if (error) { error.hidden = true; error.textContent = ""; }
    try {
      const responses = await Promise.all([nativeFetch("/api/monitor/status"), nativeFetch(`/api/monitor/events?latest=true&limit=50&errors_only=${Boolean(errorsOnly && errorsOnly.checked)}`)]);
      if (responses.some((response) => !response.ok)) throw new Error("MonitorQueryError");
      const [health, data] = await Promise.all(responses.map((response) => response.json()));
      if (generation !== panelGeneration) return;
      if (!health || !Array.isArray(data.events) || !["ok", "running", "recording"].includes(health.status)) throw new Error("MonitorFormatError");
      panelQueryFailed = false;
      status.textContent = "正在记录";
      const summary = document.getElementById("monitor-summary");
      if (summary) summary.dataset.monitorBase = `本机共 ${Number(health.total_events) || 0} 条，保留 ${Number(health.retention_days) || 0} 天。`;
      list.replaceChildren();
      const events = data.events.slice(0, 50);
      if (!events.length) {
        const item = document.createElement("li");
        item.textContent = errorsOnly && errorsOnly.checked ? "当前没有错误记录。" : "尚无记录。继续操作工作台后，点击刷新记录。";
        list.appendChild(item);
      }
      events.forEach((event) => {
        const item = document.createElement("li");
        const time = document.createElement("time");
        const timestamp = new Date(typeof event.details?.client_time_ms === "number" ? event.details.client_time_ms : event.created_at || event.timestamp || "");
        time.textContent = Number.isFinite(timestamp.getTime()) ? timestamp.toLocaleString() : "时间未提供";
        if (Number.isFinite(timestamp.getTime())) time.dateTime = timestamp.toISOString();
        const description = document.createElement("span");
        const details = detailsOf(event.details);
        const detailText = summaryDetails(details);
        description.textContent = ` ${labels.kind[event.kind] || "事件"}：${labels.action[event.action] || token(event.action, 48) || "未知操作"} · ${targetLabel(event.target)} · ${labels.route[event.route] || "工作台"}${detailText ? ` · ${detailText}` : ""}${token(event.case_id, 128) ? ` · 病例 ${event.case_id}` : ""}${token(event.request_id, 64) ? ` · 请求 ${event.request_id}` : ""}`;
        item.append(time, description);
        list.appendChild(item);
      });
    } catch (_) {
      if (generation !== panelGeneration) return;
      panelQueryFailed = true;
      status.textContent = "记录服务暂不可查询";
      if (error) {
        error.hidden = false;
        error.textContent = "读取操作记录失败。请确认风见服务已启动，然后点击刷新记录重试。";
      }
    } finally {
      if (generation === panelGeneration && button) button.disabled = false;
      renderLocalState();
    }
  }

  function targetLabel(target) {
    if (!token(target)) return "工作台";
    if (labels.target[target]) return labels.target[target];
    if (target.startsWith("route:")) return labels.route[target.slice(6)] || "页面导航";
    if (target.startsWith("stage:")) return labels.stage[target.slice(6)] || "处理阶段";
    if (target === "endplane-model") return "3D 裁切模型";
    if (target.startsWith("ct-") && labels.view[target.slice(3)]) return `${labels.view[target.slice(3)]} CT 视图`;
    if (target.startsWith("slice-range-") || target.startsWith("slice-canvas-")) return `${labels.view[target.split("-").pop()] || "CT"}切片`;
    if (target.startsWith("endplane-anterior-")) return "鼻孔侧裁切层面";
    if (target.startsWith("endplane-posterior-")) return "咽喉侧裁切层面";
    return target;
  }

  function summaryDetails(details) {
    const parts = [];
    ["stage", "status", "outcome", "view", "tool", "mode", "gesture"].forEach((key) => { if (labels[key][details[key]]) parts.push(labels[key][details[key]]); });
    const fields = { status_code: "HTTP 状态", duration_ms: "耗时 ms", index: "层号", radius: "画笔半径", level: "窗位", width: "窗宽", anterior_index: "鼻孔侧层号", posterior_index: "咽喉侧层号", operation_count: "编辑次数", point_count: "笔迹点数", delta_x: "水平位移 px", delta_y: "垂直位移 px", delta: "滚轮增量", button: "鼠标按钮", value: "数值", line: "行", column: "列", count: "次数", controls: "视图参数" };
    Object.entries(fields).forEach(([key, name]) => { if (Object.prototype.hasOwnProperty.call(details, key)) parts.push(`${name} ${details[key]}`); });
    ["overlay", "dirty", "checked"].forEach((key) => { if (typeof details[key] === "boolean") parts.push(`${{ overlay: "掩膜", dirty: "未保存更改", checked: "勾选" }[key]}${details[key] ? "开启" : "关闭"}`); });
    if (details.method) parts.push(details.method);
    if (details.source) parts.push(`脚本 ${details.source}`);
    if (details.error_type) parts.push(details.error_type);
    return parts.join(" · ");
  }

  function bindPanel() {
    if (!sessionRecorded) {
      sessionRecorded = true;
      record("lifecycle", "session_started", { status: "recording" }, "monitor");
    }
    if (!panelBound) {
      const refresh = document.getElementById("monitor-refresh");
      const filter = document.getElementById("monitor-errors-only");
      if (refresh) refresh.addEventListener("click", () => { void refreshPanel(); });
      if (filter) filter.addEventListener("change", () => { void refreshPanel(); });
      panelBound = Boolean(refresh);
    }
    if (route() === "env") void refreshPanel();
  }

  window.WindcatcherMonitor = Object.freeze({ setContextProvider: (provider) => { contextProvider = typeof provider === "function" ? provider : null; }, record, flush, refreshPanel });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bindPanel, { once: true });
  else bindPanel();
})();
