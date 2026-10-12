// ── Incidents page: confirmed incidents + every AI analysis ever made ──
const inc = {
  tab: 'incidents', cases: [], service: 'all', selected: null, signature: null, toastTimer: null,
  patternOpen: false,
  // Analysis history: list filters, one selected entry and its full record.
  history: {data: null, kind: '', service: '', target: null, selected: null, detailN: null, detail: null},
};
const $incList = document.getElementById('inc-list');
const $incDetail = document.getElementById('inc-detail');
const $incService = document.getElementById('inc-service');
const INC_HELP_TEXT = {
  incidents: 'Incidents of whole services, written from experience. When a service shows an incident\'s error pattern again, the analysis shows it as "This happened before" with its root cause and resolution, and the AI is given it too. Save one from a service analysis in AI Insights, or write one here.',
  history: 'Every AI analysis made from AI Insights (alerts, services, unknown templates), newest first, failed ones included. Re-analyzing or deleting an analysis in AI Insights keeps the earlier answers here; delete the ones you no longer need from this page.',
};
const HISTORY_BADGE = {window: 'badge-blue', service: 'badge-green', template: 'badge-yellow'};
const HISTORY_TAB = {window: 'windows', service: 'services', template: 'templates'};

// The history list filters, as sent to the API (list and bulk delete alike).
function historyFilters() {
  const h = inc.history;
  if (h.target) return {kind: h.target.kind, id: h.target.id};
  const filters = {};
  if (h.kind) filters.kind = h.kind;
  if (h.service) filters.service = h.service;
  return filters;
}

async function fetchIncidents() {
  const params = new URLSearchParams(historyFilters());
  const [cases, history] = await Promise.all([
    getJson('/api/incident-cases', 'Unable to load incidents'),
    getJson(`/api/insights/history?${params}`, 'Unable to load the analysis history'),
  ]);
  return {...cases, history};
}

async function incReload() {
  inc.signature = null;
  renderIncidents(await fetchIncidents());
}

function incToast(message, isError = false) {
  const $toast = document.getElementById('inc-toast');
  $toast.textContent = message;
  $toast.className = 'ins-toast' + (isError ? ' ins-toast-error' : '');
  clearTimeout(inc.toastTimer);
  inc.toastTimer = setTimeout(() => { $toast.textContent = ''; }, isError ? 10000 : 6000);
}

