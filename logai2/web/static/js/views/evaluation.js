// ── Evaluate: a lab run, read-only except "Choose" (POST /api/eval/choices) ──
// Everything shown comes from /api/eval/*; nothing is computed here beyond picking labels and drawing.
const ev = {runs: [], run: null, board: 'eval', kind: 'M1', board_data: null, keys: [], key: null, from: '', to: '',
            mode: 'multi', off: new Set(), choices: {}, calMethod: null, calKnob: null, sweep: null, seq: 0, fitSeq: 0};
const $ev = id => document.getElementById(id);
const evColors = () => { const c = getComputedStyle(document.documentElement); return ['accent', 'orange', 'green', 'blue', 'yellow', 'gray'].map(n => c.getPropertyValue(`--${n}`).trim()); };
const EV_KINDS = ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7'];
const EV_TONE = {'adopt candidate': 'ok', 'sigma stays': '', mixed: 'warn', pending: 'warn', 'no method': 'bad'};
const EV_GATES = ['coverage', 'band', 'alert_rate', 'flaps', 'detect', 'cost'];
const EV_GATE = {pass: ['✓ pass', 'badge-green'], fail: ['✗ fail', 'badge-red'], not_checked: ['– n/a', 'badge-gray']};
const evNum = (v, d = 2) => v == null || !isFinite(v) ? '—' : Number(v).toFixed(d);
const evPct = v => v == null || !isFinite(v) ? '—' : `${(v * 100).toFixed(1)}%`;
const evKnob = k => k == null ? '—' : String(Number(k));

function evStatus(msg, error = false) {
  $ev('ev-status').textContent = msg;
  $ev('ev-status').classList.toggle('ins-toast-error', error);
}
function evError(el, err) { el.textContent = err.message || String(err); el.classList.add('ins-toast-error'); }

async function evGet(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    let msg = `Request failed (${resp.status})`;
    try { msg = (await resp.json()).error || msg; } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  return resp.json();
}

// ── Load ──
async function loadEvaluation() {
  const seq = ++ev.seq;
  evStatus('');
  let runs;
  let saved;
  try { [runs, saved] = await Promise.all([evGet('/api/eval/runs'), evGet('/api/eval/choices')]); } catch (err) { evStatus(err.message, true); return; }
  if (seq !== ev.seq) return;
  ev.runs = runs;
  ev.choices = Object.fromEntries(saved.map(c => [c.kind, c]));
  $ev('ev-empty').hidden = true;
  if (!runs.length) {
    $ev('ev-body').hidden = true;
    $ev('ev-run').innerHTML = '';
    $ev('ev-strength').innerHTML = '';
    $ev('ev-counts').textContent = '';
    const e = $ev('ev-empty');
    e.hidden = false;
    e.innerHTML = '<p>No lab runs yet. Run <code>python -m scripts.lab.run --site laptop --test B --methods core --days 7</code> first, then reload.</p>';
    return;
  }
  if (!runs.some(r => r.id === ev.run)) ev.run = runs[0].id;
  $ev('ev-run').innerHTML = runs.map(r =>
    `<option value="${r.id}">#${r.id} · ${escapeHtml(r.site)} · Test ${escapeHtml(r.test)} · ${escapeHtml(r.window_from)} to ${escapeHtml(r.window_to)} · ${escapeHtml(r.status)}</option>`).join('');
  $ev('ev-run').value = String(ev.run);
  await evLoadRun(seq);
}

async function evLoadRun(seq = ++ev.seq) {
  const run = ev.runs.find(r => r.id === ev.run);
  const counts = (run.kinds && run.kinds.counts) || run.kinds || {};
  $ev('ev-strength').innerHTML = run.strength ? `<span class="badge badge-blue" title="Strength of the evidence">${escapeHtml(run.strength)}</span>` : '';
  $ev('ev-counts').textContent = EV_KINDS.filter(k => counts[k]).map(k => `${k} ×${counts[k]}`).join(' · ');
  const empty = $ev('ev-empty');
  if (run.status !== 'ok') {
    $ev('ev-body').hidden = true;
    empty.hidden = false;
    empty.innerHTML = `<p><strong>Run #${run.id} is ${escapeHtml(run.status)}.</strong> ${run.status === 'running' ? 'Reload when it finishes.'
      : 'It has no usable results.'}</p>${run.error ? `<p class="ins-toast ins-toast-error">${escapeHtml(run.error)}</p>` : ''}`;
    return;
  }
  empty.hidden = true;
  $ev('ev-body').hidden = false;
  let data;
  try { data = await evGet(`/api/eval/scoreboard?run=${run.id}`); } catch (err) { evStatus(err.message, true); return; }
  if (seq !== ev.seq) return;
  ev.board_data = data;
  const kinds = EV_KINDS.filter(k => data[k]);
  if (!kinds.length) {
    $ev('ev-body').hidden = true;
    empty.hidden = false;
    empty.innerHTML = `<p>Run #${run.id} has no results.</p>`;
    return;
  }
  if (!data[ev.kind]) ev.kind = kinds[0];
  evRenderBoard();
  evRenderCards();
  evRenderScore();
  await evLoadKind(seq);
}

