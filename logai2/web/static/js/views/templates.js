// ── Templates (adapted from logai-engine views/templates.js) ──
// A service's templates with this hour's rate against its normal band, and
// the document each one is linked to: by hand (the select), inherited, auto-matched on a
// document's ERROR lines, or by the LLM; automatic links can be confirmed or rejected.
const tpl = {search: '', service: '', level: '', link: '', page: 1, size: 100, refreshTimer: null};
const $tplTbody = document.getElementById('template-tbody');
const $tplService = document.getElementById('filter-service');
const $tplLevel = document.getElementById('filter-level');
const $tplLink = document.getElementById('filter-link');
const $similarModal = document.getElementById('similar-modal');
const NO_DOCUMENT = '__none__';
const AUTOMATIC_SOURCES = ['inherited', 'corpus', 'llm', 'draft'];
const $tplSearch = document.getElementById('search-input');
const $tplPageInfo = document.getElementById('page-info');
const $tplPageBtns = document.getElementById('page-btns');
const TPL_LEVELS = ['FATAL', 'CRITICAL', 'ERROR', 'WARN', 'WARNING', 'NOTICE', 'INFO', 'DEBUG', 'TRACE'];

function fetchTemplates() {
  const params = new URLSearchParams({page: tpl.page, size: tpl.size});
  if (tpl.search) params.set('q', tpl.search);
  if (tpl.service) params.set('service', tpl.service);
  if (tpl.level) params.set('level', tpl.level);
  if (tpl.link) params.set('link_source', tpl.link);
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

const tplSim = x => (x == null ? '' : x.toFixed(2));

function tplDocSelect(t, docs) {
  const none = t.link_source === 'manual' && !t.doc_id;
  const options = [`<option value="">Automatic</option>`,
    `<option value="${NO_DOCUMENT}"${none ? ' selected' : ''}>No document</option>`].concat(docs.map(d =>
    `<option value="${escapeHtml(d.doc_id)}"${d.doc_id === t.doc_id ? ' selected' : ''}>${escapeHtml(d.doc_id)} · ${escapeHtml(d.title)}</option>`));
  return `<select class="filter-select doc-select" data-service="${escapeHtml(t.service)}" data-template="${escapeHtml(t.template_id)}" aria-label="Document for ${escapeHtml(t.template_id)}">${options.join('')}</select>`;
}

// What linked it, and Confirm / Reject for the automatic sources.
function tplLinkMeta(t) {
  const src = t.link_source;
  if (!src) return t.embedded ? '' : '<span class="link-meta" title="The linker has not embedded this template yet">not embedded yet</span>';
  const doc = t.doc_id ? escapeHtml(t.doc_id) : '';
  const label = {
    manual: t.doc_id ? '<span class="badge badge-gray">manual</span>' : '<span class="badge badge-gray" title="Never linked automatically">no document</span>',
    inherited: `<span class="badge badge-blue" title="Nearest manually linked template of this service and level">inherited ${tplSim(t.similarity)}</span> from <span class="tmpl-id">${escapeHtml(t.inherited_from || '')}</span>`,
    corpus: `<span class="badge badge-yellow" title="Matched on ${doc}'s ERROR lines">auto ${tplSim(t.similarity)}</span>`,
    llm: `<span class="badge badge-yellow" title="${escapeHtml(t.note || '')}">AI-confirmed</span>`,
    draft: `<span class="badge badge-yellow" title="Unverified: check commands before running them">AI suggestion ${doc}</span>`,
    rejected: `<span class="badge badge-gray" title="Rejected ${doc}; not sent to the LLM again">rejected</span>`,
  }[src] || escapeHtml(src);
  const safe = t.doc_verdict === 'safe' ? ' <span class="badge badge-green" title="Normal operation, no action">SAFE</span>' : '';
  const actions = AUTOMATIC_SOURCES.includes(src)
    ? ` <button type="button" class="btn link-confirm">Confirm</button><button type="button" class="btn btn-danger link-reject">Reject</button>` : '';
  return `<span class="link-meta" data-service="${escapeHtml(t.service)}" data-template="${escapeHtml(t.template_id)}">${label}${safe}${actions}</span>`;
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
        <td><div class="link-cell">${tplDocSelect(t, docs)}${tplLinkMeta(t)}</div></td>
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

const linkUrl = (service, template, suffix = '') =>
  `/api/templates/${encodeURIComponent(service)}/${encodeURIComponent(template)}/link${suffix}`;

$tplTbody.addEventListener('change', async event => {
  const select = event.target.closest('.doc-select');
  if (!select) return;
  const {service, template} = select.dataset;
  const value = select.value;
  select.disabled = true;
  try {
    if (!value) await apiMutation(linkUrl(service, template), 'DELETE', {});
    else await apiMutation(linkUrl(service, template), 'PUT', {doc_id: value === NO_DOCUMENT ? null : value});
    if (value && value !== NO_DOCUMENT) await offerSimilar(service, template, value);
  } catch (error) {
    notifyDialog(`Could not link ${template}: ${error.message}`);
  }
  select.blur();
  loadData();
});

$tplTbody.addEventListener('click', async event => {
  const button = event.target.closest('.link-confirm, .link-reject');
  if (!button) return;
  const {service, template} = button.closest('.link-meta').dataset;
  button.disabled = true;
  try {
    await apiMutation(linkUrl(service, template, button.classList.contains('link-confirm') ? '/confirm' : '/reject'), 'POST', {});
  } catch (error) {
    notifyDialog(`Could not update ${template}: ${error.message}`);
  }
  loadData();
});

// "Apply to similar": same service and level, pre-ticked at the inherit threshold; ticked ones become manual links.
async function offerSimilar(service, template, docId) {
  const data = await getJson(`/api/templates/${encodeURIComponent(service)}/${encodeURIComponent(template)}/similar`,
    'Unable to load similar templates');
  const items = data.items.filter(i => !(i.current_source === 'manual' && i.current_doc_id === docId));
  if (!items.length) return;
  document.getElementById('similar-modal-intro').textContent =
    `${template} is now linked to ${docId}. Link these similar ${service} templates too?`;
  document.getElementById('similar-error').textContent = '';
  document.getElementById('similar-list').innerHTML = items.map(i => `
    <label class="similar-item">
      <input type="checkbox" value="${escapeHtml(i.template_id)}"${i.pre_ticked ? ' checked' : ''}>
      <span><span class="tmpl-id">${escapeHtml(i.template_id)}</span> <span class="tmpl-text">${highlightWildcards(escapeHtml(i.text))}</span></span>
      <span class="link-meta">${i.similarity.toFixed(2)}${i.current_doc_id ? ` · now ${escapeHtml(i.current_doc_id)} (${escapeHtml(i.current_source)})` : ''}</span>
    </label>`).join('');
  const apply = document.getElementById('similar-apply');
  apply.onclick = async () => {
    const also = [...document.querySelectorAll('#similar-list input:checked')].map(i => i.value);
    apply.disabled = true;
    try {
      if (also.length) await apiMutation(linkUrl(service, template), 'PUT', {doc_id: docId, also});
      $similarModal.classList.remove('open');
      loadData();
    } catch (error) {
      document.getElementById('similar-error').textContent = error.message;
    } finally { apply.disabled = false; }
  };
  $similarModal.classList.add('open');
}

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
$tplLink.addEventListener('change', () => { tpl.link = $tplLink.value; tpl.page = 1; loadData(); });
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
