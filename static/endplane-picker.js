/* CT-region selection uses the server's exact reslice pixel grid. */
(function (root) {
  "use strict";
  function selected(data, ids, expected) {
    const regions = data.candidates.filter(c => ids.includes(c.id));
    if (regions.length !== expected || regions.some(c => !c.valid || !c.runs.length ||
      !Array.isArray(c.seed_lps_mm) || c.seed_lps_mm.length !== 3)) return null;
    return regions.sort((a, b) => b.seed_lps_mm[0] - a.seed_lps_mm[0]);
  }
  function selectableCandidates(data) {
    return data.candidates.filter(c => c.valid && c.runs.length && Array.isArray(c.seed_lps_mm) && c.seed_lps_mm.length === 3);
  }
  function hit(data, x, y) {
    return selectableCandidates(data).find(c => c.runs.some(([row, start, end]) => row === y && x >= start && x < end));
  }
  function toggle(ids, id, expected) {
    if (ids.includes(id)) return ids.filter(value => value !== id);
    if (expected === 1) return [id];
    return ids.length < expected ? [...ids, id] : ids;
  }
  function uncoveredRuns(data) {
    return Array.isArray(data.uncovered_runs) ? data.uncovered_runs : [];
  }
  function containsRun(runs, x, y) {
    return runs.some(([row, start, end]) => row === y && x >= start && x < end);
  }
  function coverageNote(data) {
    if (!uncoveredRuns(data).length) return "";
    const reasons = [...new Set(data.candidates.filter(c => !c.valid).map(c => c.reason).filter(Boolean))];
    const detail = reasons.length ? " 原因：" + reasons.slice(0, 2).join("；") + (reasons.length > 2 ? "等" : "") + "。" : "";
    return " 橙色是掩膜中的空气，但未归入可选开口；请调整切面或检查掩膜，不会自动纳入裁切。" + detail;
  }
  const views = new WeakMap();
  function selectionMessage(data, ids, expected) {
    if (!data.candidates.length) return "当前切面没有可选开口，请移动切面或检查掩膜。" + coverageNote(data);
    const valid = selected(data, ids, expected);
    if (valid) {
      const visible = selectableCandidates(data).length;
      if (visible > expected) return `已选 ${expected}/${expected} 个开口候选；另有 ${visible - expected} 个未选候选仍留在模型中。请查看真实裁切预览，确认入口是否完整。` + coverageNote(data);
      return `已选 ${expected}/${expected} 个开口候选。${expected === 2 ? "蓝色：左鼻孔；紫色：右鼻孔。" : "标色：当前开口。"}高亮边界来自将用于裁切的体素选区；保存前请核对三维裁切预览。` + coverageNote(data);
    }
    const available = selectableCandidates(data).length;
    if (available < expected) {
      const reasons = [...new Set(data.candidates.filter(region => !region.valid).map(region => region.reason).filter(Boolean))];
      return `当前切面只有 ${available}/${expected} 个可用开口。${reasons.join("；") || "没有足够完整的开口"}。请前后移动或调整切面角度；若仍不连通，请返回掩膜检查。` + coverageNote(data);
    }
    const target = data.role === "posterior" ? "鼻咽出口" : "鼻孔入口";
    return `还需选择 ${Math.max(0,expected-ids.length)} 个${target}（已选 ${ids.length}/${expected}）。点击高亮范围中的任一点，即选中整个对应开口；也可点击下方按钮。` + coverageNote(data);
  }
  function outlineRuns(ctx, runs, size, scale) {
    const occupied = new Uint8Array(size * size);
    for (const [row, start, end] of runs) occupied.fill(1, row * size + start, row * size + end);
    ctx.save();
    ctx.strokeStyle = "#eafaff";
    ctx.lineWidth = 1.8 / scale;
    ctx.beginPath();
    for (const [row, start, end] of runs) {
      ctx.moveTo(start, row); ctx.lineTo(start, row + 1);
      ctx.moveTo(end, row); ctx.lineTo(end, row + 1);
      for (let x = start; x < end; x++) {
        if (row === 0 || !occupied[(row - 1) * size + x]) {
          ctx.moveTo(x, row); ctx.lineTo(x + 1, row);
        }
        if (row === size - 1 || !occupied[(row + 1) * size + x]) {
          ctx.moveTo(x, row + 1); ctx.lineTo(x + 1, row + 1);
        }
      }
    }
    ctx.stroke();
    ctx.restore();
  }
  function boundedView(size, x, y, span) {
    span = Math.min(size, Math.max(size / 16, span));
    return { x: Math.max(0, Math.min(size - span, x)), y: Math.max(0, Math.min(size - span, y)), span };
  }
  function fitView(data) {
    const candidates = selectableCandidates(data);
    const runs = candidates.length ? candidates.flatMap(c => c.runs)
      : uncoveredRuns(data).length ? uncoveredRuns(data) : data.candidates.flatMap(c => c.runs);
    if (!runs.length) return { x: 0, y: 0, span: data.size };
    let x0 = data.size, y0 = data.size, x1 = 0, y1 = 0;
    for (const [row, start, end] of runs) { x0 = Math.min(x0, start); x1 = Math.max(x1, end); y0 = Math.min(y0, row); y1 = Math.max(y1, row + 1); }
    const span = Math.min(data.size, Math.max(data.size / 6, Math.max(x1-x0,y1-y0) * 1.7));
    return boundedView(data.size, (x0+x1-span)/2, (y0+y1-span)/2, span);
  }
  function toPixel(view, x, y) {
    return [Math.floor(view.x+x*view.span),Math.floor(view.y+y*view.span)];
  }
  function labelLayout(data, view) {
    const groups = [[],[]], size = data.size, gap = size * .08, edge = size * .055;
    let visibleIndex = 0;
    selectableCandidates(data).forEach(region => {
      const index = visibleIndex++;
      let x=0,y=0,count=0;
      for (const [row,start,end] of region.runs) { const n=end-start; x+=(start+end-1)*.5*n; y+=row*n; count+=n; }
      if (!count) return;
      const anchorX=(x/count-view.x)*size/view.span, anchorY=(y/count-view.y)*size/view.span;
      if (anchorX<0 || anchorX>size || anchorY<0 || anchorY>size) return;
      const side=anchorX<size/2?0:1;
      groups[side].push({ id:region.id,index,anchorX,anchorY,x:side?size-edge:edge,y:anchorY });
    });
    for (const group of groups) {
      group.sort((a,b)=>a.anchorY-b.anchorY);
      group.forEach((item,i)=>{ item.y=Math.max(edge,item.y,i?group[i-1].y+gap:edge); });
      if (group.length) {
        group[group.length-1].y=Math.min(size-edge,group[group.length-1].y);
        for(let i=group.length-2;i>=0;i--) group[i].y=Math.min(group[i].y,group[i+1].y-gap);
      }
    }
    return groups.flat();
  }
  function render(data, ids, expected, onPick, busy = false, onViewChange = null) {
    const canvas = document.getElementById("endplane-section-regions");
    const image = document.getElementById("endplane-section-image");
    const choices = document.getElementById("endplane-region-choices");
    const message = document.getElementById("endplane-region-status");
    canvas.hidden = false;
    canvas.width = canvas.height = data.size;
    const ctx = canvas.getContext("2d");
    let state = views.get(canvas);
    if (!state || state.data !== data) { state = {data,view:fitView(data)}; views.set(canvas,state); }
    const ordered = data.candidates.filter(c => ids.includes(c.id)).sort((a, b) => b.center_lps_mm[0] - a.center_lps_mm[0]);
    let labels=[];
    const paint = () => {
    const view=state.view, scale=data.size/view.span;
    ctx.clearRect(0,0,data.size,data.size);
    if(image.complete && image.naturalWidth) { ctx.drawImage(image,view.x,view.y,view.span,view.span,0,0,data.size,data.size); image.hidden=true; }
    ctx.save(); ctx.scale(scale,scale); ctx.translate(-view.x,-view.y);
    for (const [index, region] of selectableCandidates(data).entries()) {
      const chosen = ids.includes(region.id);
      ctx.fillStyle = chosen ? data.role === "posterior" ? "rgba(255,170,60,.55)" : ordered.indexOf(region) === 0 ? "rgba(90,170,255,.55)" : "rgba(210,130,255,.55)" : "rgba(255,220,100,.25)";
      for (const [row, start, end] of region.runs) ctx.fillRect(start, row, end - start, 1);
    }
    ctx.fillStyle = "rgba(255,113,46,.72)";
    for (const [row, start, end] of uncoveredRuns(data)) ctx.fillRect(start, row, end - start, 1);
    for (const region of data.candidates) if (ids.includes(region.id)) outlineRuns(ctx, region.runs, data.size, scale);
    ctx.restore();
    labels=labelLayout(data,view);
    const radius=data.size*.032;
    for(const label of labels) {
      ctx.strokeStyle=ids.includes(label.id)?"#6dc5ef":"#e9d69d"; ctx.lineWidth=data.size/300;
      ctx.beginPath(); ctx.moveTo(label.x,label.y); ctx.lineTo(label.anchorX,label.anchorY); ctx.stroke();
      ctx.fillStyle="#101820"; ctx.beginPath(); ctx.arc(label.x,label.y,radius,0,Math.PI*2); ctx.fill(); ctx.stroke();
      ctx.fillStyle="white"; ctx.font=`bold ${data.size/28}px sans-serif`; ctx.textAlign="center"; ctx.textBaseline="middle";
      ctx.fillText(String(label.index+1),label.x,label.y);
    }
    if (onViewChange) onViewChange({ ...view });
    const zoomLabel=document.getElementById("endplane-section-zoom");
    if(zoomLabel) zoomLabel.textContent=`${(data.size/view.span).toFixed(1)}× · 滚轮缩放，拖动平移`;
    };
    const zoom=(factor,x=.5,y=.5)=>{
      const v=state.view, span=Math.min(data.size,Math.max(data.size/16,v.span*factor));
      state.view=boundedView(data.size,v.x+x*(v.span-span),v.y+y*(v.span-span),span); paint();
    };
    for(const [id,action] of [
      ["endplane-section-fit",()=>{state.view=fitView(data);paint();}],
      ["endplane-section-full",()=>{state.view={x:0,y:0,span:data.size};paint();}],
      ["endplane-section-plus",()=>zoom(.75)], ["endplane-section-minus",()=>zoom(1/.75)],
    ]) { const button=document.getElementById(id); if(button) {button.disabled=false;button.onclick=action;} }
    canvas.onwheel=event=>{event.preventDefault();const r=canvas.getBoundingClientRect();zoom(event.deltaY<0?.8:1.25,(event.clientX-r.left)/r.width,(event.clientY-r.top)/r.height);};
    let drag=null,moved=false;
    canvas.onpointerdown=event=>{if(event.button!==0)return;drag={x:event.clientX,y:event.clientY,view:{...state.view}};moved=false;canvas.setPointerCapture(event.pointerId);};
    canvas.onpointermove=event=>{if(!drag)return;const r=canvas.getBoundingClientRect(),dx=event.clientX-drag.x,dy=event.clientY-drag.y;if(Math.hypot(dx,dy)>3)moved=true;if(moved){state.view=boundedView(data.size,drag.view.x-dx*drag.view.span/r.width,drag.view.y-dy*drag.view.span/r.height,drag.view.span);paint();}};
    canvas.onpointerup=event=>{drag=null;if(canvas.hasPointerCapture(event.pointerId))canvas.releasePointerCapture(event.pointerId);};
    canvas.onpointercancel=()=>{drag=null;moved=true;};
    canvas.onclick = event => {
      if (busy || moved) return;
      const rect = canvas.getBoundingClientRect();
      const x=(event.clientX-rect.left)/rect.width,y=(event.clientY-rect.top)/rect.height;
      const label=labels.find(l=>Math.hypot(l.x-x*data.size,l.y-y*data.size)<=data.size*.04);
      const [pixelX,pixelY]=toPixel(state.view,x,y);
      const region = label ? data.candidates.find(c=>c.id===label.id) : hit(data,pixelX,pixelY);
      const invalid = !region && data.candidates.find(c=>!c.valid && containsRun(c.runs,pixelX,pixelY));
      if (region?.valid) onPick(region.id);
      else if (invalid?.reason) message.textContent = "此处暂不可选：" + invalid.reason + "。请调整切面。";
      else if (region?.reason) message.textContent = `此处暂不可选：${region.reason}。请调整切面。`;
      else if (containsRun(uncoveredRuns(data),pixelX,pixelY))
        message.textContent = "橙色是掩膜空气，但没有匹配到可保存的实际开口；请调整切面或检查掩膜。";
    };
    paint();
    choices.replaceChildren();
    let visibleIndex = 0;
    selectableCandidates(data).forEach(region => {
      const index = visibleIndex++;
      const button = document.createElement("button");
      button.type = "button"; button.className = "vt-btn";
      button.textContent = `开口候选 ${index + 1}${ids.includes(region.id) ? " · 已选" : ""}${!region.valid ? " · 需调整切面" : ""}`;
      button.setAttribute("aria-pressed", String(ids.includes(region.id)));
      button.classList.toggle("is-on", ids.includes(region.id));
      button.disabled = busy || !region.valid || !region.runs.length;
      button.title = region.reason || "点击一次选择或取消整个开口；保存前核对三维裁切预览";
      button.addEventListener("click", () => onPick(region.id));
      choices.append(button);
    });
    message.textContent = selectionMessage(data, ids, expected);
  }
  const api = { selected, selectableCandidates, hit, toggle, uncoveredRuns, containsRun, coverageNote, fitView, toPixel, labelLayout, selectionMessage, render };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.EndplanePicker = api;
})(typeof window !== "undefined" ? window : globalThis);
