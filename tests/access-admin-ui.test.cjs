"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static", "access-admin.js"), "utf8");
const appSource = fs.readFileSync(path.join(root, "static", "app.js"), "utf8");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");
const A = "a".repeat(32);
const B = "b".repeat(32);
const C = "c".repeat(32);
const CASE = "case-20261102-120000-abcd";

function element(id = "", tag = "div") {
  const classes = new Set();
  return {
    id, tagName: tag.toUpperCase(), children: [], listeners: {}, value: "", textContent: "",
    disabled: false, hidden: ["page-accounts", "case-grants-panel"].includes(id),
    classList: { toggle(name, on) { if (on) classes.add(name); else classes.delete(name); }, contains(name) { return classes.has(name); } },
    addEventListener(name, listener) { this.listeners[name] = listener; },
    setAttribute(name, value) { this[name] = value; },
    replaceChildren(...children) { this.children = children; if (this.tagName === "SELECT") this.value = children[0]?.value || ""; },
    append(...children) { this.children.push(...children); },
  };
}

function response(status, data) {
  return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(data) };
}

function harness({ multiuser = true, admin = true, owner = true, holdUsers = false } = {}) {
  const ids = ["page-accounts", "case-grants-panel", "accounts-refresh", "account-create-form",
    "account-create-submit", "account-username", "account-password", "account-role", "accounts-status",
    "accounts-list", "grants-case", "grants-refresh", "grants-status", "grants-list", "grant-form",
    "grant-user-id", "grant-role", "grant-submit", "grant-users"];
  const nodes = Object.fromEntries(ids.map(id => [id, element(id, id === "grants-case" ? "select" : "div")]));
  nodes["account-role"].value = "member";
  nodes["grant-role"].value = "viewer";
  nodes["grant-submit"].disabled = true;
  nodes["account-create-submit"].disabled = true;
  const nav = element("accounts-nav", "button");
  nav.hidden = true;
  const users = [
    { id: A, username: "owner", global_role: "admin", kind: "human", active: true },
    { id: B, username: "member", global_role: "member", kind: "human", active: true },
  ];
  const grants = [
    { id: A, username: "owner", role: "owner", active: true },
    { id: B, username: "member", role: "viewer", active: true },
  ];
  let releaseUsers;
  const userGate = new Promise(resolve => { releaseUsers = resolve; });
  const calls = [];
  const window = { WindcatcherMultiuser: multiuser, WindcatcherCloudAccess: true, confirm: () => true };
  const context = vm.createContext({
    window, document: {
      getElementById: id => nodes[id],
      querySelector: () => nav,
      createElement: tag => element("", tag),
    },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      const method = options.method || "GET";
      if (url === "/api/admin/users" && method === "GET") {
        if (holdUsers) await userGate;
        return admin ? response(200, users) : response(403, { detail: { reason: "需要账号管理员权限" } });
      }
      if (url === "/api/admin/users" && method === "POST") {
        const body = JSON.parse(options.body);
        const created = { id: C, username: body.username, global_role: body.global_role, kind: "human", active: true };
        users.push(created);
        return response(201, created);
      }
      if (url === "/api/admin/users/" + B + "/disable" && method === "POST") {
        users.find(user => user.id === B).active = false;
        return response(200, { status: "disabled" });
      }
      if (url === "/api/cases/" + CASE + "/grants" && method === "GET") {
        return owner ? response(200, grants) : response(403, { detail: { reason: "case owner required" } });
      }
      if (url.startsWith("/api/cases/" + CASE + "/grants/") && method === "PUT") {
        const id = url.split("/").at(-1);
        const role = JSON.parse(options.body).role;
        const found = grants.find(grant => grant.id === id);
        if (found) found.role = role;
        else grants.push({ id, username: "new-member", role, active: true });
        return response(200, { status: "granted", role });
      }
      if (url === "/api/cases/" + CASE + "/grants/" + B && method === "POST" &&
          options.headers["X-Windcatcher-Method"] === "DELETE") {
        grants.splice(grants.findIndex(grant => grant.id === B), 1);
        return response(200, { status: "revoked" });
      }
      throw Error("Unexpected request: " + method + " " + url);
    },
  });
  vm.runInContext(source, context);
  return { nodes, nav, calls, users, grants, releaseUsers, adminUi: window.WindcatcherAccessAdmin };
}

test("account navigation and case authorization panel are wired into existing pages", () => {
  assert.match(index, /data-route="accounts"[^>]*hidden/);
  assert.match(index, /id="page-accounts"[^>]*hidden/);
  assert.match(index, /id="case-grants-panel"[^>]*hidden/);
  assert.match(index, /static\/access-admin\.js/);
  assert.match(appSource, /WindcatcherAccessAdmin\?\.activateAccounts/);
  assert.match(appSource, /WindcatcherAccessAdmin\?\.activateCaseGrants/);
});

