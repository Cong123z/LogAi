// ── LLM status reason (reported by the engine) ──
function llmReasonText(state) {
  if (!state || state.status === 'ok') return '';
  const since = state.reason_since ? ` (since ${formatFullTs(state.reason_since)})` : '';
  return `${state.reason || 'LLM analysis is unavailable'}${since}`;
}

function renderLlmBanner(state, bannerId = 'llm-banner') {
  const banner = document.getElementById(bannerId);
  const text = llmReasonText(state);
  banner.hidden = !text;
  if (!text) return;
  banner.className = `llm-banner ${state.status === 'disabled' ? 'llm-disabled' : 'llm-error'}`;
  const label = state.status === 'disabled' ? 'LLM analysis is off' : 'LLM analysis has a problem';
  banner.innerHTML = `<strong>${label}:</strong> <span>${escapeHtml(text)}</span>`
    + (state.status === 'disabled' ? ' <a href="#llm">Open LLM profiles</a>' : '');
}

// ── LLM profiles ──
const $llmTbody = document.getElementById('llm-tbody');
const $llmModal = document.getElementById('llm-modal');
const $llmStatus = document.getElementById('llm-engine-status');
const $llmAction = document.getElementById('llm-action-status');
const llm = {profiles: [], activeId: null, signature: null};

function fetchLlmProfiles() { return getJson('/api/llm-profiles', 'Unable to load LLM profiles'); }

function renderLlmProfiles(data) {
  // Polled every few seconds; skip unchanged renders to keep focus.
  const signature = JSON.stringify(data);
  if (signature === llm.signature) return;
  llm.signature = signature;
  llm.profiles = data.profiles || [];
  llm.activeId = data.active_profile_id;
  const engine = data.engine || {};
  const waiting = engine.alive && (engine.llm_profile_id || null) !== (llm.activeId || null);
  $llmStatus.className = 'sync-status' + (waiting ? '' : (engine.status === 'error' ? ' llm-status-error' : engine.status === 'disabled' ? ' llm-status-disabled' : ''));
  if (waiting) $llmStatus.textContent = 'Waiting for the engine to switch profile…';
  else if (engine.status === 'ok') $llmStatus.textContent = `Engine: ${engine.reason || 'LLM analysis is running'}.`;
  else $llmStatus.textContent = llmReasonText(engine);
  document.getElementById('llm-use-default-btn').disabled = !llm.activeId;
  if (!llm.profiles.length) {
    $llmTbody.innerHTML = '<tr><td colspan="6" class="empty-state">No profiles yet. Add one to choose the LLM used for analysis.</td></tr>';
    return;
  }
  $llmTbody.innerHTML = llm.profiles.map(p => {
    const isActive = p.id === llm.activeId;
    const status = isActive
      ? `<span class="badge badge-green">Active</span>${engine.llm_profile_id === p.id ? ' <span class="badge badge-info">In use</span>' : ''}`
      : `<button type="button" class="btn" data-llm-use="${escapeHtml(p.id)}" aria-label="Use profile ${escapeHtml(p.name)}">Use</button>`;
    return `<tr>
      <td>${escapeHtml(p.name)}</td>
      <td><span class="tmpl-id">${escapeHtml(p.model)}</span></td>
      <td><div class="tmpl-text">${escapeHtml(p.endpoint)}</div></td>
      <td><span class="tmpl-id">${escapeHtml(p.api_key_hint || '—')}</span></td>
      <td>${status}</td>
      <td><div class="btn-row">
        <button type="button" class="btn" data-llm-edit="${escapeHtml(p.id)}" aria-label="Edit profile ${escapeHtml(p.name)}">Edit</button>
        <button type="button" class="btn btn-danger" data-llm-delete="${escapeHtml(p.id)}" aria-label="Delete profile ${escapeHtml(p.name)}">Delete</button>
      </div></td>
    </tr>`;
  }).join('');
}

async function reloadLlmProfiles() {
  llm.signature = null;
  renderLlmProfiles(await fetchLlmProfiles());
}

function openLlmEditor(profile = null) {
  document.getElementById('llm-modal-title').textContent = profile ? 'Edit LLM profile' : 'Add LLM profile';
  document.getElementById('llm-id').value = profile ? profile.id : '';
  document.getElementById('llm-name').value = profile ? profile.name : '';
  document.getElementById('llm-endpoint').value = profile ? profile.endpoint : '';
  document.getElementById('llm-model').value = profile ? profile.model : '';
  const key = document.getElementById('llm-key');
  key.value = '';
  key.required = !profile;
  key.placeholder = profile ? `Current: ${profile.api_key_hint || 'none'} (leave blank to keep; required if you change the host)` : '';
  document.getElementById('llm-activate-field').hidden = Boolean(profile);
  document.getElementById('llm-error').textContent = '';
  $llmModal.classList.add('open');
  document.getElementById('llm-name').focus();
}

document.getElementById('add-llm-profile-btn').addEventListener('click', () => openLlmEditor());
document.getElementById('llm-use-default-btn').addEventListener('click', async () => {
  $llmAction.textContent = '';
  try { await apiMutation('/api/llm-profiles/active', 'PUT', {profile_id: null}); await reloadLlmProfiles(); }
  catch (err) { $llmAction.textContent = err.message; }
});
document.getElementById('llm-form').addEventListener('submit', async event => {
  event.preventDefault();
  const id = document.getElementById('llm-id').value;
  const payload = {
    name: document.getElementById('llm-name').value,
    endpoint: document.getElementById('llm-endpoint').value,
    model: document.getElementById('llm-model').value,
    api_key: document.getElementById('llm-key').value,
  };
  if (!id) payload.activate = document.getElementById('llm-activate').checked;
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  try {
    await apiMutation(id ? `/api/llm-profiles/${encodeURIComponent(id)}` : '/api/llm-profiles', id ? 'PUT' : 'POST', payload);
    document.getElementById('llm-key').value = '';
    $llmModal.classList.remove('open');
    await reloadLlmProfiles();
  } catch (err) {
    document.getElementById('llm-error').textContent = err.message;
  } finally {
    if (submit) submit.disabled = false;
  }
});
$llmTbody.addEventListener('click', async event => {
  const use = event.target.closest('[data-llm-use]');
  const edit = event.target.closest('[data-llm-edit]');
  const del = event.target.closest('[data-llm-delete]');
  $llmAction.textContent = '';
  try {
    if (use) {
      await apiMutation('/api/llm-profiles/active', 'PUT', {profile_id: use.dataset.llmUse});
    } else if (edit) {
      openLlmEditor(llm.profiles.find(p => p.id === edit.dataset.llmEdit));
      return;
    } else if (del) {
      const profile = llm.profiles.find(p => p.id === del.dataset.llmDelete);
      if (!profile || !(await confirmDialog(`The profile "${profile.name}" and its stored API key will be removed.`, {title: 'Delete LLM profile?', confirmLabel: 'Delete profile'}))) return;
      await apiMutation(`/api/llm-profiles/${encodeURIComponent(profile.id)}`, 'DELETE', {});
    } else {
      return;
    }
    await reloadLlmProfiles();
  } catch (err) {
    $llmAction.textContent = err.message;
  }
});
