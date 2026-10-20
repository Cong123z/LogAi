// ── Baseline review ──
// The engine suggests the hours a service was not NORMAL; the operator says
// whether each was a real incident (Exclude) or normal (Keep). Decisions are
// used by the next daily commit or by "Recompute baseline now".
const bl = {service: '', commandId: null, pollTimer: null};
const $blService = document.getElementById('bl-service');
const $blStatus = document.getElementById('bl-status');
const $blSuggest = document.getElementById('bl-suggest-tbody');
const $blDecisions = document.getElementById('bl-decisions-tbody');
const $blDays = document.getElementById('bl-days-tbody');

const blHour = h => `${String(h).padStart(2, '0')}:00`;
const blHours = (from, to) => `${blHour(from)}–${blHour(to)}`;

function blCard(label, value, sub, tone) {
  return `<div class="stat-card${tone ? ` pl-${tone}` : ''}"><div>
    <div class="stat-label">${escapeHtml(label)}</div>
    <div class="stat-value">${escapeHtml(value)}</div>
    ${sub ? `<div class="pl-sub">${escapeHtml(sub)}</div>` : ''}
  </div></div>`;
}

function blShowStatus(message, error = false) {
  $blStatus.textContent = message;
  $blStatus.classList.toggle('ins-toast-error', error);
}

async function loadBaseline() {
  const services = await getJson('/api/services', 'Unable to load services');
  if (!services.includes(bl.service)) bl.service = services[0] || '';
  $blService.innerHTML = services.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
  $blService.value = bl.service;
  if (!bl.service) {
    document.getElementById('bl-stats').innerHTML = '';
    [$blSuggest, $blDecisions, $blDays].forEach(t => {
      t.innerHTML = `<tr><td colspan="5" class="empty-state">No services yet: the engine adds them as it reads logs.</td></tr>`;
    });
    return;
  }
  renderBaseline(await getJson(`/api/baseline?service=${encodeURIComponent(bl.service)}`, 'Unable to load baseline'));
}

function renderBaseline(data) {
  const committed = data.days.filter(d => d.committed);
  const count = type => committed.filter(d => d.daytype === type).length;
  const profile = (n, type) => n === 0
    ? (type === 'weekend' ? 'No days yet: weekday profile, upper alerts only' : 'No days yet: floor-only alerts')
    : (n < data.full_after_days ? `Young: mean ± 4σ until ${data.full_after_days} days` : 'Full: mean ± 3σ');
  const weekday = count('weekday'), weekend = count('weekend');
  const last = data.last_commit;
  document.getElementById('bl-stats').innerHTML = [
    blCard('Weekday days', String(weekday), profile(weekday, 'weekday'), weekday < data.full_after_days ? 'warn' : ''),
    blCard('Weekend days', String(weekend), profile(weekend, 'weekend'), weekend < data.full_after_days ? 'warn' : ''),
    blCard('Open suggestions', String(data.suggestions.length), 'Unreviewed hours are kept'),
    blCard('Last commit', last ? new Date(last.at).toLocaleString() : 'Never', last ? `Owner day ${last.today}` : 'Runs at the daily commit'),
  ].join('');

  $blSuggest.innerHTML = data.suggestions.length ? data.suggestions.map(s => `
    <tr data-day="${escapeHtml(s.day)}" data-from="${s.hour_from}" data-to="${s.hour_to}">
      <td>${escapeHtml(s.day)}</td>
      <td class="tpl-num">${blHours(s.hour_from, s.hour_to)}</td>
      <td><div class="doc-text" title="${escapeHtml(s.keys.join(', '))}">${s.keys.map(k => `<span class="tmpl-id">${escapeHtml(k)}</span>`).join(' ')}</div></td>
      <td><div class="btn-row">
        <button type="button" class="btn btn-danger bl-decide" data-decision="exclude">Exclude: real incident</button>
        <button type="button" class="btn bl-decide" data-decision="keep">Keep: normal</button>
      </div></td>
    </tr>`).join('')
    : '<tr><td colspan="4" class="empty-state">No unreviewed periods: every non-NORMAL hour has a decision.</td></tr>';

  $blDecisions.innerHTML = data.decisions.length ? data.decisions.map(d => `
    <tr>
      <td>${escapeHtml(d.day)}</td>
      <td class="tpl-num">${blHours(d.hour_from, d.hour_to)}</td>
      <td><span class="badge ${d.decision === 'exclude' ? 'badge-red' : 'badge-green'}">${d.decision === 'exclude' ? 'Excluded: real incident' : 'Kept: normal'}</span></td>
      <td title="${escapeHtml(formatFullTs(d.created_at))}">${escapeHtml(formatTs(d.created_at))}</td>
      <td><button type="button" class="btn bl-undo" data-id="${Number(d.id)}">Undo</button></td>
    </tr>`).join('')
    : '<tr><td colspan="5" class="empty-state">No decisions yet.</td></tr>';

  $blDays.innerHTML = data.days.length ? data.days.map(d => `
    <tr>
      <td>${escapeHtml(d.day)}</td>
      <td>${d.daytype === 'weekend' ? 'Weekend' : 'Weekday'}</td>
      <td><span class="badge ${d.committed ? 'badge-green' : 'badge-gray'}">${d.committed ? 'Committed' : 'Open'}</span></td>
    </tr>`).join('')
    : '<tr><td colspan="3" class="empty-state">No committed days yet: alerts use the floors only.</td></tr>';
}

