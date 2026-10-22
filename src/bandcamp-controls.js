import { invokeLogged as invoke } from './diagnostics.js';
import { getCurrentWebview } from '@tauri-apps/api/webview';

document.body.classList.add('bandcamp-controls');
document.querySelector('#app').innerHTML = `
  <h2>Verifica Bandcamp</h2>
  <p id="query" class="hint"></p>
  <p class="instructions">Completa l’eventuale verifica nella pagina a destra. Appena i risultati sono pronti, verranno trasferiti e questa finestra si chiuderà automaticamente. Chiudere prima annulla la ricerca.</p>
  <button id="use-all-results" type="button">Usa tutti i risultati</button>
  <div class="verification-buttons">
    <button id="refresh-results" type="button">Aggiorna risultati</button>
    <button id="return-search" type="button">Torna alla ricerca</button>
  </div>
  <p id="verification-status" role="status" aria-live="polite">Caricamento Bandcamp…</p>
  <ul id="choices" aria-label="Risultati selezionabili"></ul>
  <p id="selection-count" class="hint">Nessun risultato selezionato</p>
  <button id="confirm-selection" type="button" disabled>Conferma selezione</button>
  <p class="hint">Salva riferimenti di catalogo. Anteprima e file completo non ancora verificati.</p>
  <button id="cancel-verification" type="button">Chiudi</button>`;

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
    caption.textContent = `${track.title} — ${track.artist || 'Artista non indicato'}`;
    label.append(checkbox, caption);
    row.dataset.selected = String(checkbox.checked);
    row.append(label);
    return row;
  }));
  document.querySelector('#selection-count').textContent = `${selected.size} selezionati su ${results.length}`;
  document.querySelector('#confirm-selection').disabled = busy || !selected.size;
  document.querySelector('#refresh-results').disabled = busy;
  document.querySelector('#return-search').disabled = busy;
  document.querySelector('#use-all-results').disabled = busy;
}

function pick(url, hint = null) {
  const resultUrl = hint?.link_url === url ? hint.result_url : url;
  if (!results.some(track => track.url === resultUrl)) {
    status.textContent = 'Il collegamento cliccato non identifica un brano dell’elenco. Seleziona la casella accanto al titolo nel pannello.';
    return;
  }
  selected.add(resultUrl);
  status.textContent = 'Risultato selezionato. Premi «Conferma selezione» per salvarlo in SweatyDL.';
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
  status.textContent = 'Lettura dei risultati Bandcamp…';
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
      ? `${results.length} risultati pronti. Scegli quelli che desideri e conferma.`
      : 'Nessun brano o album selezionabile in questa pagina.';
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
  status.textContent = 'Lettura e trasferimento dei risultati Bandcamp…';
  render();
  try {
    const count = await invoke('use_bandcamp_results');
    status.textContent = `${count} risultati trasferiti. Scegli quali salvare nella finestra principale.`;
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
    status.textContent = 'Ricerca riaperta. Attendi il caricamento, poi aggiorna i risultati.';
  } catch (error) { status.textContent = String(error); }
};
document.querySelector('#confirm-selection').onclick = async () => {
  if (busy || !selected.size) return;
  busy = true;
  render();
  try {
    const count = await invoke('confirm_bandcamp', { selectedUrls: [...selected], generation });
    status.textContent = `${count} risultati confermati. Li trovi nella selezione della finestra principale.`;
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
  if (automatic) status.textContent = 'Attendo la verifica e il caricamento dei risultati…';
  else await readResults();
}
start().catch(error => { status.textContent = `Collegamento Bandcamp non disponibile: ${error}`; });
