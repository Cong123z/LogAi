// ── Documentation ──
// Reading-first: a list on the left, the selected document in full on the right, actions pinned
// under it. A document is ERROR (the log lines it is for: the only part matched against templates)
// plus Handling (what to do). "AI suggestions" lists the LLM's drafts with the template each was
// written for: Approve (link becomes manual, document active), Edit (saving approves), Reject.
// Every document is of one kind: ERROR (verdict "action": something to fix) or SAFE (normal operation);
// the All / ERROR / SAFE buttons filter the list.
// Documents belong to a service. The filter shows one service's documents, or all of them; "All
// services" groups documents with the same ERROR line into one row, with a version per service.
const docs = {mode: 'active', items: [], drafts: new Map(), selected: null, editing: false, query: '', kind: '', level: '', levelOrder: [],
              service: '', services: []};
const $docService = document.getElementById('doc-service');
const svcLabel = service => service || 'shared';
const svcChip = service => `<span class="svc-chip${service ? '' : ' shared'}" title="${service ? 'Service' : 'Shared: applies to every service'}">${escapeHtml(svcLabel(service))}</span>`;
// Same ERROR = the same normalized ERROR lines; a document without one is its own group.
const errorKey = doc => (doc.error_lines.length ? `e:${doc.error_lines.join('\n')}` : `d:${doc.doc_id}`);
const $docList = document.getElementById('doc-list');
const $docPane = document.getElementById('doc-pane');
const $docSearch = document.getElementById('doc-search');

const isDraftMode = () => docs.mode === 'draft';
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const docUrl = id => `/api/documents/${encodeURIComponent(id)}`;
const draftLinkUrl = (t, decision) =>
  `/api/templates/${encodeURIComponent(t.service)}/${encodeURIComponent(t.template_id)}/link/${decision}`;

// #documentation/suggestions opens the AI suggestions tab (links from the LLM and Pipeline pages);
// #documentation/<doc_id> and #documentation/suggestions/<doc_id> also select that document (links from Alerting).
let docLastSub = null;
function syncDocModeFromRoute() {
  const sub = state.subView || '';
  const [head, ...rest] = sub.split('/');
  const suggestions = head === 'suggestions';
  const mode = suggestions ? 'draft' : 'active';
  if (mode !== docs.mode) {
    docs.mode = mode;
    docs.selected = null;
    docs.editing = false;
    document.querySelectorAll('#doc-mode-toggle .toggle-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.docMode === mode);
      b.setAttribute('aria-selected', String(b.dataset.docMode === mode));
    });
  }
  if (sub === docLastSub) return;   // later loads (after an edit) keep the user's selection
  docLastSub = sub;
  let id = suggestions ? rest.join('/') : sub;
  try { id = decodeURIComponent(id); } catch (e) { id = ''; }
  if (id) { docs.selected = id; docs.editing = false; }
}

async function loadDocumentation() {
  syncDocModeFromRoute();
  const [items, drafts, draftLinks] = await Promise.all([
    getJson(`/api/documents?status=${docs.mode}`, 'Unable to load documentation'),
    isDraftMode() ? null : getJson('/api/documents?status=draft', 'Unable to load AI suggestions'),
    isDraftMode() ? getJson('/api/templates?link_source=draft&size=500', 'Unable to load AI suggestions') : null,
  ]);
  docs.items = items;
  if (isDraftMode()) docs.levelOrder = (await getJson('/api/template-levels', 'Unable to load levels')).map(l => l.level);
  docs.services = await getJson('/api/services', 'Unable to load services');
  renderServiceOptions();
  if (draftLinks) docs.drafts = new Map(draftLinks.items.map(t => [t.doc_id, t]));
  const nDrafts = drafts ? drafts.length : items.length;
  const $count = document.getElementById('doc-draft-count');
  $count.hidden = !nDrafts;
  $count.textContent = nDrafts;
  const shown = docs.items.filter(docMatches);
  if (!shown.some(d => d.doc_id === docs.selected) && !(docs.editing && docs.selected === null)) {
    docs.selected = shown.length ? shown[0].doc_id : null;
    docs.editing = false;
  }
  renderDocumentation();
}

