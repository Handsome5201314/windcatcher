/* Local AI candidates: inspection never changes the official segmentation. */
(() => {
  'use strict';
  const $ = id => document.getElementById('seg-model-' + id);
  if (!$('panel')) return;
  let context = {}, key = '', epoch = 0, pending = false, current = null, available = false;
  let draftNotice = null, draftApplied = false;
  let mode = 'current', timer = null, controller = null;
  const running = () => current && ['queued','running'].includes(current.status);
  function status(text, error=false) {
    $('status').textContent=text; $('status').classList.toggle('is-error',error);
  }
  function render() {
    $('panel').hidden=!context.caseId || !Number.isInteger(context.maskVersion);
    $('run').disabled=pending || context.busy || !available || running();
    $('refresh').disabled=pending || !context.caseId;
    $('cancel').hidden=!running(); $('cancel').disabled=pending;
    const canAdopt=Boolean(current && current.status==='succeeded' && current.current && !draftApplied && !pending && !context.busy && !context.editing);
    $('adopt').hidden=!canAdopt; $('adopt').disabled=!canAdopt;
    $('adopt-help').hidden=!canAdopt;
    $('mode').disabled=pending || context.busy || context.editing || !current ||
      current.status!=='succeeded' || !current.current;
    $('jump').disabled=$('mode').disabled || mode==='current';
    $('mode').value=mode;
    const learningPanel=$('panel').closest?.('details');
    if (learningPanel && current?.status==='succeeded' && current.current) learningPanel.open=true;
    $('panel').setAttribute('aria-busy',String(pending || Boolean(running())));
  }
  function changeMode(next) {
    if (next!=='current' && ($('mode').disabled || !current)) return;
    const previous=mode;mode=next;
    if (context.onViewChange?.(next,current?.run_id)===false) mode=previous;
    render();
  }
  function show(data) {
    if (data.case_id!==context.caseId || data.auto_promotion!==false || !Array.isArray(data.runs))
      throw new Error('候选响应与当前病例不匹配');
    for (const run of data.runs) {
      if (run.case_id!==context.caseId || !/^model-[0-9a-f]{32}$/.test(run.run_id) || run.committed!==false)
        throw new Error('候选记录无效');
    }
    const previousRun=current?.run_id;
    available=data.model_available===true;current=data.runs[0] || null;
    if (mode!=='current' && (!current || current.status!=='succeeded' || !current.current)) changeMode('current');
    else if(mode!=='current' && previousRun!==current?.run_id) changeMode(mode);
    status(draftNotice || (current ? (!current.current ? '输入已变化，请重新生成候选。' : current.message)
      : available ? '本机模型已就绪。生成候选后，可在上方 CT 查看新旧差异。' : '本机模型环境尚未就绪。'),
      current?.status==='failed');
    const s=current?.summary;
    $('result').textContent=draftNotice || (s ? `原主鼻腔 ${s.baseline_volume_ml} mL → AI 候选 ${s.candidate_volume_ml} mL；新增 ${s.added_voxels}、旧有新无 ${s.removed_voxels} 个体素。耗时 ${current.elapsed_seconds} 秒。差异不代表对错，尚待核对。` : '');
    draftNotice = null;
    render();
  }
  async function request(caseId, signal, suffix='', body) {
    const r=await fetch(`/api/cases/${encodeURIComponent(caseId)}/segment/models${suffix}`,{
      method:body?'POST':'GET',credentials:'same-origin',signal,
      ...(body?{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    let data;try {data=await r.json();} catch (_) {throw new Error('模型服务返回了无法读取的响应');}
    if (!r.ok) throw new Error(data?.detail?.reason || `模型请求失败（${r.status}）`);
    return data;
  }
  async function task(operation) {
    if (!context.caseId || pending) return;
    const mine=epoch;pending=true;controller=new AbortController();clearTimeout(timer);render();
    const caseId=context.caseId, signal=controller.signal;
    try {
      const result=await operation((suffix,body)=>request(caseId,signal,suffix,body));if (mine===epoch) show(result);
    } catch(error) {
      if (mine===epoch && error.name!=='AbortError') status(error.message,true);
    } finally {
      if (mine===epoch) {pending=false;render();if(running()) timer=setTimeout(refresh,3000);}
    }
  }
  function refresh() {return task(read=>read());}
  $('run').addEventListener('click',()=>{
    if ($('run').disabled) return;
    status('正在提交本机候选任务…');
    return task(async read=>{await read('',{mask_version:context.maskVersion,model_id:'upper_airway_fink_v1'});return read();});
  });
  $('refresh').addEventListener('click',refresh);
  $('cancel').addEventListener('click',()=>{
    if(!running() || pending)return;
    return task(async read=>{await read('/'+current.run_id+'/cancel',{});return read();});
  });
  $('adopt').addEventListener('click',()=>{
    if($('adopt').disabled || !current?.run_id)return;
    if(typeof window.confirm==='function' && !window.confirm('将 AI 候选写入当前待复核草稿，并使旧下游产物失效。仍需人工复核，确定继续吗？'))return;
    status('正在把 AI 候选转为待复核草稿…');
    return task(async read=>{
      const result=await read('/'+current.run_id+'/draft',{});
      await context.onDraftAdopted?.(result);
      draftApplied=true;
      draftNotice=result.message || 'AI 候选已转为待复核草稿；请在 CT 中人工复核。';
      return read();
    });
  });
  $('mode').addEventListener('change',()=>changeMode($('mode').value));
  $('jump').addEventListener('click',()=>{if(!$('jump').disabled)context.onJump?.(current.summary.jump_slices);});
  window.WindcatcherSegmentationModels={
    setContext(next) {
      const nextKey=next.caseId+':'+next.maskVersion;
      if (key!==nextKey) {
        controller?.abort();clearTimeout(timer);epoch++;pending=false;current=null;available=false;mode='current';draftNotice=null;draftApplied=false;
        $('result').textContent='';key=nextKey;context=next;render();
        if(next.caseId && Number.isInteger(next.maskVersion))refresh();
      } else {
        context=next;
        if(context.editing && mode!=='current')changeMode('current');
        render();
      }
    },
    sliceUrl(view,params) {
      if(mode==='current' || !current?.current || current.status!=='succeeded')return null;
      const query=new URLSearchParams(params);query.set('mode',mode);
      return `/api/cases/${encodeURIComponent(context.caseId)}/segment/models/${current.run_id}/slice/${view}?${query}`;
    },
    currentMode:()=>mode,
  };
})();
