// ── Pipeline: live engine and linker status (worker_status) ──
// Stage states, numbers, history and per-service throughput all come from /api/pipeline; nothing here recomputes
// a stage's status. Every poll calls setOption on the existing chart instances (chartFor returns them).
const pl = {data: null, sel: 'llm', svc: null, sort: {key: null, dir: 1}, cards: new Map(), routeSub: null,
            pick: null, mode: 'combined', filter: ''};   // pick: chosen services, null until chosen (then the 5 busiest)
const PL_MAX = 8, PL_DEFAULT = 5;
const PL_COLORS = ['#4f8ef7', '#f59e0b', '#10b981', '#ef4444', '#a855f7', '#14b8a6', '#ec4899', '#84cc16'];
const plStore = {   // localStorage can be blocked or full: the page works without it
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* kept for this visit only */ } },
};
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
const plFlush = s => (s == null ? '—' : s < 1 ? `${Math.round(s * 1000)} ms` : `${s.toFixed(1)} s`);
const plPct = v => (v == null ? '—' : `${v >= 0 ? '+' : ''}${Math.round(v)} %`);
const plHm = t => new Date(t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false});
const ST_WORD = {ok: 'OK', warn: 'Degraded', bad: 'Down'};
const ST_ICON = {ok: '●', warn: '▲', bad: '■'};