function renderServiceOptions() {
  const current = docs.service;
  $docService.innerHTML = '<option value="">All services</option>'
    + docs.services.map(svc => `<option value="${escapeHtml(svc)}">${escapeHtml(svc)}</option>`).join('');
  $docService.value = current;
}

function docMatches(doc) {
  if (docs.kind && doc.verdict !== docs.kind) return false;
  if (docs.level && doc.template_level !== docs.level) return false;
  if (docs.service && doc.service !== docs.service) return false;
  const q = docs.query.toLowerCase();
  if (!q) return true;
  const t = docs.drafts.get(doc.doc_id);
  return [doc.doc_id, doc.title, doc.error_code, doc.error, doc.handling, t && t.text, t && t.template_id]
    .some(v => v && String(v).toLowerCase().includes(q));
}

// Handling as written, but numbered steps run together on one line ("1. … 2. …", as LLMs often
// write them) are put one per line so they can be read and checked step by step.
function handlingText(text) {
  if (text.includes('\n') || (text.match(/(?:^|\s)\d{1,2}\.\s/g) || []).length < 2) return text;
  return text.replace(/\s+(?=\d{1,2}\.\s)/g, '\n');
}

function verdictBadge(doc) {
  return doc.verdict === 'safe'
    ? '<span class="badge badge-green" title="Normal operation, no action needed">SAFE</span>'
    : '<span class="badge badge-error" title="An error: something to fix">ERROR</span>';
}

function renderKindCounts() {
  const inService = d => !docs.service || d.service === docs.service;
  const n = kind => docs.items.filter(d => inService(d) && (!kind || d.verdict === kind)).length;
  document.querySelectorAll('#doc-kind-toggle .kind-count').forEach(el => { el.textContent = n(el.dataset.count); });
}

