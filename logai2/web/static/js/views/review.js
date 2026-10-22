// ── Review: decide which hours stay in the baseline ──
// A days x hours heatmap of one key against its baseline (colours come from the API's `cls`),
// an inspector for the selected hours, and the coverage the baseline is built from.
// Decisions are used by the next daily commit or by "Recompute baseline".
const rv = {service: '', key: '*total', days: 14, sel: null, view: 'chart', lastSub: null,
            matrix: null, base: null, insp: null, cells: new Map(), rows: [], index: new Map(), cur: null,
            seq: 0, inspSeq: 0, commandId: null, pollTimer: null, docs: [], templates: [], tplService: null};
const rvPicker = createKeyPicker(document.getElementById('rv-picker'), {onPick: key => { rv.key = key; rvChangeRoute(); }});
const $rv = id => document.getElementById(id);
const RV_DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const RV_MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const rvPad = h => String(h).padStart(2, '0');
const rvHour = h => `${rvPad(h)}:00`;
const rvHours = (from, to) => `${rvHour(from)}–${rvHour(to)}`;
const rvN = n => n == null ? '—' : (Math.round(n * 10) / 10).toLocaleString();
const rvZ = z => z == null ? 'n/a' : `${z >= 0 ? '+' : ''}${z.toFixed(1)}`;
function rvDayLabel(iso) {
  const d = new Date(`${iso}T00:00:00`);
  return `${RV_DOW[d.getDay()]} ${d.getDate()} ${RV_MON[d.getMonth()]}`;
}
const rvWeekend = row => row.daytype === 'weekend';
// Text colour that reads on a hex fill.
function rvInk(fill) {
  const m = /^#([0-9a-f]{6})$/i.exec(fill || '');
  if (!m) return chartTokens().text;
  const [r, g, b] = [0, 2, 4].map(i => parseInt(m[1].slice(i, i + 2), 16));
  return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? '#0f172a' : '#ffffff';
}
function rvStatus(message, error = false) {
  $rv('rv-status').textContent = message;
  $rv('rv-status').classList.toggle('ins-toast-error', error);
}
function rvCard(label, value, sub, tone, attrs = '') {
  const inner = `<div><div class="stat-label">${escapeHtml(label)}</div>
    <div class="stat-value">${escapeHtml(value)}</div>
    ${sub ? `<div class="pl-sub">${escapeHtml(sub)}</div>` : ''}</div>`;
  return attrs ? `<button type="button" class="stat-card${tone ? ` pl-${tone}` : ''}" ${attrs}>${inner}</button>`
    : `<div class="stat-card${tone ? ` pl-${tone}` : ''}">${inner}</div>`;
}

// ── Route: #review/<service>/<key>/<days>[/<day>/<hour_from>/<hour_to>] ──
function rvApplyRoute() {
  const sub = state.subView || '';
  if (sub === rv.lastSub) return;
  rv.lastSub = sub;
  let p;
  try { p = sub.split('/').filter(Boolean).map(decodeURIComponent); } catch (e) { return; }   // a malformed tail: keep the defaults
  if (p[0]) rv.service = p[0];
  if (p[1]) rv.key = p[1];
  if (p[2] === '14' || p[2] === '28') rv.days = Number(p[2]);
  rv.sel = null;
  const from = Number(p[4]), to = Number(p[5]);
  if (p.length >= 6 && /^\d{4}-\d\d-\d\d$/.test(p[3]) && !Number.isNaN(Date.parse(p[3]))
      && Number.isInteger(from) && Number.isInteger(to) && from >= 0 && from < to && to <= 24) {
    rv.sel = {day: p[3], from, to};
  }
}
function rvWriteRoute() {
  const enc = encodeURIComponent;
  let sub = `${enc(rv.service)}/${enc(rv.key)}/${rv.days}`;
  if (rv.sel) sub += `/${rv.sel.day}/${rv.sel.from}/${rv.sel.to}`;
  rv.lastSub = state.subView = sub;
  if (state.activeView === 'review') history.replaceState(null, '', `#review/${sub}`);
}

// ── Load ──
async function loadReview() {
  rvApplyRoute();
  const seq = ++rv.seq;
  const services = await getJson('/api/services', 'Unable to load services');
  if (seq !== rv.seq) return;
  if (!services.includes(rv.service)) rv.service = services[0] || '';
  $rv('rv-service').innerHTML = services.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
  $rv('rv-service').value = rv.service;
  if (!rv.service) {
    $rv('rv-cards').innerHTML = '';
    $rv('rv-review-list').innerHTML = '<p class="sync-status">No services yet: the engine adds them as it reads logs.</p>';
    return;
  }
  const svc = encodeURIComponent(rv.service);
  if (rv.tplService !== rv.service) {
    const t = await getJson(`/api/templates?service=${svc}&size=500`, 'Unable to load templates');
    rv.tplService = rv.service;
    rv.templates = t.items;
  }
  rv.docs = await getJson('/api/documents', 'Unable to load documents');
  rvRenderControls();
  let matrixData, base;
  try {
    [matrixData, base] = await Promise.all([
      getJson(`/api/baseline/matrix?service=${svc}&key=${encodeURIComponent(rv.key)}&days=${rv.days}`, 'Unable to load the matrix'),
      getJson(`/api/baseline?service=${svc}`, 'Unable to load baseline')]);
  } catch (err) {
    if (seq === rv.seq) rvShowLoadError(`${err.message} for "${rv.key}".`);
    return;
  }
  if (seq !== rv.seq) return;
  rvStatus('');
  rv.matrix = matrixData; rv.base = base;
  rvIndexMatrix();
  rvRenderAll();
}