function renderIncidents(data) {
  const h = inc.history;
  if (data) {
    inc.cases = data.cases || [];
    if (data.history) h.data = data.history;
  }
  const signature = JSON.stringify([inc.tab, inc.cases, inc.service, inc.selected, h.data, h.selected, h.detailN, Boolean(h.detail)]);
  if (signature === inc.signature) return;  // keep focus between polls
  inc.signature = signature;
  document.querySelectorAll('[data-inc-tab]').forEach(button => {
    const active = button.dataset.incTab === inc.tab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
  });
  document.getElementById('inc-count-incidents').textContent = inc.cases.length;
  document.getElementById('inc-count-history').textContent = h.data ? h.data.all_total : '';
  document.getElementById('inc-history-filters').hidden = inc.tab !== 'history' || Boolean(h.target);
  document.getElementById('inc-new').hidden = inc.tab !== 'incidents';
  const failed = h.data ? h.data.failed_total || 0 : 0;
  const $deleteFailed = document.getElementById('inc-history-delete-failed');
  $deleteFailed.hidden = inc.tab !== 'history' || !failed;
  $deleteFailed.textContent = `Delete failed (${failed})`;
  document.getElementById('inc-target').hidden = inc.tab !== 'history' || !h.target;
  document.getElementById('inc-target-label').textContent = h.target ? `Only ${h.target.label}` : '';
  document.getElementById('inc-help').textContent = INC_HELP_TEXT[inc.tab];
  $incList.setAttribute('aria-label', inc.tab === 'history' ? 'Analyses' : 'Incidents');
  if (inc.tab === 'history') {
    renderHistory();
    return;
  }
  $incService.disabled = false;
  const services = [...new Set(inc.cases.map(c => c.service))].sort();
  if (inc.service !== 'all' && !services.includes(inc.service)) inc.service = 'all';
  $incService.innerHTML = [['all', 'All services'], ...services.map(svc => [svc, svc])].map(([value, label]) =>
    `<option value="${escapeHtml(value)}"${value === inc.service ? ' selected' : ''}>${escapeHtml(label)}</option>`).join('');
  const items = inc.cases.filter(c => inc.service === 'all' || c.service === inc.service);
  if (!items.some(c => c.id === inc.selected)) inc.selected = items.length ? items[0].id : null;
  $incList.innerHTML = items.length
    ? items.map(c => `<li><button type="button" class="ins-item" data-inc-id="${escapeHtml(c.id)}"${c.id === inc.selected ? ' aria-current="true"' : ''}>
        <span class="ins-item-top"><span class="ins-item-title">${escapeHtml(c.id)} · ${escapeHtml(c.service)}</span></span>
        <span class="ins-item-sub">${escapeHtml(c.title || '')}</span>
        <span class="ins-item-status st-none">${escapeHtml(formatTs(c.occurred_at || c.created_at))}</span>
      </button></li>`).join('')
    : '<li class="ins-empty" style="padding:16px">No incidents saved yet. Analyze a service in AI Insights, then use "Save as service incident".</li>';
  const selected = items.find(c => c.id === inc.selected);
  $incDetail.innerHTML = selected ? incidentDetailHtml(selected) : '<p class="ins-empty">Select an incident.</p>';
}

// ── Analysis history tab ──
function renderHistory() {
  const h = inc.history;
  const data = h.data || {items: [], services: [], total: 0};
  $incService.disabled = Boolean(h.target);
  $incService.innerHTML = [['', 'All services'], ...data.services.map(svc => [svc, svc])].map(([value, label]) =>
    `<option value="${escapeHtml(value)}"${value === h.service ? ' selected' : ''}>${escapeHtml(label)}</option>`).join('');
  document.getElementById('inc-kind').value = h.kind;
  const items = data.items || [];
  if (!items.some(row => row.n === h.selected)) h.selected = items.length ? items[0].n : null;
  $incList.innerHTML = items.length
    ? items.map(row => `<li><button type="button" class="ins-item" data-hist-n="${row.n}"${row.n === h.selected ? ' aria-current="true"' : ''}>
        <span class="ins-item-top"><span class="badge ${HISTORY_BADGE[row.kind] || 'badge-gray'}">${escapeHtml(row.kind_label)}</span><span class="ins-item-title">${escapeHtml(row.target)}</span></span>
        <span class="ins-item-sub hist-sub${row.status === 'failed' ? ' st-failed' : ''}">${escapeHtml(row.headline)}</span>
        <span class="ins-item-status st-none" title="${escapeHtml(formatTs(row.at))}">${escapeHtml(formatFullTs(row.at))}</span>
      </button></li>`).join('')
      + (data.total > items.length ? `<li class="ins-empty" style="padding:12px 16px">Showing the newest ${items.length} of ${data.total}. Filter by type or service to see older ones.</li>` : '')
    : `<li class="ins-empty" style="padding:16px">${h.target || h.kind || h.service ? 'No analysis matches these filters.' : 'No analyses yet. Every AI analysis requested in AI Insights is kept here.'}</li>`;
  if (h.selected === null) {
    $incDetail.innerHTML = '<p class="ins-empty">Select an analysis.</p>';
  } else if (h.detailN !== h.selected) {
    $incDetail.innerHTML = '<p class="ins-empty" aria-busy="true">Loading…</p>';
    loadHistoryEntry(h.selected);
  } else {
    $incDetail.innerHTML = h.detail ? historyDetailHtml(h.detail) : '<p class="ins-empty" aria-busy="true">Loading…</p>';
  }
}