// AI suggestions: one button per level of the templates the drafts were written for.
function renderLevelToggle() {
  const $box = document.getElementById('doc-level-toggle');
  const counts = new Map();
  if (isDraftMode()) docs.items.filter(d => (!docs.service || d.service === docs.service) && d.template_level)
    .forEach(d => counts.set(d.template_level, (counts.get(d.template_level) || 0) + 1));
  if (docs.level && !counts.has(docs.level)) docs.level = '';
  $box.hidden = !counts.size;
  const rank = l => docs.levelOrder.indexOf(l) < 0 ? docs.levelOrder.length : docs.levelOrder.indexOf(l);   // server order (LEVEL_ORDER)
  const levels = [...counts.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  $box.innerHTML = [['', 'Any level', [...counts.values()].reduce((a, b) => a + b, 0)], ...levels.map(l => [l, l, counts.get(l)])]
    .map(([v, label, n]) => `<button class="toggle-btn${v === docs.level ? ' active' : ''}" data-doc-level="${escapeHtml(v)}" aria-pressed="${v === docs.level}">${escapeHtml(label)} <span class="kind-count">${n}</span></button>`).join('');
}

function renderDocumentation() {
  const drafting = isDraftMode();
  document.getElementById('add-document-btn').hidden = drafting;
  document.getElementById('approve-safe-btn').hidden =
    !drafting || !docs.items.some(d => d.verdict === 'safe' && docs.drafts.has(d.doc_id));
  const inService = docs.items.filter(d => !docs.service || d.service === docs.service);
  const linked = inService.filter(d => d.linked_templates > 0).length;
  document.getElementById('doc-sync-status').textContent = drafting
    ? `${plural(inService.length, 'suggestion')} to review. Unverified: check commands before running them.`
    : `${plural(inService.length, 'document')} · ${linked} linked to templates`;
  renderKindCounts();
  renderLevelToggle();
  renderDocList();
  renderDocPane();
}

function renderDocList() {
  const shown = docs.items.filter(docMatches);
  if (!shown.length) {
    $docList.innerHTML = `<p class="empty-state">${docs.items.length
      ? `No ${docs.kind ? (docs.kind === 'safe' ? 'SAFE ' : 'ERROR ') : ''}document${docs.query ? ' matches this search' : 's here'}.`
      : isDraftMode() ? 'No AI suggestions waiting. New ones appear as the LLM writes them.' : 'No documents yet.'}</p>`;
    return;
  }
  const groups = new Map();
  for (const doc of shown) {
    const key = docs.service ? `d:${doc.doc_id}` : errorKey(doc);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(doc);
  }
  $docList.innerHTML = [...groups.values()].map(group => {
    const doc = group.find(d => d.doc_id === docs.selected) || group[0];
    const t = docs.drafts.get(doc.doc_id);
    const top = isDraftMode()
      ? `<span class="tmpl-id">${escapeHtml(doc.doc_id)}</span>${verdictBadge(doc)}${doc.confidence == null ? ''
          : `<span class="split-item-meta">confidence ${doc.confidence.toFixed(2)}</span>`}`
      : `<span class="tmpl-id">${escapeHtml(doc.doc_id)}</span>${verdictBadge(doc)}${doc.error_lines.length ? ''
          : '<span class="badge badge-gray" title="No ERROR line: never matched automatically">no log line</span>'}<span class="split-item-meta">${plural(doc.linked_templates, 'template')}</span>`;
    const meta = isDraftMode()
      ? (t ? `${escapeHtml(t.service)} · ${escapeHtml(t.template_id)} · ${escapeHtml(t.level)}` : 'its template is linked otherwise now')
      : escapeHtml(doc.error_code || '');
    const services = group.length > 1
      ? `<span class="split-item-top"><span class="split-item-meta">same ERROR in ${group.length}:</span>${group.map(d => svcChip(d.service)).join('')}</span>`
      : `<span class="split-item-top">${svcChip(doc.service)}</span>`;
    return `<button type="button" class="split-item" role="option" data-doc-id="${escapeHtml(doc.doc_id)}"
        aria-selected="${group.some(d => d.doc_id === docs.selected)}">
      <span class="split-item-top">${top}</span>
      <span class="split-item-title">${escapeHtml(doc.title || 'Untitled')}</span>
      ${meta ? `<span class="split-item-meta">${meta}</span>` : ''}
      ${services}
    </button>`;
  }).join('');
}

function selectedDoc() { return docs.items.find(d => d.doc_id === docs.selected) || null; }

function renderDocPane() {
  if (docs.editing) return renderDocEditor();
  const doc = selectedDoc();
  if (!doc) {
    $docPane.innerHTML = `<div class="pane-empty">${isDraftMode() ? 'Nothing to review.' : 'Select a document to read it.'}</div>`;
    return;
  }
  const t = docs.drafts.get(doc.doc_id);
  const drafting = isDraftMode();
  const badges = [
    drafting ? '<span class="badge badge-yellow">AI suggestion · unverified</span>' : '',
    svcChip(doc.service),
    verdictBadge(doc),
    doc.level ? `<span class="badge ${levelBadgeClass(doc.level)}" title="Log level of its ERROR line">${escapeHtml(doc.level)} line</span>` : '',
    !drafting && doc.source === 'llm' ? '<span class="badge badge-yellow">AI-written, approved</span>' : '',
    doc.error_code ? `<span class="tmpl-id">${escapeHtml(doc.error_code)}</span>` : '',
    doc.confidence != null && drafting ? `<span>confidence ${doc.confidence.toFixed(2)}</span>` : '',
  ].join('');
  const written = drafting ? (t ? `
    <section class="pane-section">
      <h3>Written for this template</h3>
      <div class="pane-badges"><span class="tmpl-id">${escapeHtml(t.service)} · ${escapeHtml(t.template_id)}</span>
        <span class="badge ${levelBadgeClass(t.level)}">${escapeHtml(t.level)}</span>
        <span class="badge alert-state-${escapeHtml(t.state.toLowerCase())}">${escapeHtml(t.state)}</span>
        ${t.rate == null ? '' : `<span>${tplNum(t.rate)}/min this hour</span>`}</div>
      <div class="code-block" style="margin-top:6px">${highlightWildcards(escapeHtml(t.text))}</div>
    </section>` : '<div class="pane-callout">Its template has been linked another way since; this suggestion can only be edited or left.</div>') : '';
  const versions = docs.items.filter(d => doc.error_lines.length && errorKey(d) === errorKey(doc));
  const versionBar = versions.length > 1 ? `<div class="versions" role="group" aria-label="Same ERROR in other services">
      Same ERROR in ${versions.length} services:
      ${versions.map(d => `<button type="button" class="toggle-btn${d.doc_id === doc.doc_id ? ' active' : ''}" data-act="version"
        data-doc-id="${escapeHtml(d.doc_id)}" aria-pressed="${d.doc_id === doc.doc_id}">${escapeHtml(svcLabel(d.service))} · ${escapeHtml(d.doc_id)}</button>`).join('')}</div>` : '';
  const basedOn = doc.based_on && doc.based_on.length
    ? `<p class="hint">Adapted from ${doc.based_on.map(id => `<span class="tmpl-id">${escapeHtml(id)}</span>`).join(', ')}, written for another service.</p>` : '';
  const errorSection = doc.error
    ? `<div class="code-block">${highlightWildcards(escapeHtml(doc.error))}</div>
       ${doc.error_lines.length ? `<p class="hint">Matched as: <span class="tmpl-text">${highlightWildcards(escapeHtml(doc.error_lines.join(' | ')))}</span>${doc.embedded ? '' : ' (embedding…)'}</p>` : ''}`
    : '<p class="prose prose-empty">No ERROR line: this document is never matched automatically, only linked by hand or inherited.</p>';
  const actions = drafting
    ? (t ? `<button type="button" class="btn btn-primary" data-act="approve">Approve</button>
            <button type="button" class="btn" data-act="edit">Edit</button>
            <button type="button" class="btn btn-danger" data-act="reject">Reject</button>`
         : `<button type="button" class="btn" data-act="edit">Edit</button>`)
      + '<span class="spacer"></span><kbd>↑ ↓ move between suggestions</kbd>'
    : `<button type="button" class="btn btn-primary" data-act="edit">Edit</button>
       <button type="button" class="btn" data-act="kind">${doc.verdict === 'safe' ? 'Mark as ERROR' : 'Mark as SAFE'}</button>
       <button type="button" class="btn btn-danger" data-act="delete">Delete</button>`;
  $docPane.innerHTML = `
    <div class="pane-body">
      <header class="pane-head"><h2>${escapeHtml(doc.title || 'Untitled')}</h2>
        <div class="pane-badges"><span class="tmpl-id">${escapeHtml(doc.doc_id)}</span>${badges}</div>${versionBar}${basedOn}</header>
      ${drafting ? '<div class="pane-callout">Written by the LLM from the template text only. Read it in full and check every command before approving: approving links the template to it and makes it an active document.</div>' : ''}
      ${written}
      <section class="pane-section"><h3>ERROR · the log line(s) it is for</h3>${errorSection}</section>
      <section class="pane-section"><h3>Handling · what to do</h3>
        ${doc.handling ? `<div class="prose">${escapeHtml(handlingText(doc.handling))}</div>` : '<p class="prose prose-empty">No handling text.</p>'}</section>
      ${drafting ? '' : `<section class="pane-section"><h3>Linked templates (${doc.linked_templates})</h3><div id="doc-linked">${doc.linked_templates ? '<p class="loading">Loading…</p>' : '<p class="prose-empty">None.</p>'}</div></section>`}
    </div>
    <footer class="pane-actions">${actions}</footer>`;
  if (!drafting && doc.linked_templates) loadDocLinks(doc.doc_id);
}

async function loadDocLinks(docId) {
  try {
    const data = await getJson(`/api/templates?doc_id=${encodeURIComponent(docId)}&size=500`, 'Unable to load linked templates');
    const $box = document.getElementById('doc-linked');
    if (!$box || docs.selected !== docId) return;
    $box.innerHTML = `<div class="linked-list">${data.items.map(t => `
      <div class="linked-item">
        <span class="tmpl-id">${escapeHtml(t.service)} · ${escapeHtml(t.template_id)}</span>
        <span class="pane-badges"><span class="badge ${levelBadgeClass(t.level)}">${escapeHtml(t.level)}</span>${linkSourceBadge(t)}</span>
        <div class="tmpl-text">${highlightWildcards(escapeHtml(t.text))}</div>
      </div>`).join('')}</div>${data.total > data.items.length ? `<p class="hint">Showing ${data.items.length} of ${data.total}.</p>` : ''}`;
  } catch (err) {
    const $box = document.getElementById('doc-linked');
    if ($box) $box.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
  }
}

function renderDocEditor() {
  const doc = selectedDoc();
  const creating = !doc;
  const approving = isDraftMode() && doc && docs.drafts.has(doc.doc_id);
  $docPane.innerHTML = `
    <form class="pane" id="doc-form" style="position:static;border:0;max-height:none">
      <div class="pane-body">
        <header class="pane-head"><h2>${creating ? 'Add document' : `Edit ${escapeHtml(doc.doc_id)}`}</h2>
          ${approving ? '<div class="pane-callout">Saving approves this suggestion: the template is linked to it.</div>' : ''}</header>
        <div class="form-field"><label for="doc-f-title">Title</label><input id="doc-f-title" maxlength="200" required value="${escapeHtml(doc ? doc.title : '')}"></div>
        <div class="form-field"><label for="doc-f-code">Error code</label><input id="doc-f-code" maxlength="100" value="${escapeHtml(doc ? doc.error_code : '')}"></div>
        <div class="form-field"><label for="doc-f-service">Service</label>
          <select id="doc-f-service">
            <option value="">Shared: every service</option>
            ${docs.services.map(svc => `<option value="${escapeHtml(svc)}"${(doc ? doc.service === svc : docs.service === svc) ? ' selected' : ''}>${escapeHtml(svc)}</option>`).join('')}
          </select></div>
        <div class="form-field"><label for="doc-f-level">Log level of the ERROR line</label>
          <select id="doc-f-level">
            ${[['', 'Any level'], ['ERROR', 'ERROR (also FATAL, CRITICAL)'], ['WARN', 'WARN'], ['INFO', 'INFO (and others)']]
              .map(([v, label]) => `<option value="${v}"${(doc ? doc.level || '' : '') === v ? ' selected' : ''}>${label}</option>`).join('')}
          </select>
          <p class="hint">Automatic matching only links templates of this level; the LLM is shown it too.</p></div>
        <div class="form-field"><label for="doc-f-verdict">Kind</label>
          <select id="doc-f-verdict">
            <option value="action"${doc && doc.verdict === 'safe' ? '' : ' selected'}>ERROR: something to fix</option>
            <option value="safe"${doc && doc.verdict === 'safe' ? ' selected' : ''}>SAFE: normal operation, no action needed</option>
          </select></div>
        <div class="form-field"><label for="doc-f-error">ERROR: the log line(s) this document is for, one per line</label>
          <textarea id="doc-f-error" class="mono-input" maxlength="5000" placeholder="TransactionManager Transaction timeout reqID 123 msisdn 84912345678">${escapeHtml(doc ? doc.error : '')}</textarea>
          <p class="hint">Only these lines are matched against templates (numbers and ids become &lt;*&gt;). Empty: never matched automatically.</p></div>
        <div class="form-field"><label for="doc-f-handling">Handling: what to do</label>
          <textarea id="doc-f-handling" class="tall" maxlength="20000">${escapeHtml(doc ? doc.handling : '')}</textarea></div>
        <div class="form-error" id="doc-f-error-msg" aria-live="polite"></div>
      </div>
      <footer class="pane-actions">
        <button type="submit" class="btn btn-primary">${approving ? 'Save and approve' : 'Save'}</button>
        <button type="button" class="btn" data-act="cancel">Cancel</button>
      </footer>
    </form>`;
  document.getElementById('doc-f-title').focus();
}

async function saveDocument(event) {
  event.preventDefault();
  const doc = selectedDoc();
  const approving = isDraftMode() && doc && docs.drafts.has(doc.doc_id);
  const payload = {
    revision: doc ? doc.revision : null,
    title: document.getElementById('doc-f-title').value,
    error_code: document.getElementById('doc-f-code').value,
    error: document.getElementById('doc-f-error').value,
    handling: document.getElementById('doc-f-handling').value,
    verdict: document.getElementById('doc-f-verdict').value,
    service: document.getElementById('doc-f-service').value,
    level: document.getElementById('doc-f-level').value,
  };
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  try {
    const saved = await apiMutation(doc ? docUrl(doc.doc_id) : '/api/documents', doc ? 'PUT' : 'POST', payload);
    if (approving) await apiMutation(draftLinkUrl(docs.drafts.get(doc.doc_id), 'confirm'), 'POST', {});
    docs.editing = false;
    if (!doc) docs.selected = saved.doc_id;
    if (approving) docs.selected = nextDocId(doc.doc_id);
    showToast(approving ? `${doc.doc_id} saved and approved` : `${saved.doc_id} saved`);
    await loadDocumentation();
  } catch (err) {
    document.getElementById('doc-f-error-msg').textContent = err.status === 409 && err.payload && err.payload.error === 'stale'
      ? 'This document changed in another window. Cancel to load the latest version, then edit again.' : err.message;
  } finally { if (submit) submit.disabled = false; }
}

// The item to show after the current one leaves the list.
function nextDocId(docId) {
  const shown = docs.items.filter(docMatches);
  const i = shown.findIndex(d => d.doc_id === docId);
  const next = shown[i + 1] || shown[i - 1];
  return next ? next.doc_id : null;
}

async function decide(docIds, decision) {
  let done = 0;
  try {
    for (const docId of docIds) {
      const t = docs.drafts.get(docId);
      if (!t) continue;
      await apiMutation(draftLinkUrl(t, decision), 'POST', {});
      done += 1;
    }
    if (docIds.includes(docs.selected)) docs.selected = nextDocId(docIds[docIds.length - 1]);
    showToast(docIds.length === 1
      ? `${docIds[0]} ${decision === 'confirm' ? 'approved: the template is linked to it' : 'rejected: the template is not sent to the LLM again'}`
      : `${plural(done, 'suggestion')} approved`);
  } catch (err) {
    showToast(`Could not update the suggestion: ${err.message}`, true);
  }
  await loadDocumentation();
}

// One click from the reading pane: the document stays as written, only its kind changes.
async function setKind(doc, verdict) {
  try {
    await apiMutation(docUrl(doc.doc_id), 'PUT', {revision: doc.revision, title: doc.title, error_code: doc.error_code,
                                                 error: doc.error, handling: doc.handling, verdict});   // service kept
    showToast(`${doc.doc_id} is now ${verdict === 'safe' ? 'SAFE' : 'ERROR'}`);
  } catch (err) {
    showToast(err.status === 409 ? `${doc.doc_id} changed in another window; reloaded` : `Could not change ${doc.doc_id}: ${err.message}`, true);
  }
  await loadDocumentation();
}

async function deleteDocument(doc) {
  if (doc.linked_templates > 0) {
    notifyDialog(`${doc.doc_id} is linked to ${plural(doc.linked_templates, 'template')}. Unlink them on the Templates page first.`, 'Document in use');
    return;
  }
  if (!(await confirmDialog(`${doc.doc_id} will be removed from the documentation corpus.`, {title: 'Delete this document?', confirmLabel: 'Delete document'}))) return;
  try {
    await apiMutation(docUrl(doc.doc_id), 'DELETE', {});
    docs.selected = nextDocId(doc.doc_id);
    showToast(`${doc.doc_id} deleted`);
  } catch (err) {
    if (err.status === 409 && err.payload && err.payload.error === 'linked') {
      notifyDialog(`${doc.doc_id} is linked to ${plural(err.payload.templates, 'template')}. Unlink them on the Templates page first.`, 'Document in use');
    } else {
      notifyDialog(err.message, 'Unable to delete the document');
    }
  }
  await loadDocumentation();
}

function selectDoc(docId, focus = false) {
  docs.selected = docId;
  docs.editing = false;
  renderDocList();
  renderDocPane();
  const item = [...$docList.querySelectorAll('.split-item')].find(b => b.dataset.docId === docId);
  if (item && focus) item.focus();
  if (window.matchMedia('(max-width: 900px)').matches) $docPane.scrollIntoView({behavior: 'smooth', block: 'start'});
}

$docList.addEventListener('click', event => {
  const item = event.target.closest('.split-item');
  if (item) selectDoc(item.dataset.docId);
});

$docList.addEventListener('keydown', event => {
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
  const items = [...$docList.querySelectorAll('.split-item')];
  const i = items.findIndex(b => b.dataset.docId === docs.selected);
  const next = items[Math.min(items.length - 1, Math.max(0, i + (event.key === 'ArrowDown' ? 1 : -1)))];
  if (next) { event.preventDefault(); selectDoc(next.dataset.docId, true); }
});

$docPane.addEventListener('click', async event => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const doc = selectedDoc();
  const act = button.dataset.act;
  if (act === 'version') { selectDoc(button.dataset.docId); return; }
  if (act === 'edit') { docs.editing = true; renderDocPane(); return; }
  if (act === 'cancel') { docs.editing = false; if (!doc) docs.selected = docs.items.length ? docs.items[0].doc_id : null; renderDocPane(); return; }
  if (!doc) return;
  if (act === 'kind') { button.disabled = true; await setKind(doc, doc.verdict === 'safe' ? 'action' : 'safe'); return; }
  if (act === 'approve') { button.disabled = true; await decide([doc.doc_id], 'confirm'); }
  else if (act === 'reject') {
    if (await confirmDialog(`${doc.doc_id} will be rejected. Its template stays undocumented and is not sent to the LLM again (you can still link it by hand).`,
      {title: 'Reject this suggestion?', confirmLabel: 'Reject'})) await decide([doc.doc_id], 'reject');
  } else if (act === 'delete') deleteDocument(doc);
});

