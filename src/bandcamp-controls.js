import { invokeLogged as invoke } from './diagnostics.js';
import { getCurrentWebview } from '@tauri-apps/api/webview';

document.body.classList.add('bandcamp-controls');
document.querySelector('#app').innerHTML = `
  <h2>Bandcamp verification</h2>
  <p id="query" class="hint"></p>
  <p class="instructions">Complete any verification on the page to the right. When results are ready, they will be transferred and this window will close automatically. Closing it earlier cancels the search.</p>
  <button id="use-all-results" type="button">Use all results</button>
  <div class="verification-buttons">
    <button id="refresh-results" type="button">Refresh results</button>
    <button id="return-search" type="button">Return to search</button>
  </div>
  <p id="verification-status" role="status" aria-live="polite">Loading Bandcamp…</p>
  <ul id="choices" aria-label="Selectable results"></ul>
  <p id="selection-count" class="hint">No results selected</p>
  <button id="confirm-selection" type="button" disabled>Confirm selection</button>
  <p class="hint">Saves catalog references. Preview and full file not yet verified.</p>
  <button id="cancel-verification" type="button">Close</button>`;

const status = document.querySelector('#verification-status');
const selected = new Set();
let results = [];
let generation = null;
let busy = false;
let pendingPick = null;
let resetPending = false;
let selectionHint = null;
let automatic = false;

function render() {
  document.querySelector('#choices').replaceChildren(...results.map(track => {
    const row = document.createElement('li');
    const label = document.createElement('label');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = selected.has(track.url);
    checkbox.disabled = busy;
    checkbox.onchange = () => {
      if (checkbox.checked) selected.add(track.url); else selected.delete(track.url);
      render();
    };
    const caption = document.createElement('span');
    caption.textContent = `${track.title} — ${track.artist || 'Unknown artist'}`;
    label.append(checkbox, caption);
    row.dataset.selected = String(checkbox.checked);
    row.append(label);
    return row;
  }));
  document.querySelector('#selection-count').textContent = `${selected.size} selected out of ${results.length}`;
  document.querySelector('#confirm-selection').disabled = busy || !selected.size;
  document.querySelector('#refresh-results').disabled = busy;
  document.querySelector('#return-search').disabled = busy;
  document.querySelector('#use-all-results').disabled = busy;
}

function pick(url, hint = null) {
  const resultUrl = hint?.link_url === url ? hint.result_url : url;
  if (!results.some(track => track.url === resultUrl)) {
    status.textContent = 'The clicked link does not identify a track in the list. Select the checkbox next to its title in the panel.';
    return;
  }
  selected.add(resultUrl);
  status.textContent = 'Result selected. Press «Confirm selection» to save it in SweatyDL.';
  render();
  const index = results.findIndex(track => track.url === resultUrl);
  document.querySelector('#choices').children[index]?.scrollIntoView({ block: 'nearest' });
}

async function context() {
  const value = await invoke('bandcamp_context');
  automatic = !!value.automatic;
  if (automatic) document.body.classList.add('automatic-verification');
  document.querySelector('#query').textContent = value.query || '';
  if (generation !== value.session) {
    generation = value.session;
    selected.clear();
    results = [];
    selectionHint = null;
  }
}

async function readResults() {
  if (busy) return;
  busy = true;
  status.textContent = 'Reading Bandcamp results…';
  render();
  let readSucceeded = false;
  try {
    await context();
    const report = await invoke('import_bandcamp');
    generation = report.session;
    if (report.query) document.querySelector('#query').textContent = report.query;
    results = report.results;
    selectionHint = report.selection_hint || null;
    readSucceeded = true;
    for (const url of selected) {
      if (!results.some(track => track.url === url)) selected.delete(url);
    }
    status.textContent = results.length
      ? `${results.length} results ready. Choose the ones you want and confirm.`
      : 'No selectable tracks or albums on this page.';
  } catch (error) {
    // No stale choices may be confirmed after a failed refresh or a new query.
    results = [];
    selected.clear();
    selectionHint = null;
    status.textContent = String(error);
  } finally {
    busy = false;
    render();
    if (resetPending) {
      resetPending = false;
      pendingPick = null;
      void readResults();
    } else if (pendingPick) {
      const url = pendingPick;
      pendingPick = null;
      if (readSucceeded) pick(url, selectionHint);
    }
  }
}

document.querySelector('#refresh-results').onclick = readResults;
document.querySelector('#use-all-results').onclick = async () => {
  if (busy) return;
  busy = true;
  pendingPick = null;
  status.textContent = 'Reading and transferring Bandcamp results…';
  render();
  try {
    const count = await invoke('use_bandcamp_results');
    status.textContent = `${count} results transferred. Choose which ones to save in the main window.`;
  } catch (error) {
    selected.clear();
    results = [];
    selectionHint = null;
    status.textContent = String(error);
  }
  finally {
    busy = false;
    render();
    if (resetPending) {
      resetPending = false;
      void readResults();
    }
  }
};
document.querySelector('#return-search').onclick = async () => {
  if (busy) return;
  selected.clear();
  results = [];
  render();
  try {
    await invoke('return_bandcamp_search');
    status.textContent = 'Search reopened. Wait for it to load, then refresh the results.';
  } catch (error) { status.textContent = String(error); }
};
document.querySelector('#confirm-selection').onclick = async () => {
  if (busy || !selected.size) return;
  busy = true;
  render();
  try {
    const count = await invoke('confirm_bandcamp', { selectedUrls: [...selected], generation });
    status.textContent = `${count} results confirmed. You can find them in the selection in the main window.`;
  } catch (error) { status.textContent = String(error); }
  finally {
    busy = false;
    render();
    if (resetPending) {
      resetPending = false;
      pendingPick = null;
      void readResults();
    } else if (pendingPick) {
      void readResults();
    }
  }
};
document.querySelector('#cancel-verification').onclick = async () => {
  try { await invoke('close_bandcamp'); }
  catch (error) { status.textContent = String(error); }
};

async function start() {
  await context();
  const view = getCurrentWebview();
  await view.listen('bandcamp-auto-status', ({ payload: message }) => {
    status.textContent = message;
  });
  await view.listen('bandcamp-picked', ({ payload: url }) => {
    if (!busy && results.some(track => track.url === url)) {
      pick(url);
      return;
    }
    pendingPick = url;
    void readResults();
  });
  await view.listen('bandcamp-page-loaded', () => { if (!automatic) void readResults(); });
  await view.listen('bandcamp-reset', () => {
    selected.clear();
    results = [];
    selectionHint = null;
    pendingPick = null;
    if (busy) resetPending = true; else if (!automatic) void readResults();
    render();
  });
  if (automatic) status.textContent = 'Waiting for verification and results to load…';
  else await readResults();
}
start().catch(error => { status.textContent = `Bandcamp connection unavailable: ${error}`; });
