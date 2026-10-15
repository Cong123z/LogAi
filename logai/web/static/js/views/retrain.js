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
    const steps = (st.progress || {}).steps || [];
    const step = steps.length ? ` · now: ${steps[steps.length - 1].label}` : '';
    return `Retraining (${st.trigger || 'manual'}) for ${rtDuration(Date.now() / 1000 - st.started_at)}${step}… The engine restarts when it finishes.`;
  }
  const parts = [];
  if (data.engine_state === 'engine_unavailable') parts.push('The engine is not running (no recent heartbeat).');
  if (data.run_pending) parts.push('Retrain requested; waiting for the engine to start it…');
  const last = (st.history || [])[st.history ? st.history.length - 1 : 0];
  if (last && last.result === 'missed') parts.push(`The retrain scheduled for ${rtWhen(last.started_at, data.schedule.timezone)} was missed: the engine was not running.`);
  else if (st.state === 'failed' && last && last.failed_step) parts.push(`Last retrain failed at ${last.failed_step.label}${last.rolled_back ? ' (previous version restored)' : ''}.`);
  else if (st.state === 'failed') parts.push(`Last retrain failed:${st.error || 'unknown error'}${last && last.rolled_back && !/restored/.test(st.error || '') ? ' (previous version restored)' : ''}.`);
  else if (st.state === 'succeeded') parts.push(`Last retrain succeeded ${rtWhen(st.finished_at, data.schedule.timezone)}.`);
  parts.push(data.schedule.enabled ? `Next retrain: ${rtWhen(data.next_run_at, data.schedule.timezone)}.` : 'Automatic retrain is off.');
  return parts.join(' ');
}

// "+54 templates: 18 joined existing groups · 36 in 11 new groups". Templates
// the live engine placed between runs never reach the retrain's lineage, so
// "joined" is the growth since the previous run minus the new-group members.
function rtChanges(run) {
  const l = run.lineage;
  if (!l) return '—';
  const inNew = `${l.new_group_templates} in ${l.new_groups} new groups`;
  if (run.templates == null || run.prevTemplates == null) {
    return `${l.added_templates} joined existing groups · ${inNew}`;
  }
  const growth = run.templates - run.prevTemplates;
  const joined = growth - l.new_group_templates;
  // Pruned ungrouped templates can make the growth smaller than the parts.
  if (joined < 0) return `${growth >= 0 ? '+' : ''}${growth} templates · ${inNew}`;
  return `+${growth} templates: ${joined} joined existing groups · ${inNew}`;
}

<<<<<<< HEAD
=======
// One line per training step, from the counts the engine reports for it.
const rtN = n => formatCount(n || 0);
const rtTime = ts => new Date(ts * 1000).toLocaleString(undefined, {dateStyle: 'short', timeStyle: 'medium'});
const RT_STEP_TEXT = {
  range: d => [d.index, d.start_ts && `from ${rtTime(d.start_ts)}`,
    d.resumed && `resuming, ${rtN(d.already_indexed)} logs already read`].filter(Boolean).join(' · '),
  read: d => [`${rtN(d.fetched)}${d.target ? ` of ${rtN(d.target)}` : ''} logs`,
    `${rtN(d.templates)} templates`, d.skipped_dup && `${rtN(d.skipped_dup)} duplicates skipped`,
    d.rate && `${rtN(d.rate)} logs/s`, d.last_event_ts && `logs up to ${rtTime(d.last_event_ts)}`].filter(Boolean).join(' · '),
  templates: d => d.templates == null ? '' : `${rtN(d.templates)} templates: ${rtN(d.seen_in_window)} seen in the window, ${rtN(d.kept)} kept from earlier, ${rtN(d.pruned)} pruned`,
  embed: d => `${rtN(d.to_embed)} new or changed templates to embed · ${rtN(d.reused)} reused`,
  cluster: d => d.new_groups == null ? `${rtN(d.to_place)} templates without a group to place`
    : `${rtN(d.joined_existing)} joined existing groups · ${rtN(d.new_group_templates)} formed ${rtN(d.new_groups)} new groups`,
  groups: d => d.groups == null ? '' : `${rtN(d.groups)} groups (${rtN(d.singletons)} single-template) · ${rtN(d.ungrouped)} templates without a group`,
  model: d => [d.windows != null && `${rtN(d.windows)} (service, group) windows`,
    d.kept != null && `trained on ${rtN(d.kept)} of ${rtN(d.scored)} feature vectors`,
    d.fitted === false && 'not fitted: too few vectors, the previous model is kept'].filter(Boolean).join(' · '),
  publish: d => d.groups == null ? '' : `${rtN(d.templates)} templates · ${rtN(d.groups)} groups, then documentation matching`,
};

