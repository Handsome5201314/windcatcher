'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const sourcePath = path.join(__dirname, '../static/segmentation-learning.js');
function element() { return {hidden:false, disabled:false, checked:false, open:false, value:'', textContent:'',
  listeners:{}, children:[], classList:{toggle(){}}, setAttribute(){},
  addEventListener(k,f){this.listeners[k]=f;}, replaceChildren(...c){this.children=c;}, appendChild(c){this.children.push(c);} }; }
const sample = {sample_id:'seg-'+'a'.repeat(32), case_id:'case-a', before_version:1, after_version:2,
  added_voxels:0, removed_voxels:1, reference_status:'unreviewed'};
const response = body => ({ok:true, status:200, json:async()=>body});
const tick = () => new Promise(resolve=>setImmediate(resolve));
function harness(fetcher) {
  assert.ok(fs.existsSync(sourcePath), 'segmentation learning UI is missing');
  const ids = ['panel','status','refresh','select','details','strategy','run','result','checked','authorized','subject','reason','approve','reject'];
  const nodes = Object.fromEntries(ids.map(id=>['seg-learning-'+id,element()]));
  const window = {}, calls=[];
  const context=vm.createContext({window, document:{getElementById:id=>nodes[id], createElement:element},
    fetch:(...args)=>{calls.push(args);return fetcher(...args);}, AbortController, encodeURIComponent});
  vm.runInContext(fs.readFileSync(sourcePath,'utf8'), context);
  const api=window.WindcatcherSegmentationLearning;
  const config={caseId:'case-a',maskVersion:2,busy:false};
  api.setContext(config);
  nodes['seg-learning-panel'].open=true;
  nodes['seg-learning-panel'].listeners.toggle();
  return {nodes,calls,api,config,click:id=>nodes['seg-learning-'+id].listeners.click()};
}
function catalog(samples=[sample]) {return {case_id:'case-a',samples,strategies:['identity_v1','remove_tiny_islands_v1'],auto_promotion:false};}
test('empty and unreviewed states do not claim learned quality', async()=>{
  const h=harness(async()=>response(catalog([]))); await tick();
  assert.match(h.nodes['seg-learning-status'].textContent,/尚无/);
  assert.equal(h.nodes['seg-learning-run'].disabled,true);
  assert.equal(h.nodes['seg-learning-approve'].disabled,true);
});
test('reference needs two explicit checks and anonymous subject grouping',async()=>{
  const h=harness(async()=>response(catalog())); await tick();
  await h.click('approve');
  assert.equal(h.calls.length,1);
  assert.equal(h.nodes['seg-learning-approve'].disabled,true);
});
test('late response from previous case is ignored',async()=>{
  let resolve; const h=harness(()=>new Promise(r=>resolve=r));
  h.api.setContext({...h.config,caseId:null}); resolve(response(catalog())); await tick();
  assert.equal(h.nodes['seg-learning-panel'].hidden,true);
  assert.equal(h.nodes['seg-learning-run'].disabled,true);
});
test('candidate only runs the isolated learning API and reports missing reference',async()=>{
  const h=harness(async(url)=>response(url.endsWith('/candidate')?{case_id:'case-a',sample_id:sample.sample_id,
    strategy_id:'identity_v1',committed:false,auto_promotion:false,evaluation_status:'reference_required',changed_voxels:0}:catalog()));
  await tick(); await h.click('run');
  assert.match(h.nodes['seg-learning-result'].textContent,/尚无已审核参考/);
  assert.equal(h.calls.length,2);
  assert.ok(h.calls[1][0].endsWith('/segment/learning/candidate'));
  assert.equal(h.nodes['seg-learning-approve'].disabled,true);
});
test('network failures remain visible and refresh allows retry',async()=>{
  const h=harness(async()=>{throw new Error('network failed');}); await tick();
  assert.match(h.nodes['seg-learning-status'].textContent,/network failed/);
  assert.equal(h.nodes['seg-learning-refresh'].disabled,false);
});
test('reference acknowledgement also updates the selected record label',async()=>{
  const h=harness(async(url)=>response(url.endsWith('/reference')
    ? {sample_id:sample.sample_id,reference_status:'approved',committed:false} : catalog()));
  await tick();
  h.nodes['seg-learning-subject'].value='subject-001';
  h.nodes['seg-learning-checked'].checked=true;
  h.nodes['seg-learning-authorized'].checked=true;
  h.nodes['seg-learning-reason'].value='external_residual';
  await h.click('approve');
  assert.match(h.nodes['seg-learning-select'].children[0].textContent,/已核对参考/);
  assert.equal(h.nodes['seg-learning-approve'].disabled,true);
});