// A failed load must not leave the previous key's data on screen: nothing may be decided on it.
function rvShowLoadError(message) {
  rv.matrix = rv.base = rv.insp = null; rv.sel = null; rv.cells = new Map(); rv.rows = []; rv.index = new Map();
  rvWriteRoute();
  rvStatus(message, true);
  $rv('rv-inspector').hidden = true;
  ['rv-matrix', 'rv-coverage'].forEach(id => echarts.getInstanceByDom($rv(id))?.clear());
  $rv('rv-cards').innerHTML = '';
  $rv('rv-legend').innerHTML = '';
  $rv('rv-matrix-table').innerHTML = '';
  $rv('rv-review-count').textContent = '';
  $rv('rv-review-list').innerHTML = '<p class="sync-status">Nothing to show: the data did not load.</p>';
  $rv('rv-decision-list').innerHTML = '';
  $rv('rv-cov-summary').textContent = '';
}

function rvRenderControls() {
  rvPicker.render({service: rv.service, key: rv.key, templates: rv.templates, docs: rv.docs, baselineState: rv.matrix?.profiles?.weekday});
  $rv('rv-days').querySelectorAll('button').forEach(b => {
    const on = Number(b.dataset.days) === rv.days;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

function rvIndexMatrix() {
  rv.cells = new Map(); rv.rows = rv.matrix.days; rv.index = new Map();
  let i = 0;
  rv.rows.forEach((row, y) => row.hours.forEach(c => {
    rv.cells.set(`${row.day}|${c.hour}`, {...c, row, y});
    rv.index.set(`${y}|${c.hour}`, i++);
  }));
}

function rvRenderAll() {
  rvRenderControls();   // the picker's young-baseline note needs the matrix just loaded
  rvRenderCards();
  rvRenderLegend();
  rvRenderMatrix();
  rvRenderLists();
  rvRenderCoverage();
  if (rv.sel) loadInspector(); else $rv('rv-inspector').hidden = true;
}

function rvProfileText(type) {
  const profile = rv.matrix.profiles[type], k = rv.matrix.k[type];
  const of = (rv.matrix.coverage[type][0] || {}).of;
  const n = rv.base.days.filter(d => d.committed && d.daytype === type).length;
  const used = of ? Math.min(n, of) : n;
  const text = {
    none: 'No days yet: floor-only alerts',
    borrowed: 'Borrows weekday, upper alerts only',
    young: `Young: mean ± ${k}σ, ${used} of ${rv.base.full_after_days[type]} days`,
    full: `Full: mean ± ${k}σ`,
  }[profile];
  return [text, profile === 'full' || profile === 'borrowed' ? '' : 'warn'];
}

function rvRenderCards() {
  const [wd, wdTone] = rvProfileText('weekday'), [we, weTone] = rvProfileText('weekend');
  const last = rv.base.last_commit, n = rv.base.suggestions.length;
  $rv('rv-cards').innerHTML = [
    rvCard('Weekday profile', rv.matrix.profiles.weekday, wd, wdTone),
    rvCard('Weekend profile', rv.matrix.profiles.weekend, we, weTone),
    rvCard('To review', String(n), n ? 'Open the first one' : 'Unreviewed hours are kept', '', n ? 'id="rv-first"' : 'disabled'),
    rvCard('Last commit', last ? new Date(last.at).toLocaleString() : 'Never', last ? `Owner day ${last.today} · next at the daily commit` : 'Runs at the daily commit'),
  ].join('');
  const first = $rv('rv-first');
  if (first) first.addEventListener('click', () => rvSelectSuggestion(rvSuggestions()[0]));
}

function rvRenderLegend() {
  const t = chartTokens(), k = rv.matrix.k.weekday;
  const items = [['none', 'no baseline'], ['in', `inside ±${k}σ`], ['up1', `above, to 6σ`], ['up2', '6–10σ'], ['up3', '> 10σ'],
                 ['down1', 'below, to 6σ'], ['down2', '6–10σ'], ['down3', '> 10σ']];
  const drops = rv.key === '*total';
  $rv('rv-legend').innerHTML = items.filter(([c]) => drops || !c.startsWith('down'))
    .map(([c, label]) => `<span><i style="background:${t.cls[c]}"></i>${label}</span>`).join('')
    + (drops ? '' : '<span>(below the band is never coloured for this key)</span>');
}

// ── Matrix ──
function rvSuggestions() {
  return [...rv.base.suggestions].sort((a, b) => b.day.localeCompare(a.day) || b.hour_from - a.hour_from);
}
function rvSelected(day, h) { return rv.sel && rv.sel.day === day && h >= rv.sel.from && h < rv.sel.to; }

function rvRenderMatrix() {
  const t = chartTokens();
  const hasData = rv.cells.size > 0;
  $rv('rv-matrix-wrap').style.display = rv.view === 'chart' ? '' : 'none';
  $rv('rv-matrix-table').hidden = rv.view !== 'table';
  rvRenderTable();
  if (rv.view !== 'chart') return;
  $rv('rv-matrix').style.height = `${70 + rv.rows.length * 28}px`;
  const data = [];
  rv.cells.forEach(c => {
    const fill = t.cls[c.cls] || t.cls.none;
    const mark = c.decision ? (c.decision === 'exclude' ? '✕' : '✓') : c.suggested ? '▲' : '';
    const sel = rvSelected(c.row.day, c.hour);
    // ponytail: browser clock hour on the newest row (the server's today); differs only if the zones differ
    const now = c.y === 0 && c.hour === new Date().getHours();
    const item = {
      value: [c.hour, c.y, c.z ?? 0], cell: c, mark,   // a null value would hide the cell
      itemStyle: {color: fill, borderColor: sel ? t.accent : (now || c.cls === 'none' ? t.dim : t.card), borderWidth: sel ? 3 : now ? 2 : c.cls === 'none' ? 1 : 1.5,
                  opacity: c.decision === 'exclude' ? 0.6 : 1},
      label: {color: rvInk(fill)},
    };
    if (c.decision === 'exclude') item.itemStyle.decal = {symbol: 'rect', dashArrayX: [1, 0], dashArrayY: [2, 4], rotation: Math.PI / 4, color: 'rgba(0,0,0,0.35)'};
    data.push(item);
  });
  const weekendOf = Object.fromEntries(rv.rows.map(r => [rvLabel(r), rvWeekend(r)]));
  chartFor($rv('rv-matrix')).setOption({
    ...baseOption(),
    grid: {left: 96, right: 8, top: 22, bottom: 8},
    xAxis: {type: 'category', position: 'top', data: [...Array(24).keys()].map(rvPad), splitLine: {show: false},
            axisLine: {show: false}, axisTick: {show: false}, axisLabel: {color: t.dim}},
    yAxis: {type: 'category', inverse: true, data: rv.rows.map(rvLabel), splitLine: {show: false},
            axisLine: {show: false}, axisTick: {show: false},
            axisLabel: {color: t.dim, fontSize: 11.5, formatter: v => weekendOf[v] ? `{we|${v}}` : v,
                        rich: {we: {color: t.muted, fontStyle: 'italic', fontSize: 11.5}}}},
    tooltip: {...baseOption().tooltip, confine: true, formatter: p => rvCellTip(p.data.cell)},
    series: [{type: 'heatmap', data, progressive: 0,
              label: {show: true, formatter: p => p.data.mark, fontSize: 12, fontWeight: 700},
              emphasis: {itemStyle: {borderColor: t.accent, borderWidth: 2}}}],
  }, true);
  if (!hasData) rvStatus('No hours read for this service yet.');
  rvBindMatrix();
}
const rvLabel = row => `${row.in_baseline ? '● ' : ''}${rvDayLabel(row.day)}`;

function rvCellTip(c) {
  const head = `<b>${rvDayLabel(c.row.day)} ${rvHours(c.hour, c.hour + 1)}</b> · ${escapeHtml(rvKeyName())}`;
  const status = c.decision ? (c.decision === 'exclude' ? 'excluded' : 'kept')
    : c.suggested ? `<b>needs review</b> · ${c.not_normal_minutes} min not NORMAL` : 'not reviewed';
  let body;
  if (c.no_baseline) {
    body = `${rvN(c.rate)}/min · no baseline for this hour${c.rate > 0 && c.cls === 'up3' ? ': no normal traffic at this hour' : ''}`;
  } else {
    body = `${rvN(c.rate)}/min · expected ${rvN(c.mean)} ± ${rvN(c.std)} (≤ ${rvN(c.band_high)}) · z <b>${rvZ(c.z)}</b>`
      + (c.std === 0 && c.mean > 0 ? '<br>σ is 0: z uses a floor of 1 event/min' : '');
  }
  return `${head}<br>${body}<br>${status}${c.row.in_baseline ? '<br>this day is in the next baseline' : ''}`;
}
const rvKeyName = () => keyLabel(rv.key, rv.templates, rv.docs);

function rvRenderTable() {
  if (rv.view !== 'table') return;
  const hours = [...Array(24).keys()];
  $rv('rv-matrix-table').innerHTML = `<table><thead><tr><th>Day</th>${hours.map(h => `<th class="r">${rvPad(h)}</th>`).join('')}</tr></thead><tbody>${
    rv.rows.map(r => `<tr><th scope="row">${escapeHtml(rvLabel(r))}</th>${hours.map(h => {
      const c = rv.cells.get(`${r.day}|${h}`);
      const mark = c ? (c.decision ? (c.decision === 'exclude' ? ' ✕' : ' ✓') : c.suggested ? ' ▲' : '') : '';
      return `<td class="r" title="${c ? escapeHtml(`${rvN(c.rate)}/min`) : ''}">${c ? (c.z == null ? '—' : rvZ(c.z)) + mark : ''}</td>`;
    }).join('')}</tr>`).join('')}</tbody></table>`;
}

function rvSelectAt(day, hour, extend) {
  const c = rv.cells.get(`${day}|${hour}`);
  if (extend && rv.sel && rv.sel.day === day) {
    rv.sel = {day, from: Math.min(rv.sel.from, hour), to: Math.max(rv.sel.to, hour + 1)};
  } else if (c && c.suggested) {
    const s = rv.base.suggestions.find(x => x.day === day && x.hour_from <= hour && hour < x.hour_to);
    rv.sel = s ? {day, from: s.hour_from, to: s.hour_to} : {day, from: hour, to: hour + 1};
  } else {
    rv.sel = {day, from: hour, to: hour + 1};
  }
  rvSelectionChanged();
}
function rvSelectSuggestion(s) {
  if (!s) return;
  rv.sel = {day: s.day, from: s.hour_from, to: s.hour_to};
  rvSelectionChanged();
}
function rvSelectionChanged() {
  rv.note = null; rvDecisionNote('');
  rvWriteRoute();
  rvRenderMatrix(); rvRenderLists(); loadInspector();
  $rv('rv-inspector').scrollIntoView({block: 'nearest', behavior: REDUCED_MOTION ? 'auto' : 'smooth'});
}

function rvBindMatrix() {
  const chart = chartFor($rv('rv-matrix'));
  chart.off('click');
  chart.on('click', p => rvSelectAt(p.data.cell.row.day, p.data.cell.hour, Boolean(p.event && p.event.event && p.event.event.shiftKey)));
}

// Keyboard: arrows move a highlighted cell (with its tooltip), Enter selects, Shift+arrows extend.
$rv('rv-matrix').addEventListener('keydown', event => {
  const step = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]}[event.key];
  const chart = echarts.getInstanceByDom($rv('rv-matrix'));
  if (!chart || !rv.rows.length) return;
  if (event.key === 'Enter' && rv.cur) {
    event.preventDefault();
    rvSelectAt(rv.rows[rv.cur.y].day, rv.cur.h, event.shiftKey);
    return;
  }
  if (!step) return;
  event.preventDefault();
  const cur = rv.cur || {y: 0, h: 0};
  const y = Math.max(0, Math.min(rv.rows.length - 1, cur.y + step[1])), h = Math.max(0, Math.min(23, cur.h + step[0]));
  if (rv.cur) chart.dispatchAction({type: 'downplay', seriesIndex: 0, dataIndex: rv.index.get(`${rv.cur.y}|${rv.cur.h}`)});
  rv.cur = {y, h};
  const dataIndex = rv.index.get(`${y}|${h}`);
  if (dataIndex == null) return;   // no minutes read for that hour
  chart.dispatchAction({type: 'highlight', seriesIndex: 0, dataIndex});
  chart.dispatchAction({type: 'showTip', seriesIndex: 0, dataIndex});
  if (event.shiftKey && rv.sel && rv.sel.day === rv.rows[y].day) rvSelectAt(rv.rows[y].day, h, true);
});

// ── To review / Decisions ──
function rvRenderLists() {
  const sugg = rvSuggestions();
  $rv('rv-review-count').textContent = `(${sugg.length})`;
  $rv('rv-review-list').innerHTML = sugg.length ? sugg.map((s, i) => {
    let peak = null;
    for (let h = s.hour_from; h < s.hour_to; h++) {
      const c = rv.cells.get(`${s.day}|${h}`);
      if (c && c.z != null && (peak == null || Math.abs(c.z) > Math.abs(peak))) peak = c.z;
    }
    const cur = rv.sel && rv.sel.day === s.day && rv.sel.from === s.hour_from && rv.sel.to === s.hour_to;
    return `<button type="button" class="rv-item" data-i="${i}" aria-current="${Boolean(cur)}">
      <span><b>${rvDayLabel(s.day)}</b> ${rvHours(s.hour_from, s.hour_to)}</span>
      <span class="sync-status">${s.keys.length} key${s.keys.length === 1 ? '' : 's'}${peak == null ? '' : ` · peak z ${rvZ(peak)}`}</span></button>`;
  }).join('') : '<p class="sync-status">Nothing to review: every non-NORMAL hour has a decision.</p>';
  $rv('rv-review-list').querySelectorAll('[data-i]').forEach(b => b.addEventListener('click', () => rvSelectSuggestion(sugg[Number(b.dataset.i)])));
  $rv('rv-decision-list').innerHTML = rv.base.decisions.length ? rv.base.decisions.map(d => `
    <div class="rv-item rv-row"><span><span class="badge ${d.decision === 'exclude' ? 'badge-red' : 'badge-green'}">${d.decision === 'exclude' ? '✕ excluded' : '✓ kept'}</span>
      ${rvDayLabel(d.day)} ${rvHours(d.hour_from, d.hour_to)}</span>
      <button type="button" class="btn rv-undo" data-id="${Number(d.id)}">Undo</button></div>`).join('')
    : '<p class="sync-status">No decisions yet.</p>';
}
$rv('rv-decision-list').addEventListener('click', event => {
  const b = event.target.closest('.rv-undo');
  if (b) rvUndo(Number(b.dataset.id));
});

// ── Inspector ──
function rvDecisionFor(sel) {
  return rv.base.decisions.find(d => d.day === sel.day && d.hour_from < sel.to && d.hour_to > sel.from);
}
async function loadInspector() {
  const seq = ++rv.inspSeq, sel = rv.sel;
  const q = new URLSearchParams({service: rv.service, key: rv.key, day: sel.day, hour_from: sel.from, hour_to: sel.to});
  let data;
  try { data = await getJson(`/api/baseline/inspect?${q}`, 'Unable to load the selection'); }
  catch (err) { if (seq === rv.inspSeq) rvStatus(err.message, true); return; }
  if (seq !== rv.inspSeq) return;
  rv.insp = data;
  $rv('rv-inspector').hidden = false;
  rvRenderInspector(sel, data);
}

function rvRenderInspector(sel, d) {
  const row = rv.rows.find(r => r.day === sel.day);
  const s = d.sentence, name = rvKeyName();
  $rv('rv-insp-title').textContent = `${rvDayLabel(sel.day)}, ${rvHours(sel.from, sel.to)} · ${rv.service} · ${name}`;
  const decided = rvDecisionFor(sel);
  $rv('rv-sentence').innerHTML = (s.keys ? `<b>${s.keys} key${s.keys === 1 ? '' : 's'} left NORMAL</b> for ${s.not_normal_minutes} min. ` : 'Nothing left NORMAL. ')
    + (s.peak ? `${escapeHtml(name)} ${s.peak.z != null && s.peak.z < 0 ? 'fell to' : 'peaked at'} <b>${rvN(s.peak.rate)}/min</b>`
      + (s.expected ? ` against ${rvN(s.expected.mean)} ± ${rvN(s.expected.std)} expected (z ${rvZ(s.peak.z)}). ` : '. ') : '')
    + (decided ? (decided.decision === 'exclude' ? 'Excluded from the baseline.' : 'Kept in the baseline.')
      : s.keys ? 'Not reviewed. ' : '')
    + (!decided && s.in_baseline ? 'This day is in the next baseline.' : '');
  rvRenderBounds(sel, d);
  rvRenderIncreased(d);
  $rv('rv-new-tbody').innerHTML = d.new.length ? d.new.map(n => `<tr>
    <td><span class="tmpl-id">${escapeHtml(n.template_id)}</span><div class="doc-text">${escapeHtml(n.text)}</div></td>
    <td>${escapeHtml(n.level)}</td><td title="${escapeHtml(formatFullTs(n.first_seen))}">${escapeHtml(formatTs(n.first_seen))}</td>
    <td class="tpl-num">${rvN(n.rate)}/min</td><td>${n.doc_id ? escapeHtml(n.doc_id) : '<span class="badge badge-gray">Not linked</span>'}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty-state">No template first seen in this range.</td></tr>';
  $rv('rv-chips').innerHTML = d.keys.length ? d.keys.map(k => `<span class="rv-chip" title="${escapeHtml(k.periods.map(p =>
    `${new Date(p.from * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}–${new Date(p.to * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})} ${p.state}`).join(', '))}">${escapeHtml(k.key)}</span>`).join('')
    : '<span class="sync-status">none</span>';
  rvRenderEffect(sel, d, row, decided);
}

