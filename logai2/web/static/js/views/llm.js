// ── LLM profiles (as in logai-engine) ──
// The linker's resolver uses the one active profile; the API key is write-only. The banner and the
// cards show the resolver's state and what it produced (polled every 5 s).
const llm = {items: [], editing: null};
const $llmTbody = document.getElementById('llm-tbody');
const $llmModal = document.getElementById('llm-modal');

function llmBanner(usage, active) {
  const down = Object.entries(usage.profiles || {}).filter(([, h]) => h.down_until);
  const $b = document.getElementById('llm-banner');
  let cls = '', text = '';
  if (!active) {
    cls = 'llm-disabled';
    text = 'No active profile: templates are not sent to an LLM. Add a profile and activate it.';
  } else if (!usage.linker_running) {
    cls = 'llm-error';
    text = 'The linker is not running: nothing is sent to the LLM. Check the logai2-linker container.';
  } else if (down.length) {
    cls = down.length === active.length ? 'llm-error' : 'llm-disabled';
    text = `${down.map(([name, h]) => `${name} is failing (${h.error})`).join('; ')}`
      + (down.length === active.length ? '. No active profile answers.' : '. The next active profile answers instead.');
  } else if (usage.last_error) {
    cls = 'llm-disabled';
    text = `Last LLM error: ${usage.last_error}`;
  } else if (usage.warnings.some(w => w.startsWith('LLM daily limit'))) {
    cls = 'llm-disabled';
    text = usage.warnings.find(w => w.startsWith('LLM daily limit'));
  }
  $b.hidden = !text;
  $b.className = `llm-banner ${cls}`;
  $b.textContent = text;
}

function llmStats(usage, actives) {
  const active = actives[0];
  const count = (period, ...sources) => sources.reduce((n, s) => n + (usage[period][s] || 0), 0);
  const last = usage.last_cycle;
  document.getElementById('llm-stats').innerHTML = [
    plCard('Active profiles', active ? String(actives.length) : 'None',
      active ? actives.map(p => p.name).join(' → ') : 'nothing is sent', active ? '' : 'warn'),
    plCard('Last hour', String(count('hour', 'llm', 'draft', 'rejected')),
      `limit ${usage.limits.per_service_hour} per service`),
    plCard('Last 24 h', String(count('day', 'llm', 'draft', 'rejected')),
      `${count('day', 'llm')} confirmed · ${count('day', 'draft')} suggested · ${count('day', 'rejected')} rejected · limit ${usage.limits.daily_max}`),
    plCard('Suggestions waiting', String(usage.drafts_waiting), 'Documentation → AI suggestions', usage.drafts_waiting ? 'warn' : ''),
    plCard('Last cycle', last ? `${last.confirmed + last.suggested} done` : '—',
      last ? `${last.failed} failed · ${last.none} none` : 'no LLM call yet'),
  ].join('');
}

async function loadLlmPage() {
  const [items, usage] = await Promise.all([
    getJson('/api/llm/profiles', 'Unable to load LLM profiles'),
    getJson('/api/llm/usage', 'Unable to load LLM usage'),
  ]);
  const actives = items.filter(p => p.active);
  llmBanner(usage, actives.length ? actives : null);
  llmStats(usage, actives);
  llm.health = usage.profiles || {};
  if ($llmTbody.contains(document.activeElement)) return;   // do not swap the table under a click
  llm.items = items;
  $llmTbody.innerHTML = llm.items.length ? llm.items.map(p => `
    <tr data-profile="${escapeHtml(p.name)}">
      <td>${p.priority}</td>
      <td>${escapeHtml(p.name)}</td>
      <td><span class="tmpl-id">${escapeHtml(p.base_url)}</span></td>
      <td>${escapeHtml(p.model)}</td>
      <td>${p.timeout_seconds}s</td>
      <td>${p.has_api_key ? 'saved' : '—'}</td>
      <td>${llmStatusBadge(p)}</td>
      <td>${p.active ? ((llm.health[p.name] || {}).answers || 0) : '—'}</td>
      <td><div class="btn-row">
        <button type="button" class="btn llm-activate">${p.active ? 'Deactivate' : 'Activate'}</button>
        <button type="button" class="btn llm-edit">Edit</button>
        <button type="button" class="btn btn-danger llm-delete">Delete</button>
      </div></td>
    </tr>`).join('')
    : '<tr><td colspan="9" class="empty-state">No profile: templates are not sent to an LLM.</td></tr>';
}

function llmStatusBadge(p) {
  if (!p.active) return '<span class="badge badge-gray">inactive</span>';
  const h = (llm.health || {})[p.name];
  return h && h.down_until
    ? `<span class="badge badge-red" title="${escapeHtml(h.error || '')}">failing: skipped until ${escapeHtml(new Date(h.down_until * 1000).toLocaleTimeString())}</span>`
    : '<span class="badge badge-green">active</span>';
}

function openLlmEditor(name = null) {
  const p = llm.items.find(item => item.name === name);
  llm.editing = p || null;
  document.getElementById('llm-modal-title').textContent = p ? `Edit ${p.name}` : 'Add LLM profile';
  document.getElementById('llm-name').value = p ? p.name : '';
  document.getElementById('llm-name').readOnly = Boolean(p);
  document.getElementById('llm-base-url').value = p ? p.base_url : '';
  document.getElementById('llm-model').value = p ? p.model : '';
  document.getElementById('llm-api-key').value = '';
  document.getElementById('llm-timeout').value = p ? p.timeout_seconds : 120;
  document.getElementById('llm-priority').value = p ? p.priority : 100;
  document.getElementById('llm-error').textContent = '';
  $llmModal.classList.add('open');
}

document.getElementById('llm-add-btn').addEventListener('click', () => openLlmEditor());

$llmTbody.addEventListener('click', async event => {
  const row = event.target.closest('tr[data-profile]');
  if (!row) return;
  const name = row.dataset.profile;
  const p = llm.items.find(item => item.name === name);
  try {
    if (event.target.closest('.llm-edit')) return openLlmEditor(name);
    if (event.target.closest('.llm-activate')) {
      await apiMutation(`/api/llm/profiles/${encodeURIComponent(name)}/activate`, 'POST', {active: !p.active});
    } else if (event.target.closest('.llm-delete')) {
      if (!(await confirmDialog(`The LLM profile ${name} will be deleted.`, {title: 'Delete this profile?', confirmLabel: 'Delete'}))) return;
      await apiMutation(`/api/llm/profiles/${encodeURIComponent(name)}`, 'DELETE', {});
    } else return;
  } catch (err) {
    notifyDialog(err.message, 'Unable to update the profile');
  }
  loadLlmPage();
});

document.getElementById('llm-form').addEventListener('submit', async event => {
  event.preventDefault();
  const name = document.getElementById('llm-name').value.trim();
  const payload = {
    base_url: document.getElementById('llm-base-url').value.trim(),
    model: document.getElementById('llm-model').value.trim(),
    api_key: document.getElementById('llm-api-key').value,
    timeout_seconds: Number(document.getElementById('llm-timeout').value),
    priority: Number(document.getElementById('llm-priority').value),
    revision: llm.editing ? llm.editing.revision : null,
  };
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  try {
    await apiMutation(`/api/llm/profiles/${encodeURIComponent(name)}`, 'PUT', payload);
    $llmModal.classList.remove('open');
    loadLlmPage();
  } catch (err) {
    document.getElementById('llm-error').textContent = err.status === 409
      ? 'This profile changed in another window (or the name is taken). Close and try again.' : err.message;
  } finally { if (submit) submit.disabled = false; }
});
