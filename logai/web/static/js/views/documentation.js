function renderDocumentation(data) {
  state.documents = data.items;
  state.documentRevision = data.revision;
  state.documentSynchronization = data.synchronization || {};
  const sync = data.synchronization || {};
  const syncLabel = {applied: 'Engine up to date', pending: 'Engine applying changes…', failed: 'Engine failed to apply', engine_unavailable: 'Engine unavailable'}[sync.state] || `Engine: ${sync.state || 'unknown'}`;
  const $sync = document.getElementById('doc-sync-status');
  $sync.className = `sync-status sync-pill sync-${sync.state || 'unknown'}`;
  $sync.textContent = `${data.total} entries · ${syncLabel}${sync.error ? ` · ${sync.error}` : ''}`;
  if (!data.items.length) {
    $documentationTbody.innerHTML = '<tr><td colspan="6" class="empty-state">No documentation entries.</td></tr>';
    return;
  }
  const items = [...data.items].sort((left, right) => {
    const a = left[state.documentSort] ?? '';
    const b = right[state.documentSort] ?? '';
    const comparison = compareValues(a, b);
    return state.documentOrder === 'asc' ? comparison : -comparison;
  });
  $documentationTbody.innerHTML = items.map(doc => `
    <tr data-doc-id="${escapeHtml(doc.id)}">
      <td><span class="tmpl-id">${escapeHtml(doc.id)}</span></td>
      <td>${escapeHtml(doc.title || 'Untitled')}</td>
      <td><div class="doc-text" title="${escapeHtml(doc.text)}">${escapeHtml(doc.text)}</div></td>
      <td><span class="tmpl-id">${escapeHtml(doc.error_code || '—')}</span></td>
      <td>${doc.group_count || 0}</td>
      <td><div class="btn-row"><button class="btn edit-document">Edit</button><button class="btn btn-danger delete-document">Delete</button></div></td>
    </tr>`).join('');
  $documentationTbody.querySelectorAll('.edit-document').forEach(btn => btn.addEventListener('click', () => openDocumentEditor(btn.closest('tr').dataset.docId)));
  $documentationTbody.querySelectorAll('.delete-document').forEach(btn => btn.addEventListener('click', () => deleteDocument(btn.closest('tr').dataset.docId)));
}


function openDocumentEditor(docId = null) {
  const doc = state.documents.find(item => item.id === docId);
  document.getElementById('document-modal-title').textContent = doc ? 'Edit document' : 'Add document';
  document.getElementById('document-id').value = doc ? doc.id : '';
  document.getElementById('document-title').value = doc ? doc.title : '';
  document.getElementById('document-text').value = doc ? doc.text : '';
  document.getElementById('document-error-code').value = doc ? doc.error_code : '';
  document.getElementById('document-error').textContent = '';
  $documentModal.classList.add('open');
}


function openDeleteConfirmation(docId, payload) {
  const groups = Array.isArray(payload.groups) && payload.groups.length
    ? payload.groups
    : (payload.group_ids || []).map(groupId => ({group_id: groupId}));
  state.pendingDocumentDeletion = {
    docId,
    revision: state.documentRevision,
  };
  document.getElementById('delete-confirm-message').textContent =
    `${docId} is assigned to ${groups.length} active group${groups.length === 1 ? '' : 's'}. Delete it anyway?`;
  document.getElementById('delete-confirm-groups').innerHTML = groups.map(group => `
    <li>
      <span class="delete-group-id">${escapeHtml(group.group_id || 'Unknown group')}</span>
      ${group.service || group.representative_template ? `<span class="delete-group-meta">${escapeHtml([group.service, group.representative_template].filter(Boolean).join(' · '))}</span>` : ''}
    </li>
  `).join('');
  document.getElementById('delete-confirm-error').textContent = '';
  document.getElementById('delete-document-confirm').disabled = false;
  $deleteConfirmModal.classList.add('open');
}

async function pollDocumentationDeletion(corpusRevision, overrideRevision, docId) {
  clearTimeout(state.documentationDeletionPollTimer);
  try {
    const data = await fetchDocumentation();
    const sync = data.synchronization || {};
    const applied = sync.applied_corpus_revision === corpusRevision
      && sync.applied_override_revision === overrideRevision
      && sync.state === 'applied';
    if (applied) {
      showGroupingOperation('applied', `${docId}: deletion applied`);
      await loadData();
      return;
    }
    if (sync.attempted_corpus_revision === corpusRevision && sync.state === 'failed') {
      showGroupingOperation('failed', `${docId}: ${sync.error || 'deletion refresh failed'}`);
      return;
    }
    showGroupingOperation('pending', `${docId}: deletion pending`);
  } catch (error) {
    showGroupingOperation('failed', `${docId}: documentation status unavailable`);
  }
  state.documentationDeletionPollTimer = setTimeout(
    () => pollDocumentationDeletion(corpusRevision, overrideRevision, docId), 1000
  );
}