async function evLoadKind(seq = ++ev.seq) {
  let keys, sweep;
  try {
    [keys, sweep] = await Promise.all([evGet(`/api/eval/keys?run=${ev.run}&kind=${ev.kind}`),
                                       evGet(`/api/eval/sweep?run=${ev.run}&kind=${ev.kind}`)]);
  } catch (err) { evStatus(err.message, true); return false; }
  if (seq !== ev.seq) return;
  ev.keys = keys; ev.sweep = sweep;
  const methods = Object.keys(sweep);
  if (!methods.includes(ev.calMethod)) ev.calMethod = methods.includes('sigma') ? 'sigma' : methods[0] || null;
  ev.calKnob = null;
  evRenderCal();
  await evLoadFit();
}

// ── Cards and scoreboard ──
function evRenderBoard() {
  $ev('ev-boards').querySelectorAll('button').forEach(b => {
    const on = b.dataset.board === ev.board;
    b.classList.toggle('active', on); b.setAttribute('aria-pressed', String(on));
  });
  $ev('ev-board-eval').hidden = ev.board !== 'eval';
  $ev('ev-board-cal').hidden = ev.board !== 'cal';
}

function evRenderCards() {
  const run = ev.runs.find(r => r.id === ev.run);
  $ev('ev-cards').innerHTML = EV_KINDS.filter(k => ev.board_data[k]).map(k => {
    const d = ev.board_data[k].decision;
    const on = k === ev.kind ? ' ev-on' : '';
    let value, sub;
    if (run.test === 'A') { value = 'Test A'; sub = 'recall and delay, no verdict'; }
    else if (!d) { value = 'no verdict'; sub = ''; }
    else {
      value = d.verdict || '—';
      sub = [d.leader ? `leader ${d.leader}` : '', d.strength, d.stability].filter(Boolean).join(' · ');
    }
    const tone = d && EV_TONE[d.verdict] ? EV_TONE[d.verdict] : '';
    return rvCard(k, value, sub, tone, `data-kind="${k}" aria-pressed="${k === ev.kind}" title="${escapeHtml(d && d.note || '')}"`)
      .replace('class="stat-card', `class="stat-card${on}`);
  }).join('');
}

function evCi(ci, v) {
  // A 90 % CI as a small bar on a fixed 0.5 to 1.5 scale; the tick is the point estimate.
  if (v == null || !ci) return `<td class="r">${evNum(v)}</td>`;
  const lo = Math.max(0, Math.min(1, (ci[0] - 0.5))), hi = Math.max(0, Math.min(1, (ci[1] - 0.5))), at = Math.max(0, Math.min(1, v - 0.5));
  return `<td class="r"><span class="ev-ci" role="img" aria-label="ratio ${evNum(v, 3)}, 90% CI ${evNum(ci[0], 3)} to ${evNum(ci[1], 3)}">`
    + `<i style="left:${lo * 100}%;width:${Math.max(hi - lo, 0.01) * 100}%"></i><b style="left:${at * 100}%"></b></span> ${evNum(v)}`
    + `<span class="ev-sub">${evNum(ci[0])}–${evNum(ci[1])}</span></td>`;
}

