// ── Evaluation: the 4 band methods against one key's real counts (read-only; Re-tune is the only write) ──
// All numbers come from /api/eval/*; nothing is computed here beyond layout and picking the narrowest unflagged method.
// Helpers rvPad/rvDayLabel come from review.js.
const ev = {service: '', key: '*total', days: 7, data: null, tune: null, tab: null, keys: [], keysFor: null, seen: null,
            seq: 0, lastSub: null, timer: null, built: false, cursor: 0, labels: []};
const $ev = id => document.getElementById(id);
const EV_METHODS = {
  sigma: {live: true, desc: 'Live band: mean ± kσ from the baseline table.'},
  nb: {desc: 'Negative binomial quantiles: skewed, whole counts.'},
  median: {desc: 'Median day ± robust spread: one bad day moves nothing.'},
  ewma: {desc: 'Recent days weigh more (half-life 2 days).'},
};
const EV_IDS = Object.keys(EV_METHODS);
const EV_KINDS = ['*total', '*error', '*warn/*info', 'busy', 'sparse'];
const EV_MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const evFmt = v => v == null ? '—' : v >= 100 ? Math.round(v).toLocaleString('en-US') : v >= 10 ? v.toFixed(0) : v.toFixed(1);
const evPct = v => v == null ? '—' : `${(v * 100).toFixed(1)} %`;
const evDay = iso => `${+iso.slice(8)} ${EV_MON[+iso.slice(5, 7) - 1]}`;
const evRatioTone = r => r == null ? '' : r < 0.95 ? 'ev-good-fg' : r > 1.05 ? 'ev-warn-fg' : '';

function evStatus(message, error = false) {
  $ev('ev-status').textContent = message;
  $ev('ev-status').classList.toggle('ins-toast-error', error);
}

// ── Route: #evaluation/<service>/<key>/<days> ──
function evApplyRoute() {
  const sub = state.subView || '';
  if (sub === ev.lastSub) return;
  ev.lastSub = sub;
  let p;
  try { p = sub.split('/').filter(Boolean).map(decodeURIComponent); } catch (e) { return; }
  if (p[0]) ev.service = p[0];
  if (p[1]) ev.key = p[1];
  if (['1', '7'].includes(p[2])) ev.days = Number(p[2]);
}
function evWriteRoute() {
  const sub = `${encodeURIComponent(ev.service)}/${encodeURIComponent(ev.key)}/${ev.days}`;
  ev.lastSub = state.subView = sub;
  if (state.activeView === 'evaluation') history.replaceState(null, '', `#evaluation/${sub}`);
}

// ── Load ──
async function loadEvaluation() {
  evApplyRoute();
  const seq = ++ev.seq;
  const services = await getJson('/api/services', 'Unable to load services');
  if (seq !== ev.seq) return;
  if (!services.includes(ev.service)) ev.service = services[0] || '';
  $ev('ev-service').innerHTML = services.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
  $ev('ev-service').value = ev.service;
  if (!ev.service) {
    evClear();
    evStatus('No services yet: the engine adds them as it reads logs.');
    return;
  }
  const svc = encodeURIComponent(ev.service);
  try {
    const [keys, tune] = await Promise.all([
      ev.keysFor === ev.service ? ev.keys : getJson(`/api/eval/keys?service=${svc}`, 'Unable to load the keys'),
      getJson('/api/eval/tune', 'Unable to load the tune status')]);
    if (seq !== ev.seq) return;
    ev.keys = keys; ev.keysFor = ev.service; ev.tune = tune;
  } catch (err) {
    if (seq === ev.seq) { evClear(); evStatus(`${err.message}.`, true); }
    return;
  }
  if (!ev.keys.some(k => k.key === ev.key)) ev.key = '*total';
  evWriteRoute();
  evRenderControls();
  evRenderTune();
  evPoll();
  let data;
  try {
    data = await getJson(`/api/eval/bands?service=${svc}&key=${encodeURIComponent(ev.key)}&days=${ev.days}`, 'Unable to load the bands');
  } catch (err) {
    if (seq === ev.seq) { evClear(); evStatus(`${err.message} for "${ev.key}".`, true); }
    return;
  }
  if (seq !== ev.seq) return;
  evStatus('');
  const same = ev.seen === `${ev.service}/${ev.key}`;      // same key reloaded (poll, theme): keep the tab and the hour
  ev.seen = `${ev.service}/${ev.key}`;
  ev.data = data; ev.tab = same && ev.tab ? ev.tab : data.kind;
  if (!same) ev.cursor = 0;
  evRenderAll();
}

