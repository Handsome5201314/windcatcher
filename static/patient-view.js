"use strict";
(() => {
  const base = location.pathname;
  const unlockPanel = document.getElementById("unlock-panel");
  const modelPanel = document.getElementById("model-panel");
  const unlockError = document.getElementById("unlock-error");
  const modelError = document.getElementById("model-error");
  const unlockButton = document.getElementById("unlock-button");
  const fieldSelect = document.getElementById("field");
  const motionButton = document.getElementById("motion");
  const tourButton = document.getElementById("tour-toggle");
  const resetButton = document.getElementById("view-reset");
  const modeSelect = document.getElementById("presentation-mode");
  const comparisonButton = document.getElementById("open-reference-comparison");
  const tourCaption = document.getElementById("tour-caption");
  const tourProgress = document.getElementById("tour-progress");
  const tourDuration = 60000;
  const anatomyDuration = 20000;
  let viewer;
  let modelData;
  let comparison;
  let tourElapsed = 0;
  let tourStartedAt = null;
  let tourFrame = null;
  let tourStage = -1;
  let tourPlaying = false;
  let tourComplete = false;
  let tourStarted = false;

  function setPresentation(mode) {
    viewer?.setPresentationMode(mode);
    modeSelect.value = mode;
    fieldSelect.disabled = mode !== 'flow';
    motionButton.disabled = mode !== 'flow';
    document.getElementById('legend').hidden = mode !== 'flow';
  }

  function syncTourButton() {
    tourButton.setAttribute("aria-pressed", String(tourPlaying));
    tourButton.textContent = tourPlaying ? "暂停导览" : tourComplete ? "重播导览"
      : tourStarted ? "继续导览" : "播放导览";
    tourProgress.value = Math.round(tourElapsed / 1000);
  }

  function showTourFrame() {
    const anatomy = tourElapsed < anatomyDuration;
    const stage = anatomy ? 0 : 1;
    if (stage !== tourStage) {
      setPresentation(anatomy ? "anatomy" : "flow");
      viewer.setPlaying(true);
      motionButton.setAttribute("aria-pressed", "true");
      motionButton.textContent = "暂停粒子";
      tourCaption.textContent = anatomy
        ? "1/2 · 先看气道结构形态，可从不同角度观察。此表面不是鼻甲或鼻中隔组织的独立分割。"
        : "2/2 · 观察固定计算工况下的气流路径。粒子循环不是实时呼吸。";
      tourStage = stage;
    }
    const progress = anatomy ? tourElapsed / anatomyDuration
      : (tourElapsed - anatomyDuration) / (tourDuration - anatomyDuration);
    const azimuth = -1.1 + (anatomy ? progress : 1 + progress) * Math.PI * 2;
    const elevation = anatomy ? 0.32 + 0.1 * Math.sin(progress * Math.PI)
      : 0.32 - 0.12 * Math.sin(progress * Math.PI);
    viewer.setPresentationPose(azimuth, elevation);
    tourProgress.value = Math.round(tourElapsed / 1000);
  }

  function tourTick(now) {
    tourFrame = null;
    if (!tourPlaying || !viewer || document.hidden) return;
    if (tourStartedAt === null) tourStartedAt = now - tourElapsed;
    tourElapsed = Math.min(tourDuration, Math.max(0, now - tourStartedAt));
    showTourFrame();
    if (tourElapsed >= tourDuration) {
      tourPlaying = false;
      tourComplete = true;
      tourCaption.textContent = "导览结束。可以自由旋转，或点击重播导览。";
      syncTourButton();
      return;
    }
    tourFrame = requestAnimationFrame(tourTick);
  }

  function pauseTour(message) {
    if (!tourPlaying) return;
    tourPlaying = false;
    tourStartedAt = null;
    if (tourFrame !== null) cancelAnimationFrame(tourFrame);
    tourFrame = null;
    if (message) tourCaption.textContent = message;
    syncTourButton();
  }

  function startTour(restart = false) {
    if (!viewer) return;
    if (restart || tourComplete) {
      tourElapsed = 0;
      tourComplete = false;
    }
    tourStage = -1;
    tourPlaying = true;
    tourStarted = true;
    tourStartedAt = null;
    fieldSelect.value = "velocity";
    viewer.setField("velocity");
    showTourFrame();
    syncTourButton();
    if (!document.hidden) tourFrame = requestAnimationFrame(tourTick);
  }

  async function loadModel() {
    const response = await fetch(base + "/model", { credentials: "same-origin", cache: "no-store" });
    if (response.status === 401) return false;
    if (!response.ok) throw new Error((await response.json()).detail?.reason || "模型暂不可用");
    const data = await response.json();
    modelData = data;
    pauseTour();
    viewer?.dispose();
    unlockPanel.hidden = true;
    modelPanel.hidden = false;
    try { viewer = new CfdViewer(document.getElementById("model"), data, {
      patientPresentation: true,
      onLegend: ({ field, range, unit, gradient }) => {
        const label = field === "velocity" ? "速度" : "压力";
        const displayUnit = unit === "m2/s2" ? "m²/s²" : unit;
        document.getElementById("legend-title").textContent = label + "颜色说明";
        document.getElementById("legend-scale").hidden = !range;
        document.getElementById("legend-min").textContent = range ? "低" + label + " · " + Number(range[0]).toFixed(2) + " " + displayUnit : "";
        document.getElementById("legend-max").textContent = range ? "高" + label + " · " + Number(range[1]).toFixed(2) + " " + displayUnit : "";
        document.getElementById("legend-colorbar").style.background = gradient;
        document.getElementById("legend-description").textContent = range
          ? "蓝色表示" + (field === "velocity" ? "气流速度" : "压力数值") + "较低，红色表示较高。颜色只比较本模型、本次计算中的" + label + "，不代表正常或异常。"
          : "当前字段没有可显示的采样值";
        document.getElementById("legend-unit-note").textContent = !range ? "" : field === "velocity"
          ? "m/s（米/秒）是速度单位。"
          : unit === "Pa" ? "Pa（帕）是压力单位；这里展示的是本次计算工况下的压力分布。"
          : "当前显示运动学压力（m²/s²）；缺少有效的空气密度记录，未换算为 Pa。";
      },
      onError: message => { modelError.textContent = message; modelError.hidden = false; },
    }); }
    catch (error) { modelPanel.hidden = true; unlockPanel.hidden = false; throw error; }
    viewer.resize();
    setPresentation("anatomy");
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    motionButton.setAttribute("aria-pressed", String(!reducedMotion));
    motionButton.textContent = reducedMotion ? "播放粒子" : "暂停粒子";
    if (!reducedMotion) startTour(true);
    else syncTourButton();
    return true;
  }

  document.getElementById("unlock-form").addEventListener("submit", async event => {
    event.preventDefault();
    unlockError.hidden = true;
    unlockButton.disabled = true;
    unlockButton.textContent = "正在验证…";
    try {
      const response = await fetch(base + "/unlock", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ access_code: document.getElementById("access-code").value }),
      });
      if (!response.ok) throw new Error((await response.json()).detail?.reason || "访问码验证失败");
      document.getElementById("access-code").value = "";
      if (!(await loadModel())) throw new Error("访问会话未生效，请确认使用 HTTPS 并允许本站 Cookie");
    } catch (error) {
      unlockError.textContent = error.message;
      unlockError.hidden = false;
    } finally {
      unlockButton.disabled = false;
      unlockButton.textContent = "查看模型";
    }
  });

  fieldSelect.addEventListener("change", event => {
    pauseTour("导览已暂停。可以自由旋转或继续播放导览。");
    try { viewer?.setField(event.target.value); }
    catch (error) { modelError.textContent = error.message; modelError.hidden = false; }
  });
  motionButton.addEventListener("click", event => {
    pauseTour("导览已暂停。可以自由旋转或继续播放导览。");
    setPresentation("flow");
    const playing = event.currentTarget.getAttribute("aria-pressed") !== "true";
    viewer?.setPlaying(playing);
    event.currentTarget.setAttribute("aria-pressed", String(playing));
    event.currentTarget.textContent = playing ? "暂停粒子" : "播放粒子";
  });
  tourButton.addEventListener("click", () => {
    if (tourPlaying) pauseTour("导览已暂停。可以自由旋转或继续播放导览。");
    else startTour(tourComplete);
  });
  resetButton.addEventListener("click", () => {
    pauseTour();
    viewer?.reset();
    tourCaption.textContent = "视角已回正。可以自由旋转或继续播放导览。";
  });
  modeSelect.addEventListener('change', event => {
    pauseTour('导览已暂停。可自由切换结构和气流。');
    setPresentation(event.target.value);
  });
  comparisonButton.addEventListener('click', async () => {
    if (!viewer || comparison) return;
    comparisonButton.disabled = true;
    modelError.hidden = true;
    try {
      const response = await fetch(base + '/metadata', {credentials:'same-origin', cache:'no-store'});
      if (!response.ok) throw new Error((await response.json()).detail?.reason || '无法读取此分享的计算条件');
      const meta = await response.json();
      pauseTour('正在查看参考病例对照。');
      const root = document.getElementById('patient-reference-comparison');
      viewer.setVisible(false);
      for (const id of ['model','patient-actions','patient-tour-guide','legend']) document.getElementById(id).hidden = true;
      root.hidden = false;
      comparison = new window.WindcatcherReferences.ReferenceComparison(root, {
        primary: {data:modelData, meta, title:'你的模型'},
        onClose: () => {
          comparison?.dispose(); comparison = null; root.hidden = true;
          for (const id of ['model','patient-actions','patient-tour-guide']) document.getElementById(id).hidden = false;
          viewer.setVisible(true); setPresentation(modeSelect.value); viewer.resize();
        },
      });
    } catch (error) { modelError.textContent = error.message; modelError.hidden = false; }
    finally { comparisonButton.disabled = false; }
  });
  for (const eventName of ["pointerdown", "wheel"]) {
    document.getElementById("model").addEventListener(eventName, () => {
      pauseTour("导览已暂停。可以自由旋转或继续播放导览。");
    }, { passive: true });
  }
  document.addEventListener("visibilitychange", () => {
    if (!tourPlaying) return;
    tourStartedAt = null;
    if (document.hidden && tourFrame !== null) {
      cancelAnimationFrame(tourFrame);
      tourFrame = null;
    } else if (!document.hidden && tourFrame === null) {
      tourFrame = requestAnimationFrame(tourTick);
    }
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) window.location.reload();
  });
  loadModel().catch(error => {
    unlockError.textContent = error.message;
    unlockError.hidden = false;
  });
  window.addEventListener("pagehide", () => { pauseTour(); comparison?.dispose(); viewer?.dispose(); });
})();
