"use strict";
(() => {
  const conditionKeys = ['solver', 'turbulence_model', 'inlet_mode', 'total_flow_lpm', 'nu', 'rho_kg_m3'];
  const isFlow = meta => meta?.kind === 'flow' && meta.flow_verified === true ||
    meta?.kind === 'flow_test' && meta.flow_verified === false;
  function comparisonPolicy(left, leftMeta, right, rightMeta) {
    const reject = reason => ({flowAllowed: false, reason, ranges: null});
    if (![leftMeta, rightMeta].every(isFlow))
      return reject('至少一个模型仅有结构或尚未完成气流验证，当前仅对照结构。');
    if (['coordinates', 'velocity', 'pressure'].some(key => !left.units?.[key] || left.units[key] !== right.units?.[key]))
      return reject('两模型单位不同，当前仅对照结构。');
    if (conditionKeys.some(key => {
      const a = leftMeta.conditions?.[key], b = rightMeta.conditions?.[key];
      return a === undefined || a === null || a === '' || b === undefined || b === null || b === '' || a !== b;
    })) return reject('两模型的流量、物性或求解工况不同／记录不完整，当前仅对照结构。');
    const ranges = {};
    for (const field of ['velocity', 'pressure']) {
      const a = left.ranges?.[field], b = right.ranges?.[field];
      if (![a, b].every(range => Array.isArray(range) && range.length === 2 && range.every(Number.isFinite) && range[0] <= range[1]))
        return reject('两模型缺少完整的数值范围，当前仅对照结构。');
      ranges[field] = [Math.min(a[0], b[0]), Math.max(a[1], b[1])];
    }
    const testing = [leftMeta, rightMeta].some(meta => meta.kind === 'flow_test');
    return {flowAllowed: true, reason: testing
      ? '包含未复核测试气流；工况记录一致并使用统一色标，仅用于可视化核对。'
      : '工况记录一致。气流对照使用统一色标；颜色不代表正常或异常。', ranges};
  }
  function singleModelPolicy(entry) {
    const reject = reason => ({flowAllowed: false, reason, ranges: null});
    if (entry.meta.kind === 'anatomy')
      return reject('本病例仅发布结构模型，没有可查看的速度／压力数据。');
    if (!isFlow(entry.meta))
      return reject('本病例气流尚未通过复核，当前仅可看结构。');
    if (['coordinates', 'velocity', 'pressure'].some(key => !entry.data.units?.[key]) ||
        ['velocity', 'pressure'].some(field => {
          const range = entry.data.ranges?.[field];
          return !Array.isArray(range) || range.length !== 2 || !range.every(Number.isFinite) || range[0] > range[1];
        })) return reject('本病例气流展示数据不完整，当前仅可看结构。');
    return {flowAllowed: true, reason: entry.meta.kind === 'flow_test'
      ? '未复核测试气流，仅用于可视化核对，不得用于判断病情或治疗效果。'
      : '可查看本病例已复核的气流；颜色不代表正常或异常。', ranges: entry.data.ranges};
  }
  async function request(url, options = {}) {
    const response = await fetch(url, {cache: 'no-store', credentials: 'same-origin', ...options});
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).detail?.reason; } catch { detail = null; }
      throw new Error(detail || `读取参考病例失败（HTTP ${response.status}）`);
    }
    return response.json();
  }
  function conditionsText(meta) {
    if (meta.kind === 'anatomy') return '已发布结构模型 · 无速度／压力数据';
    const c = meta.conditions || {};
    const status = meta.kind === 'flow_test' ? '未复核测试气流' : '已复核气流';
    return `${status} · 流量 ${c.total_flow_lpm ?? '未记录'} L/min · 求解器 ${c.solver ?? '未记录'} · 模型 ${c.turbulence_model ?? '未记录'}`;
  }
  class ReferenceComparison {
    constructor(root, options = {}) {
      this.root = root;
      this.options = options;
      this.viewers = [];
      this.generation = 0;
      this.disposed = false;
      root.innerHTML = `<section class="reference-tools" aria-label="参考病例筛选">
        <p data-role="test-warning" class="test-warning" role="alert" hidden></p>
        <fieldset data-role="filters"><legend>按已核对标签筛选（可多选）</legend></fieldset>
        <p data-role="count" role="status"></p>
        <div class="actions"><label>查看方式 <select data-role="mode"><option value="anatomy">看结构</option><option value="flow">看气流</option></select></label>
        <label>气流字段 <select data-role="field"><option value="velocity">速度</option><option value="pressure">压力</option></select></label>
        <label><input data-role="sync" type="checkbox" checked>同步视角与相对缩放</label>
        <button data-role="reset" type="button">回正两侧视角</button><button data-role="retry" type="button">重新读取</button>
        <button data-role="close" type="button" hidden>返回我的模型</button></div>
        <p data-role="policy" class="hint" aria-live="polite"></p>
      </section>
      <div class="comparison-grid">
        <section class="comparison-item"><label>左侧模型 <select data-role="left"><option value="">选择参考病例</option></select></label>
        <h2 data-role="left-title">选择模型</h2><p data-role="left-info" class="hint"></p><div data-role="left-model" class="model" tabindex="0" aria-label="左侧三维气道模型"></div></section>
        <section class="comparison-item"><label>右侧参考 <select data-role="right"><option value="">选择参考病例</option></select></label>
        <h2 data-role="right-title">选择模型</h2><p data-role="right-info" class="hint"></p><div data-role="right-model" class="model" tabindex="0" aria-label="右侧三维气道模型"></div></section>
      </div><div data-role="legend" class="legend" hidden><h3>对照统一色标</h3><div data-role="colorbar" class="legend-colorbar"></div><p data-role="range"></p></div>
      <p data-role="status" role="status" aria-live="polite"></p>
      <p class="hint">参考病例来自不同个体。同步视角不表示解剖配准；两侧按各自尺寸取景，并非等比例测量。当前表面显示气道空气空间，不是鼻甲或鼻中隔组织的独立分割。</p>`;
      this.node = role => root.querySelector(`[data-role="${role}"]`);
      for (const role of ['left', 'right']) this.node(role).addEventListener('change', () => this.loadPair());
      this.node('mode').addEventListener('change', () => this.applyMode());
      this.node('field').addEventListener('change', () => this.applyMode());
      this.node('reset').addEventListener('click', () => {
        for (const viewer of this.viewers) viewer.reset();
      });
      this.node('retry').addEventListener('click', () => this.loadCatalog());
      this.node('close').hidden = !options.onClose;
      this.node('close').addEventListener('click', () => options.onClose?.());
      this.node('mode').disabled = this.node('field').disabled = true;
      this.loadCatalog();
    }
    status(message, error = false) {
      this.node('status').textContent = message;
      this.node('status').className = error ? 'error' : 'hint';
    }
    async loadCatalog() {
      this.catalogController?.abort();
      const controller = new AbortController();
      this.catalogController = controller;
      this.status('正在读取已发布参考病例…');
      this.node('retry').disabled = true;
      try {
        const catalog = await request('/api/references', {signal: controller.signal});
        if (this.disposed) return;
        this.catalog = catalog;
        this.node('filters').replaceChildren();
        const legend = document.createElement('legend');
        legend.textContent = '按已核对标签筛选（多选时同时满足）';
        this.node('filters').append(legend);
        for (const [key, tag] of Object.entries(catalog.tags)) {
          const label = document.createElement('label'), checkbox = document.createElement('input');
          checkbox.type = 'checkbox'; checkbox.value = key;
          checkbox.addEventListener('change', () => this.filter());
          label.append(checkbox, document.createTextNode(tag.group + ' · ' + tag.label));
          this.node('filters').append(label);
        }
        this.filter();
      } catch (error) { if (!this.disposed && error.name !== 'AbortError') this.status(error.message, true); }
      finally { if (!this.disposed && this.catalogController === controller) this.node('retry').disabled = false; }
    }
    filter() {
      const tags = [...this.node('filters').querySelectorAll('input:checked')].map(input => input.value);
      const items = this.catalog.items.filter(item => tags.every(tag => item.tags.includes(tag)));
      this.node('count').textContent = items.length ? `找到 ${items.length} 个已发布参考病例` :
        this.catalog.items.length ? '没有同时满足这些标签的参考病例，请调整筛选。' : '暂未发布参考病例。团队可在工作台左栏“参考病例”中发布。';
      for (const role of ['left', 'right']) {
        const select = this.node(role), previous = select.value;
        select.replaceChildren(new Option('选择参考病例', ''));
        if (role === 'left' && this.options.primary) {
          select.append(new Option(this.options.primary.title || '你的模型', 'primary'));
          select.value = 'primary'; select.disabled = true;
        } else {
          for (const item of items) select.append(new Option(item.kind === 'flow_test' ? '测试气流 · ' + item.title : item.title, item.id));
          if (items.some(item => item.id === previous)) select.value = previous;
          else if (role === 'left' && items.length) select.value = items[0].id;
          select.disabled = !items.length;
        }
      }
      this.loadPair();
    }
    disposeViewers() { this.viewers.forEach(viewer => viewer.dispose()); this.viewers = []; }
    async loadPair() {
      const generation = ++this.generation;
      this.modelController?.abort();
      this.modelController = new AbortController();
      const signal = this.modelController.signal;
      this.disposeViewers();
      this.node('mode').disabled = this.node('field').disabled = true;
      this.node('legend').hidden = true;
      this.node('policy').textContent = '';
      this.node('test-warning').hidden = true;
      for (const role of ['left','right']) {
        this.node(role+'-title').textContent = '选择模型';
        this.node(role+'-info').textContent = '';
      }
      const ids = ['left', 'right'].map(role => this.node(role).value);
      if (!ids.some(Boolean)) { this.status('选择已发布参考病例后显示模型。'); return; }
      this.status('正在读取固定版本模型…');
      try {
        const pair = await Promise.all(ids.map(async id => {
          if (!id) return null;
          if (id === 'primary') return this.options.primary;
          const meta = this.catalog.items.find(item => item.id === id);
          if (!meta) throw new Error('参考病例已更新，请重新读取');
          return {data: await request('/api/references/' + id + '/model?version=' + encodeURIComponent(meta.version), {signal}), meta, title: meta.title};
        }));
        if (this.disposed || generation !== this.generation) return;
        this.pair = pair;
        const testEntry = pair.find(entry => entry?.meta.kind === 'flow_test');
        this.node('test-warning').textContent = testEntry?.meta.warning || '';
        this.node('test-warning').hidden = !testEntry;
        const singleIndex = pair.findIndex(Boolean);
        this.policy = pair.every(Boolean) ? comparisonPolicy(pair[0].data, pair[0].meta, pair[1].data, pair[1].meta)
          : singleModelPolicy(pair[singleIndex]);
        pair.forEach((entry, index) => {
          if (!entry) return;
          const role = index ? 'right' : 'left';
          this.node(role+'-title').textContent = entry.meta.kind === 'flow_test' ? '测试气流 · ' + entry.title : entry.title;
          const labels=(entry.meta.tags || []).map(tag => this.catalog.tags[tag]?.label || tag);
          this.node(role+'-info').textContent = [...labels, entry.meta.description || '', conditionsText(entry.meta)].filter(Boolean).join(' · ');
          let viewer;
          viewer = new CfdViewer(this.node(role+'-model'), entry.data, {
            patientPresentation: true,
            onViewChange: state => {
              if (!viewer || !this.node('sync').checked) return;
              for (const peer of this.viewers) if (peer !== viewer) peer.applyPresentationState(state);
            },
            onLegend: legend => { if (index === singleIndex) this.updateLegend(legend); },
            onError: message => this.status(message, true),
          });
          this.viewers.push(viewer);
        });
        this.node('mode').querySelector('[value="flow"]').disabled = !this.policy.flowAllowed;
        this.node('mode').disabled = false;
        this.node('mode').value = 'anatomy';
        if (this.policy.flowAllowed) this.viewers.forEach(viewer => viewer.setColorRanges(this.policy.ranges));
        this.node('legend').querySelector('h3').textContent = pair.every(Boolean) ? '对照统一色标' : '本模型色标';
        this.node('policy').textContent = this.policy.reason;
        this.applyMode();
        this.status(pair.every(Boolean) ? '拖动任一模型可同步旋转；可取消同步后单独观察。' : '模型已加载；选择另一侧参考可进行对照。');
      } catch (error) {
        if (!this.disposed && generation === this.generation && error.name !== 'AbortError') {
          this.disposeViewers(); this.status(error.message, true);
        }
      }
    }
    updateLegend({range, unit, gradient}) {
      this.node('colorbar').style.background = gradient;
      const scope = this.pair?.every(Boolean) ? '两侧共用范围' : '本模型范围';
      this.node('range').textContent = `${Number(range[0]).toFixed(2)} — ${Number(range[1]).toFixed(2)} ${unit === 'm2/s2' ? 'm²/s²' : unit} · ${scope}，蓝低红高，不代表正常或异常。`;
    }
    applyMode() {
      const flow = this.policy?.flowAllowed && this.node('mode').value === 'flow';
      this.node('mode').value = flow ? 'flow' : 'anatomy';
      this.node('field').disabled = !flow;
      this.node('legend').hidden = !flow;
      for (const viewer of this.viewers) {
        viewer.setPresentationMode(flow ? 'flow' : 'anatomy');
        if (flow) viewer.setField(this.node('field').value);
      }
    }
    dispose() {
      this.disposed = true; this.generation++;
      this.catalogController?.abort(); this.modelController?.abort();
      this.disposeViewers(); this.root.replaceChildren();
    }
  }
  window.WindcatcherReferences = {comparisonPolicy, ReferenceComparison};
  const root = document.getElementById('reference-library');
  if (root) {
    const comparison = new ReferenceComparison(root);
    window.addEventListener('pagehide', () => comparison.dispose());
    window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
  }
})();