function evRenderScore() {
  const run = ev.runs.find(r => r.id === ev.run), k = ev.board_data[ev.kind];
  $ev('ev-score-title').textContent = `Scoreboard · ${ev.kind}`;
  const d = k.decision;
  $ev('ev-decision').textContent = d ? (d.text || d.note || '') + (d.test_days != null ? ` (${d.test_days} test days)` : '') : '';
  let head, rows;
  if (run.test === 'A') {
    const sizes = ['small', 'medium', 'large'];
    head = '<th>Method</th><th class="r">Knob</th><th>Status</th>' + sizes.map(s => `<th class="r">Recall ${s}</th>`).join('')
      + '<th class="r">Delay median</th><th class="r">Delay p90</th>';
    rows = k.methods.map(m => {
      const r = m.recall || {}, dl = m.delay || {};
      return `<tr><td>${escapeHtml(m.method)}</td><td class="r">${evKnob(m.knob)}</td><td>${escapeHtml(m.status)}</td>`
        + sizes.map(s => `<td class="r">${evPct(r[s])}</td>`).join('')
        + `<td class="r">${evNum(dl.median, 1)}</td><td class="r">${evNum(dl.p90, 1)}</td></tr>`;
    }).join('');
  } else {
    head = '<th>Method</th><th class="r">Knob</th><th>Verdict</th><th class="r">Ratio vs σ (90% CI)</th><th class="r">Coverage</th>'
      + '<th class="r">Interval score</th><th class="r">Sharpness</th><th class="r">MASE</th><th class="r">Alert rate</th><th class="r">Flap share</th>'
      + EV_GATES.map(g => `<th>${g.replace('_', ' ')}</th>`).join('');
    rows = k.methods.map(m => {
      if (m.status !== 'ok') return `<tr><td>${escapeHtml(m.method)}</td><td colspan="${9 + EV_GATES.length}" class="ev-dim">${escapeHtml(m.status)}</td></tr>`;
      const ci = m.ci90 || {}, g = m.gates || {};
      return `<tr><td>${escapeHtml(m.method)}${m.method === 'sigma' ? ' <span class="badge badge-gray">baseline</span>' : ''}</td><td class="r">${evKnob(m.knob)}</td>`
        + `<td>${escapeHtml(m.verdict || '—')}</td>${evCi(ci.ratio, m.ratio_vs_sigma)}<td class="r">${evPct(m.coverage)}</td>`
        + `<td class="r">${evNum(m.interval_score)}</td><td class="r">${evNum(m.sharpness)}</td><td class="r">${evNum(m.mase, 3)}</td>`
        + `<td class="r">${evNum(m.alert_rate, 3)}</td><td class="r">${evPct(m.flap_share)}</td>`
        + EV_GATES.map(n => { const [t, c] = EV_GATE[g[n]] || EV_GATE.not_checked; return `<td><span class="badge ${c}">${t}</span></td>`; }).join('') + '</tr>';
    }).join('');
  }
  $ev('ev-score').innerHTML = `<thead><tr>${head}</tr></thead><tbody>${rows || '<tr><td>No methods.</td></tr>'}</tbody>`;
}

