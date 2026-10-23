// ── Alerting (adapted from logai-engine views/alerting.js) ──
// One card per alert row from /api/alerts: a document, an undocumented
// template, or the whole service. Polled every 5 s by main.js.
const al = {rows: [], filter: 'all', updatedAt: null};
const $alertCards = document.getElementById('alert-cards');
const ALERT_STATES = ['ALERTING', 'WARMING', 'COOLING'];
const TOTAL_LABELS = {'*total': 'All events', '*error': 'Errors'};

function fetchAlerts() { return getJson('/api/alerts', 'Unable to load alert state'); }

const alNum = n => (Math.round(n * 10) / 10).toLocaleString();

// Normal = this hour's baseline mean ± σ; the hover shows the alert band (mean ± kσ).
function alNormal({mean, std, band_low: low, band_high: high}) {
  if (mean == null) return '<span class="alert-band" title="No committed baseline yet: floor only">(no baseline yet)</span>';
  const band = high == null ? '' : ` title="Alert band ${alNum(Math.max(low, 0))}–${alNum(high)}/min"`;
  return `<span class="alert-band"${band}>(normal ${alNum(mean)} ± ${alNum(std)})</span>`;
}

// How a template line got its document; drafts drive nothing, they are shown so the operator can act.
function alLink(link) {
  if (!link || !link.source || link.source === 'rejected' || (link.source === 'manual' && !link.doc_id)) return '';
  const doc = escapeHtml(link.doc_id || '');
  const tag = {
    manual: '', inherited: `<span class="badge badge-blue">inherited</span>`,
    corpus: `<span class="badge badge-yellow" title="Matched on ${doc}'s ERROR lines">auto ${link.similarity == null ? '' : link.similarity.toFixed(2)}</span>`,
    llm: `<span class="badge badge-yellow" title="${escapeHtml(link.note || '')}">AI-confirmed</span>`,
    draft: `<span class="badge badge-yellow" title="Unverified: check commands before running them">AI suggestion: ${escapeHtml(link.title || doc)}</span>`,
  }[link.source] || '';
  const safe = link.verdict === 'safe' ? ' <span class="badge badge-green">SAFE</span>' : '';
  return tag || safe ? ` <span class="link-meta">${tag}${safe}</span>` : '';
}

// Links out of a card are plain anchors to hash routes (main.js), so they open in a new tab too.
const alBaseline = (service, key) => `#baseline/${encodeURIComponent(service)}/${encodeURIComponent(key)}/1`;
const alAnchor = (href, label, title) => `<a class="btn" href="${escapeHtml(href)}"${title ? ` title="${escapeHtml(title)}"` : ''}>${escapeHtml(label)}</a>`;

function alLine(service, label, item, mono) {
  return `<li class="alert-line">
    <div class="${mono ? 'tmpl-text' : 'alert-line-label'}" title="${escapeHtml(label)}">${escapeHtml(label)}</div>
    <span class="alert-rate">${alNum(item.count)}/min</span>${alNormal(item)}${alLink(item.link)}
    <a class="alert-chart" href="${escapeHtml(alBaseline(service, item.key || item.template_id))}" title="Baseline chart for this line">chart</a>
  </li>`;
}

// The buttons under a card's header, by kind of row.
function alActions(row) {
  const svc = row.service;
  if (row.kind === 'document') {
    const n = row.templates.length;
    return [alAnchor(`#documentation/${encodeURIComponent(row.doc_id)}`, 'Open document'),
      alAnchor(alBaseline(svc, `doc:${row.doc_id}`), 'Baseline'),
      alAnchor(`#templates/${encodeURIComponent(svc)}?doc_id=${encodeURIComponent(row.doc_id)}`, `${n} template${n === 1 ? '' : 's'}`,
        'Templates of this service linked to the document')].join('');
  }
  if (row.kind === 'service') {
    return alAnchor(alBaseline(svc, '*total'), 'Baseline') + alAnchor('#pipeline', 'Pipeline');
  }
  return [alAnchor(`#templates/${encodeURIComponent(svc)}/${encodeURIComponent(row.keys[0])}`, 'Link a document'),
    row.draft_doc_id ? alAnchor(`#documentation/suggestions/${encodeURIComponent(row.draft_doc_id)}`, 'AI suggestion') : '',
    alAnchor(alBaseline(svc, '*error'), 'Baseline')].join('');
}

