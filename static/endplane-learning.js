/* Explicit human feedback on current-case proposals; never saves endplanes. */
(() => {
  "use strict";
  const $ = id => document.getElementById("endplane-learning-" + id);
  if (!$("panel")) return;
  const labels = { proposed: "待人工核对", accepted: "已接受", adjusted: "已修正", rejected: "已否决" };
  const state = { context: null, key: null, open: false, fetched: false, loading: false,
    pending: false, proposing: false, autoGenerating: false, request: 0, epoch: 0, controller: null, items: [], selected: null,
    loadedId: null, loadedSignature: null, draftSignature: null, message: "", failed: false };
  const clone = value => JSON.parse(JSON.stringify(value));
  const vector = value => Array.isArray(value) && value.length === 3 &&
    value.every(part => typeof part === "number" && Number.isFinite(part));
  const plane = value => value && vector(value.point_lps_mm) && vector(value.normal_lps) &&
    Math.hypot(...value.normal_lps) > 0;

  function validPlan(plan, context) {
    return Boolean(plan && plan.scope === context.scope && plan.mask_version === context.maskVersion &&
      ["oblique_planes_v1", "oblique_local_inlet_v2", "oblique_bilateral_local_v3"].includes(plan.format) &&
      !Object.hasOwn(plan, "anterior_index") && !Object.hasOwn(plan, "posterior_index") &&
      plane(plan.posterior_plane) &&
      (plan.format === "oblique_bilateral_local_v3"
        ? context.scope === "main_airway" && !Object.hasOwn(plan, "anterior_plane") &&
          !Object.hasOwn(plan, "anterior_seed_lps_mm") &&
          plane(plan.left_inlet_plane) && plane(plan.right_inlet_plane) &&
          vector(plan.left_inlet_seed_lps_mm) && vector(plan.right_inlet_seed_lps_mm) &&
          vector(plan.posterior_seed_lps_mm)
        : plane(plan.anterior_plane) && !Object.hasOwn(plan, "left_inlet_plane") &&
          !Object.hasOwn(plan, "right_inlet_plane") && !Object.hasOwn(plan, "posterior_seed_lps_mm") &&
          (plan.format === "oblique_local_inlet_v2"
            ? context.scope === "main_airway" && vector(plan.anterior_seed_lps_mm)
            : !Object.hasOwn(plan, "anterior_seed_lps_mm"))));
  }

  function draftPlan(plan) {
    if (plan.format === "oblique_bilateral_local_v3") {
      return { scope: plan.scope, mask_version: plan.mask_version, format: plan.format,
        left_inlet_plane: clone(plan.left_inlet_plane), right_inlet_plane: clone(plan.right_inlet_plane),
        posterior_plane: clone(plan.posterior_plane),
        left_inlet_seed_lps_mm: [...plan.left_inlet_seed_lps_mm],
        right_inlet_seed_lps_mm: [...plan.right_inlet_seed_lps_mm],
        posterior_seed_lps_mm: [...plan.posterior_seed_lps_mm] };
    }
    return { scope: plan.scope, mask_version: plan.mask_version, format: plan.format,
      anterior_plane: clone(plan.anterior_plane), posterior_plane: clone(plan.posterior_plane),
      ...(plan.format === "oblique_local_inlet_v2" ? { anterior_seed_lps_mm: [...plan.anterior_seed_lps_mm] } : {}) };
  }

  function draft() {
    try { return state.context?.getDraft?.() || { plan: null, error: "当前端面草稿尚未就绪" }; }
    catch (error) { return { plan: null, error: error.message || "当前端面草稿无法读取" }; }
  }

  function signature(value) {
    return validPlan(value.plan, state.context) ? JSON.stringify(draftPlan(value.plan)) : null;
  }

  function selected() { return state.items.find(item => item.proposal_id === state.selected) || null; }
  function current(item) {
    return Boolean(item && item.case_id === state.context?.caseId && item.scope === state.context.scope &&
      item.mask_version === state.context.maskVersion && validPlan(item.plan, state.context));
  }
  function clearConfirmation() { $("confirm").checked = false; }
  function notice(message, failed = false) { state.message = message; state.failed = failed; }
  function active() { return Boolean(state.key && state.open); }
  function canAct(item = selected()) {
    return active() && !state.loading && !state.pending && state.context.canEdit && current(item) && item.status === "proposed";
  }

  function render() {
    $("panel").hidden = !state.open;
    $("status").textContent = state.message;
    $("status").classList.toggle("is-error", state.failed);
    $("panel").setAttribute("aria-busy", String(state.loading || state.pending));
    $("refresh").disabled = !active() || state.loading || state.pending;
    $("refresh").textContent = state.loading ? "读取中…" : "刷新候选";
    $("select").disabled = !active() || state.loading || state.pending || !state.items.length;
    const item = selected();
    const stale = item && item.mask_version !== state.context?.maskVersion;
    $("details").hidden = !item;
    $("details").textContent = item ? `来源：${item.source} / ${item.source_version || item.version}；` +
      `掩膜版本：${item.mask_version}；状态：${labels[item.status]}` +
      (item.plan?.format === "oblique_local_inlet_v2"
        ? "。单支局部入口候选仅供几何预览；当前不能用于双侧鼻呼吸 CFD 算例。"
        : item.plan?.format === "oblique_bilateral_local_v3"
          ? "。三端局部候选需人工核对左右鼻孔与鼻咽，反馈不会直接保存正式端面。" : "") +
      (stale ? "。陈旧版本：属于旧掩膜，不能加载或反馈。" :
        !current(item) ? "。方案格式无效或不完整，不能加载或反馈。" : "") : "";
    $("load").disabled = !active() || state.loading || state.pending || !state.context.canEdit || !current(item);
    const allowed = canAct(item);
    $("confirm").disabled = !allowed;
    const reviewed = allowed && $("confirm").checked;
    const value = draft();
    $("register").disabled = !active() || state.loading || state.pending || !state.context.canEdit ||
      Boolean(value.error) || !validPlan(value.plan, state.context);
    $("register").textContent = state.proposing ? "登记中…" : "登记当前草稿";
    $("register").title = value.error || "将完整的当前草稿登记为待人工核对的候选，正式端面不会保存";
    $("auto").disabled = !active() || state.loading || state.pending || !state.context.canEdit;
    $("auto").textContent = state.autoGenerating ? "生成中…" : "本机生成候选";
    $("auto").title = "仅根据当前已确认掩膜在本机生成单支局部入口候选；需要人工核对，不会保存正式端面，也不能直接用于双侧鼻呼吸 CFD";
    const acceptedDraft = state.loadedId === item?.proposal_id && state.loadedSignature === signature(value);
    $("accept").disabled = !reviewed || !acceptedDraft || Boolean(value.error);
    $("accept").title = acceptedDraft ? "核对候选后记录接受反馈" : "先加载此候选，核对 CT 与草稿；修改后的方案请记录为修正";
    $("adjust").disabled = !reviewed || Boolean(value.error) || !validPlan(value.plan, state.context);
    $("adjust").title = value.error || "用当前两端草稿记录修正反馈";
    $("reject").disabled = !reviewed;
  }

  function populate() {
    $("select").replaceChildren();
    for (const [index, item] of state.items.entries()) {
      const option = document.createElement("option");
      option.value = item.proposal_id;
      option.textContent = `${index + 1}. ${item.source} / ${item.source_version || item.version} · ${labels[item.status]}` +
        (item.mask_version === state.context.maskVersion ? "" : " · 旧掩膜");
      $("select").appendChild(option);
    }
    $("select").value = state.selected || "";
  }

  async function responseJson(response) {
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const reason = typeof data?.detail?.reason === "string" ? data.detail.reason :
        response.status === 401 ? "登录已失效，请重新登录后重试" :
        response.status === 403 ? "没有此病例的候选复核权限" :
        response.status === 409 ? "病例或候选状态已变化，请刷新" : "请求失败，请重试";
      throw new Error(`HTTP ${response.status}：${reason.slice(0, 240)}`);
    }
    return data;
  }

  async function refresh(preferredId = null) {
    if (!active() || state.loading || state.pending) return;
    const context = state.context, key = state.key, request = ++state.request;
    state.controller?.abort();
    state.controller = new AbortController();
    state.loading = true;
    clearConfirmation();
    state.loadedId = state.loadedSignature = null;
    notice("正在读取当前病例的候选与纠错记录…"); render();
    try {
      const response = await fetch(`/api/cases/${encodeURIComponent(context.caseId)}/segment/endplanes/learning?scope=${encodeURIComponent(context.scope)}`,
        { credentials: "same-origin", signal: state.controller.signal });
      const data = await responseJson(response);
      if (request !== state.request || key !== state.key || !active()) return;
      if (data?.case_id !== context.caseId || data.scope !== context.scope || !Array.isArray(data.proposals) ||
          data.proposals.some(item => !item || item.case_id !== context.caseId || item.scope !== context.scope ||
            !Number.isInteger(item.mask_version) || !/^proposal-[0-9a-f]{32}$/.test(item.proposal_id) ||
            !Object.hasOwn(labels, item.status) || typeof item.source !== "string" ||
            typeof (item.source_version || item.version) !== "string")) {
        throw new Error("候选返回的数据与当前病例不一致或无效，请刷新重试");
      }
      state.items = data.proposals;
      if (typeof preferredId === "string" && state.items.some(item => item.proposal_id === preferredId)) state.selected = preferredId;
      if (!selected()) state.selected = state.items.find(item => item.mask_version === context.maskVersion && item.status === "proposed")?.proposal_id || state.items[0]?.proposal_id || null;
      state.fetched = true;
      populate();
      notice(state.items.length ? `已读取 ${state.items.length} 条候选；加载到草稿后请对照 CT 人工核对。` :
        "当前范围暂无候选。可点击“本机生成候选”；也可在现有编辑器手动选好两端，再点击“登记当前草稿”，即可积累人工示例；登记后仍需加载并人工核对反馈。");
    } catch (error) {
      if (request !== state.request || key !== state.key || !active()) return;
      state.items = []; state.selected = null; populate();
      state.fetched = true;
      notice(`候选读取失败：${error.message || "网络不可用，请重试"}`, true);
    } finally {
      if (request === state.request && key === state.key) { state.loading = false; state.controller = null; render(); }
    }
  }

  async function registerDraft() {
    if (!active() || state.loading || state.pending || !state.context.canEdit) return;
    const value = draft();
    if (value.error || !validPlan(value.plan, state.context)) {
      notice(value.error || "请先完成当前两端草稿及局部入口选点，再登记候选。", true); render(); return;
    }
    const context = state.context, key = state.key, request = ++state.request, epoch = state.epoch;
    state.pending = state.proposing = true;
    state.controller = new AbortController();
    clearConfirmation(); state.loadedId = state.loadedSignature = null;
    notice("正在登记当前草稿为候选，尚未记录人工反馈…"); render();
    try {
      const response = await fetch(`/api/cases/${encodeURIComponent(context.caseId)}/segment/endplanes/learning/propose`, {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: context.scope, mask_version: context.maskVersion, plan: draftPlan(value.plan),
          source: "operator-manual", source_version: "ui-v1" }), signal: state.controller.signal,
      });
      const data = await responseJson(response);
      if (request !== state.request || key !== state.key || !active()) return;
      if (data?.status !== "available" || !/^proposal-[0-9a-f]{32}$/.test(data.proposal_id) ||
          data.proposal?.proposal_id !== data.proposal_id || !current(data.proposal) ||
          !Object.hasOwn(labels, data.proposal.status) || data.human_review_required !== true || data.committed !== false) {
        throw new Error("登记返回的数据与当前病例不一致或无效");
      }
      state.pending = state.proposing = false;
      await refresh(data.proposal_id);
      if (epoch === state.epoch && key === state.key && active() && !state.failed) {
        if (state.selected !== data.proposal_id) notice("登记响应已返回，但刷新后未找到该候选。请刷新核对记录后重试。", true);
        else notice(selected().status === "proposed"
          ? "当前草稿已登记，已选中对应候选。请加载候选、对照 CT 核对并勾选确认后记录反馈；正式端面尚未保存。"
          : `此草稿已登记，候选状态为“${labels[selected().status]}”。请核对已有记录；正式端面尚未保存。`);
      }
    } catch (error) {
      if (request !== state.request || key !== state.key || !active()) return;
      notice(`草稿登记未确认：${error.message || "网络不可用"}。请刷新核对记录后重试。`, true);
    } finally {
      if (epoch === state.epoch && key === state.key && active()) {
        state.pending = state.proposing = false; state.controller = null; render();
      }
    }
  }

  async function generateAutoCandidate() {
    if (!active() || state.loading || state.pending || !state.context.canEdit) return;
    const context = state.context, key = state.key, request = ++state.request, epoch = state.epoch;
    state.pending = state.autoGenerating = true;
    state.controller = new AbortController();
    clearConfirmation(); state.loadedId = state.loadedSignature = null;
    notice("正在从当前已确认掩膜本机生成候选；不会保存正式端面…"); render();
    try {
      const response = await fetch(`/api/cases/${encodeURIComponent(context.caseId)}/segment/endplanes/learning/auto-propose`, {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: context.scope, mask_version: context.maskVersion }),
        signal: state.controller.signal,
      });
      const data = await responseJson(response);
      if (request !== state.request || key !== state.key || epoch !== state.epoch || !active()) return;
      if (data?.status === "unavailable") {
        const reasons = {
          unsupported_scope: "当前范围暂不支持本机局部入口候选",
          local_inlet_unavailable: "本机局部入口几何依赖尚未就绪",
          mask_not_ready_or_stale: "气道掩膜未确认或版本已变化",
          case_context_unavailable: "当前病例几何暂时无法读取",
          mask_changed_during_generation: "生成期间掩膜版本发生变化",
          posterior_single_section_missing: "在候选深度未找到单一、足够大的鼻咽出口截面；需对照 CT 核对覆盖范围与掩膜连通性",
          geometry_or_preflight_unavailable: "当前几何未通过局部入口技术预检，无法可靠提案",
        };
        const reason = typeof data.reason === "string" ? reasons[data.reason] || "当前几何条件不满足自动提案要求" : "几何条件未满足";
        notice(`本机未生成候选：${reason}。请手动选面或刷新病例后重试；没有保存正式端面。`, true);
        return;
      }
      const proposals = data?.proposals;
      if (data?.status !== "available" || !Array.isArray(proposals) || !proposals.length ||
          proposals.length > 8 || !/^proposal-[0-9a-f]{32}$/.test(data.proposal_id) ||
          data.proposal?.proposal_id !== data.proposal_id || !current(data.proposal) ||
          !Object.hasOwn(labels, data.proposal.status) || proposals[0]?.proposal_id !== data.proposal_id ||
          new Set(proposals.map(item => item?.proposal_id)).size !== proposals.length ||
          proposals.some(item => !/^proposal-[0-9a-f]{32}$/.test(item?.proposal_id) ||
            !current(item) || !Object.hasOwn(labels, item.status)) ||
          data.human_review_required !== true || data.committed !== false) {
        throw new Error("本机候选返回的数据与当前病例不一致或无效");
      }
      state.pending = state.autoGenerating = false;
      await refresh(data.proposal_id);
      if (epoch === state.epoch && key === state.key && active() && !state.failed) {
        if (state.selected !== data.proposal_id || proposals.some(item => !state.items.some(saved =>
          saved.proposal_id === item.proposal_id && current(saved)))) {
          notice("本机候选返回，但刷新后未找到全部记录。请刷新核对后重试。", true);
        }
        else notice(`本机已返回 ${proposals.length} 条单支局部入口候选。可逐条加载并对照 CT 核对；正式端面尚未保存，当前不能用于双侧鼻呼吸 CFD 算例。`);
      }
    } catch (error) {
      if (request !== state.request || key !== state.key || epoch !== state.epoch || !active()) return;
      notice(`本机候选未确认：${error.message || "网络不可用"}。请刷新核对记录后重试。`, true);
    } finally {
      if (epoch === state.epoch && key === state.key && active()) {
        state.pending = state.autoGenerating = false; state.controller = null; render();
      }
    }
  }

  async function loadDraft() {
    const item = selected();
    if (!active() || state.loading || state.pending || !state.context.canEdit || !current(item)) return;
    clearConfirmation();
    try {
      const loaded = state.context.applyDraft?.(draftPlan(item.plan));
      if (loaded === false) { notice("保留当前草稿，候选尚未加载。"); render(); return; }
      state.loadedId = item.proposal_id;
      state.loadedSignature = signature(draft());
      state.draftSignature = state.loadedSignature;
      notice("候选已加载到草稿，已触发端面预览。请核对 CT 后勾选确认，再记录反馈；正式端面尚未保存。");
    } catch (error) { notice(`候选未加载：${error.message || "当前编辑器不可用"}`, true); }
    render();
  }

  async function submit(status) {
    const item = selected();
    if (!canAct(item) || !$("confirm").checked) return;
    const value = draft();
    if (status === "accepted" && (value.error || state.loadedId !== item.proposal_id ||
        state.loadedSignature !== signature(value))) {
      notice("请先加载并核对此候选；草稿已有改动时，请使用“以当前草稿修正”。", true); render(); return;
    }
    if (status === "adjusted" && (value.error || !validPlan(value.plan, state.context))) {
      notice(value.error || "请完成当前两端草稿及局部入口选点，再记录修正。", true); render(); return;
    }
    const context = state.context, key = state.key, request = ++state.request, epoch = state.epoch;
    state.pending = true;
    state.controller = new AbortController();
    const feedback = { reason_code: "human_reviewed" };
    if (status === "adjusted") feedback.adjusted_plan = draftPlan(value.plan);
    clearConfirmation(); notice("正在记录人工反馈…"); render();
    try {
      const response = await fetch(`/api/cases/${encodeURIComponent(context.caseId)}/segment/endplanes/learning/feedback`, {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proposal_id: item.proposal_id, status, feedback }), signal: state.controller.signal,
      });
      const data = await responseJson(response);
      if (request !== state.request || key !== state.key || !active()) return;
      if (!current(data) || data.proposal_id !== item.proposal_id || data.status !== status) {
        throw new Error("反馈返回的病例、范围、掩膜、候选或状态与本次提交不一致或无效");
      }
      state.pending = false;
      await refresh(item.proposal_id);
      if (epoch === state.epoch && key === state.key && active()) {
        const refreshed = state.items.find(proposal => proposal.proposal_id === item.proposal_id);
        if (state.failed) notice(`反馈未确认记录：${state.message}。请刷新核对状态。`, true);
        else if (!current(refreshed) || refreshed.status !== status) {
          notice("反馈未确认记录：刷新后的目标候选缺失或状态与本次反馈不一致。请刷新核对状态后重试。", true);
        } else notice(`已记录“${labels[status]}”反馈；正式端面仍需在编辑器中单独保存。`);
      }
    } catch (error) {
      if (request !== state.request || key !== state.key || !active()) return;
      notice(`反馈未确认记录：${error.message || "网络不可用"}。请刷新核对状态后重试。`, true);
    } finally {
      if (epoch === state.epoch && key === state.key && active()) { state.pending = false; state.controller = null; render(); }
    }
  }

  function setContext(context = {}) {
    const key = typeof context.caseId === "string" && ["main_airway", "full_candidate"].includes(context.scope) &&
      Number.isInteger(context.maskVersion) && context.maskVersion >= 0
      ? `${context.caseId}|${context.scope}|${context.maskVersion}` : null;
    const changed = key !== state.key;
    const closing = !context.isOpen && state.open;
    if (changed || closing) {
      state.epoch += 1;
      state.controller?.abort(); state.controller = null; state.request += 1;
      state.loading = state.pending = state.proposing = state.autoGenerating = false;
      clearConfirmation(); state.loadedId = state.loadedSignature = state.draftSignature = null;
      if (changed) { state.items = []; state.selected = null; state.fetched = false; }
    }
    if (state.context?.canEdit && !context.canEdit) clearConfirmation();
    state.context = context; state.key = key; state.open = Boolean(context.isOpen);
    if (changed) populate();
    const draftSignature = signature(draft());
    if (state.draftSignature !== draftSignature) {
      clearConfirmation(); state.loadedId = state.loadedSignature = null;
      state.draftSignature = draftSignature;
    }
    if (!key) notice("请先确认当前病例的气道掩膜，再读取候选与纠错记录。");
    else if (!state.fetched && !state.loading && state.open) { refresh(); return; }
    render();
  }

  $("refresh").addEventListener("click", refresh);
  $("select").addEventListener("change", () => {
    state.selected = $("select").value; state.loadedId = state.loadedSignature = null;
    clearConfirmation(); notice("请加载所选候选并核对 CT，再记录反馈。"); render();
  });
  $("load").addEventListener("click", loadDraft);
  $("auto").addEventListener("click", generateAutoCandidate);
  $("register").addEventListener("click", registerDraft);
  $("confirm").addEventListener("change", render);
  for (const [id, status] of [["accept", "accepted"], ["adjust", "adjusted"], ["reject", "rejected"]]) {
    $(id).addEventListener("click", () => submit(status));
  }
  window.WindcatcherEndplaneLearning = { setContext };
  setContext();
})();