async function deleteDocument(docId) {
  const documentEntry = state.documents.find(item => item.id === docId);
  const knownAssigned = Number(documentEntry && documentEntry.group_count || 0) > 0
    || Number(documentEntry && documentEntry.manual_group_count || 0) > 0;
  if (!knownAssigned && !(await confirmDialog(`${docId} will be removed from the documentation corpus.`, {title: 'Delete this document?', confirmLabel: 'Delete document'}))) return;
  try {
    await apiMutation(`/api/documentation/${encodeURIComponent(docId)}`, 'DELETE', {revision:state.documentRevision});
    await loadData();
  } catch (err) {
    if (err.status === 409 && err.payload && err.payload.error === 'document_in_use') {
      openDeleteConfirmation(docId, err.payload);
      return;
    }
    if (err.status === 409 && err.payload && err.payload.error === 'revision_conflict') {
      notifyDialog('Documentation changed in another window. The latest list is loaded; try again.', 'Documentation changed');
      await loadData();
      return;
    }
    notifyDialog(err.message, 'Unable to delete the document');
  }
}

async function confirmDocumentDeletion() {
  const pending = state.pendingDocumentDeletion;
  if (!pending) return;
  const button = document.getElementById('delete-document-confirm');
  const error = document.getElementById('delete-confirm-error');
  button.disabled = true;
  error.textContent = '';
  try {
    const result = await apiMutation(
      `/api/documentation/${encodeURIComponent(pending.docId)}`,
      'DELETE',
      {revision: pending.revision, force: true},
    );
    state.pendingDocumentDeletion = null;
    $deleteConfirmModal.classList.remove('open');
    showGroupingOperation('pending', `${pending.docId}: deletion pending`);
    await loadData();
    pollDocumentationDeletion(result.revision, result.override_revision, pending.docId);
  } catch (err) {
    if (err.status === 409 && err.payload && err.payload.error === 'document_in_use') {
      error.textContent = 'Assignments changed. Review the affected groups and confirm again.';
      openDeleteConfirmation(pending.docId, err.payload);
    } else if (err.status === 409 && err.payload && err.payload.error === 'revision_conflict') {
      error.textContent = 'Documentation changed. Close this dialog and reload the latest list.';
      button.disabled = false;
    } else {
      error.textContent = err.message;
      button.disabled = false;
    }
  }
}


document.querySelectorAll('#view-documentation th[data-document-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const field = th.dataset.documentSort;
    if (state.documentSort === field) {
      state.documentOrder = state.documentOrder === 'desc' ? 'asc' : 'desc';
    } else {
      state.documentSort = field;
      state.documentOrder = field === 'group_count' ? 'desc' : 'asc';
    }
    updateDocumentSortUI();
    renderDocumentation({
      items: state.documents,
      total: state.documents.length,
      revision: state.documentRevision,
      synchronization: state.documentSynchronization,
    });
  });
});

function updateDocumentSortUI() {
  document.querySelectorAll('#view-documentation th[data-document-sort]').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    const active = th.dataset.documentSort === state.documentSort;
    th.classList.toggle('sorted', active);
    arrow.textContent = active ? (state.documentOrder === 'desc' ? '▼' : '▲') : '';
  });
}


async function prefillDocumentEditor(suggestion, assignGroupIds = []) {
  // The Alerts/Insights tabs never load the corpus, so fetch its revision
  // first or the POST would be rejected as a revision conflict.
  try { state.documentRevision = (await fetchDocumentation()).revision; } catch (_) {}
  openDocumentEditor();
  state.pendingAssign = assignGroupIds.length ? assignGroupIds : null;
  document.getElementById('document-title').value = suggestion.title || '';
  document.getElementById('document-text').value = suggestion.text || '';
  document.getElementById('document-error-code').value = suggestion.error_code || '';
}


document.getElementById('delete-document-confirm').addEventListener('click', confirmDocumentDeletion);


document.getElementById('add-document-btn').addEventListener('click', () => openDocumentEditor());
document.getElementById('document-form').addEventListener('submit', async event => {
  event.preventDefault();
  const docId = document.getElementById('document-id').value;
  const payload = {
    revision: state.documentRevision,
    title: document.getElementById('document-title').value,
    text: document.getElementById('document-text').value,
    error_code: document.getElementById('document-error-code').value,
  };
  if (!docId && state.pendingAssign) payload.assign_group_ids = state.pendingAssign;
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  try {
    const result = await apiMutation(docId ? `/api/documentation/${encodeURIComponent(docId)}` : '/api/documentation', docId ? 'PUT' : 'POST', payload);
    $documentModal.classList.remove('open');
    if (payload.assign_group_ids) {
      const assigned = result.assigned_group_ids || [];
      const skipped = result.assignment_skipped || [];
      insToast(`Saved ${result.item.id}` + (assigned.length ? ` and assigned it to ${assigned.join(', ')}.` : '.')
        + (skipped.length ? ` Not assigned to ${skipped.map(s => `${s.group_id} (${s.reason.replace('_', ' ')})`).join(', ')}.` : ''), skipped.length > 0);
    }
    state.pendingAssign = null;
    await loadData();
  } catch (err) { document.getElementById('document-error').textContent = err.message; }
  finally { if (submit) submit.disabled = false; }
});