$docPane.addEventListener('submit', event => { if (event.target.id === 'doc-form') saveDocument(event); });

let docSearchTimer;
$docSearch.addEventListener('input', () => {
  clearTimeout(docSearchTimer);
  docSearchTimer = setTimeout(() => { docs.query = $docSearch.value.trim(); renderDocList(); }, 200);
});

document.getElementById('add-document-btn').addEventListener('click', () => {
  docs.selected = null;
  docs.editing = true;
  renderDocList();
  renderDocPane();
});

document.getElementById('approve-safe-btn').addEventListener('click', async () => {
  const safe = docs.items.filter(d => d.verdict === 'safe' && docs.drafts.has(d.doc_id)).map(d => d.doc_id);
  if (!(await confirmDialog(`${plural(safe.length, 'SAFE suggestion')} will become active documents linked to their templates: ${safe.join(', ')}.`,
    {title: 'Approve all SAFE suggestions?', confirmLabel: 'Approve', danger: false}))) return;
  decide(safe, 'confirm');
});

$docService.addEventListener('change', () => {
  docs.service = $docService.value;
  const shown = docs.items.filter(docMatches);
  if (!shown.some(d => d.doc_id === docs.selected)) { docs.selected = shown.length ? shown[0].doc_id : null; docs.editing = false; }
  renderDocumentation();
});

