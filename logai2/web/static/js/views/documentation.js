// ── Documentation (adapted from logai-engine views/documentation.js) ──
// The runbook corpus: list, editor with a revision check, delete (blocked
// while templates link to it) and, per document, the templates linked to it.
// A document is ERROR (the log lines it is for: the only part matched against
// templates) plus Handling (what to do).
// "AI suggestions" lists the LLM's draft documents with the template each was written for:
// Approve (the link becomes manual, the document active), Edit (saving approves), Reject.
const docs = {items: [], sort: 'doc_id', order: 'asc', expanded: new Set(), mode: 'active', drafts: new Map()};
const $docTbody = document.getElementById('documentation-tbody');
const $documentModal = document.getElementById('document-modal');

function docCompare(a, b) {
  return typeof a === 'number' && typeof b === 'number'
    ? a - b
    : String(a).localeCompare(String(b), undefined, {numeric: true, sensitivity: 'base'});
}

async function loadDocumentation() {
  const [items, drafts, draftLinks] = await Promise.all([
    getJson(`/api/documents?status=${docs.mode}`, 'Unable to load documentation'),
    docs.mode === 'draft' ? null : getJson('/api/documents?status=draft', 'Unable to load AI suggestions'),
    docs.mode === 'draft' ? getJson('/api/templates?link_source=draft&size=500', 'Unable to load AI suggestions') : null,
  ]);
  docs.items = items;
  if (draftLinks) docs.drafts = new Map(draftLinks.items.map(t => [t.doc_id, t]));
  const nDrafts = drafts ? drafts.length : items.length;
  document.getElementById('doc-draft-count').textContent = nDrafts ? `(${nDrafts})` : '';
  renderDocumentation();
}

function draftCell(doc) {
  const t = docs.drafts.get(doc.doc_id);
  const conf = doc.confidence == null ? '' : ` · confidence ${doc.confidence.toFixed(2)}`;
  return t
    ? `<div><span class="tmpl-id">${escapeHtml(t.service)} · ${escapeHtml(t.template_id)}</span>${escapeHtml(conf)}</div>
       <div class="tmpl-text" title="${escapeHtml(t.text)}">${highlightWildcards(escapeHtml(t.text))}</div>`
    : '<span class="sync-status">no template (linked otherwise since)</span>';
}

function docActions(doc) {
  if (docs.mode !== 'draft') {
    return '<button type="button" class="btn edit-document">Edit</button><button type="button" class="btn btn-danger delete-document">Delete</button>';
  }
  const linked = docs.drafts.has(doc.doc_id);
  return (linked ? '<button type="button" class="btn btn-primary approve-draft">Approve</button>' : '')
    + '<button type="button" class="btn edit-document">Edit</button>'
    + (linked ? '<button type="button" class="btn btn-danger reject-draft">Reject</button>' : '');
}

function renderDocumentation() {
  const drafting = docs.mode === 'draft';
  document.getElementById('doc-links-th').firstChild.textContent = drafting ? 'Written for ' : 'Linked templates ';
  document.getElementById('approve-safe-btn').hidden = !drafting || !docs.items.some(d => d.verdict === 'safe' && docs.drafts.has(d.doc_id));
  document.getElementById('add-document-btn').hidden = drafting;
  const linked = docs.items.filter(d => d.linked_templates > 0).length;
  document.getElementById('doc-sync-status').textContent = drafting
    ? `${docs.items.length} AI suggestion${docs.items.length === 1 ? '' : 's'}: unverified, check commands before running them`
    : `${docs.items.length} document${docs.items.length === 1 ? '' : 's'} · ${linked} linked to templates`;
  if (!docs.items.length) {
    $docTbody.innerHTML = `<tr><td colspan="6" class="empty-state">${drafting ? 'No AI suggestions waiting.' : 'No documentation entries.'}</td></tr>`;
    return;
  }
  const items = [...docs.items].sort((l, r) => {
    const c = docCompare(l[docs.sort] ?? '', r[docs.sort] ?? '');
    return docs.order === 'asc' ? c : -c;
  });
  $docTbody.innerHTML = items.map(doc => {
    const open = docs.expanded.has(doc.doc_id);
    const id = escapeHtml(doc.doc_id);
    return `
    <tr data-doc-id="${id}">
      <td><span class="tmpl-id">${id}</span></td>
      <td>${escapeHtml(doc.title || 'Untitled')}</td>
      <td>${docErrorCell(doc)}</td>
      <td><span class="tmpl-id">${escapeHtml(doc.error_code || '—')}</span></td>
      <td>${drafting ? draftCell(doc) : doc.linked_templates
        ? `<button type="button" class="group-expand doc-links-toggle${open ? ' expanded' : ''}" aria-expanded="${open}"><span class="group-expand-icon" aria-hidden="true">›</span>${doc.linked_templates}</button>`
        : '0'}</td>
      <td><div class="btn-row">${docActions(doc)}</div></td>
    </tr>
    <tr class="group-members-row" data-links-for="${id}"${open ? '' : ' hidden'}><td colspan="6"><div class="group-members-panel"><p class="loading">Loading linked templates...</p></div></td></tr>`;
  }).join('');
  docs.expanded.forEach(docId => { if (docs.items.some(d => d.doc_id === docId)) loadDocLinks(docId); });
}

