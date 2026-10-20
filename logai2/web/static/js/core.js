// ── State ────────────────────────────────────────────────────────
const state = {
  activeView: 'pipeline',
  reconnectTimer: null,
  alertRefreshTimer: null,
};

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

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  // Also used inside quoted attributes, so quotes must be escaped too.
  return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── API ──────────────────────────────────────────────────────────
async function getJson(url, errorMessage) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(errorMessage);
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
