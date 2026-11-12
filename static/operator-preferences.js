"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const multiuser = () => window.WindcatcherMultiuser === true;
  const stepLabels = {
    qc: "质量检查", segment: "分割", endplanes: "端面裁切", surface: "表面模型",
    cfd_prep: "CFD 算例", cfd_run: "CFD 求解", cfd_results: "病例流场",
    report: "报告", review: "CFD 复核", share: "患者分享",
  };
  const steps = Object.keys(stepLabels);
  const inputIds = [
    "pref-pose-enabled", "pref-azimuth", "pref-elevation", "pref-zoom",
    "pref-velocity-enabled", "pref-velocity-min", "pref-velocity-max",
    "pref-pressure-enabled", "pref-pressure-unit", "pref-pressure-min", "pref-pressure-max",
    ...steps.map(step => "pref-step-" + step),
  ];
  let version = null;
  let saved = null;
  let loading = null;
  let busy = false;
  let conflict = false;
  let currentViewer = null;
  let currentPayload = null;
  let posedViewer = null;

  function status(message, error = false) {
    const node = byId("operator-preferences-status");
    node.textContent = message;
    node.classList.toggle("is-error", error);
    node.setAttribute("role", error ? "alert" : "status");
  }

  function syncControls() {
    const enabled = version !== null && !busy && !conflict;
    for (const id of inputIds) byId(id).disabled = !enabled;
    for (const id of ["pref-azimuth", "pref-elevation", "pref-zoom"])
      byId(id).disabled = !enabled || !byId("pref-pose-enabled").checked;
    for (const id of ["pref-velocity-min", "pref-velocity-max"])
      byId(id).disabled = !enabled || !byId("pref-velocity-enabled").checked;
    for (const id of ["pref-pressure-unit", "pref-pressure-min", "pref-pressure-max"])
      byId(id).disabled = !enabled || !byId("pref-pressure-enabled").checked;
    byId("operator-preferences-save").disabled = !enabled;
    byId("operator-preferences-refresh").disabled = busy;
  }

  function renderFavorites() {
    const box = byId("operator-favorite-links");
    box.replaceChildren();
    for (const step of saved?.favorite_steps || []) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button button-quiet button-compact";
      button.textContent = stepLabels[step];
      button.addEventListener("click", () => window.WindcatcherOpenFavoriteStep?.(step));
      box.append(button);
    }
  }

  function renderPreferences(preferences) {
    const pose = preferences.view_pose;
    byId("pref-pose-enabled").checked = pose !== null;
    byId("pref-azimuth").value = pose ? String(pose.azimuth * 180 / Math.PI) : "0";
    byId("pref-elevation").value = pose ? String(pose.elevation * 180 / Math.PI) : "0";
    byId("pref-zoom").value = pose ? String(pose.zoom) : "1";
    const velocity = preferences.display_ranges.velocity;
    byId("pref-velocity-enabled").checked = velocity !== null;
    byId("pref-velocity-min").value = velocity ? String(velocity.min) : "";
    byId("pref-velocity-max").value = velocity ? String(velocity.max) : "";
    const pressure = preferences.display_ranges.pressure;
    byId("pref-pressure-enabled").checked = pressure !== null;
    byId("pref-pressure-unit").value = pressure ? pressure.unit : "Pa";
    byId("pref-pressure-min").value = pressure ? String(pressure.min) : "";
    byId("pref-pressure-max").value = pressure ? String(pressure.max) : "";
    for (const step of steps) byId("pref-step-" + step).checked = preferences.favorite_steps.includes(step);
    syncControls();
    renderFavorites();
  }

  function readNumber(id, minimum, maximum) {
    const input = byId(id);
    const raw = String(input.value).trim();
    const value = Number(raw);
    if (!raw || !Number.isFinite(value) || value < minimum || value > maximum) {
      input.focus();
      throw new Error((input.labels?.[0]?.textContent || id) + "须在 " + minimum + " 到 " + maximum + " 之间");
    }
    return value;
  }

  function readRange(kind) {
    if (!byId("pref-" + kind + "-enabled").checked) return null;
    const minimum = readNumber("pref-" + kind + "-min", kind === "velocity" ? 0 : -1e6, kind === "velocity" ? 500 : 1e6);
    const maximum = readNumber("pref-" + kind + "-max", kind === "velocity" ? 0 : -1e6, kind === "velocity" ? 500 : 1e6);
    if (minimum >= maximum) {
      byId("pref-" + kind + "-max").focus();
      throw new Error((kind === "velocity" ? "速度" : "压力") + "最大值必须大于最小值");
    }
    return { min: minimum, max: maximum,
      unit: kind === "velocity" ? "m/s" : byId("pref-pressure-unit").value };
  }

  function readPreferences() {
    const pose = byId("pref-pose-enabled").checked ? {
      azimuth: readNumber("pref-azimuth", -180, 180) * Math.PI / 180,
      elevation: readNumber("pref-elevation", -90, 90) * Math.PI / 180,
      zoom: readNumber("pref-zoom", 0.7, 5),
    } : null;
    const favorites = [...(saved?.favorite_steps || []).filter(step => byId("pref-step-" + step).checked),
      ...steps.filter(step => byId("pref-step-" + step).checked && !saved?.favorite_steps.includes(step))];
    if (favorites.length > 8) throw new Error("常用步骤最多选择 8 项");
    return { view_pose: pose,
      display_ranges: { velocity: readRange("velocity"), pressure: readRange("pressure") },
      favorite_steps: favorites };
  }

  async function request(method, body) {
    const response = await fetch("/api/operator/preferences", {
      method, cache: "no-store", credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const raw = await response.text();
    let data;
    try { data = raw ? JSON.parse(raw) : null; }
    catch { throw Object.assign(new Error("服务响应格式异常（HTTP " + response.status + "）"), { status: response.status }); }
    if (!response.ok) {
      throw Object.assign(new Error(data?.detail?.reason || data?.reason || "请求失败（HTTP " + response.status + "）"), { status: response.status });
    }
    const preferences = data?.preferences;
    const ranges = preferences?.display_ranges;
    if (!data || !Number.isSafeInteger(data.version) || data.version < 0 || data.schema_version !== 1 ||
        !preferences || typeof preferences !== "object" || !ranges || typeof ranges !== "object" ||
        !("velocity" in ranges) || !("pressure" in ranges) || !("view_pose" in preferences) ||
        !Array.isArray(preferences.favorite_steps) || preferences.favorite_steps.some(step => !steps.includes(step)))
      throw new Error("个人偏好响应格式异常");
    return data;
  }

  function applyToCurrentViewer() {
    const viewer = currentViewer;
    const payload = currentPayload;
    if (!viewer || viewer.disposed || !payload || !saved) return;
    try {
      if (saved.view_pose && typeof viewer.applyOperatorPose === "function") {
        viewer.applyOperatorPose(saved.view_pose);
        posedViewer = viewer;
      } else if (!saved.view_pose && posedViewer === viewer) {
        viewer.reset();
        posedViewer = null;
      }
      viewer.setColorRanges(null);
      const ranges = { ...viewer.ranges };
      for (const field of ["velocity", "pressure"]) {
        const choice = saved.display_ranges[field];
        if (choice && payload.units?.[field] === choice.unit) ranges[field] = [choice.min, choice.max];
      }
      viewer.setColorRanges(ranges);
    } catch (error) {
      status("偏好已保存，但当前流场显示应用失败：" + error.message, true);
    }
  }

  function accept(row) {
    version = row.version;
    saved = row.preferences;
    conflict = false;
    renderPreferences(saved);
    applyToCurrentViewer();
  }

  async function load(force = false) {
    if (!multiuser()) return false;
    if (loading) return loading;
    if (version !== null && !force) return true;
    busy = true;
    syncControls();
    status("正在读取当前账号的偏好…");
    loading = (async () => {
      try {
        accept(await request("GET"));
        status("已读取个人偏好（版本 " + version + "）。");
        return true;
      } catch (error) {
        status("读取失败：" + error.message + "。请重新加载。", true);
        return false;
      } finally {
        busy = false;
        loading = null;
        syncControls();
      }
    })();
    return loading;
  }

  async function save(event) {
    event.preventDefault();
    if (!multiuser() || busy || conflict || version === null) return;
    let preferences;
    try { preferences = readPreferences(); }
    catch (error) { status(error.message, true); return; }
    busy = true;
    syncControls();
    status("正在保存个人偏好…");
    try {
      accept(await request("PUT", { expected_version: version, preferences }));
      status("偏好已保存（版本 " + version + "）。");
    } catch (error) {
      if (error.status === 409) {
        conflict = true;
        status("其他页面已修改个人偏好。请点击“重新加载”获取最新版本；当前未保存改动会被替换。", true);
      } else status("保存失败：" + error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
  }

  function activate() {
    if (!multiuser()) return;
    byId("operator-preferences-panel").hidden = false;
    return load();
  }

  function applyToViewer(viewer, payload) {
    if (!multiuser()) return;
    if (currentViewer !== viewer) posedViewer = null;
    currentViewer = viewer;
    currentPayload = payload;
    if (version !== null) applyToCurrentViewer();
    else load();
  }

  if (multiuser()) {
    byId("operator-preferences-panel").hidden = false;
    byId("operator-preferences-form").addEventListener("submit", save);
    byId("operator-preferences-refresh").addEventListener("click", () => load(true));
    for (const id of ["pref-pose-enabled", "pref-velocity-enabled", "pref-pressure-enabled"])
      byId(id).addEventListener("change", syncControls);
    byId("pref-pressure-unit").addEventListener("change", () => {
      byId("pref-pressure-min").value = "";
      byId("pref-pressure-max").value = "";
      status("压力单位已切换，请按新单位重新填写最小值和最大值。");
    });
    for (const step of steps) byId("pref-step-" + step).addEventListener("change", event => {
      if (steps.filter(name => byId("pref-step-" + name).checked).length > 8) {
        event.target.checked = false;
        status("常用步骤最多选择 8 项。", true);
      }
    });
    syncControls();
  }
  window.WindcatcherOperatorPreferences = { activate, applyToViewer };
})();