function alTitle(row) {
  if (row.kind === 'service') return 'Whole service';
  if (row.kind === 'template') return 'Undocumented';
  if (row.kind === 'cluster') return `Undocumented · ${row.templates.length} similar templates`;
  return row.title || row.doc_id;
}

function renderAlerts(rows) {
  al.rows = rows;
  al.updatedAt = Date.now();
  renderAlertFreshness();
  document.getElementById('alert-stat-total').textContent = rows.length;
  ALERT_STATES.forEach(s => {
    document.getElementById(`alert-stat-${s.toLowerCase()}`).textContent = rows.filter(r => r.state === s).length;
  });
  const shown = al.filter === 'all' ? rows : rows.filter(r => r.state === al.filter);
  if (!shown.length) {
    $alertCards.innerHTML = `<p class="empty-state">${rows.length ? 'No alerts match this state.' : 'All quiet: every service is inside its normal band.'}</p>`;
    return;
  }
  // Rows arrive ranked (ALERTING first, then oldest); keep that order.
  $alertCards.innerHTML = shown.map(row => {
    const st = row.state.toLowerCase();
    const lines = row.totals.map(t => alLine(row.service, TOTAL_LABELS[t.key] || 'Linked templates combined', t, false))
      .concat(row.templates.map(t => alLine(row.service, t.text || t.template_id, t, true)));
    return `<article class="alert-card alert-card-${escapeHtml(st)}">
      <header class="alert-card-head">
        <span class="badge alert-state-${escapeHtml(st)}">${escapeHtml(row.state)}</span>
        <h3 class="alert-card-title">${escapeHtml(row.service)} · ${escapeHtml(alTitle(row))}</h3>
        <span class="alert-card-since" title="${escapeHtml(formatFullTs(row.since))}">since ${escapeHtml(formatTs(row.since))}</span>
      </header>
      <div class="btn-row alert-actions">${alActions(row)}</div>
      ${row.handling_step ? `<p class="alert-step" title="First step of the document's Handling">${escapeHtml(row.handling_step)}</p>` : ''}
      <ul class="alert-lines">${lines.join('')}</ul>
    </article>`;
  }).join('');
}

document.querySelectorAll('#alert-status-toggle .toggle-btn').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('#alert-status-toggle .toggle-btn').forEach(item => item.classList.toggle('active', item === button));
    al.filter = button.dataset.alertStatus;
    document.querySelectorAll('[data-alert-card]').forEach(card => card.setAttribute('aria-pressed', String(card.dataset.alertCard === al.filter)));
    renderAlerts(al.rows);
  });
});

// The KPI cards filter the list too, through the same toggle.
document.querySelectorAll('[data-alert-card]').forEach(card => card.addEventListener('click', () => {
  document.querySelector(`#alert-status-toggle [data-alert-status="${card.dataset.alertCard}"]`).click();
}));

// "Updated Xs ago", turning into a warning when polling stops delivering.
const ALERT_STALE_SECONDS = 15;
function renderAlertFreshness() {
  const label = document.getElementById('alert-last-updated');
  if (!al.updatedAt) return;
  const age = Math.round((Date.now() - al.updatedAt) / 1000);
  const stale = state.activeView === 'alerting' && age > ALERT_STALE_SECONDS;
  label.textContent = stale ? `Stale: last update ${age}s ago` : `Live · updated ${age < 2 ? 'just now' : `${age}s ago`}`;
  label.closest('.live-status').classList.toggle('stale', stale);
}
setInterval(renderAlertFreshness, 1000);