// Rewrite only when the HTML changed, and keep keyboard focus on the same row.
function plSetHtml(el, html) {
  if (el._plHtml === html) return;
  const svc = document.activeElement?.closest?.('tr[data-svc]')?.dataset.svc;
  el._plHtml = html; el.innerHTML = html;
  if (svc) [...el.querySelectorAll('tr[data-svc]')].find(r => r.dataset.svc === svc)?.focus();
}

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
    llm: [!l ? 'not running' : `${l.llm_today ?? 0}${d.llm_daily_max ? ` / ${d.llm_daily_max}` : ''} in 24 h`, `${prof.length - down} profiles up, ${down} down`],
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
         {source: name('linker'), target: name('flush'), lineStyle: {type: 'dashed'}, label: {show: true, formatter: 'via Postgres', color: t.muted, fontSize: 11, rotate: 0, position: 'middle', offset: [34, 0]}},
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
      return `<p>Pending: ${plInt(p.templates || 0)} templates, ${plInt(p.documents || 0)} documents.</p>
        <p>${l.links_changed ? 'Links changed last cycle.' : 'No link changes last cycle.'} ${l.last_error ? `Last error: ${escapeHtml(l.last_error)}` : 'No error.'}</p>`;
    }
    case 'llm': {
      const prof = plProfiles(d);
      if (!l) return '<p>The linker is not running, so the LLM resolver is idle.</p>';
      return `<p>${d.llm_daily_max ? `${plInt(l.llm_today ?? 0)} of ${plInt(d.llm_daily_max)} LLM calls counted in the last 24 h (rolling; includes calls that wrote nothing).`
        : `${plInt(l.llm_today ?? 0)} LLM calls in the last 24 h (rolling; includes calls that wrote nothing), no daily limit.`}</p>` + (prof.length ? `<div class="table-wrap"><table><thead><tr><th>Profile</th><th>State</th><th class="r">Answers</th></tr></thead><tbody>${
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
  plSetHtml($pl('pl-stage'), `<h4>${escapeHtml(n.name)} <span class="pl-state pl-state-${st.status}">${ST_ICON[st.status]} ${ST_WORD[st.status]}</span></h4>
    <p>${st.reason ? escapeHtml(st.reason) : 'No problem reported.'}</p>${plStageBody(n.id, d)}`);
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

// ── Throughput: the services ticked in the table, last 60 closed minutes ──
function plBusiest(d) {
  return [...(d.services || [])].filter(r => r.avg15 != null).sort((a, b) => b.avg15 - a.avg15 || a.service.localeCompare(b.service))
    .slice(0, PL_DEFAULT).map(r => r.service);
}
function plChosen(d) {
  const known = new Set(Object.keys(d.throughput || {}));
  return (pl.pick || plBusiest(d)).filter(s => known.has(s));
}
// Selection comes from the URL (#pipeline?services=a,b) when the route changes, else from localStorage.
function plApplyRoute() {
  const sub = state.subView || '';
  if (sub === pl.routeSub) return;
  pl.routeSub = sub;
  let list = new URLSearchParams(sub.startsWith('?') ? sub : '').get('services');
  if (list == null) list = plStore.get('pl.services');
  pl.pick = list ? list.split(',').filter(Boolean).map(decodeURIComponent).slice(0, PL_MAX) : null;
  pl.mode = plStore.get('pl.mode') === 'multiples' ? 'multiples' : 'combined';
}
function plSavePick() {
  const list = pl.pick.map(encodeURIComponent).join(',');
  plStore.set('pl.services', list);
  pl.routeSub = `?services=${list}`;
  history.replaceState(null, '', `#pipeline${pl.routeSub}`);
  state.subView = pl.routeSub;
}

function plTpOption(d, names, t, single) {
  const tp = d.throughput || {};
  return {
    ...baseOption(), grid: {left: 44, right: 12, top: single ? 8 : 28, bottom: 24},
    legend: single ? undefined : {top: 0, textStyle: {color: t.dim, fontSize: 11}},
    xAxis: {type: 'time', axisLine: {lineStyle: {color: t.grid}}, axisLabel: {color: t.dim, fontSize: 10, hideOverlap: true}},
    yAxis: {type: 'value', min: 0, splitNumber: 3, splitLine: {lineStyle: {color: t.grid}}, axisLabel: {color: t.dim, fontSize: 10}},
    tooltip: {...baseOption().tooltip, trigger: 'axis', valueFormatter: v => `${plInt(v)}/min`},
    series: names.map(n => ({name: n, type: 'line', symbol: 'none', lineStyle: {width: 2},
      color: single ? t.line : PL_COLORS[Math.max(0, (pl.pick || []).indexOf(n)) % PL_COLORS.length],
      data: tp[n].points.map(([ts, v]) => [ts * 1000, v])})),
  };
}
function plDisposeCard(el) {
  const chart = el.querySelector('.pl-tp-chart');
  _chartObservers.get(chart)?.disconnect(); _chartObservers.delete(chart);
  echarts.getInstanceByDom(chart)?.dispose();
  el.remove();
}
function plRenderThroughput(d) {
  const t = chartTokens(), names = plChosen(d), host = $pl('pl-tp'), multi = pl.mode === 'multiples';
  document.querySelectorAll('#pl-tp-mode .toggle-btn').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === pl.mode)));
  $pl('pl-tp-empty').hidden = names.length > 0;
  $pl('pl-tp-empty').textContent = Object.keys(d.throughput || {}).length
    ? `No service ticked. Tick up to ${PL_MAX} services in the table below.`
    : 'No throughput yet: the engine reports it after its first closed minute.';
  host.classList.toggle('combined', !multi);
  const keys = !names.length ? [] : multi ? names : ['*'];
  for (const [k, el] of pl.cards) if (!keys.includes(k)) { plDisposeCard(el); pl.cards.delete(k); }
  for (const k of keys) {
    let card = pl.cards.get(k);
    if (!card) {
      card = document.createElement('div');
      card.className = multi ? 'pl-mini' : '';
      card.innerHTML = `${multi ? '<div class="pl-mini-title"></div>' : ''}<div class="pl-tp-chart" role="img"></div>`;
      pl.cards.set(k, card);
    }
    if (multi) {
      const tp = d.throughput[k];
      card.querySelector('.pl-mini-title').innerHTML = `<b>${escapeHtml(k)}</b><span>last ${plInt(tp.last)}/min · avg ${plInt(tp.avg15)}</span>`;
    }
    const chart = card.querySelector('.pl-tp-chart');
    chart.setAttribute('aria-label', multi ? `${k}: events per minute, last 60 minutes` : 'Events per minute per service, last 60 minutes');
    chartFor(chart).setOption(plTpOption(d, multi ? [k] : names, t, multi), {notMerge: true});
  }
  const cards = keys.map(k => pl.cards.get(k));
  if (cards.some((c, i) => host.children[i] !== c)) host.append(...cards);
}

// Inline sparkline: one polyline scaled to the service's own maximum.
function plSpark(pts) {
  if (!pts || pts.length < 2) return '';
  const max = Math.max(...pts.map(p => p[1])) || 1, t0 = pts[0][0], span = pts[pts.length - 1][0] - t0 || 1;
  const xy = pts.map(([ts, n]) => `${((ts - t0) / span * 96).toFixed(1)},${(19 - n / max * 18).toFixed(1)}`).join(' ');
  return `<svg class="pl-spark" viewBox="0 0 96 20" preserveAspectRatio="none" aria-hidden="true"><polyline points="${xy}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>`;
}
const plDelta = r => (r.delta_1h == null ? '—' : plPct(r.delta_1h * 100));

