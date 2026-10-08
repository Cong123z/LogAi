// ── State ────────────────────────────────────────────────────────
const state = {
  search: '',
  service: '',
  level: '',
  status: 'all',
  sort: 'last_seen',
  order: 'desc',
  page: 1,
  perPage: 50,
  autoRefresh: false,
  refreshTimer: null,
  documents: [],
  documentRevision: null,
  overrideRevision: null,
  assignmentGroup: null,
  undocumentedGroupsOnly: false,
  singleGroupsOnly: false,
  groups: [],
  groupSort: 'error_code',
  groupOrder: 'asc',
  documentSort: 'group_count',
  documentOrder: 'desc',
  documentSynchronization: {},
  alerts: [],
  alertFilter: 'all',
  alertSort: 'alert_state',
  alertOrder: 'desc',
  alertRefreshTimer: null,
  activeView: 'templates',
  reconnectTimer: null,
  groupingRevision: null,
  groupingSynchronization: {},
  groupingTemplate: null,
  groupingMode: 'existing',
  groupingPollTimer: null,
  documentationPollTimer: null,
  documentationDeletionPollTimer: null,
  pendingDocumentDeletion: null,
  pendingDocumentationClear: null,
  documentationMutationsBlocked: false,
  expandedGroups: new Set(),
};

// ── DOM refs ─────────────────────────────────────────────────────
const $tbody = document.getElementById('template-tbody');
const $search = document.getElementById('search-input');
const $service = document.getElementById('filter-service');
const $level = document.getElementById('filter-level');
const $statusToggle = document.getElementById('status-toggle');
const $autoRefresh = document.getElementById('auto-refresh');
const $pageInfo = document.getElementById('page-info');
const $pageBtns = document.getElementById('page-btns');
const $modalOverlay = document.getElementById('modal-overlay');
const $modalClose = document.getElementById('modal-close');
const $detailGrid = document.getElementById('detail-grid');
const $groupsTbody = document.getElementById('groups-tbody');
const $alertsTbody = document.getElementById('alerts-tbody');
const $documentationTbody = document.getElementById('documentation-tbody');
const $documentModal = document.getElementById('document-modal');
const $assignmentModal = document.getElementById('assignment-modal');
const $groupAssignmentModal = document.getElementById('group-assignment-modal');
const $deleteConfirmModal = document.getElementById('delete-confirm-modal');
const $clearConfirmModal = document.getElementById('clear-confirm-modal');

// ── Helpers ──────────────────────────────────────────────────────
function formatTs(epoch) {
  if (!epoch) return '—';
  const d = new Date(epoch * 1000);
  const now = Date.now();
  const diff = (now - d.getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function formatFullTs(epoch) {
  if (!epoch) return '—';
  return new Date(epoch * 1000).toLocaleString();
}

function highlightWildcards(text) {
  if (!text) return '';
  return text.replace(/&lt;\*&gt;/g, '<span class="wildcard">&lt;*&gt;</span>')
             .replace(/<\*>/g, '<span class="wildcard">&lt;*&gt;</span>');
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  // Also used inside quoted attributes, so quotes must be escaped too.
  return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function levelBadgeClass(level) {
  const l = (level || 'INFO').toUpperCase();
  if (l === 'ERROR' || l === 'FATAL' || l === 'CRITICAL') return 'badge-error';
  if (l === 'WARN' || l === 'WARNING') return 'badge-warn';
  if (l === 'INFO') return 'badge-info';
  return 'badge-debug';
}

// Table sort order: numbers numerically, everything else as natural text.
function compareValues(a, b) {
  return typeof a === 'number' && typeof b === 'number'
    ? a - b
    : String(a).localeCompare(String(b), undefined, {numeric:true, sensitivity:'base'});
}

function formatCount(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}

// ── API ──────────────────────────────────────────────────────────
async function fetchTemplates() {
  const params = new URLSearchParams();
  if (state.search) params.set('search', state.search);
  if (state.service) params.set('service', state.service);
  if (state.level) params.set('level', state.level);
  if (state.status !== 'all') params.set('status', state.status);
  params.set('sort', state.sort);
  params.set('order', state.order);
  params.set('page', state.page);
  params.set('per_page', state.perPage);

  const resp = await fetch(`/api/templates?${params}`);
  if (!resp.ok) throw new Error('Unable to load templates');
  return resp.json();
}

async function fetchGroups() {
  const resp = await fetch('/api/groups');
  if (!resp.ok) throw new Error('Unable to load groups');
  return resp.json();
}

async function fetchDocumentation() {
  const resp = await fetch('/api/documentation');
  if (!resp.ok) throw new Error('Unable to load documentation');
  return resp.json();
}

async function fetchAlerts() {
  const resp = await fetch('/api/alerts');
  if (!resp.ok) throw new Error('Unable to load alert state');
  return resp.json();
}


async function apiMutation(url, method, body) {
  const resp = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const data = await resp.json();
  if (!resp.ok) {
    const error = new Error(data.message || data.error || 'Request failed');
    error.status = resp.status;
    error.payload = data;
    throw error;
  }
  return data;
}

function showGroupingOperation(stateName, message) {
  const container = document.getElementById('grouping-operation-status');
  container.className = `operation-status visible ${stateName === 'applied' ? 'applied' : (stateName === 'failed' ? 'failed' : '')}`;
  document.getElementById('grouping-operation-message').textContent = message;
}
