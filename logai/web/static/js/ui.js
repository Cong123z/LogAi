document.querySelectorAll('[data-close]').forEach(button => {
  button.addEventListener('click', () => {
    document.getElementById(button.dataset.close).classList.remove('open');
    if (button.dataset.close === 'delete-confirm-modal') {
      state.pendingDocumentDeletion = null;
      clearTimeout(state.documentationDeletionPollTimer);
    }
    if (button.dataset.close === 'clear-confirm-modal') {
      state.pendingDocumentationClear = null;
      clearTimeout(state.documentationPollTimer);
    }
  });
});


document.getElementById('grouping-operation-dismiss').addEventListener('click', () => document.getElementById('grouping-operation-status').classList.remove('visible'));

// ── Modal controller ──
// Views open and close dialogs by toggling .open on a .modal-overlay; this
// watches that class so every dialog gets the same keyboard behavior: focus
// moves in, Tab stays inside, Escape closes, focus returns to the opener.
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const modalOpeners = new Map();
const openModals = () => [...document.querySelectorAll('.modal-overlay.open')];

function focusables(overlay) {
  return [...overlay.querySelectorAll(FOCUSABLE)].filter(el => el.offsetParent !== null && !el.closest('[hidden]'));
}

function onModalOpened(overlay) {
  const opener = document.activeElement;
  if (opener && !overlay.contains(opener)) modalOpeners.set(overlay, opener);
  // A view may already have focused a field (e.g. the editor's first input).
  requestAnimationFrame(() => {
    updateCharCounts(overlay);  // after the view has filled the fields
    if (overlay.contains(document.activeElement)) return;
    const preferred = overlay.querySelector('[autofocus], .form-field input:not([type="hidden"]), .form-field textarea, .form-field select');
    const target = (preferred && preferred.offsetParent !== null) ? preferred : focusables(overlay).find(el => !el.classList.contains('modal-close')) || focusables(overlay)[0];
    if (target) target.focus();
  });
}

function onModalClosed(overlay) {
  const opener = modalOpeners.get(overlay);
  modalOpeners.delete(overlay);
  if (overlay.id === 'confirm-modal') settleConfirm(false);
  if (opener && document.contains(opener) && !openModals().length) opener.focus();
}

const modalObserver = new MutationObserver(records => {
  records.forEach(record => {
    const overlay = record.target;
    const wasOpen = (record.oldValue || '').split(/\s+/).includes('open');
    const isOpen = overlay.classList.contains('open');
    if (isOpen && !wasOpen) onModalOpened(overlay);
    if (!isOpen && wasOpen) onModalClosed(overlay);
  });
});
document.querySelectorAll('.modal-overlay').forEach(overlay => {
  modalObserver.observe(overlay, {attributes: true, attributeFilter: ['class'], attributeOldValue: true});
});

function closeModal(overlay) {
  // Prefer the dialog's own close button so its cleanup handler runs.
  const close = overlay.querySelector(`[data-close="${overlay.id}"]`) || overlay.querySelector('.modal-close');
  if (close) close.click();
  else overlay.classList.remove('open');
}

document.addEventListener('keydown', event => {
  const overlay = openModals().pop();
  if (!overlay) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeModal(overlay);
    return;
  }
  if (event.key !== 'Tab') return;
  const items = focusables(overlay);
  if (!items.length) { event.preventDefault(); return; }
  const first = items[0], last = items[items.length - 1];
  if (event.shiftKey && (document.activeElement === first || !overlay.contains(document.activeElement))) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && (document.activeElement === last || !overlay.contains(document.activeElement))) {
    event.preventDefault(); first.focus();
  }
});

// ── confirmDialog / notifyDialog: in-page replacements for confirm() and alert() ──
let confirmResolve = null;
function settleConfirm(result) {
  const resolve = confirmResolve;
  confirmResolve = null;
  if (resolve) resolve(result);
}

function confirmDialog(message, {title = 'Are you sure?', confirmLabel = 'Confirm', danger = true, cancel = true} = {}) {
  settleConfirm(false);
  document.getElementById('confirm-modal-title').textContent = title;
  document.getElementById('confirm-modal-message').textContent = message;
  const ok = document.getElementById('confirm-modal-ok');
  ok.textContent = confirmLabel;
  ok.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;
  document.getElementById('confirm-modal-cancel').hidden = !cancel;
  document.getElementById('confirm-modal').classList.add('open');
  return new Promise(resolve => { confirmResolve = resolve; });
}

function notifyDialog(message, title = 'Something went wrong') {
  return confirmDialog(message, {title, confirmLabel: 'OK', danger: false, cancel: false});
}

document.getElementById('confirm-modal-ok').addEventListener('click', () => {
  settleConfirm(true);
  document.getElementById('confirm-modal').classList.remove('open');
});

// ── Character counters for limited fields in dialogs ──
function updateCharCounts(root) {
  root.querySelectorAll('.form-field input[maxlength], .form-field textarea[maxlength]').forEach(field => {
    if (field.type === 'password' || field.type === 'number') return;
    let counter = field.parentElement.querySelector(`.char-count[data-for="${field.id}"]`);
    if (!counter) {
      counter = document.createElement('span');
      counter.className = 'char-count';
      counter.dataset.for = field.id;
      counter.setAttribute('aria-hidden', 'true');  // the browser already enforces maxlength
      field.insertAdjacentElement('afterend', counter);
    }
    const max = Number(field.maxLength);
    counter.textContent = `${field.value.length.toLocaleString()} / ${max.toLocaleString()}`;
    counter.classList.toggle('near-limit', field.value.length >= max * 0.9);
  });
}
document.addEventListener('input', event => {
  const field = event.target.closest('.form-field input[maxlength], .form-field textarea[maxlength]');
  if (field) updateCharCounts(field.closest('.form-field'));
});
