// ── AI Insights ──
const ins = {
  tab: 'windows', data: null, signature: null,
  filter: {windows: 'attention', services: 'all', templates: 'all'},
  selected: {windows: null, services: null, templates: null},
  deleteTarget: null, toastTimer: null,
};
const INS_FILTERS = {
  windows: [['attention', 'Needs attention'], ['analyzed', 'Analyzed'], ['all', 'All windows']],
  services: [['all', 'All services'], ['analyzed', 'Analyzed']],
  templates: [['all', 'All unknown'], ['suspicious', 'Suspicious'], ['untriaged', 'Not triaged']],
};
// Language of the AI's replies, remembered per browser; UI labels stay English.
const $insLanguage = document.getElementById('ins-language');
try { if (localStorage.getItem('logai.aiLanguage') === 'vi') $insLanguage.value = 'vi'; } catch (e) { /* storage unavailable */ }
$insLanguage.addEventListener('change', () => {
  try { localStorage.setItem('logai.aiLanguage', $insLanguage.value); } catch (e) { /* storage unavailable */ }
});
const INS_ERRORS = {engine_unavailable: 'The analysis engine is not running', analysis_pending: 'An analysis is already running'};
const $insList = document.getElementById('ins-list');
const $insDetail = document.getElementById('ins-detail');
const $insToast = document.getElementById('ins-toast');

async function fetchInsights() {
  const resp = await fetch('/api/insights');
  if (!resp.ok) throw new Error('Unable to load AI insights');
  return resp.json();
}

function insToast(message, isError = false) {
  $insToast.textContent = message;
  $insToast.className = 'ins-toast' + (isError ? ' ins-toast-error' : '');
  clearTimeout(ins.toastTimer);
  ins.toastTimer = setTimeout(() => { $insToast.textContent = ''; }, isError ? 10000 : 6000);
}

function insItems(tab) {
  const data = ins.data || {};
  if (tab === 'windows') return (data.windows || []).map(w => ({id: w.key, kind: 'window', raw: w, analysis: w.analysis}));
  if (tab === 'services') return (data.services || []).map(s => ({id: s.service, kind: 'service', raw: s, analysis: s.analysis}));
  return (data.templates || []).map(t => ({id: t.template_id, kind: 'template', raw: t, analysis: t.analysis}));
}

function insFiltered(tab) {
  const filter = ins.filter[tab];
  return insItems(tab).filter(item => {
    if (filter === 'analyzed') return Boolean(item.analysis);
    if (filter === 'attention') return item.raw.alert_state !== 'NORMAL' || Boolean(item.analysis);
    if (filter === 'suspicious') return item.analysis && item.analysis.verdict === 'suspicious';
    if (filter === 'untriaged') return !item.analysis;
    return true;
  });
}

function insItemHtml(item, selected) {
  const r = item.raw;
  const status = {...insightStatus(item.kind, item.analysis)};
  const recalled = item.analysis && item.analysis.recalled;
  if (recalled && recalled.length) status.label += ` · ↺ ${recalled[0].id}`;
  let badge = '', title = '', sub = '';
  if (item.kind === 'window') {
    badge = `<span class="badge alert-state-${escapeHtml(r.alert_state.toLowerCase())}">${escapeHtml(r.alert_state)}</span>`;
    title = `${r.group_id} · ${r.service}`;
    sub = r.representative_template || '';
  } else if (item.kind === 'service') {
    title = r.service;
    sub = item.analysis && item.analysis.summary ? item.analysis.summary : 'Whole-service health review';
  } else {
    badge = `<span class="badge ${levelBadgeClass(r.level)}">${escapeHtml(r.level)}</span>`;
    title = `${r.template_id} · ${r.service}`;
    sub = r.template_text || '';
  }
  return `<li><button type="button" class="ins-item" data-ins-id="${escapeHtml(item.id)}"${selected ? ' aria-current="true"' : ''}>
    <span class="ins-item-top">${badge}<span class="ins-item-title">${escapeHtml(title)}</span></span>
    <span class="ins-item-sub">${escapeHtml(sub)}</span>
    <span class="ins-item-status ${status.cls}"${status.busy ? ' aria-busy="true"' : ''}>${escapeHtml(status.label)}</span>
  </button></li>`;
}