function rvRenderBounds(sel, d) {
  const t = chartTokens();
  const hours = d.day.map(r => `${rvPad(r.hour)}:00`);
  const lo = d.day.map(r => r.band_low == null ? null : Math.max(0, r.band_low));
  const hi = d.day.map((r, i) => r.band_high == null ? null : r.band_high - lo[i]);
  const outOfBand = d.day.map(r => {
    const c = rv.cells.get(`${sel.day}|${r.hour}`);
    return c && /^(up|down)/.test(c.cls) ? r.rate : null;
  });
  chartFor($rv('rv-bounds')).setOption({
    ...baseOption(),
    grid: {left: 56, right: 16, top: 30, bottom: 28},
    legend: {top: 0, textStyle: {color: t.dim}, selectedMode: true, data: ['Expected band', 'Same day last week', 'This day']},
    xAxis: {type: 'category', data: hours, boundaryGap: false, axisLabel: {color: t.dim}, axisLine: {lineStyle: {color: t.grid}}},
    yAxis: {type: 'value', name: 'events/min', nameTextStyle: {color: t.muted}, axisLabel: {color: t.dim}, splitLine: {lineStyle: {color: t.grid}}},
    tooltip: {...baseOption().tooltip, trigger: 'axis', confine: true, formatter: ps => {
      const r = d.day[ps[0].dataIndex];
      return `<b>${rvHour(r.hour)}</b><br>this day <b>${rvN(r.rate)}</b>/min<br>expected ${r.band_high == null ? 'no baseline' : `${rvN(r.band_low)}–${rvN(r.band_high)}`}`
        + `<br>last week ${rvN(r.last_week)}${r.not_normal_minutes ? `<br>${r.not_normal_minutes} min not NORMAL` : ''}`;
    }},
    series: [
      {name: 'lower', type: 'line', data: lo, stack: 'band', symbol: 'none', lineStyle: {opacity: 0}, areaStyle: {opacity: 0}, silent: true, tooltip: {show: false}},
      {name: 'Expected band', type: 'line', data: hi, stack: 'band', symbol: 'none', lineStyle: {opacity: 0}, areaStyle: {color: t.band}, itemStyle: {color: t.lastweek}, silent: true},
      {name: 'Same day last week', type: 'line', data: d.day.map(r => r.last_week), symbol: 'none', lineStyle: {type: 'dashed', width: 1.5, color: t.lastweek}, itemStyle: {color: t.lastweek}},
      {name: 'This day', type: 'line', data: d.day.map(r => r.rate), symbol: 'circle', symbolSize: 5, lineStyle: {width: 2, color: t.line}, itemStyle: {color: t.line},
       markArea: {silent: true, itemStyle: {color: 'transparent', borderColor: t.accent, borderWidth: 2},
                  data: [[{xAxis: `${rvPad(sel.from)}:00`}, {xAxis: `${rvPad(Math.max(sel.from, sel.to - 1))}:00`}]]}},
      {name: 'Outside the band', type: 'scatter', data: outOfBand, symbolSize: 11, itemStyle: {color: t.cls.up2, borderColor: t.card, borderWidth: 2}, z: 5, tooltip: {show: false}},
      {name: 'Not NORMAL', type: 'scatter', data: d.day.map(r => r.not_normal_minutes ? 0 : null), symbol: 'triangle', symbolSize: 9, itemStyle: {color: t.cls.up2}, silent: true, tooltip: {show: false}},
    ],
  }, true);
}

