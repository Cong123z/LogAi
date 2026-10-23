// ── Key picker: one grouped "Show" control shared by Baseline and Review ──
// Group buttons (All events / level / documentation verdict) pick a combined series; the Show popover
// lists that group's combined key, documents and templates. Pure view: keys are served by /api/baseline/*.
const KP_ERROR = ['ERROR', 'FATAL', 'CRITICAL'], KP_WARN = ['WARN', 'WARNING'];   // same sets as links.level_class
const KP_LINKED = ['manual', 'inherited', 'corpus', 'llm'];                        // links that drive alerts (links.ALERTING_SOURCES)
const KP_GROUPS = [
  {id: 'all', label: 'All events', key: '*total', combined: 'All events'},
  {sep: 'Level'},
  {id: 'ERROR', label: 'ERROR', dot: 'error', key: '*error', combined: 'All ERROR lines combined', stored: true},
  {id: 'WARN', label: 'WARN', dot: 'warn', key: '*warn', combined: 'All WARN lines combined', stored: true, fresh: true},
  {id: 'INFO', label: 'INFO', dot: 'info', key: '*info', combined: 'All INFO lines combined', stored: true, fresh: true},
  {sep: 'Documentation'},
  {id: 'action', label: 'ERROR docs', dot: 'error', key: 'verdict:action', combined: 'All templates of ERROR documents', derived: true},
  {id: 'safe', label: 'SAFE docs', dot: 'safe', key: 'verdict:safe', combined: 'All templates of SAFE documents', derived: true},
];
const kpIsDocGroup = id => id === 'action' || id === 'safe';
const kpLinked = t => KP_LINKED.includes(t.link_source);

function levelClass(level) {
  const l = String(level || '').toUpperCase();
  return KP_ERROR.includes(l) ? 'ERROR' : KP_WARN.includes(l) ? 'WARN' : 'INFO';
}
function keyGroup(key, templates, docs) {
  const g = KP_GROUPS.find(x => x.key === key);
  if (g) return g.id;
  if (key.startsWith('doc:')) {
    const d = docs.find(x => `doc:${x.doc_id}` === key);
    return d && kpIsDocGroup(d.verdict) ? d.verdict : 'all';
  }
  if (key.startsWith('*')) return key === '*warn' || key === '*info' || key === '*error' ? levelClass(key.slice(1)) : 'all';
  const t = templates.find(x => x.template_id === key);
  return t ? levelClass(t.level) : 'all';
}
function keyLabel(key, templates, docs) {
  const g = KP_GROUPS.find(x => x.key === key);
  if (g) return g.combined;
  if (key.startsWith('doc:')) {
    const d = docs.find(x => `doc:${x.doc_id}` === key);
    return `${key.slice(4)} · ${d ? d.title : ''}`;
  }
  const t = templates.find(x => x.template_id === key);
  return t ? `${t.template_id} · ${t.text}` : key;
}

