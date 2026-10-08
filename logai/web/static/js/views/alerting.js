function renderAlerts(data) {
  state.alerts = data.items;
  const counts = data.counts || {};
  document.getElementById('alert-stat-total').textContent = data.total;
  document.getElementById('alert-stat-alerting').textContent = counts.ALERTING || 0;
  document.getElementById('alert-stat-warming').textContent = counts.WARMING || 0;
  document.getElementById('alert-stat-cooling').textContent = counts.COOLING || 0;
  document.getElementById('alert-stat-normal').textContent = counts.NORMAL || 0;
  state.alertsUpdatedAt = Date.now();
  renderAlertFreshness();

  let items = state.alertFilter === 'all'
    ? [...data.items]
    : data.items.filter(item => item.alert_state === state.alertFilter);
  const stateRank = {NORMAL:0, COOLING:1, WARMING:2, ALERTING:3};
  const levelRank = {TRACE:0, DEBUG:1, INFO:2, WARN:3, WARNING:3, ERROR:4, FATAL:5, CRITICAL:5};
  items.sort((left, right) => {
    const value = item => {
      if (state.alertSort === 'alert_state') return stateRank[item.alert_state] ?? -1;
      if (state.alertSort === 'level') return levelRank[item.level] ?? levelRank.INFO;
      return item[state.alertSort] ?? '';
    };
    const a = value(left);
    const b = value(right);
    const comparison = compareValues(a, b);
    return state.alertOrder === 'asc' ? comparison : -comparison;
  });
  if (!items.length) {
    $alertsTbody.innerHTML = '<tr><td colspan="10" class="empty-state">No groups match this alert state.</td></tr>';
    return;
  }
  $alertsTbody.innerHTML = items.map(item => `
    <tr>
      <td><span class="badge alert-state-${escapeHtml(item.alert_state.toLowerCase())}">${escapeHtml(item.alert_state)}</span></td>
      <td><span class="badge ${levelBadgeClass(item.level)}">${escapeHtml(item.level)}</span></td>
      <td><span class="group-id" title="${escapeHtml(item.group_id)}">${escapeHtml(item.group_id)}</span></td>
      <td>${escapeHtml(item.service)}</td>
      <td><span class="tmpl-id">${escapeHtml(item.error_code || '—')}</span></td>
      <td>${Number(item.anomaly_score || 0).toFixed(3)}</td>
      <td>${formatCount(item.consecutive_anomaly_count || 0)}</td>
      <td title="${formatFullTs(item.timestamp)}">${formatTs(item.timestamp)}</td>
      <td>${aiCell(item)}</td>
      <td><div class="tmpl-text" title="${escapeHtml(item.representative_template || '')}">${highlightWildcards(escapeHtml(item.representative_template || '—'))}</div></td>
    </tr>`).join('');
}

// ── AI analysis status shared by the Alerts table and AI Insights ──
function insightStatus(kind, a) {
  if (!a) return {label: 'Not analyzed', cls: 'st-none', busy: false};
  if (a.status === 'requested' || a.status === 'pending') return {label: 'Analyzing…', cls: 'st-busy', busy: true};
  if (a.status === 'failed') return {label: 'Failed', cls: 'st-failed', busy: false};
  if (kind === 'service') {
    const health = ['healthy', 'degraded', 'critical'].includes(a.health) ? a.health : 'unknown';
    return {label: health[0].toUpperCase() + health.slice(1), cls: `st-${health}`, busy: false};
  }
  if (kind === 'template') {
    const verdict = ['suspicious', 'benign', 'unsure'].includes(a.verdict) ? a.verdict : 'unsure';
    return {label: verdict[0].toUpperCase() + verdict.slice(1), cls: `st-${verdict}`, busy: false};
  }
  if (a.documentation_id) return {label: `${a.documentation_id} · ${Math.round((a.confidence || 0) * 100)}%`, cls: 'st-matched', busy: false};
  return {label: 'Suggestion', cls: 'st-suggestion', busy: false};
}

function aiCell(item) {
  const status = insightStatus('window', item.analysis);
  const label = item.analysis ? status.label : 'Analyze';
  return `<a class="ai-btn ${status.cls}" href="#insights" data-open-insight="${escapeHtml(item.key)}"${status.busy ? ' aria-busy="true"' : ''} aria-label="AI analysis for ${escapeHtml(item.group_id)} on ${escapeHtml(item.service)}: ${escapeHtml(label)}">${escapeHtml(label)}</a>`;
}


document.querySelectorAll('#view-alerting th[data-alert-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const field = th.dataset.alertSort;
    if (state.alertSort === field) {
      state.alertOrder = state.alertOrder === 'desc' ? 'asc' : 'desc';
    } else {
      state.alertSort = field;
      state.alertOrder = field === 'group_id' || field === 'service' || field === 'error_code' ? 'asc' : 'desc';
    }
    updateAlertSortUI();
    renderAlerts(alertDataFromState());
  });
});

function updateAlertSortUI() {
  document.querySelectorAll('#view-alerting th[data-alert-sort]').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    const active = th.dataset.alertSort === state.alertSort;
    th.classList.toggle('sorted', active);
    arrow.textContent = active ? (state.alertOrder === 'desc' ? '▼' : '▲') : '';
  });
}

function alertDataFromState() {
  const counts = {};
  state.alerts.forEach(item => { counts[item.alert_state] = (counts[item.alert_state] || 0) + 1; });
  return {items:state.alerts, total:state.alerts.length, counts};
}


$alertsTbody.addEventListener('click', event => {
  const link = event.target.closest('[data-open-insight]');
  if (!link) return;
  ins.tab = 'windows';
  ins.filter.windows = 'all';
  ins.selected.windows = link.dataset.openInsight;
  ins.signature = null;
});


document.querySelectorAll('#alert-status-toggle .toggle-btn').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('#alert-status-toggle .toggle-btn').forEach(item => item.classList.remove('active'));
    button.classList.add('active');
    state.alertFilter = button.dataset.alertStatus;
    document.querySelectorAll('[data-alert-card]').forEach(card => card.setAttribute('aria-pressed', String(card.dataset.alertCard === state.alertFilter)));
    renderAlerts(alertDataFromState());
  });
});

// The KPI cards filter the table too, through the same toggle.
document.querySelectorAll('[data-alert-card]').forEach(card => card.addEventListener('click', () => {
  document.querySelector(`#alert-status-toggle [data-alert-status="${card.dataset.alertCard}"]`).click();
}));

// "Updated Xs ago", turning into a warning when polling stops delivering.
const ALERT_STALE_SECONDS = 10;
function renderAlertFreshness() {
  const label = document.getElementById('alert-last-updated');
  if (!state.alertsUpdatedAt) return;
  const age = Math.round((Date.now() - state.alertsUpdatedAt) / 1000);
  const stale = state.activeView === 'alerting' && age > ALERT_STALE_SECONDS;
  label.textContent = stale ? `Stale: last update ${age}s ago` : `Live · updated ${age < 2 ? 'just now' : `${age}s ago`}`;
  label.closest('.live-status').classList.toggle('stale', stale);
}
setInterval(renderAlertFreshness, 1000);
