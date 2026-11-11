import { invoke as nativeInvoke, isTauri } from '@tauri-apps/api/core';

let sequence = 0;
const newId = () => globalThis.crypto?.randomUUID?.() || `ui-${Date.now()}-${++sequence}`;

export function recordDiagnostic(eventName, details = {}, level = 'info', requestId = '') {
  if (!isTauri()) return Promise.resolve();
  return nativeInvoke('diagnostic_event', { eventName, details, level, requestId }).catch(() => {});
}

// Never log complete IPC arguments/results: they can contain signed media URLs.
export function createLoggedInvoker(invoke, record = recordDiagnostic, makeId = newId) {
  const report = (...args) => {
    try { Promise.resolve(record(...args)).catch(() => {}); } catch { /* Logging must not affect the operation. */ }
  };
  return async (command, args = {}) => {
    const id = args.requestId || makeId();
    const started = Date.now();
    const details = { command, mode: args.mode };
    report('ipc_started', details, 'info', id);
    try {
      const result = await invoke(command, { ...args, traceId: id });
      report('ipc_completed', { command, elapsed_ms: Date.now() - started,
        result_count: Array.isArray(result?.results) ? result.results.length : undefined }, 'info', id);
      return result;
    } catch (error) {
      report('ipc_failed', { command, elapsed_ms: Date.now() - started,
        error: String(error), stack: error?.stack }, 'error', id);
      // Native errors are user-facing messages. Never display a raw arbitrary Error object.
      const message = typeof error === 'string' ? error : 'Operation failed. Check the log.';
      throw message.includes('[ref. ') ? message : `${message} [ref. ${id}]`;
    }
  };
}
export const invokeLogged = createLoggedInvoker(nativeInvoke);

export function initDiagnostics(document, record = recordDiagnostic) {
  const dialog = document.querySelector('#diagnostic-dialog');
  const status = document.querySelector('#diagnostic-status');
  const content = document.querySelector('#diagnostic-content');
  document.querySelector('#show-diagnostics').onclick = async () => {
    dialog.showModal();
    status.textContent = 'Loading log…'; content.value = '';
    try {
      const report = await nativeInvoke('diagnostic_report');
      status.textContent = `${report.path} · Session ${report.session}`;
      content.value = report.text || 'No events recorded.';
    } catch { status.textContent = 'Log unavailable. Launch the Tauri app and check the folder permissions.'; }
  };
  document.querySelector('#close-diagnostics').onclick = () => dialog.close();
  document.querySelector('#copy-diagnostics').onclick = async () => {
    try {
      if (globalThis.navigator?.clipboard?.writeText) await navigator.clipboard.writeText(content.value);
      else { content.focus(); content.select(); if (!document.execCommand('copy')) throw new Error(); }
      status.textContent = 'Diagnostics copied.';
    } catch { content.focus(); content.select(); status.textContent = 'Text selected: press ⌘C or Ctrl+C to copy it.'; }
  };
  document.querySelector('#open-log-folder').onclick = async () => {
    try { await nativeInvoke('show_log_directory'); }
    catch { status.textContent = 'Unable to open the log folder.'; }
  };
  globalThis.addEventListener?.('error', event => {
    void record('javascript_error', { message: event.message, file: event.filename,
      line: event.lineno, column: event.colno, stack: event.error?.stack }, 'error');
  });
  globalThis.addEventListener?.('unhandledrejection', event => {
    void record('unhandled_rejection', { error: String(event.reason), stack: event.reason?.stack }, 'error');
  });
}