async function loadHistoryEntry(n) {
  const h = inc.history;
  h.detailN = n;
  h.detail = null;
  let detail;
  try {
    detail = await getJson(`/api/insights/history/${n}`, 'Unable to load this analysis');
  } catch (err) {
    detail = {error: err.message};
  }
  if (h.detailN !== n) return;  // another entry was selected meanwhile
  h.detail = detail;
  renderIncidents();
}

function historyDetailHtml(d) {
  if (d.error) return `<p class="st-failed">${escapeHtml(d.error)}</p>`;
  const a = d.record || {};
  let groupId = null;
  if (d.kind === 'window') {
    try { groupId = JSON.parse(d.id)[1]; } catch (err) { groupId = null; }
  }
  const recalled = a.recalled || [];
  const recall = recalled.length
    ? `<p class="ins-meta">Past incidents recalled: ${recalled.map(r => `${escapeHtml(r.id)} (${Math.round((r.overlap || 0) * 100)}%)`).join(', ')}${a.similar_case_id ? ` · the AI used ${escapeHtml(a.similar_case_id)}` : ''}</p>`
    : '';
  const sameItem = inc.history.target ? '' : '<button type="button" class="btn" data-hist-action="same">All analyses of this item</button>';
  return `<div class="ins-head"><h2>${escapeHtml(d.target)}</h2>
      <span class="badge ${HISTORY_BADGE[d.kind] || 'badge-gray'}">${escapeHtml(d.kind_label)}</span>
      <span class="ins-meta">${escapeHtml(formatFullTs(d.at))}${d.service && d.kind !== 'service' ? ` · ${escapeHtml(d.service)}` : ''}</span></div>
    ${recall}
    <div class="ins-section"><h3>AI analysis</h3>${insAnalysisBody({kind: d.kind, analysis: a, raw: {group_id: groupId}}, true)}</div>
    <div class="ins-actions">${sameItem}<a class="btn" href="#insights" data-hist-action="insights">Open in AI Insights</a><button type="button" class="btn btn-danger" data-hist-action="delete">Delete</button></div>`;
}

function setIncidentTab(tab) {
  inc.tab = tab;
  inc.signature = null;
  renderIncidents();
}

document.querySelectorAll('[data-inc-tab]').forEach(button => button.addEventListener('click', () => setIncidentTab(button.dataset.incTab)));
document.querySelector('[role="tablist"][aria-label="Incident page"]').addEventListener('keydown', event => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  const tabs = [...document.querySelectorAll('[data-inc-tab]')];
  const index = tabs.indexOf(document.activeElement);
  if (index < 0) return;
  event.preventDefault();
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
    : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next].click();
  tabs[next].focus();
});

$incService.addEventListener('change', () => {
  if (inc.tab === 'incidents') {
    inc.service = $incService.value;
    renderIncidents();
    return;
  }
  inc.history.service = $incService.value;
  inc.history.selected = null;
  incReload().catch(err => incToast(err.message, true));
});
document.getElementById('inc-kind').addEventListener('change', event => {
  inc.history.kind = event.target.value;
  inc.history.selected = null;
  incReload().catch(err => incToast(err.message, true));
});
document.getElementById('inc-target-clear').addEventListener('click', () => {
  inc.history.target = null;
  inc.history.selected = null;
  incReload().catch(err => incToast(err.message, true));
});

document.getElementById('inc-history-delete-failed').addEventListener('click', async () => {
  const h = inc.history;
  const count = h.data ? h.data.failed_total || 0 : 0;
  if (!count) return;
  const scope = h.target ? ` of ${h.target.label}`
    : [h.kind && `of type "${document.getElementById('inc-kind').selectedOptions[0].text}"`, h.service && `in ${h.service}`].filter(Boolean).join(' ');
  const ok = await confirmDialog(`Delete ${count} failed ${count === 1 ? 'analysis' : 'analyses'}${scope ? ` ${scope}` : ''} from the history? This cannot be undone.`,
    {title: 'Delete failed analyses?', confirmLabel: `Delete ${count}`});
  if (!ok) return;
  try {
    const result = await apiMutation('/api/insights/history/delete', 'POST', {failed: true, ...historyFilters()});
    h.selected = null;
    await incReload();
    incToast(`${result.deleted} failed ${result.deleted === 1 ? 'analysis' : 'analyses'} deleted`);
  } catch (err) {
    incToast(err.message, true);
  }
});

