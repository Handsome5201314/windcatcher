"use strict";

(function () {
  const REGIONS = [
    { id: "left-upper", label: "左侧上部区域", side: "left", level: "upper", color: "#978de0" },
    { id: "left-middle", label: "左侧中部区域", side: "left", level: "middle", color: "#4fb5ce" },
    { id: "left-lower", label: "左侧下部区域", side: "left", level: "lower", color: "#60b992" },
    { id: "right-upper", label: "右侧上部区域", side: "right", level: "upper", color: "#d59ac4" },
    { id: "right-middle", label: "右侧中部区域", side: "right", level: "middle", color: "#e2ab66" },
    { id: "right-lower", label: "右侧下部区域", side: "right", level: "lower", color: "#cf866e" },
  ];
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const number = (value, digits = 2) => finite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits }) : "—";
  const percent = (value, incomplete = false) => finite(value) ? `${number(value, 1)}%` : (incomplete ? "待标注完整" : "占比不可用");

  function convexPolygon(points) {
    if (points.length < 3) return false;
    let sign = 0;
    for (let i = 0; i < points.length; i += 1) {
      const a = points[i], b = points[(i + 1) % points.length], c = points[(i + 2) % points.length];
      const cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]);
      if (Math.abs(cross) < 1e-8 || (sign && Math.sign(cross) !== sign)) return false;
      sign = Math.sign(cross);
    }
    // Consistent turns alone also accept a star; each edge must bound all vertices.
    return points.every((a, i) => {
      const b = points[(i + 1) % points.length];
      return points.every((p) => sign * ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) >= -1e-8);
    });
  }

  class CfdSections {
    constructor() {
      this.root = document.getElementById("cfd-sections");
      if (!this.root) throw new Error("分析截面界面未加载");
      this.el = {};
      for (const name of ["status", "regions", "result", "editor", "visible", "position", "position-value", "preview", "canvas", "selected", "draft-info", "u", "v", "add-point", "close", "undo", "clear-region", "confirm", "compute"]) this.el[name] = document.getElementById(`cfd-section-${name}`);
      this.listeners = [];
      this.sequence = 0;
      this.selected = REGIONS[0].id;
      this.polygons = new Map();
      this.draft = [];
      this.cursor = [0, 0];
      this.busy = false;
      this.dirty = false;
      this.regionButtons = new Map();
      this.buildRegionButtons();
      this.listen(this.el.visible, "change", () => this.viewer?.setAnalysisVisible(this.el.visible.checked));
      this.listen(this.el.position, "input", () => {
        this.el["position-value"].textContent = `${this.el.position.value}%`;
        this.invalidate("截面位置已修改，正在读取新的截面；原区域需重新标注。");
        this.polygons.clear(); this.draft = []; this.previewData = null;
        this.viewer?.clearAnalysisSection();
        clearTimeout(this.previewTimer);
        this.previewTimer = setTimeout(() => this.preview(), 220);
        this.draw(); this.updateControls();
      });
      this.listen(this.el.preview, "click", () => this.preview());
      this.listen(this.el.canvas, "pointerdown", (event) => {
        if (this.busy || !this.previewData || event.button !== 0) return;
        event.preventDefault(); this.el.canvas.focus();
        const rect = this.el.canvas.getBoundingClientRect();
        this.cursor = this.toUv(event.clientX - rect.left, event.clientY - rect.top);
        this.addPoint(this.cursor);
      });
      this.listen(this.el.canvas, "keydown", (event) => this.onKey(event));
      this.listen(this.el.canvas, "focus", () => this.draw());
      this.listen(this.el.canvas, "blur", () => this.draw());
      this.listen(this.el["add-point"], "click", () => {
        if (!this.el.u.value.trim() || !this.el.v.value.trim()) return this.status("请输入两个有限的截面坐标。", true);
        const point = [Number(this.el.u.value), Number(this.el.v.value)];
        if (!point.every(finite)) return this.status("请输入两个有限的截面坐标。", true);
        this.cursor = point; this.addPoint(point);
      });
      this.listen(this.el.close, "click", () => this.closePolygon());
      this.listen(this.el.undo, "click", () => { this.draft.pop(); this.draw(); this.updateControls(); });
      this.listen(this.el["clear-region"], "click", () => {
        this.polygons.delete(this.selected); this.draft = [];
        this.invalidate("区域已清除，需重新计算分布。"); this.draw(); this.updateControls();
      });
      this.listen(this.el.confirm, "change", () => this.updateControls());
      this.listen(this.el.compute, "click", () => this.compute());
      this.resizeObserver = new ResizeObserver(() => this.draw());
      this.resizeObserver.observe(this.el.canvas);
      this.updateControls();
    }

    listen(element, name, handler) { element.addEventListener(name, handler); this.listeners.push([element, name, handler]); }
    hasDraft() { return this.dirty || this.draft.length > 0; }
    status(message, error = false) { this.el.status.textContent = message; this.el.status.classList.toggle("is-error", error); }

    buildRegionButtons() {
      for (const side of ["right", "left"]) {
        const group = document.createElement("div");
        const heading = document.createElement("strong");
        heading.textContent = `患者${side === "left" ? "左" : "右"}侧`;
        group.append(heading);
        for (const region of REGIONS.filter((item) => item.side === side)) {
          const button = document.createElement("button");
          button.type = "button"; button.className = "cfd-region-button"; button.dataset.region = region.id;
          button.setAttribute("aria-pressed", String(region.id === this.selected));
          const swatch = document.createElement("i"); swatch.style.backgroundColor = region.color; swatch.setAttribute("aria-hidden", "true");
          const label = document.createElement("span"); label.textContent = region.label;
          const value = document.createElement("span"); value.className = "cfd-region-value"; value.textContent = "未标注";
          button.append(swatch, label, value); group.append(button);
          this.regionButtons.set(region.id, { button, value });
          this.listen(button, "click", () => this.selectRegion(region.id));
        }
        this.el.regions.append(group);
      }
      const button = document.createElement("button");
      button.type = "button"; button.className = "cfd-region-button cfd-region-unassigned";
      button.setAttribute("aria-pressed", "false");
      const swatch = document.createElement("i"); swatch.style.backgroundColor = "#b5c7c5"; swatch.setAttribute("aria-hidden", "true");
      const label = document.createElement("span"); label.textContent = "未标注区域";
      const value = document.createElement("span"); value.className = "cfd-region-value"; value.textContent = "待计算";
      button.append(swatch, label, value); this.el.regions.append(button);
      this.regionButtons.set("unassigned", { button, value });
      this.listen(button, "click", () => this.selectRegion("unassigned"));
    }

    selectRegion(id) {
      if (!this.regionButtons.has(id) || this.busy) return;
      if (this.selected !== id && this.draft.length) return this.status("当前区域还有未闭合的顶点，请先闭合或撤销顶点后再切换。", true);
      this.selected = id;
      this.viewer?.selectAnalysisRegion(id);
      this.el.selected.textContent = id === "unassigned" ? "未标注区域（请选择上／中／下区域进行标注）" : REGIONS.find((item) => item.id === id).label;
      for (const [key, { button }] of this.regionButtons) button.setAttribute("aria-pressed", String(key === id));
      this.renderResult(); this.draw(); this.updateControls();
    }

    async request(path, options = {}) {
      const response = await fetch(`/api/cases/${encodeURIComponent(this.caseId)}/cfd-sections${path}`, { ...options, headers: { "Content-Type": "application/json" }, signal: this.abortController.signal });
      let data;
      try { data = await response.json(); } catch { throw new Error(`截面接口返回无效数据（HTTP ${response.status}），请刷新重试。`); }
      if (!response.ok) {
        const message = typeof data.detail === "string" ? data.detail : data.detail?.message || data.detail?.reason;
        throw new Error(message || `截面操作未成功（HTTP ${response.status}），请检查标注并重试。`);
      }
      return data.result || data;
    }

    async load(caseId, viewer) {
      const same = this.caseId === caseId && this.viewer === viewer;
      if (this.busy && same) return;
      if (!same) {
        this.clear(); this.caseId = caseId; this.viewer = viewer;
        this.root.hidden = false; this.el.editor.open = true;
        this.abortController = new AbortController();
        this.status("正在读取该病例的截面统计…");
      }
      const sequence = ++this.sequence;
      this.setBusy(true);
      try {
        const data = await this.request("");
        if (sequence !== this.sequence) return;
        const currentSource = data.current_source_signature || data.source_signature;
        const currentGeometry = data.current_geometry_signature || data.geometry_signature;
        const unavailable = data.source_status === "unavailable" || data.status === "error" || (("current_source_signature" in data) && !data.current_source_signature) || (("current_geometry_signature" in data) && !data.current_geometry_signature);
        if (unavailable) {
          this.result = null; this.previewData = null; this.polygons.clear(); this.draft = []; this.el.confirm.checked = false; this.dirty = false;
          this.viewer?.clearAnalysisSection(); this.renderResult(); this.draw(); this.el.editor.open = true;
          this.status(`当前流场或几何来源不可用，旧标注已停用。${data.reason || "检查模型后刷新截面重试。"}`, true); return;
        }
        const changed = data.status === "stale" || (this.previewData && ((currentSource && currentSource !== this.previewData.source_signature) || (currentGeometry && currentGeometry !== this.previewData.geometry_signature)));
        if (same && this.previewData && !changed && this.hasDraft()) {
          this.status("保留当前未保存的标注；完成后计算并保存。"); return;
        }
        if (same && this.result && !changed && data.status === "ready" && data.analysis_signature === this.result.analysis_signature) return;
        if (same && this.previewData && !changed && data.status !== "ready") return;
        if (data.status === "ready" && data.annotation && !changed) this.useResult(data);
        else {
          this.result = null; this.polygons.clear(); this.draft = []; this.previewData = null;
          this.viewer?.clearAnalysisSection(); this.renderResult();
          this.el.editor.open = true;
          this.status(data.status === "stale" || changed ? "流场或网格已更新，旧统计已失效；请重新核对截面并标注。" : "尚无截面统计。选择截面并标注区域后即可计算。");
          this.setBusy(false);
          await this.preview();
        }
      } catch (error) { if (error.name !== "AbortError" && sequence === this.sequence) this.status(error.message, true); }
      finally { if (sequence === this.sequence) this.setBusy(false); }
    }

    async preview() {
      if (!this.caseId || this.busy) return;
      const sequence = ++this.sequence;
      this.setBusy(true); this.status("正在从真实流场网格读取截面…");
      try {
        const data = await this.request("/preview", { method: "POST", body: JSON.stringify({ position_fraction: Number(this.el.position.value) / 100 }) });
        if (sequence !== this.sequence) return;
        this.validatePreview(data);
        const samePlane = this.previewData && this.previewData.source_signature === data.source_signature && this.previewData.geometry_signature === data.geometry_signature && JSON.stringify(this.previewData.plane) === JSON.stringify(data.plane);
        if (!samePlane) { this.polygons.clear(); this.draft = []; this.result = null; this.el.confirm.checked = false; this.dirty = true; }
        this.previewData = data;
        this.viewer?.setAnalysisSection(samePlane && this.result ? this.result : data, (id) => this.selectRegion(id));
        this.viewer?.setAnalysisVisible(this.el.visible.checked);
        this.cursor = [0, 0]; this.syncCursor(); this.renderResult(); this.draw();
        this.status("截面已读取。请根据解剖边界标注，未覆盖的气流会单独显示。");
      } catch (error) { if (error.name !== "AbortError" && sequence === this.sequence) this.status(error.message, true); }
      finally { if (sequence === this.sequence) this.setBusy(false); }
    }

    validatePreview(data) {
      const mesh = data.section_mesh;
      if (!data.plane || !data.source_signature || !data.geometry_signature || !Array.isArray(mesh?.uv_mm) || mesh.uv_mm.length < 6 || !mesh.uv_mm.every(finite) || mesh.uv_mm.length % 2 || !Array.isArray(mesh.indices) || mesh.indices.length < 3 || mesh.indices.length % 3 || !mesh.indices.every((index) => Number.isInteger(index) && index >= 0 && index < mesh.uv_mm.length / 2)) throw new Error("截面网格或来源记录不完整，请刷新截面重试。");
    }

    invalidate(message) {
      this.dirty = true; this.result = null; this.el.confirm.checked = false;
      if (this.previewData) this.viewer?.setAnalysisSection({ section_mesh: this.previewData.section_mesh });
      this.renderResult(); this.status(message);
    }

    addPoint(point) {
      if (!this.previewData || this.busy || this.selected === "unassigned") return;
      if (this.draft.length >= 64) return this.status("单一区域最多 64 个顶点，请闭合现有边界或撤销后调整。", true);
      if (this.draft.some((p) => Math.hypot(p[0] - point[0], p[1] - point[1]) < 0.01)) return this.status("这个顶点已存在，请选择不同的坐标。", true);
      this.invalidate("标注正在编辑，原统计已失效。闭合区域后重新计算。");
      this.draft.push([...point]); this.syncCursor(); this.draw(); this.updateControls();
    }

    closePolygon() {
      if (!convexPolygon(this.draft)) return this.status("边界必须是无交叉的凸多边形，请依次沿边界添加顶点；可撤销后调整。", true);
      this.polygons.set(this.selected, this.draft.map((point) => [...point])); this.draft = [];
      this.status("区域已标注。继续标注其余区域，核对后计算分布。"); this.draw(); this.updateControls();
    }

    onKey(event) {
      if (!this.previewData || this.busy) return;
      const step = event.shiftKey ? 5 : 1;
      const movement = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] }[event.key];
      if (movement) { event.preventDefault(); this.cursor = this.cursor.map((value, i) => value + movement[i]); this.syncCursor(); this.draw(); }
      else if (event.key === "Enter") { event.preventDefault(); this.addPoint(this.cursor); }
      else if (event.key === "Backspace") { event.preventDefault(); this.draft.pop(); this.draw(); this.updateControls(); }
      else if (event.key === "Escape") { event.preventDefault(); this.draft = []; this.draw(); this.updateControls(); }
    }

    syncCursor() { this.el.u.value = number(this.cursor[0], 1).replace(/,/g, ""); this.el.v.value = number(this.cursor[1], 1).replace(/,/g, ""); }

    async compute() {
      if (!this.previewData || !this.polygons.size || this.draft.length || !this.el.confirm.checked || this.busy) return;
      const sequence = ++this.sequence;
      const data = this.previewData;
      const annotation = { plane: data.plane, regions: REGIONS.filter((region) => this.polygons.has(region.id)).map(({ color, ...region }) => ({ ...region, polygon_uv_mm: this.polygons.get(region.id) })), confirmed: true, source_signature: data.source_signature, geometry_signature: data.geometry_signature };
      this.setBusy(true); this.status("正在积分真实速度场并保存分布…");
      try {
        const result = await this.request("", { method: "POST", body: JSON.stringify(annotation) });
        if (sequence !== this.sequence) return;
        if (result.status !== "ready") throw new Error("统计尚未完成，请检查截面来源后重试。");
        if (!result.annotation) result.annotation = annotation;
        this.useResult(result);
      } catch (error) { if (error.name !== "AbortError" && sequence === this.sequence) this.status(error.message, true); }
      finally { if (sequence === this.sequence) this.setBusy(false); }
    }

    useResult(result) {
      this.validatePreview(result);
      this.result = result; this.previewData = result; this.dirty = false; this.draft = [];
      this.polygons = new Map((result.annotation.regions || []).map((region) => [region.id, region.polygon_uv_mm]));
      this.el.confirm.checked = Boolean(result.annotation.confirmed);
      this.el.position.value = String(Math.round(result.plane.position_fraction * 100));
      this.el["position-value"].textContent = `${this.el.position.value}%`;
      this.viewer?.setAnalysisSection(result, (id) => this.selectRegion(id));
      this.viewer?.setAnalysisVisible(this.el.visible.checked); this.viewer?.selectAnalysisRegion(this.selected);
      this.el.editor.open = false; this.renderResult(); this.draw(); this.updateControls();
      this.status("分布已保存。占比按该截面的正向穿越流量统计；工程复核状态见下方说明。");
    }

    renderResult() {
      this.el.result.replaceChildren();
      for (const [id, { value }] of this.regionButtons) {
        const region = id === "unassigned" ? this.result?.unassigned : this.result?.regions?.find((item) => item.id === id);
        const incomplete = this.result?.coverage_complete === false || this.result?.validation?.coverage_status === "incomplete";
        value.textContent = region ? percent(id === "unassigned" ? region.percent_of_section : region.percent_of_side, id !== "unassigned" && incomplete) : (id === "unassigned" ? "待计算" : (this.polygons.has(id) ? "已标注" : "未标注"));
      }
      if (!this.result) return;
      const result = this.result;
      const incomplete = result.coverage_complete === false || result.validation?.coverage_status === "incomplete";
      const selected = this.selected === "unassigned" ? result.unassigned : result.regions?.find((region) => region.id === this.selected);
      const dl = document.createElement("dl"); dl.className = "kv cfd-section-metrics";
      const addMetric = (label, value) => { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = label; dd.textContent = value; dl.append(dt, dd); };
      addMetric("选中区域", this.selected === "unassigned" ? "未标注区域" : REGIONS.find((item) => item.id === this.selected).label);
      addMetric("正向流量", selected ? `${number(selected.forward_lpm)} L/min` : "该区域尚未标注");
      if (this.selected !== "unassigned") addMetric("占该侧正向流量", selected ? percent(selected.percent_of_side, incomplete) : "—");
      addMetric("占截面正向流量", selected && finite(selected.percent_of_section) ? `${number(selected.percent_of_section, 1)}%` : "—");
      addMetric("反向 / 净流量", selected ? `${number(selected.reverse_lpm)} / ${number(selected.net_lpm)} L/min` : "—");
      addMetric("全截面正向流量", `${number(result.totals?.forward_lpm)} L/min`);
      addMetric("全截面反向 / 净流量", `${number(result.totals?.reverse_lpm)} / ${number(result.totals?.net_lpm)} L/min`);
      this.el.result.append(dl);
      const summary = document.createElement("p"); summary.className = "cfd-section-summary";
      const sides = result.sides || [];
      summary.textContent = sides.map((side) => `${side.id === "left" ? "左" : "右"}侧 ${number(side.forward_lpm)} L/min（${finite(side.percent_of_section) ? `${number(side.percent_of_section, 1)}%` : "占比不可用"}）`).join("；");
      this.el.result.append(summary);
      const notes = [];
      if (incomplete) notes.push(`未标注区正向流量 ${number(result.unassigned?.forward_lpm, 3)} L/min，面积 ${number(result.unassigned?.area_mm2)} mm²；左右及各侧占比需补齐标注后解读。`);
      if (typeof result.inlet_condition === "string") notes.push(result.inlet_condition);
      else if (result.inlet_condition?.description) notes.push(result.inlet_condition.description);
      notes.push("上／中／下是此处人工标注的分析区域。正向比例描述气流穿过这张截面的分布，反向流量单独保留。");
      if (result.validation?.status !== "passed") notes.push(`工程复核待完成${result.validation?.reasons?.length ? `：${result.validation.reasons.join("；")}` : "。"}`);
      for (const note of notes) { const p = document.createElement("p"); p.className = "cfd-section-note"; p.textContent = note; this.el.result.append(p); }
    }

    setBusy(busy) { this.busy = busy; this.root.setAttribute("aria-busy", String(busy)); this.updateControls(); this.draw(); }
    updateControls() {
      const available = Boolean(this.previewData) && !this.busy, editable = available && this.selected !== "unassigned";
      this.el.position.disabled = this.busy; this.el.preview.disabled = this.busy || !this.caseId;
      for (const name of ["u", "v", "add-point"]) this.el[name].disabled = !editable;
      this.el.confirm.disabled = !available;
      this.el.close.disabled = !editable || this.draft.length < 3;
      this.el.undo.disabled = !editable || !this.draft.length;
      this.el["clear-region"].disabled = !editable || (!this.polygons.has(this.selected) && !this.draft.length);
      this.el.compute.disabled = !available || !this.polygons.size || Boolean(this.draft.length) || !this.el.confirm.checked;
      this.el.compute.textContent = this.busy ? "处理中…" : "计算并保存分布";
      this.el["draft-info"].textContent = this.draft.length ? `${this.draft.length} 个顶点，闭合后保存此区域` : (this.polygons.has(this.selected) ? "此区域已标注；添加新顶点可替换边界" : "尚未添加顶点");
      for (const [id, { button }] of this.regionButtons) button.disabled = this.busy || (id === "unassigned" && !this.result);
      if (!this.result) for (const [id, { value }] of this.regionButtons) value.textContent = id === "unassigned" ? "待计算" : (this.polygons.has(id) ? "已标注" : "未标注");
    }

    toUv(x, y) { const t = this.transform; return t ? [(x - t.x) / t.scale + t.minU, t.maxV - (y - t.y) / t.scale] : [0, 0]; }
    toCanvas(point) { const t = this.transform; return [t.x + (point[0] - t.minU) * t.scale, t.y + (t.maxV - point[1]) * t.scale]; }

    draw() {
      const canvas = this.el.canvas, context = canvas.getContext("2d");
      if (!context) return;
      const rect = canvas.getBoundingClientRect();
      const width = Math.max(rect.width, 1), height = Math.max(rect.height, 1), ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
      context.scale(ratio, ratio); context.fillStyle = "#0d1519"; context.fillRect(0, 0, width, height);
      const mesh = this.previewData?.section_mesh;
      if (!mesh) { context.fillStyle = "#cfe3df"; context.font = '13px "Segoe UI", sans-serif'; context.textAlign = "center"; context.fillText(this.busy ? "正在读取截面…" : "选择位置后读取真实气道截面", width / 2, height / 2); return; }
      let minU = Infinity, maxU = -Infinity, minV = Infinity, maxV = -Infinity;
      for (let i = 0; i < mesh.uv_mm.length; i += 2) { minU = Math.min(minU, mesh.uv_mm[i]); maxU = Math.max(maxU, mesh.uv_mm[i]); minV = Math.min(minV, mesh.uv_mm[i + 1]); maxV = Math.max(maxV, mesh.uv_mm[i + 1]); }
      const scale = Math.min((width - 56) / Math.max(maxU - minU, 1), (height - 56) / Math.max(maxV - minV, 1));
      this.transform = { minU, maxV, scale, x: (width - (maxU - minU) * scale) / 2, y: (height - (maxV - minV) * scale) / 2 };
      const vertex = (i) => this.toCanvas([mesh.uv_mm[i * 2], mesh.uv_mm[i * 2 + 1]]);
      context.beginPath();
      for (let i = 0; i < mesh.indices.length; i += 3) { const a = vertex(mesh.indices[i]), b = vertex(mesh.indices[i + 1]), c = vertex(mesh.indices[i + 2]); context.moveTo(...a); context.lineTo(...b); context.lineTo(...c); context.closePath(); }
      context.fillStyle = "#708c88"; context.fill();
      const path = (points) => { context.beginPath(); points.forEach((point, i) => { const p = this.toCanvas(point); if (!i) context.moveTo(...p); else context.lineTo(...p); }); };
      for (const region of REGIONS) {
        const polygon = this.polygons.get(region.id);
        if (!polygon?.length) continue;
        path(polygon); context.closePath(); context.globalAlpha = 0.22; context.fillStyle = region.color; context.fill(); context.globalAlpha = 1;
        context.strokeStyle = region.color; context.lineWidth = region.id === this.selected ? 3 : 1.5; context.stroke();
      }
      if (this.draft.length) {
        const color = REGIONS.find((region) => region.id === this.selected)?.color || "#b5c7c5";
        path(this.draft); context.strokeStyle = color; context.lineWidth = 2; context.stroke();
        for (const point of this.draft) { const p = this.toCanvas(point); context.beginPath(); context.arc(...p, 3.5, 0, Math.PI * 2); context.fillStyle = color; context.fill(); }
      }
      if (document.activeElement === canvas) { const p = this.toCanvas(this.cursor); context.strokeStyle = "#ffffff"; context.lineWidth = 1; context.beginPath(); context.moveTo(p[0] - 7, p[1]); context.lineTo(p[0] + 7, p[1]); context.moveTo(p[0], p[1] - 7); context.lineTo(p[0], p[1] + 7); context.stroke(); }
      context.fillStyle = "#cfe3df"; context.font = '12px "Segoe UI", "Microsoft YaHei UI", sans-serif'; context.textAlign = "left"; context.fillText("患者右", 10, height - 10); context.textAlign = "right"; context.fillText("患者左", width - 10, height - 10); context.textAlign = "center"; context.fillText("上", width / 2, 18);
    }

    clear() {
      clearTimeout(this.previewTimer); this.abortController?.abort(); this.sequence += 1;
      this.viewer?.clearAnalysisSection(); this.viewer = null; this.caseId = null; this.previewData = null; this.result = null;
      this.dirty = false; this.busy = false; this.draft = []; this.polygons.clear(); this.el.confirm.checked = false;
      this.selected = REGIONS[0].id; this.el.selected.textContent = REGIONS[0].label;
      for (const [id, { button }] of this.regionButtons) button.setAttribute("aria-pressed", String(id === this.selected));
      this.el.position.value = "50"; this.el["position-value"].textContent = "50%";
      this.root.hidden = true; this.root.setAttribute("aria-busy", "false"); this.status(""); this.renderResult(); this.updateControls();
    }

    dispose() { this.clear(); this.resizeObserver.disconnect(); for (const [element, name, handler] of this.listeners) element.removeEventListener(name, handler); this.listeners = []; }
  }

  window.CfdSections = CfdSections;
})();
