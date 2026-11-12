"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
const apiSource = source.slice(source.indexOf("async function api(path, options = {}) {"), source.indexOf("\nfunction busy("));

function file(size, name) {
  const blob = new Blob([Buffer.alloc(size, name === "001.dcm" ? 1 : 2)]);
  blob.name = name;
  blob.webkitRelativePath = "Selected/Series/" + name;
  return blob;
}

function harness({ loseAck = false, loseCompletionAck = false, alwaysFail = false, cloud = true } = {}) {
  const chunks = [];
  const calls = [];
  const received = [];
  let lost = false;
  let completionLost = false;
  const response = (body) => ({ ok: true, status: 200, json: async () => body });
  const context = vm.createContext({
    Blob, FormData, setTimeout: (fn) => { fn(); },
    window: { WindcatcherCloudAccess: cloud },
    fetch: async (url, options) => {
      calls.push(url);
      if (url === "/api/cases/upload") {
        assert.equal(cloud, false, "cloud must not send the whole folder in one request");
        assert.ok(options.body instanceof FormData);
        return response({ case_id: "multipart" });
      }
      if (url === "/api/uploads") {
        const manifest = JSON.parse(options.body);
        assert.deepEqual(manifest.files.map(x=>x.path), ["Series/001.dcm", "Series/002.dcm"]);
        return response({ upload_id: "upload-test", chunk_size: 4 * 1024 * 1024 });
      }
      if (url.includes("/chunks?")) {
        assert.equal(options.headers["Content-Type"], "application/octet-stream");
        assert.ok(options.body instanceof Blob);
        assert.ok(options.body.size <= 4 * 1024 * 1024);
        const bytes = Buffer.from(await options.body.arrayBuffer());
        const offset = Number(new URL(url, "https://test").searchParams.get("offset"));
        chunks.push({ offset, bytes });
        if (alwaysFail) throw new TypeError("Failed to fetch");
        const stored = Buffer.concat(received);
        if (offset < stored.length) assert.ok(stored.subarray(offset,offset+bytes.length).equals(bytes));
        else { assert.equal(offset, stored.length); received.push(bytes); }
        if (loseAck && !lost) { lost = true; throw new TypeError("Failed to fetch"); }
        return response({ received_bytes: Buffer.concat(received).length });
      }
      if (url.endsWith("/cancel")) return response({ status: "cancelled" });
      if (url.endsWith("/complete")) {
        if (loseCompletionAck && !completionLost) { completionLost = true; throw new TypeError("Failed to fetch"); }
        return response({ case_id: "chunked" });
      }
      throw new Error("Unexpected URL " + url);
    },
  });
  vm.runInContext(apiSource, context);
  return { context, chunks, calls, received };
}

test("cloud streams a folder in bounded chunks and preserves file bytes and paths", async () => {
  const h = harness({ loseAck: true });
  const files = [file(3 * 1024 * 1024, "001.dcm"), file(3 * 1024 * 1024, "002.dcm")];
  const progress = [];
  const result = await h.context.apiUploadCase(files, true, true, (done,total)=>progress.push([done,total]));
  assert.equal(result.case_id, "chunked");
  assert.equal(h.chunks.length, 3); // First chunk retries after its acknowledgement is lost.
  const expected = Buffer.concat(await Promise.all(files.map(async f=>Buffer.from(await f.arrayBuffer()))));
  assert.ok(Buffer.concat(h.received).equals(expected));
  assert.deepEqual(progress.at(-1), [expected.length, expected.length]);
});

test("network failure is explained, preserves the input, and cancels only its upload", async () => {
  const h = harness({ alwaysFail: true });
  const files = [file(4, "001.dcm"), file(4, "002.dcm")];
  await assert.rejects(h.context.apiUploadCase(files, true, true), /上传连接中断/);
  assert.equal(files.length, 2);
  assert.ok(h.calls.includes("/api/uploads/upload-test/cancel"));
  assert.equal(h.calls.includes("/api/uploads/upload-test/complete"), false);
});

test("existing non-cloud uploads retain multipart transport", async () => {
  const h = harness({ cloud: false });
  const result = await h.context.apiUploadCase([file(4,"001.dcm"),file(4,"002.dcm")], true, true);
  assert.equal(result.case_id, "multipart");
  assert.deepEqual(h.calls, ["/api/cases/upload"]);
});

test("a lost completion acknowledgement retries the same upload", async () => {
  const h = harness({ loseCompletionAck: true });
  const result = await h.context.apiUploadCase([file(4,"001.dcm"),file(4,"002.dcm")], true, true);
  assert.equal(result.case_id, "chunked");
  assert.equal(h.calls.filter(url=>url.endsWith("/complete")).length, 2);
  assert.equal(h.calls.filter(url=>url === "/api/uploads").length, 1);
});
