// ── LLM profiles (as in logai-engine) ──
// The linker's resolver calls the active profiles in order (1, 2, 3...); the API key is write-only.
// The banner and the usage line show the resolver's state and what it produced (polled every 5 s).
const llm = {items: [], editing: null, health: {}};
const $llmOrder = document.getElementById('llm-order');
const $llmUnused = document.getElementById('llm-unused');
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

function llmUsageLine(usage) {
  const count = (period, ...sources) => sources.reduce((n, s) => n + (usage[period][s] || 0), 0);
  const limit = usage.limits.daily_max ? ` / ${usage.limits.daily_max}` : '';
  document.getElementById('llm-usage').innerHTML = `Last 24 h: ${count('day', 'llm', 'draft', 'rejected')}${limit} calls · `
    + `${count('day', 'llm')} confirmed · ${count('day', 'draft')} suggested · ${count('day', 'rejected')} rejected · `
    + `<a href="#documentation/suggestions">${usage.drafts_waiting} waiting for review</a>`;
}

const llmKey = p => (p.has_api_key ? 'saved' : 'none');
const llmMeta = p => `${escapeHtml(p.model)} · ${escapeHtml(p.base_url)} · key ${llmKey(p)}`;

function llmSlot(p, i, order, inUse) {
  const h = llm.health[p.name] || {};
  const failing = h.down_until && h.down_until * 1000 > Date.now();
  const use = p.name === inUse;
  const [cls, badge, text] = failing
    ? ['fail', 'badge-red', `FAILING · skipped until ${new Date(h.down_until * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`]
    : use ? ['use', 'badge-blue', 'IN USE'] : ['', 'badge-gray', 'STANDBY'];
  const note = failing ? h.error || '' : use ? `${h.answers || 0} answers since start` : 'Answers only when the ones above fail';
  const name = escapeHtml(p.name);
  return `<div class="llm-slot ${cls}" data-profile="${name}">
      <div class="llm-num" aria-label="Order ${i + 1}">${i + 1}</div>
      <div class="llm-main">
        <div><strong>${name}</strong> <span class="hint">priority ${i + 1}</span> <span class="badge ${badge}">${text}</span></div>
        <div class="tmpl-id">${llmMeta(p)}</div>
        <div class="hint" style="overflow-wrap:anywhere">${escapeHtml(note)}</div>
      </div>
      <div class="btn-row">
        <button type="button" class="btn llm-move" data-dir="-1" aria-label="Move ${name} up"${i === 0 ? ' disabled' : ''}>↑</button>
        <button type="button" class="btn llm-move" data-dir="1" aria-label="Move ${name} down"${i === order.length - 1 ? ' disabled' : ''}>↓</button>
        <button type="button" class="btn llm-edit">Edit</button>
        <button type="button" class="btn llm-stop">Stop using</button>
      </div>
    </div>${i < order.length - 1 ? `<div class="llm-arrow">↓ if it fails, call #${i + 2}</div>` : ''}`;
}

async function loadLlmPage() {
  const [items, usage] = await Promise.all([
    getJson('/api/llm/profiles', 'Unable to load LLM profiles'),
    getJson('/api/llm/usage', 'Unable to load LLM usage'),
  ]);
  const actives = items.filter(p => p.active);   // the API lists them by priority
  llmBanner(usage, actives.length ? actives : null);
  llmUsageLine(usage);
  llm.health = usage.profiles || {};
  if ($llmOrder.contains(document.activeElement) || $llmUnused.contains(document.activeElement)) return;   // do not swap the lists under a click
  llm.items = items;
  const inUse = (actives.find(p => !(llm.health[p.name] || {}).down_until
    || llm.health[p.name].down_until * 1000 <= Date.now()) || {}).name;
  $llmOrder.innerHTML = actives.length ? actives.map((p, i) => llmSlot(p, i, actives, inUse)).join('')
    : '<p class="empty-state">No profile in the call order: templates are not sent to an LLM.</p>';
  const unused = items.filter(p => !p.active);
  $llmUnused.innerHTML = unused.length ? unused.map(p => `
    <div class="llm-slot off" data-profile="${escapeHtml(p.name)}">
      <div class="llm-main"><strong>${escapeHtml(p.name)}</strong><div class="tmpl-id">${llmMeta(p)}</div></div>
      <div class="btn-row">
        <button type="button" class="btn llm-add-order">Add as #${actives.length + 1}</button>
        <button type="button" class="btn llm-edit">Edit</button>
        <button type="button" class="btn btn-danger llm-delete">Delete</button>
      </div></div>`).join('')
    : '<p class="hint">Every profile is in the call order.</p>';
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

async function onLlmClick(event) {
  const row = event.target.closest('[data-profile]');
  const btn = event.target.closest('button');
  if (!row || !btn) return;
  const name = row.dataset.profile;
  const order = llm.items.filter(p => p.active).map(p => p.name);
  const i = order.indexOf(name);
  let next = null, focus = null;
  if (btn.classList.contains('llm-edit')) return openLlmEditor(name);
  if (btn.classList.contains('llm-move')) {
    const j = i + Number(btn.dataset.dir);
    [order[i], order[j]] = [order[j], order[i]];
    next = order;
    focus = `[data-profile="${CSS.escape(name)}"] [data-dir="${btn.dataset.dir}"]`;
  } else if (btn.classList.contains('llm-stop')) next = order.filter(n => n !== name);
  else if (btn.classList.contains('llm-add-order')) next = [...order, name];
  try {
    if (btn.classList.contains('llm-delete')) {
      if (!(await confirmDialog(`The LLM profile ${name} will be deleted.`, {title: 'Delete this profile?', confirmLabel: 'Delete'}))) return;
      await apiMutation(`/api/llm/profiles/${encodeURIComponent(name)}`, 'DELETE', {});
    } else if (next) await apiMutation('/api/llm/profiles/order', 'PUT', {order: next});   // the whole order, every time
    else return;
  } catch (err) {
    notifyDialog(err.message, 'Unable to update the profile');
  }
  document.activeElement.blur();
  await loadLlmPage();
  if (focus) { const el = document.querySelector(focus); if (el && !el.disabled) el.focus(); }
}
$llmOrder.addEventListener('click', onLlmClick);
$llmUnused.addEventListener('click', onLlmClick);

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
    loadLlmPage();
  } catch (err) {
    document.getElementById('llm-error').textContent = err.status === 409
      ? 'This profile changed in another window (or the name is taken). Close and try again.' : err.message;
  } finally { if (submit) submit.disabled = false; }
});