function rvRenderIncreased(d) {
  const t = chartTokens(), inc = d.increased;
  $rv('rv-increased').style.height = `${Math.max(60, inc.length * 26 + 10)}px`;
  const short = id => id.slice(id.indexOf(':') + 1);
  chartFor($rv('rv-increased')).setOption({
    ...baseOption(),
    grid: {left: 130, right: 90, top: 4, bottom: 4},
    xAxis: {type: 'value', show: false},
    yAxis: {type: 'category', inverse: true, data: inc.map(r => short(r.template_id)), axisLine: {show: false}, axisTick: {show: false},
            axisLabel: {color: t.dim, fontSize: 11.5, formatter: (v, i) => `${inc[i].level.padEnd(5)} ${v}`}},
    tooltip: {...baseOption().tooltip, confine: true, formatter: p => {
      const r = inc[p.dataIndex];
      return `<b>${escapeHtml(r.template_id)}</b> ${escapeHtml(r.level)}<br>${escapeHtml(r.text)}<br>${rvN(r.rate)}/min vs ${rvN(r.mean)} normal`
        + `<br>document: ${r.doc_id ? escapeHtml(r.doc_id) : 'not linked'}<br><i>click: open in Templates</i>`;
    }},
    series: [{type: 'bar', data: inc.map(r => r.excess), barWidth: 14, itemStyle: {color: t.cls.up2, borderRadius: [0, 4, 4, 0]},
              label: {show: true, position: 'right', color: t.dim, fontSize: 11.5,
                      formatter: p => inc[p.dataIndex].times_normal == null ? 'new vs 0' : `×${inc[p.dataIndex].times_normal} normal`}}],
  }, true);
  const chart = chartFor($rv('rv-increased'));
  chart.off('click');
  chart.on('click', p => rvOpenTemplate(inc[p.dataIndex]));
  $rv('rv-increased-tbody').innerHTML = inc.length ? inc.map((r, i) => `<tr>
    <td><button type="button" class="rv-link" data-i="${i}"><span class="tmpl-id">${escapeHtml(r.template_id)}</span></button><div class="doc-text">${escapeHtml(r.text)}</div></td>
    <td>${escapeHtml(r.level)}</td><td class="tpl-num">${rvN(r.rate)}/min</td><td class="tpl-num">${rvN(r.mean)}/min</td>
    <td class="tpl-num">${r.times_normal == null ? '—' : `×${r.times_normal}`}</td><td class="tpl-num">${r.not_normal_minutes}</td>
    <td>${r.doc_id ? escapeHtml(r.doc_id) : '<span class="badge badge-gray">Not linked</span>'}</td></tr>`).join('')
    : '<tr><td colspan="7" class="empty-state">No template rose above its normal in this range.</td></tr>';
}
$rv('rv-increased-tbody').addEventListener('click', event => {
  const b = event.target.closest('.rv-link');
  if (b && rv.insp) rvOpenTemplate(rv.insp.increased[Number(b.dataset.i)]);
});