test("local or shared mode does not query personal account endpoints", async () => {
  const h = harness({ multiuser: false });
  assert.equal(await h.adminUi.probeAdmin(), false);
  h.adminUi.activateCaseGrants([{ case_id: CASE }], CASE);
  assert.equal(h.calls.length, 0);
  assert.equal(h.nav.hidden, true);
  assert.equal(h.nodes["case-grants-panel"].hidden, true);
});

test("admin creation and disable use the existing endpoints without leaking the password into the list", async () => {
  const h = harness();
  await h.adminUi.probeAdmin();
  assert.equal(h.nav.hidden, false);
  assert.equal(h.nodes["page-accounts"].hidden, false);
  assert.equal(h.nodes["accounts-list"].children.length, 2);
  h.nodes["account-username"].value = "new-member";
  h.nodes["account-password"].value = "long-password-123456";
  await h.nodes["account-create-form"].listeners.submit({ preventDefault() {} });
  const created = h.calls.find(call => call.url === "/api/admin/users" && call.options.method === "POST");
  assert.deepEqual(JSON.parse(created.options.body), {
    username: "new-member", password: "long-password-123456", global_role: "member",
  });
  assert.equal(h.nodes["account-password"].value, "");
  assert.equal(h.nodes["accounts-list"].children.length, 3);
  assert.equal(h.nodes["accounts-list"].children.some(row => row.children[0].children[1].textContent.includes("long-password")), false);
  const memberRow = h.nodes["accounts-list"].children.find(row => row.children[0].children[0].textContent === "member");
  await memberRow.children[1].listeners.click();
  assert.equal(h.users.find(user => user.id === B).active, false);
  assert.equal(h.calls.some(call => call.url === "/api/admin/users/" + B + "/disable" && call.options.method === "POST"), true);
});

test("non-admin owner can manage known account IDs without access to the account directory", async () => {
  const h = harness({ admin: false });
  assert.equal(await h.adminUi.probeAdmin(), false);
  assert.equal(h.nav.hidden, true);
  await h.adminUi.activateCaseGrants([{ case_id: CASE }], CASE);
  assert.equal(h.nodes["case-grants-panel"].hidden, false);
  assert.equal(h.nodes["grant-submit"].disabled, false);
  assert.equal(h.nodes["grants-list"].children.length, 2);
  assert.equal(h.nodes["grant-users"].children.length, 0);
  h.nodes["grant-user-id"].value = C;
  h.nodes["grant-role"].value = "reviewer";
  await h.nodes["grant-form"].listeners.submit({ preventDefault() {} });
  const grant = h.calls.find(call => call.options.method === "PUT");
  assert.equal(grant.url, "/api/cases/" + CASE + "/grants/" + C);
  assert.deepEqual(JSON.parse(grant.options.body), { role: "reviewer" });
  assert.equal(h.grants.find(row => row.id === C).role, "reviewer");
});

test("revocation uses the precise cloud method override and refreshes grants", async () => {
  const h = harness();
  await h.adminUi.activateCaseGrants([{ case_id: CASE }], CASE);
  const memberRow = h.nodes["grants-list"].children.find(row => row.children[0].children[0].textContent === "member");
  await memberRow.children[1].listeners.click();
  const revoked = h.calls.find(call => call.url === "/api/cases/" + CASE + "/grants/" + B);
  assert.equal(revoked.options.method, "POST");
  assert.equal(revoked.options.headers["X-Windcatcher-Method"], "DELETE");
  assert.equal(h.grants.some(row => row.id === B), false);
  assert.equal(h.nodes["grants-list"].children.length, 1);
});

test("loading and denied states disable destructive controls and show the server error", async () => {
  const loading = harness({ holdUsers: true });
  const pending = loading.adminUi.probeAdmin();
  assert.match(loading.nodes["accounts-status"].textContent, /正在读取/);
  assert.equal(loading.nodes["accounts-refresh"].disabled, true);
  loading.releaseUsers();
  await pending;
  assert.equal(loading.nodes["accounts-refresh"].disabled, false);

  const denied = harness({ admin: false, owner: false });
  await denied.adminUi.probeAdmin();
  await denied.adminUi.activateCaseGrants([{ case_id: CASE }], CASE);
  assert.equal(denied.nav.hidden, true);
  assert.equal(denied.nodes["grant-submit"].disabled, true);
  assert.match(denied.nodes["grants-status"].textContent, /case owner required/);
});