function renderInsights(data) {
  if (data) ins.data = data;
  const signature = JSON.stringify([ins.data, ins.tab, ins.filter, ins.selected]);
  if (signature === ins.signature) return;  // keep focus/selection between polls
  ins.signature = signature;
  renderLlmBanner(ins.data.llm, 'ins-llm-banner');
  const counts = {
    windows: (ins.data.windows || []).filter(w => w.alert_state !== 'NORMAL').length,
    services: (ins.data.services || []).length,
    templates: (ins.data.templates || []).length,
  };
  for (const tab of ['windows', 'services', 'templates']) {
    document.getElementById(`ins-count-${tab}`).textContent = counts[tab];
  }
  document.querySelectorAll('[data-ins-tab]').forEach(button => {
    const active = button.dataset.insTab === ins.tab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
  });
  const $filter = document.getElementById('ins-filter');
  $filter.innerHTML = INS_FILTERS[ins.tab].map(([value, label]) =>
    `<option value="${value}"${value === ins.filter[ins.tab] ? ' selected' : ''}>${label}</option>`).join('');
  document.getElementById('ins-triage-all').hidden = ins.tab !== 'templates';

  const items = insFiltered(ins.tab);
  if (!items.some(item => item.id === ins.selected[ins.tab])) {
    ins.selected[ins.tab] = items.length ? items[0].id : null;
  }
  $insList.innerHTML = items.length
    ? items.map(item => insItemHtml(item, item.id === ins.selected[ins.tab])).join('')
    : `<li class="ins-empty" style="padding:16px">${ins.tab === 'templates' ? 'No unknown templates. New log patterns that fit no group will appear here.' : 'Nothing matches this filter.'}</li>`;
  const selected = items.find(item => item.id === ins.selected[ins.tab]);
  $insDetail.innerHTML = selected ? insDetailHtml(selected) : '<p class="ins-empty">Select an item to see its AI analysis.</p>';
}

function insMeta(a) {
  if (!a || !a.analyzed_at) return '';
  const kTokens = n => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
  const cost = [
    typeof a.duration_s === 'number' ? `${a.duration_s.toFixed(1)} s` : '',
    a.usage ? `${kTokens(a.usage.prompt_tokens || 0)} → ${kTokens(a.usage.completion_tokens || 0)} tokens` : '',
    a.attempts > 1 ? 'retried' : '',
  ].filter(Boolean).map(part => ' · ' + escapeHtml(part)).join('');
  return `<p class="ins-meta">Analyzed ${escapeHtml(formatFullTs(a.analyzed_at))}${a.model ? ' · ' + escapeHtml(a.model) : ''}${a.language ? ' · ' + escapeHtml(a.language.toUpperCase()) : ''}${cost}</p>`;
}

const INS_CASE_BUTTON = '<button type="button" class="btn" data-ins-action="case">Save as service incident</button>';

function insSuggestionHtml(suggestion, groupIds, index) {
  const target = groupIds.length ? ` and assign to ${groupIds.join(', ')}` : '';
  return `<div class="detail-value"><strong>${escapeHtml(suggestion.title)}</strong>${suggestion.error_code ? ` <span class="tmpl-id">${escapeHtml(suggestion.error_code)}</span>` : ''}</div>
    <div class="ai-text">${escapeHtml(suggestion.text)}</div>
    <div class="ins-actions"><button type="button" class="btn btn-primary" data-ins-action="save" data-ins-suggestion="${index}">Save as document${escapeHtml(target)}</button></div>`;
}

function insActions(item, extra = '') {
  const a = item.analysis;
  const running = a && (a.status === 'requested' || a.status === 'pending');
  const llm = (ins.data && ins.data.llm) || {};
  const blocked = !llm.alive || !llm.llm_enabled;
  const analyzeLabel = a ? 'Re-analyze' : (item.kind === 'template' ? 'Triage with AI' : 'Analyze with AI');
  const analyze = running ? '' : `<button type="button" class="btn${a ? '' : ' btn-primary'}" data-ins-action="analyze"${blocked ? ' disabled aria-describedby="ins-llm-banner"' : ''}>${analyzeLabel}</button>`;
  const del = a && !running ? '<button type="button" class="btn btn-danger" data-ins-action="delete">Delete analysis</button>' : '';
  return `<div class="ins-actions">${extra}${analyze}${del}</div>`;
}

