/* Visible, resumable batch control for existing anonymous workbench cases. */
"use strict";

(function () {
  const byId = id => document.getElementById("orchestration-" + id);
  const panel = byId("panel");
  if (!panel) return;
  const nodes = {
    load: byId("load"), current: byId("current"), all: byId("select-all"),
    start: byId("start"), cancel: byId("cancel"), useHarness: byId("use-harness"),
    retryFailed: byId("retry-failed"),
    status: byId("status"), cases: byId("cases"), results: byId("results"),
    refresh: byId("refresh"), live: byId("live"), progress: byId("progress"),
    progressLabel: byId("progress-label"), clock: byId("clock"),
    clear: byId("clear"), selectedCount: byId("selected-count"),
    casesStatus: byId("cases-status"), empty: byId("empty"),
  };
  const CASE_ID = /^case-\d{8}-\d{6}-[0-9a-f]{4}$/;
  const BATCH_ID = /^[A-Za-z0-9_-]{1,128}$/;
  const STORAGE_KEY = "windcatcher:orchestration-batch";
  const PENDING_KEY = STORAGE_KEY + ":pending";
  const STATUS = Object.freeze({ queued: "排队中", running: "处理中", solved: "求解且数值闸门通过",
    blocked: "已暂停", failed: "处理失败", cancelled: "已取消", completed: "已结束" });
  const ACTION = Object.freeze({ wait_for_review: "等待人工复核", segment_candidate: "生成掩膜候选",
    propose_endplanes: "自动生成并验证双侧三端口", review_endplanes: "局部修正三端口",
    compare_segmentation_candidates: "分割策略副本试算与对照",
    preflight: "检查病例版本与处理条件", harness_selection: "Harness 正在选择处理步骤", generate_report: "生成报告",
    prepare_surface_cfd: "建模与准备 CFD", submit_ready_cfd: "运行 CFD",
    inspect_numeric_gate: "检查数值闸门", human_review: "人工复核",
    configure_harness: "检查 Harness 配置", retry_harness: "修复 Harness 后重试",
    review_report: "复核报告", review_numeric_gate: "复核数值质量",
    review_geometry: "检查几何", review_solver: "检查求解日志",
    review_action_failure: "检查失败步骤", resubmit_current_version: "核对版本后重新提交" });
  const state = { cases: [], selected: new Set(), batchId: null, busy: false,
    running: false, timer: null, polling: false, requestKey: null, requestSignature: null,
    startedAt: null, updatedAt: null, clockTimer: null, loaded: false,
    submitting: false, openingCase: false, expanded: new Set(), loadingPromise: null };

  function setStatus(value, error) {
    nodes.status.textContent = value;
    nodes.status.classList.toggle("is-error", Boolean(error));
  }

  function updateButtons() {
    const currentId = document.getElementById("case-select")?.value;
    const pending = Boolean(state.requestKey);
    nodes.load.disabled = state.busy || state.running || pending;
    nodes.current.disabled = state.busy || state.running || pending || !state.cases.includes(currentId);
    nodes.all.disabled = state.busy || state.running || pending || state.cases.length === 0;
    nodes.clear.disabled = state.busy || state.running || pending || state.selected.size === 0;
    nodes.selectedCount.textContent = "已选 " + state.selected.size + " 例";
    nodes.start.disabled = state.busy || state.running || state.selected.size === 0;
    nodes.start.textContent = state.submitting ? "正在提交…" : state.running ? "正在处理…"
      : pending ? "重试确认提交" : state.selected.size ? "开始处理 " + state.selected.size + " 例" : "开始自动处理";
    nodes.start.setAttribute("aria-busy", String(state.submitting));
    nodes.cancel.hidden = !state.running;
    nodes.cancel.disabled = state.busy || !state.running;
    nodes.useHarness.disabled = state.busy || state.running || pending;
    nodes.retryFailed.disabled = state.busy || state.running || pending;
    nodes.refresh.hidden = !state.batchId;
    nodes.refresh.disabled = state.polling;
  }

  function updateClock() {
    if (!state.startedAt) return;
    const elapsed = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000));
    const age = state.updatedAt ? Math.max(0, Math.floor((Date.now() - state.updatedAt) / 1000)) : null;
    nodes.clock.textContent = (state.running || state.busy ? "已用时 " + elapsed + " 秒 · " : "")
      + (age === null ? "等待服务器确认提交" : "最近同步 " + age + " 秒前")
      + (state.running ? " · 自动刷新中" : "");
  }

  async function requestJson(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      return await readJson(await fetch(url, { credentials: "same-origin", cache: "no-store",
        ...options, signal: controller.signal }));
    } catch (error) {
      if (controller.signal.aborted) throw new Error("请求超时（20 秒），服务器可能仍在处理");
      throw error;
    } finally { clearTimeout(timer); }
  }

  async function readJson(response) {
    let data = null;
    try { data = await response.json(); } catch { /* Preserve HTTP status below. */ }
    if (!response.ok) {
      const reason = data?.detail?.reason || data?.detail?.message || data?.reason;
      throw new Error(typeof reason === "string" ? reason.slice(0, 240) : "请求失败（HTTP " + response.status + "）");
    }
    return data;
  }

  function drawCases() {
    nodes.cases.replaceChildren();
    for (const caseId of state.cases) {
      const label = document.createElement("label");
      const box = document.createElement("input");
      const text = document.createElement("span");
      box.type = "checkbox";
      box.id = "orchestration-case-" + caseId;
      box.value = caseId;
      box.checked = state.selected.has(caseId);
      box.disabled = state.busy || state.running || Boolean(state.requestKey);
      box.addEventListener("change", () => {
        if (state.requestKey || state.busy || state.running) {
          box.checked = state.selected.has(caseId);
          return;
        }
        if (box.checked) state.selected.add(caseId);
        else state.selected.delete(caseId);
        state.requestSignature = null;
        updateButtons();
      });
      text.textContent = caseId;
      label.append(box, text);
      nodes.cases.appendChild(label);
    }
    updateButtons();
  }

  async function loadCases() {
    // An acknowledged batch may be restored after reload; its case list is
    // still read-only while running, but must be fetched for the UI.
    if (state.busy || state.requestKey) return;
    state.busy = true;
    nodes.empty.hidden = true;
    nodes.casesStatus.textContent = "正在读取当前账号可见的已导入病例…";
    nodes.casesStatus.classList.toggle("is-error", false);
    updateButtons();
    try {
      const data = await requestJson("/api/cases", {
        credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
      });
      if (!Array.isArray(data)) throw new Error("病例列表格式无效");
      const ids = data.filter(item => item && item.synthetic !== true && CASE_ID.test(item.case_id))
        .map(item => item.case_id);
      if (new Set(ids).size !== ids.length) throw new Error("病例列表存在重复编号");
      state.cases = ids.sort();
      state.selected = new Set([...state.selected].filter(id => state.cases.includes(id)));
      state.loaded = true;
      nodes.empty.hidden = state.cases.length > 0;
      nodes.casesStatus.textContent = state.cases.length
        ? "共 " + state.cases.length + " 例已导入病例，勾选要处理的病例。" : "当前账号没有可选择的真实病例。";
      if (!state.batchId && !state.requestKey) setStatus("选好病例后开始。系统只推进当前可自动完成的步骤；遇到人工闸门会暂停并说明原因。", false);
    } catch (error) {
      state.cases = [];
      state.selected.clear();
      state.loaded = false;
      nodes.casesStatus.textContent = "读取失败：" + (error.message || "网络不可用") + "。请点击“刷新列表”重试。";
      nodes.casesStatus.classList.toggle("is-error", true);
      if (!state.batchId) setStatus(nodes.casesStatus.textContent, true);
    } finally {
      state.busy = false;
      drawCases();
      if (state.batchId) void poll();
    }
  }

  function selectOnlyCurrent() {
    const caseId = document.getElementById("case-select")?.value;
    if (!state.cases.includes(caseId) || state.busy || state.running || state.requestKey) return;
    state.selected = new Set([caseId]);
    state.requestSignature = null;
    drawCases();
    setStatus("已选当前病例 " + caseId + "。启动后会在人工闸门处暂停。", false);
  }

  function selectAll() {
    if (state.busy || state.running || state.requestKey) return;
    state.selected = new Set(state.cases);
    state.requestSignature = null;
    drawCases();
    setStatus("已选 " + state.selected.size + " 例，将按匿名编号顺序逐例推进可自动完成的步骤。", false);
  }

  function clearSelection() {
    if (state.busy || state.running || state.requestKey) return;
    state.selected.clear();
    state.requestSignature = null;
    drawCases();
  }

  function activate() {
    updateButtons();
    if (!state.loaded && !state.loadingPromise && !state.busy) {
      state.loadingPromise = loadCases().finally(() => { state.loadingPromise = null; });
    }
    return state.loadingPromise || Promise.resolve();
  }

  async function startCurrentCase(caseId) {
    if (!CASE_ID.test(caseId)) {
      setStatus("当前病例编号无效，请在工作台重新选择病例", true);
      return false;
    }
    await activate();
    if (!state.loaded || !state.cases.includes(caseId)) {
      setStatus("当前病例不在此账号可处理的列表中，请刷新列表并检查访问权限", true);
      return false;
    }
    if (state.busy || state.running) {
      setStatus("已有自动处理任务正在进行，请先查看当前进度", true);
      return false;
    }
    const signature = caseId + "|" + nodes.useHarness.checked + "|" + nodes.retryFailed.checked;
    if (state.requestKey && state.requestSignature !== signature) {
      setStatus("上一批次提交结果尚未确认，请先重试确认提交，避免重复处理", true);
      return false;
    }
    state.selected = new Set([caseId]);
    if (!state.requestKey) state.requestSignature = null;
    drawCases();
    setStatus("已选当前病例，正在启动自动处理；遇到质量闸门会暂停并说明原因。", false);
    return await start();
  }

  function addText(parent, tag, value, className) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = value;
    parent.appendChild(element);
  }

  function drawBatch(batch) {
    if (!batch || batch.batch_id !== state.batchId || !Array.isArray(batch.items))
      throw new Error("批次状态与当前任务不匹配");
    const active = ["pending", "queued", "running"].includes(batch.status);
    state.running = active;
    state.startedAt = Date.parse(batch.created_at) || state.startedAt || Date.now();
    state.updatedAt = Date.now();
    nodes.live.hidden = false;
    const done = batch.items.filter(item => ["solved", "blocked", "failed", "cancelled"].includes(item.status)).length;
    const currentIndex = batch.items.findIndex(item => item.status === "running");
    const currentAction = currentIndex >= 0 ? batch.items[currentIndex].result?.current_action : null;
    nodes.progress.max = Math.max(1, batch.items.length);
    nodes.progress.value = done;
    nodes.progressLabel.textContent = "已检查 " + done + " / " + batch.items.length + " 例"
      + (currentIndex >= 0 ? " · 正在处理第 " + (currentIndex + 1) + " 例：" + (ACTION[currentAction] || "准备检查") : "");
    if (state.clockTimer) clearInterval(state.clockTimer);
    state.clockTimer = active ? setInterval(updateClock, 1000) : null;
    updateClock();
    nodes.results.hidden = false;
    const focusedId = document.activeElement?.id;
    nodes.results.replaceChildren();
    const counts = batch.counts || {};
    const finished = Object.entries(counts).filter(([, value]) => Number.isInteger(value) && value > 0)
      .map(([key, value]) => (STATUS[key] || key) + " " + value).join(" · ");
    addText(nodes.results, "strong", (STATUS[batch.status] || batch.status) + (finished ? " · " + finished : ""));
    const list = document.createElement("ol");
    for (const item of batch.items) {
      const caseId = item?.input?.case_id;
      if (!CASE_ID.test(caseId)) continue;
      const row = document.createElement("li");
      const result = item.result || {};
      addText(row, "strong", caseId + " · " + (STATUS[item.status] || item.status || "等待中"));
      if (item.status === "running") {
        row.className = "is-running";
        addText(row, "span", "当前步骤：" + (ACTION[result.current_action] || "准备检查"));
      }
      const details = document.createElement("details");
      details.className = "agent-run-details";
      details.open = state.expanded.has(caseId);
      details.addEventListener("toggle", () => {
        if (details.isConnected === false) return;
        if (details.open) state.expanded.add(caseId); else state.expanded.delete(caseId);
      });
      addText(details, "summary", "执行详情");
      details.children[0].id = "orchestration-detail-" + caseId;
      addText(details, "small", "批次：" + batch.batch_id);
      if (Array.isArray(result.actions) && result.actions.length) {
        const steps = result.actions.map(event => {
          if (!event || !Object.hasOwn(ACTION, event.action)) return null;
          const origin = event.source === "deepseek-harness" ? "Harness" : "服务端";
          const tokens = event.metering?.total_tokens;
          const usage = event.source === "deepseek-harness"
            ? "（" + (Number.isSafeInteger(tokens) && tokens >= 0 ? tokens + " token" : "token 未返回") + "）" : "";
          return origin + "：" + ACTION[event.action] + usage;
        }).filter(Boolean);
        if (steps.length) addText(details, "span", "执行记录：" + steps.join(" → "));
      }
      const actionRecords = Array.isArray(result.actions) ? result.actions : [];
      const serverOnly = actionRecords.length > 0 && actionRecords.every(event => event?.source === "server_rules");
      const modelCallRecorded = actionRecords.some(event => event?.source === "deepseek-harness");
      if (item.status !== "running" && serverOnly) {
        addText(details, "span", "本次未调用模型。");
      } else if (item.status !== "running" && modelCallRecorded && result.llm_usage?.status === "unknown") {
        addText(details, "span", "模型 token 用量未完整返回，不能按 0 计算。");
      }
      const steps = Array.isArray(result.completed_steps) ? result.completed_steps.join(" → ") : "";
      if (steps) addText(details, "span", "已执行：" + steps);
      if (typeof result.reason === "string") addText(row, "span", (item.status === "failed" ? "失败原因：" : "暂停原因：") + result.reason);
      if (Array.isArray(result.remediation) && result.remediation.length) {
        addText(row, "span", "下一步：" + result.remediation.filter(value => typeof value === "string").join("；"));
      } else if (typeof result.next_action === "string" && result.next_action) {
        addText(row, "span", "下一步：" + (ACTION[result.next_action] || "查看病例详情"));
      }
      if (["blocked", "failed", "solved"].includes(item.status)) {
        const open = document.createElement("button");
        open.id = "orchestration-open-" + caseId;
        open.type = "button";
        open.className = "button button-quiet button-compact agent-case-open";
        open.textContent = item.status === "blocked" ? "打开病例处理" : item.status === "failed" ? "打开病例排查" : "在工作台查看";
        open.addEventListener("click", async () => {
          if (state.openingCase) return;
          state.openingCase = true;
          open.disabled = true;
          try {
            if (!globalThis.WindcatcherWorkbench?.openCase) throw new Error("工作台尚未就绪，请稍后重试");
            await globalThis.WindcatcherWorkbench.openCase(caseId);
          } catch (error) {
            setStatus("无法打开病例：" + (error.message || "请稍后重试"), true);
          } finally { state.openingCase = false; open.disabled = false; }
        });
        row.appendChild(open);
      }
      row.appendChild(details);
      list.appendChild(row);
    }
    nodes.results.appendChild(list);
    if (focusedId) document.getElementById(focusedId)?.focus?.({ preventScroll: true });
    setStatus(active ? "任务已接收，正在逐例处理；遇到人工闸门或失败时会暂停并保留原因。"
      : "本轮结束：通过 " + (counts.solved || 0) + " 例，暂停 " + (counts.blocked || 0)
        + " 例，失败 " + (counts.failed || 0) + " 例，取消 " + (counts.cancelled || 0) + " 例。", false);
    drawCases();
    return active;
  }

  async function poll() {
    if (!state.batchId || state.polling) return;
    if (state.timer) clearTimeout(state.timer);
    state.polling = true;
    updateButtons();
    const batchId = state.batchId;
    try {
      const data = await requestJson("/api/orchestration/batches/" + encodeURIComponent(batchId), {
        credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
      });
      if (batchId !== state.batchId) return;
      if (drawBatch(data)) {
        const seconds = Number.isFinite(data.poll_after_seconds) ? data.poll_after_seconds : 4;
        state.timer = setTimeout(poll, Math.max(2000, Math.min(15000, seconds * 1000)));
      }
    } catch (error) {
      if (batchId === state.batchId) {
        setStatus("暂时无法同步：" + (error.message || "网络不可用") + "。任务状态未知，正在自动重连，请勿重复启动。", true);
        state.timer = setTimeout(poll, 4000);
        drawCases();
      }
    } finally {
      state.polling = false;
      updateButtons();
    }
  }

  async function start() {
    if (state.busy || state.running || !state.selected.size) return false;
    if (!state.requestKey && !globalThis.crypto?.randomUUID) {
      setStatus("浏览器缺少安全随机数支持，不能创建可恢复批次。", true);
      return false;
    }
    const caseIds = state.cases.filter(id => state.selected.has(id));
    const useHarness = nodes.useHarness.checked;
    const retryFailed = nodes.retryFailed.checked;
    const signature = caseIds.join("|") + "|" + useHarness + "|" + retryFailed;
    if (state.requestKey && signature !== state.requestSignature) {
      setStatus("上一批次提交结果尚未确认，只能沿用原病例和设置重试", true);
      return false;
    }
    if (!state.requestKey) {
      state.requestKey = globalThis.crypto.randomUUID();
      state.requestSignature = signature;
    }
    state.busy = true;
    state.submitting = true;
    state.startedAt = Date.now();
    state.updatedAt = null;
    nodes.live.hidden = false;
    nodes.progressLabel.textContent = "正在提交 " + caseIds.length + " 例";
    nodes.progress.max = caseIds.length; nodes.progress.value = 0;
    if (state.clockTimer) clearInterval(state.clockTimer);
    state.clockTimer = setInterval(updateClock, 1000);
    updateClock();
    setStatus("正在建立任务；收到回执后显示每例进度…", false);
    drawCases();
    try {
      try { sessionStorage.setItem(PENDING_KEY, JSON.stringify({ key: state.requestKey, caseIds, useHarness, retryFailed })); } catch { /* Optional recovery storage. */ }
      const data = await requestJson("/api/orchestration/batches", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ request_key: state.requestKey, case_ids: caseIds,
          use_harness: useHarness, retry_failed: retryFailed }),
      });
      if (!BATCH_ID.test(data?.batch_id)) throw new Error("提交返回的批次编号无效");
      state.batchId = data.batch_id;
      state.expanded.clear();
      try { sessionStorage.setItem(STORAGE_KEY, data.batch_id); sessionStorage.setItem(PENDING_KEY, ""); } catch { /* Private mode may disallow storage. */ }
      state.requestKey = null;
      state.requestSignature = null;
      if (state.timer) clearTimeout(state.timer);
      state.running = true;
      state.busy = false;
      state.submitting = false;
      if (Array.isArray(data.items)) drawBatch(data);
      drawCases();
      await poll();
      return true;
    } catch (error) {
      setStatus("提交未确认：" + (error.message || "网络不可用") + "。重试会沿用相同请求编号。", true);
      if (state.clockTimer) clearInterval(state.clockTimer);
      return false;
    } finally {
      state.busy = false;
      state.submitting = false;
      drawCases();
    }
  }

  async function cancel() {
    if (!state.batchId || state.busy || !state.running) return;
    state.busy = true;
    updateButtons();
    try {
      const data = await requestJson("/api/orchestration/batches/" + encodeURIComponent(state.batchId) + "/cancel", {
        method: "POST", credentials: "same-origin", headers: { Accept: "application/json" },
      });
      if (Array.isArray(data.items)) drawBatch(data);
      if (state.timer) clearTimeout(state.timer);
      await poll();
    } catch (error) {
      setStatus("停止请求未确认：" + (error.message || "网络不可用") + "。请刷新批次状态。", true);
    } finally {
      state.busy = false;
      drawCases();
    }
  }

  nodes.load.addEventListener("click", loadCases);
  nodes.current.addEventListener("click", selectOnlyCurrent);
  nodes.all.addEventListener("click", selectAll);
  nodes.clear.addEventListener("click", clearSelection);
  nodes.start.addEventListener("click", start);
  nodes.cancel.addEventListener("click", cancel);
  nodes.refresh.addEventListener("click", poll);
  try {
    const saved = sessionStorage.getItem(STORAGE_KEY);
    const pending = JSON.parse(sessionStorage.getItem(PENDING_KEY) || "null");
    if (pending && typeof pending.key === "string" && Array.isArray(pending.caseIds)
        && pending.caseIds.length && pending.caseIds.every(id => CASE_ID.test(id))) {
      state.cases = pending.caseIds; state.selected = new Set(pending.caseIds);
      state.loaded = true;
      nodes.casesStatus.textContent = "已恢复上次提交的 " + pending.caseIds.length + " 例；请确认任务回执。";
      nodes.useHarness.checked = pending.useHarness === true;
      nodes.retryFailed.checked = pending.retryFailed === true;
      state.requestKey = pending.key;
      state.requestSignature = pending.caseIds.join("|") + "|" + nodes.useHarness.checked + "|" + nodes.retryFailed.checked;
      drawCases();
      setStatus("上次提交未收到确认。点击“重试确认提交”可继续确认同一任务，不会重复创建。", true);
    } else if (typeof saved === "string" && BATCH_ID.test(saved)) { state.batchId = saved; state.running = true; poll(); }
  } catch { /* Session storage is optional; server state remains authoritative. */ }
  updateButtons();
  globalThis.WindcatcherOrchestration = { activate, startCurrentCase };
  if (globalThis.location?.hash === "#agent") void activate();
})();
