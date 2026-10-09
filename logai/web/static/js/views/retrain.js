// ── Retrain schedule ──
const rt = {data: null, dirty: false};
const RT_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const BROWSER_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
const $rtStatus = document.getElementById('rt-status');
const $rtAction = document.getElementById('rt-action-status');
document.getElementById('rt-days').innerHTML = RT_DAYS.map((day, i) =>
  `<button type="button" class="toggle-btn" data-rt-day="${i}" aria-pressed="false">${day}</button>`).join('');
if (Intl.supportedValuesOf) {
  document.getElementById('rt-timezones').innerHTML = Intl.supportedValuesOf('timeZone')
    .map(zone => `<option value="${escapeHtml(zone)}">`).join('');
}

function fetchRetrain() { return getJson('/api/retrain', 'Unable to load retrain schedule'); }

function rtSetDays(days) {
  document.querySelectorAll('[data-rt-day]').forEach(button => {
    const on = days.includes(Number(button.dataset.rtDay));
    button.classList.toggle('active', on);
    button.setAttribute('aria-pressed', String(on));
  });
  rtSyncPresets();
}

// Light up "Every day" / "Mon–Fri" when the picked days are exactly that preset.
function rtSyncPresets() {
  const days = [...document.querySelectorAll('[data-rt-day].active')].map(b => b.dataset.rtDay).join();
  document.getElementById('rt-every-day').setAttribute('aria-pressed', String(days === '0,1,2,3,4,5,6'));
  document.getElementById('rt-weekdays').setAttribute('aria-pressed', String(days === '0,1,2,3,4'));
}

function rtSetTrainingData(lookbackHours, maxDocs) {
  const days = lookbackHours >= 24 && lookbackHours % 24 === 0;
  document.getElementById('rt-lookback').value = days ? lookbackHours / 24 : lookbackHours;
  document.getElementById('rt-lookback-unit').value = days ? 'days' : 'hours';
  document.getElementById('rt-max-docs').value = maxDocs;
}

function rtFillForm(data) {
  const s = data.schedule;
  document.getElementById('rt-enabled').checked = s.enabled;
  document.getElementById('rt-time').value = s.time;
  // Never saved: offer the browser's timezone instead of the UTC default.
  document.getElementById('rt-timezone').value = data.saved ? s.timezone : BROWSER_ZONE;
  rtSetDays(s.weekdays);
  rtSetTrainingData(s.lookback_hours, s.max_docs);
}

function rtReadForm() {
  const lookback = Number(document.getElementById('rt-lookback').value);
  const unit = document.getElementById('rt-lookback-unit').value;
  return {
    enabled: document.getElementById('rt-enabled').checked,
    time: document.getElementById('rt-time').value,
    timezone: document.getElementById('rt-timezone').value.trim(),
    weekdays: [...document.querySelectorAll('[data-rt-day].active')].map(b => Number(b.dataset.rtDay)),
    lookback_hours: unit === 'days' ? lookback * 24 : lookback,
    max_docs: Number(document.getElementById('rt-max-docs').value),
  };
}

function rtMarkDirty() {
  rt.dirty = true;
  document.getElementById('rt-save-btn').disabled = false;
  document.getElementById('rt-discard-btn').disabled = false;
}

function rtWhen(ts, zone) {
  if (!ts) return '—';
  const date = new Date(ts * 1000);
  const opts = {dateStyle: 'medium', timeStyle: 'short'};
  let text;
  try { text = `${date.toLocaleString(undefined, {...opts, timeZone: zone})} (${zone})`; }
  catch (err) { text = date.toLocaleString(undefined, opts); }
  return zone && zone !== BROWSER_ZONE ? `${text} · ${date.toLocaleString(undefined, opts)} your time` : text;
}

