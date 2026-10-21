// ── Pipeline: live engine and linker status (worker_status) ──
// Stage states, numbers, history and per-service volume all come from /api/pipeline; nothing here recomputes
// a stage's status. Every poll calls setOption on the existing chart instances (chartFor returns them).
const pl = {data: null, sel: 'llm', svc: null, showAll: false, sort: {key: null, dir: 1}, minis: new Map()};
const PL_MINI_CAP = 6;
const $pl = id => document.getElementById(id);

async function fetchPipeline() {
  try { return await getJson('/api/pipeline', 'Unable to load pipeline stats'); }
  catch (err) { plClear(); throw err; }
}

function plDuration(seconds) {
  if (seconds == null || !isFinite(seconds)) return '—';
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
}
const plPercent = ratio => (ratio == null ? '—' : `${(ratio * 100).toFixed(1)}%`);
const plInt = n => (n == null ? '—' : Math.round(n).toLocaleString());
const plZ = z => (z == null ? 'n/a' : `${z >= 0 ? '+' : ''}${z.toFixed(1)}`);
const plFlush = s => (s == null ? '—' : s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(1)} s`);
const plVsWeek = r => (r.rate == null || !r.last_week ? null : (r.rate / r.last_week - 1) * 100);
const plPct = v => (v == null ? '—' : `${v >= 0 ? '+' : ''}${Math.round(v)} %`);
const plHm = t => new Date(t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
const ST_WORD = {ok: 'OK', warn: 'Degraded', bad: 'Down'};
const ST_ICON = {ok: '●', warn: '▲', bad: '■'};

function plCard(label, value, sub = '', tone = '') {
  return `<div class="stat-card${tone ? ` pl-${tone}` : ''}"><div>
    <div class="stat-label">${escapeHtml(label)}</div>
    <div class="stat-value">${escapeHtml(value)}</div>
    ${sub ? `<div class="pl-sub">${escapeHtml(sub)}</div>` : ''}
  </div></div>`;
}

// ── Stage nodes: text from the numbers, state from data.stages ──
const PL_NODES = [
  {id: 'elasticsearch', name: 'Elasticsearch', x: 7, y: 70}, {id: 'reader', name: 'Reader', x: 24, y: 70},
  {id: 'templates', name: 'Templates', x: 41, y: 70}, {id: 'detection', name: 'Minutes & detection', x: 58, y: 70},
  {id: 'flush', name: 'Postgres flush', x: 75, y: 70}, {id: 'prometheus', name: 'Prometheus', x: 92, y: 70},
  {id: 'llm', name: 'LLM resolver', x: 58, y: 18}, {id: 'linker', name: 'Linker', x: 75, y: 18},
];
const PL_MAIN = ['elasticsearch', 'reader', 'templates', 'detection', 'flush', 'prometheus'];

function plIndices(d) {
  const status = d.status || {};
  return {...Object.fromEntries((d.cursors || []).map(c => [c.index, {pos_ms: c.since_ms, floor: c.floor_ms}])),
          ...(status.indices || {})};
}

function plProfiles(d) {
  const now = Date.now() / 1000;
  return Object.entries((d.linker || {}).llm_profiles || {}).map(([name, p]) => ({name, ...p, down: (p.down_until || 0) > now}));
}

// Headline and second line for each stage.
function plText(d) {
  const s = d.status || {}, l = d.linker, rows = d.services || [];
  const idx = Object.values(plIndices(d));
  const now = Date.now();
  const oldest = idx.length ? Math.max(...idx.map(i => (i.pos_ms ? now - i.pos_ms : 0))) / 1000 : null;
  const sum = k => rows.reduce((n, r) => n + (r[k] || 0), 0);
  const prof = plProfiles(d), down = prof.filter(p => p.down).length;
  const pending = l && l.pending ? (l.pending.templates || 0) + (l.pending.documents || 0) : null;
  return {
    elasticsearch: [`${idx.filter(i => !i.failing).length} / ${idx.length} indices`, oldest == null ? 'no position yet' : `oldest position ${plDuration(oldest)}`],
    reader: [`${(s.events_per_second_15m ?? s.events_per_second ?? 0).toFixed(0)} ev/s`, `cache hit ${plPercent(s.cache_hit_ratio)}`],
    templates: [`${plInt(sum('templates'))} templates`, `+${plInt(sum('new_24h'))} in 24 h · ${plInt(sum('unparsed_1h'))} unparsed`],
    detection: [`lag ${plDuration(s.lag_seconds)}`, `${s.detection_enabled ? 'On' : 'Off'} · ${plInt(sum('not_normal'))} keys not NORMAL`],
    flush: [`flush ${plFlush(s.flush_seconds)}`, `max 15 min ${plFlush(s.flush_seconds_max)}`],
    prometheus: ['metrics export', 'scraped by Prometheus'],
    linker: [pending == null ? 'no report' : `${pending} pending`, !l ? 'automatic links off' : l.links_changed ? 'links changed last cycle' : 'no link changes last cycle'],
    llm: [!l ? 'not running' : `${l.llm_today ?? 0} today`, `${prof.length - down} profiles up, ${down} down`],
  };
}

function plTopologyOption(d, t) {
  const text = plText(d), stages = d.stages || {};
  const pos = Object.fromEntries(PL_NODES.map(n => [n.id, [n.x, n.y]]));
  const eps = (d.status || {}).events_per_second_15m ?? (d.status || {}).events_per_second ?? 0;
  const live = !REDUCED_MOTION && !d.stale && eps > 0;
  // Stepped so a small change in rate does not restart the particle animation every poll.
  const period = eps >= 500 ? 1.5 : eps >= 50 ? 2.5 : 4;
  const width = 1.5 + Math.min(3, Math.log10(eps + 1));
  const stage = id => stages[id] || {status: 'warn', reason: null};
  const name = id => PL_NODES.find(n => n.id === id).name;
  const col = id => t.status[stage(id).status];
  return {
    ...baseOption(),
    grid: {left: 0, right: 0, top: 0, bottom: 0},
    xAxis: {type: 'value', min: 0, max: 100, show: false}, yAxis: {type: 'value', min: 0, max: 100, show: false},
    tooltip: {...baseOption().tooltip, formatter: p => {
      if (p.dataType !== 'node') return '';
      const n = PL_NODES[p.dataIndex], st = stage(n.id), [h, sub] = text[n.id];
      return `<b>${escapeHtml(n.name)}</b> · ${ST_ICON[st.status]} ${ST_WORD[st.status]}<br>${escapeHtml(h)}<br>${escapeHtml(sub)}${st.reason ? `<br><i>${escapeHtml(st.reason)}</i>` : ''}`;
    }},
    series: [
      {type: 'lines', coordinateSystem: 'cartesian2d', z: 1, silent: true, lineStyle: {width: 0},
       data: PL_MAIN.slice(0, -1).map((a, i) => ({coords: [pos[a], pos[PL_MAIN[i + 1]]]})),
       effect: {show: live, period, trailLength: 0, symbol: 'circle', symbolSize: 5, color: t.accent}},
      {type: 'graph', coordinateSystem: 'cartesian2d', z: 2,
       data: PL_NODES.map(n => ({name: n.name, value: [n.x, n.y], symbol: 'roundRect', symbolSize: [150, 82],
         itemStyle: {color: t.cls.none, borderColor: col(n.id), borderWidth: 2},
         label: {show: true, formatter: [`{n|${n.name}}`, `{h|${text[n.id][0]}}`, `{s|${text[n.id][1]}}`, `{st|${ST_ICON[stage(n.id).status]} ${ST_WORD[stage(n.id).status]}}`].join('\n'),
           rich: {n: {color: t.dim, fontSize: 11.5, fontWeight: 600, lineHeight: 16}, h: {color: t.text, fontSize: 16, fontWeight: 700, lineHeight: 22},
                  s: {color: t.muted, fontSize: 11, lineHeight: 15}, st: {color: col(n.id), fontSize: 11, fontWeight: 700, lineHeight: 16}}}})),
       links: [
         ...PL_MAIN.slice(0, -1).map((a, i) => ({source: name(a), target: name(PL_MAIN[i + 1]), lineStyle: {width}})),
         {source: name('linker'), target: name('flush'), lineStyle: {type: 'dashed'}, label: {show: true, formatter: 'via Postgres', color: t.muted, fontSize: 11}},
         {source: name('linker'), target: name('llm')},
       ],
       edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 9,
       lineStyle: {color: t.muted, width: 2, opacity: 0.8}, emphasis: {focus: 'none', scale: false}},
    ],
  };
}

function plRenderTopology(d) {
  const t = chartTokens(), el = $pl('pl-topology');
  const fresh = !echarts.getInstanceByDom(el);
  const c = chartFor(el);
  c.setOption(plTopologyOption(d, t));
  if (fresh) c.on('click', p => {
    if (p.seriesType === 'graph' && p.dataType === 'node') { pl.sel = PL_NODES[p.dataIndex].id; plRenderStage(pl.data); }
  });
}

// ── Stage details under the topology ──
function plIndexTable(d) {
  const idx = plIndices(d), names = Object.keys(idx).sort();
  return names.length ? `<div class="table-wrap"><table><thead><tr><th>Index</th><th>Position</th><th>Reads from</th><th>Status</th></tr></thead><tbody>${plIndexRows(d)}</tbody></table></div>`
    : '<p>No index is being read yet.</p>';
}
function plIndexRows(d) {
  const idx = plIndices(d);
  return Object.keys(idx).sort().map(index => {
    const item = idx[index];
    const badge = item.failing ? '<span class="badge badge-red">failing</span>'
      : item.caught_up ? '<span class="badge badge-green">caught up</span>'
      : item.caught_up === false ? '<span class="badge badge-yellow">catching up</span>'
      : '<span class="badge badge-gray">not reading</span>';
    return `<tr><td><span class="tmpl-id">${escapeHtml(index)}</span></td>
      <td><span class="ins-meta">${item.pos_ms ? escapeHtml(formatFullTs(item.pos_ms / 1000)) : 'no events yet'}</span></td>
      <td><span class="ins-meta">${item.floor ? escapeHtml(formatFullTs(item.floor / 1000)) : '—'}</span></td><td>${badge}</td></tr>`;
  }).join('');
}
function plServiceRows(d) {
  const rows = (d.services || []).filter(r => !pl.svc || r.service === pl.svc);
  return `<div class="table-wrap"><table><thead><tr><th>Service</th><th class="r">Miss ratio 5 min</th><th class="r">Templates</th><th class="r">New 24 h</th><th class="r">Unparsed 1 h</th><th class="r">Keys not NORMAL</th></tr></thead><tbody>${
    rows.map(r => `<tr><td><span class="tmpl-id">${escapeHtml(r.service)}</span></td><td class="r">${plPercent(r.miss_ratio)}</td><td class="r">${plInt(r.templates)}</td><td class="r">${plInt(r.new_24h)}</td><td class="r">${plInt(r.unparsed_1h)}</td><td class="r">${plInt(r.not_normal)}</td></tr>`).join('')
      || '<tr><td colspan="6" class="empty-state">No service.</td></tr>'}</tbody></table></div>`;
}

function plStageBody(id, d) {
  const s = d.status || {}, l = d.linker;
  switch (id) {
    case 'elasticsearch': return plIndexTable(d);
    case 'reader': case 'templates': return (pl.svc ? `<p>Filtered to ${escapeHtml(pl.svc)}. Click the same row again to clear.</p>` : '') + plServiceRows(d);
    case 'detection': return `<p>Detection ${s.detection_enabled ? 'On: alerts are raised.' : 'Off: catching up, counts only.'} Lag ${escapeHtml(plDuration(s.lag_seconds))}. Bootstrap ${s.bootstrap && s.bootstrap.since ? escapeHtml(formatFullTs(s.bootstrap.since)) : '—'}.</p>`;
    case 'flush': {
      const c = s.last_commit;
      return `<p>Last flush ${plFlush(s.flush_seconds)}, slowest of the last 15 min ${plFlush(s.flush_seconds_max)}. Late counts since start: ${escapeHtml(JSON.stringify(s.late_counts ?? 0))}.</p>
        <p>Last commit ${c ? `${escapeHtml(formatFullTs(Date.parse(c.at) / 1000))} (baseline day ${escapeHtml(c.today)})` : 'never'}.</p>`;
    }
    case 'prometheus': return '<p>The engine exports its metrics for Prometheus to scrape.</p>';
    case 'linker': {
      if (!l) return '<p>The linker has not reported: automatic links are off.</p>';
      const p = l.pending || {};
      return `<p>Pending: ${plInt(p.templates || 0)} templates, ${plInt(p.documents || 0)} documents. Last report ${escapeHtml(formatTs(l.heartbeat))}.</p>
        <p>${l.links_changed ? 'Links changed last cycle.' : 'No link changes last cycle.'} ${l.last_error ? `Last error: ${escapeHtml(l.last_error)}` : 'No error.'}</p>`;
    }
    case 'llm': {
      const prof = plProfiles(d);
      if (!l) return '<p>The linker is not running, so the LLM resolver is idle.</p>';
      return `<p>${plInt(l.llm_today ?? 0)} answers today.</p>` + (prof.length ? `<div class="table-wrap"><table><thead><tr><th>Profile</th><th>State</th><th class="r">Answers</th></tr></thead><tbody>${
        prof.map(p => `<tr><td><span class="tmpl-id">${escapeHtml(p.name)}</span></td><td>${p.down
          ? `<span class="badge badge-yellow">down until ${escapeHtml(formatFullTs(p.down_until))}</span> ${escapeHtml(p.error || '')}`
          : '<span class="badge badge-green">up</span>'}</td><td class="r">${plInt(p.answers)}</td></tr>`).join('')}</tbody></table></div>` : '<p>No LLM profile is configured.</p>');
    }
  }
  return '';
}

function plRenderStage(d) {
  const n = PL_NODES.find(x => x.id === pl.sel) || PL_NODES[0];
  const st = (d.stages || {})[n.id] || {status: 'warn', reason: null};
  $pl('pl-stage').innerHTML = `<h4>${escapeHtml(n.name)} <span class="pl-state pl-state-${st.status}">${ST_ICON[st.status]} ${ST_WORD[st.status]}</span></h4>
    <p>${st.reason ? escapeHtml(st.reason) : 'No problem reported.'}</p>${plStageBody(n.id, d)}`;
}

// ── Lag and events/s ──
function plLineOption(hist, key, unit, t, mark) {
  return {
    ...baseOption(), grid: {left: 52, right: 14, top: 26, bottom: 24},
    xAxis: {type: 'category', data: hist.map(p => plHm(p.t)), boundaryGap: false, axisLine: {lineStyle: {color: t.grid}},
            axisLabel: {color: t.dim, fontSize: 11, interval: Math.max(0, Math.floor(hist.length / 5) - 1)}},
    yAxis: {type: 'value', min: 0, splitLine: {lineStyle: {color: t.grid}}, axisLabel: {color: t.dim, fontSize: 11, formatter: v => v + unit}},
    tooltip: {...baseOption().tooltip, trigger: 'axis', valueFormatter: v => (v == null ? '—' : `${Math.round(v * 10) / 10}${unit}`)},
    series: [{type: 'line', data: hist.map(p => p[key]), symbol: 'none', lineStyle: {width: 2, color: t.accent},
              markLine: mark ? {silent: true, symbol: 'none', lineStyle: {type: 'dashed', color: t.status.warn}, label: {color: t.status.warn, formatter: '5 min', position: 'insideEndTop'}, data: [{yAxis: 300}]} : undefined}],
  };
}
function plRenderHistory(d) {
  const t = chartTokens(), hist = (d.status || {}).history || [];
  chartFor($pl('pl-lag')).setOption(plLineOption(hist, 'lag', ' s', t, true));
  chartFor($pl('pl-eps')).setOption(plLineOption(hist, 'eps', '/s', t, false));
  $pl('pl-lag-note').textContent = hist.length ? 'One point per minute from the engine. Dashed line: 5 min, where lag turns Degraded.'
    : 'No history yet: the engine records one point per minute.';
}

function plRenderTiles(d) {
  const s = d.status, boot = s.bootstrap || {};
  const late = s.late_counts;
  const lateN = late == null ? 0 : typeof late === 'object' ? Object.values(late).reduce((a, b) => a + (+b || 0), 0) : +late;
  const lag = s.lag_seconds;
  $pl('pl-tiles').innerHTML = [
    plCard('Lag now', plDuration(lag), 'behind the newest log read', lag > 300 ? 'warn' : ''),
    plCard('Events/s', `${Number(s.events_per_second_15m ?? s.events_per_second ?? 0).toFixed(1)}/s`, '15-min average'),
    plCard('Cache hit ratio', plPercent(s.cache_hit_ratio), 'lines already templated'),
    plCard('Late counts', plInt(lateN), 'since start; should stay 0', lateN ? 'warn' : ''),
    plCard('Detection', s.detection_enabled ? 'On' : 'Off', s.detection_enabled ? 'alerts are raised' : 'catching up; counts only', s.detection_enabled ? '' : 'warn'),
    plCard('Bootstrap', boot.since ? formatFullTs(boot.since) : '—', boot.until ? `first start read until ${formatFullTs(boot.until)}` : ''),
  ].join('');
}

// ── Volume small multiples ──
function plMiniOption(v, row, t) {
  const hrs = v.hours, hh = hrs.map(h => String(h.hour).padStart(2, '0'));
  const out = hrs.map(h => (h.rate != null && h.band_high != null && (h.rate > h.band_high || h.rate < h.band_low)) ? h.rate : null);
  return {
    ...baseOption(), grid: {left: 44, right: 8, top: 8, bottom: 20},
    xAxis: {type: 'category', data: hh, boundaryGap: false, axisLine: {lineStyle: {color: t.grid}}, axisLabel: {color: t.dim, fontSize: 10, interval: 5}},
    yAxis: {type: 'value', splitNumber: 3, splitLine: {lineStyle: {color: t.grid}}, axisLabel: {color: t.dim, fontSize: 10}},
    tooltip: {...baseOption().tooltip, trigger: 'axis', formatter: ps => {
      const h = hrs[ps[0].dataIndex];
      return `<b>${escapeHtml(v.service)} ${hh[ps[0].dataIndex]}:00</b><br>today ${plInt(h.rate)}/min<br>expected ${plInt(h.band_low)}–${plInt(h.band_high)}<br>last week ${plInt(h.last_week)}`;
    }},
    series: [
      {type: 'line', data: hrs.map(h => h.band_low), stack: 'b', symbol: 'none', lineStyle: {opacity: 0}, silent: true},
      {type: 'line', data: hrs.map(h => (h.band_high != null && h.band_low != null ? h.band_high - h.band_low : null)), stack: 'b', symbol: 'none', lineStyle: {opacity: 0}, areaStyle: {color: t.band}, silent: true},
      {type: 'line', data: hrs.map(h => h.last_week), symbol: 'none', lineStyle: {type: 'dashed', width: 1.2, color: t.lastweek}},
      {type: 'line', data: hrs.map(h => h.rate), symbol: 'none', lineStyle: {width: 2, color: t.line}},
      {type: 'scatter', data: out, symbolSize: 8, itemStyle: {color: t.cls.up2}},
    ],
  };
}
function plMiniTitle(row, v) {
  const last = v.hours[v.hours.length - 1] || {};
  const w = plVsWeek(row || {});
  const state = last.rate == null || last.band_high == null ? '' : last.rate > last.band_high ? '▲ above expected' : last.rate < last.band_low ? '▼ below expected' : '';
  return `<b>${escapeHtml(v.service)}</b><span>now ${plInt(row && row.rate)}/min · expected ${plInt(last.band_low)}–${plInt(last.band_high)} · ${w == null ? 'no last week' : `${plPct(w)} vs last week`}${state ? ` · ${state}` : ''}</span>`;
}
function plDisposeMini(el) {
  _chartObservers.get(el)?.disconnect(); _chartObservers.delete(el);
  echarts.getInstanceByDom(el)?.dispose();
}
function plRenderMultiples(d) {
  const t = chartTokens(), vols = d.volume || [];
  const order = (d.services || []).map(r => r.service);
  const rank = s => { const i = order.indexOf(s); return i < 0 ? order.length : i; };
  const sorted = [...vols].sort((a, b) => rank(a.service) - rank(b.service));
  const shown = pl.showAll ? sorted : sorted.slice(0, PL_MINI_CAP);
  const host = $pl('pl-multiples');
  $pl('pl-multiples-empty').hidden = vols.length > 0;
  const more = $pl('pl-multiples-more');
  more.hidden = vols.length <= PL_MINI_CAP;
  more.textContent = pl.showAll ? `Show worst ${PL_MINI_CAP}` : `Show all ${vols.length}`;
  const want = new Set(shown.map(v => v.service));
  for (const [svc, el] of pl.minis) if (!want.has(svc)) { plDisposeMini(el.querySelector('.pl-mini-chart')); el.remove(); pl.minis.delete(svc); }
  for (const v of shown) {
    let card = pl.minis.get(v.service);
    if (!card) {
      card = document.createElement('div');
      card.className = 'pl-mini'; card.tabIndex = 0; card.setAttribute('role', 'link');
      card.innerHTML = '<div class="pl-mini-title"></div><div class="pl-mini-chart" role="img"></div>';
      const go = () => { location.hash = `#baseline/${encodeURIComponent(v.service)}/*total/1`; };
      card.addEventListener('click', go);
      card.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
      pl.minis.set(v.service, card);
    }
    card.setAttribute('aria-label', `${v.service}: open Baseline for today`);
    card.classList.toggle('sel', pl.svc === v.service);
    card.querySelector('.pl-mini-title').innerHTML = plMiniTitle((d.services || []).find(r => r.service === v.service), v);
    chartFor(card.querySelector('.pl-mini-chart'), 'svg').setOption(plMiniOption(v, null, t));
  }
  // Keep worst-first order in the DOM without recreating the charts.
  const cards = shown.map(v => pl.minis.get(v.service));
  if (cards.some((c, i) => host.children[i] !== c)) host.append(...cards);
}

