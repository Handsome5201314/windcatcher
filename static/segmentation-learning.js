/* Case-local correction review and isolated strategy comparison. */
(() => {
  'use strict';
  const $ = id => document.getElementById('seg-learning-' + id);
  if (!$('panel')) return;
  const labels = {unreviewed:'待核对', approved:'已核对参考', rejected:'不作为参考'};
  const strategies = {identity_v1:'原版本（不改动）', remove_tiny_islands_v1:'去除小于 2 mm³ 的孤立块（试验）',
    remove_small_islands_v1:'去除小于 5 mm³ 的孤立块（试验）'};
  let context = {}, key = '', epoch = 0, controller = null, pending = false, fetched = false, items = [];
  const selected = () => items.find(item => item.sample_id === $('select').value);
  const ready = () => Boolean(key && !pending && !context.busy);
  const reviewable = () => ready() && selected()?.after_version === context.maskVersion &&
    selected()?.reference_status === 'unreviewed' && $('checked').checked && $('authorized').checked &&
    /^subject-[A-Za-z0-9_-]{3,64}$/.test($('subject').value.trim());
  function message(text, error = false) {
    $('status').textContent = text; $('status').classList.toggle('is-error', error);
  }
  function clearChecks() { $('checked').checked = false; $('authorized').checked = false; }
  function render() {
    $('panel').hidden = !key;
    $('panel').setAttribute('aria-busy', String(pending));
    $('refresh').disabled = !ready();
    $('select').disabled = !ready() || !items.length;
    $('strategy').disabled = !ready() || !selected();
    $('run').disabled = !ready() || !selected();
    $('approve').disabled = $('reject').disabled = !reviewable();
    for (const id of ['checked','authorized','subject','reason']) $(id).disabled = !ready() || !selected() ||
      selected().after_version !== context.maskVersion || selected().reference_status !== 'unreviewed';
    const item = selected();
    $('details').textContent = item ? `v${item.before_version} → v${item.after_version}：补回 ${item.added_voxels} 个体素，删除 ${item.removed_voxels} 个体素。${labels[item.reference_status] || '未知状态'}。` +
      (item.after_version !== context.maskVersion ? '这是历史版本，不能用当前 CT 审核它。' : '') : '';
  }
  function populate(select, values, label) {
    select.replaceChildren();
    for (const value of values) {
      const option = document.createElement('option'); option.value = value; option.textContent = label(value);
      select.appendChild(option);
    }
    select.value = values[0] || '';
  }
  async function request(suffix = '', body) {
    const response = await fetch(`/api/cases/${encodeURIComponent(context.caseId)}/segment/learning${suffix}`, {
      method: body ? 'POST' : 'GET', credentials:'same-origin', signal:controller.signal,
      ...(body ? {headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {}),
    });
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('服务返回了无法读取的响应'); }
    if (!response.ok) throw new Error(data?.detail?.reason || `请求失败（${response.status}）`);
    return data;
  }
  async function task(text, operation) {
    if (!ready()) return;
    const mine = epoch; controller = new AbortController(); pending = true; message(text); render();
    try {
      const apply = await operation();
      if (mine === epoch) apply();
    } catch (error) {
      if (mine === epoch && error.name !== 'AbortError') message(error.message, true);
    } finally { if (mine === epoch) { pending = false; render(); } }
  }
  async function refresh() {
    await task('正在读取分割修正记录…', async () => {
      const expected = context.caseId, data = await request();
      if (data.case_id !== expected || !Array.isArray(data.samples) || !Array.isArray(data.strategies) ||
          data.auto_promotion !== false || data.samples.some(item => item.case_id !== expected ||
            !/^seg-[0-9a-f]{32}$/.test(item.sample_id) || !Number.isInteger(item.after_version)) ||
          data.strategies.some(id => !Object.hasOwn(strategies, id))) throw new Error('学习记录响应不匹配');
      return () => {
        items = data.samples; fetched = true; clearChecks(); $('result').textContent = '';
        populate($('select'), items.map(item=>item.sample_id), id=>{const item=items.find(x=>x.sample_id===id);
          return `v${item.before_version} → v${item.after_version} · ${labels[item.reference_status] || '未知'}`;});
        populate($('strategy'), data.strategies, id=>strategies[id]);
        if (data.latest_comparison?.case_id === expected && data.latest_comparison?.auto_promotion === false) {
          $('result').textContent = data.latest_comparison.reference_available
            ? 'Harness 已自动完成三种策略的参考对照。选择策略后可查看各项指标。'
            : 'Harness 已自动完成三种策略的副本试算；尚无已核对参考，暂不能判断效果。';
        }
        message(items.length ? `已有 ${items.length} 条修正记录；${items.filter(x=>x.reference_status==='approved').length} 条已核对参考。` :
          '尚无修正记录。今后的掩膜编辑、导入和清理会自动留存前后差异。');
      };
    });
  }
  async function run() {
    if (!selected()) return;
    const item = selected(), strategy = $('strategy').value, expected = context.caseId;
    await task('正在本机副本试算，正式掩膜保持当前版本…', async () => {
      const data = await request('/candidate', {sample_id:item.sample_id, strategy_id:strategy});
      if (data.case_id !== expected || data.sample_id !== item.sample_id || data.strategy_id !== strategy ||
          data.committed !== false || data.auto_promotion !== false ||
          !['reference_required','evaluated'].includes(data.evaluation_status)) throw new Error('候选试算响应不匹配');
      return () => {
        if (data.evaluation_status === 'reference_required') {
          $('result').textContent = `试算已完成，改变 ${data.changed_voxels} 个体素。尚无已审核参考，不能判断更好或更差。`;
        } else {
          const a = data.baseline_metrics, b = data.metrics;
          if (!a || !b) throw new Error('缺少实际评测指标');
          const mm = value => value == null ? '不可计算（空掩膜）' : Number(value).toFixed(2) + ' mm';
          $('result').textContent = `与已核对参考比较（原版本 → 候选）：\n` +
            `漏分：${a.false_negative_voxels} → ${b.false_negative_voxels} 个体素\n` +
            `误分：${a.false_positive_voxels} → ${b.false_positive_voxels} 个体素\n` +
            `参考通道被分断：${a.reference_components_split} → ${b.reference_components_split} 处\n` +
            `边界误差 HD95：${mm(a.surface_hd95_mm)} → ${mm(b.surface_hd95_mm)}\n` +
            `最大边界距离：${mm(a.max_surface_distance_mm)} → ${mm(b.max_surface_distance_mm)}\n` +
            '单例对照不能证明算法可以升级；需要独立病例集评测。';
        }
        message('副本试算完成。结果未应用到正式掩膜。');
      };
    });
  }
  async function review(disposition) {
    if (!reviewable()) return;
    const id = selected().sample_id;
    await task('正在保存参考审核…', async () => {
      const data = await request('/reference', {sample_id:id, mask_version:context.maskVersion, disposition,
        reason_code:$('reason').value, reference_checked:true, use_authorized:true, subject_group:$('subject').value.trim()});
      if (data.sample_id !== id || data.reference_status !== disposition || data.committed !== false)
        throw new Error('参考审核响应不匹配');
      return () => {
        const item = items.find(x=>x.sample_id===id); item.reference_status = disposition; clearChecks();
        populate($('select'), items.map(row=>row.sample_id), sampleId => {
          const row = items.find(value=>value.sample_id===sampleId);
          return `v${row.before_version} → v${row.after_version} · ${labels[row.reference_status]}`;
        });
        $('select').value = id;
        message(disposition === 'approved' ? '已登记参考。可用“试算并对照”比较实际误分和漏分。' : '已标记不作为参考。');
      };
    });
  }
  function setContext(value = {}) {
    const next = value.caseId && Number.isInteger(value.maskVersion) ? `${value.caseId}|${value.maskVersion}` : '';
    if (next !== key) {
      epoch++; controller?.abort(); pending = false; fetched = false; items = []; clearChecks();
      $('subject').value = ''; $('result').textContent = ''; $('select').replaceChildren();
    }
    context = {...value}; key = next; render();
    if (key && $('panel').open && !fetched && ready()) refresh();
  }
  $('panel').addEventListener('toggle', () => { if ($('panel').open && !fetched) refresh(); });
  $('refresh').addEventListener('click', refresh); $('run').addEventListener('click', run);
  $('approve').addEventListener('click', () => review('approved'));
  $('reject').addEventListener('click', () => review('rejected'));
  $('select').addEventListener('change', () => { clearChecks(); $('result').textContent = ''; render(); });
  for (const id of ['checked','authorized','subject']) $(id).addEventListener('input', render);
  window.WindcatcherSegmentationLearning = {setContext};
  setContext();
})();
