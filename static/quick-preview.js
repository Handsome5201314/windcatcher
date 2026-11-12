"use strict";

/* Primary workbench summary. It only renders server-recorded case state and delegates actions to app.js. */
(function () {
  const $ = id => document.getElementById(id);
  const handlers = { onPrimary: null, onAdvanced: null, onResults: null, onRebuild: null };
  let bound = false;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function statusClass(status) {
    if (["ok", "skipped"].includes(status)) return "done";
    if (status === "running") return "active";
    if (["warn", "blocked"].includes(status)) return "warn";
    if (status === "failed") return "failed";
    return "pending";
  }

  function combineStatus(stages) {
    const statuses = stages.map(stage => stage?.status || "pending");
    if (statuses.includes("failed")) return "failed";
    if (statuses.includes("running")) return "running";
    if (statuses.includes("blocked")) return "blocked";
    if (statuses.includes("warn")) return "warn";
    if (statuses.length && statuses.every(status => ["ok", "skipped"].includes(status))) return "ok";
    return "pending";
  }

  function stageText(status, fallback = "未开始") {
    return ({ ok: "已完成", skipped: "已跳过", running: "进行中", warn: "待复核", blocked: "受阻", failed: "未通过" }[status] || fallback);
  }

  function stageDetails(name, stages) {
    const segment = stages.segment || {};
    const surface = stages.surface || {};
    const cfdPrep = stages.cfd_prep || {};
    const cfdRun = stages.cfd_run || {};
    if (name === "import") {
      if (stages.import?.status === "running") return "正在读取 CT";
      if (stages.import?.status === "failed" || stages.qc?.status === "failed") return "输入质控未通过";
      if (["ok", "warn"].includes(stages.qc?.status)) return "CT 与输入质控已记录";
      return "等待导入 CT";
    }
    if (name === "model") {
      if (segment.status === "failed") return "分割失败 · 查看原因";
      if (segment.status === "running") return "正在生成候选";
      if (segment.data?.need_human_confirm === true) return "主鼻腔候选已生成 · 待确认气道范围";
      if (segment.status === "ok") return "主鼻腔候选已生成";
      return "等待自动建模";
    }
    if (name === "quality") {
      if (surface.data?.verdict === "fail" || surface.status === "failed") return "几何质检未通过";
      if (surface.status === "blocked") return "几何处理已暂停 · 查看原因";
      if (surface.status === "warn") return "有警告 · 需要专家查看";
      if (surface.status === "running") return "正在执行几何检查";
      if (surface.status === "ok" && surface.data?.verdict === "pass") return "几何基础检查通过";
      return "等待模型质检";
    }
    if (name === "cfd") {
      if (cfdRun.status === "running") return "正在求解气流";
      if (["ok", "warn"].includes(cfdRun.status)) return cfdRun.status === "warn" ? "求解完成 · 待数值复核" : "求解已完成";
      if (cfdRun.status === "failed") return "气流求解失败 · 查看原因";
      if (cfdRun.status === "blocked") return "气流求解已暂停 · 查看原因";
      if (cfdPrep.status === "running") return "正在准备 CFD 算例";
      if (["ok", "warn"].includes(cfdPrep.status)) return "算例已准备 · 等待求解";
      if (["blocked", "failed"].includes(cfdPrep.status)) return "算例准备受阻";
      return "等待 CFD 算例";
    }
    if (["ok", "warn"].includes(cfdRun.status)) return cfdRun.status === "warn" ? "结果已生成 · 待复核" : "真实流场可查看";
    if (cfdRun.status === "failed") return "求解失败 · 暂无流场";
    if (cfdRun.status === "blocked") return "求解暂停 · 暂无流场";
    return "求解完成后显示";
  }

  function stageInfo(data) {
    const stages = data?.stages || {};
    const cfdStatus = combineStatus([stages.cfd_prep, stages.cfd_run]);
    const previewStatus = ["ok", "warn"].includes(stages.cfd_run?.status) ? stages.cfd_run.status : "pending";
    return [
      { key: "import", label: "1 导入 CT", status: combineStatus([stages.import, stages.qc]), detail: stageDetails("import", stages) },
      { key: "model", label: "2 自动建模", status: stages.segment?.status === "ok" && stages.segment?.data?.need_human_confirm === true ? "warn" : stages.segment?.status || "pending", detail: stageDetails("model", stages) },
      { key: "quality", label: "3 自动质检", status: stages.surface?.status || "pending", detail: stageDetails("quality", stages) },
      { key: "cfd", label: "4 CFD 求解", status: cfdStatus, detail: stageDetails("cfd", stages) },
      { key: "preview", label: "5 气流预览", status: previewStatus, detail: stageDetails("preview", stages) },
    ];
  }

  function metric(label, value, meta, tone = "") {
    return `<div class="quick-preview-metric ${tone ? `is-${tone}` : ""}"><span class="quick-preview-metric-label">${escapeHtml(label)}</span><strong class="quick-preview-metric-value">${escapeHtml(value)}</strong><span class="quick-preview-metric-meta">${escapeHtml(meta)}</span></div>`;
  }

  function recordedNumber(value) {
    if (typeof value !== "number" && typeof value !== "string") return null;
    if (typeof value === "string" && !value.trim()) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function formatNumber(value, unit = "") {
    const number = recordedNumber(value);
    if (number === null) return "未生成";
    return `${number.toFixed(number >= 100 ? 0 : 2).replace(/\.00$/, "")}${unit}`;
  }

  function recorded(value, fallback = "未记录") {
    if (value === null || value === undefined || value === "") return fallback;
    if (Array.isArray(value)) return value.length ? value.join("、") : fallback;
    if (typeof value === "object") return fallback;
    return String(value);
  }

  function evidenceRows(rows) {
    return rows.map(([label, value]) => `<div class="quick-preview-evidence-row"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(recorded(value))}</dd></div>`).join("");
  }

  function geometryBasisLabel(value) {
    if (value === "internal_bilateral_nasal_airway_with_stable_opening_planes") return "内部双侧鼻腔气道 + 稳定鼻孔/鼻咽端面";
    if (value === "internal_nasal_airway_with_stable_opening_planes") return "内部鼻腔气道 + 稳定鼻孔/鼻咽端面";
    return value;
  }

  function boundaryLayoutLabel(value) {
    if (value === "bilateral_local_v3") return "双侧鼻孔入口 + 单鼻咽出口";
    if (value === "combined_inlet_v1") return "合并入口 + 单鼻咽出口";
    return value;
  }

  function renderEvidence(data) {
    const host = $("quick-preview-evidence-content");
    if (!host) return;
    if (!data) {
      host.innerHTML = "";
      return;
    }
    const stages = data.stages || {};
    const importData = stages.import?.data || {};
    const qcData = stages.qc?.data || {};
    const segmentData = stages.segment?.data || {};
    const segmentParams = segmentData.params || {};
    const surfaceData = stages.surface?.data || {};
    const cfdPrepData = stages.cfd_prep?.data || {};
    const cfdRunData = stages.cfd_run?.data || {};
    const endplanes = surfaceData.endplanes || {};
    const cfdMeta = cfdPrepData.boundary_layout || cfdRunData.boundary_layout;
    const gate = cfdRunData.gate || cfdRunData.validation || {};
    const geometryBasis = geometryBasisLabel(cfdPrepData.geometry_basis);
    const geometryBranch = typeof cfdPrepData.external_air_domain === "object"
      ? cfdPrepData.external_air_domain?.reason
      : cfdPrepData.external_air_domain;
    const artifacts = [
      ...(stages.import?.artifacts || []),
      ...(stages.qc?.artifacts || []),
      ...(stages.segment?.artifacts || []),
      ...(stages.surface?.artifacts || []),
      ...(stages.cfd_prep?.artifacts || []),
      ...(stages.cfd_run?.artifacts || []),
    ];
    const uniqueArtifacts = [...new Set(artifacts)].slice(0, 8);
    host.innerHTML = `
      <div class="quick-preview-evidence-grid">
        <section class="quick-preview-evidence-block"><h3>输入</h3><dl>${evidenceRows([
          ["病例", data.case_id],
          ["来源", data.synthetic ? "合成测试数据" : data.source_type],
          ["体数据读取", importData.loader],
          ["体数据形状", Array.isArray(importData.shape) ? importData.shape.join(" × ") : null],
          ["层厚", qcData.slice_thickness_mm == null ? null : `${qcData.slice_thickness_mm} mm`],
          ["像素间距", Array.isArray(qcData.pixel_spacing_mm) ? `${qcData.pixel_spacing_mm.join(" × ")} mm` : null],
          ["最近更新", stages.qc?.updated_at || stages.import?.updated_at],
        ])}</dl></section>
        <section class="quick-preview-evidence-block"><h3>自动建模</h3><dl>${evidenceRows([
          ["方法", segmentData.candidate_source],
          ["规则版本", segmentData.algorithm_version],
          ["HU 区间", segmentData.hu_range || (segmentParams.hu_low != null && segmentParams.hu_high != null ? `${segmentParams.hu_low} ~ ${segmentParams.hu_high} HU` : null)],
          ["连通性", segmentParams.connectivity],
          ["掩膜版本", segmentData.mask_version],
          ["人工闸门", segmentData.need_human_confirm === true ? "待人工确认" : segmentData.need_human_confirm === false ? "已确认" : null],
          ["候选体积", segmentData.volume_ml == null ? null : `${segmentData.volume_ml} mL`],
          ["最近更新", stages.segment?.updated_at],
        ])}</dl></section>
        <section class="quick-preview-evidence-block"><h3>几何与边界</h3><dl>${evidenceRows([
          ["当前范围", surfaceData.model_scope],
          ["几何版本", surfaceData.geometry_algorithm_version],
          ["端面来源", endplanes.source],
          ["网格连通分量", surfaceData.connected_components],
          ["体积差", surfaceData.volume_deviation_percent == null ? null : `${surfaceData.volume_deviation_percent}%`],
          ["CFD 边界布局", boundaryLayoutLabel(cfdMeta)],
          ["主流程基线", geometryBasis],
          ["实验分支", geometryBranch],
          ["最近更新", stages.surface?.updated_at],
        ])}</dl></section>
        <section class="quick-preview-evidence-block"><h3>CFD 闸门</h3><dl>${evidenceRows([
          ["求解器", cfdPrepData.solver],
          ["算例模板", cfdPrepData.template_version],
          ["入口工况", cfdPrepData.inlet_mode],
          ["总流量", cfdPrepData.total_flow_lpm == null ? null : `${cfdPrepData.total_flow_lpm} L/min`],
          ["运行编号", cfdRunData.run_id],
          ["数值闸门", gate.numeric_ok === true ? "通过" : gate.numeric_ok === false ? "未通过" : null],
          ["正式资格", gate.eligible === true ? "通过" : gate.eligible === false ? "未通过" : null],
          ["最近更新", stages.cfd_run?.updated_at],
        ])}</dl></section>
      </div>
      <div class="quick-preview-evidence-footer"><span>产物索引</span><code>${escapeHtml(uniqueArtifacts.length ? uniqueArtifacts.join(" · ") : "未生成")}</code><span>所有“未记录”表示服务端尚未写入对应产物，不代表数值为 0。</span></div>`;
  }

  function render(data, options = {}) {
    const panel = $("quick-preview-panel");
    if (!panel) return;
    if (!data) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const stages = data.stages || {};
    const action = options.action || {};
    const stageHost = $("quick-preview-stages");
    if (stageHost) {
      stageHost.innerHTML = stageInfo(data).map(stage => `<div class="quick-preview-stage is-${statusClass(stage.status)}"><div class="quick-preview-stage-top"><strong>${escapeHtml(stage.label)}</strong><span class="quick-preview-stage-icon">${stage.status === "ok" ? "✓" : stage.status === "running" ? "◌" : stage.status === "failed" ? "!" : stage.status === "warn" || stage.status === "blocked" ? "!" : "○"}</span></div><span>${escapeHtml(stage.detail)} · ${escapeHtml(stageText(stage.status))}</span></div>`).join("");
    }
    const caseNode = $("quick-preview-case");
    if (caseNode) caseNode.textContent = data.case_id || "未选择病例";
    const sourceNode = $("quick-preview-source");
    if (sourceNode) sourceNode.textContent = data.synthetic ? "来源：合成测试数据 · 仅本地验证" : "来源：病例服务端产物 · 本机研究模式";

    const segmentData = stages.segment?.data || {};
    const surfaceData = stages.surface?.data || {};
    const cfdPrepData = stages.cfd_prep?.data || {};
    const cfdRunStatus = stages.cfd_run?.status || "pending";
    const inletVelocity = recordedNumber(cfdPrepData.mean_inlet_velocity_mps);
    const inletFlow = recordedNumber(cfdPrepData.total_flow_lpm);
    const metrics = $("quick-preview-metrics");
    if (metrics) {
      const qualityTone = surfaceData.verdict === "pass" ? "ok" : surfaceData.verdict === "fail" || stages.surface?.status === "failed" ? "warn" : "";
      metrics.innerHTML = [
        metric("气道体积", formatNumber(segmentData.volume_ml, " mL"), stages.segment?.data?.need_human_confirm === true ? "候选结果 · 待确认" : "来源：分割产物", stages.segment?.data?.need_human_confirm === true ? "warn" : ""),
        metric("入口工况", inletVelocity !== null ? formatNumber(inletVelocity, " m/s") : inletFlow !== null ? formatNumber(inletFlow, " L/min") : "未生成", stages.cfd_prep?.status === "ok" ? "来源：CFD 算例" : "等待 CFD 算例", ""),
        metric("掩膜-模型体积差", formatNumber(surfaceData.volume_deviation_percent, " %"), surfaceData.verdict ? `几何质检：${surfaceData.verdict}` : "等待几何质检", qualityTone),
        metric("CFD 结果", ["ok", "warn"].includes(cfdRunStatus) ? (cfdRunStatus === "warn" ? "待复核" : "已生成") : "未生成", ["ok", "warn"].includes(cfdRunStatus) ? "可进入真实流场页" : "不显示估算值", cfdRunStatus === "warn" ? "warn" : ["ok"].includes(cfdRunStatus) ? "ok" : ""),
      ].join("");
    }

    const quality = $("quick-preview-quality");
    if (quality) {
      const needsMaskReview = stages.segment?.data?.need_human_confirm === true;
      const failed = stages.surface?.status === "failed" || surfaceData.verdict === "fail";
      const blocked = stages.surface?.status === "blocked";
      const warned = stages.surface?.status === "warn";
      const tone = failed ? "failed" : blocked || needsMaskReview || warned ? "warn" : "";
      quality.className = `quick-preview-quality${tone ? ` is-${tone}` : ""}`;
      const title = failed ? "自动质检未通过" : blocked ? "几何处理已暂停" : needsMaskReview ? "建模候选等待确认" : warned ? "几何质检有警告" : stages.surface?.status === "ok" ? "自动质检已完成" : "自动质检尚未完成";
      const detail = failed || blocked || warned ? (stages.surface?.message || "请在完整工程流程中查看原因并处理") : needsMaskReview ? "自动流程在这里暂停；请对照 CT 确认当前气道范围" : stages.surface?.status === "ok" ? "连通性、几何和体积对照状态来自本地产物" : "没有通过质检前，不会把结果当作正式 CFD 入口";
      const mark = failed || blocked || needsMaskReview || warned ? "!" : stages.surface?.status === "ok" ? "✓" : "○";
      quality.innerHTML = `<div class="quick-preview-quality-main"><div class="quick-preview-quality-mark" aria-hidden="true">${mark}</div><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div></div>`;
    }

    const primary = $("quick-preview-primary");
    const rebuild = $("quick-preview-rebuild");
    const rebuildVisible = options.rebuild?.visible === true;
    if (rebuild) {
      rebuild.hidden = !rebuildVisible;
      rebuild.disabled = !rebuildVisible || options.rebuild.disabled === true;
    }
    if (primary) {
      primary.textContent = action.label || "打开完整工程流程";
      primary.disabled = action.disabled === true;
      primary.setAttribute("aria-busy", String(action.busy === true));
      primary.title = action.disabled ? "当前阶段正在处理或缺少前置条件" : "沿用工作台当前主操作，不绕过人工闸门";
    }
    const results = $("quick-preview-results");
    const resultButton = $("quick-preview-open-results");
    const resultReady = ["ok", "warn"].includes(cfdRunStatus);
    if (results) results.textContent = resultReady ? (cfdRunStatus === "warn" ? "真实 CFD 已生成 · 待数值复核" : "真实 CFD 已生成，可查看流场") : cfdRunStatus === "failed" ? "气流求解失败 · 暂无流场" : cfdRunStatus === "blocked" ? "气流求解已暂停 · 暂无流场" : "求解完成后显示真实流场";
    if (resultButton) {
      resultButton.disabled = !resultReady;
      resultButton.title = resultReady ? "打开真实病例流场" : "求解完成后可查看真实流场";
    }
    renderEvidence(data);
  }

  function openAdvanced() {
    const details = $("advanced-workflow-panel");
    if (!details) return;
    details.open = true;
    window.setTimeout(() => details.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
  }

  function bind(next = {}) {
    Object.assign(handlers, next);
    if (bound) return;
    bound = true;
    $("quick-preview-primary")?.addEventListener("click", () => handlers.onPrimary?.());
    $("quick-preview-rebuild")?.addEventListener("click", () => handlers.onRebuild?.());
    $("quick-preview-open-results")?.addEventListener("click", () => handlers.onResults?.());
    $("quick-preview-open-advanced")?.addEventListener("click", () => { openAdvanced(); handlers.onAdvanced?.(); });
  }

  window.WindcatcherQuickPreview = { bind, render, openAdvanced };
}());