// Open the Templates page filtered to this template (it searches template text).
function rvOpenTemplate(item) {
  if (!item) return;
  setTplMode('all');
  tpl.search = item.text; $tplSearch.value = item.text;
  tpl.service = rv.service;
  $tplService.innerHTML = `<option value="">All services</option><option value="${escapeHtml(rv.service)}">${escapeHtml(rv.service)}</option>`;
  $tplService.value = rv.service;
  setView('templates');
}

function rvRenderEffect(sel, d, row, decided) {
  if (rv.note) rvDecisionNote(rv.note.text, rv.note.error);
  const effect = $rv('rv-effect');
  $rv('rv-exclude').hidden = $rv('rv-keep').hidden = Boolean(decided);
  $rv('rv-undo').hidden = !decided;
  $rv('rv-undo').dataset.id = decided ? decided.id : '';
  const commit = `Takes effect at the next commit (${rv.base.commit_time || '02:00'}) or Recompute.`;
  const thin = d.left_days.filter(x => x.days < 3);
  if (decided) {
    effect.className = 'rv-effect';
    effect.textContent = `Decided: ${decided.decision === 'exclude' ? 'excluded' : 'kept'}. Undo it to suggest these hours again. ${commit}`;
  } else if (!d.sentence.in_baseline) {
    effect.className = 'rv-effect';
    effect.textContent = `This day is not in the next baseline, so a decision changes nothing yet. ${commit}`;
  } else {
    const kind = row && rvWeekend(row) ? 'weekend' : 'weekday';
    effect.className = `rv-effect${thin.length ? ' warn' : ''}`;
    effect.textContent = `Excluding leaves ${d.left_days.map(x => `${rvHour(x.hour)} with ${x.days} of ${x.of}`).join(', ')} ${kind} days`
      + (thin.length ? ` (fewer than 3 at ${thin.map(x => rvHour(x.hour)).join(', ')}: floor-only alerts there). ` : '. ') + commit;
  }
}