// ── Calibrate ──
const evSaved = c => `Saved: ${c.kind} uses ${c.method} at knob ${evKnob(c.knob)} (run #${c.run_id})${c.note ? ` — ${c.note}` : ''}.`;
function evRenderCal() {
  const sweep = ev.sweep || {}, methods = Object.keys(sweep);
  $ev('ev-cal-title').textContent = `Calibrate · ${ev.kind}`;
  $ev('ev-cal-method').innerHTML = methods.map(m => `<option>${escapeHtml(m)}</option>`).join('');
  if (ev.calMethod) $ev('ev-cal-method').value = ev.calMethod;
  const rows = sweep[ev.calMethod] || [];
  // The star is the saved choice when it is this run's and method's, else the run's own chosen knob.
  const saved = ev.choices[ev.kind], mine = saved && saved.run_id === ev.run && saved.method === ev.calMethod;
  const star = r => mine ? r.knob === saved.knob : r.chosen;
  const chosen = rows.find(star);
  if (ev.calKnob == null && chosen) ev.calKnob = chosen.knob;
  $ev('ev-pills').innerHTML = rows.map(r =>
    `<button type="button" class="toggle-btn${r.knob === ev.calKnob ? ' active' : ''}" aria-pressed="${r.knob === ev.calKnob}" data-knob="${r.knob}">${evKnob(r.knob)}${star(r) ? ' ★' : ''}</button>`).join('');
  $ev('ev-choose').disabled = !rows.length || ev.calKnob == null;
  $ev('ev-choice').textContent = saved ? evSaved(saved) : '';
  $ev('ev-choice').classList.remove('ins-toast-error');
  const el = $ev('ev-sweep');
  if (!rows.length) { echarts.getInstanceByDom(el)?.clear(); return; }
  const t = chartTokens(), pal = evColors(), cats = rows.map(r => evKnob(r.knob));
  const series = (name, f, color, axis) => [['test', 'solid'], ['tune', 'dashed']].map(([w, ls]) => ({
    name: `${name} (${w})`, type: 'line', xAxisIndex: axis, yAxisIndex: axis, symbolSize: 6,
    lineStyle: {type: ls, color}, itemStyle: {color}, data: rows.map(r => r[w] ? r[w][f] : null),
    markPoint: w === 'test' ? {symbol: 'pin', symbolSize: 28, label: {formatter: '★', color: '#fff'}, itemStyle: {color: t.status.ok},
      data: rows.map((r, i) => r.chosen && r.test && r.test[f] != null ? {coord: [i, r.test[f]], name: 'chosen'} : null).filter(Boolean)} : undefined}));
  const axis = (i, name, fmt) => ({gridIndex: i, type: 'value', name, nameTextStyle: {color: t.dim}, axisLabel: {color: t.dim, formatter: fmt}, splitLine: {lineStyle: {color: t.grid}}});
  const xax = i => ({gridIndex: i, type: 'category', data: cats, name: 'knob', nameLocation: 'middle', nameGap: 28, axisLabel: {color: t.dim}, axisLine: {lineStyle: {color: t.grid}}});
  chartFor(el).setOption({...baseOption(), color: [pal[0], pal[1]],
    tooltip: {...baseOption().tooltip, trigger: 'axis'},
    legend: {top: 0, textStyle: {color: t.dim}},
    grid: [{left: 56, right: '56%', top: 56, bottom: 44}, {left: '52%', right: 24, top: 56, bottom: 44}],
    xAxis: [xax(0), xax(1)], yAxis: [axis(0, 'coverage', v => `${Math.round(v * 100)}%`), axis(1, 'alert rate', null)],
    series: [...series('coverage', 'coverage', pal[0], 0), ...series('alert rate', 'alert_rate', pal[1], 1)]}, true);
}

// ── Fit chart ──
function evMethodsOfKey() {
  const k = ev.keys.find(x => `${x.service}\t${x.key}` === ev.key);
  return k ? k.methods : [];
}
function evRenderFitControls() {
  $ev('ev-key').innerHTML = ev.keys.map(k => `<option value="${escapeHtml(k.service)}\t${escapeHtml(k.key)}">${escapeHtml(k.service)} · ${escapeHtml(k.key)}</option>`).join('');
  if (ev.key) $ev('ev-key').value = ev.key;
  $ev('ev-from').value = ev.from; $ev('ev-to').value = ev.to;
  $ev('ev-mode').querySelectorAll('button').forEach(b => {
    const on = b.dataset.mode === ev.mode;
    b.classList.toggle('active', on); b.setAttribute('aria-pressed', String(on));
  });
  $ev('ev-methods').innerHTML = '<span class="filter-label">Methods</span>' + evMethodsOfKey().map(m => {
    const on = !ev.off.has(m);
    return `<button type="button" class="toggle-btn${on ? ' active' : ''}" aria-pressed="${on}" data-method="${escapeHtml(m)}">${escapeHtml(m)}</button>`;
  }).join('');
}

function evDefaultDays() {
  const run = ev.runs.find(r => r.id === ev.run);
  const to = new Date(run.window_to + 'T00:00:00Z');
  const from = new Date(Math.max(Date.parse(run.window_from + 'T00:00:00Z'), to.getTime() - 6 * 864e5));
  return [from.toISOString().slice(0, 10), run.window_to];
}