function evClear() {
  ev.data = null;
  $ev('ev-cards').innerHTML = '';
  $ev('ev-rows').innerHTML = '';
  $ev('ev-panel').hidden = true;
}

function evRenderControls() {
  $ev('ev-key').innerHTML = ev.keys.map(k => `<option value="${escapeHtml(k.key)}">${escapeHtml(k.label)}</option>`).join('');
  $ev('ev-key').value = ev.key;
  $ev('ev-days').querySelectorAll('button').forEach(b => {
    const on = Number(b.dataset.days) === ev.days;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

// ── Tune status bar, Re-tune and its poll ──
function evRenderTune() {
  const t = ev.tune, r = t.result, busy = t.status === 'running', prog = t.progress;
  const box = $ev('ev-tune');
  box.classList.toggle('ev-untuned', !r && !busy);
  box.classList.toggle('ev-busy', busy);
  let title, detail;
  if (busy) {
    title = 'Re-tuning…';
    detail = 'the last result stays until this run finishes' + (prog && prog.total ? ` · ${prog.done} / ${prog.total} keys` : '');
  } else if (r) {
    const d = new Date(r.run_at), days = r.test_days;
    title = `Tuned ${d.getDate()} ${EV_MON[d.getMonth()]} ${rvPad(d.getHours())}:${rvPad(d.getMinutes())}`;
    const span = !days.length ? '' : days.length === 1 || days[0] === days[days.length - 1] ? evDay(days[0])
      : days[0].slice(0, 7) === days[days.length - 1].slice(0, 7) ? `${+days[0].slice(8)}–${evDay(days[days.length - 1])}`
      : `${evDay(days[0])}–${evDay(days[days.length - 1])}`;
    const nkeys = Object.values(r.kinds).reduce((a, k) => a + k.keys, 0);
    detail = `test days ${span} · ${nkeys} keys · 4 methods`;
  } else {
    title = 'Not tuned yet';
    detail = 'every method is drawn at k 3 until the first Re-tune';
  }
  if (t.error && !busy) detail += ` · the last Re-tune failed: ${t.error}`;
  $ev('ev-tune-title').textContent = title;
  $ev('ev-tune-detail').textContent = detail;
  const btn = $ev('ev-retune');
  btn.disabled = busy;
  btn.textContent = busy ? 'Running…' : 'Re-tune';
}

function evPoll() {
  if (ev.tune.status !== 'running' || ev.timer) return;
  ev.timer = setInterval(async () => {
    if (state.activeView !== 'evaluation') { clearInterval(ev.timer); ev.timer = null; return; }
    try { ev.tune = await getJson('/api/eval/tune', 'Unable to load the tune status'); } catch (e) { return; }
    evRenderTune();
    if (ev.tune.status !== 'running') { clearInterval(ev.timer); ev.timer = null; loadEvaluation(); }
  }, 2000);
}

$ev('ev-retune').addEventListener('click', async () => {
  try {
    await apiMutation('/api/eval/tune', 'POST', {});
    evStatus('');
  } catch (err) { evStatus(err.message, true); }
  try {
    ev.tune = await getJson('/api/eval/tune', 'Unable to load the tune status');
    evRenderTune();
    evPoll();
  } catch (err) { evStatus(err.message, true); }
});
$ev('ev-service').addEventListener('change', e => { ev.service = e.target.value; ev.key = '*total'; evWriteRoute(); loadEvaluation(); });
$ev('ev-key').addEventListener('change', e => { ev.key = e.target.value; evWriteRoute(); loadEvaluation(); });
$ev('ev-days').addEventListener('click', e => {
  const b = e.target.closest('button[data-days]');
  if (b) { ev.days = Number(b.dataset.days); evWriteRoute(); loadEvaluation(); }
});

// ── Render ──
const evKindInfo = (kind, id) => (ev.tune.result?.kinds[kind]?.methods || {})[id];
function evRenderAll() {
  $ev('ev-panel').hidden = false;
  evRenderCards();
  evRenderCharts();
  evRenderTable();
}

function evRenderCards() {
  const d = ev.data, info = id => evKindInfo(d.kind, id);
  const ok = d.methods.filter(m => m.tuned && !info(m.id)?.flag && m.ratio != null);
  const best = ok.length ? ok.reduce((a, m) => m.ratio < a.ratio ? m : a).id : null;
  $ev('ev-cards').innerHTML = d.methods.map(m => {
    const k = m.tuned ? String(m.k) : `${m.k} (not tuned)`;
    return `<a class="stat-card ev-card${m.id === best ? ' ev-best' : ''}" href="#m-${m.id}" data-m="${m.id}">
      <div class="ev-card-head"><span class="ev-mono">${m.id}</span>
        ${m.id === best ? '<span class="ev-tag ev-good">Narrowest at 99.5 %</span>' : ''}
        ${EV_METHODS[m.id].live ? '<span class="ev-tag">live</span>' : ''}</div>
      <div class="ev-desc">${EV_METHODS[m.id].desc}</div>
      ${info(m.id)?.flag ? `<div class="ev-warn-fg">${escapeHtml(info(m.id).flag)}</div>` : ''}
      <dl><div><dt>k</dt><dd>${k}</dd></div>
        <div><dt>coverage (estimated)</dt><dd>${evPct(m.coverage)}</dd></div>
        <div><dt>hours out</dt><dd class="${m.outside ? 'ev-warn-fg' : ''}">${m.missing ? '—' : m.outside}</dd></div></dl>
      <div><div class="ev-ratio"><span>width vs σ</span><span class="ev-mono ${info(m.id)?.flag ? '' : evRatioTone(m.ratio)}">${m.ratio == null ? '—' : m.ratio.toFixed(2)}</span></div>
        <div class="ev-bar" aria-hidden="true"><i style="width:${m.ratio == null ? 0 : Math.min(100, m.ratio / 1.5 * 100).toFixed(1)}%"></i><b></b></div></div>
    </a>`;
  }).join('');
}
$ev('ev-cards').addEventListener('click', e => {
  const a = e.target.closest('a[data-m]');
  if (!a) return;
  e.preventDefault();           // a real #hash would change the route
  const el = $ev(`m-${a.dataset.m}`);
  el.scrollIntoView({behavior: REDUCED_MOTION ? 'auto' : 'smooth', block: 'start'});
});

function evBuild() {
  $ev('ev-charts').innerHTML = EV_IDS.map((id, i) => `<div class="ev-block" id="m-${id}">
    <div class="ev-block-head"><h4>${id}</h4><span class="sync-status" data-k></span><span class="ev-grow"></span><span class="sync-status" data-cur></span></div>
    <div class="ev-chart" id="ev-c-${id}" role="img"${i === EV_IDS.length - 1 ? ' style="height:152px"' : ''}></div></div>`).join('');
  ev.built = true;
}

function evRuns(flags) {
  const runs = [];
  flags.forEach((f, i) => { if (f) { if (i && flags[i - 1]) runs[runs.length - 1][1] = i; else runs.push([i, i]); } });
  return runs;
}

function evRenderCharts() {
  if (!ev.built || !$ev('ev-c-sigma')) evBuild();
  const t = chartTokens(), d = ev.data, pts = d.points, multi = ev.days > 1;
  const labels = ev.labels = pts.map(p => `${p.day} ${rvPad(p.hour)}`);
  $ev('ev-title').textContent = ev.keys.find(k => k.key === ev.key)?.label || ev.key;
  let ymax = 1;
  pts.forEach(p => { if (p.rate != null) ymax = Math.max(ymax, p.rate); });
  d.methods.forEach(m => m.bands.forEach(b => { if (b) ymax = Math.max(ymax, b.high); }));
  ymax *= 1.05;
  const nights = [];
  for (let i = 0; i < pts.length; i++) {
    if (pts[i].hour !== 0) continue;
    let j = i; while (j + 1 < pts.length && pts[j + 1].day === pts[i].day && pts[j + 1].hour <= 6) j++;
    nights.push([{xAxis: labels[i], itemStyle: {color: t.grid, opacity: 0.35}}, {xAxis: labels[j]}]);
  }
  d.methods.forEach((m, mi) => {
    const last = mi === d.methods.length - 1, el = $ev(`ev-c-${m.id}`), blk = $ev(`m-${m.id}`);
    const flags = pts.map((p, i) => {
      const b = m.bands[i];
      return !!b && p.rate != null && (p.rate > b.high || (ev.key === '*total' && p.rate < b.low));
    });
    const gaps = m.bands.filter(b => !b).length;
    blk.querySelector('[data-k]').textContent = `k ${m.k}${m.tuned ? '' : ' (not tuned)'}`
      + (m.missing ? '' : gaps ? ` · no range for ${gaps} h` : '');
    el.setAttribute('aria-label', m.missing ? `${m.id}: ${m.missing}`
      : `${m.id}: ${m.outside} hours outside its range in the shown days`);
    const chart = chartFor(el);
    chart.group = 'ev';
    chart.setOption({
      ...baseOption(),
      grid: {left: 52, right: 16, top: 8, bottom: last ? 24 : 4},
      xAxis: {type: 'category', data: labels, boundaryGap: false, axisLine: {lineStyle: {color: t.grid}}, axisTick: {show: last},
              axisLabel: {show: last, color: t.dim, fontSize: 12,
                          interval: (_, v) => multi ? v.endsWith(' 00') : Number(v.slice(-2)) % 6 === 0,
                          formatter: v => multi ? rvDayLabel(v.slice(0, 10)).slice(0, -4) : `${v.slice(-2)}:00`},
              axisPointer: {show: true, snap: true, lineStyle: {color: t.accent, type: 'dashed'}}},
      yAxis: {type: 'value', min: 0, max: ymax, splitNumber: 2, splitLine: {lineStyle: {color: t.grid}},
              axisLabel: {color: t.dim, fontSize: 11, formatter: evFmt}},
      tooltip: {...baseOption().tooltip, trigger: 'axis', showContent: false},
      series: [
        {type: 'line', data: m.bands.map(b => b ? Math.max(0, b.low) : null), stack: 'b', symbol: 'none', lineStyle: {opacity: 0}, silent: true,
         markArea: {silent: true, data: nights}},
        {type: 'line', data: m.bands.map(b => b ? b.high - Math.max(0, b.low) : null), stack: 'b', symbol: 'none',
         lineStyle: {opacity: 0}, areaStyle: {color: t.band}, silent: true},
        {type: 'line', data: pts.map(p => p.rate), symbol: 'none', lineStyle: {width: 2, color: t.line}, z: 3,
         markArea: {silent: true, data: evRuns(flags).map(([a, b]) => [
           {xAxis: labels[a], itemStyle: {color: t.status.bad + '1f', borderColor: t.status.bad, borderWidth: 1.5}},
           {xAxis: labels[Math.min(b + 1, labels.length - 1)]}])}},
        {type: 'scatter', data: pts.map((p, i) => flags[i] ? p.rate : null), symbolSize: 8, z: 4,
         itemStyle: {color: t.status.warn, borderColor: t.card, borderWidth: 1.5}},
      ],
      graphic: m.missing ? [{type: 'text', left: 'center', top: 'middle', style: {text: m.missing, fill: t.dim, fontSize: 13}}] : [],
    }, true);
    chart.off('updateAxisPointer');
    chart.on('updateAxisPointer', e => {
      const v = e.axesInfo && e.axesInfo[0] && e.axesInfo[0].value;
      const i = typeof v === 'number' ? v : labels.indexOf(v);
      if (i >= 0 && i !== ev.cursor) evCursor(i);      // moves the pointer of all four; the i !== ev.cursor guard stops the loop
    });
  });
  echarts.connect('ev');
  const slider = $ev('ev-hour');
  slider.max = String(pts.length - 1);
  const lastRate = pts.map(p => p.rate).findLastIndex(r => r != null);
  evCursor(Math.min(ev.cursor, pts.length - 1) || (lastRate < 0 ? pts.length - 1 : lastRate));
}

// Reads the rate and every method's range at hour i and moves the axis pointer of all four charts.
function evCursor(i) {
  const d = ev.data, p = d.points[i];
  if (!p) return;
  ev.cursor = i;
  $ev('ev-hour').value = String(i);
  const label = `${rvDayLabel(p.day)} ${rvPad(p.hour)}:00 · rate ${p.rate == null ? '—' : evFmt(p.rate)}`;
  $ev('ev-hour-label').textContent = label;
  $ev('ev-hour').setAttribute('aria-valuetext', label);
  d.methods.forEach(m => {
    const b = m.bands[i], out = b && p.rate != null && (p.rate > b.high || (ev.key === '*total' && p.rate < b.low));
    const el = $ev(`m-${m.id}`).querySelector('[data-cur]');
    el.textContent = b ? `range ${evFmt(Math.max(0, b.low))}–${evFmt(b.high)}${out ? ' · outside' : ''}` : 'no range';
    el.style.color = out ? 'var(--orange)' : '';
    // the pointer is driven by option (showTip hides it again with showContent false); the last cursor stays on mouse leave
    chartFor($ev(`ev-c-${m.id}`)).setOption({xAxis: {axisPointer: {value: i, status: 'show'}}}, {lazyUpdate: true});
  });
}
$ev('ev-hour').addEventListener('input', e => evCursor(Number(e.target.value)));

// ── Tune table per kind ──
function evRenderTable() {
  const r = ev.tune.result, tab = ev.tab, kd = r?.kinds[tab];
  $ev('ev-tabs').innerHTML = EV_KINDS.map(k => `<button type="button" aria-pressed="${k === tab}" data-kind="${escapeHtml(k)}">${escapeHtml(k)}</button>`).join('');
  $ev('ev-caption').textContent = kd ? `${tab} · ${kd.keys} ${kd.keys === 1 ? 'key' : 'keys'}` : `${tab} · no keys tuned`;
  const rows = EV_IDS.map(id => ({id, ...(kd?.methods[id] || {})}));
  rows.sort((a, b) => (!!a.flag - !!b.flag) || (a.ratio ?? Infinity) - (b.ratio ?? Infinity));
  $ev('ev-rows').innerHTML = rows.map((m, i) => `<tr>
    <td class="sync-status">${i + 1}</td><td class="ev-mono">${m.id}</td>
    <td class="r ev-mono">${m.k == null ? '—' : m.k}</td><td class="r ev-mono">${m.hours ?? '—'}</td><td class="r ev-mono">${evPct(m.coverage)}</td>
    <td class="r ev-mono ${m.flag ? '' : evRatioTone(m.ratio)}">${m.ratio == null ? '—' : m.ratio.toFixed(2)}</td>
    <td class="${m.flag && (m.flag.startsWith('never') || m.flag.startsWith('few')) ? 'ev-warn-fg' : 'sync-status'}">${escapeHtml(kd ? m.flag || '' : 'not tuned yet')}</td></tr>`).join('');
}
$ev('ev-tabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-kind]');
  if (b) { ev.tab = b.dataset.kind; evRenderTable(); }
});