// ── Services table ──
function plRenderServices(d) {
  const {key, dir} = pl.sort;
  let rows = (d.services || []).map(r => ({...r, vs_week: plVsWeek(r)}));
  if (key) rows.sort((a, b) => {
    const x = a[key], y = b[key];
    if (x == null || y == null) return (x == null) - (y == null);
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
  });
  document.querySelectorAll('#pl-services th').forEach(th => {
    const b = th.querySelector('.pl-sort');
    th.setAttribute('aria-sort', b && b.dataset.sort === key ? (dir > 0 ? 'ascending' : 'descending') : 'none');
  });
  $pl('pl-services-tbody').innerHTML = rows.length ? rows.map(r => {
    const out = r.z != null && Math.abs(r.z) > 3;
    return `<tr data-svc="${escapeHtml(r.service)}" tabindex="0" class="${pl.svc === r.service ? 'sel' : ''}">
      <td><span class="tmpl-id">${escapeHtml(r.service)}</span></td><td class="r">${plInt(r.rate)}</td><td class="r">${plPct(r.vs_week)}</td>
      <td class="r">${out ? `<span class="badge ${r.z < 0 ? 'badge-red' : 'badge-yellow'}">${r.z < 0 ? '▼' : '▲'} ${plZ(r.z)}</span>` : plZ(r.z)}</td>
      <td class="r">${plPercent(r.miss_ratio)}</td><td class="r">${plInt(r.templates)}</td><td class="r">${plInt(r.new_24h)}</td>
      <td class="r">${plInt(r.unparsed_1h)}</td><td class="r">${plInt(r.not_normal)}</td></tr>`;
  }).join('') : '<tr><td colspan="9" class="empty-state">No service yet.</td></tr>';
}