const rtDuration = seconds => {
  const s = Math.max(0, Math.round(seconds || 0));
  return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`
    : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
};

function rtStatusText(data) {
  const st = data.status || {};
  if (data.engine_state === 'retraining') {
    return `Retraining (${st.trigger || 'manual'}) for ${rtDuration(Date.now() / 1000 - st.started_at)}… The engine restarts when it finishes.`;
  }
  const parts = [];
  if (data.engine_state === 'engine_unavailable') parts.push('The engine is not running (no recent heartbeat).');
  if (data.run_pending) parts.push('Retrain requested; waiting for the engine to start it…');
  const last = (st.history || [])[st.history ? st.history.length - 1 : 0];
  if (last && last.result === 'missed') parts.push(`The retrain scheduled for ${rtWhen(last.started_at, data.schedule.timezone)} was missed: the engine was not running.`);
  else if (st.state === 'failed') parts.push(`Last retrain failed: ${st.error || 'unknown error'}${last && last.rolled_back && !/restored/.test(st.error || '') ? ' (previous version restored)' : ''}.`);
  else if (st.state === 'succeeded') parts.push(`Last retrain succeeded ${rtWhen(st.finished_at, data.schedule.timezone)}.`);
  parts.push(data.schedule.enabled ? `Next retrain: ${rtWhen(data.next_run_at, data.schedule.timezone)}.` : 'Automatic retrain is off.');
  return parts.join(' ');
}

function renderRetrain(data) {
  rt.data = data;
  if (!rt.dirty) rtFillForm(data);
  const busy = data.engine_state === 'retraining' || data.run_pending;
  $rtStatus.className = 'sync-status' + ((data.status || {}).state === 'failed' || data.engine_state === 'engine_unavailable' ? ' llm-status-error' : '');
  $rtStatus.textContent = (rt.dirty ? 'Unsaved changes. ' : '') + rtStatusText(data);
  const runButton = document.getElementById('rt-run-btn');
  runButton.disabled = busy;
  runButton.title = busy ? 'A retrain is already running or waiting to start' : '';
  const history = [...((data.status || {}).history || [])].reverse();
  const badge = result => ({succeeded: 'badge-known', missed: 'badge-warn'})[result] || 'badge-red';
  const resultLabel = run => run.result === 'failed'
    ? `${run.interrupted ? 'interrupted' : 'failed'}${run.rolled_back ? ' · rolled back' : ''}`
    : run.result;
  document.getElementById('rt-history-tbody').innerHTML = history.length ? history.map(run => {
    const l = run.lineage;
    const ids = l ? `+${l.added_templates} templates into ${l.grown_groups} groups · ${l.new_groups} new groups` : '—';
    return `<tr>
      <td>${escapeHtml(rtWhen(run.started_at, data.schedule.timezone))}</td>
      <td>${escapeHtml(run.trigger === 'manual' ? 'Manual' : 'Schedule')}</td>
      <td>${escapeHtml(String(run.lookback_hours))} h</td>
      <td>${formatCount(run.max_docs || 0)}</td>
      <td>${escapeHtml(rtDuration(run.duration_seconds))}</td>
      <td><span class="badge ${badge(run.result)}"${run.error ? ` title="${escapeHtml(run.error)}"` : ''}>${escapeHtml(resultLabel(run))}</span></td>
      <td>${run.templates != null ? `${formatCount(run.templates)} / ${formatCount(run.groups)}` : '—'}</td>
      <td><span class="ins-meta">${escapeHtml(ids)}</span></td>
    </tr>`;
  }).join('') : '<tr><td colspan="8" class="empty-state">No retrain has run yet.</td></tr>';
}

document.getElementById('rt-form').addEventListener('input', rtMarkDirty);
document.getElementById('rt-days').addEventListener('click', event => {
  const button = event.target.closest('[data-rt-day]');
  if (!button) return;
  const on = !button.classList.contains('active');
  button.classList.toggle('active', on);
  button.setAttribute('aria-pressed', String(on));
  rtSyncPresets();
  rtMarkDirty();
});
document.getElementById('rt-every-day').addEventListener('click', () => { rtSetDays([0, 1, 2, 3, 4, 5, 6]); rtMarkDirty(); });
document.getElementById('rt-weekdays').addEventListener('click', () => { rtSetDays([0, 1, 2, 3, 4]); rtMarkDirty(); });
document.getElementById('rt-defaults').addEventListener('click', event => {
  event.preventDefault();
  if (!rt.data) return;
  rtSetTrainingData(rt.data.defaults.lookback_hours, rt.data.defaults.max_docs);
  rtMarkDirty();
});
document.getElementById('rt-discard-btn').addEventListener('click', () => {
  rt.dirty = false;
  $rtAction.textContent = '';
  document.getElementById('rt-save-btn').disabled = true;
  document.getElementById('rt-discard-btn').disabled = true;
  if (rt.data) renderRetrain(rt.data);
});
document.getElementById('rt-form').addEventListener('submit', async event => {
  event.preventDefault();
  const schedule = rtReadForm();
  if (!schedule.weekdays.length) { $rtAction.textContent = 'Choose at least one day.'; return; }
  const resp = await fetch('/api/retrain', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({schedule, revision: rt.data && rt.data.revision}),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    // 409: keep the draft; Discard loads what was saved elsewhere.
    $rtAction.textContent = resp.status === 409
      ? 'The schedule was changed elsewhere. Discard to load it, or save again to overwrite.'
      : (body.message || 'Unable to save the schedule');
    if (resp.status === 409 && rt.data) rt.data.revision = body.current_revision;
    return;
  }
  $rtAction.textContent = '';
  rt.dirty = false;
  document.getElementById('rt-save-btn').disabled = true;
  document.getElementById('rt-discard-btn').disabled = true;
  renderRetrain(body);
});
document.getElementById('rt-run-btn').addEventListener('click', () => {
  if (!rt.data) return;
  const s = rt.data.schedule;
  document.getElementById('rt-run-lookback').value = s.lookback_hours;
  document.getElementById('rt-run-max-docs').value = s.max_docs;
  document.getElementById('rt-run-error').textContent = '';
  document.getElementById('rt-run-modal').classList.add('open');
});
document.getElementById('rt-run-form').addEventListener('submit', async event => {
  event.preventDefault();
  const resp = await fetch('/api/retrain/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      lookback_hours: Number(document.getElementById('rt-run-lookback').value),
      max_docs: Number(document.getElementById('rt-run-max-docs').value),
    }),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    document.getElementById('rt-run-error').textContent = body.message || 'Unable to start the retrain';
    return;
  }
  document.getElementById('rt-run-modal').classList.remove('open');
  renderRetrain(body);
});