// Result line in the inspector's decision row; survives the re-render that follows a decision.
function rvDecisionNote(text, error = false) {
  rv.note = {text, error};
  const el = $rv('rv-note');
  el.textContent = text; el.hidden = !text;
  el.classList.toggle('ins-toast-error', error);
}
async function rvDecide(decision) {
  const sel = rv.sel;
  $rv('rv-exclude').disabled = $rv('rv-keep').disabled = true;
  try {
    const result = await apiMutation('/api/baseline/decisions', 'POST',
      {service: rv.service, day: sel.day, hour_from: sel.from, hour_to: sel.to, decision});
    if (result.warning) showToast(`${result.warning.map(rvHour).join(', ')} now ${result.warning.length === 1 ? 'has' : 'have'} fewer than 3 days of baseline: floor-only alerts there.`, true);
    rvStatus(`${decision === 'exclude' ? 'Excluded' : 'Kept'} ${rvDayLabel(sel.day)} ${rvHours(sel.from, sel.to)}. Takes effect at the next commit (${rv.base.commit_time || '02:00'}) or Recompute.`);
    rvDecisionNote(`${decision === 'exclude' ? 'Excluded' : 'Kept'} ${rvDayLabel(sel.day)} ${rvHours(sel.from, sel.to)}. Takes effect at the next commit (${rv.base.commit_time || '02:00'}) or Recompute.`);
    await rvReloadData();
    const next = rvSuggestions()[0];
    rv.sel = next ? {day: next.day, from: next.hour_from, to: next.hour_to} : null;
    rvWriteRoute();
    rvRenderAll();
  } catch (err) {
    rvStatus(`Could not save the decision: ${err.message}`, true);
    rvDecisionNote(`Could not save the decision: ${err.message}`, true);
    if (err.status === 409) {   // the data changed under us: show what is true now
      try { await rvReloadData(); rvRenderAll(); rvDecisionNote(`Could not save the decision: ${err.message}. The data was reloaded.`, true); } catch (e) { /* keep the error shown */ }
    }
  } finally { $rv('rv-exclude').disabled = $rv('rv-keep').disabled = false; }
}
async function rvUndo(id) {
  try {
    await apiMutation(`/api/baseline/decisions/${id}`, 'DELETE', {});
    rv.note = null; rvDecisionNote('');
    rvStatus('Decision undone: the hours are suggested again.');
    await rvReloadData();
    rvRenderAll();
  } catch (err) { rvStatus(`Could not undo: ${err.message}`, true); }
}
async function rvReloadData() {
  const svc = encodeURIComponent(rv.service);
  [rv.matrix, rv.base] = await Promise.all([
    getJson(`/api/baseline/matrix?service=${svc}&key=${encodeURIComponent(rv.key)}&days=${rv.days}`, 'Unable to load the matrix'),
    getJson(`/api/baseline?service=${svc}`, 'Unable to load baseline')]);
  rvIndexMatrix();
}
$rv('rv-exclude').addEventListener('click', () => rvDecide('exclude'));
$rv('rv-keep').addEventListener('click', () => rvDecide('keep'));
$rv('rv-undo').addEventListener('click', event => rvUndo(Number(event.currentTarget.dataset.id)));
function rvCloseInspector() { rv.sel = null; rvWriteRoute(); $rv('rv-inspector').hidden = true; rvRenderMatrix(); rvRenderLists(); }
$rv('rv-insp-close').addEventListener('click', rvCloseInspector);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.activeView === 'review' && rv.sel && !document.querySelector('.modal-overlay.open, .modal-overlay.active')) rvCloseInspector();
});

