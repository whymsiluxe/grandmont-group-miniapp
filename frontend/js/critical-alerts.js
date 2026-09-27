// Глобальный попап критических алертов (Фаза 10.16) — крутится независимо от того,
// какой view открыт, поверх всего приложения. Polling — тот же паттерн, что чат
// (см. startChatPolling()/_chatPollTick() в chat.js), без WebSocket.

const CRITICAL_ALERT_POLL_MS = 15000;
let _criticalAlertModalOpen = false;
let _criticalAlertQueue = [];
let _criticalAlertPollTimer = null;

function initCriticalAlertsPolling() {
  if (_criticalAlertPollTimer) return;
  _pollCriticalAlerts();
  _criticalAlertPollTimer = setInterval(_pollCriticalAlerts, CRITICAL_ALERT_POLL_MS);
}

// 22.09 (iPhone screenshot audit, item found live): the queue used to be
// WHOLESALE REPLACED on every 15s poll (`_criticalAlertQueue = data.alerts`),
// which could show/queue the same alert twice across polls with no id-based
// dedup, and _closeCriticalAlertModal() removed by ARRAY POSITION
// (`queue.shift()`), not by the id of the alert actually being closed -- if a
// poll replaced the array while a modal was open, shift() could remove the
// wrong entry entirely. A real device confirmed stacked, near-identical
// critical-alert popups from this. Fixed: merge polled alerts into the
// existing queue by id (dedup, don't replace), and close/advance by id.
let _criticalAlertShownIds = new Set(); // alerts already shown this session -- never re-show after being displayed once, even if a stale poll response still lists it
let _criticalAlertAckedIds = new Set(); // optimistic local ack state -- a poll response that hasn't caught up yet must not resurrect one we already acked
let _criticalAlertCurrentId = null; // id of the alert currently on screen, if any

async function _pollCriticalAlerts() {
  try {
    const data = await api('/api/critical-alerts/pending');
    const fresh = (data.alerts || []).filter(a => !_criticalAlertAckedIds.has(a.id));

    // 27.09 (owner report: critical popup shown 3x again): existingIds was a
    // snapshot taken ONCE before this loop -- if a single poll response ever
    // contains the same alert id more than once (backend aggregation glitch,
    // or a stale+fresh copy of the same record), both copies passed this
    // check and were pushed into the queue, since neither push updated the
    // set it was itself checked against. Updating it as we go makes this
    // loop dedup within a single poll response too, not just across polls.
    const existingIds = new Set(_criticalAlertQueue.map(a => a.id));
    for (const alert of fresh) {
      if (existingIds.has(alert.id)) continue;
      existingIds.add(alert.id);
      _criticalAlertQueue.push(alert);
    }
    // Reconcile: drop anything from the local queue the server no longer
    // considers pending (acked from another device, resolved, etc.) --
    // except the one currently on screen, which finishes its own close flow.
    const freshIds = new Set(fresh.map(a => a.id));
    const openId = _criticalAlertModalOpen ? _criticalAlertCurrentId : null;
    _criticalAlertQueue = _criticalAlertQueue.filter(a => freshIds.has(a.id) || a.id === openId);

    if (_criticalAlertQueue.length && !_criticalAlertModalOpen) {
      _showNextCriticalAlert();
    }
  } catch (e) {}
}

function _showNextCriticalAlert() {
  // Invariant enforced here, not just by callers: the same alert must never
  // be presented while a modal is already open, or after it was already
  // shown/acked this session -- both checked at this single choke point so a
  // future caller can't reintroduce the bug by forgetting to check first.
  if (_criticalAlertModalOpen) return;
  const next = _criticalAlertQueue.find(a => !_criticalAlertShownIds.has(a.id) && !_criticalAlertAckedIds.has(a.id));
  if (!next) return;
  _showCriticalAlertModal(next);
}

function _showCriticalAlertModal(alert) {
  _criticalAlertModalOpen = true;
  _criticalAlertCurrentId = alert.id;
  _criticalAlertShownIds.add(alert.id);
  const now = Math.floor(Date.now() / 1000);
  const isResolutionPrompt = alert.deadline_at && now >= alert.deadline_at && !alert.resolution;

  const modal = document.createElement('div');
  modal.id = 'critical-alert-modal';
  modal.innerHTML = isResolutionPrompt ? _criticalAlertResolutionHtml(alert) : _criticalAlertAckHtml(alert);
  document.body.appendChild(modal);
  hapticImpact('heavy');

  if (isResolutionPrompt) {
    document.getElementById('ca-resolve-yes').addEventListener('click', () => _resolveCriticalAlert(alert.id, 'yes'));
    document.getElementById('ca-resolve-no').addEventListener('click', () => _showCriticalAlertResolveNoForm(alert.id));
  } else {
    document.getElementById('ca-ack-btn').addEventListener('click', () => _ackCriticalAlert(alert.id));
  }
}

