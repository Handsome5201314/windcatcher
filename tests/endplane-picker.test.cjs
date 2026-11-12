"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const picker = require("../static/endplane-picker.js");
test("unavailable region explains its concrete reason without hover", () => {
  const message = picker.selectionMessage({ candidates: [{ valid: false,
    reason: "所选鼻咽支段形成多个端面或没有形成端面", runs: [[1, 2, 3]] }] }, [], 1);
  assert.match(message, /多个端面/);
  assert.match(message, /移动|调整/);
  assert.doesNotMatch(message, /点击.*编号/);
});
const data = { candidates: [
  { id: 7, valid: true, seed_lps_mm: [4, 0, 0], runs: [[10, 20, 30]] },
  { id: 9, valid: true, seed_lps_mm: [-4, 0, 0], runs: [[12, 21, 31]] },
  { id: 11, valid: false, seed_lps_mm: [9, 0, 0], runs: [[14, 22, 32]] },
] };
test("CT hit testing matches exact server pixel runs, including exclusive end", () => {
  assert.equal(picker.hit(data, 20, 10).id, 7);
  assert.equal(picker.hit(data, 30, 10), undefined);
  assert.equal(picker.hit(data, 20, 11), undefined);
});
test("two-target selection does not silently replace either chosen region", () => {
  assert.deepEqual(picker.toggle([7, 9], 11, 2), [7, 9]);
  assert.deepEqual(picker.toggle([7, 9], 7, 2), [9]);
  assert.deepEqual(picker.toggle([9], 7, 2), [9, 7]);
});
test("only complete valid selections produce seeds and ordering follows LPS X", () => {
  assert.equal(picker.selected(data, [7], 2), null);
  assert.equal(picker.selected(data, [7, 11], 2), null);
  assert.deepEqual(picker.selected(data, [9, 7], 2).map(c => c.id), [7, 9]);
});
test("extra visible branch does not masquerade as a complete nostril", () => {
  const third = { id: 12, valid: true, seed_lps_mm: [9, 0, 0], runs: [[16, 24, 32]] };
  const message = picker.selectionMessage({ role: "anterior", candidates: [...data.candidates, third] }, [7, 9], 2);
  assert.match(message, /另有 1 个未选候选/);
  assert.match(message, /真实裁切预览/);
  assert.match(message, /仍留在模型中/);
});
test("invalid fragments are not shown as selectable numbered entrances", () => {
  assert.deepEqual(picker.selectableCandidates(data).map(c => c.id), [7, 9]);
  assert.equal(picker.hit(data, 22, 14), undefined);
  const message = picker.selectionMessage({ role: "anterior", candidates: data.candidates,
    uncovered_runs: [[14, 22, 32]], uncovered_count: 10 }, [7, 9], 2);
  assert.match(message, /橙色是掩膜中的空气/);
  assert.match(message, /未归入可选开口/);
  assert.equal(picker.containsRun(picker.uncoveredRuns({ uncovered_runs: [[14, 22, 32]] }), 22, 14), true);
  assert.equal(picker.containsRun(picker.uncoveredRuns({ uncovered_runs: [[14, 22, 32]] }), 32, 14), false);
  assert.deepEqual(picker.uncoveredRuns({}), []);
  assert.equal(picker.coverageNote({}), "");
});
test("tiny spur reason remains visible beside valid nostrils", () => {
  const withReason = { role: "anterior", candidates: [data.candidates[0], data.candidates[1],
    { ...data.candidates[2], reason: "疑似小碎片，不能作为完整鼻孔入口" }],
    uncovered_runs: [[14, 22, 32]] };
  assert.match(picker.selectionMessage(withReason, [7, 9], 2), /小碎片/);
  assert.match(picker.selectionMessage(withReason, [7, 9], 2), /不能作为完整鼻孔入口/);
  assert.equal(picker.hit(withReason, 22, 14), undefined);
});
test("outlet selection replaces one region explicitly", () => {
  assert.deepEqual(picker.toggle([7], 9, 1), [9]);
});

test("independent nostril selection asks for a nostril region, not the nasopharynx", () => {
  const message = picker.selectionMessage({
    role: "anterior",
    candidates: [{ id: 7, valid: true, seed_lps_mm: [4, 0, 0], runs: [[10, 20, 30]] }],
  }, [], 1);
  assert.match(message, /鼻孔入口/);
  assert.doesNotMatch(message, /鼻咽出口/);
});

test("posterior selection asks for a nasopharynx outlet", () => {
  const message = picker.selectionMessage({
    role: "posterior",
    candidates: [{ id: 7, valid: true, seed_lps_mm: [4, 0, 0], runs: [[10, 20, 30]] }],
  }, [], 1);
  assert.match(message, /鼻咽出口/);
});

test("small neighbouring openings are fitted and remain correctly hit-tested after zoom", () => {
  const tiny = { size:384, candidates:[
    {id:1,valid:true,seed_lps_mm:[5,0,0],runs:[[167,190,195],[168,191,195]]},
    {id:2,valid:true,seed_lps_mm:[-5,0,0],runs:[[168,203,206],[169,203,206]]},
  ]};
  const view = picker.fitView(tiny);
  assert.ok(view.span <= 96, "small openings must not occupy a few pixels in the full CT frame");
  const display = [(192-view.x)*384/view.span,(168-view.y)*384/view.span];
  const pixel = picker.toPixel(view, display[0]/384, display[1]/384);
  assert.equal(picker.hit(tiny, ...pixel).id,1);
  const labels=picker.labelLayout(tiny,view);
  for(let i=0;i<labels.length;i++) for(let j=i+1;j<labels.length;j++)
    assert.ok(Math.hypot(labels[i].x-labels[j].x,labels[i].y-labels[j].y)>=28);
});

test("fitting empty and edge-touching sections stays inside the image",()=>{
  assert.deepEqual(picker.fitView({size:384,candidates:[]}),{x:0,y:0,span:384});
  const view=picker.fitView({size:384,candidates:[{id:1,runs:[[0,0,8]]}]});
  assert.ok(view.x>=0 && view.y>=0 && view.x+view.span<=384 && view.y+view.span<=384);
});
