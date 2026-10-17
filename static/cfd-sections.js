"use strict";

(function () {
  // 新结果只做整截面后处理；左右、上中下人工区域不再参与计算。
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const number = (value, digits = 2) => finite(value)
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits }) : "—";
  const clampFraction = (value) => {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? Math.max(0.05, Math.min(0.95, numeric)) : 0.5;
  };

  class CfdSections {
    constructor() {
      this.root = document.getElementById("cfd-sections");
      if (!this.root) throw new Error("分析截面界面未加载");
      this.el = {};
      for (const name of ["status", "result", "editor", "visible", "position", "position-value", "preview", "compute", "drag-hint"]) {
        this.el[name] = document.getElementById(`cfd-section-${name}`);
      }
      this.listeners = [];
      this.sequence = 0;
      this.busy = false;
      this.previewData = null;
      this.result = null;
      this.caseId = null;
      this.viewer = null;
      this.abortController = null;
      this.listen(this.el.visible, "change", () => this.viewer?.setAnalysisVisible(this.el.visible.checked));
      this.listen(this.el.position, "input", () => {
        const fraction = clampFraction(Number(this.el.position.value) / 100);
        this.el["position-value"].textContent = `${Math.round(fraction * 100)}%`;
        this.viewer?.setAnalysisPositionFraction(fraction);
        this.viewer?.setAnalysisPending(true);
        if (!this.pendingResult) this.pendingResult = this.result;
        this.result = null;
        this.renderResult();
        this.status("截面位置已调整；松开滑杆或模型上的截面后将重新计算。");
      });
      this.listen(this.el.position, "change", () => this.selectPosition(Number(this.el.position.value) / 100));
      this.listen(this.el.preview, "click", () => this.preview());
      this.listen(this.el.compute, "click", () => this.selectPosition(Number(this.el.position.value) / 100));
      this.updateControls();
    }

    listen(element, name, handler) {
      if (!element) return;
      element.addEventListener(name, handler);
      this.listeners.push([element, name, handler]);
    }

    status(message, error = false) {
      if (!this.el.status) return;
      this.el.status.textContent = message;
      this.el.status.classList.toggle("is-error", error);
    }

    async request(path, options = {}) {
      const response = await fetch(`/api/cases/${encodeURIComponent(this.caseId)}/cfd-sections${path}`, {
        ...options,
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        signal: this.abortController?.signal,
      });
      let data;
      try { data = await response.json(); } catch { throw new Error(`截面接口返回无效数据（HTTP ${response.status}），请刷新重试。`); }
      if (!response.ok) {
        const detail = data.detail;
        const message = typeof detail === "string" ? detail : detail?.message || detail?.reason;
        throw new Error(message || `截面操作未成功（HTTP ${response.status}），请检查模型后重试。`);
      }
      return data.result || data;
    }

    validate(data) {
      const mesh = data?.section_mesh;
      if (!data?.plane || !data.source_signature || !data.geometry_signature
        || !Array.isArray(mesh?.positions) || mesh.positions.length < 9 || mesh.positions.length % 3
        || !mesh.positions.every(finite) || !Array.isArray(mesh.indices) || mesh.indices.length < 3 || mesh.indices.length % 3
        || !mesh.indices.every((index) => Number.isInteger(index) && index >= 0 && index < mesh.positions.length / 3)) {
        throw new Error("截面网格或来源记录不完整，请刷新截面重试。");
      }
      if (mesh.speeds && (!Array.isArray(mesh.speeds) || mesh.speeds.length !== mesh.positions.length / 3 || !mesh.speeds.every(finite))) {
        throw new Error("截面速度数据与网格不匹配，请刷新截面重试。");
      }
    }

    setFraction(fraction) {
      const value = Math.round(clampFraction(fraction) * 100);
      this.el.position.value = String(value);
      this.el["position-value"].textContent = `${value}%`;
    }

    callbacks() {
      return {
        onPositionChange: (fraction) => {
          this.setFraction(fraction);
          this.viewer?.setAnalysisPending(true);
          if (!this.pendingResult) this.pendingResult = this.result;
          this.result = null;
          this.renderResult();
          this.status("正在拖动截面；松开鼠标后提交当前位置并重新计算。");
        },
        onPositionCommit: (fraction) => this.selectPosition(fraction),
        onPositionCancel: () => {
          this.viewer?.setAnalysisPending(false);
          this.result = this.pendingResult || null;
          if (this.previewData?.plane) this.setFraction(this.previewData.plane.position_fraction);
          this.pendingResult = null;
          this.renderResult();
          this.status("已取消移动，恢复原截面结果。");
        },
      };
    }

    async load(caseId, viewer) {
      const same = this.caseId === caseId && this.viewer === viewer;
      if (this.busy && same) return;
      if (!same) {
        this.clear();
        this.caseId = caseId;
        this.viewer = viewer;
        this.root.hidden = false;
        this.abortController = new AbortController();
      }
      const sequence = ++this.sequence;
      this.setBusy(true);
      this.status("正在读取该病例的截面统计…");
      try {
        const data = await this.request("");
        if (sequence !== this.sequence) return;
        const unavailable = data.source_status === "unavailable"
          || ("current_source_signature" in data && !data.current_source_signature)
          || ("current_geometry_signature" in data && !data.current_geometry_signature);
        if (unavailable) {
          this.result = null;
          this.previewData = null;
          this.viewer?.clearAnalysisSection();
          this.renderResult();
          this.status(`当前流场或几何来源不可用。${data.reason || "检查模型后刷新截面重试。"}`, true);
          return;
        }
        const savedFraction = data.plane?.position_fraction ?? data.position_fraction;
        if (finite(savedFraction)) this.setFraction(savedFraction);
        if (data.status === "ready" && data.analysis_type === "whole_section") {
          this.useResult(data);
          return;
        }
        this.result = null;
        this.previewData = null;
        this.pendingResult = null;
        this.viewer?.clearAnalysisSection();
        this.setBusy(false);
        await this.preview();
        if (data.status === "stale") this.status("旧截面结果已失效，已读取当前流场；拖动或确认位置后重新计算。", false);
      } catch (error) {
        if (error.name !== "AbortError" && sequence === this.sequence) this.status(error.message, true);
      } finally {
        if (sequence === this.sequence) this.setBusy(false);
      }
    }

    async preview() {
      if (!this.caseId || this.busy) return;
      const sequence = ++this.sequence;
      this.setBusy(true);
      this.status("正在从最终流场读取截面…");
      try {
        const data = await this.request("/preview", {
          method: "POST",
          body: JSON.stringify({ position_fraction: Number(this.el.position.value) / 100 }),
        });
        if (sequence !== this.sequence) return;
        this.validate(data);
        this.previewData = data;
        this.result = null;
        this.pendingResult = null;
        this.setFraction(data.plane.position_fraction);
        this.viewer?.setAnalysisSection(data, this.callbacks());
        this.viewer?.setAnalysisVisible(this.el.visible.checked);
        this.renderResult();
        this.status("截面已读取。可在三维模型上拖动橙色截面，松开鼠标后重新计算整截面气流。", false);
      } catch (error) {
        if (error.name !== "AbortError" && sequence === this.sequence) this.status(error.message, true);
      } finally {
        if (sequence === this.sequence) this.setBusy(false);
      }
    }

    async selectPosition(fraction) {
      if (!this.caseId || this.busy) return;
      const next = clampFraction(fraction);
      this.setFraction(next);
      if (!this.previewData) return this.preview();
      const sequence = ++this.sequence;
      const source = this.previewData;
      this.result = null;
      this.renderResult();
      this.setBusy(true);
      this.status("正在按选定截面重新计算流量和速度统计…");
      try {
        const result = await this.request("/analyze", {
          method: "POST",
          body: JSON.stringify({
            position_fraction: next,
            source_signature: source.source_signature,
            geometry_signature: source.geometry_signature,
          }),
        });
        if (sequence !== this.sequence) return;
        this.useResult(result);
      } catch (error) {
        if (error.name === "AbortError" || sequence !== this.sequence) return;
        this.status(error.message, true);
        if (/过期|更新|不一致|刷新/.test(error.message)) {
          this.previewData = null;
          this.pendingResult = null;
          this.setBusy(false);
          await this.preview();
        } else if (source) {
          this.viewer?.setAnalysisSection(source, this.callbacks());
          this.viewer?.setAnalysisVisible(this.el.visible.checked);
          this.result = source.analysis_type === "whole_section" ? source : null;
          this.setFraction(source.plane.position_fraction);
          this.renderResult();
          this.pendingResult = null;
          this.status(error.message, true);
        }
      } finally {
        if (sequence === this.sequence) this.setBusy(false);
      }
    }

    useResult(result) {
      this.validate(result);
      this.result = result;
      this.pendingResult = null;
      this.previewData = result;
      this.setFraction(result.plane.position_fraction);
      this.viewer?.setAnalysisSection(result, this.callbacks());
      this.viewer?.setAnalysisVisible(this.el.visible.checked);
      this.renderResult();
      this.updateControls();
      this.status("已按当前截面重新计算并保存；结果来自已有最终 CFD 速度场。", false);
    }

    addMetric(list, label, value) {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value;
      list.append(dt, dd);
    }

    renderResult() {
      if (!this.el.result) return;
      this.el.result.replaceChildren();
      if (!this.result) return;
      const result = this.result;
      const metrics = document.createElement("dl");
      metrics.className = "kv cfd-section-metrics";
      this.addMetric(metrics, "截面位置", `${number((result.plane.position_fraction || 0) * 100, 1)}%`);
      this.addMetric(metrics, "截面坐标", `${number(result.plane.position_coordinate_mm)} mm`);
      this.addMetric(metrics, "截面面积", `${number(result.totals?.area_mm2)} mm²`);
      this.addMetric(metrics, "正向 / 反向流量", `${number(result.totals?.forward_lpm)} / ${number(result.totals?.reverse_lpm)} L/min`);
      this.addMetric(metrics, "净流量", `${number(result.totals?.net_lpm)} L/min`);
      this.addMetric(metrics, "平均 / P95 / 最大流速", `${number(result.velocity?.mean_mps)} / ${number(result.velocity?.p95_mps)} / ${number(result.velocity?.max_mps)} m/s`);
      this.el.result.append(metrics);
      const note = document.createElement("p");
      note.className = "cfd-section-note";
      const reasons = result.validation?.reasons || [];
      note.textContent = `正向流量沿 CT 前→后方向统计；${reasons.length ? `待复核：${reasons.join("；")}` : "请结合网格与工程复核解读。"}`;
      this.el.result.append(note);
    }

    setBusy(busy) {
      this.busy = busy;
      this.root.setAttribute("aria-busy", String(busy));
      this.viewer?.setAnalysisBusy(busy);
      this.updateControls();
    }

    updateControls() {
      const available = Boolean(this.previewData) && !this.busy;
      if (this.el.position) this.el.position.disabled = this.busy || !this.caseId;
      if (this.el.preview) this.el.preview.disabled = this.busy || !this.caseId;
      if (this.el.compute) {
        this.el.compute.disabled = !available;
        this.el.compute.textContent = this.busy ? "处理中…" : "按当前位置重新计算";
      }
      if (this.el["drag-hint"]) this.el["drag-hint"].hidden = !this.previewData;
    }

    clear() {
      clearTimeout(this.previewTimer);
      this.abortController?.abort();
      this.sequence += 1;
      this.viewer?.clearAnalysisSection();
      this.viewer = null;
      this.caseId = null;
      this.previewData = null;
      this.result = null;
      this.pendingResult = null;
      this.busy = false;
      if (this.el.position) this.el.position.value = "50";
      if (this.el["position-value"]) this.el["position-value"].textContent = "50%";
      this.root.hidden = true;
      this.root.setAttribute("aria-busy", "false");
      this.status("");
      this.renderResult();
      this.updateControls();
    }

    dispose() {
      this.clear();
      for (const [element, name, handler] of this.listeners) element.removeEventListener(name, handler);
      this.listeners = [];
    }
  }

  window.CfdSections = CfdSections;
})();
