// ── LLM profiles (Data sources page; as in logai-engine) ──
// The linker's resolver uses the one active profile. The API key is write-only.
const llm = {items: [], editing: null};
const $llmTbody = document.getElementById('llm-tbody');
const $llmModal = document.getElementById('llm-modal');

async function loadLlmProfiles() {
  llm.items = await getJson('/api/llm/profiles', 'Unable to load LLM profiles');
  $llmTbody.innerHTML = llm.items.length ? llm.items.map(p => `
    <tr data-profile="${escapeHtml(p.name)}">
      <td>${escapeHtml(p.name)}</td>
      <td><span class="tmpl-id">${escapeHtml(p.base_url)}</span></td>
      <td>${escapeHtml(p.model)}</td>
      <td>${p.timeout_seconds}s</td>
      <td>${p.has_api_key ? 'saved' : '—'}</td>
      <td>${p.active ? '<span class="badge badge-green">active</span>' : ''}</td>
      <td><div class="btn-row">
        <button type="button" class="btn llm-activate">${p.active ? 'Deactivate' : 'Activate'}</button>
        <button type="button" class="btn llm-edit">Edit</button>
        <button type="button" class="btn btn-danger llm-delete">Delete</button>
      </div></td>
    </tr>`).join('')
    : '<tr><td colspan="7" class="empty-state">No profile: templates are not sent to an LLM.</td></tr>';
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
  loadLlmProfiles();
});

document.getElementById('llm-form').addEventListener('submit', async event => {
  event.preventDefault();
  const name = document.getElementById('llm-name').value.trim();
  const payload = {
    base_url: document.getElementById('llm-base-url').value.trim(),
    model: document.getElementById('llm-model').value.trim(),
    api_key: document.getElementById('llm-api-key').value,
    timeout_seconds: Number(document.getElementById('llm-timeout').value),
    revision: llm.editing ? llm.editing.revision : null,
  };
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  try {
    await apiMutation(`/api/llm/profiles/${encodeURIComponent(name)}`, 'PUT', payload);
    $llmModal.classList.remove('open');
    loadLlmProfiles();
  } catch (err) {
    document.getElementById('llm-error').textContent = err.status === 409
      ? 'This profile changed in another window (or the name is taken). Close and try again.' : err.message;
  } finally { if (submit) submit.disabled = false; }
});
