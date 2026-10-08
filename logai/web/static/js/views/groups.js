function renderGroups(items) {
  const total = items.length;
  const documented = items.filter(group => group.documented).length;
  document.getElementById('group-stat-total').textContent = total;
  document.getElementById('group-stat-documented').textContent = documented;
  document.getElementById('group-stat-undocumented').textContent = total - documented;
  const availableGroupIds = new Set(items.map(group => group.group_id));
  state.expandedGroups.forEach(groupId => {
    if (!availableGroupIds.has(groupId)) state.expandedGroups.delete(groupId);
  });
  document.getElementById('groups-locked-banner').hidden = !state.documentationMutationsBlocked;
  if (state.undocumentedGroupsOnly) items = items.filter(group => !group.documented);
  if (state.singleGroupsOnly) items = items.filter(group => group.singleton);
  if (!items.length) {
    $groupsTbody.innerHTML = '<tr><td colspan="7" class="empty-state">No groups found.</td></tr>';
    return;
  }
  const documentsById = new Map(state.documents.map(doc => [doc.id, doc]));
  const documentationLocked = state.documentationMutationsBlocked;
  items = [...items].sort((left, right) => {
    const value = group => {
      if (state.groupSort === 'error_code') {
        const document = documentsById.get(group.documentation_id);
        return group.error_code || (document && document.error_code) || '';
      }
      return group[state.groupSort] ?? '';
    };
    const a = value(left);
    const b = value(right);
    const comparison = compareValues(a, b);
    return state.groupOrder === 'asc' ? comparison : -comparison;
  });
  $groupsTbody.innerHTML = items.map((g, index) => {
    const document = documentsById.get(g.documentation_id);
    const errorCode = g.error_code || (document && document.error_code) || '—';
    const templates = Array.isArray(g.templates) ? g.templates : [];
    const expanded = state.expandedGroups.has(g.group_id);
    const memberRowId = `group-members-${index}`;
    const templateLabel = `${templates.length} template${templates.length === 1 ? '' : 's'}`;
    const memberRows = templates.length ? templates.map(template => `
      <div class="group-member">
        <span class="tmpl-id">${escapeHtml(template.template_id || '—')}</span>
        <span class="group-member-text">${highlightWildcards(escapeHtml(template.template_text || '—'))}</span>
        <span>${escapeHtml(template.service || 'unknown')}</span>
        <span><span class="badge ${levelBadgeClass(template.level)}">${escapeHtml(template.level || 'INFO')}</span></span>
        <span class="group-member-events">${formatCount(template.event_count || 0)}</span>
      </div>
    `).join('') : '<div class="group-member"><span class="sync-status">No templates recorded.</span></div>';
    return `
      <tr data-group-id="${escapeHtml(g.group_id)}">
        <td><div class="group-cell"><span class="group-id">${escapeHtml(g.group_id || '—')}</span>${g.singleton ? ' <span class="badge badge-unknown" title="Only one template: HDBSCAN found no similar templates for it">single</span>' : ''}<button type="button" class="group-expand${expanded ? ' expanded' : ''}" aria-expanded="${expanded}" aria-controls="${memberRowId}" title="${expanded ? 'Hide' : 'Show'} group templates"><span class="group-expand-icon" aria-hidden="true">&#9656;</span><span>${templateLabel}</span></button></div></td>
        <td>${escapeHtml(g.service || 'unknown')}</td>
        <td><div class="tmpl-text" title="${escapeHtml(g.representative_template || '')}">${highlightWildcards(escapeHtml(g.representative_template || '—'))}</div></td>
        <td>${g.documentation_id ? `<span class="tmpl-id">${escapeHtml(errorCode)}</span>` : '<span class="badge badge-unknown">Undocumented</span>'}</td>
        <td><span class="badge ${g.documentation_source === 'manual' ? 'badge-info' : (g.documentation_source === 'stale_override' ? 'badge-warn' : (g.documented ? 'badge-known' : 'badge-unknown'))}"${g.membership_changed_since_assignment ? ' title="Membership changed after this manual assignment"' : ''}>${escapeHtml(g.documentation_source || (g.documented ? 'automatic' : 'none'))}</span></td>
        <td>${formatCount(g.event_count || 0)}</td>
        <td><div class="btn-row"><button class="btn assign-doc"${documentationLocked ? ' disabled title="Waiting for template grouping"' : ''}>${g.documented ? 'Change' : 'Assign'}</button>${(g.documented || g.documentation_id || g.manual_documentation_id) ? `<button class="btn clear-doc"${documentationLocked ? ' disabled title="Waiting for template grouping"' : ''}>Clear</button>` : ''}</div></td>
      </tr>
      <tr class="group-members-row" id="${memberRowId}" data-members-for="${escapeHtml(g.group_id)}"${expanded ? '' : ' hidden'}>
        <td colspan="7"><div class="group-members-panel"><div class="group-member-list"><div class="group-member group-member-header"><span>Template ID</span><span>Template</span><span>Service</span><span>Level</span><span class="group-member-events">Events</span></div>${memberRows}</div></div></td>
      </tr>`;
  }).join('');
  $groupsTbody.querySelectorAll('.group-expand').forEach(button => button.addEventListener('click', () => {
    const groupId = button.closest('tr').dataset.groupId;
    const isExpanded = button.getAttribute('aria-expanded') === 'true';
    if (isExpanded) state.expandedGroups.delete(groupId);
    else state.expandedGroups.add(groupId);
    button.setAttribute('aria-expanded', String(!isExpanded));
    button.classList.toggle('expanded', !isExpanded);
    button.title = `${isExpanded ? 'Show' : 'Hide'} group templates`;
    button.closest('tr').nextElementSibling.hidden = isExpanded;
  }));
  $groupsTbody.querySelectorAll('.assign-doc').forEach(btn => btn.addEventListener('click', () => openAssignment(btn.closest('tr').dataset.groupId)));
  $groupsTbody.querySelectorAll('.clear-doc').forEach(btn => btn.addEventListener('click', () => {
    const groupId = btn.closest('tr').dataset.groupId;
    const group = state.groups.find(item => item.group_id === groupId);
    if (group) openClearConfirmation(group);
  }));
}


