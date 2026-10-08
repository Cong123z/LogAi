function renderGroupAssignmentOptions(query = '') {
  const current = state.groupingTemplate && state.groupingTemplate.group_id;
  const normalized = query.trim().toLowerCase();
  const groups = state.groups.filter(group => group.group_id !== current && (
    !normalized || `${group.group_id} ${group.representative_template || ''}`.toLowerCase().includes(normalized)
  ));
  document.getElementById('group-assignment-target').innerHTML = groups.map(group =>
    `<option value="${escapeHtml(group.group_id)}">${escapeHtml(group.group_id)} — ${escapeHtml(group.representative_template || '')}</option>`
  ).join('');
  document.getElementById('group-assignment-save').disabled = state.groupingMode === 'existing' && !groups.length;
}

async function openGroupAssignment() {
  if (!state.groupingTemplate) return;
  const data = await fetchGroups();
  state.groups = data.items;
  state.groupingRevision = data.grouping_revision;
  state.groupingSynchronization = data.grouping_synchronization || {};
  state.groupingMode = 'existing';
  document.querySelectorAll('#group-assignment-mode .toggle-btn').forEach(button => button.classList.toggle('active', button.dataset.mode === 'existing'));
  document.getElementById('group-assignment-existing').style.display = '';
  document.getElementById('group-assignment-new').style.display = 'none';
  document.getElementById('group-assignment-search').value = '';
  document.getElementById('group-assignment-error').textContent = '';
  renderGroupAssignmentOptions();
  $groupAssignmentModal.classList.add('open');
}

async function pollGroupingResult(revision, templateId) {
  clearTimeout(state.groupingPollTimer);
  try {
    const resp = await fetch('/api/grouping/status');
    const status = await resp.json();
    const result = (status.results || {})[templateId];
    const reason = result && (result.message || result.reason_code);
    if (status.revision !== revision || status.state === 'pending' || status.state === 'engine_unavailable') {
      showGroupingOperation(status.state, `${templateId}: ${status.state}`);
    } else if (status.state === 'applied') {
      showGroupingOperation('applied', `${templateId}: assignment applied`);
      await loadData();
      return;
    } else if (status.state === 'failed' || (result && result.retryable === false)) {
      showGroupingOperation('failed', `${templateId}: ${reason || 'assignment failed'}`);
      return;
    } else {
      showGroupingOperation(status.state, `${templateId}: ${reason || status.state}`);
    }
  } catch (error) {
    showGroupingOperation('engine_unavailable', `${templateId}: engine unavailable`);
  }
  state.groupingPollTimer = setTimeout(() => pollGroupingResult(revision, templateId), 1000);
}


// ── Render ───────────────────────────────────────────────────────
function renderStats(stats) {
  document.getElementById('stat-total').textContent = stats.total;
  document.getElementById('stat-known').textContent = stats.known;
  document.getElementById('stat-unknown').textContent = stats.unknown;
  document.getElementById('stat-services').textContent = stats.services.length;

  // Populate service dropdown (keep current selection)
  const currentService = $service.value;
  $service.innerHTML = '<option value="">All Services</option>';
  stats.services.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = s;
    if (s === currentService) opt.selected = true;
    $service.appendChild(opt);
  });

  // Populate level dropdown
  const currentLevel = $level.value;
  $level.innerHTML = '<option value="">All Levels</option>';
  stats.levels.forEach(l => {
    const opt = document.createElement('option');
    opt.value = l;
    opt.textContent = l;
    if (l === currentLevel) opt.selected = true;
    $level.appendChild(opt);
  });
}

