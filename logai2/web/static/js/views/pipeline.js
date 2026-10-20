// ── Pipeline (live engine status from worker_status, written every flush) ──
// The sparkline keeps one events/s sample per heartbeat and lives only in this tab.
const pl = {samples: [], lastBeat: null};
const PL_MAX_SAMPLES = 60;

function fetchPipeline() { return getJson('/api/pipeline', 'Unable to load pipeline stats'); }

function plDuration(seconds) {
  if (seconds == null || !isFinite(seconds)) return '—';
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
}

const plPercent = ratio => (ratio == null ? '—' : `${(ratio * 100).toFixed(1)}%`);

function plCard(label, value, sub = '', tone = '') {
  return `<div class="stat-card${tone ? ` pl-${tone}` : ''}"><div>
    <div class="stat-label">${escapeHtml(label)}</div>
    <div class="stat-value">${escapeHtml(value)}</div>
    ${sub ? `<div class="pl-sub">${escapeHtml(sub)}</div>` : ''}
  </div></div>`;
}

function plRecordSample(s) {
  if (!s.heartbeat || s.heartbeat === pl.lastBeat) return;
  pl.lastBeat = s.heartbeat;
  pl.samples.push(s.events_per_second || 0);
  if (pl.samples.length > PL_MAX_SAMPLES) pl.samples.shift();
}

function plSparkline() {
  const rates = pl.samples;
  const $svg = document.getElementById('pl-spark');
  const $caption = document.getElementById('pl-spark-caption');
  if (rates.length < 2) {
    $svg.innerHTML = '';
    $caption.textContent = 'Collecting samples… (one every heartbeat)';
    return;
  }
  const max = Math.max(...rates, 1);
  const step = 100 / (PL_MAX_SAMPLES - 1);
  const offset = 100 - (rates.length - 1) * step;
  const points = rates.map((r, i) => `${(offset + i * step).toFixed(2)},${(38 - r / max * 34).toFixed(2)}`).join(' ');
  $svg.innerHTML = `<polyline points="${points}" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
  $caption.textContent = `Events/s, last ${rates.length} heartbeats · peak ${max.toFixed(1)}/s`;
}

function renderPipeline({status, cursors, stale}) {
  const $status = document.getElementById('pl-status');
  $status.className = 'sync-status' + (stale ? ' llm-status-error' : '');
  if (!status) {
    $status.textContent = 'The engine has not reported yet (no heartbeat).';
    return;
  }
  $status.textContent = stale
    ? `The engine is not running (last heartbeat ${formatTs(status.heartbeat)}). Showing its last numbers.`
    : `Engine running · heartbeat ${formatTs(status.heartbeat)}`;

  document.getElementById('pl-warnings').innerHTML = (status.warnings || [])
    .map(w => `<p class="ins-toast ins-toast-error">${escapeHtml(w)}</p>`).join('');

  if (!stale) plRecordSample(status);
  const lag = status.lag_seconds;
  document.getElementById('pl-throughput').innerHTML = [
    plCard('Lag', plDuration(lag), 'behind the newest log read', lag > 300 ? 'warn' : ''),
    plCard('Events/s', `${Number(status.events_per_second || 0).toFixed(1)}/s`, 'since the last flush'),
    plCard('Cache hit ratio', plPercent(status.cache_hit_ratio), 'lines already templated'),
  ].join('');
  plSparkline();

  const boot = status.bootstrap || {};
  const commit = status.last_commit;
  document.getElementById('pl-engine').innerHTML = [
    plCard('Detection', status.detection_enabled ? 'On' : 'Off',
      status.detection_enabled ? 'alerts are raised' : 'catching up; counts only', status.detection_enabled ? '' : 'warn'),
    plCard('Bootstrap', boot.since ? formatFullTs(boot.since) : '—', boot.until ? `first start read until ${formatFullTs(boot.until)}` : ''),
    plCard('Last commit', commit ? formatFullTs(Date.parse(commit.at) / 1000) : 'never', commit ? `baseline day ${commit.today}` : 'daily baseline commit'),
  ].join('');

  // Position per index from the engine's live status; es_cursors fills in indices it is not reporting.
  const indices = {...Object.fromEntries((cursors || []).map(c => [c.index, {pos_ms: c.since_ms, floor: c.floor_ms}])),
                   ...(status.indices || {})};
  const names = Object.keys(indices).sort();
  document.getElementById('pl-indices-tbody').innerHTML = names.length ? names.map(index => {
    const item = indices[index];
    const badge = item.failing ? '<span class="badge badge-red">failing</span>'
      : item.caught_up ? '<span class="badge badge-green">caught up</span>'
      : item.caught_up === false ? '<span class="badge badge-yellow">catching up</span>'
      : '<span class="badge badge-gray">not reading</span>';
    return `<tr>
      <td><span class="tmpl-id">${escapeHtml(index)}</span></td>
      <td><span class="ins-meta">${item.pos_ms ? escapeHtml(formatFullTs(item.pos_ms / 1000)) : 'no events yet'}</span></td>
      <td><span class="ins-meta">${item.floor ? escapeHtml(formatFullTs(item.floor / 1000)) : '—'}</span></td>
      <td>${badge}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="4" class="empty-state">No index is being read yet.</td></tr>';
}
