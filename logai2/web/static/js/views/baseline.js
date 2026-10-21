// ── Baseline: how one key behaved against its baseline (read-only; decisions live in review.js) ──
// All numbers, bands, incidents and decisions come from /api/baseline/*; nothing is computed here
// beyond picking labels. Helpers rvPad/rvN/rvZ/rvDayLabel/rvInk/rvCard come from review.js.
const bl = {service: '', key: '*total', days: 14, series: null, matrix: null, sel: null, zoom: null,
            seq: 0, incSeq: 0, lastSub: null, tplService: null, templates: []};
const $bl = id => document.getElementById(id);
const blIncKey = i => `${i.day}|${i.hour_from}|${i.hour_to}`;
const blPlural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;

function blStatus(message, error = false) {
  $bl('bl-status').textContent = message;
  $bl('bl-status').classList.toggle('ins-toast-error', error);
}

// ── Route: #baseline/<service>/<key>/<days> ──
function blApplyRoute() {
  const sub = state.subView || '';
  if (sub === bl.lastSub) return;
  bl.lastSub = sub;
  const p = sub.split('/').filter(Boolean).map(decodeURIComponent);
  if (p[0]) bl.service = p[0];
  if (p[1]) bl.key = p[1];
  if (['1', '7', '14'].includes(p[2])) bl.days = Number(p[2]);
  bl.sel = bl.zoom = null;
}
function blWriteRoute() {
  const sub = `${encodeURIComponent(bl.service)}/${encodeURIComponent(bl.key)}/${bl.days}`;
  bl.lastSub = state.subView = sub;
  if (state.activeView === 'baseline') history.replaceState(null, '', `#baseline/${sub}`);
}

// ── Load ──
async function loadBaseline() {
  blApplyRoute();
  const seq = ++bl.seq;
  const services = await getJson('/api/services', 'Unable to load services');
  if (seq !== bl.seq) return;
  if (!services.includes(bl.service)) bl.service = services[0] || '';
  $bl('bl-service').innerHTML = services.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
  $bl('bl-service').value = bl.service;
  if (!bl.service) {
    blClear();
    blStatus('No services yet: the engine adds them as it reads logs.');
    return;
  }
  const svc = encodeURIComponent(bl.service), key = encodeURIComponent(bl.key);
  let series, matrix;
  try {
    if (bl.tplService !== bl.service) {
      const t = await getJson(`/api/templates?service=${svc}&size=500`, 'Unable to load templates');
      if (seq !== bl.seq) return;
      bl.tplService = bl.service;
      bl.templates = t.items;
    }
    [series, matrix] = await Promise.all([
      getJson(`/api/baseline/series?service=${svc}&key=${key}&days=${bl.days}`, 'Unable to load the series'),
      getJson(`/api/baseline/matrix?service=${svc}&key=${key}&days=${bl.days === 1 ? 7 : bl.days}`, 'Unable to load the matrix')]);
  } catch (err) {
    if (seq === bl.seq) { blClear(); blRenderControls(); blStatus(`${err.message} for "${bl.key}".`, true); }
    return;
  }
  if (seq !== bl.seq) return;
  blStatus('');
  bl.series = series; bl.matrix = matrix;
  blRenderControls();
  blRenderAll();
}

// A failed load must not leave the previous key's data on screen.
function blClear() {
  bl.series = bl.matrix = bl.sel = null;
  bl.incSeq++;
  ['bl-main', 'bl-matrix', 'bl-profile', 'bl-modules'].forEach(id => echarts.getInstanceByDom($bl(id))?.clear());
  $bl('bl-cards').innerHTML = '';
  $bl('bl-legend').innerHTML = '';
  $bl('bl-inc').innerHTML = '';
  $bl('bl-inc-count').textContent = '';
  $bl('bl-detail').hidden = true;
  $bl('bl-profile-note').textContent = '';
}

