"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
function productionFunction(name) {
  const start = source.match(new RegExp(`^(?:async )?function ${name}\\(`, "m")).index;
  return source.slice(start, source.indexOf("\n}", start) + 2);
}
function fixture() {
  const elements = new Map();
  const state = {
    cases: [{ case_id: "solved-case", stages: { cfd_run: "warn", segment: "ok" } }],
    caseId: "other-case", case: { case_id: "other-case", stages: { segment: { status: "warn" } } },
    ctViewer: { meta: { mask_review: { need_human_confirm: true }, mask_external_air_suspected: true } },
    cfdResults: { caseId: "solved-case", actionPending: false },
  };
  const context = vm.createContext({
    state, renderCfdValidation() {},
    $: (id) => {
      if (!elements.has(id)) elements.set(id, { hidden: false, disabled: false, textContent: "", innerHTML: "", title: "" });
      return elements.get(id);
    },
  });
  vm.runInContext(["escapeHtml", "cfdResultsCaseRecord", "cfdCanPostprocess", "renderCfdResultState"].map(productionFunction).join("\n"), context);
  return { context, state, elements };
}

test("actual summary contract enables generation from real-field availability, independent of another CT viewer", () => {
  const { context, elements } = fixture();
  const record = context.cfdResultsCaseRecord();
  assert.equal(record.stages.cfd_run, "warn");
  context.renderCfdResultState({
    status: "missing", generation: { can_generate: true, timestep: "1000", reason: "最终时间步 1000 已存在，无需重新求解" },
    validation: { eligible: false, numeric_ok: false },
  }, record);
  assert.equal(elements.get("btn-cfd-postprocess").disabled, false);
  assert.match(elements.get("cfd-results-state").innerHTML, /求解场已存在|无需重新求解/);
  assert.doesNotMatch(elements.get("cfd-results-state").innerHTML, /请先完成 CFD 求解/);
});

test("full selected case takes precedence over summary while other cases keep their own identity", () => {
  const { context, state } = fixture();
  state.case = { case_id: "solved-case", stages: { cfd_run: { status: "warn", data: { solved: true } } } };
  assert.equal(context.cfdResultsCaseRecord().stages.cfd_run.data.solved, true);
  assert.equal(context.cfdResultsCaseRecord("missing-case"), null);
});

test("missing-field reason is displayed and escaped instead of inventing an uncompleted solve", () => {
  const { context, elements } = fixture();
  context.renderCfdResultState({ status: "missing", generation: {
    can_generate: false, reason: "最终时间步 1000 缺少 U 场 <script>", remediation: ["检查场与网格是否完整"],
  } }, context.cfdResultsCaseRecord());
  assert.equal(elements.get("btn-cfd-postprocess").disabled, true);
  const html = elements.get("cfd-results-state").innerHTML;
  assert.match(html, /缺少 U|检查场与网格是否完整|&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>|请先完成 CFD 求解/);
});

test("an old completed-stage summary never overrides blocked or absent API availability", () => {
  const { context, elements } = fixture();
  for (const generation of [{ can_generate: false, reason: "当前掩膜未确认" }, undefined]) {
    context.renderCfdResultState({ status: "missing", generation }, context.cfdResultsCaseRecord());
    assert.equal(elements.get("btn-cfd-postprocess").disabled, true);
  }
});

test("active or pending requests disable duplicate generation, including queued tasks", () => {
  const { context, state, elements } = fixture();
  for (const status of ["running", "queued"]) {
    context.renderCfdResultState({ status, generation: { can_generate: true }, task: { status } }, context.cfdResultsCaseRecord());
    assert.equal(elements.get("btn-cfd-postprocess").disabled, true);
    assert.equal(elements.get("btn-cfd-postprocess").hidden, true);
    assert.equal(elements.get("btn-cfd-postprocess-cancel").hidden, false);
  }
  state.cfdResults.actionPending = true;
  context.renderCfdResultState({ status: "missing", generation: { can_generate: true } }, context.cfdResultsCaseRecord());
  assert.equal(elements.get("btn-cfd-postprocess").disabled, true);
});

test("failed browser postprocessing can be retried while its original fields remain available", () => {
  const { context, elements } = fixture();
  context.renderCfdResultState({
    status: "failed", generation: { can_generate: true }, task: { status: "failed", message: "后处理曾超时" },
  }, context.cfdResultsCaseRecord());
  assert.equal(elements.get("btn-cfd-postprocess").disabled, false);
  assert.match(elements.get("cfd-results-state").innerHTML, /后处理曾超时/);
});