let kpSeq = 0;
function createKeyPicker(root, {onPick}) {
  const uid = `kp${++kpSeq}`;
  const st = {service: '', key: '*total', templates: [], groups: {}, remote: null, rseq: 0, timer: 0, docs: [], baselineState: null, docIds: new Set(), group: 'all', open: false, active: 0, opts: []};
  root.innerHTML = `
    <div class="kp-bar"><div class="kp-groups" role="group" aria-label="Group"></div></div>
    <div class="kp-bar">
      <span class="filter-label" id="${uid}-label">Show</span>
      <div class="kp-show">
        <button type="button" class="kp-trigger" aria-haspopup="listbox" aria-expanded="false" aria-labelledby="${uid}-label ${uid}-cur">
          <span id="${uid}-cur"></span>
          <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
        </button>
        <div class="kp-pop" hidden>
          <input class="kp-search" type="search" placeholder="Filter by id, document or text…" aria-label="Filter the list" autocomplete="off"
                 role="combobox" aria-controls="${uid}-list" aria-expanded="true">
          <div class="kp-list" id="${uid}-list" role="listbox" aria-label="Series to show"></div>
        </div>
      </div>
    </div>
    <div class="kp-crumb"></div>`;
  const $ = sel => root.querySelector(sel);
  const groupsEl = $('.kp-groups'), trigger = $('.kp-trigger'), pop = $('.kp-pop'), search = $('.kp-search'), list = $('.kp-list');
  const group = () => KP_GROUPS.find(g => g.id === st.group);
  const levelBadge = l => `<span class="badge ${l === 'ERROR' ? 'badge-error' : l === 'WARN' ? 'badge-warn' : 'badge-info'}">${l}</span>`;
  const verdictBadge = v => v === 'safe' ? '<span class="badge badge-green">SAFE</span>' : '<span class="badge badge-error">ERROR</span>';

  // Active documents of this service (or of none), and those with a linked template here (a verdict key needs one).
  const svcDocs = () => st.docs.filter(d => !d.service || d.service === st.service);
  const linkedDocIds = () => new Set(st.templates.filter(kpLinked).map(t => t.doc_id));
  const inGroup = (t, id) => id === 'all' || (kpIsDocGroup(id) ? kpLinked(t) && t.doc_verdict === id && st.docIds.has(t.doc_id) : levelClass(t.level) === id);
  const linkedIn = id => st.templates.filter(t => kpLinked(t) && t.doc_id === id).length;   // linked templates in this service
  const groupDocs = id => {
    const ds = svcDocs();
    if (id === 'all') { const ids = linkedDocIds(); return ds.filter(d => ids.has(d.doc_id)); }
    if (kpIsDocGroup(id)) { const ids = linkedDocIds(); return ds.filter(d => d.verdict === id && ids.has(d.doc_id)); }
    const ids = new Set(st.templates.filter(t => kpLinked(t) && inGroup(t, id)).map(t => t.doc_id));
    return ds.filter(d => ids.has(d.doc_id));
  };
  const groupCount = id => st.groups[id] || 0;   // exact, from /api/baseline/groups

  function renderGroups() {
    groupsEl.innerHTML = KP_GROUPS.map(g => {
      if (g.sep) return `<span class="kp-gl">${g.sep}</span>`;
      const n = groupCount(g.id), on = g.id === st.group;
      return `<button type="button" class="toggle-btn${on ? ' active' : ''}" aria-pressed="${on}" data-g="${g.id}"${n ? '' : ` disabled title="Nothing in this group for ${escapeHtml(st.service)}"`}>`
        + `${g.dot ? `<span class="kp-dot ${g.dot}" aria-hidden="true"></span>` : ''}${g.label}<span class="kp-n">${n}</span></button>`;
    }).join('');
  }

  // Options for the current group: the combined series, then documents, then templates.
  function options(q) {
    const g = group(), ql = q.trim().toLowerCase();
    const hit = (...xs) => !ql || xs.some(x => String(x || '').toLowerCase().includes(ql));
    const tpls = (st.remote || st.templates).filter(t => inGroup(t, g.id)).sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
    const out = [];
    if (hit(g.combined, g.key)) out.push({sec: 'Combined'}, {key: g.key, html: `<strong>${escapeHtml(g.combined)}</strong><span class="kp-txt">${escapeHtml(g.key)}</span>`});
    const ds = groupDocs(g.id).filter(d => hit(d.doc_id, d.title));
    if (ds.length) out.push({sec: `Documents (${ds.length})`}, ...ds.map(d => ({key: `doc:${d.doc_id}`,
      html: `<span class="tmpl-id">${escapeHtml(d.doc_id)}</span>${verdictBadge(d.verdict)}<span class="kp-txt">${escapeHtml(d.title)} · ${linkedIn(d.doc_id)} templates</span>`})));
    const ts = st.remote ? tpls : tpls.filter(t => hit(t.template_id, t.text, t.doc_id));   // the server already matched a remote list
    if (ts.length) out.push({sec: `Templates (${ts.length})`}, ...ts.map(t => ({key: t.template_id,
      html: `<span class="tmpl-id">${escapeHtml(t.template_id)}</span>${levelBadge(levelClass(t.level))}<span class="kp-txt">${escapeHtml(t.text)}</span>`})));
    if (st.failed) out.push({sec: 'Search failed: showing the loaded templates only'});
    // The pages load at most 500 templates; the rest is reached by typing (server search).
    if (st.groups.all > st.templates.length) out.push({sec: `Showing ${ts.length} of ${groupCount(g.id)}`});
    return out;
  }
  function renderList() {
    st.opts = options(search.value);
    const pick = st.opts.filter(o => o.key);
    st.active = Math.min(st.active, Math.max(pick.length - 1, 0));
    let i = -1;
    list.innerHTML = pick.length ? st.opts.map(o => o.sec
      ? `<div class="kp-sec" role="presentation">${escapeHtml(o.sec)}</div>`
      : `<div class="kp-opt${o.key === st.key ? ' cur' : ''}" role="option" id="${uid}-o${++i}" data-key="${escapeHtml(o.key)}" aria-selected="${i === st.active}">${o.html}</div>`).join('')
      : '<div class="kp-empty">Nothing in this group matches. Try another group or clear the filter.</div>';
    const a = $(`#${uid}-o${st.active}`);
    if (a) { a.scrollIntoView({block: 'nearest'}); search.setAttribute('aria-activedescendant', a.id); }
  }
  function setOpen(open) {
    st.open = open;
    pop.hidden = !open;
    trigger.setAttribute('aria-expanded', String(open));
    clearTimeout(st.timer); st.rseq++;
    if (open) { search.value = ''; st.remote = null; st.failed = false; st.active = 0; renderList(); search.focus(); }
  }
  function choose(key) { setOpen(false); trigger.focus(); onPick(key); }

  function render({service, key, templates, groups, docs, baselineState}) {
    Object.assign(st, {service, key, templates: templates || [], groups: groups || {}, docs: docs || [], baselineState: baselineState || null});
    st.docIds = new Set(svcDocs().map(d => d.doc_id));
    st.group = keyGroup(key, st.templates, st.docs);
    renderGroups();
    const g = group(), label = keyLabel(key, st.templates, st.docs), gi = KP_GROUPS.find(x => x.key === key);
    $(`#${uid}-cur`).textContent = label;
    trigger.title = label;
    const young = gi && gi.fresh && (st.baselineState === 'none' || st.baselineState === 'young');
    const how = gi ? (gi.derived ? 'derived: sum of linked templates'
                    : young ? 'new stored key: young baseline, full after 5 weekdays + 4 weekend days' : 'stored key')
      : key.startsWith('doc:') ? 'derived: sum of linked templates' : 'single template';
    $('.kp-crumb').innerHTML = `Showing <strong>${escapeHtml(g.label)}</strong> › <strong>${escapeHtml(label)}</strong> <code>${escapeHtml(key)}</code> <span class="link-meta">${escapeHtml(how)}</span>`;
  }

  groupsEl.addEventListener('click', e => {
    const b = e.target.closest('[data-g]');
    if (b && !b.disabled) onPick(KP_GROUPS.find(g => g.id === b.dataset.g).key);   // a group opens on its combined series
  });
  trigger.addEventListener('click', () => setOpen(!st.open));
  search.addEventListener('input', () => {
    clearTimeout(st.timer);
    const seq = ++st.rseq;   // any in-flight search is now stale
    st.remote = null; st.failed = false; st.active = 0; renderList();
    const q = search.value.trim();
    if (!q || !(st.groups.all > st.templates.length)) return;
    st.timer = setTimeout(async () => {
      try {
        const r = await getJson(`/api/templates?service=${encodeURIComponent(st.service)}&q=${encodeURIComponent(q)}&size=50`, 'Unable to search templates');
        if (seq !== st.rseq || !st.open) return;
        st.remote = r.items; st.active = 0; renderList();
      } catch (err) { if (seq === st.rseq && st.open) { st.failed = true; renderList(); } }
    }, 300);
  });
  search.addEventListener('keydown', e => {
    const pick = st.opts.filter(o => o.key);
    if (e.key === 'ArrowDown') { st.active = Math.min(st.active + 1, pick.length - 1); renderList(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { st.active = Math.max(st.active - 1, 0); renderList(); e.preventDefault(); }
    else if (e.key === 'Enter' && pick[st.active]) { e.preventDefault(); choose(pick[st.active].key); }
    else if (e.key === 'Escape') { setOpen(false); trigger.focus(); }
  });
  list.addEventListener('click', e => { const o = e.target.closest('[data-key]'); if (o) choose(o.dataset.key); });
  document.addEventListener('click', e => { if (st.open && !$('.kp-show').contains(e.target)) setOpen(false); });
  return {render};
}