$incList.addEventListener('click', event => {
  const historyButton = event.target.closest('[data-hist-n]');
  if (historyButton) {
    inc.history.selected = Number(historyButton.dataset.histN);
    renderIncidents();
    const again = $incList.querySelector(`[data-hist-n="${inc.history.selected}"]`);
    if (again) again.focus();
    return;
  }
  const button = event.target.closest('[data-inc-id]');
  if (!button) return;
  inc.selected = button.dataset.incId;
  renderIncidents();
  const again = $incList.querySelector(`[data-inc-id="${CSS.escape(inc.selected)}"]`);
  if (again) again.focus();
});

$incDetail.addEventListener('click', async event => {
  const action = event.target.closest('[data-hist-action]');
  const d = inc.history.detail;
  if (!action || !d || d.error) return;
  if (action.dataset.histAction === 'delete') {
    const ok = await confirmDialog(`Delete this analysis of ${d.target} (${formatFullTs(d.at)}) from the history? This cannot be undone.`,
      {title: 'Delete analysis?', confirmLabel: 'Delete'});
    if (!ok) return;
    try {
      await apiMutation('/api/insights/history/delete', 'POST', {n: d.n});
      Object.assign(inc.history, {selected: null, detailN: null, detail: null});
      await incReload();
      incToast('Analysis deleted');
    } catch (err) {
      incToast(err.message, true);
    }
    return;
  }
  if (action.dataset.histAction === 'same') {
    inc.history.target = {kind: d.kind, id: d.id, label: d.target};
    inc.history.selected = d.n;
    incReload().catch(err => incToast(err.message, true));
    return;
  }
  // Open in AI Insights: the link's #insights hash switches the view.
  const tab = HISTORY_TAB[d.kind];
  ins.tab = tab;
  ins.filter[tab] = 'all';
  ins.selected[tab] = d.id;
  ins.signature = null;
});

// "History" in AI Insights: this page's history tab, filtered to that item.
$insDetail.addEventListener('click', event => {
  if (!event.target.closest('[data-open-history]')) return;
  const item = insSelectedItem();
  if (!item) return;
  const label = item.kind === 'window' ? `${item.raw.group_id} · ${item.raw.service}` : item.id;
  Object.assign(inc.history, {target: {kind: item.kind, id: item.id, label}, selected: null});
  inc.tab = 'history';
  inc.signature = null;
});

// Polls re-render the detail; remember whether the error pattern is open.
$incDetail.addEventListener('toggle', event => {
  if (event.target.classList.contains('pattern-collapse')) inc.patternOpen = event.target.open;
}, true);
$incDetail.addEventListener('click', async event => {
  if (event.target.closest('[data-inc-action="edit"]')) {
    const c = inc.cases.find(x => x.id === inc.selected);
    if (c) openIncidentEditor({
      mode: 'edit', caseId: c.id, service: c.service, title: c.title, root_cause: c.root_cause,
      resolution: c.resolution, documentation_id: c.documentation_id || '',
      rows: (c.pattern && c.pattern.length ? c.pattern : (c.template_texts || []).map(text => ({text}))),
    });
    return;
  }
  if (!event.target.closest('[data-ins-action="delete-case"]') || !inc.selected) return;
  const caseId = inc.selected;
  if (!(await confirmDialog(`${caseId} will be deleted. The AI will no longer see it and it is no longer recalled.`, {title: 'Delete incident?', confirmLabel: 'Delete incident'}))) return;
  try {
    await apiMutation(`/api/incident-cases/${encodeURIComponent(caseId)}`, 'DELETE', {});
    incToast(`${caseId} deleted.`);
    inc.signature = null;
    renderIncidents(await fetchIncidents());
  } catch (err) { incToast(err.message, true); }
});