async function evLoadFit() {
  const seq = ++ev.fitSeq, st = $ev('ev-fit-status'), el = $ev('ev-fit');
  st.textContent = ''; st.classList.add('ins-toast-error');
  $ev('ev-fit-title').textContent = `Fit · ${ev.kind}`;
  if (!ev.keys.some(k => `${k.service}\t${k.key}` === ev.key)) ev.key = ev.keys[0] ? `${ev.keys[0].service}\t${ev.keys[0].key}` : null;
  if (!ev.from || !ev.to) [ev.from, ev.to] = evDefaultDays();
  evRenderFitControls();
  if (!ev.key) {
    echarts.getInstanceByDom(el)?.clear();
    st.classList.remove('ins-toast-error');
    st.textContent = `No drawn keys for ${ev.kind} in this run.`;
    return;
  }
  const [service, key] = ev.key.split('\t');
  const methods = evMethodsOfKey().filter(m => !ev.off.has(m));
  const base = `/api/eval/series?run=${ev.run}&kind=${ev.kind}&service=${encodeURIComponent(service)}&key=${encodeURIComponent(key)}&from=${ev.from}&to=${ev.to}`;
  let out;
  try {
    // One call per method, so the calibrated method alone can be drawn at the picked knob.
    out = await Promise.all(methods.map(async m => {
      const knob = ev.calMethod === m && ev.calKnob != null ? `&knob=${ev.calKnob}` : '';
      return [m, (await evGet(`${base}&methods=${encodeURIComponent(m)}${knob}`))[m]];
    }));
  } catch (err) {
    if (seq === ev.fitSeq) { echarts.getInstanceByDom(el)?.clear(); evError(st, err); }
    return;
  }
  if (seq !== ev.fitSeq) return;
  evDrawFit(out.filter(([, v]) => v));
}

function evDrawFit(data) {
  const el = $ev('ev-fit'), t = chartTokens(), pal = evColors(), chart = chartFor(el);
  if (!data.length) { chart.clear(); $ev('ev-fit-status').classList.remove('ins-toast-error'); $ev('ev-fit-status').textContent = 'No method selected.'; return; }
  const multi = ev.mode === 'multi', n = data.length, ph = 150;
  el.style.height = multi ? `${n * ph + 90}px` : '420px';
  chart.resize();
  const ms = ts => Date.parse(ts);
  const pt = (p, f) => [ms(p.t), p[f] == null ? null : p[f]];
  const grids = [], xs = [], ys = [], series = [], titles = [];
  const count = data[0][1].points;
  data.forEach(([m, v], i) => {
    const gi = multi ? i : 0, color = pal[i % pal.length];
    if (multi) {
      grids.push({left: 56, right: 24, top: 28 + i * ph, height: ph - 38});
      xs.push({gridIndex: i, type: 'time', axisLabel: {color: t.dim, show: i === n - 1}, splitLine: {show: false}, axisLine: {lineStyle: {color: t.grid}}});
      ys.push({gridIndex: i, type: 'value', axisLabel: {color: t.dim}, splitLine: {lineStyle: {color: t.grid}}});
      titles.push({text: `${m} · knob ${evKnob(v.knob)}`, left: 56, top: 6 + i * ph, textStyle: {color: t.text, fontSize: 12}});
    } else if (i === 0) {
      grids.push({left: 56, right: 24, top: 40, bottom: 70});
      xs.push({gridIndex: 0, type: 'time', axisLabel: {color: t.dim}, axisLine: {lineStyle: {color: t.grid}}});
      ys.push({gridIndex: 0, type: 'value', axisLabel: {color: t.dim}, splitLine: {lineStyle: {color: t.grid}}});
    }
    const common = {xAxisIndex: gi, yAxisIndex: gi, symbol: 'none', silent: true};
    const mid = v.points.map(p => pt(p, 'low'));
    series.push({...common, name: `${m} low`, type: 'line', stack: `band-${m}`, lineStyle: {opacity: 0}, data: mid, tooltip: {show: false}});
    series.push({...common, name: `${m} band`, type: 'line', stack: `band-${m}`, lineStyle: {opacity: 0},
      areaStyle: {color, opacity: multi ? 0.22 : 0.12}, data: v.points.map(p => [ms(p.t), p.high == null || p.low == null ? null : p.high - p.low]), tooltip: {show: false}});
    series.push({...common, name: `${m} expected`, type: 'line', lineStyle: {color, type: 'dashed', width: 1.2}, itemStyle: {color}, data: v.points.map(p => pt(p, 'expected'))});
    if (multi || i === 0) series.push({...common, name: multi ? `${m} count` : 'count', type: 'line', lineStyle: {color: t.line, width: 1.2}, itemStyle: {color: t.line},
      data: (multi ? v.points : count).map(p => pt(p, 'count')),
      markArea: {silent: true, itemStyle: {color: t.status.bad, opacity: 0.12}, data: (v.episodes || []).map(e => [{xAxis: ms(e.start)}, {xAxis: ms(e.end)}])}});
    else series.push({...common, name: `${m} episodes`, type: 'line', data: [], markArea: {silent: true, itemStyle: {color, opacity: 0.1}, data: (v.episodes || []).map(e => [{xAxis: ms(e.start)}, {xAxis: ms(e.end)}])}});
    series.push({xAxisIndex: gi, yAxisIndex: gi, name: `${m} flagged`, type: 'scatter', symbol: 'diamond', symbolSize: 7, itemStyle: {color: t.status.bad},
      data: v.points.filter(p => p.flag).map(p => pt(p, 'count'))});
  });
  chart.setOption({...baseOption(), color: pal, title: multi ? titles : [],
    tooltip: {...baseOption().tooltip, trigger: 'axis'},
    legend: multi ? {show: false} : {top: 0, textStyle: {color: t.dim}, data: ['count', ...data.map(([m]) => `${m} expected`)]},
    grid: grids, xAxis: xs, yAxis: ys, series,
    dataZoom: [{type: 'inside', xAxisIndex: xs.map((_, i) => i)}, {type: 'slider', xAxisIndex: xs.map((_, i) => i), bottom: 8, height: 22}]}, true);
}

