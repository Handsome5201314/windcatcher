"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const selectedCase = byId("cfd-review-case-id");
  let caseId = "";
  let runSignature = "";
  let currentGate = null;
  let currentRun = null;
  let requestId = 0;

  const reviewPath = id => "/api/cases/" + encodeURIComponent(id) + "/cfd-reviews";
  const gatePath = id => "/api/cases/" + encodeURIComponent(id) + "/gate";
  const line = text => {
    const p = document.createElement("p");
    p.className = "share-record";
    p.textContent = text;
    return p;
  };
  function showStatus(message, error = false) {
    const target = byId("cfd-review-status");
    target.textContent = message;
    target.classList.toggle("is-error", error);
  }
  async function request(path, options) {
    const response = await fetch(path, { cache: "no-store", ...options });
    let body;
    try { body = await response.json(); }
    catch { throw new Error("服务响应异常（HTTP " + response.status + "）"); }
    if (!response.ok) throw new Error(body.detail?.reason || body.reason || "请求失败（HTTP " + response.status + "）");
    return body;
  }
  function describeCheck(check) {
    const detail = check.detail;
    if (check.name === "残差收敛" && detail?.initial_residual && detail?.residual_control) {
      const actual = detail.initial_residual;
      const limit = detail.residual_control;
      const parts = [];
      if (Number.isFinite(actual.p)) parts.push("p " + actual.p.toExponential(2) + " / 阈值 " + limit.p);
      for (const field of ["Ux", "Uy", "Uz"]) {
        if (Number.isFinite(actual[field])) parts.push(field + " " + actual[field].toExponential(2) + " / 阈值 " + limit.U);
      }
      return parts.join("；") + (detail.reason ? "；" + detail.reason : "");
    }
    return typeof detail === "string" ? detail : detail?.reason || "";
  }
  function renderGate(gate, error) {
    currentGate = error ? null : gate;
    const node = byId("cfd-review-numeric-status");
    node.textContent = error ? "自动检查读取失败：" + error.message
      : gate?.eligible ? "全部发布条件已通过；仍需由团队核对发布对象。"
      : gate?.numeric_ok ? "数值检查通过；网格敏感性或工程复核尚未通过。"
      : "数值检查未通过，不能发布患者链接。";
    node.classList.toggle("is-error", Boolean(error) || gate?.eligible !== true);
    const checks = Array.isArray(gate?.checks) ? gate.checks : [];
    byId("cfd-review-checks").replaceChildren(...checks.map(check => line(
      (check.status === "ok" ? "通过" : "待复核") + " · " + (check.name || "检查") +
      (describeCheck(check) ? " · " + describeCheck(check) : ""))));
  }
  function renderRecords(records) {
    const container = byId("cfd-review-history");
    container.replaceChildren(...(records.length ? records.map(record => {
      const label = record.kind === "mesh_sensitivity" ? "网格证据" : "工程意见";
      const decision = record.decision === "changes_required" ? "需要调整" :
        record.decision === "reviewed" ? "已审阅，待独立确认" : "待独立核实";
      const grids = Array.isArray(record.grids) && record.grids.length ?
        " · " + record.grids.map(grid => grid.role + " " + grid.cells + " 单元 / " + grid.pressure_drop_pa + " Pa").join("；") : "";
      return line(new Date(record.created_at * 1000).toLocaleString() + " · " + label + " · " +
        decision + (record.stale ? " · 旧算例记录" : " · 当前算例") + " · " + record.reviewer +
        " · 报告 " + record.evidence_ref + grids + " · " + record.note);
    }) : [line("暂无复核记录")]));
  }
  function setFormsEnabled(enabled, copyEnabled = enabled) {
    for (const name of ["cfd-review-mesh-form", "cfd-review-engineer-form"]) {
      for (const field of byId(name).querySelectorAll("input, select, textarea, button")) field.disabled = !enabled;
    }
    byId("cfd-review-copy").disabled = !copyEnabled;
  }
  async function loadSelectedCase() {
    const selected = selectedCase.value;
    if (!selected) { showStatus("请先选择病例。", true); return false; }
    const currentRequest = ++requestId;
    caseId = selected;
    runSignature = "";
    currentRun = null;
    currentGate = null;
    setFormsEnabled(false);
    byId("cfd-review-content").hidden = true;
    showStatus("正在读取复核状态…");
    try {
      const [state, gateResult] = await Promise.all([
        request(reviewPath(selected)),
        request(gatePath(selected)).then(value => ({ value }), error => ({ error })),
      ]);
      if (currentRequest !== requestId || selected !== caseId) return false;
      currentRun = state.run;
      runSignature = state.run?.status === "ready" ? state.run.signature : "";
      byId("cfd-review-run").textContent = runSignature ?
        "当前运行 " + state.run.run_id.slice(0, 12) + " · 证据指纹 " + runSignature.slice(0, 12) :
        state.run?.reason || "尚无可复核的算例";
      renderGate(gateResult.value, gateResult.error);
      renderRecords(state.records || []);
      setFormsEnabled(Boolean(runSignature), Boolean(runSignature && currentGate));
      byId("cfd-review-content").hidden = false;
      showStatus(runSignature ? "已读取当前算例。复核记录不改变发布闸门。" : "请先完成求解，再登记本算例的复核证据。", !runSignature);
      return true;
    } catch (error) {
      if (currentRequest === requestId) showStatus("无法读取复核状态：" + error.message, true);
      return false;
    }
  }
  function syncCases(cases, currentCaseId) {
    const previous = selectedCase.value;
    selectedCase.replaceChildren(new Option("选择病例…", ""), ...(cases || []).slice().reverse().map(item =>
      new Option(item.case_id + (item.synthetic ? " · 合成" : ""), item.case_id)));
    const valid = value => cases?.some(item => item.case_id === value);
    selectedCase.value = valid(previous) ? previous : valid(currentCaseId) ? currentCaseId : "";
    if (!selectedCase.value) {
      requestId++;
      caseId = "";
      runSignature = "";
      currentRun = null;
      currentGate = null;
      setFormsEnabled(false);
      byId("cfd-review-content").hidden = true;
      showStatus(cases?.length ? "请选择病例读取复核状态。" : "暂无病例，请先导入。", false);
    }
  }
  async function save(form, payload) {
    if (!caseId || !runSignature) { showStatus("请先读取当前算例。", true); return; }
    const selected = caseId;
    const signature = runSignature;
    const currentRequest = requestId;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    showStatus("正在保存复核记录…");
    try {
      await request(reviewPath(selected), { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_signature: signature, ...payload }) });
      if (caseId !== selected || requestId !== currentRequest) return;
      const refreshed = await loadSelectedCase();
      showStatus(refreshed ? "复核记录已保存；数值检查与患者发布状态未改变。" :
        "记录已保存，但刷新失败；请重新读取当前算例。", !refreshed);
    } catch (error) { if (caseId === selected && requestId === currentRequest) showStatus(error.message, true); }
    finally { if (caseId === selected && requestId === currentRequest) button.disabled = !runSignature; }
  }
  function grid(role) {
    return { role, run_ref: byId("cfd-review-" + role + "-id").value.trim(),
      cells: Number(byId("cfd-review-" + role + "-cells").value),
      pressure_drop_pa: Number(byId("cfd-review-" + role + "-dp").value) };
  }
  byId("cfd-review-case-form").addEventListener("submit", event => { event.preventDefault(); return loadSelectedCase(); });
  selectedCase.addEventListener("change", loadSelectedCase);
  byId("cfd-review-mesh-form").addEventListener("submit", event => {
    event.preventDefault();
    return save(event.currentTarget, { kind: "mesh_sensitivity", reviewer: byId("cfd-review-mesh-reviewer").value,
      note: byId("cfd-review-mesh-note").value, evidence_ref: byId("cfd-review-mesh-evidence").value,
      grids: [grid("coarse"), grid("medium"), grid("fine")] });
  });
  byId("cfd-review-engineer-form").addEventListener("submit", event => {
    event.preventDefault();
    return save(event.currentTarget, { kind: "engineering_review", reviewer: byId("cfd-review-engineer-name").value,
      decision: byId("cfd-review-engineer-decision").value,
      evidence_ref: byId("cfd-review-engineer-evidence").value,
      note: byId("cfd-review-engineer-note").value });
  });
  byId("cfd-review-copy").addEventListener("click", async () => {
    if (!currentRun || !currentGate) { showStatus("自动检查不可用，暂不能复制摘要。", true); return; }
    const checks = Array.isArray(currentGate.checks) ? currentGate.checks : [];
    const summary = ["CFD 匿名复核摘要", "病例编号：" + caseId,
      "算例运行：" + currentRun.run_id, "证据指纹：" + currentRun.signature,
      "数值检查：" + (currentGate.numeric_ok ? "通过" : "未通过"),
      "患者发布：" + (currentGate.eligible ? "条件已满足" : "禁止"),
      ...checks.map(check => (check.name || "检查") + "：" + (check.status === "ok" ? "通过" : "待复核") +
        (describeCheck(check) ? "；" + describeCheck(check) : ""))].join("\n");
    try { await navigator.clipboard.writeText(summary); showStatus("匿名复核摘要已复制；分享前请核对内容。", false); }
    catch { showStatus("浏览器未允许复制，请使用复核页逐项核对。", true); }
  });
  window.WindcatcherCfdReview = { syncCases, activate(cases, currentCaseId) {
    syncCases(cases, currentCaseId);
    if (selectedCase.value) return loadSelectedCase();
  } };
})();