// Live steps (started_at/finished_at) and history steps (duration_seconds).
function rtStepsHtml(steps, {running = false, failedKey = null} = {}) {
  const now = Date.now() / 1000;
  return `<ol class="rt-steps">${steps.map((s, i) => {
    const last = i === steps.length - 1;
    const state = last && running ? 'current' : last && failedKey === s.key ? 'failed' : 'done';
    const seconds = s.duration_seconds ?? ((s.finished_at || now) - s.started_at);
    const d = s.detail || {};
    const text = (RT_STEP_TEXT[s.key] || (() => ''))(d);
    let bar = '';
    if (state === 'current' && s.key === 'read' && d.target) {
      const left = d.rate ? Math.max(0, d.target - d.fetched) / d.rate : null;
      bar = `<div class="rt-step-bar"><progress max="${d.target}" value="${Math.min(d.fetched || 0, d.target)}"></progress>
        <span class="ins-meta">${Math.floor(100 * (d.fetched || 0) / d.target)}%${left != null ? ` · about ${rtDuration(left)} left` : ''}</span></div>`;
    }
    return `<li class="rt-step rt-step-${state}">
      <span class="rt-step-label">${escapeHtml(s.label)}</span>
      <span class="rt-step-time">${escapeHtml(rtDuration(seconds))}</span>
      ${text ? `<div class="ins-meta">${escapeHtml(text)}</div>` : ''}${bar}
    </li>`;
  }).join('')}</ol>`;
}

// Warnings and errors the engine logged during the run (any module).
function rtLogHtml(log, open) {
  if (!log || !log.length) return '';
  return `<details class="rt-log" data-rt-log${open ? ' open' : ''}>
    <summary>Warnings and errors from the engine log (${log.length})</summary>
    <ul>${log.map(line => `<li class="rt-log-${line.level === 'WARNING' ? 'warn' : 'error'}">
      <span class="rt-log-meta">${escapeHtml(new Date(line.ts * 1000).toLocaleTimeString())} ${escapeHtml(line.level)} ${escapeHtml(line.logger)}</span>
      ${escapeHtml(line.message)}</li>`).join('')}</ul>
  </details>`;
}

function rtRunDetailHtml(run) {
  const failed = run.result === 'failed';
  let head;
  if (failed) {
    const where = run.failed_step ? `Failed at <strong>${escapeHtml(run.failed_step.label)}</strong>`
      : run.interrupted ? 'Interrupted: the engine stopped while retraining' : 'Failed before training started';
    head = `${where} after ${escapeHtml(rtDuration(run.duration_seconds))}`
      + (run.error_type ? ` · ${escapeHtml(run.error_type)}` : '')
      + (run.where ? ` · <code>${escapeHtml(run.where)}</code>` : '')
      + (run.rolled_back ? ' · previous version restored' : '');
  } else {
    head = `Retrain ${escapeHtml(run.result)} in ${escapeHtml(rtDuration(run.duration_seconds))}`;
  }
  return `<div class="rt-run-head${failed ? ' rt-run-failed' : ''}">
      <span>${head}</span>
      <button type="button" class="btn" data-rt-close>Close</button>
    </div>
    ${failed && run.error ? `<pre class="rt-error">${escapeHtml(run.error)}</pre>` : ''}
    ${run.steps && run.steps.length ? rtStepsHtml(run.steps, {failedKey: failed && run.failed_step ? run.failed_step.key : null}) : ''}
    ${rtLogHtml(run.log, failed || rt.logOpen)}`;
}

// Live steps while retraining; otherwise the run picked in the history, or
// the last run when it failed (until closed).
function renderRetrainDetail(data) {
  const st = data.status || {};
  const history = st.history || [];
  const $detail = document.getElementById('rt-run-detail');
  let html = '';
  if (data.engine_state === 'retraining' && st.progress) {
    html = rtStepsHtml(st.progress.steps || [], {running: true}) + rtLogHtml(st.progress.log, rt.logOpen);
  } else {
    const last = history[history.length - 1];
    const run = rt.openRun != null ? history.find(r => r.started_at === rt.openRun)
      : last && last.result === 'failed' && st.state === 'failed' && rt.closedRun !== last.started_at ? last : null;
    if (run) html = rtRunDetailHtml(run);
  }
  $detail.hidden = !html;
  // Polled every 2 s: rewrite only on change, so a finished run's panel keeps
  // its text selection (the log's open state is kept in rt.logOpen).
  if (rt.detailHtml !== html) $detail.innerHTML = html;
  rt.detailHtml = html;
}

