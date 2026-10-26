// ── Retrain: current run, history, nightly schedule, "Run retrain" dialog ──
// Reads /api/retrain (polled: 2 s while a run is running, else 15 s); a run's details come from /api/retrain/runs/<id>.
// Actions (start, estimate, cancel) are engine commands: POST, then poll /api/commands/<id>.
const rt = {data: null, seq: 0, selected: null, routeSub: null, closed: null, shown: null, cache: {},
            draft: null, dialogSeq: 0, estTimer: null, svc: new Set(), cancelling: false};
const $rt = id => document.getElementById(id);
const RT_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const RT_BADGE = {succeeded: ['badge-green', 'Succeeded'], failed: ['badge-red', 'Failed'],
                  running: ['badge-blue', 'Running'], cancelled: ['badge-gray', 'Cancelled']};
const rtPad = n => String(n).padStart(2, '0');

// ── formatting (times in the server's timezone: the schedule and the day tiles use it too) ──
function rtParts(sec, opts) {
  const parts = {};
  new Intl.DateTimeFormat('en-GB', {timeZone: rt.data && rt.data.timezone, hourCycle: 'h23', ...opts})
    .formatToParts(new Date(sec * 1000)).forEach(p => { parts[p.type] = p.value; });
  return parts;
}
function rtAt(sec) {
  if (!sec) return '—';
  const p = rtParts(sec, {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
  return `${p.weekday} ${p.day} ${p.month} ${p.hour}:${p.minute}`;
}
function rtClock(sec) {
  const p = rtParts(sec, {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  return `${p.hour}:${p.minute}:${p.second}`;
}
function rtLocalDate(sec) {
  const p = rtParts(sec, {year: 'numeric', month: '2-digit', day: '2-digit'});
  return `${p.year}-${p.month}-${p.day}`;
}
const rtLocalHm = sec => { const p = rtParts(sec, {hour: '2-digit', minute: '2-digit'}); return `${p.hour}:${p.minute}`; };
function rtDay(iso, month = true) {   // "Tue 20 Oct" from a YYYY-MM-DD date
  const p = {};
  new Intl.DateTimeFormat('en-GB', {timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short'})
    .formatToParts(new Date(`${iso}T12:00:00Z`)).forEach(x => { p[x.type] = x.value; });
  return `${p.weekday} ${p.day}${month ? ` ${p.month}` : ''}`;
}
function rtDur(s) {
  if (s == null || s < 0) return '—';
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${rtPad(Math.floor(s % 60))}s`;
  return `${Math.floor(s / 3600)}h ${rtPad(Math.floor((s % 3600) / 60))}m`;
}
function rtEta(s) {
  if (s == null) return '';
  if (s < 90) return 'under a minute left';
  if (s < 5400) return `about ${Math.round(s / 60)}m left`;
  return `about ${Math.floor(s / 3600)}h ${rtPad(Math.round((s % 3600) / 60))}m left`;
}
function rtN(n) {
  if (n == null) return '?';
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(Math.round(n));
}
const rtKind = run => (run.kind === 'reread' ? 'Re-read' : 'Rebuild');
const rtElapsed = run => (run.finished_at || Date.now() / 1000) - run.started_at;
function rtScope(s) {
  if (!s || !s.days) return 'All services';
  const days = [...s.days].sort();
  const contiguous = days.every((d, i) => i === 0 || Date.parse(d) - Date.parse(days[i - 1]) === 86400000);
  const sameMonth = days[0].slice(0, 7) === days[days.length - 1].slice(0, 7);
  const dayText = days.length === 1 ? rtDay(days[0])
    : contiguous ? `${rtDay(days[0], !sameMonth)} – ${rtDay(days[days.length - 1])}` : days.map(d => rtDay(d)).join(', ');
  return `${s.services ? s.services.join(', ') : 'All services'} · ${dayText} · ${rtPad(s.hour_from)}:00–${rtPad(s.hour_to)}:00`;
}
const rtStep = (run, key) => (run.steps || []).find(s => s.key === key);
function rtFailedLabel(run) {
  const step = rtStep(run, run.failed_step);
  return step ? step.label : run.failed_step || 'an unknown step';
}

// One detail line per step, from the keys the engine writes.
function rtStepLine(run, step) {
  const d = step.detail || {};
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  if (step.key === 'scope') {
    const live = d.hours_live ? ` · ${d.hours_live} in the live hour` : '';
    return `${plural(d.services, 'service')} · ${d.hours_chosen} hours chosen · ${d.hours_not_in_es} not in ES${live} · ${d.hours_to_read} to read`;
  }
  if (step.key === 'partitions') return d.first_day ? `partitions ready from ${rtDay(d.first_day)}` : '';
  if (step.key === 'read') {
    const bits = [d.docs_target ? `${rtN(d.docs_read)} of ${rtN(d.docs_target)} docs` : `${rtN(d.docs_read)} docs`,
      `${d.hours_done} of ${d.hours_total} hours replaced`, `${d.new_templates} new templates`];
    if (d.rate) bits.push(`${rtN(d.rate)} docs/s`);
    if (d.position_ms) bits.push(`${run.state === 'failed' ? 'stopped at' : 'logs up to'} ${rtAt(d.position_ms / 1000)}`);
    if (d.resumed_at) bits.push(`resumed ${rtAt(d.resumed_at)}`);
    return bits.join(' · ');
  }
  if (step.key === 'mark') return `${plural(d.days, 'finished day')} marked`;
  if (step.key === 'rebuild') return `baseline rows ${d.rows_before} → ${d.rows}`;
  if (step.key === 'housekeeping') return `${plural((d.partitions_dropped || []).length, 'partition')} dropped · ${plural(d.runs_deleted, 'old run')} deleted`;
  return '';
}

// ── status line ──
function rtStatus(data) {
  const $s = $rt('rt-status');
  const last = data.runs.find(r => r.state !== 'running');
  const next = data.next_run_at ? `${rtAt(data.next_run_at)} (${data.timezone})` : 'not scheduled';
  let cls = '', text;
  if (data.engine_stale) {
    cls = 'llm-status-error';
    text = 'The engine is not running (no heartbeat). A retrain starts or continues when it is back.';
  } else if (data.current) {
    const run = data.current;
    const steps = run.steps || [];
    const step = steps.find(s => !s.finished_at) || steps[steps.length - 1];
    const now = step ? ` · now: ${step.label}…` : '…';
    text = run.kind === 'reread'
      ? `Re-reading (${run.trigger}) for ${rtDur(rtElapsed(run))}${now} Live detection keeps running.`
      : `Rebuilding (${run.trigger}) for ${rtDur(rtElapsed(run))}${now}`;
  } else if (last && last.state === 'failed') {
    cls = 'llm-status-error';
    const read = (rtStep(last, 'read') || {}).detail;
    const kept = last.kind === 'reread' && read ? ` ${read.hours_done} of ${read.hours_total} hours were replaced and kept; the baseline was not rebuilt.` : '';
    text = `Last retrain failed at ${rtFailedLabel(last)}.${kept} Next retrain: ${next}.`;
  } else {
    const done = !last ? 'No retrain has run yet.'
      : last.state === 'cancelled' ? `Last retrain was cancelled ${rtAt(last.started_at)} (${rtKind(last)}, ${rtDur(rtElapsed(last))}).`
        : `Last retrain succeeded ${rtAt(last.finished_at)} (${rtKind(last)}, ${rtDur(rtElapsed(last))}).`;
    if (!data.schedule.enabled) {
      cls = 'llm-status-disabled';
      text = `Automatic retrain is off. ${done} Use Run retrain to start one.`;
    } else text = `${done} Next retrain: ${next}.`;
  }
  $s.className = `sync-status ${cls}`.trim();
  if ($s.textContent !== text) $s.textContent = text;
}

// ── details panel ──
function rtPick(data) {
  const byId = id => data.runs.find(r => r.id === id);
  if (rt.selected) return byId(rt.selected) || {id: rt.selected};
  const row = data.current || data.runs.find(r => r.state !== 'running');
  if (!row || row.id === rt.closed) return null;
  return data.current || (row.state === 'failed' ? row : null);
}

async function rtLoadRun(row) {   // the list has no log; a finished run never changes, so it is fetched once
  if (row.state === 'running') return row;
  if (rt.cache[row.id]) return rt.cache[row.id];
  const run = await getJson(`/api/retrain/runs/${row.id}`, 'Unable to load the run');
  if (run.state !== 'running') rt.cache[row.id] = run;
  return run;
}

function rtRenderDetail(run) {
  const $panel = $rt('rt-detail');
  $panel.hidden = !run;
  if (!run) { rt.shown = null; return; }
  const running = run.state === 'running';
  const scope = run.kind === 'reread' ? rtScope(run.scope) : 'All services';
  const scopeStep = (rtStep(run, 'scope') || {}).detail || {};
  const read = (rtStep(run, 'read') || {}).detail || {};
  const meta = [`run #${run.id}`, run.trigger];
  if (run.kind === 'reread') {
    if (scopeStep.patterns != null) meta.push(`${scopeStep.patterns} pattern${scopeStep.patterns === 1 ? '' : 's'}`);
    if (read.live_lag_s != null && running) meta.push(`live lag ${rtDur(read.live_lag_s)}`);
    if (read.paused) meta.push(`lane paused ${read.paused}× for live`);
  }
  const [badge, label] = RT_BADGE[run.state] || ['badge-gray', run.state];
  $rt('rt-d-title').innerHTML = `<strong>${rtKind(run)}</strong> · ${escapeHtml(scope)} <span class="badge ${badge}">${label}</span>`;
  $rt('rt-d-meta').textContent = meta.join(' · ');
  $rt('rt-cancel').hidden = !(running && run.kind === 'reread');
  $rt('rt-cancel').disabled = rt.cancelling;
  $rt('rt-close').hidden = running && run.kind === 'reread';

  const $err = $rt('rt-d-error');
  $err.hidden = run.state !== 'failed';
  if (run.state === 'failed') {
    $err.innerHTML = `<div>Failed at <strong>${escapeHtml(rtFailedLabel(run))}</strong> after ${rtDur(rtElapsed(run))}</div>`
      + `<pre>${escapeHtml(`${run.error_type ? `${run.error_type}: ` : ''}${run.error || 'no error text'}`)}</pre>`;
  }

  // Steps: ✓ done, ● current, ✕ failed, ○ waiting; a re-read shows the rebuild it will do (or did not do).
  const steps = (run.steps || []).map(s => {
    const failed = run.state === 'failed' && s.key === run.failed_step;
    const cur = !s.finished_at && running;
    const mark = failed ? ['fail', '✕'] : cur ? ['cur', '●'] : s.finished_at ? ['done', '✓'] : ['wait', '○'];
    return {cls: mark[0], mark: mark[1], label: s.label, time: s.started_at ? rtDur((s.finished_at || Date.now() / 1000) - s.started_at) : '',
      line: rtStepLine(run, s), bar: cur && s.key === 'read' ? s.detail : null};
  });
  if (run.kind === 'reread' && !rtStep(run, 'rebuild')) {
    const why = running ? 'mark days, rebuild every service'
      : `a ${run.state} re-read never rebuilds; hours not read yet keep their old counts`;
    steps.push({cls: 'wait', mark: '○', label: 'Rebuild baseline', time: running ? 'waiting' : 'not run', line: why});
  }
  $rt('rt-d-steps').innerHTML = steps.map(s => `<li class="rt-step ${s.cls}">
      <span class="rt-mark" aria-hidden="true">${s.mark}</span><span class="rt-lbl">${escapeHtml(s.label)}</span><span class="rt-time">${escapeHtml(s.time)}</span>
      <div class="rt-sub">${escapeHtml(s.line)}</div>
      ${s.bar ? `<div class="rt-bar"><progress max="100" value="${s.bar.pct}" aria-label="Read progress"></progress><span class="ins-meta">${s.bar.pct}%${s.bar.eta_s != null ? ` · ${rtEta(s.bar.eta_s)}` : ''}</span></div>` : ''}
    </li>`).join('');

  const lines = (run.log || []).filter(l => l.level === 'WARNING' || l.level === 'ERROR');
  const $log = $rt('rt-d-log');
  $log.hidden = running;
  if (rt.shown !== run.id) $log.open = run.state === 'failed';   // the viewer's own toggle survives the polls
  $rt('rt-d-log-n').textContent = lines.length;
  $rt('rt-d-log-list').innerHTML = lines.map(l => `<li class="${l.level === 'ERROR' ? 'e' : ''}"><span class="ins-meta">${rtClock(l.ts)} ${escapeHtml(l.level)} ${escapeHtml(l.logger)}</span> ${escapeHtml(l.message)}</li>`).join('');
  rt.shown = run.id;
}

// ── schedule ──
const rtSchedule = () => rt.draft || rt.data.schedule;
function rtRenderSchedule() {
  const sch = rtSchedule();
  const dirty = rt.draft !== null && JSON.stringify(rt.draft) !== JSON.stringify(rt.data.schedule);
  const $on = $rt('rt-sch-on'), $time = $rt('rt-sch-time');
  if ($on.checked !== sch.enabled) $on.checked = sch.enabled;
  if ($time.value !== sch.time) $time.value = sch.time;
  document.querySelectorAll('#rt-sch-days [data-day]').forEach(b => {
    const on = sch.weekdays.includes(Number(b.dataset.day));
    b.setAttribute('aria-pressed', String(on));
    b.classList.toggle('active', on);
  });
  $rt('rt-sch-tz').textContent = rt.data.timezone;
  $time.setAttribute('aria-label', `Time, ${rt.data.timezone}`);
  $rt('rt-sch-note').textContent = `A run at ${rt.data.schedule.time} belongs to the new day: it rebuilds from the days before it.`;
  $rt('rt-sch-save').disabled = !dirty;
  $rt('rt-sch-discard').disabled = !dirty;
}
function rtEditSchedule(change) {
  rt.draft = change({...rtSchedule(), weekdays: [...rtSchedule().weekdays]});
  $rt('rt-sch-msg').textContent = '';
  rtRenderSchedule();
}
$rt('rt-sch-on').addEventListener('change', event => rtEditSchedule(s => ({...s, enabled: event.target.checked})));
$rt('rt-sch-time').addEventListener('input', event => event.target.value && rtEditSchedule(s => ({...s, time: event.target.value})));
$rt('rt-sch-days').addEventListener('click', event => {
  const b = event.target.closest('[data-day]');
  if (!b) return;
  const day = Number(b.dataset.day);
  rtEditSchedule(s => ({...s, weekdays: s.weekdays.includes(day) ? s.weekdays.filter(d => d !== day) : [...s.weekdays, day].sort()}));
});
$rt('rt-sch-discard').addEventListener('click', () => { rt.draft = null; $rt('rt-sch-msg').textContent = ''; rtRenderSchedule(); });
$rt('rt-sched').addEventListener('submit', async event => {
  event.preventDefault();
  const sch = rtSchedule();
  if (!sch.weekdays.length) { $rt('rt-sch-msg').textContent = 'Choose at least one day.'; return; }
  $rt('rt-sch-save').disabled = true;
  try {
    const data = await apiMutation('/api/retrain/schedule', 'PUT', {schedule: sch, revision: rt.data.revision});
    rt.draft = null;
    $rt('rt-sch-msg').textContent = '';
    rtRender(data);
  } catch (err) {
    $rt('rt-sch-msg').textContent = err.status === 409
      ? 'The schedule was changed elsewhere; your changes are kept, review them and save again.' : err.message;
    if (err.status === 409) { rt.data = await getJson('/api/retrain', 'Unable to load retrain'); }
    rtRenderSchedule();
  }
});

// ── history ──
function rtRenderHistory(data) {
  const $body = $rt('rt-history');
  $body.innerHTML = data.runs.length ? data.runs.map(r => {
    const [badge, label] = RT_BADGE[r.state] || ['badge-gray', r.state];
    const at = r.state === 'failed' ? `<div class="ins-meta">at ${escapeHtml(rtFailedLabel(r))}</div>` : '';
    return `<tr class="rt-row${r.id === rt.shown ? ' selected' : ''}" tabindex="0" data-run="${r.id}"${r.id === rt.shown ? ' aria-current="true"' : ''}>
      <td>${escapeHtml(rtAt(r.started_at))}</td><td>${rtKind(r)}</td><td>${escapeHtml(r.kind === 'reread' ? rtScope(r.scope) : 'All services')}</td>
      <td>${escapeHtml(r.trigger)}</td><td>${rtDur(rtElapsed(r))}</td><td><span class="badge ${badge}">${label}</span>${at}</td></tr>`;
  }).join('') : '<tr><td colspan="6" class="empty-state">No retrain has run yet.</td></tr>';
}
function rtOpenRun(id) { setView(`retrain/${id}`); }
$rt('rt-history').addEventListener('click', event => {
  const row = event.target.closest('[data-run]');
  if (row) rtOpenRun(row.dataset.run);
});
$rt('rt-history').addEventListener('keydown', event => {
  const row = event.target.closest('[data-run]');
  if (row && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); rtOpenRun(row.dataset.run); }
});
$rt('rt-close').addEventListener('click', () => {
  rt.closed = rt.shown;
  rt.selected = null;
  rt.routeSub = null;
  setView('retrain');
});

// ── commands (start, estimate, cancel): poll /api/commands/<id> ──
async function rtWait(commandId, alive = () => true) {
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 800));
    if (!alive()) return null;
    try {
      const cmd = await getJson(`/api/commands/${commandId}`, 'Unable to read the command');
      if (cmd.done) return cmd.result || {};
    } catch (err) { /* keep polling: the web may be restarting */ }
  }
}
$rt('rt-cancel').addEventListener('click', async () => {
  const id = rt.shown;
  rt.cancelling = true;
  $rt('rt-cancel').disabled = true;
  try {
    const result = await rtWait((await apiMutation(`/api/retrain/runs/${id}/cancel`, 'POST', {})).command_id);
    showToast(result.error ? `Cancel failed: ${result.error}` : 'Re-read cancelled. Hours already replaced stay.', Boolean(result.error));
  } catch (err) {
    showToast(`Cancel failed: ${err.message}`, true);
  }
  rt.cancelling = false;
  loadData();
});

// ── Run retrain dialog ──
const $rtModal = $rt('rt-modal');
const rtKindValue = () => $rtModal.querySelector('input[name="rt-kind"]:checked').value;
const rtSelectedDays = () => [...$rtModal.querySelectorAll('#rt-days input:checked')].map(i => i.value);

function rtOldestSec(data) {
  const ms = Object.values(data.es_oldest || {}).filter(v => v != null);
  return ms.length ? Math.min(...ms) / 1000 : null;
}

function rtBuildDialog() {
  const data = rt.data;
  const nowSec = Date.now() / 1000;
  const today = rtLocalDate(nowSec);
  const oldest = rtOldestSec(data);
  const oldestDay = oldest && rtLocalDate(oldest);
  const hour = rtParts(nowSec, {hour: '2-digit'}).hour;
  $rt('rt-es-range').textContent = (oldest ? `Elasticsearch holds ${rtAt(oldest)} to now. ` : '')
    + `The live hour (${hour}:00) and later are never re-read.`;
  const days = [];
  for (let i = 6; i >= 0; i--) days.push(new Date(Date.parse(`${today}T12:00:00Z`) - i * 86400000).toISOString().slice(0, 10));
  $rt('rt-days').innerHTML = days.map(d => {
    const missing = oldestDay && d < oldestDay;
    const sub = d === today ? `to ${hour}:00` : missing ? 'not in ES' : d === oldestDay && rtLocalHm(oldest) !== '00:00' ? `from ${rtLocalHm(oldest)}` : 'full';
    const checked = d !== today && !missing;
    return `<label class="rt-day"><input type="checkbox" value="${d}" ${checked ? 'checked' : ''} ${missing ? 'disabled' : ''}>${escapeHtml(rtDay(d, false))}<small>${escapeHtml(sub)}</small></label>`;
  }).join('');
  rt.svc = new Set();
  rtRenderChips();
  $rt('rt-hour-from').innerHTML = Array.from({length: 24}, (_, h) => `<option value="${h}">${rtPad(h)}:00</option>`).join('');
  $rt('rt-hour-to').innerHTML = Array.from({length: 24}, (_, h) => `<option value="${h + 1}">${rtPad(h + 1)}:00</option>`).join('');
  $rt('rt-hour-from').value = '0';
  $rt('rt-hour-to').value = '24';
  $rtModal.querySelector('input[name="rt-kind"][value="reread"]').checked = true;
  $rt('rt-llm').hidden = !data.llm_active;
  $rt('rt-error').textContent = '';
  rtDialogChanged();
}

function rtRenderChips() {
  const all = rt.svc.size === 0;
  $rt('rt-svc').innerHTML = `<button type="button" class="btn rt-chip" data-svc="" aria-pressed="${all}">All services</button>`
    + rt.data.services.map(s => `<button type="button" class="btn rt-chip" data-svc="${escapeHtml(s)}" aria-pressed="${rt.svc.has(s)}">${escapeHtml(s)}</button>`).join('');
}
$rt('rt-svc').addEventListener('click', event => {
  const b = event.target.closest('[data-svc]');
  if (!b) return;
  if (!b.dataset.svc) rt.svc = new Set();
  else if (!rt.svc.delete(b.dataset.svc)) rt.svc.add(b.dataset.svc);
  rtRenderChips();
  $rt('rt-svc').querySelector(`[data-svc="${CSS.escape(b.dataset.svc)}"]`).focus();   // the chips were redrawn
  rtDialogChanged();
});

function rtBody() {
  if (rtKindValue() === 'rebuild') return {kind: 'rebuild'};
  return {kind: 'reread', services: rt.svc.size ? [...rt.svc] : null, days: rtSelectedDays(),
          hour_from: Number($rt('rt-hour-from').value), hour_to: Number($rt('rt-hour-to').value)};
}

// Estimate: an engine command; the newest request wins, older ones are dropped.
function rtDialogChanged() {
  const reread = rtKindValue() === 'reread';
  $rt('rt-reread').hidden = !reread;
  $rt('rt-start').textContent = reread ? 'Start re-read' : 'Start rebuild';
  $rt('rt-start').disabled = reread && !rtSelectedDays().length;
  $rt('rt-error').textContent = '';
  clearTimeout(rt.estTimer);
  const seq = ++rt.dialogSeq;
  const $est = $rt('rt-est');
  if (!reread) { $est.textContent = 'Rebuilds the baseline from the stored counts of every service. Takes seconds.'; return; }
  if (!rtSelectedDays().length) { $est.textContent = 'Choose at least one day.'; return; }
  $est.textContent = 'Estimating…';
  rt.estTimer = setTimeout(async () => {
    try {
      const {command_id: id} = await apiMutation('/api/retrain/estimate', 'POST', rtBody());
      const r = await rtWait(id, () => seq === rt.dialogSeq && $rtModal.classList.contains('open'));
      if (!r || seq !== rt.dialogSeq) return;
      if (r.error) $est.textContent = `Cannot estimate: ${r.error}`;
      else if (!r.hours_to_read) $est.textContent = 'Nothing to re-read in this range (hours not in Elasticsearch or still live).';
      else $est.textContent = `About ${rtN(r.docs)} docs, ~${rtDur(r.eta_s).replace(/^(\d+m) \d+s$/, '$1').replace(' s', 's')}. Stored counts of these hours are replaced.`;
    } catch (err) {
      if (seq === rt.dialogSeq) $est.textContent = `Cannot estimate: ${err.message}`;
    }
  }, 400);
}
$rtModal.addEventListener('change', event => {
  if (event.target.id === 'rt-hour-from' && Number($rt('rt-hour-to').value) <= Number(event.target.value)) $rt('rt-hour-to').value = String(Number(event.target.value) + 1);
  if (event.target.id === 'rt-hour-to' && Number($rt('rt-hour-from').value) >= Number(event.target.value)) $rt('rt-hour-from').value = String(Number(event.target.value) - 1);
  rtDialogChanged();
});
$rtModal.querySelectorAll('[data-close="rt-modal"]').forEach(b => b.addEventListener('click', () => { rt.dialogSeq++; }));
$rt('rt-run').addEventListener('click', () => {
  if (!rt.data) return;
  rtBuildDialog();
  $rtModal.classList.add('open');
});
$rt('rt-form').addEventListener('submit', async event => {
  event.preventDefault();
  const $start = $rt('rt-start');
  const $error = $rt('rt-error');
  const seq = ++rt.dialogSeq;
  $start.disabled = true;
  $error.textContent = '';
  const label = $start.textContent;
  $start.textContent = 'Starting…';
  try {
    const {command_id: id} = await apiMutation('/api/retrain/runs', 'POST', rtBody());
    const r = await rtWait(id, () => seq === rt.dialogSeq && $rtModal.classList.contains('open'));
    if (!r) return;   // the dialog was closed while waiting
    if (r.error) { $error.textContent = r.error; return; }
    $rtModal.classList.remove('open');
    rt.selected = null;
    r.run_id ? rtOpenRun(r.run_id) : loadData();
  } catch (err) {
    $error.textContent = err.status === 409 ? 'A retrain is running; wait for it to finish.' : err.message;
  } finally {
    $start.textContent = label;
    $start.disabled = rtKindValue() === 'reread' && !rtSelectedDays().length;
  }
});

// ── page ──
function rtRender(data) {
  rt.data = data;
  const running = Boolean(data.current);
  POLL_MS.retrain = running ? 2000 : 15000;
  const $run = $rt('rt-run');
  const reread = running && data.current.kind === 'reread';
  $run.disabled = reread || data.engine_stale;
  $run.title = reread ? 'A re-read is running' : data.engine_stale ? 'The engine is not running' : '';
  rtStatus(data);
  rtRenderSchedule();
}

async function loadRetrain() {
  const seq = ++rt.seq;
  const data = await getJson('/api/retrain', 'Unable to load retrain');
  const sub = /^\d+$/.test(state.subView) ? Number(state.subView) : null;   // #retrain/<run id>
  if (sub !== rt.routeSub) { rt.routeSub = sub; rt.selected = sub; }
  const row = rtPick(data);
  const run = row ? await rtLoadRun(row).catch(() => row) : null;
  if (seq !== rt.seq) return;   // a newer load started meanwhile
  rtRender(data);
  rtRenderDetail(run);
  rtRenderHistory(data);
  if (state.activeView === 'retrain' && state.alertRefreshTimer && rt.pollMs !== POLL_MS.retrain) {   // 2 s while running, else 15 s
    rt.pollMs = POLL_MS.retrain;
    clearInterval(state.alertRefreshTimer);
    state.alertRefreshTimer = setInterval(loadData, rt.pollMs);
  }
}