function plSelectService(svc) {
  pl.svc = pl.svc === svc ? null : svc;
  if (pl.svc && !['reader', 'templates'].includes(pl.sel)) pl.sel = 'reader';
  if (pl.svc && !pl.minis.has(pl.svc)) pl.showAll = true;
  plRenderAll(pl.data);
}
$pl('pl-services-tbody').addEventListener('click', e => {
  const tr = e.target.closest('tr[data-svc]'); if (tr) plSelectService(tr.dataset.svc);
});
$pl('pl-services-tbody').addEventListener('keydown', e => {
  const tr = e.target.closest('tr[data-svc]'); if (tr && e.key === 'Enter') plSelectService(tr.dataset.svc);
});
document.querySelector('#pl-services thead').addEventListener('click', e => {
  const key = e.target.closest('.pl-sort')?.dataset.sort; if (!key) return;
  pl.sort = {key, dir: pl.sort.key === key ? -pl.sort.dir : 1};
  plRenderServices(pl.data);
});
$pl('pl-multiples-more').addEventListener('click', () => { pl.showAll = !pl.showAll; plRenderMultiples(pl.data); });

function plRenderIndices(d) {
  const rows = plIndexRows(d);
  $pl('pl-indices-tbody').innerHTML = rows || '<tr><td colspan="4" class="empty-state">No index is being read yet.</td></tr>';
}