// ── Incident editor: save from analysis, edit, or write by hand ──
const incEditor = {mode: null, caseId: null, service: null, rows: [], searchTimer: null, searchSeq: 0};
const $incForm = document.getElementById('incident-form');
const $incPattern = document.getElementById('incident-pattern');
const $incResults = document.getElementById('incident-template-results');
const $incSearch = document.getElementById('incident-template-search');
const $incServiceSelect = document.getElementById('incident-service');
const INC_HELP = {
  analysis: 'Saves what the whole service is suffering now. The error pattern below is what the engine measured during this analysis.',
  edit: 'Rewrite this incident from experience. The next time its pattern comes back, people and the AI see your root cause and resolution.',
  new: 'Write an incident from experience: pick the service and the templates that show this problem.',
};

async function openIncidentEditor(opts) {
  Object.assign(incEditor, {
    mode: opts.mode, caseId: opts.caseId || null, service: opts.service || null,
    rows: (opts.rows || []).filter(r => r && r.text).map(r => ({...r, added: false})),
  });
  document.getElementById('incident-modal-title').textContent =
    {analysis: 'Save as service incident', edit: `Edit ${opts.caseId}`, new: 'New incident'}[opts.mode];
  document.getElementById('incident-modal-help').textContent = INC_HELP[opts.mode];
  document.getElementById('incident-title').value = opts.title || '';
  document.getElementById('incident-root-cause').value = opts.root_cause || '';
  document.getElementById('incident-resolution').value = opts.resolution || '';
  document.getElementById('incident-doc').value = opts.documentation_id || '';
  document.getElementById('incident-error').textContent = '';
  $incSearch.value = '';
  $incResults.innerHTML = '';
  document.getElementById('incident-service-field').hidden = opts.mode !== 'new';
  if (opts.mode === 'new') {
    $incServiceSelect.innerHTML = '<option value="">Loading services…</option>';
    try {
      const resp = await fetch('/api/templates?per_page=1');
      const services = ((await resp.json()).stats || {}).services || [];
      $incServiceSelect.innerHTML = services.map(svc => `<option value="${escapeHtml(svc)}">${escapeHtml(svc)}</option>`).join('');
      incEditor.service = services.includes(inc.service) ? inc.service : (services[0] || null);
      if (incEditor.service) $incServiceSelect.value = incEditor.service;
    } catch (err) { $incServiceSelect.innerHTML = ''; }
  }
  renderIncidentPattern();
  document.getElementById('incident-modal').classList.add('open');
  document.getElementById(opts.mode === 'new' ? 'incident-service' : 'incident-title').focus();
}

function renderIncidentPattern() {
  const fmt = x => x === null || x === undefined ? '' : ` · ${Number(x).toFixed(1)}× normal`;
  $incPattern.innerHTML = incEditor.rows.length
    ? incEditor.rows.map((row, index) => `<li><code>${highlightWildcards(escapeHtml(row.text))}</code>
        <span class="ins-meta">${escapeHtml(row.level || '')}${fmt(row.ratio)}${row.added ? ' · added' : ''}</span>
        <button type="button" class="btn" data-pattern-remove="${index}" aria-label="Remove ${escapeHtml(row.text)}">Remove</button></li>`).join('')
    : '<li class="ins-empty">No templates yet. Search below and add the templates that show this problem.</li>';
}

$incPattern.addEventListener('click', event => {
  const button = event.target.closest('[data-pattern-remove]');
  if (!button) return;
  incEditor.rows.splice(Number(button.dataset.patternRemove), 1);
  renderIncidentPattern();
});
$incServiceSelect.addEventListener('change', () => {
  incEditor.service = $incServiceSelect.value;
  incEditor.rows = [];
  $incResults.innerHTML = '';
  renderIncidentPattern();
});
$incSearch.addEventListener('input', () => {
  clearTimeout(incEditor.searchTimer);
  incEditor.searchTimer = setTimeout(searchIncidentTemplates, 250);
});

