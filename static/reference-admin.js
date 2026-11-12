"use strict";
(() => {
  const byId = id => document.getElementById('ref-admin-' + id);
  const form = byId('form');
  if (!form) return;
  const testOption = byId('kind').querySelector('[value="flow_test"]');
  if (testOption) testOption.disabled = window.WindcatcherActivityAccess !== true;
  let catalog, generation = 0, busy = false;
  const status = (message, error = false) => {
    byId('status').textContent = message;
    byId('status').classList.toggle('is-error', error);
  };
  async function request(path, options = {}) {
    const response = await fetch(path, {cache: 'no-store', credentials: 'same-origin', ...options});
    let body;
    try { body = await response.json(); }
    catch { throw new Error(`服务响应异常（HTTP ${response.status}）`); }
    if (!response.ok) throw new Error(body.detail?.reason || `操作失败（HTTP ${response.status}）`);
    return body;
  }
  const current = () => catalog?.items.find(item => item.case_id === byId('case').value);
  function updateKind() {
    const testing = byId('kind').value === 'flow_test';
    byId('test-confirmation').hidden = !testing;
    byId('publish').textContent = testing ? '发布未复核测试气流' : '核对并发布参考';
  }
  function fill() {
    const item = current(), entry = item?.entry;
    form.hidden = !item;
    const cfdStatus = {pending:'尚未开始',ok:'阶段完成',warn:'有提示，待核查',blocked:'处理受阻',failed:'失败或中断',running:'处理中'};
    byId('readiness').textContent = item ? `结构：${item.surface_ready ? '已有模型' : '尚未就绪'}；CFD：${cfdStatus[item.cfd_status] || '状态待核查'}。正式气流需完整复核；测试气流需已完成求解与可视化，24 小时后失效。` : '暂无可选病例。';
    if (!item) return;
    const index = catalog.items.indexOf(item) + 1;
    byId('title').value = entry?.title || `参考病例 ${String(index).padStart(2, '0')}`;
    byId('description').value = entry?.description || '';
    byId('note').value = entry?.review_note || '';
    byId('kind').value = entry?.kind || 'anatomy';
    byId('test-confirm').checked = false;
    updateKind();
    byId('authorization').checked = Boolean(entry?.authorization_confirmed);
    byId('labels-confirmed').checked = Boolean(entry?.labels_confirmed);
    byId('tags').replaceChildren();
    for (const [key, tag] of Object.entries(catalog.tags)) {
      const label = document.createElement('label'), checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.value = key; checkbox.checked = Boolean(entry?.tags.includes(key));
      label.className = 'share-consent';
      label.append(checkbox, document.createTextNode(tag.group + ' · ' + tag.label));
      byId('tags').append(label);
    }
    const kindLabel = {anatomy:'仅结构',flow:'结构＋已复核气流',flow_test:'结构＋未复核测试气流'};
    byId('record').textContent = entry ? `当前记录：${{draft:'草稿',published:'已发布',withdrawn:'已撤回',expired:'已过期'}[entry.status]} · ${kindLabel[entry.kind] || '未知类型'}` : '尚未登记为参考病例，不会自动对外展示。';
    byId('revoke').hidden = entry?.status !== 'published';
    byId('publish').disabled = !item.surface_ready || busy;
  }
  async function activate(_cases, selectedCase) {
    if (busy) return;
    if (!window.WindcatcherCloudAccess) {
      status('请从已登录的公网研究工作台管理参考病例。'); return false;
    }
    const ticket = ++generation;
    status('正在读取工作台病例…');
    byId('refresh').disabled = true;
    try {
      const result = await request('/api/reference-admin/cases');
      if (ticket !== generation) return;
      const previous = byId('case').value;
      catalog = result;
      byId('case').replaceChildren(new Option('选择工作台已有病例', ''));
      result.items.forEach((item, index) => byId('case').append(new Option(`${index + 1}. ${item.case_id} · ${item.surface_ready ? '结构可用' : '结构未就绪'}`, item.case_id)));
      byId('case').value = result.items.some(item => item.case_id === previous) ? previous
        : result.items.some(item => item.case_id === selectedCase) ? selectedCase : result.items[0]?.case_id || '';
      fill();
      status(`已读取 ${result.items.length} 个工作台病例。选择已确认的标签，再保存草稿或发布。`);
      return true;
    } catch (error) { status(error.message, true); return false; }
    finally { if (ticket === generation) byId('refresh').disabled = false; }
  }
  async function submit(action) {
    if (busy || !current() || action !== 'revoke' && !form.reportValidity()) return;
    const item = current(), caseId = item.case_id;
    const payload = {title: byId('title').value.trim(), description: byId('description').value.trim(),
      tags: [...byId('tags').querySelectorAll('input:checked')].map(input => input.value),
      authorization_confirmed: byId('authorization').checked, labels_confirmed: byId('labels-confirmed').checked,
      test_use_confirmed: byId('test-confirm').checked,
      reviewer: byId('reviewer').value.trim(), review_note: byId('note').value.trim(), kind: byId('kind').value, action};
    if (action === 'publish' && (!payload.authorization_confirmed || !payload.labels_confirmed || !payload.tags.length)) {
      status('请至少选择一个已核对标签，并确认本病例展示授权与标签。', true); return;
    }
    if (action === 'publish' && payload.kind === 'flow_test' && !payload.test_use_confirmed) {
      status('发布未复核测试气流前，请确认测试用途与 24 小时有效期。', true); return;
    }
    busy = true;
    const controls = [byId('case'), byId('refresh'), ...form.querySelectorAll('input,select,textarea,button')];
    controls.forEach(node => { node.disabled = true; });
    status(action === 'publish' ? '正在校验并生成展示快照…' : '正在保存草稿…');
    try {
      if (action === 'revoke') await request('/api/reference-admin/' + item.entry.id + '/revoke', {method:'POST'});
      else await request('/api/reference-admin/cases/' + caseId, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      busy = false;
      const refreshed = await activate(null, caseId);
      if (refreshed === false) { status('操作已保存，但列表刷新失败；请点击刷新核对记录。', true); return; }
      status(action === 'revoke' ? '参考病例已撤回，公开清单和模型访问立即失效。' : action === 'publish'
        ? payload.kind === 'flow_test' ? '未复核测试气流已发布，24 小时后自动下线。可打开参考病例库核对页面。'
          : '参考病例已发布。可点击“打开参考病例库”查看；患者分享页也可进入对照。' : '草稿已保存，尚未对外展示。');
    } catch (error) { status(error.message, true); }
    finally {
      busy = false; controls.forEach(node => { node.disabled = false; });
      byId('publish').disabled = !current()?.surface_ready;
    }
  }
  byId('case').addEventListener('change', () => { fill(); status('已切换病例，请核对对应标签与展示授权。'); });
  byId('kind').addEventListener('change', updateKind);
  byId('refresh').addEventListener('click', () => activate());
  form.addEventListener('submit', event => { event.preventDefault(); submit(event.submitter?.value || 'draft'); });
  byId('revoke').addEventListener('click', () => submit('revoke'));
  window.WindcatcherReferenceAdmin = {activate};
})();
