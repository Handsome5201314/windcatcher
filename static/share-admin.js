"use strict";
(() => {
  const byId = id => document.getElementById(id);
  let caseId = "";
  let requestId = 0;
  let gateEligible = false;
  const status = (message, error = false) => {
    const target = byId("admin-status");
    target.textContent = message;
    target.classList.toggle("is-error", error);
  };
  async function request(path, options) {
    const response = await fetch(path, { cache: "no-store", ...options });
    const body = await response.text();
    let result;
    try { result = JSON.parse(body); }
    catch { throw new Error(`服务响应异常（HTTP ${response.status}）`); }
    if (!response.ok) throw new Error(result.detail?.reason || result.reason || `操作失败（HTTP ${response.status}）`);
    return result;
  }
  const path = (suffix, selected = caseId) => "/api/cases/" + encodeURIComponent(selected) + suffix;
  const row = (...items) => {
    const p = document.createElement("p");
    p.className = "share-record";
    p.textContent = items.join(" · ");
    return p;
  };
  function renderGate(gate) {
    const checks = Array.isArray(gate?.checks) ? gate.checks : [];
    const pending = checks.filter(check => check.status !== "ok");
    gateEligible = gate?.eligible === true && gate?.numeric_ok === true;
    const summary = gateEligible ? "结果检查已通过，可以提交发布审核。"
      : "暂不能发布：" + (pending.length ? pending.map(check => check.name).join("、")
        : (gate?.reasons || []).join("；") || "结果检查未通过");
    const statusNode = byId("share-gate-status");
    statusNode.textContent = summary;
    statusNode.classList.toggle("is-error", !gateEligible);
    byId("share-gate-checks").replaceChildren(...checks.map(check => row(
      check.name || "检查", check.status === "ok" ? "通过" : "待复核",
      typeof check.detail === "string" ? check.detail : check.detail?.reason || "")));
    byId("share-gate").hidden = false;
    byId("publish-submit").disabled = !gateEligible;
  }
  function renderGateError(error) {
    gateEligible = false;
    const statusNode = byId("share-gate-status");
    statusNode.textContent = "无法读取结果检查：" + (error.message || "请刷新后重试");
    statusNode.classList.toggle("is-error", true);
    byId("share-gate-checks").replaceChildren();
    byId("share-gate").hidden = false;
    byId("publish-submit").disabled = true;
  }
  async function refresh(selected = caseId) {
    const reviewsRequest = request(path("/review-events", selected));
    const sharesRequest = request(path("/shares", selected));
    const gateRequest = request(path("/gate", selected)).then(value => ({ value }), error => ({ error }));
    const [reviews, shares, gateResult] = await Promise.all([
      reviewsRequest, sharesRequest, gateRequest,
    ]);
    if (selected !== caseId) return;
    if (gateResult.error) renderGateError(gateResult.error);
    else renderGate(gateResult.value);
    byId("reviews").replaceChildren(...(reviews.length ? reviews.map(item => row(
      new Date(item.created_at * 1000).toLocaleString(), item.stage, item.action, item.reason)) : [row("暂无人工处理记录")]));
    const container = byId("shares");
    container.replaceChildren();
    if (!shares.length) container.append(row("暂无已发布链接"));
    for (const share of shares) {
      const item = document.createElement("div");
      item.className = "share-record";
      item.append(row(share.mode === "test" ? "未验证测试" : "正式发布", "版本 " + share.source_signature.slice(0, 12),
        "到期 " + new Date(share.expires_at * 1000).toLocaleString(),
        share.revoked_at ? "已撤销" : Date.now() / 1000 >= share.expires_at ? "已过期" : "有效"));
      if (!share.revoked_at && Date.now() / 1000 < share.expires_at) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button button-quiet button-compact";
        button.textContent = "撤销此链接";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try { await request(path("/shares/" + share.id + "/revoke"), { method: "POST" }); await refresh(); status("链接已撤销"); }
          catch (error) { button.disabled = false; status(error.message, true); }
        });
        item.append(button);
      }
      container.append(item);
    }
  }
  async function submit(form, action, success = "已保存") {
    const button = form.querySelector("button[type=submit], button:not([type])");
    button.disabled = true;
    status("处理中…");
    try { await action(); status(success); }
    catch (error) { status(error.message, true); }
    finally { button.disabled = form === byId("publish-form") ? !gateEligible : false; }
  }
  function syncCases(cases, currentCaseId) {
    const select = byId("case-id");
    const previous = select.value;
    select.replaceChildren(new Option("选择病例…", ""), ...(cases || []).slice().reverse().map(item =>
      new Option(item.case_id + (item.synthetic ? " · 合成" : ""), item.case_id)));
    const valid = value => cases?.some(item => item.case_id === value);
    select.value = valid(previous) ? previous : valid(currentCaseId) ? currentCaseId : "";
    if (!select.value) {
      caseId = "";
      gateEligible = false;
      byId("admin-content").hidden = true;
      byId("share-gate").hidden = true;
      byId("publish-submit").disabled = true;
      status(cases?.length ? "请选择病例后读取记录。" : "暂无病例，请先在病例页导入。", false);
    }
  }
  async function loadSelectedCase() {
    const selected = byId("case-id").value;
    if (!selected) { status("请先选择病例。", true); return; }
    const currentRequest = ++requestId;
    caseId = selected;
    gateEligible = false;
    byId("admin-content").hidden = true;
    byId("share-gate").hidden = true;
    byId("publish-submit").disabled = true;
    byId("issued").hidden = true;
    byId("test-issued").hidden = true;
    await submit(byId("case-form"), async () => {
      await refresh(selected);
      if (currentRequest === requestId && caseId === selected) byId("admin-content").hidden = false;
    }, "记录已更新");
  }
  function activate(cases, currentCaseId) {
    syncCases(cases, currentCaseId);
    if (byId("case-id").value) return loadSelectedCase();
  }
  byId("case-form").addEventListener("submit", event => {
    event.preventDefault();
    return loadSelectedCase();
  });
  byId("case-id").addEventListener("change", loadSelectedCase);
  byId("share-open-cfd-review").addEventListener("click", () => { window.location.hash = "cfd-review"; });
  byId("review-form").addEventListener("submit", event => {
    event.preventDefault();
    return submit(event.currentTarget, async () => {
      await request(path("/review-events"), { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operator: byId("operator").value, stage: byId("review-stage").value, action: byId("review-action").value,
          reason: byId("review-reason").value }) });
      byId("review-reason").value = "";
      await refresh();
    });
  });
  byId("publish-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!gateEligible) { status("结果检查未通过，不能发布；请先到病例流场核对阻断项。", true); return; }
    return submit(event.currentTarget, async () => {
      const published = await request(path("/shares"), { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reviewer: byId("reviewer").value, review_note: byId("review-note").value,
          days: Number(byId("days").value) }) });
      byId("issued-link").value = location.origin + published.url;
      byId("issued-code").value = published.access_code;
      byId("issued").hidden = false;
      await refresh();
    });
  });
  byId("test-publish-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!byId("test-recipient-confirmed").checked) {
      status("请先确认患者本人同意接收未验证测试可视化。", true);
      return;
    }
    return submit(event.currentTarget, async () => {
      const published = await request(path("/shares/test"), { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operator: byId("test-operator").value, reason: byId("test-reason").value,
          recipient_confirmed: true }) });
      byId("test-issued-link").value = location.origin + published.url;
      byId("test-issued-code").value = published.access_code;
      byId("test-issued").hidden = false;
      byId("test-recipient-confirmed").checked = false;
      await refresh();
    }, "未验证测试链接已生成，24 小时内有效；请单独交付访问码。");
  });
  window.WindcatcherShareAdmin = { syncCases, activate };
})();