function docErrorCell(doc) {
  const lines = doc.error_lines.length
    ? `<div class="doc-error-lines" title="As matched against templates">${highlightWildcards(escapeHtml(doc.error_lines.join('\n')))}</div>`
    : '<span class="badge badge-gray" title="Never matched automatically: manual and inherited links only">no ERROR line</span>';
  const pending = doc.error_lines.length && !doc.embedded ? ' <span class="link-meta">embedding…</span>' : '';
  const badges = (doc.source === 'llm' ? ' <span class="badge badge-yellow">AI-written</span>' : '')
    + (doc.verdict === 'safe' ? ' <span class="badge badge-green">SAFE</span>' : '');
  return `${lines}${pending}${badges}<div class="doc-text" title="${escapeHtml(doc.handling)}">${escapeHtml(doc.handling)}</div>`;
}

async function loadDocLinks(docId) {
  const row = [...$docTbody.querySelectorAll('tr[data-links-for]')].find(r => r.dataset.linksFor === docId);
  if (!row) return;
  const panel = row.querySelector('.group-members-panel');
  try {
    const data = await getJson(`/api/templates?doc_id=${encodeURIComponent(docId)}&size=500`, 'Unable to load linked templates');
    panel.innerHTML = `<div class="group-member-list doc-links">
      <div class="group-member group-member-header"><span>Service</span><span>Template ID</span><span>Template text</span><span>Level</span></div>
      ${data.items.map(t => `<div class="group-member">
        <span>${escapeHtml(t.service)}</span>
        <span class="tmpl-id">${escapeHtml(t.template_id)}</span>
        <span class="group-member-text">${highlightWildcards(escapeHtml(t.text))}</span>
        <span><span class="badge ${levelBadgeClass(t.level)}">${escapeHtml(t.level)}</span></span>
      </div>`).join('')}
    </div>${data.total > data.items.length ? `<p class="sync-status">Showing ${data.items.length} of ${data.total}.</p>` : ''}`;
  } catch (err) {
    panel.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
  }
}

function openDocumentEditor(docId = null) {
  const doc = docs.items.find(item => item.doc_id === docId);
  document.getElementById('document-modal-title').textContent = !doc ? 'Add document'
    : docs.mode === 'draft' ? `Edit AI suggestion ${doc.doc_id} (saving approves it)` : `Edit ${doc.doc_id}`;
  document.getElementById('document-id').value = doc ? doc.doc_id : '';
  document.getElementById('document-title').value = doc ? doc.title : '';
  document.getElementById('document-error-lines').value = doc ? doc.error : '';
  document.getElementById('document-text').value = doc ? doc.handling : '';
  document.getElementById('document-error-code').value = doc ? doc.error_code : '';
  document.getElementById('document-error').textContent = '';
  docs.editingRevision = doc ? doc.revision : null;
  $documentModal.classList.add('open');
}

function notifyLinked(docId, n) {
  notifyDialog(`${docId} is linked to ${n} template${n === 1 ? '' : 's'}. Unlink them on the Templates page first.`, 'Document in use');
}

async function deleteDocument(docId) {
  const doc = docs.items.find(item => item.doc_id === docId);
  if (doc && doc.linked_templates > 0) { notifyLinked(docId, doc.linked_templates); return; }
  if (!(await confirmDialog(`${docId} will be removed from the documentation corpus.`, {title: 'Delete this document?', confirmLabel: 'Delete document'}))) return;
  try {
    await apiMutation(`/api/documents/${encodeURIComponent(docId)}`, 'DELETE', {});
  } catch (err) {
    if (err.status === 409 && err.payload && err.payload.error === 'linked') {
      notifyLinked(docId, err.payload.templates);   // linked since the list loaded
    } else {
      notifyDialog(err.message, 'Unable to delete the document');
    }
  }
  docs.expanded.delete(docId);
  await loadData();
}

