// ── Pipeline (live engine performance from the heartbeat) ──
// Counters are totals since the engine started; rates come from the change
// between two heartbeats (every ~5 s). The sparkline lives only in this tab.
const pl = {samples: [], startedAt: null, lastBeat: null};
const PL_MAX_SAMPLES = 60;

async function fetchPipeline() {
  // /api/health answers 503 while the engine is down; the last runtime is still useful.
  const [health, sources] = await Promise.all([
    fetch('/api/health').then(resp => resp.json()),
    fetchSources().catch(() => null),
  ]);
  return {health, sources};
}

function plDuration(seconds) {
  if (seconds == null || !isFinite(seconds)) return '—';
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
}

const plNum = n => Number(n || 0).toLocaleString();

function plCard(label, value, sub = '', tone = '') {
  return `<div class="stat-card${tone ? ` pl-${tone}` : ''}"><div>
    <div class="stat-label">${escapeHtml(label)}</div>
    <div class="stat-value">${escapeHtml(value)}</div>
    ${sub ? `<div class="pl-sub">${escapeHtml(sub)}</div>` : ''}
  </div></div>`;
}

function plRecordSample(p, beat) {
  if (p.started_at !== pl.startedAt) {  // engine restarted: counters reset
    pl.samples = [];
    pl.startedAt = p.started_at;
  }
  if (!beat || beat === pl.lastBeat) return;
  pl.lastBeat = beat;
  const prev = pl.samples[pl.samples.length - 1];
  const sample = {t: beat, processed: p.events_processed || 0, received: p.events_received || 0, rate: null};
  if (prev && beat > prev.t) sample.rate = Math.max(0, (sample.processed - prev.processed) / (beat - prev.t));
  pl.samples.push(sample);
  if (pl.samples.length > PL_MAX_SAMPLES) pl.samples.shift();
}

function plSparkline() {
  const rates = pl.samples.map(s => s.rate).filter(r => r != null);
  const $svg = document.getElementById('pl-spark');
  const $caption = document.getElementById('pl-spark-caption');
  if (rates.length < 2) {
    $svg.innerHTML = '';
    $caption.textContent = 'Collecting samples… (one every heartbeat, ~5 s)';
    return;
  }
  const max = Math.max(...rates, 1);
  const step = 100 / (PL_MAX_SAMPLES - 1);
  const offset = 100 - (rates.length - 1) * step;
  const points = rates.map((r, i) => `${(offset + i * step).toFixed(2)},${(38 - r / max * 34).toFixed(2)}`).join(' ');
  $svg.innerHTML = `<polyline points="${points}" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
  $caption.textContent = `Processed logs/s, last ${rates.length} heartbeats · peak ${max.toFixed(1)}/s`;
}

function renderPipeline({health, sources}) {
  const now = Date.now() / 1000;
  const p = health.pipeline;
  const $status = document.getElementById('pl-status');
  const alive = health.engine_alive;
  $status.className = 'sync-status' + (!alive || (p && p.es_error) ? ' llm-status-error' : '');
  if (!p) {
    $status.textContent = alive
      ? 'The engine is running an older version without pipeline stats; restart it to see them.'
      : 'The engine is not running (no recent heartbeat).';
    return;
  }
  $status.textContent = !alive
    ? `The engine is not running (last heartbeat ${formatTs(health.last_heartbeat_at)}). Showing its last numbers.`
    : p.es_error
      ? `Cannot reach Elasticsearch since ${formatFullTs(p.es_error_since)}: ${p.es_error}`
      : `Engine ${health.state} · counters since start ${formatFullTs(p.started_at)} · heartbeat ${formatTs(health.last_heartbeat_at)}`;

  if (alive) plRecordSample(p, health.last_heartbeat_at);
  const last = pl.samples[pl.samples.length - 1];
  const rate = last && last.rate != null ? `${last.rate.toFixed(1)}/s` : '—';
  const avgLatency = p.latency_count ? `${(p.latency_sum / p.latency_count * 1000).toFixed(2)} ms` : '—';
  const backlog = Math.max(0, (p.events_received || 0) - (p.events_processed || 0) - (p.events_failed || 0));

  document.getElementById('pl-throughput').innerHTML = [
    plCard('Uptime', plDuration(now - p.started_at), `started ${formatFullTs(p.started_at)}`),
    plCard('Logs collected', plNum(p.events_received), 'read from Elasticsearch'),
    plCard('Logs processed', plNum(p.events_processed), `${plNum(backlog)} skipped or pending`),
    plCard('Throughput', rate, 'processed, last heartbeat'),
    plCard('Avg latency', avgLatency, 'per event, parse → features'),
    plCard('Current batch', plNum(p.batch_size), `${plNum(p.pending_predictions)} awaiting scoring`),
    plCard('Last log processed', formatTs(health.last_processed_event_at), 'by event time'),
    plCard('Last checkpoint', formatTs(health.last_checkpoint_commit_at), `last poll ${formatTs(health.last_successful_poll_at)}`),
  ].join('');
  plSparkline();

  const retries = p.llm_retries || {};
  const otherRetries = Object.entries(retries).filter(([k]) => k !== 'timeout').reduce((sum, [, v]) => sum + v, 0);
  const tone = n => (n > 0 ? 'warn' : '');
  document.getElementById('pl-errors').innerHTML = [
    plCard('Failed (DLQ)', plNum(p.events_failed), `${plNum(p.dlq_records)} written to dlq.jsonl`, p.events_failed > 0 ? 'bad' : ''),
    plCard('ES poll errors', plNum(p.es_poll_errors), 'after all retries', tone(p.es_poll_errors)),
    plCard('ES retries', plNum(p.es_retries), 'timeouts, connection errors', tone(p.es_retries)),
    plCard('Malformed hits', plNum(p.malformed_hits), 'skipped: no _source or _id', tone(p.malformed_hits)),
    plCard('LLM timeouts', plNum(retries.timeout), 'read timeouts retried', tone(retries.timeout)),
    plCard('LLM other retries', plNum(otherRetries), 'cut-off or non-JSON replies', tone(otherRetries)),
    plCard('LLM analyses', plNum(p.llm_requests), `LLM ${health.llm_status || 'unknown'}`),
  ].join('');

  const progress = (sources && sources.indices) || {};
  const names = Object.keys(progress).sort();
  document.getElementById('pl-indices-tbody').innerHTML = names.length ? names.map(index => {
    const item = progress[index];
    const badge = item.error
      ? `<span class="badge badge-red" title="${escapeHtml(item.error)}">error</span>`
      : '<span class="badge badge-green">ok</span>';
    return `<tr>
      <td><span class="tmpl-id">${escapeHtml(index)}</span></td>
      <td>${plNum(item.events_total)}</td>
      <td><span class="ins-meta">${item.last_event_ts ? escapeHtml(formatTs(item.last_event_ts)) : 'no events yet'}</span></td>
      <td>${badge}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="4" class="empty-state">No index is being read yet.</td></tr>';
}