// ── Events ──
$ev('ev-run').addEventListener('change', e => { ev.run = Number(e.target.value); ev.from = ev.to = ''; ev.off = new Set(); evLoadRun(); });
$ev('ev-boards').addEventListener('click', e => {
  const b = e.target.closest('[data-board]');
  if (b) { ev.board = b.dataset.board; evRenderBoard(); if (ev.board === 'cal') evRenderCal(); }
});
$ev('ev-cards').addEventListener('click', async e => {
  const b = e.target.closest('[data-kind]');
  if (!b || b.dataset.kind === ev.kind) return;
  const prev = {kind: ev.kind, key: ev.key, off: ev.off};
  ev.kind = b.dataset.kind; ev.key = null; ev.off = new Set();
  evRenderCards(); evRenderScore();
  // A failed keys/sweep fetch keeps the old kind (its keys, sweep and fit chart are still on screen).
  if (await evLoadKind() === false) { Object.assign(ev, prev); evRenderCards(); evRenderScore(); }
});
$ev('ev-key').addEventListener('change', e => { ev.key = e.target.value; evLoadFit(); });
$ev('ev-from').addEventListener('change', e => { ev.from = e.target.value; evLoadFit(); });
$ev('ev-to').addEventListener('change', e => { ev.to = e.target.value; evLoadFit(); });
$ev('ev-mode').addEventListener('click', e => { const b = e.target.closest('[data-mode]'); if (b) { ev.mode = b.dataset.mode; evLoadFit(); } });
$ev('ev-methods').addEventListener('click', e => {
  const b = e.target.closest('[data-method]');
  if (!b) return;
  ev.off.has(b.dataset.method) ? ev.off.delete(b.dataset.method) : ev.off.add(b.dataset.method);
  evLoadFit();
});
$ev('ev-cal-method').addEventListener('change', e => { ev.calMethod = e.target.value; ev.calKnob = null; evRenderCal(); evLoadFit(); });
$ev('ev-pills').addEventListener('click', e => {
  const b = e.target.closest('[data-knob]');
  if (!b) return;
  ev.calKnob = Number(b.dataset.knob); evRenderCal(); evLoadFit();
});
$ev('ev-choose').addEventListener('click', async () => {
  const out = $ev('ev-choice'), btn = $ev('ev-choose');
  btn.disabled = true;
  try {
    const r = await apiMutation('/api/eval/choices', 'POST', {kind: ev.kind, method: ev.calMethod, knob: ev.calKnob, run_id: ev.run, note: $ev('ev-note').value});
    ev.choices[r.kind] = r;
    evRenderCal();                                  // moves the star to the saved knob and shows the saved line
  } catch (err) {
    out.classList.add('ins-toast-error');
    out.textContent = err.message;
  } finally { btn.disabled = ev.calKnob == null || !(ev.sweep[ev.calMethod] || []).length; }
});
