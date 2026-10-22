// ── Templates (adapted from logai-engine views/templates.js) ──
// Two modes: "To review" lists the automatic links (inherited, auto-matched, AI) waiting for a
// person, alerting first; "All templates" lists every template. Each row shows this hour's rate
// against its normal band and the linked document; the drawer holds the decision.
// mode null = not chosen yet: the first answer picks "review" when anything waits for review.
const tpl = {mode: null, search: '', service: '', level: '', link: '', status: '', page: 1, size: 100, refreshTimer: null,
             items: new Map(), order: [], docs: [], drawerItem: null};
const $tplModeToggle = document.getElementById('tpl-mode-toggle');
const $tplReviewCount = document.getElementById('tpl-review-count');
const $tplTbody = document.getElementById('template-tbody');
const $tplService = document.getElementById('filter-service');
const $tplLevel = document.getElementById('filter-level');
const $tplLink = document.getElementById('filter-link');
const $tplStatusToggle = document.getElementById('tpl-status-toggle');
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
  if (tpl.mode === 'review') params.set('link_source', 'review');
  else if (tpl.link) params.set('link_source', tpl.link);
  if (tpl.status) params.set('status', tpl.status);
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

// This hour's rate, and under it the normal band; above the band says so in words, not only in red.
function tplRateHtml(t) {
  const high = t.rate != null && t.band_high != null && t.rate > t.band_high;
  const rate = t.rate == null
    ? '<span class="sync-status" title="No minutes counted for this service this hour">—</span>'
    : `<span class="${high ? 'rate-high' : ''}" title="Average per minute this hour">${tplNum(t.rate)}/min${high ? ' ▲ above normal' : ''}</span>`;
  const band = t.band_high == null
    ? '<span title="No committed baseline yet">normal —</span>'
    : `<span title="Normal band for this hour (mean ± kσ)">normal ${t.band_low <= 0
        ? `≤ ${tplNum(t.band_high)}` : `${tplNum(t.band_low)}–${tplNum(t.band_high)}`}/min</span>`;
  return `<div class="cell-stack tpl-num">${rate}<span class="link-meta">${band}</span></div>`;
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

// Document cell: the document (or why there is none), then how it was linked.
function tplDocCell(t) {
  const linked = t.doc_id && t.link_source !== 'rejected';
  const head = linked
    ? `<div class="link-doc"><span class="tmpl-id">${escapeHtml(t.doc_id)}</span>${escapeHtml(t.doc_title || '')}</div>`
    : t.link_source === 'manual' ? '<div class="link-doc">No document (set by hand)</div>'
    : `<div class="link-doc">${t.embedded ? 'Not linked' : 'Not linked · not embedded yet'}</div>`;
  const how = t.link_source === 'manual' && !t.doc_id ? '' : linkSourceBadge(t);
  const safe = linked && t.doc_verdict === 'safe' ? '<span class="badge badge-green">SAFE</span>' : '';
  return `<div class="cell-stack">${head}${how || safe ? `<span class="link-meta">${how}${safe}</span>` : ''}</div>`;
}

// Review for an automatic link, else Link… / Change; the row opens the same drawer.
function tplActionButton(t) {
  const needsReview = AUTOMATIC_SOURCES.includes(t.link_source);
  const label = needsReview ? 'Review' : t.doc_id ? 'Change' : 'Link…';
  return `<button type="button" class="btn${needsReview ? ' btn-primary' : ''} tpl-review" data-key="${escapeHtml(tplKey(t))}"
    aria-label="${label.replace('…', '')} ${escapeHtml(t.template_id)}">${label}</button>`;
}

function tplStateCell(t) {
  return t.state === 'NORMAL' ? '<span class="link-meta">normal</span>'
    : `<span class="badge alert-state-${escapeHtml(t.state.toLowerCase())}">${escapeHtml(t.state)}</span>`;
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

function renderTemplates(data) {
  if (!data.items.length && tpl.mode === 'review') {
    $tplTbody.innerHTML = `
      <tr><td colspan="6" class="empty-state">
        <p>Nothing to review${tpl.search || tpl.service || tpl.level ? ' for these filters' : ''}.</p>
        <button type="button" class="btn" data-tpl-goto="all">Show all templates</button>
      </td></tr>`;
  } else if (!data.items.length) {
    $tplTbody.innerHTML = `
      <tr><td colspan="6" class="empty-state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
          <circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>
        </svg>
        <p>No templates found matching your filters.</p>
      </td></tr>`;
  } else {
    $tplTbody.innerHTML = data.items.map(t => `
      <tr class="tpl-row" data-key="${escapeHtml(tplKey(t))}">
        <td>${tplStateCell(t)}</td>
        <td><div class="cell-stack"><span class="link-meta"><span class="tmpl-id">${escapeHtml(t.template_id)}</span>
          <span class="badge ${levelBadgeClass(t.level)}">${escapeHtml(t.level)}</span>
          <span class="badge ${t.known ? 'badge-known' : 'badge-unknown'}">${t.known ? 'Known' : 'Unknown'}</span></span>
          <div class="tmpl-text">${highlightWildcards(escapeHtml(t.text))}</div></div></td>
        <td>${tplRateHtml(t)}</td>
        <td>${tplDocCell(t)}</td>
        <td class="tpl-num" title="${escapeHtml(formatFullTs(t.last_seen))}">${escapeHtml(formatTs(t.last_seen))}</td>
        <td class="tpl-action">${tplActionButton(t)}</td>
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
  if (tpl.mode === null) {      // first load: open on To review when anything waits for a decision
    setTplMode(data.review_total > 0 ? 'review' : 'all');
    if (tpl.mode === 'review') return loadTemplates();
  }
  const lastPage = Math.max(1, Math.ceil(data.total / tpl.size));
  if (tpl.page > lastPage) {    // the list shrank under us: show its last page instead of an empty one
    tpl.page = lastPage;
    return loadTemplates();
  }
  renderServiceFilter(services);
  tpl.items = new Map(data.items.map(t => [tplKey(t), t]));
  tpl.order = data.items.map(tplKey);
  tpl.docs = docs;
  $tplReviewCount.textContent = data.review_total;
  $tplReviewCount.hidden = !data.review_total;
  $tplModeToggle.querySelector('[data-tpl-mode="review"]')
    .setAttribute('aria-label', `To review, ${data.review_total} template${data.review_total === 1 ? '' : 's'}`);
  for (const [k, n] of Object.entries(data.stats)) document.getElementById(`tpl-stat-${k}`).textContent = n.toLocaleString();
  renderTemplates(data);
}

// Known / Unknown: the toggle and the stat cards set the same filter; the cards show it only in All templates.
function setTplStatus(status) {
  tpl.status = status;
  tpl.page = 1;
  document.querySelectorAll('[data-tpl-status]').forEach(b => {
    const on = b.dataset.tplStatus === status && (tpl.mode === 'all' || b.closest('#tpl-status-toggle'));
    b.setAttribute('aria-pressed', String(Boolean(on)));
    b.classList.toggle('active', Boolean(on) && b.classList.contains('toggle-btn'));
  });
}

// A card is a way into All templates: its number is the list it opens.
document.querySelectorAll('[data-tpl-status]').forEach(b => b.addEventListener('click', () => {
  if (tpl.mode !== 'all') setTplMode('all');
  else if (b.dataset.tplStatus === tpl.status) return;
  setTplStatus(b.dataset.tplStatus);
  loadData();
}));

// The link and known filters overlap To review (it lists only automatic links): hidden and cleared there.
function setTplMode(mode) {
  tpl.mode = mode;
  tpl.page = 1;
  $tplModeToggle.querySelectorAll('[data-tpl-mode]').forEach(b => {
    b.classList.toggle('active', b.dataset.tplMode === mode);
    b.setAttribute('aria-selected', String(b.dataset.tplMode === mode));
  });
  $tplLink.hidden = $tplStatusToggle.hidden = mode === 'review';
  if (mode === 'review') tpl.link = $tplLink.value = '';
  setTplStatus(mode === 'review' ? '' : tpl.status);
}

$tplModeToggle.addEventListener('click', event => {
  const button = event.target.closest('[data-tpl-mode]');
  if (!button || button.dataset.tplMode === tpl.mode) return;
  setTplMode(button.dataset.tplMode);
  loadData();
});

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

// The button is the keyboard path; a click anywhere else on the row opens the same drawer.
$tplTbody.addEventListener('click', event => {
  if (event.target.closest('[data-tpl-goto]')) {
    setTplMode('all');
    loadData();
    return;
  }
  const target = event.target.closest('.tpl-review, .tpl-row');
  if (target && (target.matches('.tpl-review') || !event.target.closest('a, button, input, select'))) {
    openTemplateDrawer(tpl.items.get(target.dataset.key));
  }
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

// Decision first: the template line next to the proposed document's ERROR line, why they were linked,
// then the actions (pinned while the drawer scrolls); Handling is one click away.
async function openTemplateDrawer(t) {
  if (!t) return;
  const $title = document.getElementById('tpl-drawer-title');
  $title.textContent = t.template_id;
  const src = t.link_source;
  const automatic = AUTOMATIC_SOURCES.includes(src);
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
    <section class="pane-section">
      <div class="pane-badges"><span class="tmpl-id">${escapeHtml(t.service)}</span>
        <span class="badge ${levelBadgeClass(t.level)}">${escapeHtml(t.level)}</span>${tplStateCell(t)}</div>
      <div class="drawer-facts">${tplRateHtml(t)}
        <span class="link-meta">first seen <span title="${escapeHtml(formatFullTs(t.first_seen))}">${escapeHtml(formatTs(t.first_seen))}</span>
          · last seen <span title="${escapeHtml(formatFullTs(t.last_seen))}">${escapeHtml(formatTs(t.last_seen))}</span></span></div></section>
    <section class="pane-section"><h3>${automatic ? 'Proposed document' : 'Document'}</h3>
      <p class="hint">Template</p><div class="code-block">${highlightWildcards(escapeHtml(t.text))}</div>
      <div id="tpl-drawer-doc">${t.doc_id ? '<p class="loading">Loading the document…</p>' : ''}</div>
      <div class="pane-badges" style="margin-top:8px">${linkSourceBadge(t)}</div>
      <p class="reason" style="margin-top:6px">${how}</p>
      ${t.note ? `<p class="hint">LLM's reason:</p><p class="reason">${escapeHtml(t.note)}</p>` : ''}</section>
    ${automatic ? `<div class="drawer-actions">
      <button type="button" class="btn btn-primary" data-act="confirm">${src === 'draft' ? 'Approve suggestion' : 'Confirm link'}</button>
      <button type="button" class="btn btn-danger" data-act="reject">Reject</button></div>` : ''}
    <section class="pane-section"><h3>Link by hand</h3>
      <div class="link-picker"><select class="filter-select" id="tpl-drawer-select" aria-label="Document for ${escapeHtml(t.template_id)}">${options.join('')}</select>
        <button type="button" class="btn" data-act="link">Save link</button></div>
      <p class="hint">"Automatic" removes any link and lets inheritance, ERROR matching and the LLM decide again.</p></section>
    <div class="form-error" id="tpl-drawer-error" aria-live="polite"></div>`;
  $tplDrawerBody.dataset.key = tplKey(t);
  tpl.drawerItem = t;   // the actions use this, not tpl.items: a reload may drop the row while the drawer is open
  $tplDrawerBody.scrollTop = 0;
  $tplDrawer.classList.add('open');
  // after the modal controller (a MutationObserver) has recorded the opener, so Escape returns focus there
  queueMicrotask(() => $title.focus());
  try {
    const doc = await linkedDocument(t);
    const $doc = document.getElementById('tpl-drawer-doc');
    if (!$doc || $tplDrawerBody.dataset.key !== tplKey(t)) return;
    $doc.innerHTML = doc ? `
      <p class="hint" style="margin-top:10px"><span class="tmpl-id">${escapeHtml(doc.doc_id)}</span> ${escapeHtml(doc.title)}${doc.verdict === 'safe'
        ? ' <span class="badge badge-green">SAFE</span>' : ''}${doc.error_code ? ` <span class="tmpl-id">${escapeHtml(doc.error_code)}</span>` : ''} · ERROR line</p>
      ${doc.error ? `<div class="code-block">${highlightWildcards(escapeHtml(doc.error))}</div>` : '<p class="prose-empty">No ERROR line.</p>'}
      <details class="drawer-details"><summary>Handling</summary>${doc.handling
        ? `<div class="prose">${escapeHtml(handlingText(doc.handling))}</div>` : '<p class="prose-empty">No handling text.</p>'}</details>`
      : (t.doc_id ? '<p class="prose-empty">Document not found.</p>' : '');
  } catch (err) {
    const $doc = document.getElementById('tpl-drawer-doc');
    if ($doc) $doc.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
  }
}

$tplDrawerBody.addEventListener('click', async event => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const t = tpl.drawerItem;
  if (!t) return;
  const act = button.dataset.act;
  // every action at once: a second click while this one (and Next item's reload) runs would act on a decided row
  const actions = [...$tplDrawerBody.querySelectorAll('[data-act]')];
  const busy = on => actions.forEach(b => { b.disabled = on; });
  busy(true);
  try {
    if (act === 'confirm' || act === 'reject') {
      if (act === 'reject' && !(await confirmDialog(`The ${t.link_source === 'draft' ? 'AI suggestion' : 'link'} for ${t.template_id} will be rejected.`,
        {title: 'Reject this link?', confirmLabel: 'Reject'}))) { busy(false); return; }
      await apiMutation(linkUrl(t.service, t.template_id, `/${act}`), 'POST', {});
      showToast(`${t.template_id}: ${act === 'confirm' ? 'link confirmed' : 'rejected'}`);
      if (tpl.mode === 'review') {   // next item: the row after this one on the page, if it is still there
        const next = tpl.order[tpl.order.indexOf(tplKey(t)) + 1];
        await loadData();
        if (next && tpl.items.has(next)) return openTemplateDrawer(tpl.items.get(next));
        $tplDrawer.classList.remove('open');
        $tplTbody.querySelector('button')?.focus();   // the opener was re-rendered away: land in the list, not on body
        return;
      }
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
    busy(false);
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