function _criticalAlertAckHtml(alert) {
  return `
    <div class="critical-alert-inner">
      <div class="critical-alert-icon">🔴</div>
      <div class="critical-alert-title">${esc(alert.title)}</div>
      ${alert.subtitle ? `<div class="critical-alert-subtitle">${esc(alert.subtitle)}</div>` : ''}
      <textarea id="ca-comment-input" class="critical-alert-comment" placeholder="Комментарий (опционально)…" rows="3"></textarea>
      <button class="critical-alert-btn critical-alert-btn-primary" id="ca-ack-btn">Принял</button>
    </div>`;
}

function _criticalAlertResolutionHtml(alert) {
  return `
    <div class="critical-alert-inner">
      <div class="critical-alert-icon">⏰</div>
      <div class="critical-alert-title">Вопрос решён?</div>
      <div class="critical-alert-subtitle">${esc(alert.title)}</div>
      <div class="critical-alert-actions-row">
        <button class="critical-alert-btn critical-alert-btn-yes" id="ca-resolve-yes">Да</button>
        <button class="critical-alert-btn critical-alert-btn-no" id="ca-resolve-no">Нет</button>
      </div>
    </div>`;
}

async function _ackCriticalAlert(alertId) {
  const comment = document.getElementById('ca-comment-input')?.value.trim() || '';
  try {
    await api(`/api/critical-alerts/${alertId}/ack`, {
      method: 'POST',
      body: JSON.stringify({ comment }),
    });
    hapticImpact('light');
  } catch (e) {
    showToast('Ошибка: ' + e.message, 'error');
    return;
  }
  // Optimistic local ack -- an in-flight/already-queued poll response that
  // hasn't caught up to this ack yet must not resurrect this alert.
  _criticalAlertAckedIds.add(alertId);
  _closeCriticalAlertModal(alertId);
}

function _showCriticalAlertResolveNoForm(alertId) {
  const inner = document.querySelector('#critical-alert-modal .critical-alert-inner');
  inner.innerHTML = `
    <div class="critical-alert-icon">✍️</div>
    <div class="critical-alert-title">Укажите причину</div>
    <textarea id="ca-resolve-note-input" class="critical-alert-comment" placeholder="Почему вопрос не решён…" rows="3"></textarea>
    <button class="critical-alert-btn critical-alert-btn-primary" id="ca-resolve-no-submit">Отправить</button>
  `;
  document.getElementById('ca-resolve-no-submit').addEventListener('click', async () => {
    const note = document.getElementById('ca-resolve-note-input').value.trim();
    if (!note) { showToast('Укажите причину', 'error'); return; }
    await _submitCriticalAlertResolution(alertId, 'no', note, []);
  });
}

async function _resolveCriticalAlert(alertId, resolution) {
  if (resolution === 'yes') {
    _criticalAlertPendingResolveId = alertId;
    document.getElementById('critical-alert-camera-input').click();
    return;
  }
}

let _criticalAlertPendingResolveId = null;

async function _handleCriticalAlertPhotos(files) {
  if (!_criticalAlertPendingResolveId || !files.length) return;
  await _submitCriticalAlertResolution(_criticalAlertPendingResolveId, 'yes', '', files);
  _criticalAlertPendingResolveId = null;
}

async function _submitCriticalAlertResolution(alertId, resolution, note, files) {
  try {
    const formData = new FormData();
    formData.append('resolution', resolution);
    formData.append('note', note);
    for (const f of files) formData.append('files', f);
    const res = await fetch(`${API_BASE}/api/critical-alerts/${alertId}/resolve`, {
      method: 'POST',
      headers: { ..._authHeaders() },
      body: formData,
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    hapticImpact('medium');
  } catch (e) {
    showToast('Ошибка: ' + e.message, 'error');
    return;
  }
  _criticalAlertAckedIds.add(alertId);
  _closeCriticalAlertModal(alertId);
}

// 22.09 (iPhone screenshot audit): removes by ID, not array position -- see
// the comment on _pollCriticalAlerts() for why position-based shift() was
// unsafe (a poll could replace/reorder the queue while a modal was open).
function _closeCriticalAlertModal(closedAlertId) {
  const modal = document.getElementById('critical-alert-modal');
  if (modal) modal.remove();
  _criticalAlertModalOpen = false;
  _criticalAlertCurrentId = null;
  if (closedAlertId != null) {
    _criticalAlertQueue = _criticalAlertQueue.filter(a => a.id !== closedAlertId);
  }
  if (_criticalAlertQueue.length) {
    setTimeout(() => _showNextCriticalAlert(), 300);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const input = document.createElement('input');
  input.type = 'file';
  input.id = 'critical-alert-camera-input';
  input.accept = 'image/*';
  input.capture = 'environment';
  input.multiple = true;
  input.style.display = 'none';
  document.body.appendChild(input);
  input.addEventListener('change', e => {
    _handleCriticalAlertPhotos(Array.from(e.target.files));
    e.target.value = '';
  });
});