async function decideDraft(docId, decision) {
  const t = docs.drafts.get(docId);
  if (!t) return;
  await apiMutation(`/api/templates/${encodeURIComponent(t.service)}/${encodeURIComponent(t.template_id)}/link/${decision}`, 'POST', {});
}

async function decideAndReload(docIds, decision) {
  try {
    for (const docId of docIds) await decideDraft(docId, decision);
  } catch (err) {
    notifyDialog(err.message, 'Unable to update the suggestion');
  }
  await loadData();
}

$docTbody.addEventListener('click', event => {
  const row = event.target.closest('tr[data-doc-id]');
  if (!row) return;
  const docId = row.dataset.docId;
  if (event.target.closest('.approve-draft')) decideAndReload([docId], 'confirm');
  else if (event.target.closest('.reject-draft')) decideAndReload([docId], 'reject');
  else if (event.target.closest('.edit-document')) openDocumentEditor(docId);
  else if (event.target.closest('.delete-document')) deleteDocument(docId);
  else if (event.target.closest('.doc-links-toggle')) {
    const button = event.target.closest('.doc-links-toggle');
    const open = !docs.expanded.has(docId);
    if (open) docs.expanded.add(docId); else docs.expanded.delete(docId);
    button.classList.toggle('expanded', open);
    button.setAttribute('aria-expanded', String(open));
    row.nextElementSibling.hidden = !open;
    if (open) loadDocLinks(docId);
  }
});

document.querySelectorAll('#view-documentation th[data-document-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const field = th.dataset.documentSort;
    if (docs.sort === field) docs.order = docs.order === 'desc' ? 'asc' : 'desc';
    else { docs.sort = field; docs.order = field === 'linked_templates' ? 'desc' : 'asc'; }
    document.querySelectorAll('#view-documentation th[data-document-sort]').forEach(h => {
      const active = h.dataset.documentSort === docs.sort;
      h.classList.toggle('sorted', active);
      h.querySelector('.sort-arrow').textContent = active ? (docs.order === 'desc' ? '▼' : '▲') : '';
    });
    renderDocumentation();
  });
});

document.getElementById('add-document-btn').addEventListener('click', () => openDocumentEditor());
document.getElementById('approve-safe-btn').addEventListener('click', async () => {
  const safe = docs.items.filter(d => d.verdict === 'safe' && docs.drafts.has(d.doc_id)).map(d => d.doc_id);
  if (!(await confirmDialog(`${safe.length} SAFE suggestion${safe.length === 1 ? '' : 's'} will become active documents linked to their templates.`,
    {title: 'Approve all SAFE suggestions?', confirmLabel: 'Approve', danger: false}))) return;
  decideAndReload(safe, 'confirm');
});
document.querySelectorAll('#doc-mode-toggle .toggle-btn').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('#doc-mode-toggle .toggle-btn').forEach(b => b.classList.toggle('active', b === button));
  docs.mode = button.dataset.docMode;
  docs.expanded.clear();
  loadData();
}));
document.getElementById('document-form').addEventListener('submit', async event => {
  event.preventDefault();
  const docId = document.getElementById('document-id').value;
  const payload = {
    revision: docs.editingRevision,
    title: document.getElementById('document-title').value,
    error: document.getElementById('document-error-lines').value,
    handling: document.getElementById('document-text').value,
    error_code: document.getElementById('document-error-code').value,
  };
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  try {
    await apiMutation(docId ? `/api/documents/${encodeURIComponent(docId)}` : '/api/documents', docId ? 'PUT' : 'POST', payload);
    if (docId && docs.mode === 'draft') await decideDraft(docId, 'confirm');   // saving a suggestion approves it
    $documentModal.classList.remove('open');
    await loadData();
  } catch (err) {
    const message = err.status === 409 && err.payload && err.payload.error === 'stale'
      ? 'This document changed in another window. Close the editor to load the latest version, then edit again.'
      : err.message;
    document.getElementById('document-error').textContent = message;
    if (err.status === 409) loadData();
  } finally { if (submit) submit.disabled = false; }
});
