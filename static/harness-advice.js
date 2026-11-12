/* 只将服务端白名单代码映射为文案；不渲染任意模型文本或病例内容。 */
"use strict";

(function () {
  const panel = document.getElementById("harness-advice-panel");
  const button = document.getElementById("harness-advice-generate");
  const status = document.getElementById("harness-advice-status");
  const result = document.getElementById("harness-advice-result");
  const next = document.getElementById("harness-advice-next");
  const recap = document.getElementById("harness-advice-recap");
  const draft = document.getElementById("harness-advice-draft");
  const proof = document.getElementById("harness-advice-proof");
  const metering = document.getElementById("harness-advice-metering");
  const caseLabel = document.getElementById("harness-advice-case");
  if (!panel || !button || !status || !result || !next || !recap || !draft || !proof || !metering) return;

  const enabled = window.WindcatcherMultiuser === true;
  const pilotReady = window.WindcatcherHarnessEnabled === true;
  const stages = Object.freeze({
    import: "导入 CT", anonymize: "本地去标识", qc: "输入质控",
    segment: "气道分割", surface: "几何质检", cfd_prep: "CFD 准备",
    cfd_run: "CFD 求解与数值检查", report: "报告",
  });
  const steps = Object.freeze({
    review_failed_stage: "检查失败或受阻的阶段",
    review_numeric_gate: "检查 CFD 数值闸门",
    review_warning_stage: "人工检查警告阶段",
    review_preparation: "复核基准算例准备条件",
    review_eligibility: "复核分享与使用条件",
    review_next_stage: "复核下一处理阶段",
    review_report: "人工复核报告",
  });
  const reasons = Object.freeze({
    stage_failure: "存在失败或受阻阶段",
    stage_warning: "存在警告阶段",
    baseline_blocked: "基准算例尚未就绪",
    numeric_gate_failed: "数值检查未通过",
    eligibility_not_met: "使用条件未满足",
  });
  const draftSteps = Object.freeze({
    qc: "输入质控", segment: "气道分割", surface: "几何质检",
    cfd_prep: "CFD 准备", cfd_results: "CFD 结果", report: "报告", review: "人工复核",
  });
  const triggers = Object.freeze({ manual: "手动检查时", numeric_gate_failed: "数值检查未通过时" });
  let caseId = null;
  let requestVersion = 0;
  let pending = false;
  panel.hidden = !enabled;

  function showStatus(message, isError = false) {
    status.textContent = message;
    status.classList.toggle("is-error", isError);
  }

  function setCase(value) {
    requestVersion += 1;
    caseId = enabled && typeof value === "string" && value ? value : null;
    if (caseLabel) caseLabel.textContent = caseId ? "当前工作台病例：" + caseId : "尚未在工作台选择病例。";
    pending = false;
    button.disabled = !caseId || !pilotReady;
    button.classList.toggle("is-busy", false);
    button.setAttribute("aria-busy", "false");
    result.hidden = true;
    next.textContent = "";
    recap.textContent = "";
    draft.textContent = "";
    proof.textContent = "";
    metering.textContent = "";
    showStatus(!pilotReady ? "DeepSeek Harness 只读试点尚未启用。"
      : caseId ? "点击按钮获取当前病例的只读建议。" : "请先选择病例。");
  }

  function validAdvice(data, expectedCaseId) {
    return data && data.status === "advisory_only" && data.source === "deepseek-harness"
      && data.case_id === expectedCaseId
      && data.human_review_required === true && data.automated_action === false
      && /^[a-f0-9]{64}$/.test(data.input_sha256)
      && data.next_step && Object.hasOwn(steps, data.next_step.code)
      && Object.hasOwn(stages, data.next_step.stage)
      && Array.isArray(data.failure_recap)
      && data.failure_recap.every(code => Object.hasOwn(reasons, code))
      && data.skill_draft && Object.hasOwn(triggers, data.skill_draft.trigger_condition)
      && Array.isArray(data.skill_draft.suggested_steps)
      && data.skill_draft.suggested_steps.length >= 1 && data.skill_draft.suggested_steps.length <= 3
      && data.skill_draft.suggested_steps.every(code => Object.hasOwn(draftSteps, code))
      && Array.isArray(data.skill_draft.allowed_tools)
      && (data.skill_draft.allowed_tools.length === 0
          || (data.skill_draft.allowed_tools.length === 1 && data.skill_draft.allowed_tools[0] === "inspect_cfd_case"));
  }

  function usageLabel(value) {
    const tokenCount = number => Number.isSafeInteger(number) && number >= 0;
    if (!value || value.source !== "deepseek-harness-sdk-events"
        || value.status !== "reported"
        || !tokenCount(value.input_tokens)
        || !tokenCount(value.output_tokens)
        || !tokenCount(value.total_tokens)) {
      return "本次 token 用量未完整返回，不能按 0 计算；费用待账单核对。";
    }
    const count = number => number.toLocaleString("zh-CN");
    const cached = tokenCount(value.cache_read_tokens)
      ? "，缓存读取 " + count(value.cache_read_tokens) : "";
    return "本次模型用量（SDK 事件）：输入 " + count(value.input_tokens)
      + "，输出 " + count(value.output_tokens) + "，合计 "
      + count(value.total_tokens) + cached + " token；费用待账单核对。";
  }

  async function generate() {
    if (!enabled || !pilotReady || !caseId || pending) return;
    const targetCaseId = caseId;
    const version = ++requestVersion;
    pending = true;
    button.disabled = true;
    button.classList.toggle("is-busy", true);
    button.setAttribute("aria-busy", "true");
    result.hidden = true;
    showStatus("正在读取当前病例的状态摘要…");
    try {
      const response = await fetch(`/api/cases/${encodeURIComponent(targetCaseId)}/harness-advice`, {
        method: "POST", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
      });
      if (response.status === 503) throw new Error("只读建议服务未启用，请检查服务配置后重试。");
      if (response.status === 401 || response.status === 403) throw new Error("当前账号无权获取此病例建议，请重新登录或检查病例授权。");
      if (response.status === 404) throw new Error("病例不存在或当前账号无权访问，请重新选择病例。");
      if (response.status === 409) throw new Error("病例状态已变化，请刷新病例后重试。");
      if (!response.ok) throw new Error(`建议请求失败（HTTP ${response.status}），请稍后重试。`);
      const data = await response.json();
      if (version !== requestVersion || targetCaseId !== caseId) return;
      if (!validAdvice(data, targetCaseId)) throw new Error("建议响应不符合只读契约，请检查服务状态。");
      next.textContent = `${steps[data.next_step.code]} · ${stages[data.next_step.stage]}`;
      recap.textContent = data.failure_recap.length
        ? data.failure_recap.map(code => reasons[code]).join("；") + "。"
        : "本次状态摘要未列出失败或警告；仍须人工复核。";
      draft.textContent = `${triggers[data.skill_draft.trigger_condition]}建议复核：${data.skill_draft.suggested_steps.map(code => draftSteps[code]).join(" → ")}。仅供确认，不会保存或执行。`;
      proof.textContent = `状态摘要 SHA-256：${data.input_sha256.slice(0, 16)}…`;
      metering.textContent = usageLabel(data.metering);
      result.hidden = false;
      showStatus("建议已生成；仅供人工复核。");
    } catch (error) {
      if (version !== requestVersion || targetCaseId !== caseId) return;
      showStatus(error instanceof Error ? error.message : "建议暂时不可用，请重试。", true);
    } finally {
      if (version === requestVersion && targetCaseId === caseId) {
        pending = false;
        button.disabled = false;
        button.classList.toggle("is-busy", false);
        button.setAttribute("aria-busy", "false");
      }
    }
  }

  button.addEventListener("click", generate);
  window.WindcatcherHarnessAdvice = { setCase };
  setCase(null);
})();