// ── Services table ──
function plRenderServices(d) {
  const {key, dir} = pl.sort, chosen = new Set(plChosen(d)), q = pl.filter.trim().toLowerCase();
  const rows = (d.services || []).filter(r => !q || r.service.toLowerCase().includes(q));
  if (key) rows.sort((a, b) => {
    const x = a[key], y = b[key];
    if (x == null || y == null) return (x == null) - (y == null);
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * dir;
  });
  document.querySelectorAll('#pl-services th').forEach(th => {
    const b = th.querySelector('.pl-sort');
    th.setAttribute('aria-sort', b && b.dataset.sort === key ? (dir > 0 ? 'ascending' : 'descending') : 'none');
  });
  const tp = d.throughput || {};
  plSetHtml($pl('pl-services-tbody'), rows.length ? rows.map(r => `<tr data-svc="${escapeHtml(r.service)}" tabindex="0" class="${pl.svc === r.service ? 'sel' : ''}">
      <td><input type="checkbox" data-pick="${escapeHtml(r.service)}" aria-label="Show ${escapeHtml(r.service)} in the chart"${chosen.has(r.service) ? ' checked' : ''}${tp[r.service] ? '' : ' disabled'}></td>
      <td><span class="tmpl-id">${escapeHtml(r.service)}</span></td><td class="r">${plInt(r.last)}</td><td class="r">${plInt(r.avg15)}</td>
      <td>${plSpark((tp[r.service] || {}).points)}</td><td class="r">${plDelta(r)}</td>
      <td class="r">${plPercent(r.miss_ratio)}</td><td class="r">${plInt(r.templates)}</td><td class="r">${plInt(r.new_24h)}</td>
      <td class="r">${plInt(r.unparsed_1h)}</td><td class="r">${plInt(r.not_normal)}</td></tr>`).join('')
    : `<tr><td colspan="11" class="empty-state">${(d.services || []).length ? 'No service matches.' : 'No service yet.'}</td></tr>`);
}

function plSelectService(svc) {
  pl.svc = pl.svc === svc ? null : svc;
  if (pl.svc && !['reader', 'templates'].includes(pl.sel)) pl.sel = 'reader';
  plRenderAll(pl.data);
}
$pl('pl-services-tbody').addEventListener('change', e => {
  const box = e.target.closest('input[data-pick]'); if (!box || !pl.data) return;
  const msg = $pl('pl-tp-msg'), cur = plChosen(pl.data), svc = box.dataset.pick;
  msg.hidden = true;
  if (box.checked && cur.length >= PL_MAX) {
    box.checked = false;
    msg.textContent = `At most ${PL_MAX} services in the chart: untick one first.`; msg.hidden = false;
    return;
  }
  pl.pick = box.checked ? [...cur, svc] : cur.filter(x => x !== svc);
  plSavePick();
  plRenderThroughput(pl.data);
});
$pl('pl-tp-mode').addEventListener('click', e => {
  const mode = e.target.closest('[data-mode]')?.dataset.mode; if (!mode || !pl.data) return;
  pl.mode = mode; plStore.set('pl.mode', mode);
  for (const [, el] of pl.cards) plDisposeCard(el);   // the card shape differs between the two modes
  pl.cards.clear();
  plRenderThroughput(pl.data);
});
$pl('pl-search').addEventListener('input', e => { pl.filter = e.target.value; if (pl.data) plRenderServices(pl.data); });
$pl('pl-services-tbody').addEventListener('click', e => {
  if (e.target.closest('input')) return;
  const tr = e.target.closest('tr[data-svc]'); if (tr) plSelectService(tr.dataset.svc);
});
$pl('pl-services-tbody').addEventListener('keydown', e => {
  const tr = e.target.closest('tr[data-svc]'); if (tr && e.key === 'Enter' && !e.target.closest('input')) plSelectService(tr.dataset.svc);
});
document.querySelector('#pl-services thead').addEventListener('click', e => {
  const key = e.target.closest('.pl-sort')?.dataset.sort; if (!key) return;
  if (!pl.data) return;
  pl.sort = {key, dir: pl.sort.key === key ? -pl.sort.dir : 1};
  plRenderServices(pl.data);
});

function plRenderIndices(d) {
  const rows = plIndexRows(d);
  $pl('pl-indices-tbody').innerHTML = rows || '<tr><td colspan="4" class="empty-state">No index is being read yet.</td></tr>';
}

function plRenderAll(d) {
  plRenderTopology(d);
  plRenderStage(d);
  plRenderHistory(d);
  plRenderTiles(d);
  plRenderThroughput(d);
  plRenderServices(d);
  plRenderIndices(d);
}

// A failed or empty load must not leave the previous numbers on screen.
function plClear() {
  pl.data = null;
  ['pl-topology', 'pl-lag', 'pl-eps'].forEach(id => echarts.getInstanceByDom($pl(id))?.clear());
  for (const [, el] of pl.cards) plDisposeCard(el);
  pl.cards.clear();
  $pl('pl-stage')._plHtml = $pl('pl-services-tbody')._plHtml = null;
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
  plApplyRoute();
  pl.data = d;
  plRenderAll(d);
}