document.querySelectorAll('#doc-kind-toggle .toggle-btn').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('#doc-kind-toggle .toggle-btn').forEach(b => {
    b.classList.toggle('active', b === button);
    b.setAttribute('aria-pressed', String(b === button));
  });
  docs.kind = button.dataset.docKind;
  const shown = docs.items.filter(docMatches);
  if (!shown.some(d => d.doc_id === docs.selected)) { docs.selected = shown.length ? shown[0].doc_id : null; docs.editing = false; }
  renderDocList();
  renderDocPane();
}));

document.getElementById('doc-level-toggle').addEventListener('click', event => {
  const button = event.target.closest('[data-doc-level]');
  if (!button) return;
  docs.level = button.dataset.docLevel;
  renderLevelToggle();
  const shown = docs.items.filter(docMatches);
  if (!shown.some(d => d.doc_id === docs.selected)) { docs.selected = shown.length ? shown[0].doc_id : null; docs.editing = false; }
  renderDocList();
  renderDocPane();
});

document.querySelectorAll('#doc-mode-toggle .toggle-btn').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('#doc-mode-toggle .toggle-btn').forEach(b => {
    b.classList.toggle('active', b === button);
    b.setAttribute('aria-selected', String(b === button));
  });
  $docList.setAttribute('aria-label', button.dataset.docMode === 'draft' ? 'AI suggestions' : 'Documents');
  setView(button.dataset.docMode === 'draft' ? 'documentation/suggestions' : 'documentation');
}));
