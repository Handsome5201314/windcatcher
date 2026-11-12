"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const multiuser = () => window.WindcatcherMultiuser === true;
  const roles = { viewer: "仅查看", editor: "编辑", reviewer: "复核与分享", owner: "owner（管理授权）" };
  let users = [];
  let cases = [];
  let selectedCase = "";
  let grantsLoaded = false;
  let grantRequestId = 0;
  let adminRequest = null;
  let adminAllowed = false;
  let accountPending = false;
  let grantPending = false;
  let accountButtons = [];
  let grantButtons = [];

  function status(id, message, isError = false) {
    const node = byId(id);
    node.textContent = message;
    node.classList.toggle("is-error", isError);
  }

  async function request(path, options = {}) {
    let method = options.method || "GET";
    const headers = { ...(options.headers || {}) };
    if (method === "DELETE" && window.WindcatcherCloudAccess) {
      method = "POST";
      headers["X-Windcatcher-Method"] = "DELETE";
    }
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      cache: "no-store", credentials: "same-origin", ...options,
      method, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const raw = await response.text();
    let data;
    try { data = raw ? JSON.parse(raw) : null; }
    catch { throw Object.assign(new Error("服务响应格式异常（HTTP " + response.status + "）"), { status: response.status }); }
    if (!response.ok) {
      throw Object.assign(new Error(data?.detail?.reason || data?.reason || "请求失败（HTTP " + response.status + "）"), { status: response.status });
    }
    return data;
  }

  function option(label, value) {
    const node = document.createElement("option");
    node.textContent = label;
    node.value = value;
    return node;
  }

  function setAccountPending(pending) {
    accountPending = pending;
    byId("account-create-submit").disabled = pending || !adminAllowed;
    byId("accounts-refresh").disabled = pending;
    for (const button of accountButtons) button.disabled = pending;
  }

  function renderUserChoices() {
    byId("grant-users").replaceChildren(...users.filter(user => user.active && user.kind === "human")
      .map(user => option(user.username + " · " + (user.global_role === "admin" ? "管理员" : "成员"), user.id)));
  }

  function renderUsers() {
    const list = byId("accounts-list");
    list.replaceChildren();
    accountButtons = [];
    if (!users.length) {
      const empty = document.createElement("p");
      empty.textContent = "暂无账号。";
      list.append(empty);
    }
    for (const user of users) {
      const row = document.createElement("div");
      row.className = "access-row";
      const identity = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = user.username;
      const meta = document.createElement("small");
      meta.textContent = (user.kind === "service" ? "服务身份" : user.global_role === "admin" ? "账号管理员" : "普通成员")
        + " · " + (user.active ? "启用中" : "已停用") + " · ID " + user.id;
      identity.append(name, meta);
      row.append(identity);
      if (user.kind === "human" && user.active) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button button-quiet button-compact";
        button.textContent = "停用";
        button.setAttribute("aria-label", "停用账号 " + user.username);
        button.disabled = accountPending;
        button.addEventListener("click", () => disableUser(user, button));
        accountButtons.push(button);
        row.append(button);
      }
      list.append(row);
    }
    renderUserChoices();
  }

  async function refreshUsers({ initial = false } = {}) {
    if (!multiuser()) return false;
    if (adminRequest) return adminRequest;
    status("accounts-status", "正在读取账号…");
    byId("accounts-refresh").disabled = true;
    adminRequest = (async () => {
      try {
        const rows = await request("/api/admin/users");
        if (!Array.isArray(rows)) throw new Error("账号列表格式异常");
        users = rows;
        adminAllowed = true;
        document.querySelector('.side-nav button[data-route="accounts"]').hidden = false;
        byId("page-accounts").hidden = false;
        renderUsers();
        status("accounts-status", "已读取 " + rows.length + " 个账号。");
        return true;
      } catch (error) {
        if (error.status === 403 || error.status === 404) {
          adminAllowed = false;
          document.querySelector('.side-nav button[data-route="accounts"]').hidden = true;
          if (!initial) byId("page-accounts").hidden = false;
          status("accounts-status", "当前账号无账号管理权限。", true);
        } else {
          document.querySelector('.side-nav button[data-route="accounts"]').hidden = false;
          byId("page-accounts").hidden = false;
          status("accounts-status", "读取账号失败：" + error.message, true);
        }
        return false;
      } finally {
        byId("accounts-refresh").disabled = false;
        byId("account-create-submit").disabled = !adminAllowed || accountPending;
        adminRequest = null;
      }
    })();
    return adminRequest;
  }

  async function createUser(event) {
    event.preventDefault();
    if (!adminAllowed || accountPending) return;
    const username = byId("account-username").value.trim();
    const password = byId("account-password").value;
    const globalRole = byId("account-role").value;
    if (!username || !password || !["member", "admin"].includes(globalRole)) {
      status("accounts-status", "请填写账号名、初始密码及有效角色。", true);
      return;
    }
    setAccountPending(true);
    status("accounts-status", "正在创建账号…");
    try {
      await request("/api/admin/users", { method: "POST", body: { username, password, global_role: globalRole } });
      byId("account-password").value = "";
      byId("account-username").value = "";
      await refreshUsers();
      status("accounts-status", "账号 " + username + " 已创建。请通过安全渠道交付初始密码。", false);
    } catch (error) {
      status("accounts-status", "创建失败：" + error.message, true);
    } finally { setAccountPending(false); }
  }

  async function disableUser(user, button) {
    if (accountPending || !window.confirm("确认停用账号“" + user.username + "”？该账号现有会话会失效。")) return;
    setAccountPending(true);
    status("accounts-status", "正在停用 " + user.username + "…");
    try {
      await request("/api/admin/users/" + encodeURIComponent(user.id) + "/disable", { method: "POST" });
      await refreshUsers();
      status("accounts-status", "账号 " + user.username + " 已停用。", false);
    } catch (error) {
      status("accounts-status", "停用失败：" + error.message, true);
    } finally {
      setAccountPending(false);
      button.disabled = !user.active;
    }
  }

  function setGrantPending(pending) {
    grantPending = pending;
    byId("grants-case").disabled = pending;
    byId("grants-refresh").disabled = pending || !selectedCase;
    byId("grant-user-id").disabled = pending || !grantsLoaded;
    byId("grant-role").disabled = pending || !grantsLoaded;
    byId("grant-submit").disabled = pending || !grantsLoaded;
    for (const button of grantButtons) button.disabled = pending;
  }

  function renderGrants(rows) {
    const list = byId("grants-list");
    list.replaceChildren();
    grantButtons = [];
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.textContent = "尚无授权记录。";
      list.append(empty);
    }
    for (const grant of rows) {
      const row = document.createElement("div");
      row.className = "access-row";
      const identity = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = grant.username;
      const meta = document.createElement("small");
      meta.textContent = (roles[grant.role] || grant.role) + " · " + (grant.active ? "启用中" : "已停用") + " · ID " + grant.id;
      identity.append(name, meta);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button button-quiet button-compact";
      button.textContent = "撤销授权";
      button.setAttribute("aria-label", "撤销 " + grant.username + " 的病例授权");
      button.disabled = grantPending;
      button.addEventListener("click", () => revokeGrant(grant));
      grantButtons.push(button);
      row.append(identity, button);
      list.append(row);
    }
  }

  function syncCases(rows, currentCaseId) {
    if (!multiuser()) return;
    cases = Array.isArray(rows) ? rows : [];
    const select = byId("grants-case");
    const previous = select.value;
    select.replaceChildren(option("选择病例…", ""), ...cases.map(item => option(item.case_id, item.case_id)));
    const exists = value => cases.some(item => item.case_id === value);
    select.value = exists(previous) ? previous : exists(currentCaseId) ? currentCaseId : "";
    if (select.value !== selectedCase) {
      selectedCase = select.value;
      grantRequestId++;
      grantsLoaded = false;
      byId("grants-list").replaceChildren();
      status("grants-status", selectedCase ? "点击“读取授权”查看此病例。" : cases.length ? "请选择病例。" : "暂无病例，请先导入。", false);
      setGrantPending(false);
    }
  }

  async function loadGrants() {
    if (!multiuser() || grantPending) return;
    const caseId = byId("grants-case").value;
    if (!caseId) { status("grants-status", "请先选择病例。", true); return; }
    selectedCase = caseId;
    grantsLoaded = false;
    const requestId = ++grantRequestId;
    byId("grants-list").replaceChildren();
    setGrantPending(true);
    status("grants-status", "正在读取病例授权…");
    try {
      const rows = await request("/api/cases/" + encodeURIComponent(caseId) + "/grants");
      if (requestId !== grantRequestId) return;
      if (!Array.isArray(rows)) throw new Error("授权列表格式异常");
      grantsLoaded = true;
      renderGrants(rows);
      status("grants-status", "已读取 " + rows.length + " 条授权；只有本病例 owner 可以修改。", false);
    } catch (error) {
      if (requestId === grantRequestId) status("grants-status", "读取授权失败：" + error.message, true);
    } finally { if (requestId === grantRequestId) setGrantPending(false); }
  }

  async function saveGrant(event) {
    event.preventDefault();
    if (!grantsLoaded || grantPending || !selectedCase) return;
    const userId = byId("grant-user-id").value.trim().toLowerCase();
    const role = byId("grant-role").value;
    if (!/^[0-9a-f]{32}$/.test(userId) || !Object.hasOwn(roles, role)) {
      status("grants-status", "请输入有效的 32 位账号 ID 和病例权限。", true);
      return;
    }
    setGrantPending(true);
    status("grants-status", "正在保存授权…");
    try {
      await request("/api/cases/" + encodeURIComponent(selectedCase) + "/grants/" + encodeURIComponent(userId),
        { method: "PUT", body: { role } });
      byId("grant-user-id").value = "";
      setGrantPending(false);
      await loadGrants();
      status("grants-status", "病例授权已保存。", false);
    } catch (error) {
      status("grants-status", "保存授权失败：" + error.message, true);
    } finally { setGrantPending(false); }
  }

  async function revokeGrant(grant) {
    if (!grantsLoaded || grantPending || !selectedCase ||
        !window.confirm("确认撤销“" + grant.username + "”对本病例的访问权？")) return;
    setGrantPending(true);
    status("grants-status", "正在撤销授权…");
    try {
      await request("/api/cases/" + encodeURIComponent(selectedCase) + "/grants/" + encodeURIComponent(grant.id),
        { method: "DELETE" });
      setGrantPending(false);
      await loadGrants();
      status("grants-status", "已撤销 " + grant.username + " 的病例授权。", false);
    } catch (error) {
      status("grants-status", "撤销授权失败：" + error.message, true);
    } finally { setGrantPending(false); }
  }

  byId("accounts-refresh").addEventListener("click", () => refreshUsers());
  byId("account-create-form").addEventListener("submit", createUser);
  byId("grants-case").addEventListener("change", () => {
    selectedCase = byId("grants-case").value;
    grantsLoaded = false;
    grantRequestId++;
    byId("grants-list").replaceChildren();
    setGrantPending(false);
    if (selectedCase) return loadGrants();
    else status("grants-status", "请选择病例。", false);
  });
  byId("grants-refresh").addEventListener("click", loadGrants);
  byId("grant-form").addEventListener("submit", saveGrant);
  window.WindcatcherAccessAdmin = {
    probeAdmin: () => refreshUsers({ initial: true }),
    activateAccounts: () => refreshUsers(),
    syncCases,
    activateCaseGrants(rows, currentCaseId) {
      if (!multiuser()) return;
      byId("case-grants-panel").hidden = false;
      syncCases(rows, currentCaseId);
      if (selectedCase && !grantsLoaded) return loadGrants();
    },
  };
})();