>>>>>>> a20c75e5e4ce1b8ff90133f788d8286aedb884ca
function renderRetrain(data) {
  rt.data = data;
  if (!rt.dirty) rtFillForm(data);
  const busy = data.engine_state === 'retraining' || data.run_pending;
  $rtStatus.className = 'sync-status' + ((data.status || {}).state === 'failed' || data.engine_state === 'engine_unavailable' ? ' llm-status-error' : '');
  $rtStatus.textContent = (rt.dirty ? 'Unsaved changes. ' : '') + rtStatusText(data);
  const runButton = document.getElementById('rt-run-btn');
  runButton.disabled = busy;
  runButton.title = busy ? 'A retrain is already running or waiting to start' : '';
  // Template total of the previous run that reported one, so a row can show
  // what changed since then (the live engine adds templates between runs).
  let prevTemplates = null;
  const history = ((data.status || {}).history || []).map(run => {
    const row = {...run, prevTemplates};
    if (run.templates != null) prevTemplates = run.templates;
    return row;
  }).reverse();
  const badge = result => ({succeeded: 'badge-known', missed: 'badge-warn'})[result] || 'badge-red';
  const resultLabel = run => run.result === 'failed'
    ? `${run.interrupted ? 'interrupted' : 'failed'}${run.rolled_back ? ' · rolled back' : ''}`
    : run.result;
  // Hover: the error, then how long each step took.
  const resultTitle = run => [run.error, ...(run.steps || []).map(s => `${s.label}: ${rtDuration(s.duration_seconds)}`)]
    .filter(Boolean).join('\n');
  document.getElementById('rt-history-tbody').innerHTML = history.length ? history.map(run => {
    const ids = rtChanges(run);
    return `<tr>
      <td>${escapeHtml(rtWhen(run.started_at, data.schedule.timezone))}</td>
      <td>${escapeHtml(run.trigger === 'manual' ? 'Manual' : 'Schedule')}</td>
      <td>${escapeHtml(String(run.lookback_hours))} h</td>
      <td>${formatCount(run.max_docs || 0)}</td>
      <td>${escapeHtml(rtDuration(run.duration_seconds))}</td>
      <td><span class="badge ${badge(run.result)}"${resultTitle(run) ? ` title="${escapeHtml(resultTitle(run))}"` : ''}>${escapeHtml(resultLabel(run))}</span>
        ${run.failed_step ? `<div class="ins-meta">at ${escapeHtml(run.failed_step.label)}</div>` : ''}
        ${run.steps || run.error ? `<button type="button" class="rt-link" data-rt-run="${run.started_at}">Details</button>` : ''}</td>
      <td>${run.templates != null ? `${formatCount(run.templates)} / ${formatCount(run.groups)}` : '—'}</td>
      <td><span class="ins-meta">${escapeHtml(ids)}</span></td>
    </tr>`;
  }).join('') : '<tr><td colspan="8" class="empty-state">No retrain has run yet.</td></tr>';
  renderRetrainDetail(data);
}

document.getElementById('rt-history-tbody').addEventListener('click', event => {
  const button = event.target.closest('[data-rt-run]');
  if (!button || !rt.data) return;
  const startedAt = Number(button.dataset.rtRun);
  rt.openRun = rt.openRun === startedAt ? null : startedAt;
  renderRetrainDetail(rt.data);
  if (rt.openRun != null) document.getElementById('rt-run-detail').scrollIntoView({block: 'nearest'});
});
document.getElementById('rt-run-detail').addEventListener('click', event => {
  if (!event.target.closest('[data-rt-close]') || !rt.data) return;
  const history = (rt.data.status || {}).history || [];
  if (rt.openRun == null && history.length) rt.closedRun = history[history.length - 1].started_at;
  rt.openRun = null;
  renderRetrainDetail(rt.data);
});
// <details> toggle does not bubble; capture it to remember the log's state.
document.getElementById('rt-run-detail').addEventListener('toggle', event => {
  if (event.target.matches('[data-rt-log]')) rt.logOpen = event.target.open;
}, true);

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