function blKeyName(key = bl.key) {
  if (key === '*total') return 'All events';
  if (key === '*error') return 'Errors';
  return key;
}
function blRenderControls() {
  const now = Date.now() / 1000;
  const tpls = bl.templates.map(i => {
    const fresh = now - i.first_seen < 86400 ? ' (new)' : '';
    return `<option value="${escapeHtml(i.template_id)}">${escapeHtml(`${i.template_id} · ${i.level} · ${i.text}`.slice(0, 90) + fresh)}</option>`;
  });
  if (!bl.key.startsWith('*') && !bl.templates.some(i => i.template_id === bl.key)) {
    tpls.unshift(`<option value="${escapeHtml(bl.key)}">${escapeHtml(bl.key)}</option>`);
  }
  $bl('bl-key').innerHTML = `<option value="*total">All events</option><option value="*error">Errors</option>${tpls.join('')}`;
  $bl('bl-key').value = bl.key;
  $bl('bl-days').querySelectorAll('button').forEach(b => {
    const on = Number(b.dataset.days) === bl.days;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

function blRenderAll() {
  blRenderCards();
  blRenderMain();
  blRenderIncidents();
  blRenderMatrix();
  blRenderProfile();
  const inc = bl.sel && bl.series.incidents.find(i => blIncKey(i) === bl.sel);
  if (inc) blLoadDetail(inc); else { bl.sel = null; bl.incSeq++; $bl('bl-detail').hidden = true; }
}

const BL_STATE = {full: 'Full', young: 'Young', borrowed: 'Borrowed', none: 'None yet'};
const BL_STATE_SUB = {
  full: 'built from enough recent days', young: 'few days so far: wider band',
  borrowed: 'borrows weekday, upper alerts only', none: 'floor-only alerts until the next commit includes it'};
function blRenderCards() {
  const s = bl.series, pts = s.points;
  const now = [...pts].reverse().find(p => p.rate != null);
  const outHours = pts.filter(p => p.out).length;
  let nowSub = 'no data yet';
  if (now) {
    nowSub = now.band_high == null ? 'no baseline yet: floor-only alerts'
      : `expected ${rvN(now.band_low)}–${rvN(now.band_high)} · ${now.out ? (now.rate > now.band_high ? '▲ above' : '▼ below') : 'inside'}`;
  }
  const review = s.incidents.filter(i => i.decision === 'review').length;
  $bl('bl-cards').innerHTML = [
    rvCard('Now', now ? `${rvN(now.rate)}/min` : '—', nowSub, now && now.out ? 'bad' : ''),
    rvCard('Possible incidents', String(s.incidents.length), `${blPlural(outHours, 'hour')} outside the band`, s.incidents.some(i => i.ongoing) ? 'bad' : ''),
    rvCard('Needs review', String(review), review ? 'decide on the Review page' : 'nothing suggested', review ? 'warn' : ''),
    rvCard('Baseline', BL_STATE[s.baseline_state] || s.baseline_state, BL_STATE_SUB[s.baseline_state] || '', s.baseline_state === 'none' ? 'warn' : ''),
  ].join('');
}

// ── Actual vs expected ──
const blLabel = p => `${p.day} ${rvPad(p.hour)}`;
function blRenderMain() {
  const t = chartTokens(), s = bl.series, pts = s.points, multi = bl.days > 1;
  $bl('bl-title').textContent = `${blKeyName()}: actual vs expected, ${bl.days === 1 ? 'today' : `last ${bl.days} days`}`;
  const labels = pts.map(blLabel);
  const at = (day, hour) => Math.min(labels.indexOf(`${day} ${rvPad(hour)}`), pts.length - 1);
  const areas = s.incidents.map(i => {
    const from = at(i.day, i.hour_from), to = Math.min(at(i.day, i.hour_to - 1) + 1, pts.length - 1);
    return [{xAxis: labels[from], name: i.decision === 'exclude' ? '✕' : i.decision === 'keep' ? '✓' : '',
             itemStyle: {color: t.cls.up2 + '33', borderColor: t.status.bad, borderWidth: 1}}, {xAxis: labels[to]}];
  });
  const zoom = bl.zoom ? {startValue: bl.zoom[0], endValue: bl.zoom[1]} : {};
  chartFor($bl('bl-main')).setOption({
    ...baseOption(),
    grid: {left: 52, right: 16, top: 24, bottom: multi ? 64 : 28},
    xAxis: {type: 'category', data: labels, boundaryGap: false, axisLine: {lineStyle: {color: t.grid}},
            axisLabel: {color: t.dim, fontSize: 11, interval: (_, v) => multi ? v.endsWith(' 00') : Number(v.slice(-2)) % 2 === 0,
                        formatter: v => multi ? v.slice(5, 10) : `${v.slice(-2)}:00`}},
    yAxis: {type: 'value', min: 0, name: 'events/min', nameTextStyle: {color: t.muted, fontSize: 11},
            splitLine: {lineStyle: {color: t.grid}}, axisLabel: {color: t.dim}},
    dataZoom: multi ? [{type: 'inside', ...zoom}, {type: 'slider', height: 22, bottom: 8, borderColor: t.border,
                       textStyle: {color: t.dim}, ...zoom}] : [],
    tooltip: {...baseOption().tooltip, trigger: 'axis', confine: true, formatter: ps => {
      const p = pts[ps[0].dataIndex];
      return `<b>${escapeHtml(rvDayLabel(p.day))} ${rvPad(p.hour)}:00</b><br>actual <b>${p.rate == null ? '—' : rvN(p.rate) + '/min'}</b><br>`
        + (p.band_high == null ? 'no baseline yet (floor only)'
          : `expected ${rvN(p.band_low)}–${rvN(p.band_high)} · z ${rvZ(p.z)}`)
        + `<br>a week earlier ${rvN(p.last_week)}` + (p.out ? '<br><b>outside the band</b>' : '');
    }},
    series: [
      {type: 'line', data: pts.map(p => p.band_low == null ? null : Math.max(0, p.band_low)), stack: 'b', symbol: 'none', lineStyle: {opacity: 0}, silent: true},
      {type: 'line', data: pts.map(p => p.band_high == null || p.band_low == null ? null : p.band_high - Math.max(0, p.band_low)), stack: 'b',
       symbol: 'none', lineStyle: {opacity: 0}, areaStyle: {color: t.band}, silent: true},
      {type: 'line', data: pts.map(p => p.last_week), symbol: 'none', lineStyle: {type: 'dashed', width: 1.2, color: t.lastweek}},
      {type: 'line', data: pts.map(p => p.rate), symbol: 'none', lineStyle: {width: 2, color: t.line},
       markArea: {silent: true, label: {color: t.status.bad, fontWeight: 700, position: 'insideTop'}, data: areas}},
      {type: 'scatter', data: pts.map(p => p.out ? p.rate : null), symbolSize: 7, itemStyle: {color: t.cls.up2}},
    ],
  }, true);
}

// ── Possible incidents ──
function blDecision(i) {
  return {exclude: '✕ excluded', keep: '✓ kept', review: 'needs review'}[i.decision] || 'not suggested';
}
function blRenderIncidents() {
  const incs = bl.series.incidents;
  $bl('bl-inc-count').textContent = `(${incs.length})`;
  const rows = incs.map((i, idx) => ({i, idx})).reverse();
  $bl('bl-inc').innerHTML = rows.length ? rows.map(({i, idx}) => {
    const pk = i.peak;
    const when = `<b>${escapeHtml(rvDayLabel(i.day))}</b> ${rvHour(i.hour_from)}–${i.ongoing ? 'now' : rvHour(i.hour_to)}`;
    const state = i.ongoing ? '<span class="badge badge-error">● ongoing</span>' : `<span class="badge badge-debug">resolved ${rvHour(i.hour_to)}</span>`;
    return `<tr data-i="${idx}" tabindex="0"${blIncKey(i) === bl.sel ? ' aria-current="true"' : ''}>
      <td>${when}</td><td>${state}</td><td class="r">${i.hours} h</td><td class="r">${rvN(pk.rate)}/min</td>
      <td class="r">${pk.band_high == null ? '—' : `${rvN(pk.band_low)}–${rvN(pk.band_high)}`}</td>
      <td class="r">${i.times_normal == null ? '—' : '×' + rvN(i.times_normal)}</td><td>${blDecision(i)}</td>
      <td><button type="button" class="btn" data-review="${idx}">Review</button></td></tr>`;
  }).join('') : '<tr><td colspan="8" class="sync-status">No hour left the band in this period.</td></tr>';
}

function blSelect(idx) {
  const inc = bl.series.incidents[idx];
  if (!inc) return;
  bl.sel = blIncKey(inc);
  const pts = bl.series.points;
  const a = pts.findIndex(p => p.day === inc.day && p.hour === inc.hour_from);
  bl.zoom = bl.days > 1 && a >= 0 ? [Math.max(0, a - 12), Math.min(pts.length - 1, a + inc.hours - 1 + 12)] : null;
  blRenderMain(); blRenderIncidents();
  blLoadDetail(inc);
}

async function blLoadDetail(inc) {
  const seq = ++bl.incSeq;
  $bl('bl-detail').hidden = false;
  $bl('bl-detail-title').textContent = `${rvDayLabel(inc.day)} ${rvHours(inc.hour_from, inc.hour_to)}: where the lines came from`;
  $bl('bl-samples').innerHTML = '<p class="sync-status">Loading…</p>';
  const q = `service=${encodeURIComponent(bl.service)}&key=${encodeURIComponent(bl.key)}&day=${inc.day}&hour_from=${inc.hour_from}&hour_to=${inc.hour_to}`;
  let d;
  try {
    d = await getJson(`/api/baseline/incident?${q}`, 'Unable to load the incident');
  } catch (err) {
    if (seq !== bl.incSeq) return;
    echarts.getInstanceByDom($bl('bl-modules'))?.clear();
    $bl('bl-modules').style.height = '0';
    $bl('bl-samples').innerHTML = `<p class="ins-toast ins-toast-error">${escapeHtml(err.message)}</p>`;
    return;
  }
  if (seq !== bl.incSeq) return;
  blRenderModules(d.modules);
  $bl('bl-samples').innerHTML = blSamplesHtml(d);
}

function blRenderModules(modules) {
  const el = $bl('bl-modules'), t = chartTokens();
  const total = modules.reduce((a, m) => a + m.lines, 0);
  if (!total) { echarts.getInstanceByDom(el)?.clear(); el.style.height = '0'; return; }
  el.style.height = `${Math.max(70, 16 + modules.length * 26)}px`;
  chartFor(el).setOption({
    ...baseOption(),
    grid: {left: 130, right: 56, top: 4, bottom: 4},
    xAxis: {type: 'value', max: 100, show: false},
    yAxis: {type: 'category', inverse: true, data: modules.map(m => m.module), axisTick: {show: false},
            axisLine: {show: false}, axisLabel: {color: t.dim, width: 120, overflow: 'truncate'}},
    tooltip: {...baseOption().tooltip, confine: true, formatter: p => {
      const m = modules[p.dataIndex];
      return `${escapeHtml(m.module)}<br><b>${m.lines.toLocaleString()}</b> lines · ${rvN(100 * m.lines / total)}%`;
    }},
    series: [{type: 'bar', data: modules.map(m => 100 * m.lines / total), barMaxWidth: 16, itemStyle: {color: t.cls.up2},
              label: {show: true, position: 'right', color: t.dim, formatter: p => `${rvN(p.value)}%`}}],
  }, true);
  chartFor(el).resize();
}
function blSamplesHtml(d) {
  const modulesNote = d.modules.length ? '' : '<p class="sync-status">No module breakdown: no alert minutes were recorded in this range.</p>';
  if (!d.ongoing) {
    return `${modulesNote}<h4>Sample lines</h4><p class="sync-status">Not available for this period: the engine keeps only the newest 20 lines per ERROR template. Keeping sample lines per incident is a planned upgrade.</p>`;
  }
  if (!d.samples.length) return `${modulesNote}<h4>Sample lines</h4><p class="sync-status">No sample lines yet: the engine keeps them for ERROR templates only.</p>`;
  return `${modulesNote}<h4>Newest sample lines</h4><div class="table-wrap"><table>
    <thead><tr><th>Time</th><th>Template</th><th>Line</th></tr></thead><tbody>${d.samples.map(s =>
      `<tr><td>${escapeHtml(new Date(s.at * 1000).toLocaleTimeString())}</td><td class="tmpl-id">${escapeHtml(s.template_id)}</td>
       <td class="tmpl-text">${escapeHtml(s.line)}</td></tr>`).join('')}</tbody></table></div>`;
}

// ── Days x hours (read-only; colours come from the API's cls) ──
function blRenderMatrix() {
  const t = chartTokens(), m = bl.matrix, rows = m.days;
  const drops = ['*total', '*error'].includes(bl.key), k = m.k.weekday;
  const items = [['none', 'no baseline'], ['in', `inside ±${k}σ`], ['up1', 'above, to 6σ'], ['up2', '6–10σ'], ['up3', '> 10σ'],
                 ['down1', 'below, to 6σ'], ['down2', '6–10σ'], ['down3', '> 10σ']];
  $bl('bl-legend').innerHTML = items.filter(([c]) => drops || !c.startsWith('down'))
    .map(([c, label]) => `<span><i style="background:${t.cls[c]}"></i>${label}</span>`).join('');
  const data = [];
  rows.forEach((row, y) => row.hours.forEach(c => {
    const fill = t.cls[c.cls] || t.cls.none;
    data.push({value: [c.hour, y, c.z ?? 0], cell: c, day: row.day,
               mark: c.decision ? (c.decision === 'exclude' ? '✕' : '✓') : '',
               itemStyle: {color: fill, borderColor: c.cls === 'none' ? t.dim : t.card, borderWidth: 1.5, opacity: c.decision === 'exclude' ? 0.6 : 1},
               label: {color: rvInk(fill)}});
  }));
  $bl('bl-matrix').style.height = `${70 + rows.length * 28}px`;
  chartFor($bl('bl-matrix')).setOption({
    ...baseOption(),
    grid: {left: 96, right: 8, top: 22, bottom: 8},
    xAxis: {type: 'category', position: 'top', data: [...Array(24).keys()].map(rvPad), splitLine: {show: false},
            axisLine: {show: false}, axisTick: {show: false}, axisLabel: {color: t.dim}},
    yAxis: {type: 'category', inverse: true, data: rows.map(r => rvDayLabel(r.day)), splitLine: {show: false},
            axisLine: {show: false}, axisTick: {show: false}, axisLabel: {color: t.dim, fontSize: 11.5}},
    tooltip: {...baseOption().tooltip, confine: true, formatter: p => {
      const c = p.data.cell;
      return `<b>${escapeHtml(rvDayLabel(p.data.day))} ${rvHours(c.hour, c.hour + 1)}</b><br>`
        + (c.no_baseline ? `${rvN(c.rate)}/min · no baseline for this hour`
          : `${rvN(c.rate)}/min · expected ${rvN(c.mean)} ± ${rvN(c.std)} · z <b>${rvZ(c.z)}</b>`)
        + (c.decision ? `<br>${c.decision === 'exclude' ? 'excluded' : 'kept'}` : '');
    }},
    series: [{type: 'heatmap', data, progressive: 0, label: {show: true, formatter: p => p.data.mark, fontSize: 12, fontWeight: 700}}],
  }, true);
}

// ── The baseline itself ──
function blRenderProfile() {
  const t = chartTokens(), pr = bl.series.profile;
  const wd = Object.fromEntries(pr.weekday.map(r => [r.hour, r])), we = Object.fromEntries(pr.weekend.map(r => [r.hour, r]));
  const today = Object.fromEntries(pr.today.map(r => [r.hour, r.rate]));
  const hours = [...Array(24).keys()];
  const lo = h => wd[h] ? Math.max(0, wd[h].band_low) : null;
  $bl('bl-profile-note').textContent = pr.weekday.length || pr.weekend.length ? ''
    : 'No baseline yet for this key: it alerts only above its level floor until the next commit includes it.';
  chartFor($bl('bl-profile')).setOption({
    ...baseOption(),
    grid: {left: 52, right: 12, top: 14, bottom: 26},
    xAxis: {type: 'category', data: hours.map(rvPad), boundaryGap: false, axisLine: {lineStyle: {color: t.grid}}, axisLabel: {color: t.dim}},
    yAxis: {type: 'value', splitLine: {lineStyle: {color: t.grid}}, axisLabel: {color: t.dim}},
    tooltip: {...baseOption().tooltip, trigger: 'axis', confine: true, formatter: ps => {
      const h = ps[0].dataIndex;
      return `<b>${rvPad(h)}:00</b><br>` + (wd[h] ? `weekday ${rvN(wd[h].mean)} (band ${rvN(lo(h))}–${rvN(wd[h].band_high)})` : 'weekday: no baseline')
        + `<br>weekend ${we[h] ? rvN(we[h].mean) : '—'}<br>today ${rvN(today[h])}`;
    }},
    series: [
      {type: 'line', data: hours.map(lo), stack: 'p', symbol: 'none', lineStyle: {opacity: 0}, silent: true},
      {type: 'line', data: hours.map(h => wd[h] ? wd[h].band_high - lo(h) : null), stack: 'p', symbol: 'none',
       lineStyle: {opacity: 0}, areaStyle: {color: t.band}, silent: true},
      {name: 'weekday', type: 'line', data: hours.map(h => wd[h] ? wd[h].mean : null), symbol: 'none', lineStyle: {width: 1.5, color: t.dim}},
      {name: 'weekend', type: 'line', data: hours.map(h => we[h] ? we[h].mean : null), symbol: 'none', lineStyle: {width: 1.5, type: 'dashed', color: t.muted}},
      {name: 'today', type: 'line', data: hours.map(h => today[h] ?? null), symbol: 'circle', symbolSize: 5,
       lineStyle: {width: 2, color: t.accent}, itemStyle: {color: t.accent}},
    ],
  }, true);
}

// ── Events ──
function blChangeRoute() { bl.sel = bl.zoom = null; blWriteRoute(); loadData(); }
$bl('bl-service').addEventListener('change', () => { bl.service = $bl('bl-service').value; bl.key = '*total'; bl.tplService = null; blChangeRoute(); });
$bl('bl-key').addEventListener('change', () => { bl.key = $bl('bl-key').value; blChangeRoute(); });
$bl('bl-days').addEventListener('click', event => {
  const b = event.target.closest('[data-days]');
  if (!b || Number(b.dataset.days) === bl.days) return;
  bl.days = Number(b.dataset.days); blChangeRoute();
});
function blOpenReview(idx) {
  const i = bl.series.incidents[idx];
  if (!i) return;
  location.hash = `#review/${encodeURIComponent(bl.service)}/${encodeURIComponent(bl.key)}/14/${i.day}/${i.hour_from}/${i.hour_to}`;
}
$bl('bl-inc').addEventListener('click', event => {
  const btn = event.target.closest('[data-review]');
  if (btn) return blOpenReview(Number(btn.dataset.review));
  const row = event.target.closest('tr[data-i]');
  if (row) blSelect(Number(row.dataset.i));
});
$bl('bl-inc').addEventListener('keydown', event => {
  const row = event.target.closest('tr[data-i]');
  if (row && event.target === row && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); blSelect(Number(row.dataset.i)); }
});
