// ── Data sources (Elasticsearch indices) ──
const src = {data: null, draft: null, signature: null};
const $srcStatus = document.getElementById('src-status');
const $srcAction = document.getElementById('src-action-status');

function fetchSources() { return getJson('/api/es-indices', 'Unable to load data sources'); }

const srcSaved = () => ((src.data && src.data.entries) || []).map(e => e.pattern);
const srcPatterns = () => src.draft || srcSaved();
const srcDirty = () => src.draft !== null && JSON.stringify(src.draft) !== JSON.stringify(srcSaved());

function srcMatches(pattern, index) {
  const re = new RegExp('^' + pattern.split('*').map(p => p.replace(/[.+?^${}()|[\]\\#-]/g, '\\$&')).join('.*') + '$');
  return re.test(index);
}

function srcStatusText(data) {
  if (data.state === 'engine_unavailable') return 'The engine is not running (no recent heartbeat); the selection is applied when it starts.';
  if (data.state === 'pending') return 'Saved. Waiting for the engine to apply the new selection…';
  if (data.state === 'no_index_selected') return 'No index selected: the engine is not reading any logs.';
  const active = data.active_indices;
  if (data.source === 'configured') return 'The engine reads the index from its configuration. Save a selection here to choose indices.';
  return `Engine is reading ${active ? active.length : 0} index(es).`;
}

function renderSources(data, force = false) {
  const signature = JSON.stringify([data, src.draft, document.getElementById('src-filter').value]);
  if (!force && signature === src.signature) return;
  src.signature = signature;
  src.data = data;
  const dirty = srcDirty();
  $srcStatus.className = 'sync-status' + (data.error || data.state === 'engine_unavailable' ? ' llm-status-error' : '');
  $srcStatus.textContent = (dirty ? 'Unsaved changes. ' : '') + srcStatusText(data) + (data.error ? ` Elasticsearch: ${data.error}` : '');
  document.getElementById('src-save-btn').disabled = !dirty;
  document.getElementById('src-discard-btn').disabled = !dirty;

  const addedAt = Object.fromEntries((data.entries || []).map(e => [e.pattern, e.added_at]));
  const resolved = data.resolved || {};
  const progress = data.indices || {};
  const now = Date.now() / 1000;
  const patterns = srcPatterns();
  document.getElementById('src-selected-tbody').innerHTML = patterns.length ? patterns.map(pattern => {
    const saved = pattern in addedAt;
    const indices = resolved[pattern];
    let reading;
    if (!saved) reading = '<span class="badge badge-yellow">Not saved yet</span>';
    else if (data.source === 'configured' || !indices) reading = '<span class="ins-meta">—</span>';
    else if (!indices.length) reading = '<span class="badge badge-gray">No matching index yet</span>';
    else reading = indices.map(index => {
      const p = progress[index] || {};
      const lag = p.last_event_ts ? `last event ${Math.max(0, Math.round(now - p.last_event_ts))}s ago` : 'no events yet';
      const badge = p.error ? `<span class="badge badge-red" title="${escapeHtml(p.error)}">error</span>` : '';
      return `<div><span class="tmpl-id">${escapeHtml(index)}</span> <span class="ins-meta">${escapeHtml(lag)} · ${Number(p.events_total || 0)} read</span> ${badge}</div>`;
    }).join('');
    return `<tr>
      <td><span class="tmpl-id">${escapeHtml(pattern)}</span></td>
      <td>${reading}</td>
      <td><span class="ins-meta">${saved ? escapeHtml(formatFullTs(addedAt[pattern])) : 'on save'}</span></td>
      <td><button type="button" class="btn btn-danger" data-src-remove="${escapeHtml(pattern)}" aria-label="Stop reading ${escapeHtml(pattern)}">Remove</button></td>
    </tr>`;
  }).join('') : '<tr><td colspan="4" class="empty-state">Nothing selected. Tick an index below or add a pattern.</td></tr>';

  const filter = document.getElementById('src-filter').value.trim().toLowerCase();
  const available = data.available;
  const $available = document.getElementById('src-available-tbody');
  if (!available) {
    $available.innerHTML = '<tr><td colspan="4" class="empty-state">The engine has not listed the Elasticsearch indices yet.</td></tr>';
    return;
  }
  const rows = available.filter(item => !filter || item.index.includes(filter));
  $available.innerHTML = rows.length ? rows.map(item => {
    const exact = patterns.includes(item.index);
    const viaPattern = !exact && patterns.find(p => p.includes('*') && srcMatches(p, item.index));
    const health = {green: 'badge-green', yellow: 'badge-yellow', red: 'badge-red'}[item.health] || 'badge-gray';
    return `<tr>
      <td><label class="switch"${viaPattern ? ` title="Read through the pattern ${escapeHtml(viaPattern)}"` : ''}><input type="checkbox" role="switch" data-src-toggle="${escapeHtml(item.index)}" ${exact || viaPattern ? 'checked' : ''} ${viaPattern ? 'disabled' : ''} aria-label="Read ${escapeHtml(item.index)}"></label></td>
      <td><span class="tmpl-id">${escapeHtml(item.index)}</span>${viaPattern ? ` <span class="ins-meta">via ${escapeHtml(viaPattern)}</span>` : ''}</td>
      <td>${item.docs_count == null ? '—' : Number(item.docs_count).toLocaleString()}</td>
      <td><span class="badge ${health}">${escapeHtml(item.health || 'unknown')}</span></td>
    </tr>`;
  }).join('') : '<tr><td colspan="4" class="empty-state">No index matches the filter.</td></tr>';
}

function srcEdit(change) {
  const next = change(srcPatterns().slice());
  src.draft = next;
  $srcAction.textContent = '';
  renderSources(src.data, true);
}

document.getElementById('src-add-form').addEventListener('submit', event => {
  event.preventDefault();
  const input = document.getElementById('src-pattern');
  const pattern = input.value.trim().toLowerCase();
  if (!pattern) return;
  srcEdit(list => list.includes(pattern) ? list : [...list, pattern]);
  input.value = '';
});
document.getElementById('src-filter').addEventListener('input', () => renderSources(src.data, true));
document.getElementById('src-selected-tbody').addEventListener('click', event => {
  const remove = event.target.closest('[data-src-remove]');
  if (remove) srcEdit(list => list.filter(p => p !== remove.dataset.srcRemove));
});
document.getElementById('src-available-tbody').addEventListener('change', event => {
  const box = event.target.closest('[data-src-toggle]');
  if (!box) return;
  const index = box.dataset.srcToggle;
  srcEdit(list => box.checked ? (list.includes(index) ? list : [...list, index]) : list.filter(p => p !== index));
});
document.getElementById('src-discard-btn').addEventListener('click', () => {
  src.draft = null;
  $srcAction.textContent = '';
  renderSources(src.data, true);
});
document.getElementById('src-save-btn').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  $srcAction.textContent = '';
  try {
    await apiMutation('/api/es-indices', 'PUT', {patterns: srcPatterns(), revision: src.data.revision});
    src.draft = null;
    renderSources(await fetchSources(), true);
  } catch (err) {
    $srcAction.textContent = err.status === 409
      ? 'The selection was changed elsewhere; your changes are kept, review them and save again.'
      : err.message;
    if (err.status === 409) renderSources(await fetchSources(), true);
    else button.disabled = false;
  }
});

// ── Data sources: Elasticsearch endpoint and credentials ──
const conn = {data: null, dirty: false};
const $connForm = document.getElementById('src-conn-form');
const $connStatus = document.getElementById('src-conn-status');
const $connError = document.getElementById('src-conn-error');
const $connHosts = document.getElementById('src-conn-hosts');
const $connUser = document.getElementById('src-conn-user');
const $connPassword = document.getElementById('src-conn-password');
const $connVerify = document.getElementById('src-conn-verify');

function fetchConnection() { return getJson('/api/es-connection', 'Unable to load the Elasticsearch connection'); }

const connHosts = value => value.split(',').map(h => h.trim()).filter(Boolean);
// Same cluster test as the engine: scheme, host and port, order ignored.
function connCluster(hosts) {
  return (hosts || []).map(h => {
    try {
      const url = new URL(h);
      return `${url.protocol}//${url.hostname.toLowerCase()}:${url.port || (url.protocol === 'https:' ? 443 : 9200)}`;
    } catch (err) { return h; }
  }).sort().join(',');
}

function connStatusText(data) {
  const engine = data.engine || {};
  const hosts = (engine.hosts || []).join(', ') || 'Elasticsearch';
  const from = data.source === 'env' ? 'Using the server configuration. ' : '';
  if (data.state === 'engine_unavailable') return `${from}The engine is not running; the connection is used when it starts.`;
  if (data.state === 'pending') return 'Saved. Waiting for the engine to connect…';
  if (data.state === 'error') return `${from}Cannot connect to ${hosts}${data.error ? `: ${data.error}` : '.'}`;
  return `${from}Connected to ${hosts}${engine.username ? ` as ${engine.username}` : ''}.`;
}

function renderConnection(data) {
  conn.data = data;
  $connStatus.className = 'sync-status' + (data.state === 'error' || data.state === 'engine_unavailable' ? ' llm-status-error' : '');
  $connStatus.textContent = connStatusText(data);
  document.getElementById('src-conn-default-btn').disabled = data.source === 'env';
  if (conn.dirty) return;  // never overwrite what the user is typing
  const shown = data.saved || data.engine || {};
  $connHosts.value = (shown.hosts || []).join(', ');
  $connUser.value = shown.username || '';
  $connVerify.checked = shown.verify_certs !== false;
  $connPassword.value = '';
  $connPassword.placeholder = data.saved && data.saved.password_set ? 'Unchanged (leave empty to keep)' : '';
}

function connSetDirty(dirty) {
  conn.dirty = dirty;
  document.getElementById('src-conn-save-btn').disabled = !dirty;
  document.getElementById('src-conn-discard-btn').disabled = !dirty;
}

const connMovesCluster = hosts => connCluster(hosts) !== connCluster((conn.data.engine || {}).hosts);
const CONN_MOVE_WARNING = 'This endpoint is another cluster: its indices are read from now on, earlier logs are not backfilled.';

async function connMutate(method, body, url = '/api/es-connection') {
  $connError.textContent = '';
  try {
    const data = await apiMutation(url, method, body);
    $connPassword.value = '';
    connSetDirty(false);
    renderConnection(data);
  } catch (err) {
    $connError.textContent = err.status === 409
      ? 'The connection was changed elsewhere; review it and save again.'
      : err.message;
    if (err.status === 409) renderConnection(await fetchConnection());
  }
}

$connForm.addEventListener('input', () => { connSetDirty(true); $connError.textContent = ''; });
document.getElementById('src-conn-discard-btn').addEventListener('click', () => {
  connSetDirty(false);
  $connError.textContent = '';
  renderConnection(conn.data);
});
$connForm.addEventListener('submit', async event => {
  event.preventDefault();
  const hosts = connHosts($connHosts.value);
  if (connMovesCluster(hosts) && !await confirmDialog(CONN_MOVE_WARNING, {title: 'Switch cluster?', confirmLabel: 'Save connection', danger: false})) return;
  await connMutate('PUT', {
    hosts, username: $connUser.value, password: $connPassword.value,
    verify_certs: $connVerify.checked, revision: conn.data.saved ? conn.data.saved.revision : null,
  });
});
document.getElementById('src-conn-default-btn').addEventListener('click', async () => {
  const saved = conn.data && conn.data.saved;
  if (!saved) return;
  if (!await confirmDialog('Use the endpoint and credentials from the server configuration again? If it is another cluster, its indices are read from now on.', {title: 'Use server default?', confirmLabel: 'Use server default', danger: false})) return;
  await connMutate('DELETE', undefined, `/api/es-connection?revision=${encodeURIComponent(saved.revision)}`);
});
