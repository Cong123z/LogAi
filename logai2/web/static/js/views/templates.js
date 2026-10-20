// ── Templates (adapted from logai-engine views/templates.js) ──
// A service's templates with this hour's rate against its normal band, and
// the document each one is linked to (a select links or unlinks by hand).
const tpl = {search: '', service: '', level: '', page: 1, size: 100, refreshTimer: null};
const $tplTbody = document.getElementById('template-tbody');
const $tplService = document.getElementById('filter-service');
const $tplLevel = document.getElementById('filter-level');
const $tplSearch = document.getElementById('search-input');
const $tplPageInfo = document.getElementById('page-info');
const $tplPageBtns = document.getElementById('page-btns');
const TPL_LEVELS = ['FATAL', 'CRITICAL', 'ERROR', 'WARN', 'WARNING', 'NOTICE', 'INFO', 'DEBUG', 'TRACE'];

function fetchTemplates() {
  const params = new URLSearchParams({page: tpl.page, size: tpl.size});
  if (tpl.search) params.set('q', tpl.search);
  if (tpl.service) params.set('service', tpl.service);
  if (tpl.level) params.set('level', tpl.level);
  return getJson(`/api/templates?${params}`, 'Unable to load templates');
}
function fetchServices() { return getJson('/api/services', 'Unable to load services'); }
function fetchDocuments() { return getJson('/api/documents', 'Unable to load documents'); }

function highlightWildcards(text) {
  return text ? text.replace(/&lt;\*&gt;/g, '<span class="wildcard">&lt;*&gt;</span>') : '';
}

function levelBadgeClass(level) {
  const l = (level || 'INFO').toUpperCase();
  if (l === 'ERROR' || l === 'FATAL' || l === 'CRITICAL') return 'badge-error';
  if (l === 'WARN' || l === 'WARNING') return 'badge-warn';
  if (l === 'INFO') return 'badge-info';
  return 'badge-debug';
}

const tplNum = n => (Math.round(n * 10) / 10).toLocaleString();

function tplRate(t) {
  if (t.rate == null) return '<span class="sync-status" title="No minutes counted for this service this hour">—</span>';
  const high = t.band_high != null && t.rate > t.band_high;
  return `<span class="${high ? 'rate-high' : ''}" title="Average per minute this hour">${tplNum(t.rate)}/min</span>`;
}

function tplBand(t) {
  if (t.band_high == null) return '<span class="sync-status" title="No committed baseline yet">—</span>';
  return `<span title="Normal band for this hour (mean ± kσ)">${tplNum(Math.max(t.band_low, 0))}–${tplNum(t.band_high)}/min</span>`;
}

function tplDocSelect(t, docs) {
  const options = [`<option value="">— None —</option>`].concat(docs.map(d =>
    `<option value="${escapeHtml(d.doc_id)}"${d.doc_id === t.doc_id ? ' selected' : ''}>${escapeHtml(d.doc_id)} · ${escapeHtml(d.title)}</option>`));
  return `<select class="filter-select doc-select" data-service="${escapeHtml(t.service)}" data-template="${escapeHtml(t.template_id)}" aria-label="Document for ${escapeHtml(t.template_id)}">${options.join('')}</select>`;
}

function renderServiceFilter(services) {
  const current = $tplService.value;
  $tplService.innerHTML = '<option value="">All services</option>';
  services.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = s;
    opt.selected = s === current;
    $tplService.appendChild(opt);
  });
}

function renderTemplates(data, docs) {
  // Do not swap the table under an operator who is choosing a document.
  if ($tplTbody.contains(document.activeElement)) return;
  if (!data.items.length) {
    $tplTbody.innerHTML = `
      <tr><td colspan="9" class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
          <circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>
        </svg>
        <p>No templates found matching your filters.</p>
      </td></tr>`;
  } else {
    $tplTbody.innerHTML = data.items.map(t => `
      <tr>
        <td><span class="badge alert-state-${escapeHtml(t.state.toLowerCase())}">${escapeHtml(t.state)}</span></td>
        <td><span class="tmpl-id">${escapeHtml(t.template_id)}</span></td>
        <td><div class="tmpl-text" title="${escapeHtml(t.text)}">${highlightWildcards(escapeHtml(t.text))}</div></td>
        <td><span class="badge ${levelBadgeClass(t.level)}">${escapeHtml(t.level)}</span></td>
        <td class="tpl-num">${tplRate(t)}</td>
        <td class="tpl-num">${tplBand(t)}</td>
        <td>${tplDocSelect(t, docs)}</td>
        <td title="${escapeHtml(formatFullTs(t.first_seen))}">${escapeHtml(formatTs(t.first_seen))}</td>
        <td title="${escapeHtml(formatFullTs(t.last_seen))}">${escapeHtml(formatTs(t.last_seen))}</td>
      </tr>`).join('');
  }
  renderTplPagination(data.total);
}

