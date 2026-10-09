// ── Incident history page ──
const inc = {cases: [], service: 'all', selected: null, signature: null, toastTimer: null, patternOpen: false};
const $incList = document.getElementById('inc-list');
const $incDetail = document.getElementById('inc-detail');
const $incService = document.getElementById('inc-service');

async function fetchIncidents() {
  const resp = await fetch('/api/incident-cases');
  if (!resp.ok) throw new Error('Unable to load incidents');
  return resp.json();
}

function incToast(message, isError = false) {
  const $toast = document.getElementById('inc-toast');
  $toast.textContent = message;
  $toast.className = 'ins-toast' + (isError ? ' ins-toast-error' : '');
  clearTimeout(inc.toastTimer);
  inc.toastTimer = setTimeout(() => { $toast.textContent = ''; }, isError ? 10000 : 6000);
}

function renderIncidents(data) {
  if (data) inc.cases = data.cases || [];
  const signature = JSON.stringify([inc.cases, inc.service, inc.selected]);
  if (signature === inc.signature) return;  // keep focus between polls
  inc.signature = signature;
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

$incService.addEventListener('change', () => { inc.service = $incService.value; renderIncidents(); });
$incList.addEventListener('click', event => {
  const button = event.target.closest('[data-inc-id]');
  if (!button) return;
  inc.selected = button.dataset.incId;
  renderIncidents();
  const again = $incList.querySelector(`[data-inc-id="${CSS.escape(inc.selected)}"]`);
  if (again) again.focus();
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
  inc.signature = null;
});