function renderTable(items) {
  if (!items.length) {
    $tbody.innerHTML = `
      <tr><td colspan="9" class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>
        </svg>
        <p>No templates found matching your filters.</p>
      </td></tr>`;
    return;
  }

  $tbody.innerHTML = items.map(t => `
    <tr class="template-row" data-id="${escapeHtml(t.template_id)}" style="cursor:pointer">
      <td><span class="badge ${t.status === 'known' ? 'badge-known' : 'badge-unknown'}">${t.status === 'known' ? 'Known' : 'Unknown'}</span></td>
      <td><span class="tmpl-id">${escapeHtml(t.template_id)}</span></td>
      <td><div class="tmpl-text" title="${escapeHtml(t.template_text)}">${highlightWildcards(escapeHtml(t.template_text))}</div></td>
      <td>${escapeHtml(t.service)}</td>
      <td><span class="badge ${levelBadgeClass(t.level)}">${escapeHtml(t.level)}</span></td>
      <td>${formatCount(t.event_count)}</td>
      <td><span class="group-id" title="${escapeHtml(t.group_id || '—')}">${t.group_id && t.group_id !== 'UNASSIGNED_PENDING' ? escapeHtml(t.group_id) : '—'}</span></td>
      <td title="${formatFullTs(t.first_seen)}">${formatTs(t.first_seen)}</td>
      <td title="${formatFullTs(t.last_seen)}">${formatTs(t.last_seen)}</td>
    </tr>
  `).join('');

  // Row click → detail modal
  document.querySelectorAll('.template-row').forEach(row => {
    row.addEventListener('click', () => showDetail(row.dataset.id));
  });
}

function renderPagination(total, page, pages) {
  $pageInfo.textContent = `Showing ${Math.min((page - 1) * state.perPage + 1, total)}–${Math.min(page * state.perPage, total)} of ${total} templates`;

  let btns = '';
  btns += `<button class="page-btn" ${page <= 1 ? 'disabled' : ''} data-page="${page - 1}">‹</button>`;
  const range = getPageRange(page, pages);
  range.forEach(p => {
    if (p === '...') {
      btns += `<span style="padding:6px 4px;color:var(--text-muted)">…</span>`;
    } else {
      btns += `<button class="page-btn ${p === page ? 'active' : ''}" data-page="${p}">${p}</button>`;
    }
  });
  btns += `<button class="page-btn" ${page >= pages ? 'disabled' : ''} data-page="${page + 1}">›</button>`;
  $pageBtns.innerHTML = btns;

  $pageBtns.querySelectorAll('.page-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const p = parseInt(btn.dataset.page);
      if (p >= 1 && p <= pages) {
        state.page = p;
        loadData();
      }
    });
  });
}

function getPageRange(current, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages = [];
  pages.push(1);
  if (current > 3) pages.push('...');
  for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) {
    pages.push(i);
  }
  if (current < total - 2) pages.push('...');
  pages.push(total);
  return pages;
}

// ── Sort ─────────────────────────────────────────────────────────
document.querySelectorAll('thead th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const field = th.dataset.sort;
    if (state.sort === field) {
      state.order = state.order === 'desc' ? 'asc' : 'desc';
    } else {
      state.sort = field;
      state.order = 'desc';
    }
    state.page = 1;
    updateSortUI();
    loadData();
  });
});

function updateSortUI() {
  document.querySelectorAll('thead th[data-sort]').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    if (th.dataset.sort === state.sort) {
      th.classList.add('sorted');
      arrow.textContent = state.order === 'desc' ? '▼' : '▲';
    } else {
      th.classList.remove('sorted');
      arrow.textContent = '';
    }
  });
}