function plRenderAll(d) {
  plRenderTopology(d);
  plRenderStage(d);
  plRenderHistory(d);
  plRenderTiles(d);
  plRenderMultiples(d);
  plRenderServices(d);
  plRenderIndices(d);
}

// A failed or empty load must not leave the previous numbers on screen.
function plClear() {
  pl.data = null;
  ['pl-topology', 'pl-lag', 'pl-eps'].forEach(id => echarts.getInstanceByDom($pl(id))?.clear());
  for (const [, card] of pl.minis) plDisposeMini(card.querySelector('.pl-mini-chart'));
  pl.minis.clear();
  $pl('pl-multiples').innerHTML = '';
  $pl('pl-stage').innerHTML = $pl('pl-tiles').innerHTML = $pl('pl-warnings').innerHTML = '';
  $pl('pl-services-tbody').innerHTML = $pl('pl-indices-tbody').innerHTML = '';
}

function renderPipeline(d) {
  if (state.activeView !== 'pipeline') return;   // a poll that outlived a route change
  const {status, stale} = d;
  const $status = $pl('pl-status');
  $status.className = 'sync-status' + (stale ? ' llm-status-error' : '');
  document.getElementById('view-pipeline').classList.toggle('pl-stale', Boolean(stale));
  if (!status) {
    plClear();
    $status.textContent = 'The engine has not reported yet (no heartbeat).';
    return;
  }
  const linkerNote = d.linker ? ` · linker report ${formatTs(d.linker.heartbeat)}` : ' · linker has not reported';
  $status.textContent = stale
    ? `The engine is not running (last heartbeat ${formatTs(status.heartbeat)}). Showing its last numbers.${linkerNote}`
    : `Engine running · heartbeat ${formatTs(status.heartbeat)}${linkerNote}`;
  $pl('pl-warnings').innerHTML = (status.warnings || []).map(w => `<p class="ins-toast ins-toast-error">${escapeHtml(w)}</p>`).join('');
  pl.data = d;
  plRenderAll(d);
}
