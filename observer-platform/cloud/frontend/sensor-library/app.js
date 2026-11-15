(() => {
  'use strict';
  const library = window.SENSOR_LIBRARY;
  const main = document.getElementById('workspace');
  if (!library?.sensors?.length) {
    main.innerHTML = '<div class="empty">资料加载失败<br><a class="button" href="./documents/小车传感器规格文档_20261115.zip" download>下载原始文档</a></div>';
    return;
  }
  const sensors = library.sensors;
  const documents = library.documents;
  const byId = new Map(sensors.map(s => [s.id, s]));
  const docsById = new Map(documents.map(d => [d.id, d]));
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  const localFile = path => './documents/' + path.split('/').map(encodeURIComponent).join('/');
  const assetFile = path => './assets/' + path.split('/').map(encodeURIComponent).join('/');
  const kinds = {
    manufacturer: ['原厂规格', 'factory'], vendor: ['厂商驱动', 'sdk'], sdk: ['SDK 参数', 'sdk'],
    interface: ['数据接口', 'sdk'], project: ['项目配置', 'project'], measured: ['历史实测', 'project'],
    integration: ['接入说明', 'project'], provenance: ['资料来源', ''], limits: ['资料边界', ''], notice: ['原文差异', 'project'],
  };
  const icons = {
    camera: '<rect x="3" y="6" width="18" height="14" rx="3"/><path d="m7 6 2-3h6l2 3"/><circle cx="12" cy="13" r="4"/>',
    depth: '<rect x="2" y="6" width="20" height="12" rx="4"/><circle cx="7" cy="12" r="2"/><circle cx="17" cy="12" r="2"/><path d="M11 12h2M6 3v3m12-3v3"/>',
    lidar: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 4 16 4 16 0V6M4 12c0 4 16 4 16 0"/><path d="M8 17h8"/>',
    scan: '<circle cx="12" cy="12" r="3"/><path d="M5.5 5.5a9 9 0 0 0 0 13m13-13a9 9 0 0 1 0 13M2 12h3m14 0h3M12 2v3m0 14v3"/>',
    document: '<path d="M6 2h8l5 5v15H6zM14 2v6h5M9 12h7m-7 4h7"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/>',
    compare: '<path d="M4 7h16m-4-4 4 4-4 4M20 17H4m4-4-4 4 4 4"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    check: '<path d="m5 12 4 4L19 6"/><circle cx="12" cy="12" r="10"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
    copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  };
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.grid}</svg>`;
  const sensorIcon = sensor => sensor.id === 'p100r' ? 'depth' : sensor.family === 'camera' ? 'camera' : sensor.id === 'mid360' ? 'lidar' : 'scan';
  const badge = kind => { const [label, cls] = kinds[kind] || ['资料记录', '']; return `<span class="badge ${cls}">${label}</span>`; };
  const formatBytes = bytes => bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(2)} MB` : `${(bytes / 1024).toFixed(1)} KB`;
  let category = 'all', query = '', docFilter = 'all', toastTimer;
  let selectedCompare = ['a062', 'a0588'];
  try {
    const theme = localStorage.getItem('sensor-library.theme');
    if (theme === 'dark') document.documentElement.dataset.theme = theme;
    const saved = JSON.parse(localStorage.getItem('sensor-library.compare') || 'null');
    if (Array.isArray(saved)) selectedCompare = [...new Set(saved.filter(id => byId.has(id)))].slice(0, 4);
  } catch (_) { /* Browser storage is optional. */ }

  function route() {
    const [path, search] = location.hash.slice(1).split('?');
    const params = new URLSearchParams(search || '');
    return {view: path === 'compare' ? 'compare' : path === 'documents' ? 'documents' : 'device',
      id: byId.has(path?.split('/')[1]) ? path.split('/')[1] : 'a062', tab: params.get('tab') || 'overview'};
  }
  function setRoute(id, tab = 'overview') {
    location.hash = `device/${id}${tab === 'overview' ? '' : `?tab=${encodeURIComponent(tab)}`}`;
  }
  function searchable(sensor) {
    return [sensor.model, sensor.name, sensor.category, sensor.manufacturer, ...sensor.sections.flatMap(s => [s.title, ...s.rows.flatMap(r => [r.label, r.value])])].join(' ').toLowerCase();
  }
  const searchIndex = new Map(sensors.map(s => [s.id, searchable(s)]));
  function matchingSensors() {
    return sensors.filter(s => (category === 'all' || s.family === category) && (!query || searchIndex.get(s.id).includes(query.toLowerCase())));
  }
  function matchesRow(row, title) {
    return !query || `${title} ${row.label} ${row.value}`.toLowerCase().includes(query.toLowerCase());
  }
  function pageHeading(title) {
    return `<div class="page-heading"><h1>${escape(title)}</h1></div>`;
  }
  function stats() {
    return `<div class="stats" aria-label="资料统计">${[
      ['grid', sensors.length, '型号条目'], ['check', 2, '原厂规格书'],
      ['document', documents.length, '原始文档'], ['compare', library.rowCount, '参数与接口记录'],
    ].map(([i, value, label]) => `<div class="stat"><div class="stat-icon">${icon(i)}</div><div><strong>${value.toString().padStart(2, '0')}</strong><small>${label}</small></div></div>`).join('')}</div>`;
  }
  function renderNav() {
    const state = route();
    const matching = matchingSensors();
    $('sensor-count').textContent = String(matching.length).padStart(2, '0');
    $('sensor-nav').innerHTML = matching.map(s => `<a href="#device/${s.id}" class="sensor-link ${state.view === 'device' && state.id === s.id ? 'active' : ''}" ${state.view === 'device' && state.id === s.id ? 'aria-current="page"' : ''}><span class="nav-icon">${icon(sensorIcon(s))}</span><span><strong>${escape(s.model)}</strong><small>${escape(s.name)}</small></span>${state.view === 'device' && state.id === s.id ? '<span class="nav-arrow" aria-hidden="true">›</span>' : ''}</a>`).join('') || '<p class="nav-empty">无匹配设备</p>';
    document.querySelectorAll('[data-filter]').forEach(button => {
      const active = button.dataset.filter === category;
      button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    });
    document.querySelectorAll('[data-view]').forEach(link => {
      const active = link.dataset.view === state.view;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
  }
  function reference(sourceId, page) {
    if (!docsById.has(sourceId)) return '';
    return `<button type="button" class="source-ref" data-document="${escape(sourceId)}" ${page ? `data-source-page="${escape(page)}"` : ''}>${page ? `原文 P.${escape(page)}` : '查看来源'} <span aria-hidden="true">↗</span></button>`;
  }
  function panel(section, index, allRows = false) {
    const rows = allRows ? section.rows : section.rows.filter(r => matchesRow(r, section.title));
    if (!rows.length) return '';
    const wide = rows.length >= 10 || rows.some(row => String(row.value).length > 100);
    return `<section class="section-card${wide ? ' wide' : ''}" aria-label="${escape(section.title)}"><header class="section-header"><div class="section-title"><span class="section-number">${String(index + 1).padStart(2, '0')}</span><h3>${escape(section.title)}</h3></div><div class="section-meta">${badge(section.kind)}${reference(section.sourceId, section.sourcePage)}</div></header><table class="parameter-table"><thead><tr><th scope="col">参数 / 项目</th><th scope="col">规格 / 记录值</th></tr></thead><tbody>${rows.map(r => `<tr><td>${escape(r.label)}</td><td>${escape(r.value)}</td></tr>`).join('')}</tbody></table></section>`;
  }
  function hero(sensor) {
    const type = sensor.hasSpec ? 'manufacturer' : sensor.sources[0].kind === 'vendor' ? 'sdk' : 'project';
    return `<article class="device-hero"><div class="device-heading"><div class="device-title"><div class="device-topline">${badge(type)}<span class="badge">${escape(sensor.category)}</span><span class="badge">${sensor.rowCount} 条记录</span></div><h1 class="device-model">${escape(sensor.model)}</h1><p class="device-subtitle">${escape(sensor.name)}</p><div class="device-manufacturer"><span>${escape(sensor.manufacturer)}</span></div></div><div class="hero-actions"><a class="button primary" href="${escape(localFile(sensor.sources[0].file))}" download>${icon('download')}下载${sensor.hasSpec ? '规格书' : '主要文档'}</a><button class="button" type="button" data-compare-add="${sensor.id}">${icon('compare')}加入对比</button><button class="button" type="button" data-export="${sensor.id}">${icon('document')}导出参数</button></div></div><div class="hero-metrics">${sensor.highlights.slice(0, 4).map(h => `<div class="hero-metric"><span>${escape(h.label)}</span><strong>${escape(h.value)}</strong></div>`).join('')}</div></article>`;
  }
  function classify(section, sensor) {
    if (section.kind === 'limits' || section.kind === 'notice' || section.kind === 'provenance') return 'notes';
    if (/结构|机械|引脚|元器件|环境|验收|可靠|包装|维护|注意|安装|外参|坐标系/.test(section.title)) return 'structure';
    if (sensor.hasSpec && /成像传感器|光学参数|电气/.test(section.title)) return 'overview';
    if (['interface', 'sdk', 'integration'].includes(section.kind)) return 'interfaces';
    return 'overview';
  }
  function modesPanel(sensor) {
    if (!sensor.modes?.length) return '';
    return `<section class="section-card${sensor.modes.length >= 10 ? ' wide' : ''}"><header class="section-header"><div class="section-title"><span class="section-number">01</span><h3>分辨率与帧率</h3></div><div class="section-meta">${badge('manufacturer')}${reference(sensor.modes[0].sourceId, sensor.modes[0].sourcePage)}</div></header><table class="parameter-table"><thead><tr><th scope="col">输出格式 / 分辨率</th><th scope="col">最高帧率</th></tr></thead><tbody>${sensor.modes.map(m => `<tr><td>${escape(m.format)} · ${m.width} × ${m.height}</td><td>${m.fps} fps</td></tr>`).join('')}</tbody></table></section>`;
  }
  function drawings(sensor) {
    return (sensor.drawings || []).map(d => `<section class="section-card"><header class="section-header"><div class="section-title"><span class="section-number">↗</span><h3>${escape(d.title)}</h3></div>${reference(d.sourceId, d.sourcePage)}</header><a class="drawing-preview" href="${escape(assetFile(d.file))}" target="_blank" rel="noopener" aria-label="打开${escape(d.title)}大图"><img loading="lazy" src="${escape(assetFile(d.file))}" alt="${escape(d.title)}，来自原厂规格书第${d.sourcePage}页"></a></section>`).join('');
  }
  function empty(message = '无匹配参数') {
    return `<div class="empty">${icon('document')}${escape(message)}${query ? '<br><button type="button" class="button" data-clear-search>清除搜索</button>' : ''}</div>`;
  }
  function renderDevice(state) {
    const sensor = byId.get(state.id);
    const tab = ['overview', 'interfaces', 'structure', 'notes', 'sources'].includes(state.tab) ? state.tab : 'overview';
    const groups = {overview: [], interfaces: [], structure: [], notes: []};
    sensor.sections.forEach(section => groups[classify(section, sensor)].push(section));
    let content;
    if (query) {
      const modelMatches = `${sensor.model} ${sensor.name} ${sensor.manufacturer}`.toLowerCase().includes(query.toLowerCase());
      content = `<div class="sections">${sensor.sections.map((s, i) => panel(s, i, modelMatches)).join('') || empty()}</div>`;
    } else if (tab === 'sources') {
      content = `<div class="document-grid">${sensor.sources.map(s => documentCard(docsById.get(s.id))).join('')}</div>`;
    } else {
      const selected = groups[tab];
      const drawingHtml = tab === 'structure' ? drawings(sensor) : '';
      const modesHtml = tab === 'interfaces' ? modesPanel(sensor) : '';
      const sectionHtml = selected.map((s, i) => panel(s, i)).join('');
      content = `<div class="sections">${drawingHtml}${modesHtml}${sectionHtml || (!drawingHtml && !modesHtml ? empty('暂无参数') : '')}</div>`;
    }
    const tabDefs = [['overview', '参数总览', groups.overview.length], ['interfaces', sensor.hasSpec ? '模式与接口' : '接口与配置', groups.interfaces.length + (sensor.modes?.length ? 1 : 0)], ['structure', sensor.hasSpec ? '结构与验收' : '安装与坐标', groups.structure.length + (sensor.drawings?.length ? 1 : 0)], ['notes', '资料备注', groups.notes.length], ['sources', '原始文档', sensor.sources.length]];
    main.innerHTML = hero(sensor)
      + `<nav class="detail-tabs" aria-label="资料分类">${tabDefs.map(([id, label, count]) => `<a href="#device/${sensor.id}${id === 'overview' ? '' : '?tab=' + id}" class="${tab === id && !query ? 'active' : ''}" ${tab === id && !query ? 'aria-current="page"' : ''}>${label}<b>${String(count).padStart(2, '0')}</b></a>`).join('')}</nav><div class="detail-layout"><div class="detail-content">${content}</div></div>`;
    $('breadcrumb-current').textContent = sensor.category;
    document.title = `${sensor.model} · 小车传感器资料库`;
  }
  function documentCard(doc) {
    const pdf = doc.file.endsWith('.pdf');
    return `<article class="document-card"><div class="document-card-top"><span class="file-symbol ${pdf ? 'pdf' : ''}">${pdf ? 'PDF' : doc.file.endsWith('.txt') ? 'TXT' : 'MD'}</span>${badge(doc.kind)}</div><div class="document-card-body"><h3>${escape(doc.label)}</h3><p><span>${doc.sensorIds.map(id => escape(byId.get(id).model)).join(' / ')}</span><span>${doc.pages ? `${doc.pages} 页 · ` : ''}${formatBytes(doc.bytes)}${doc.revision ? ' · ' + escape(doc.revision) : ''}</span></p></div><div class="document-card-actions"><button type="button" class="button small" data-document="${escape(doc.id)}">${icon('document')}查看原文</button><a class="button small" href="${escape(localFile(doc.file))}" download>${icon('download')}下载</a></div></article>`;
  }
  function renderDocuments() {
    const selected = documents.filter(d => (docFilter === 'all' || (docFilter === 'manufacturer' ? d.kind === 'manufacturer' : d.kind !== 'manufacturer')) && (!query || `${d.label} ${d.text} ${d.sensorIds.map(id => byId.get(id).model).join(' ')}`.toLowerCase().includes(query.toLowerCase())));
    main.innerHTML = pageHeading('原始文档目录') + stats() + `<div class="directory-toolbar"><div class="segmented" role="group" aria-label="筛选文档来源">${[['all', '全部资料'], ['manufacturer', '原厂规格'], ['support', '配套说明']].map(([id, label]) => `<button type="button" data-doc-filter="${id}" class="${id === docFilter ? 'active' : ''}" aria-pressed="${id === docFilter}">${label}</button>`).join('')}</div><a class="button primary" href="./documents/小车传感器规格文档_20261115.zip" download>${icon('download')}打包下载全部</a></div><div class="document-grid">${selected.map(documentCard).join('') || empty('无匹配文档')}</div>`;
    $('breadcrumb-current').textContent = '文档目录'; document.title = '原始文档 · 小车传感器资料库';
  }
  function renderCompare() {
    const selected = selectedCompare.map(id => byId.get(id));
    const rows = ['连接方式', '图像分辨率', '帧率 / 发布频率', '原厂视场角', '供电电压', '工作电流'];
    main.innerHTML = pageHeading('传感器参数对比')
      + `<div class="comparison-picks">${sensors.map(s => `<label class="compare-pick"><input type="checkbox" value="${s.id}" data-compare-pick ${selectedCompare.includes(s.id) ? 'checked' : ''}><span><strong>${escape(s.model)}</strong><small>${escape(s.category)} · ${s.hasSpec ? '原厂规格' : '接口资料'}</small></span></label>`).join('')}</div><div class="directory-toolbar"><p>已选择 ${selected.length} / 4 个型号</p><button type="button" class="button" data-print>${icon('document')}打印 / 保存 PDF</button></div>`
      + (selected.length >= 2 ? `<div class="compare-wrap"><table class="comparison-table" style="min-width:${Math.max(600, 155 + 220 * selected.length)}px"><thead><tr><th scope="col">对比项目</th>${selected.map(s => `<th scope="col">${escape(s.model)}<br>${badge(s.hasSpec ? 'manufacturer' : s.sources[0].kind === 'vendor' ? 'sdk' : 'project')}</th>`).join('')}</tr></thead><tbody>${rows.map((label, i) => `<tr><th scope="row">${label}</th>${selected.map(s => { const cell = s.comparison[i]; return `<td>${cell.value ? escape(cell.value) + `<br>${badge(cell.kind)}${reference(cell.sourceId, cell.sourcePage)}` : '<span class="missing">现有资料未提供</span>'}</td>`; }).join('')}</tr>`).join('')}</tbody></table></div>` : empty('至少选择 2 个型号'));
    $('breadcrumb-current').textContent = '参数对比'; document.title = '参数对比 · 小车传感器资料库';
  }
  function render() {
    renderNav(); const state = route();
    if (state.view === 'compare') renderCompare(); else if (state.view === 'documents') renderDocuments(); else renderDevice(state);
    const tabs = main.querySelector('.detail-tabs');
    const activeTab = tabs?.querySelector('[aria-current="page"]');
    if (activeTab && tabs.scrollWidth > tabs.clientWidth) {
      tabs.scrollLeft = activeTab.offsetLeft - (tabs.clientWidth - activeTab.offsetWidth) / 2;
    }
  }
  function notify(message) {
    clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false;
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3400);
  }
  function saveCompare() {
    try { localStorage.setItem('sensor-library.compare', JSON.stringify(selectedCompare)); } catch (_) { /* Optional local preference. */ }
  }
  function openDocument(id, page) {
    const doc = docsById.get(id); if (!doc) return;
    $('document-title').textContent = doc.label;
    $('document-kind').textContent = (kinds[doc.kind] || ['资料原文'])[0];
    $('document-meta').textContent = `${doc.pages ? `${doc.pages} 页 · ` : ''}${formatBytes(doc.bytes)}${page ? ` · 引用第 ${page} 页` : ''}`;
    $('document-text').textContent = doc.text;
    $('open-document').href = localFile(doc.file) + (page ? `#page=${encodeURIComponent(page)}` : '');
    $('download-document').href = localFile(doc.file);
    $('download-document').download = doc.file.split('/').at(-1);
    $('document-dialog').showModal();
    const pre = $('document-text'); pre.scrollTop = 0;
    if (page) {
      const position = doc.text.indexOf(`—— 第 ${page} 页 ——`);
      if (position >= 0) {
        const range = document.createRange(); range.setStart(pre.firstChild, position); range.setEnd(pre.firstChild, Math.min(position + 1, doc.text.length));
        pre.scrollTop = range.getBoundingClientRect().top - pre.getBoundingClientRect().top + pre.scrollTop - 18;
      }
    }
  }
  function closeMenu() {
    $('sidebar').classList.remove('open'); $('sidebar-backdrop').hidden = true;
    $('menu-toggle').setAttribute('aria-expanded', 'false');
  }
  function exportSensor(id) {
    const s = byId.get(id); if (!s) return;
    const csvCell = value => '"' + String(value ?? '').replaceAll('"', '""') + '"';
    const rows = [['型号', '分节', '参数', '值', '备注', '来源类别', '原文文件', '原文页码']];
    s.sections.forEach(sec => sec.rows.forEach(r => rows.push([s.model, sec.title, r.label, r.value, r.note || '', (kinds[sec.kind] || ['资料记录'])[0], docsById.get(sec.sourceId).file, sec.sourcePage || ''])));
    (s.modes || []).forEach(m => rows.push([s.model, '分辨率与帧率', `${m.format} ${m.width}×${m.height}`, `${m.fps} fps`, '不适用于推断YUY2帧率', '原厂规格', docsById.get(m.sourceId).file, m.sourcePage]));
    const blob = new Blob(['\ufeff' + rows.map(row => row.map(csvCell).join(',')).join('\r\n')], {type: 'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = `${s.model}_参数与来源.csv`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 3000);
  }
  document.querySelector('.skip-link').addEventListener('click', event => {
    event.preventDefault(); main.focus();
  });
  document.addEventListener('click', event => {
    const target = event.target.closest('button, a'); if (!target) return;
    if (target.dataset.document) openDocument(target.dataset.document, target.dataset.sourcePage);
    if (target.dataset.filter) {
      category = target.dataset.filter; const matching = matchingSensors();
      if (route().view === 'device' && matching.length && !matching.some(s => s.id === route().id)) setRoute(matching[0].id); else render();
    }
    if (target.dataset.docFilter) { docFilter = target.dataset.docFilter; renderDocuments(); }
    if (target.hasAttribute('data-clear-search')) { query = ''; $('global-search').value = ''; render(); $('global-search').focus(); }
    if (target.dataset.compareAdd) {
      const id = target.dataset.compareAdd;
      if (!selectedCompare.includes(id)) {
        if (selectedCompare.length >= 4) { notify('最多对比 4 个型号'); return; }
        selectedCompare.push(id); saveCompare();
      }
      location.hash = 'compare';
    }
    if (target.dataset.export) exportSensor(target.dataset.export);
    if (target.hasAttribute('data-print')) window.print();
    if (target.closest('.sensor-link, .side-tools a, .brand')) closeMenu();
  });
  document.addEventListener('change', event => {
    const checkbox = event.target.closest('[data-compare-pick]'); if (!checkbox) return;
    if (checkbox.checked) {
      if (selectedCompare.length >= 4) { checkbox.checked = false; notify('最多对比 4 个型号'); return; }
      selectedCompare.push(checkbox.value);
    } else selectedCompare = selectedCompare.filter(id => id !== checkbox.value);
    saveCompare(); renderCompare();
  });
  $('global-search').addEventListener('input', event => {
    query = event.target.value.trim(); const matching = matchingSensors();
    if (route().view === 'device' && matching.length && !matching.some(s => s.id === route().id)) {
      history.replaceState(null, '', `#device/${matching[0].id}`);
    }
    render();
  });
  $('theme-toggle').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme !== 'dark'; document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    $('theme-toggle').setAttribute('aria-label', dark ? '切换浅色外观' : '切换深色外观');
    try { localStorage.setItem('sensor-library.theme', dark ? 'dark' : 'light'); } catch (_) { /* Optional. */ }
  });
  if (document.documentElement.dataset.theme === 'dark') {
    $('theme-toggle').setAttribute('aria-label', '切换浅色外观');
  }
  $('menu-toggle').addEventListener('click', () => {
    const open = !$('sidebar').classList.contains('open'); $('sidebar').classList.toggle('open', open); $('sidebar-backdrop').hidden = !open; $('menu-toggle').setAttribute('aria-expanded', String(open));
  });
  $('sidebar-backdrop').addEventListener('click', closeMenu);
  $('close-document').addEventListener('click', () => $('document-dialog').close());
  $('document-dialog').addEventListener('click', event => { if (event.target === $('document-dialog')) { const r = event.target.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) event.target.close(); } });
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); $('global-search').focus(); $('global-search').select(); }
    if (event.key === 'Escape') closeMenu();
  });
  addEventListener('hashchange', () => { render(); window.scrollTo({top: 0, behavior: 'instant'}); });
  $('doc-count').textContent = String(documents.length);
  render();
})();