function insAnalysisBody(item) {
  const a = item.analysis;
  ins.suggestions = [];
  if (!a) return `<p class="ins-empty">Not analyzed yet. The AI ${item.kind === 'template' ? 'judges whether this pattern looks suspicious and which group it belongs to' : item.kind === 'service' ? 'reviews every group of this service and lists the important issues' : 'matches this incident to a document or suggests a fix'}.</p>`;
  if (a.status === 'requested' || a.status === 'pending') return '<p class="ins-empty" aria-busy="true">Analyzing… this usually takes 5–15 seconds.</p>';
  if (a.status === 'failed') return `<p class="st-failed">Analysis failed: ${escapeHtml(a.error || 'unknown error')}</p>${insMeta(a)}`;
  if (item.kind === 'window') {
    if (a.documentation_id) {
      return `<p><span class="badge badge-blue">Matched</span> <strong>${escapeHtml(a.documentation_id)}</strong> ${escapeHtml(a.document_title || '')}</p>
        <p class="ins-meta">Confidence ${Math.round((a.confidence || 0) * 100)}%</p>
        <p class="ins-why">${escapeHtml(a.reasoning || '')}</p>${insMeta(a)}`;
    }
    ins.suggestions = [{suggestion: a.suggestion, groups: [item.raw.group_id]}];
    return `<p><span class="badge badge-yellow">No matching document</span></p>
      <p class="ins-why">${escapeHtml(a.reasoning || '')}</p>
      <h3 style="margin-top:12px">Suggested fix</h3>${insSuggestionHtml(a.suggestion, [item.raw.group_id], 0)}${insMeta(a)}`;
  }
  if (item.kind === 'service') {
    const health = ['healthy', 'degraded', 'critical'].includes(a.health) ? a.health : 'unknown';
    const color = {healthy: 'green', degraded: 'yellow', critical: 'red', unknown: 'gray'}[health];
    const issues = a.issues || [];
    ins.suggestions = issues.map(issue => ({suggestion: issue.suggestion, groups: issue.group_ids || []}));
    return `<p><span class="badge badge-${color}">${health[0].toUpperCase() + health.slice(1)}</span></p>
      <p style="line-height:1.5">${escapeHtml(a.summary || '')}</p>${insMeta(a)}
      ${issues.length ? `<div class="ins-section"><h3>Issues (${issues.length})</h3><ol class="ins-issues">${issues.map((issue, index) => `
        <li><strong>${escapeHtml(issue.title)}</strong>
          ${(issue.group_ids || []).length ? `<div class="ins-chips">${issue.group_ids.map(g => `<span class="tmpl-id">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
          ${issue.reasoning ? `<p class="ins-why">${escapeHtml(issue.reasoning)}</p>` : ''}
          ${issue.documentation_id
            ? `<span class="badge badge-blue">${escapeHtml(issue.documentation_id)}</span> ${escapeHtml(issue.document_title || '')}`
            : insSuggestionHtml(issue.suggestion, issue.group_ids || [], index)}

        </li>`).join('')}</ol></div>` : '<p class="ins-empty">No issues found.</p>'}`;
  }
  const verdict = ['suspicious', 'benign', 'unsure'].includes(a.verdict) ? a.verdict : 'unsure';
  const color = {suspicious: 'red', benign: 'green', unsure: 'gray'}[verdict];
  let group = '<p class="ins-empty">No group suggested.</p>';
  if (a.suggested_group_id === 'new') {
    group = `<p>Belongs in a <strong>new group</strong>; no existing group fits.</p><div class="ins-actions"><button type="button" class="btn btn-primary" data-ins-action="move" data-ins-group="">Create new group</button></div>`;
  } else if (a.suggested_group_id) {
    group = `<p>Belongs with <strong>${escapeHtml(a.suggested_group_id)}</strong></p><div class="ins-code">${escapeHtml(a.suggested_group_rep || '')}</div>
      <div class="ins-actions"><button type="button" class="btn btn-primary" data-ins-action="move" data-ins-group="${escapeHtml(a.suggested_group_id)}">Move to ${escapeHtml(a.suggested_group_id)}</button></div>`;
  }
  return `<p><span class="badge badge-${color}">${verdict[0].toUpperCase() + verdict.slice(1)}</span> <span class="ins-meta">Confidence ${Math.round((a.confidence || 0) * 100)}%</span></p>
    <p class="ins-why">${escapeHtml(a.reasoning || '')}</p>${insMeta(a)}
    <div class="ins-section"><h3>Suggested group</h3>${group}</div>`;
}

// "This happened before": incidents the engine recalled for this analysis,
// shown with their CURRENT text so edits made from experience appear here.
function insRecallHtml(a) {
  const recalled = (a && a.recalled) || [];
  if (!recalled.length) return '';
  const incidents = (ins.data && ins.data.incidents) || {};
  const fmt = (x, digits = 1) => x === null || x === undefined ? '—' : Number(x).toFixed(digits);
  const side = v => v ? `${fmt(v.rate_per_min)}/min${v.ratio === null || v.ratio === undefined ? '' : ` · ${fmt(v.ratio)}×`}` : '<span class="ins-meta">not abnormal now</span>';
  return `<div class="inc-recall" role="region" aria-label="Past incidents"><h3>↺ This happened before</h3>${recalled.map(r => {
    const c = incidents[r.id];
    const pct = Math.round((r.overlap || 0) * 100);
    const used = a.similar_case_id === r.id ? ' · <span class="st-matched">used by the AI ✓</span>' : '';
    if (!c) return `<div class="inc-recall-item"><span class="ins-meta">${escapeHtml(r.id)} · ${pct}% match · this incident was deleted</span></div>`;
    const comparison = r.comparison || [];
    const onlyNow = r.only_now || [];
    return `<div class="inc-recall-item">
      <div><strong>${escapeHtml(r.id)}</strong> ${escapeHtml(c.title || '')}</div>
      <div class="ins-meta">${pct}% of its error pattern is back${c.occurred_at ? ` · last time ${escapeHtml(formatFullTs(c.occurred_at))}` : ''}${used}</div>
      <p><strong>Root cause:</strong> ${escapeHtml(c.root_cause || '')}</p>
      ${c.resolution ? `<p><strong>Resolution:</strong> ${escapeHtml(c.resolution)}</p>` : ''}
      ${c.documentation_id ? `<div>Runbook <span class="badge badge-blue">${escapeHtml(c.documentation_id)}</span></div>` : ''}
      ${comparison.length ? `<details><summary>Then vs now (${comparison.length} templates)</summary><div class="table-wrap"><table>
        <thead><tr><th scope="col">Template</th><th scope="col">Then</th><th scope="col">Now</th></tr></thead>
        <tbody>${comparison.map(row => `<tr><td class="mono">${highlightWildcards(escapeHtml(row.text))}</td><td>${side(row.then)}</td><td>${side(row.now)}</td></tr>`).join('')}</tbody>
      </table></div></details>` : ''}
      ${onlyNow.length ? `<details><summary>New this time (${onlyNow.length})</summary>${onlyNow.map(t => `<div class="ins-code">${highlightWildcards(escapeHtml(t))}</div>`).join('')}</details>` : ''}
      <div class="ins-actions"><a class="btn" href="#incidents" data-open-incident="${escapeHtml(r.id)}">Open in Incidents</a></div>
    </div>`;
  }).join('')}</div>`;
}

function incidentPatternHtml(c) {
  const pattern = c.pattern || [];
  // Incidents saved before numbers were recorded only have the texts.
  if (!pattern.length) {
    return (c.template_texts || []).map(t => `<div class="ins-code">${highlightWildcards(escapeHtml(t))}</div>`).join('');
  }
  const num = (value, digits = 1) => value === null || value === undefined ? '—' : Number(value).toFixed(digits);
  const totals = c.totals || {};
  return `${totals.events_15m !== null && totals.events_15m !== undefined
      ? `<p class="ins-meta">Service at the time: ${formatCount(totals.events_15m)} events in 15 min, ${formatCount(totals.error_events_15m || 0)} of them ERROR or worse</p>` : ''}
    <div class="table-wrap"><table>
      <thead><tr><th scope="col">Template</th><th scope="col">Level</th><th scope="col">Rate /min</th><th scope="col">Normal /min</th><th scope="col">× normal</th><th scope="col">Why</th></tr></thead>
      <tbody>${pattern.map(p => `<tr>
        <td class="mono">${highlightWildcards(escapeHtml(p.text))}</td>
        <td><span class="badge ${levelBadgeClass(p.level || 'INFO')}">${escapeHtml(p.level || '—')}</span></td>
        <td>${num(p.rate_per_min)}</td><td>${num(p.baseline_per_min, 2)}</td>
        <td><strong>${p.ratio === null || p.ratio === undefined ? '—' : num(p.ratio) + '×'}</strong></td>
        <td>${(p.reasons || []).map(r => `<span class="tmpl-id">${escapeHtml(r)}</span>`).join(' ')}</td>
      </tr>`).join('')}</tbody>
    </table></div>`;
}

function incidentDetailHtml(c) {
  return `<div class="ins-head"><h2>${escapeHtml(c.id)} · ${escapeHtml(c.service)}</h2>
      <span class="ins-meta">${c.occurred_at ? `occurred ${escapeHtml(formatFullTs(c.occurred_at))}` : 'written by hand'}${c.updated_at ? ` · edited ${escapeHtml(formatFullTs(c.updated_at))}` : ''}${c.language ? ' · ' + escapeHtml(c.language.toUpperCase()) : ''}</span></div>
    <p><strong>${escapeHtml(c.title || '')}</strong></p>
    ${(c.group_ids || []).length ? `<div class="ins-chips">${c.group_ids.map(g => `<span class="tmpl-id">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
    <div class="ins-section"><h3>Root cause</h3><p class="ins-why" style="white-space:pre-line">${escapeHtml(c.root_cause || '')}</p></div>
    ${c.resolution ? `<div class="ins-section"><h3>Resolution</h3><div class="ai-text" style="white-space:pre-line">${escapeHtml(c.resolution)}</div></div>` : ''}
    ${c.documentation_id ? `<p>Runbook <span class="badge badge-blue">${escapeHtml(c.documentation_id)}</span></p>` : ''}
    <div class="ins-section"><h3>Error pattern (${(c.template_texts || []).length} templates)</h3>${incidentPatternHtml(c)}</div>
    <div class="ins-actions"><button type="button" class="btn btn-primary" data-inc-action="edit">Edit</button><button type="button" class="btn btn-danger" data-ins-action="delete-case">Delete incident</button></div>`;
}

function insDetailHtml(item) {
  const r = item.raw;
  let head = '';
  if (item.kind === 'window') {
    head = `<div class="ins-head"><h2>${escapeHtml(r.group_id)} · ${escapeHtml(r.service)}</h2>
        <span class="badge alert-state-${escapeHtml(r.alert_state.toLowerCase())}">${escapeHtml(r.alert_state)}</span>
        <span class="ins-meta">score ${Number(r.anomaly_score || 0).toFixed(3)} · ${escapeHtml(r.level)}</span></div>
      <div class="ins-code">${highlightWildcards(escapeHtml(r.representative_template || '—'))}</div>`;
  } else if (item.kind === 'service') {
    head = `<div class="ins-head"><h2>${escapeHtml(r.service)}</h2><span class="ins-meta">whole-service review</span></div>`;
  } else {
    head = `<div class="ins-head"><h2>${escapeHtml(r.template_id)} · ${escapeHtml(r.service)}</h2>
        <span class="badge ${levelBadgeClass(r.level)}">${escapeHtml(r.level)}</span>
        <span class="ins-meta">${formatCount(r.event_count || 0)} events · last seen ${escapeHtml(formatTs(r.last_seen))}</span></div>
      <div class="ins-code">${highlightWildcards(escapeHtml(r.template_text || '—'))}</div>`;
  }
  const done = item.analysis && item.analysis.status === 'done';
  const openDoc = item.kind === 'window' && done && item.analysis.documentation_id
    ? '<a class="btn" href="#documentation">Open documentation</a>' : '';
  const saveCase = item.kind === 'service' && done ? INS_CASE_BUTTON : '';
  const recall = item.kind === 'template' ? '' : insRecallHtml(item.analysis);
  return `${head}${recall}<div class="ins-section"><h3>AI analysis</h3>${insAnalysisBody(item)}</div>${insActions(item, openDoc + saveCase)}`;
}

function insSelectedItem() {
  return insItems(ins.tab).find(item => item.id === ins.selected[ins.tab]);
}

async function insReload() {
  ins.signature = null;
  renderInsights(await fetchInsights());
}

document.querySelectorAll('[data-ins-tab]').forEach(button => button.addEventListener('click', () => {
  ins.tab = button.dataset.insTab;
  renderInsights();
}));
// Arrow keys move between the tabs (roving tabindex, as for any tablist).
document.querySelector('[role="tablist"][aria-label="Insight type"]').addEventListener('keydown', event => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  const tabs = [...document.querySelectorAll('[data-ins-tab]')];
  const index = tabs.indexOf(document.activeElement);
  if (index < 0) return;
  event.preventDefault();
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
    : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next].click();
  tabs[next].focus();
});
document.getElementById('ins-filter').addEventListener('change', event => {
  ins.filter[ins.tab] = event.target.value;
  renderInsights();
});
$insList.addEventListener('click', event => {
  const button = event.target.closest('[data-ins-id]');
  if (!button) return;
  ins.selected[ins.tab] = button.dataset.insId;
  renderInsights();
  const again = $insList.querySelector(`[data-ins-id="${CSS.escape(button.dataset.insId)}"]`);
  if (again) again.focus();
});
document.getElementById('ins-triage-all').addEventListener('click', async event => {
  event.currentTarget.disabled = true;
  try {
    const result = await apiMutation('/api/insights/analyze', 'POST', {kind: 'templates_all', language: $insLanguage.value});
    insToast(result.queued ? `Queued ${result.queued} template(s) for triage.` : 'Every unknown template is already triaged or queued.');
    await insReload();
  } catch (err) { insToast(INS_ERRORS[err.payload && err.payload.error] || err.message, true); }
  finally { event.currentTarget.disabled = false; }
});
$insDetail.addEventListener('click', async event => {
  const button = event.target.closest('[data-ins-action]');
  const item = insSelectedItem();
  if (!button || !item) return;
  const action = button.dataset.insAction;
  if (action === 'save') {
    const entry = (ins.suggestions || [])[Number(button.dataset.insSuggestion)];
    if (entry && entry.suggestion) prefillDocumentEditor(entry.suggestion, entry.groups);
    return;
  }
  if (action === 'case') {
    // One incident = what the whole service is suffering now, pre-filled
    // from the service analysis: summary + every issue's cause and fix.
    const a = item.analysis || {};
    const issues = a.issues || [];
    const signature = a.signature || {};
    openIncidentEditor({
      mode: 'analysis', service: item.id,
      title: (issues[0] && issues[0].title) || `${item.id} incident`,
      root_cause: [a.summary, ...issues.map(i => i.reasoning && `- ${i.title}: ${i.reasoning}`)].filter(Boolean).join('\n'),
      resolution: issues.map(i => i.suggestion ? `- ${i.title}: ${i.suggestion.text}`
        : i.documentation_id ? `- ${i.title}: see ${i.documentation_id}` : '').filter(Boolean).join('\n'),
      documentation_id: (issues.find(i => i.documentation_id) || {}).documentation_id || '',
      rows: signature.templates || (signature.template_texts || []).map(text => ({text})),
    });
    return;
  }
  if (action === 'delete') {
    ins.deleteTarget = item;
    document.getElementById('insight-delete-message').textContent = `The AI analysis of ${item.kind === 'window' ? item.raw.group_id + ' · ' + item.raw.service : item.id} will be deleted.`;
    document.getElementById('insight-delete-modal').classList.add('open');
    document.getElementById('insight-delete-confirm').focus();
    return;
  }
  button.disabled = true;
  try {
    if (action === 'analyze') {
      await apiMutation('/api/insights/analyze', 'POST', {kind: item.kind, id: item.id, language: $insLanguage.value});
      insToast('Analysis requested. The result appears here in a few seconds.');
    } else if (action === 'move') {
      const target = button.dataset.insGroup;
      await apiMutation(`/api/templates/${encodeURIComponent(item.id)}/group`, 'PUT',
        target ? {target_group_id: target, expected_revision: ins.data.grouping_revision}
               : {create_new: true, expected_revision: ins.data.grouping_revision});
      insToast(`${item.id} will move to ${target || 'a new group'} once the engine applies the change.`);
    }
    await insReload();
  } catch (err) {
    insToast(INS_ERRORS[err.payload && err.payload.error] || err.message, true);
    button.disabled = false;
  }
});


document.getElementById('insight-delete-confirm').addEventListener('click', async event => {
  const item = ins.deleteTarget;
  if (!item) return;
  event.currentTarget.disabled = true;
  try {
    await apiMutation('/api/insights/delete', 'POST', {kind: item.kind, id: item.id});
    document.getElementById('insight-delete-modal').classList.remove('open');
    insToast('Analysis deleted.');
    await insReload();
  } catch (err) { insToast(err.message, true); }
  finally { event.currentTarget.disabled = false; ins.deleteTarget = null; }
});