async function pollDocumentationResult(overrideRevision, groupId, operation = 'documentation') {
  clearTimeout(state.documentationPollTimer);
  try {
    const data = await fetchDocumentation();
    const sync = data.synchronization || {};
    if (sync.applied_override_revision === overrideRevision && sync.state === 'applied') {
      showGroupingOperation('applied', `${groupId}: ${operation} applied`);
      await loadData();
      return;
    }
    if (sync.attempted_override_revision === overrideRevision && sync.state === 'failed') {
      showGroupingOperation('failed', `${groupId}: ${sync.error || `${operation} failed`}`);
      return;
    }
    showGroupingOperation('pending', `${groupId}: ${operation} pending`);
  } catch (error) {
    showGroupingOperation('failed', `${groupId}: documentation status unavailable`);
  }
  state.documentationPollTimer = setTimeout(
    () => pollDocumentationResult(overrideRevision, groupId, operation), 1000
  );
}


function openAssignment(groupId) {
  if (state.documentationMutationsBlocked) return;
  state.assignmentGroup = groupId;
  document.getElementById('assignment-group').textContent = groupId;
  const select = document.getElementById('assignment-document');
  select.innerHTML = state.documents.map(doc => `<option value="${escapeHtml(doc.id)}">${escapeHtml(doc.title || doc.id)} (${escapeHtml(doc.id)})</option>`).join('');
  document.getElementById('assignment-error').textContent = state.documents.length ? '' : 'Create a document first.';
  document.getElementById('assignment-save').disabled = !state.documents.length;
  $assignmentModal.classList.add('open');
}

function openClearConfirmation(group) {
  if (state.documentationMutationsBlocked) return;
  const documentId = group.manual_documentation_id || group.documentation_id || 'the current document';
  state.pendingDocumentationClear = {
    groupId: group.group_id,
    overrideRevision: state.overrideRevision,
  };
  document.getElementById('clear-confirm-message').textContent =
    `${group.group_id} is documented by ${documentId} (${group.documentation_source || 'automatic'}). Clear it?`;
  document.getElementById('clear-confirm-error').textContent = '';
  document.getElementById('clear-document-confirm').disabled = false;
  $clearConfirmModal.classList.add('open');
}

async function confirmClearDocumentation() {
  const pending = state.pendingDocumentationClear;
  if (!pending) return;
  const button = document.getElementById('clear-document-confirm');
  const error = document.getElementById('clear-confirm-error');
  button.disabled = true;
  error.textContent = '';
  try {
    const result = await apiMutation(
      `/api/groups/${encodeURIComponent(pending.groupId)}/documentation`,
      'DELETE',
      {override_revision: pending.overrideRevision, force: true},
    );
    state.overrideRevision = result.override_revision;
    state.pendingDocumentationClear = null;
    $clearConfirmModal.classList.remove('open');
    showGroupingOperation('pending', `${pending.groupId}: clear pending`);
    pollDocumentationResult(result.override_revision, pending.groupId, 'clear');
  } catch (err) {
    if (err.status === 409 && err.payload && err.payload.error === 'revision_conflict') {
      error.textContent = 'Documentation changed. Close this dialog and reload the latest groups.';
    } else if (err.status === 409 && err.payload && err.payload.error === 'grouping_pending') {
      error.textContent = err.message;
    } else {
      error.textContent = err.message;
    }
    button.disabled = false;
  }
}


document.querySelectorAll('#view-groups th[data-group-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const field = th.dataset.groupSort;
    if (state.groupSort === field) {
      state.groupOrder = state.groupOrder === 'desc' ? 'asc' : 'desc';
    } else {
      state.groupSort = field;
      state.groupOrder = 'asc';
    }
    updateGroupSortUI();
    renderGroups(state.groups);
  });
});

function updateGroupSortUI() {
  document.querySelectorAll('#view-groups th[data-group-sort]').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    const active = th.dataset.groupSort === state.groupSort;
    th.classList.toggle('sorted', active);
    arrow.textContent = active ? (state.groupOrder === 'desc' ? '▼' : '▲') : '';
  });
}


document.getElementById('clear-document-confirm').addEventListener('click', confirmClearDocumentation);


document.getElementById('assignment-save').addEventListener('click', async () => {
  try {
    const result = await apiMutation(`/api/groups/${encodeURIComponent(state.assignmentGroup)}/documentation`, 'PUT', {
      documentation_id: document.getElementById('assignment-document').value,
      override_revision: state.overrideRevision,
    });
    state.overrideRevision = result.override_revision;
    $assignmentModal.classList.remove('open');
    showGroupingOperation('pending', `${state.assignmentGroup}: documentation pending`);
    pollDocumentationResult(result.override_revision, state.assignmentGroup);
  } catch (err) { document.getElementById('assignment-error').textContent = err.message; }
});


document.getElementById('assignment-edit').addEventListener('click', () => {
  const docId = document.getElementById('assignment-document').value;
  if (!docId) return;
  $assignmentModal.classList.remove('open');
  openDocumentEditor(docId);
});
document.getElementById('undocumented-groups-only').addEventListener('change', event => {
  state.undocumentedGroupsOnly = event.target.checked;
  loadData();
});
document.getElementById('single-groups-only').addEventListener('change', event => {
  state.singleGroupsOnly = event.target.checked;
  loadData();
});
