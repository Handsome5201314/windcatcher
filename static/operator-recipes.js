"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const multiuser = () => window.WindcatcherMultiuser === true;
  const stepLabels = {
    qc: "质量检查", segment: "分割", endplanes: "端面裁切", surface: "表面模型",
    cfd_prep: "CFD 算例", cfd_results: "病例流场", report: "报告", review: "CFD 复核",
  };
  const toolLabels = {
    service_status: "服务状态", list_cfd_cases: "匿名病例列表",
    inspect_cfd_case: "病例准备状态", get_cfd_batch: "计算批次摘要",
  };
  const triggerLabels = {
    manual: "手动查看", case_stage_changed: "病例阶段变化",
    batch_finished: "计算批次完成", numeric_gate_failed: "数值质量闸门未通过",
  };
  const steps = Object.keys(stepLabels);
  const tools = Object.keys(toolLabels);
  const fields = [...steps.map(step => "recipe-step-" + step),
    ...tools.map(tool => "recipe-tool-" + tool), "operator-recipes-trigger"];
  const recipeIdPattern = /^recipe-[0-9a-f]{32}$/;
  let recipes = [];
  let candidates = [];
  let candidateLoaded = false;
  let candidateBusy = false;
  let candidateGeneration = 0;
  let candidateOrder = null;
  let loaded = false;
  let loading = null;
  let busy = false;
  let conflict = false;
  let editingId = null;
  let pendingRevokeId = null;

  function status(message, error = false) {
    const node = byId("operator-recipes-status");
    node.textContent = message;
    node.classList.toggle("is-error", error);
    node.setAttribute("role", error ? "alert" : "status");
  }

  function candidateStatus(message, error = false) {
    const node = byId("operator-recipe-candidates-status");
    node.textContent = message;
    node.classList.toggle("is-error", error);
    node.setAttribute("role", error ? "alert" : "status");
  }

  function resetCandidates() {
    candidateGeneration++;
    candidates = [];
    candidateLoaded = false;
    candidateOrder = null;
    candidateStatus("点击“分析我的操作记录”查看候选。");
  }

  function canEdit() { return multiuser() && loaded && !busy && !conflict; }

  function syncControls() {
    const enabled = canEdit();
    for (const id of fields) byId(id).disabled = !enabled;
    byId("operator-recipes-submit").disabled = !enabled;
    byId("operator-recipes-refresh").disabled = busy;
    byId("operator-recipes-cancel").hidden = editingId === null;
    byId("operator-recipes-cancel").disabled = !enabled;
    byId("operator-recipe-candidates-refresh").disabled = !enabled || candidateBusy;
    renderList();
    renderCandidates();
  }

  function validEnumList(value, choices, maximum) {
    return Array.isArray(value) && value.length >= 1 && value.length <= maximum &&
      value.every(item => typeof item === "string" && choices.includes(item)) &&
      new Set(value).size === value.length;
  }

  function validateRow(row) {
    const spec = row?.spec;
    if (!row || !recipeIdPattern.test(row.recipe_id) ||
        typeof row.owner_id !== "string" || !/^[0-9a-f]{32}$/.test(row.owner_id) ||
        !Number.isSafeInteger(row.version) || row.version < 1 || row.schema_version !== 1 ||
        typeof row.revoked !== "boolean" || !spec || typeof spec !== "object" ||
        !validEnumList(spec.suggested_steps, steps, 8) ||
        !validEnumList(spec.allowed_tools, tools, 4) ||
        !Object.hasOwn(triggerLabels, spec.trigger_condition))
      throw new Error("操作配方响应格式异常");
    return row;
  }

  function validateCandidate(candidate) {
    if (!candidate || typeof candidate !== "object" ||
        Object.keys(candidate).some(key => key !== "suggested_steps" && key !== "case_count") ||
        !validEnumList(candidate.suggested_steps, steps, 4) ||
        candidate.suggested_steps.length < 2 ||
        !Number.isSafeInteger(candidate.case_count) || candidate.case_count < 3)
      throw new Error("操作习惯线索响应格式异常");
    return candidate;
  }

  async function request(method, path, body) {
    const response = await fetch(path, {
      method, cache: "no-store", credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const raw = await response.text();
    let data;
    try { data = raw ? JSON.parse(raw) : null; }
    catch { throw Object.assign(new Error("服务响应格式异常（HTTP " + response.status + "）"), { status: response.status }); }
    if (!response.ok)
      throw Object.assign(new Error(data?.detail?.reason || data?.reason || "请求失败（HTTP " + response.status + "）"),
        { status: response.status });
    return data;
  }

  function button(text, className, action, disabled = false) {
    const node = document.createElement("button");
    node.type = "button";
    node.className = className;
    node.textContent = text;
    node.disabled = disabled;
    node.addEventListener("click", action);
    return node;
  }

  function renderList() {
    const box = byId("operator-recipes-list");
    box.replaceChildren();
    if (!recipes.length) {
      const empty = document.createElement("p");
      empty.className = "recipe-empty";
      empty.textContent = loaded ? "还没有配方草案。选好建议步骤和只读信息后，点击“创建草案”。" : "尚未载入草案。";
      box.append(empty);
      return;
    }
    for (const row of recipes) {
      const article = document.createElement("article");
      article.className = "recipe-row" + (row.revoked ? " is-revoked" : "");
      const head = document.createElement("div");
      head.className = "recipe-row-head";
      const title = document.createElement("strong");
      title.textContent = row.spec.suggested_steps.map(step => stepLabels[step]).join(" → ");
      const meta = document.createElement("small");
      meta.textContent = "版本 " + row.version + " · " + (row.revoked ? "已撤销" : "草案");
      head.append(title, meta);
      const detail = document.createElement("p");
      detail.textContent = "时机：" + triggerLabels[row.spec.trigger_condition] +
        " · 只读信息：" + row.spec.allowed_tools.map(tool => toolLabels[tool]).join("、");
      article.append(head, detail);
      if (!row.revoked) {
        const actions = document.createElement("div");
        actions.className = "recipe-row-actions";
        actions.append(button("编辑草案", "button button-quiet button-compact", () => edit(row.recipe_id), !canEdit()));
        if (pendingRevokeId === row.recipe_id) {
          actions.append(button("确认撤销", "button button-quiet button-compact", () => revoke(row.recipe_id), !canEdit()));
          actions.append(button("取消", "button button-quiet button-compact", () => {
            pendingRevokeId = null;
            syncControls();
          }, !canEdit()));
        } else {
          actions.append(button("撤销草案", "button button-quiet button-compact", () => {
            if (!canEdit()) return;
            pendingRevokeId = row.recipe_id;
            status("请再次点击“确认撤销”；撤销后不能恢复。");
            syncControls();
          }, !canEdit()));
        }
        article.append(actions);
      }
      box.append(article);
    }
  }

  function renderCandidates() {
    const box = byId("operator-recipe-candidates-list");
    box.replaceChildren();
    if (!candidateLoaded) return;
    for (const candidate of candidates) {
      const article = document.createElement("article");
      article.className = "recipe-row";
      const head = document.createElement("div");
      head.className = "recipe-row-head";
      const title = document.createElement("strong");
      title.textContent = candidate.suggested_steps.map(step => stepLabels[step]).join(" → ");
      const count = document.createElement("small");
      count.textContent = candidate.case_count + " 个不同病例中的重复顺序";
      head.append(title, count);
      const actions = document.createElement("div");
      actions.className = "recipe-row-actions";
      actions.append(button("填入草案", "button button-quiet button-compact", () => fillCandidate(candidate),
        !canEdit() || candidateBusy || editingId !== null));
      article.append(head, actions);
      box.append(article);
    }
  }

  function fillCandidate(candidate) {
    if (!canEdit() || candidateBusy || editingId !== null) return;
    resetForm();
    candidateOrder = [...candidate.suggested_steps];
    for (const step of candidateOrder) byId("recipe-step-" + step).checked = true;
    syncControls();
    byId("recipe-tool-service_status").focus();
    status("已填入建议步骤。请自行选择只读信息并核对顺序，再点击“创建草案”保存。没有自动执行任何操作。");
  }

  function resetForm() {
    editingId = null;
    pendingRevokeId = null;
    candidateOrder = null;
    for (const step of steps) byId("recipe-step-" + step).checked = false;
    for (const tool of tools) byId("recipe-tool-" + tool).checked = false;
    byId("operator-recipes-trigger").value = "manual";
    byId("operator-recipes-form-title").textContent = "新建草案";
    byId("operator-recipes-submit").textContent = "创建草案";
    syncControls();
  }

  function edit(recipeId) {
    if (!canEdit()) return;
    const row = recipes.find(item => item.recipe_id === recipeId && !item.revoked);
    if (!row) return;
    editingId = recipeId;
    pendingRevokeId = null;
    candidateOrder = null;
    for (const step of steps) byId("recipe-step-" + step).checked = row.spec.suggested_steps.includes(step);
    for (const tool of tools) byId("recipe-tool-" + tool).checked = row.spec.allowed_tools.includes(tool);
    byId("operator-recipes-trigger").value = row.spec.trigger_condition;
    byId("operator-recipes-form-title").textContent = "编辑草案 · 版本 " + row.version;
    byId("operator-recipes-submit").textContent = "保存修改";
    syncControls();
    byId("recipe-step-qc").focus();
    status("正在编辑版本 " + row.version + "；保存前不会改变已有草案。");
  }

  function readSpec() {
    const selected = steps.filter(step => byId("recipe-step-" + step).checked);
    const previous = editingId && recipes.find(row => row.recipe_id === editingId);
    const retained = (previous?.spec.suggested_steps || candidateOrder || []).filter(step => selected.includes(step));
    const suggested_steps = [...retained, ...selected.filter(step => !retained.includes(step))];
    const allowed_tools = tools.filter(tool => byId("recipe-tool-" + tool).checked);
    if (!suggested_steps.length) {
      byId("recipe-step-qc").focus();
      throw new Error("请至少选择一项建议步骤");
    }
    if (!allowed_tools.length) {
      byId("recipe-tool-service_status").focus();
      throw new Error("请至少选择一项只读信息");
    }
    const trigger_condition = byId("operator-recipes-trigger").value;
    if (!Object.hasOwn(triggerLabels, trigger_condition)) throw new Error("请选择有效的建议时机");
    return { suggested_steps, allowed_tools, trigger_condition };
  }

  function conflictStatus() {
    conflict = true;
    status("版本冲突：草案已在其他页面改变。请点击“重新加载”获取最新版本；当前填写会被替换。", true);
  }

  async function load(force = false) {
    if (!multiuser()) return false;
    if (loading) return loading;
    if (loaded && !force) return true;
    resetCandidates();
    loaded = false;
    busy = true;
    syncControls();
    status("正在读取当前账号的配方草案…");
    loading = (async () => {
      try {
        const data = await request("GET", "/api/operator/recipes");
        if (!data || !Array.isArray(data.recipes)) throw new Error("操作配方列表响应格式异常");
        recipes = data.recipes.map(validateRow);
        loaded = true;
        conflict = false;
        resetForm();
        status(recipes.length ? "已读取 " + recipes.length + " 条个人草案。" : "当前账号还没有配方草案。");
        return true;
      } catch (error) {
        if (error.status === 401 || error.status === 403) {
          recipes = [];
          editingId = null;
          pendingRevokeId = null;
        }
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

  async function loadCandidates() {
    if (!multiuser() || !loaded || busy || candidateBusy) return false;
    candidateBusy = true;
    const generation = candidateGeneration;
    syncControls();
    candidateStatus("正在分析当前账号的重复操作顺序…");
    try {
      const data = await request("GET", "/api/operator/recipe-candidates");
      if (generation !== candidateGeneration) return false;
      if (!data || !Array.isArray(data.candidates) || data.candidates.length > 5)
        throw new Error("操作习惯线索响应格式异常");
      candidates = data.candidates.map(validateCandidate);
      candidateLoaded = true;
      candidateStatus(candidates.length ? "找到 " + candidates.length + " 条使用习惯线索。请自行核对后填入草案。" :
        "近 180 天还没有跨至少 3 个病例重复的操作顺序；不会生成草案。");
      return true;
    } catch (error) {
      if (generation !== candidateGeneration) return false;
      candidates = [];
      candidateLoaded = false;
      if (error.status === 401 || error.status === 403) {
        recipes = [];
        loaded = false;
        editingId = null;
        pendingRevokeId = null;
        candidateOrder = null;
        status("会话已失效，请重新登录后读取个人草案。", true);
      }
      candidateStatus("分析失败：" + error.message + "。请重试。", true);
      return false;
    } finally {
      candidateBusy = false;
      syncControls();
    }
  }

  async function save(event) {
    event.preventDefault();
    if (!canEdit()) return;
    let spec;
    try { spec = readSpec(); }
    catch (error) { status(error.message, true); return; }
    const previous = editingId ? recipes.find(row => row.recipe_id === editingId && !row.revoked) : null;
    if (editingId && !previous) { conflictStatus(); syncControls(); return; }
    busy = true;
    syncControls();
    status(previous ? "正在保存草案修改…" : "正在创建草案…");
    try {
      const path = previous ? "/api/operator/recipes/" + previous.recipe_id : "/api/operator/recipes";
      const body = previous ? { expected_version: previous.version, spec } : { spec };
      const saved = validateRow(await request(previous ? "PUT" : "POST", path, body));
      if (previous) recipes = recipes.map(row => row.recipe_id === saved.recipe_id ? saved : row);
      else recipes.push(saved);
      resetForm();
      status(previous ? "草案修改已保存（版本 " + saved.version + "）。" : "草案已创建（版本 " + saved.version + "）。");
    } catch (error) {
      if (error.status === 409) conflictStatus();
      else status("保存失败：" + error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
  }

  async function revoke(recipeId) {
    if (!canEdit() || pendingRevokeId !== recipeId) return;
    const row = recipes.find(item => item.recipe_id === recipeId && !item.revoked);
    if (!row) { conflictStatus(); syncControls(); return; }
    busy = true;
    syncControls();
    status("正在撤销草案…");
    try {
      const revoked = validateRow(await request("POST", "/api/operator/recipes/" + recipeId + "/revoke",
        { expected_version: row.version }));
      if (!revoked.revoked) throw new Error("撤销响应格式异常");
      recipes = recipes.map(item => item.recipe_id === recipeId ? revoked : item);
      resetForm();
      status("草案已撤销（版本 " + revoked.version + "），不能恢复。");
    } catch (error) {
      if (error.status === 409) conflictStatus();
      else status("撤销失败：" + error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
  }

  function activate() {
    if (!multiuser()) return false;
    byId("operator-recipes-panel").hidden = false;
    return load();
  }

  if (multiuser()) {
    byId("operator-recipes-panel").hidden = false;
    byId("operator-recipes-form").addEventListener("submit", save);
    byId("operator-recipes-refresh").addEventListener("click", () => load(true));
    byId("operator-recipe-candidates-refresh").addEventListener("click", loadCandidates);
    byId("operator-recipes-cancel").addEventListener("click", () => {
      if (!canEdit()) return;
      resetForm();
      status("已取消编辑，现可新建草案。");
    });
    syncControls();
    window.addEventListener("hashchange", () => {
      if (window.location.hash === "#env") activate();
    });
    if (window.location.hash === "#env") activate();
  }
  window.WindcatcherOperatorRecipes = { activate };
})();
