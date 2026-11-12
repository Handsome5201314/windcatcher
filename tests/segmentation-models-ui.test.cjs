'use strict';
const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/segmentation-models.js'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const reply=data=>({ok:true,status:200,json:async()=>data});
const rid='model-'+'a'.repeat(32);
function catalog(status='succeeded') {return {case_id:'case-a',model_available:true,auto_promotion:false,runs:status?[{
 case_id:'case-a',run_id:rid,status,current:true,committed:false,message:status==='running'?'GPU 分割中':'候选完成，待复核',
 elapsed_seconds:8,summary:status==='succeeded'?{baseline_volume_ml:4,candidate_volume_ml:5,added_voxels:2,removed_voxels:1,jump_slices:{axial:7}}:undefined}]:[]};}
function harness(fetcher){
  const ids=['panel','run','refresh','cancel','adopt','adopt-help','mode','jump','status','result'];
 const nodes=Object.fromEntries(ids.map(id=>[id,{hidden:false,disabled:false,value:'',textContent:'',listeners:{},classList:{toggle(){}},
 setAttribute(){},addEventListener(k,f){this.listeners[k]=f;}}]));
 const window={},calls=[],timers=[];
 vm.runInNewContext(source,{window,document:{getElementById:id=>nodes[id.replace('seg-model-','')]},
 fetch:(...args)=>{calls.push(args);return fetcher(...args);},AbortController,URLSearchParams,encodeURIComponent,
 setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){}});
 const api=window.WindcatcherSegmentationModels,config={caseId:'case-a',maskVersion:2,busy:false,editing:false};
 api.setContext(config);
 return {api,nodes,calls,timers,config,click:id=>nodes[id].listeners.click(),change:mode=>{nodes.mode.value=mode;nodes.mode.listeners.change();}};
}
test('preview toggles before loading slices and never writes the formal mask',async()=>{
 const h=harness(async()=>reply(catalog()));await tick();let url;
 h.api.setContext({...h.config,onViewChange:()=>{url=h.api.sliceUrl('axial',{index:7});}});
 h.change('difference');
 assert.ok(url.includes('/segment/models/'+rid+'/slice/axial'));
 assert.match(url,/mode=difference/);assert.match(h.nodes.result.textContent,/差异不代表对错/);
 assert.equal(h.calls.length,1);h.change('current');assert.equal(h.api.sliceUrl('axial',{}),null);
});
test('editing leaves comparison and dirty edits prevent generation',async()=>{
 const h=harness(async()=>reply(catalog()));await tick();h.change('candidate');
 h.api.setContext({...h.config,editing:true,busy:true});
 assert.equal(h.api.currentMode(),'current');assert.equal(h.nodes.mode.disabled,true);
 await h.click('run');assert.equal(h.calls.length,1);
});
test('running job polls actual status and exposes cancel',async()=>{
 const h=harness(async()=>reply(catalog('running')));await tick();
 assert.equal(h.nodes.run.disabled,true);assert.equal(h.nodes.cancel.hidden,false);
 assert.equal(h.nodes.status.textContent,'GPU 分割中');assert.equal(h.timers.at(-1).ms,3000);
 await h.click('cancel');assert.ok(h.calls[1][0].endsWith('/'+rid+'/cancel'));
 assert.equal(h.calls[1][1].method,'POST');
});
test('late response cannot leak another case into the preview',async()=>{
 let resolve;const h=harness(()=>new Promise(r=>resolve=r));
 h.api.setContext({...h.config,caseId:null});resolve(reply(catalog()));await tick();
 assert.equal(h.nodes.panel.hidden,true);assert.equal(h.nodes.mode.disabled,true);
 assert.equal(h.api.sliceUrl('axial',{}),null);
});
test('an in-flight submission keeps its follow-up bound to the original case',async()=>{
 let resolve;const h=harness(async(url,options)=>options.method==='POST'?new Promise(r=>resolve=r):reply(catalog(null)));
 await tick();const pending=h.click('run');
 h.api.setContext({...h.config,caseId:null});resolve(reply({run_id:rid}));await pending;
 assert.ok(h.calls.every(([url])=>url.includes('/case-a/')));assert.equal(h.nodes.panel.hidden,true);
});
test('failed network is visible and can be retried',async()=>{
 const h=harness(async()=>{throw new Error('network failed');});await tick();
 assert.match(h.nodes.status.textContent,/network failed/);assert.equal(h.nodes.refresh.disabled,false);
 assert.equal(h.nodes.run.disabled,true);
});
test('stale inputs disable candidate preview',async()=>{
 const data=catalog();data.runs[0].current=false;
 const h=harness(async()=>reply(data));await tick();h.change('candidate');
 assert.equal(h.api.currentMode(),'current');assert.equal(h.nodes.mode.disabled,true);
 assert.match(h.nodes.status.textContent,/输入已变化/);
});
test('successful candidate exposes an adopt-as-draft action and refreshes after adoption',async()=>{
 let adopted=false;const calls=[];let catalogCalls=0;
 const h=harness(async(url,options)=>{
   calls.push([url,options]);
   if(options?.method==='POST' && url.endsWith('/draft')) return reply({status:'drafted',message:'AI 候选已转为待复核草稿'});
   catalogCalls+=1;return reply(catalog());
 });
 h.api.setContext({...h.config,onDraftAdopted:async()=>{adopted=true;}});await tick();
 assert.equal(h.nodes.adopt.hidden,false);assert.equal(h.nodes.adopt.disabled,false);
 await h.click('adopt');
 assert.equal(adopted,true);assert.ok(calls.some(([url,opts])=>url.endsWith('/'+rid+'/draft')&&opts.method==='POST'));
 assert.ok(catalogCalls>=2);assert.match(h.nodes.status.textContent,/AI 候选已转为待复核草稿/);
 assert.equal(h.nodes.adopt.hidden,true);
});