async function blDecide(day, hourFrom, hourTo, decision) {
  const result = await apiMutation('/api/baseline/decisions', 'POST',
    {service: bl.service, day, hour_from: hourFrom, hour_to: hourTo, decision});
  if (result.warning) {
    const one = result.warning.length === 1;
    notifyDialog(`${one ? 'Hour' : 'Hours'} ${result.warning.map(blHour).join(', ')} now ${one ? 'has' : 'have'} fewer than 3 days of baseline for this day type; ${one ? 'it falls' : 'they fall'} back to floor-only alerts.`, 'Few baseline days left');
  }
  blShowStatus(`${decision === 'exclude' ? 'Excluded' : 'Kept'} ${day} ${blHours(hourFrom, hourTo)}.`);
}

$blSuggest.addEventListener('click', async event => {
  const button = event.target.closest('.bl-decide');
  if (!button) return;
  const row = button.closest('tr');
  row.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try {
    await blDecide(row.dataset.day, Number(row.dataset.from), Number(row.dataset.to), button.dataset.decision);
  } catch (err) {
    blShowStatus(`Could not save the decision: ${err.message}`, true);
  }
  loadData();
});

$blDecisions.addEventListener('click', async event => {
  const button = event.target.closest('.bl-undo');
  if (!button) return;
  button.disabled = true;
  try {
    await apiMutation(`/api/baseline/decisions/${button.dataset.id}`, 'DELETE', {});
    blShowStatus('Decision undone: the hours are suggested again.');
  } catch (err) {
    blShowStatus(`Could not undo: ${err.message}`, true);
  }
  loadData();
});

const $blFrom = document.getElementById('bl-add-from');
const $blTo = document.getElementById('bl-add-to');
$blFrom.innerHTML = Array.from({length: 24}, (_, h) => `<option value="${h}">${blHour(h)}</option>`).join('');
$blTo.innerHTML = Array.from({length: 24}, (_, h) => `<option value="${h + 1}">${blHour(h + 1)}</option>`).join('');
$blTo.value = '1';
$blFrom.addEventListener('change', () => {
  if (Number($blTo.value) <= Number($blFrom.value)) $blTo.value = String(Number($blFrom.value) + 1);
});

document.getElementById('bl-add-form').addEventListener('submit', async event => {
  event.preventDefault();
  const from = Number($blFrom.value), to = Number($blTo.value);
  if (to <= from) { blShowStatus('"To" must be after "From".', true); return; }
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  try {
    await blDecide(document.getElementById('bl-add-day').value, from, to, document.getElementById('bl-add-decision').value);
  } catch (err) {
    blShowStatus(`Could not add the range: ${err.message}`, true);
  } finally { if (submit) submit.disabled = false; }
  loadData();
});

$blService.addEventListener('change', () => { bl.service = $blService.value; blShowStatus(''); loadData(); });

async function blPollCommand() {
  clearTimeout(bl.pollTimer);
  try {
    const cmd = await getJson(`/api/commands/${bl.commandId}`, 'Unable to read the command');
    if (cmd.done) {
      const r = cmd.result || {};
      bl.commandId = null;
      document.getElementById('bl-recompute').disabled = false;
      if (r.error) blShowStatus(`Recompute failed: ${r.error}`, true);
      else blShowStatus(`Baseline recomputed: ${r.days_committed ?? 0} days newly committed, ${r.baseline_rows ?? 0} baseline rows.`);
      if (state.activeView === 'baseline') loadData();
      return;
    }
    blShowStatus('Recompute queued: the engine runs it between flushes…');
  } catch (err) {
    blShowStatus('Recompute queued; status unavailable, retrying…', true);
  }
  bl.pollTimer = setTimeout(blPollCommand, 1000);
}

document.getElementById('bl-recompute').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    bl.commandId = (await apiMutation('/api/baseline/recompute', 'POST', {})).id;
    blPollCommand();
  } catch (err) {
    button.disabled = false;
    blShowStatus(`Could not queue the recompute: ${err.message}`, true);
  }
});
