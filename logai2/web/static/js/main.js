// Same order as the sidebar sections; swipe and the slide direction follow it.
// Unknown routes fall back to Pipeline.
const viewTitles = {
  alerting: 'Alerting',
  pipeline: 'Pipeline',
  templates: 'Templates',
  documentation: 'Documentation',
  sources: 'Data sources',
  baseline: 'Baseline',
};
// Templates polls only through its Auto-refresh toggle; Documentation and
// Baseline load on entry and after each change (Baseline polls a running recompute).
const POLL_MS = {alerting: 5000, pipeline: 2000, sources: 2000};
const viewOrder = Object.keys(viewTitles);

function setView(view, updateHash = true) {
  if (!viewTitles[view]) view = 'pipeline';
  const previousView = state.activeView;
  const previousIndex = viewOrder.indexOf(previousView);
  const nextIndex = viewOrder.indexOf(view);
  const direction = nextIndex > previousIndex ? 'slide-from-right' : 'slide-from-left';
  state.activeView = view;
  document.querySelectorAll('.view').forEach(section => {
    const active = section.id === `view-${view}`;
    section.classList.toggle('active', active);
    section.classList.remove('slide-from-left', 'slide-from-right');
    if (active && previousView !== view) {
      // Re-trigger the short pager animation when moving between views.
      void section.offsetWidth;
      section.classList.add(direction);
    }
  });
  document.querySelectorAll('.sidebar-nav a[data-view]').forEach(link => {
    const active = link.dataset.view === view;
    link.classList.toggle('active', active);
    if (active && window.matchMedia('(max-width: 768px)').matches) {
      link.scrollIntoView({behavior: 'smooth', block: 'nearest', inline: 'nearest'});
    }
  });
  document.getElementById('page-title').textContent = viewTitles[view];
  if (updateHash && window.location.hash !== `#${view}`) history.pushState(null, '', `#${view}`);
  if (state.alertRefreshTimer) {
    clearInterval(state.alertRefreshTimer);
    state.alertRefreshTimer = null;
  }
  loadData();
  if (POLL_MS[view]) state.alertRefreshTimer = setInterval(loadData, POLL_MS[view]);
}

document.querySelectorAll('.sidebar-nav a[data-view]').forEach(link => {
  link.addEventListener('click', event => {
    event.preventDefault();
    setView(link.dataset.view);
  });
});

// A horizontal swipe over the page body moves through the same views as
// the navigation. Interactive controls keep their native touch behavior.
let swipeStart = null;
const mainContent = document.querySelector('.main');
const shouldIgnoreSwipe = target => Boolean(target.closest('input, select, textarea, button, a, .table-wrap, .modal'));
mainContent.addEventListener('touchstart', event => {
  if (event.touches.length !== 1 || shouldIgnoreSwipe(event.target)) {
    swipeStart = null;
    return;
  }
  const touch = event.touches[0];
  swipeStart = {x: touch.clientX, y: touch.clientY};
}, {passive: true});
mainContent.addEventListener('touchend', event => {
  if (!swipeStart || event.changedTouches.length !== 1) return;
  const touch = event.changedTouches[0];
  const deltaX = touch.clientX - swipeStart.x;
  const deltaY = touch.clientY - swipeStart.y;
  swipeStart = null;
  if (Math.abs(deltaX) < 56 || Math.abs(deltaX) < Math.abs(deltaY) * 1.2) return;
  const currentIndex = viewOrder.indexOf(state.activeView);
  const nextIndex = deltaX < 0 ? currentIndex + 1 : currentIndex - 1;
  if (nextIndex >= 0 && nextIndex < viewOrder.length) setView(viewOrder[nextIndex]);
}, {passive: true});
window.addEventListener('hashchange', () => setView(window.location.hash.slice(1), false));
document.getElementById('reload-app').addEventListener('click', () => window.location.reload());

// ── Theme: OS preference until the viewer picks one (remembered per browser) ──
function currentTheme() {
  const picked = document.documentElement.dataset.theme;
  if (picked) return picked;
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}
function updateThemeToggle() {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  const button = document.getElementById('theme-toggle');
  button.setAttribute('aria-label', `Switch to ${next} theme`);
  button.title = `Switch to ${next} theme`;
}
document.getElementById('theme-toggle').addEventListener('click', () => {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('logai.theme', next); } catch (e) { /* storage unavailable */ }
  updateThemeToggle();
});
updateThemeToggle();

// ── Sidebar: collapse to an icon rail on desktop (remembered per browser) ──
const $sidebar = document.querySelector('.sidebar');
const $sidebarCollapse = document.getElementById('sidebar-collapse');
function setSidebarCollapsed(collapsed) {
  $sidebar.classList.toggle('collapsed', collapsed);
  $sidebarCollapse.setAttribute('aria-expanded', String(!collapsed));
  $sidebarCollapse.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
}
try { setSidebarCollapsed(localStorage.getItem('logai.sidebar') === 'collapsed'); } catch (e) { /* storage unavailable */ }
$sidebarCollapse.addEventListener('click', () => {
  const collapsed = !$sidebar.classList.contains('collapsed');
  setSidebarCollapsed(collapsed);
  try { localStorage.setItem('logai.sidebar', collapsed ? 'collapsed' : 'expanded'); } catch (e) { /* storage unavailable */ }
});

// ── Engine health pill (heartbeat in worker_status; stale after 30 s) ──
async function refreshHealth() {
  const pill = document.getElementById('engine-health');
  const text = document.getElementById('engine-health-text');
  let label = 'Web unreachable', cls = 'unavailable', since = null;
  try {
    const data = await getJson('/api/pipeline', 'Unable to load pipeline');
    since = data.status && data.status.heartbeat;
    cls = data.stale ? 'unavailable' : ((data.status.warnings || []).length ? 'degraded' : 'healthy');
    label = {healthy: 'Engine healthy', degraded: 'Engine degraded', unavailable: 'Engine unavailable'}[cls];
  } catch (err) { /* keep the unreachable label */ }
  pill.className = `health-pill ${cls}`;
  text.textContent = label;
  pill.title = since ? `Last heartbeat ${formatFullTs(since)}` : 'No engine heartbeat';
}
refreshHealth();
setInterval(refreshHealth, 5000);

// ── Main Load ────────────────────────────────────────────────────
async function loadData() {
  try {
    if (state.activeView === 'alerting') {
      renderAlerts(await fetchAlerts());
    } else if (state.activeView === 'templates') {
      await loadTemplates();
    } else if (state.activeView === 'pipeline') {
      renderPipeline(await fetchPipeline());
    } else if (state.activeView === 'sources') {
      const sources = await fetchSources();
      renderSources(sources);
      renderConnection(sources.connection);
      await loadLlmProfiles();
    } else if (state.activeView === 'documentation') {
      await loadDocumentation();
    } else if (state.activeView === 'baseline') {
      await loadBaseline();
    }
    document.getElementById('connection-status').hidden = true;
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
  } catch (err) {
    console.error('Failed to load data:', err);
    document.getElementById('connection-status').hidden = false;
    if (!state.reconnectTimer) {
      state.reconnectTimer = setTimeout(() => {
        state.reconnectTimer = null;
        loadData();
      }, 2000);
    }
  }
}

// ── Init ─────────────────────────────────────────────────────────
setView(window.location.hash.slice(1) || 'pipeline', false);