// ── Coverage ──
function rvRenderCoverage() {
  const t = chartTokens(), rows = ['weekday', 'weekend'];
  const data = [];
  rows.forEach((type, y) => rv.matrix.coverage[type].forEach(c => {
    const ratio = c.of ? c.days / c.of : 0;
    const fill = c.idle ? t.cls.none : t.seq[ratio >= 1 ? 3 : ratio >= 0.75 ? 2 : ratio >= 0.6 ? 1 : 0];
    const warn = c.idle || c.days < 3;
    data.push({value: [c.hour, y, c.days], c, type,
               itemStyle: {color: fill, borderColor: warn ? t.status.warn : t.card, borderWidth: warn ? 2 : 1.5},
               label: {color: rvInk(fill)}});
  }));
  chartFor($rv('rv-coverage')).setOption({
    ...baseOption(),
    grid: {left: 80, right: 8, top: 22, bottom: 8},
    xAxis: {type: 'category', position: 'top', data: [...Array(24).keys()].map(rvPad), splitLine: {show: false}, axisLine: {show: false}, axisTick: {show: false}, axisLabel: {color: t.dim}},
    yAxis: {type: 'category', inverse: true, data: ['Weekday', 'Weekend'], splitLine: {show: false}, axisLine: {show: false}, axisTick: {show: false}, axisLabel: {color: t.dim}},
    tooltip: {...baseOption().tooltip, confine: true, formatter: p => {
      const {c, type} = p.data, name = type === 'weekend' ? 'Weekend' : 'Weekday';
      if (c.idle) return `<b>${name} ${rvHour(c.hour)}</b><br>idle: no traffic expected<br>any ERROR/WARN template reaching its floor alerts here`;
      const ex = c.excluded.length ? ` (excluded: ${c.excluded.map(rvDayLabel).join(', ')})` : '';
      return `<b>${name} ${rvHour(c.hour)}</b> · ${c.days} of ${c.of} days${c.days < 3 ? ' · <b>floor-only</b>' : ''}<br>${c.used.map(rvDayLabel).join(', ')}${ex}`;
    }},
    series: [{type: 'heatmap', data, label: {show: true, fontSize: 11.5, fontWeight: 600, formatter: p => p.data.c.idle ? 'idle' : String(p.data.c.days)}}],
  }, true);
  const thin = data.filter(x => !x.c.idle && x.c.days < 3).length;
  $rv('rv-cov-summary').textContent = `Weekday: ${rvProfileText('weekday')[0]} · Weekend: ${rvProfileText('weekend')[0]} · ${thin} thin hour${thin === 1 ? '' : 's'}`;
}