// ── Detail Modal ─────────────────────────────────────────────────
async function showDetail(templateId) {
  const resp = await fetch(`/api/templates/${encodeURIComponent(templateId)}`);
  const data = await resp.json();
  if (data.error) return;
  state.groupingTemplate = data;
  state.groupingRevision = data.grouping_revision;
  state.groupingSynchronization = data.grouping_synchronization || {};

  const fields = [
    ['Template ID', data.template_id, true],
    ['Status', data.status === 'known' ? 'Known' : 'Unknown / pending'],
    ['Template Text', data.template_text, true],
    ['Service', data.service],
    ['Level', data.level],
    ['Event Count', (data.event_count || 0).toLocaleString()],
    ['First Seen', formatFullTs(data.first_seen)],
    ['Last Seen', formatFullTs(data.last_seen)],
    ['Group ID', data.group_id || '—', true],
  ];

  if (data.group_info && Object.keys(data.group_info).length) {
    fields.push(['Group Template', data.group_info.representative_template || '—', true]);
    fields.push(['Documented', data.group_info.documented ? 'Yes' : 'No']);
    if (data.group_info.error_code) {
      fields.push(['Error Code', data.group_info.error_code, true]);
    }
    if (data.group_info.documentation_id) {
      fields.push(['Documentation ID', data.group_info.documentation_id, true]);
    }
  }
  if (data.manual_assignment) {
    fields.push(['Requested Group', `${data.manual_assignment.target_kind}: ${data.manual_assignment.target_id}`, true]);
  }
  if (data.grouping_result) {
    fields.push(['Assignment Result', data.grouping_result.message || data.grouping_result.state]);
  }

  $detailGrid.innerHTML = fields.map(([label, value, mono]) => `
    <div class="detail-label">${label}</div>
    <div class="detail-value${mono ? ' mono' : ''}">${escapeHtml(String(value || '—'))}</div>
  `).join('');

  $modalOverlay.classList.add('open');
}

$modalClose.addEventListener('click', () => $modalOverlay.classList.remove('open'));
$modalOverlay.addEventListener('click', (e) => {
  if (e.target === $modalOverlay) $modalOverlay.classList.remove('open');
});


document.getElementById('assign-group-command').addEventListener('click', async () => {
  $modalOverlay.classList.remove('open');
  try { await openGroupAssignment(); } catch (error) { showGroupingOperation('failed', error.message); }
});
document.querySelectorAll('#group-assignment-mode .toggle-btn').forEach(button => {
  button.addEventListener('click', () => {
    state.groupingMode = button.dataset.mode;
    document.querySelectorAll('#group-assignment-mode .toggle-btn').forEach(item => item.classList.toggle('active', item === button));
    document.getElementById('group-assignment-existing').style.display = state.groupingMode === 'existing' ? '' : 'none';
    document.getElementById('group-assignment-new').style.display = state.groupingMode === 'new' ? '' : 'none';
    renderGroupAssignmentOptions(document.getElementById('group-assignment-search').value);
  });
});
document.getElementById('group-assignment-search').addEventListener('input', event => renderGroupAssignmentOptions(event.target.value));
document.getElementById('group-assignment-save').addEventListener('click', async () => {
  const templateId = state.groupingTemplate.template_id;
  const payload = {expected_revision: state.groupingRevision};
  if (state.groupingMode === 'new') payload.create_new = true;
  else payload.target_group_id = document.getElementById('group-assignment-target').value;
  try {
    const result = await apiMutation(`/api/templates/${encodeURIComponent(templateId)}/group`, 'PUT', payload);
    state.groupingRevision = result.revision;
    $groupAssignmentModal.classList.remove('open');
    showGroupingOperation(result.state, `${templateId}: ${result.state}`);
    if (result.state !== 'applied') pollGroupingResult(result.revision, templateId);
  } catch (error) {
    document.getElementById('group-assignment-error').textContent = error.status === 409 ? 'Grouping changed. Reload and submit again.' : error.message;
    if (error.status === 409) loadData();
  }
});


// ── Event Listeners ──────────────────────────────────────────────
let searchTimeout;
$search.addEventListener('input', () => {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => {
    state.search = $search.value;
    state.page = 1;
    loadData();
  }, 300);
});

$service.addEventListener('change', () => {
  state.service = $service.value;
  state.page = 1;
  loadData();
});

$level.addEventListener('change', () => {
  state.level = $level.value;
  state.page = 1;
  loadData();
});

$statusToggle.querySelectorAll('.toggle-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    $statusToggle.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.status = btn.dataset.status;
    state.page = 1;
    loadData();
  });
});

$autoRefresh.addEventListener('change', () => {
  state.autoRefresh = $autoRefresh.checked;
  if (state.autoRefresh) {
    state.refreshTimer = setInterval(loadData, 10000);
  } else {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
});

document.getElementById('page-size').addEventListener('change', event => {
  state.perPage = Number(event.target.value);
  state.page = 1;
  loadData();
});
