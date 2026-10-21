// ── Templates (adapted from logai-engine views/templates.js) ──
// A service's templates with this hour's rate against its normal band, and
// the document each one is linked to: by hand (the select), inherited, auto-matched on a
// document's ERROR lines, or by the LLM; automatic links can be confirmed or rejected.
const tpl = {search: '', service: '', level: '', link: '', page: 1, size: 100, refreshTimer: null,
             items: new Map(), docs: []};
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

// How the template got its document: one badge, the detail is in the drawer.
function linkSourceBadge(t) {
  const sim = t.similarity == null ? '' : ` ${tplSim(t.similarity)}`;
  return {
    manual: t.doc_id ? '<span class="badge badge-gray">manual</span>' : '<span class="badge badge-gray">no document (set by hand)</span>',
    inherited: `<span class="badge badge-blue">inherited${sim}</span>`,
    corpus: `<span class="badge badge-yellow">auto-matched${sim}</span>`,
    llm: '<span class="badge badge-yellow">AI-confirmed</span>',
    draft: '<span class="badge badge-yellow">AI suggestion · unverified</span>',
    rejected: '<span class="badge badge-gray">AI result rejected</span>',
  }[t.link_source] || '';
}

// URL-encoded parts: survives an HTML attribute (a NUL separator does not) and stays unambiguous.
const tplKey = t => `${encodeURIComponent(t.service)}/${encodeURIComponent(t.template_id)}`;