// ── Header controls ──
function rvChangeRoute() { rv.sel = null; rv.note = null; rvDecisionNote(''); rv.cur = null; rvWriteRoute(); loadData(); }
$rv('rv-service').addEventListener('change', () => { rv.service = $rv('rv-service').value; rv.key = '*total'; rv.tplService = null; rvChangeRoute(); });
$rv('rv-days').addEventListener('click', event => {
  const b = event.target.closest('[data-days]');
  if (!b || Number(b.dataset.days) === rv.days) return;
  rv.days = Number(b.dataset.days); rvChangeRoute();
});
$rv('rv-view').addEventListener('click', event => {
  const b = event.target.closest('[data-v]');
  if (!b) return;
  rv.view = b.dataset.v;
  $rv('rv-view').querySelectorAll('button').forEach(x => { x.classList.toggle('active', x === b); x.setAttribute('aria-pressed', String(x === b)); });
  rvRenderMatrix();
});

// ── Recompute (polls the queued command) ──
async function rvPollCommand() {
  clearTimeout(rv.pollTimer);
  try {
    const cmd = await getJson(`/api/commands/${rv.commandId}`, 'Unable to read the command');
    if (cmd.done) {
      const r = cmd.result || {};
      rv.commandId = null;
      $rv('rv-recompute').disabled = false;
      if (r.error) rvStatus(`Recompute failed: ${r.error}`, true);
      else rvStatus(`Baseline recomputed: ${r.days_committed ?? 0} days newly committed, ${r.baseline_rows ?? 0} baseline rows.`);
      if (state.activeView === 'review') loadData();
      return;
    }
    rvStatus('Recompute queued: the engine runs it between flushes…');
  } catch (err) {
    rvStatus('Recompute queued; status unavailable, retrying…', true);
  }
  rv.pollTimer = setTimeout(rvPollCommand, 1000);
}
$rv('rv-recompute').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    rv.commandId = (await apiMutation('/api/baseline/recompute', 'POST', {})).id;
    rvPollCommand();
  } catch (err) {
    button.disabled = false;
    rvStatus(`Could not queue the recompute: ${err.message}`, true);
  }
});