async function searchIncidentTemplates() {
  const query = $incSearch.value.trim();
  const seq = ++incEditor.searchSeq;  // ignore answers to older keystrokes
  if (!query || !incEditor.service) { $incResults.innerHTML = ''; return; }
  try {
    const resp = await fetch(`/api/templates?service=${encodeURIComponent(incEditor.service)}&search=${encodeURIComponent(query)}&per_page=20`);
    const items = ((await resp.json()).items || []).filter(t => t.service === incEditor.service);
    if (seq !== incEditor.searchSeq) return;
    const present = new Set(incEditor.rows.map(row => row.text));
    incEditor.results = items.filter(t => !present.has(t.template_text));
    $incResults.innerHTML = incEditor.results.length
      ? incEditor.results.map((t, index) => `<li><code>${highlightWildcards(escapeHtml(t.template_text))}</code>
          <span class="ins-meta">${escapeHtml(t.template_id)} · ${escapeHtml(t.level || '')}</span>
          <button type="button" class="btn" data-pattern-add="${index}">Add</button></li>`).join('')
      : '<li class="ins-empty">No other matching templates.</li>';
  } catch (err) {
    if (seq === incEditor.searchSeq) $incResults.innerHTML = '<li class="ins-empty">Unable to search templates.</li>';
  }
}

$incResults.addEventListener('click', event => {
  const button = event.target.closest('[data-pattern-add]');
  if (!button) return;
  const t = (incEditor.results || [])[Number(button.dataset.patternAdd)];
  if (!t) return;
  incEditor.rows.push({text: t.template_text, level: t.level, template_id: t.template_id, added: true});
  incEditor.results = incEditor.results.filter(other => other !== t);
  button.closest('li').remove();
  renderIncidentPattern();
});

$incForm.addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  const payload = {
    title: document.getElementById('incident-title').value,
    root_cause: document.getElementById('incident-root-cause').value,
    resolution: document.getElementById('incident-resolution').value,
    documentation_id: document.getElementById('incident-doc').value,
    add_template_ids: incEditor.rows.filter(row => row.added).map(row => row.template_id),
  };
  try {
    let saved;
    if (incEditor.mode === 'edit') {
      payload.keep_texts = incEditor.rows.filter(row => !row.added).map(row => row.text);
      saved = await apiMutation(`/api/incident-cases/${encodeURIComponent(incEditor.caseId)}`, 'PUT', payload);
    } else if (incEditor.mode === 'new') {
      saved = await apiMutation('/api/incident-cases', 'POST', {...payload, service: incEditor.service, manual: true});
    } else {
      payload.keep_texts = incEditor.rows.filter(row => !row.added).map(row => row.text);
      saved = await apiMutation('/api/incident-cases', 'POST', {...payload, service: incEditor.service});
    }
    document.getElementById('incident-modal').classList.remove('open');
    if (incEditor.mode === 'analysis') {
      insToast(`Saved as ${saved.id} in Incidents. It is recalled when this pattern appears again.`);
      await insReload();
    } else {
      incToast(`${saved.id} saved.`);
      inc.selected = saved.id;
      inc.signature = null;
      renderIncidents(await fetchIncidents());
    }
  } catch (err) {
    document.getElementById('incident-error').textContent = err.message;
  } finally { if (submit) submit.disabled = false; }
});

document.getElementById('inc-new').addEventListener('click', () => openIncidentEditor({mode: 'new'}));
// "Open in Incidents" from a recall panel selects that incident on the page.
$insDetail.addEventListener('click', event => {
  const link = event.target.closest('[data-open-incident]');
  if (!link) return;
  inc.selected = link.dataset.openIncident;
  inc.service = 'all';
  inc.tab = 'incidents';
  inc.signature = null;
});