// Table cell: the document (wrapped, readable) and how it was linked; Review opens the drawer.
function tplLinkCell(t) {
  const doc = t.doc_id && t.link_source !== 'rejected'
    ? `<div class="link-doc"><span class="tmpl-id">${escapeHtml(t.doc_id)}</span>${escapeHtml(t.doc_title || '')}</div>` : '';
  const none = !t.link_source
    ? `<span class="link-meta">${t.embedded ? 'Not linked' : 'Not linked · not embedded yet'}</span>` : '';
  const needsReview = AUTOMATIC_SOURCES.includes(t.link_source);
  return `<div class="link-cell">${doc}${none}<span class="link-meta">${linkSourceBadge(t)}${t.doc_verdict === 'safe' && doc
      ? '<span class="badge badge-green">SAFE</span>' : ''}</span>
    <button type="button" class="btn${needsReview ? ' btn-primary' : ''} tpl-review" data-key="${escapeHtml(tplKey(t))}"
      aria-label="${needsReview ? 'Review' : 'Link'} ${escapeHtml(t.template_id)}">${needsReview ? 'Review' : t.doc_id ? 'Change' : 'Link…'}</button></div>`;
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
        <td>${tplLinkCell(t)}</td>
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
  tpl.items = new Map(data.items.map(t => [tplKey(t), t]));
  tpl.docs = docs;
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

const $tplDrawer = document.getElementById('tpl-drawer');
const $tplDrawerBody = document.getElementById('tpl-drawer-body');

$tplTbody.addEventListener('click', event => {
  const button = event.target.closest('.tpl-review');
  if (button) openTemplateDrawer(tpl.items.get(button.dataset.key));
});

// The linked document's text, so the operator can judge the link without leaving the drawer.
async function linkedDocument(t) {
  if (!t.doc_id) return null;
  const found = tpl.docs.find(d => d.doc_id === t.doc_id);
  if (found) return found;
  const status = t.link_source === 'draft' ? 'draft' : t.link_source === 'rejected' ? 'rejected' : 'active';
  const list = await getJson(`/api/documents?status=${status}`, 'Unable to load the document');
  return list.find(d => d.doc_id === t.doc_id) || null;
}

async function openTemplateDrawer(t) {
  if (!t) return;
  document.getElementById('tpl-drawer-title').textContent = t.template_id;
  const src = t.link_source;
  const how = {
    manual: t.doc_id ? 'Linked by hand.' : 'Set by hand to "no document": never linked automatically.',
    inherited: `Inherited from <span class="tmpl-id">${escapeHtml(t.inherited_from || '')}</span>, the most similar manually linked template of this service and level (similarity ${tplSim(t.similarity)}).`,
    corpus: `Matched automatically on the document's ERROR line (similarity ${tplSim(t.similarity)}).`,
    llm: 'Confirmed by the LLM among the nearest documents.',
    draft: 'Written by the LLM for this template; unverified until approved.',
    rejected: 'An AI result was rejected: this template is not sent to the LLM again.',
  }[src] || 'Not linked to a document.';
  const options = [`<option value="">Automatic (let the linker decide)</option>`,
    `<option value="${NO_DOCUMENT}"${src === 'manual' && !t.doc_id ? ' selected' : ''}>No document</option>`]
    // this service's documents and the shared ones first; another service's only if picked on purpose
    .concat([...tpl.docs].sort((a, b) => (a.service && a.service !== t.service) - (b.service && b.service !== t.service))
      .map(d => `<option value="${escapeHtml(d.doc_id)}"${d.doc_id === t.doc_id && src === 'manual' ? ' selected' : ''}>${escapeHtml(d.doc_id)} · ${escapeHtml(d.title)} [${escapeHtml(d.service || 'shared')}]</option>`));
  $tplDrawerBody.innerHTML = `
    <section class="pane-section"><h3>Template</h3>
      <div class="pane-badges"><span class="tmpl-id">${escapeHtml(t.service)}</span>
        <span class="badge ${levelBadgeClass(t.level)}">${escapeHtml(t.level)}</span>
        <span class="badge alert-state-${escapeHtml(t.state.toLowerCase())}">${escapeHtml(t.state)}</span>
        <span>rate ${tplRate(t)} · normal ${tplBand(t)}</span></div>
      <div class="code-block" style="margin-top:6px">${highlightWildcards(escapeHtml(t.text))}</div></section>
    <section class="pane-section"><h3>Document</h3>
      <div class="pane-badges">${linkSourceBadge(t)}</div>
      <p class="reason" style="margin-top:6px">${how}</p>
      ${t.note ? `<p class="hint">LLM's reason:</p><p class="reason">${escapeHtml(t.note)}</p>` : ''}
      <div id="tpl-drawer-doc">${t.doc_id ? '<p class="loading">Loading the document…</p>' : ''}</div></section>
    ${AUTOMATIC_SOURCES.includes(src) ? `<div class="drawer-actions">
      <button type="button" class="btn btn-primary" data-act="confirm">${src === 'draft' ? 'Approve suggestion' : 'Confirm link'}</button>
      <button type="button" class="btn btn-danger" data-act="reject">Reject</button></div>` : ''}
    <section class="pane-section"><h3>Link by hand</h3>
      <div class="link-picker"><select class="filter-select" id="tpl-drawer-select" aria-label="Document for ${escapeHtml(t.template_id)}">${options.join('')}</select>
        <button type="button" class="btn" data-act="link">Save link</button></div>
      <p class="hint">"Automatic" removes any link and lets inheritance, ERROR matching and the LLM decide again.</p></section>
    <div class="form-error" id="tpl-drawer-error" aria-live="polite"></div>`;
  $tplDrawerBody.dataset.key = tplKey(t);
  $tplDrawer.classList.add('open');
  try {
    const doc = await linkedDocument(t);
    const $doc = document.getElementById('tpl-drawer-doc');
    if (!$doc || $tplDrawerBody.dataset.key !== tplKey(t)) return;
    $doc.innerHTML = doc ? `
      <div class="pane-head" style="margin-top:10px"><h2 style="font-size:16px">${escapeHtml(doc.title)}</h2>
        <div class="pane-badges"><span class="tmpl-id">${escapeHtml(doc.doc_id)}</span>${doc.verdict === 'safe'
          ? '<span class="badge badge-green">SAFE</span>' : ''}${doc.error_code ? `<span class="tmpl-id">${escapeHtml(doc.error_code)}</span>` : ''}</div></div>
      <p class="hint">ERROR</p>${doc.error ? `<div class="code-block">${highlightWildcards(escapeHtml(doc.error))}</div>` : '<p class="prose-empty">No ERROR line.</p>'}
      <p class="hint">Handling</p>${doc.handling ? `<div class="prose">${escapeHtml(handlingText(doc.handling))}</div>` : '<p class="prose-empty">No handling text.</p>'}`
      : (t.doc_id ? '<p class="prose-empty">Document not found.</p>' : '');
  } catch (err) {
    const $doc = document.getElementById('tpl-drawer-doc');
    if ($doc) $doc.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
  }
}

$tplDrawerBody.addEventListener('click', async event => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const t = tpl.items.get($tplDrawerBody.dataset.key);
  if (!t) return;
  const act = button.dataset.act;
  button.disabled = true;
  try {
    if (act === 'confirm' || act === 'reject') {
      if (act === 'reject' && !(await confirmDialog(`The ${t.link_source === 'draft' ? 'AI suggestion' : 'link'} for ${t.template_id} will be rejected.`,
        {title: 'Reject this link?', confirmLabel: 'Reject'}))) { button.disabled = false; return; }
      await apiMutation(linkUrl(t.service, t.template_id, `/${act}`), 'POST', {});
      showToast(`${t.template_id}: ${act === 'confirm' ? 'link confirmed' : 'rejected'}`);
      $tplDrawer.classList.remove('open');
    } else if (act === 'link') {
      const value = document.getElementById('tpl-drawer-select').value;
      if (!value) await apiMutation(linkUrl(t.service, t.template_id), 'DELETE', {});
      else await apiMutation(linkUrl(t.service, t.template_id), 'PUT', {doc_id: value === NO_DOCUMENT ? null : value});
      showToast(`${t.template_id}: ${!value ? 'back to automatic' : value === NO_DOCUMENT ? 'no document' : `linked to ${value}`}`);
      $tplDrawer.classList.remove('open');
      if (value && value !== NO_DOCUMENT) await offerSimilar(t.service, t.template_id, value);
    }
  } catch (error) {
    document.getElementById('tpl-drawer-error').textContent = error.message;
    button.disabled = false;
    return;
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