function renderTplPagination(total) {
  const pages = Math.max(1, Math.ceil(total / tpl.size));
  const page = tpl.page;
  $tplPageInfo.textContent = total
    ? `Showing ${(page - 1) * tpl.size + 1}–${Math.min(page * tpl.size, total)} of ${total} templates`
    : 'No templates';
  let btns = `<button class="page-btn" ${page <= 1 ? 'disabled' : ''} data-page="${page - 1}" aria-label="Previous page">‹</button>`;
  getPageRange(page, pages).forEach(p => {
    btns += p === '...'
      ? '<span style="padding:6px 4px;color:var(--text-muted)">…</span>'
      : `<button class="page-btn ${p === page ? 'active' : ''}" data-page="${p}">${p}</button>`;
  });
  btns += `<button class="page-btn" ${page >= pages ? 'disabled' : ''} data-page="${page + 1}" aria-label="Next page">›</button>`;
  $tplPageBtns.innerHTML = btns;
}

function getPageRange(current, total) {
  if (total <= 7) return Array.from({length: total}, (_, i) => i + 1);
  const pages = [1];
  if (current > 3) pages.push('...');
  for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) pages.push(i);
  if (current < total - 2) pages.push('...');
  pages.push(total);
  return pages;
}

let tplSeq = 0;
async function loadTemplates() {
  const seq = ++tplSeq;
  const [data, services, docs] = await Promise.all([fetchTemplates(), fetchServices(), fetchDocuments()]);
  if (seq !== tplSeq) return;   // a newer request (filter, page) is in flight: drop this answer
  const lastPage = Math.max(1, Math.ceil(data.total / tpl.size));
  if (tpl.page > lastPage) {    // the list shrank under us: show its last page instead of an empty one
    tpl.page = lastPage;
    return loadTemplates();
  }
  renderServiceFilter(services);
  renderTemplates(data, docs);
}

// ── Event listeners ──
$tplLevel.innerHTML = '<option value="">All levels</option>' + TPL_LEVELS.map(l => `<option value="${l}">${l}</option>`).join('');

$tplPageBtns.addEventListener('click', event => {
  const btn = event.target.closest('.page-btn');
  if (!btn || btn.disabled) return;
  tpl.page = Number(btn.dataset.page);
  loadData();
});

$tplTbody.addEventListener('change', async event => {
  const select = event.target.closest('.doc-select');
  if (!select) return;
  const {service, template} = select.dataset;
  select.disabled = true;
  try {
    await apiMutation(`/api/templates/${encodeURIComponent(service)}/${encodeURIComponent(template)}/link`, 'PUT',
      {doc_id: select.value || null});
  } catch (error) {
    notifyDialog(`Could not link ${template}: ${error.message}`);
  }
  select.blur();
  loadData();
});

let tplSearchTimeout;
$tplSearch.addEventListener('input', () => {
  clearTimeout(tplSearchTimeout);
  tplSearchTimeout = setTimeout(() => {
    tpl.search = $tplSearch.value.trim();
    tpl.page = 1;
    loadData();
  }, 300);
});

$tplService.addEventListener('change', () => { tpl.service = $tplService.value; tpl.page = 1; loadData(); });
$tplLevel.addEventListener('change', () => { tpl.level = $tplLevel.value; tpl.page = 1; loadData(); });
document.getElementById('page-size').addEventListener('change', event => {
  tpl.size = Number(event.target.value);
  tpl.page = 1;
  loadData();
});
document.getElementById('auto-refresh').addEventListener('change', event => {
  clearInterval(tpl.refreshTimer);
  tpl.refreshTimer = event.target.checked
    ? setInterval(() => { if (state.activeView === 'templates') loadData(); }, 10000)
    : null;
});
