import { getCurrentWebview } from '@tauri-apps/api/webview';
import { isTauri } from '@tauri-apps/api/core';
import { invokeLogged as invoke, initDiagnostics, recordDiagnostic } from './diagnostics.js';
import { initDownloads } from './download-ui.js';
import { canPreview, createPreviewPlayer } from './preview-player.js';

const app = document.querySelector('#app');
app.innerHTML = `
  <form id="search"><label>Titolo<input name="title" required maxlength="500" placeholder="Cerca un pezzo"></label>
  <label>Artista<input name="artist" maxlength="500" placeholder="Facoltativo"></label>
  <label>Sorgente<select name="mode"><option value="auto">Automatica</option><option>Bandcamp</option><option>SoundCloud</option><option>YouTube</option></select></label>
  <button id="search-button">Cerca</button></form>
  <p id="status" role="status"></p>
  <div id="preview-player" hidden><span id="preview-status" role="status"></span>
    <progress id="preview-progress" max="1" value="0" aria-label="Avanzamento anteprima"></progress>
    <button type="button" id="stop-preview">■ Ferma</button></div>
  <div id="bandcamp-actions" hidden>
    <p>Completa l’eventuale verifica Bandcamp. I risultati arriveranno qui automaticamente.</p>
    <button type="button" id="close-bandcamp">Annulla ricerca Bandcamp</button>
  </div>
  <div class="panels"><section><h2>Risultati</h2><ul id="results"></ul></section>
  <section><h2>Selezione <span id="count">0</span></h2><ul id="queue"></ul><p class="hint">▶ Anteprima Bandcamp. Download dell’audio disponibile sul sito, nel formato sorgente; la durata viene verificata prima del salvataggio.</p></section></div>
  <div id="download-tools">
    <label class="file-picker">Importa TXT<input id="url-file" type="file" accept=".txt,text/plain" aria-label="Importa un TXT con un URL per riga"></label>
    <label>Cartella di destinazione<div class="destination-picker"><input id="destination" placeholder="Download/SweatyDL (predefinita)" aria-label="Percorso completo della cartella di destinazione"><button type="button" id="choose-destination" aria-label="Scegli cartella">…</button></div></label>
    <button type="button" id="download-start">Scarica</button>
    <button type="button" id="download-cancel" hidden>Annulla</button>
  </div>
  <div id="download-feedback"><p id="download-status" role="status">TXT: un URL HTTPS per riga, anche da fonti diverse supportate dall’estrattore.</p>
    <progress id="download-progress" max="1" hidden aria-label="Avanzamento download"></progress></div>
  <footer><span>SweatyDL · versione di sviluppo</span><button type="button" id="show-diagnostics">Log</button></footer>
  <dialog id="diagnostic-dialog" aria-labelledby="diagnostic-title"><h2 id="diagnostic-title">Diagnostica</h2>
    <p id="diagnostic-status" role="status"></p>
    <textarea id="diagnostic-content" readonly spellcheck="false" aria-label="Eventi del log"></textarea>
    <div class="diagnostic-buttons"><button type="button" id="copy-diagnostics">Copia</button>
      <button type="button" id="open-log-folder">Mostra cartella</button><button type="button" id="close-diagnostics">Chiudi</button></div>
  </dialog>`;
initDiagnostics(document);
let queue = [];
let searchBusy = false;
try {
  const saved = JSON.parse(localStorage.getItem('sweatydl-next-queue') || '[]');
  if (Array.isArray(saved)) queue = saved.filter(validTrack).map(track => ({ ...track, downloadStatus: track.downloadStatus === 'running' ? 'pending' : track.downloadStatus }));
} catch { /* A corrupt local queue must not block startup. */ }
function validTrack(track) {
  if (!track || typeof track.title !== 'string' || typeof track.url !== 'string') return false;
  try { return new URL(track.url).protocol === 'https:'; } catch { return false; }
}
const status = document.querySelector('#status');
const previewButtons = new Set();
const preview = createPreviewPlayer({
  resolve: (track, requestId) => invoke('prepare_preview', { url: track.url, requestId }),
  cancel: requestId => invoke('cancel_preview', { requestId }),
  diagnose: (name, details, id) => recordDiagnostic(name, details, name.endsWith('_failed') ? 'error' : 'info', id),
  onChange: state => {
    document.querySelector('#preview-player').hidden = !state.message;
    document.querySelector('#preview-status').textContent = state.message;
    document.querySelector('#preview-progress').value = state.progress;
    document.querySelector('#stop-preview').hidden = ['idle', 'error'].includes(state.phase);
    for (const button of previewButtons) {
      if (!button.isConnected) { previewButtons.delete(button); continue; }
      const running = button.dataset.url === state.url && ['loading', 'playing'].includes(state.phase);
      button.textContent = running ? '■' : '▶';
      button.title = running ? 'Ferma anteprima' : 'Ascolta circa 10 secondi';
      button.setAttribute('aria-label', running ? 'Ferma anteprima' : `Anteprima: ${button.dataset.title}`);
    }
  },
});
document.querySelector('#stop-preview').onclick = () => preview.stop();
globalThis.addEventListener?.('pagehide', () => preview.stop(''));
status.textContent = isTauri() ? 'Pronto. Inserisci il titolo da cercare.' : 'Anteprima interfaccia: avvia Tauri per collegare la ricerca.';
function row(track, label, action) {
  const li = document.createElement('li');
  const text = document.createElement('span');
  const downloadLabel = track.downloadStatus === 'done' ? ' · ✓ Scaricato'
    : track.downloadStatus === 'running' ? ' · Download…'
    : track.downloadStatus === 'error' ? ` · ${track.downloadError}` : '';
  text.textContent = `${track.title} — ${track.artist || ''} (${track.source || ''})${downloadLabel}`;
  const button = document.createElement('button');
  button.type = 'button'; button.textContent = label; button.onclick = () => { if (!downloads.busy()) action(); };
  li.append(text);
  if (canPreview(track)) {
    const listen = document.createElement('button');
    listen.type = 'button'; listen.textContent = '▶';
    listen.title = `Ascolta circa 10 secondi di ${track.title}`;
    listen.dataset.url = track.url;
    listen.dataset.title = track.title;
    listen.setAttribute('aria-label', `Anteprima: ${track.title}`);
    listen.onclick = () => { if (!downloads.busy()) preview.toggle(track); };
    previewButtons.add(listen);
    li.append(listen);
  }
  li.append(button); return li;
}
function renderQueue() {
  document.querySelector('#queue').replaceChildren(...queue.map(track => row(track, 'Rimuovi', () => {
    queue = queue.filter(item => item.url !== track.url); renderQueue();
  })));
  document.querySelector('#count').textContent = queue.length;
  try { localStorage.setItem('sweatydl-next-queue', JSON.stringify(queue)); return true; }
  catch (error) { void recordDiagnostic('queue_save_failed', { error: String(error), entries: queue.length }, 'error'); status.textContent = 'Impossibile salvare la selezione su questo dispositivo.'; return false; }
}
renderQueue();
const downloads = initDownloads({ document, invoke, isTauri, getQueue: () => queue, save: renderQueue,
  stopPreview: () => preview.stop(''), storage: localStorage, canStart: () => !searchBusy,
  listen: (name, handler) => getCurrentWebview().listen(name, handler),
  diagnose: recordDiagnostic,
});
function showResults(report) {
  if (downloads.busy()) return 0;
  preview.stop('');
  const results = report.results.filter(validTrack);
  document.querySelector('#results').replaceChildren(...results.map(track => row(track, 'Salva', () => {
    if (!queue.some(item => item.url === track.url)) queue.push(track);
    renderQueue();
  })));
  return results.length;
}
async function searchBandcamp(query) {
  document.querySelector('#bandcamp-actions').hidden = false;
  document.querySelector('#close-bandcamp').disabled = false;
  status.textContent = 'Completa l’eventuale verifica Bandcamp. Attendo i risultati…';
  return invoke('search_bandcamp_browser', { query });
}
document.querySelector('#close-bandcamp').onclick = async () => {
  document.querySelector('#close-bandcamp').disabled = true;
  try { await invoke('close_bandcamp'); }
  catch (error) { status.textContent = String(error); }
};
document.querySelector('#search').onsubmit = async event => {
  event.preventDefault();
  if (searchBusy || downloads.busy()) return;
  if (!isTauri()) { status.textContent = 'La ricerca richiede npm run tauri dev.'; return; }
  preview.stop('');
  searchBusy = true;
  const button = document.querySelector('#search-button'); button.disabled = true;
  status.textContent = 'Ricerca in corso…';
  document.querySelector('#close-bandcamp').disabled = true;
  document.querySelector('#bandcamp-actions').hidden = true;
  document.querySelector('#results').replaceChildren();
  try {
    const input = Object.fromEntries(new FormData(event.target));
    let report;
    if (input.mode === 'Bandcamp') {
      const query = [input.artist.trim(), input.title.trim()].filter(Boolean).join(' ');
      report = await searchBandcamp(query);
    } else {
      report = await invoke('search', input);
      if (report.browser_query) report = await searchBandcamp(report.browser_query);
    }
    const count = showResults(report);
    status.textContent = `${count} risultati. ${Object.values(report.errors).join(' ')}`;
  } catch (error) { status.textContent = String(error); }
  finally {
    searchBusy = false;
    button.disabled = false;
    document.querySelector('#bandcamp-actions').hidden = true;
  }
};

if (isTauri()) {
  getCurrentWebview().listen('bandcamp-results-imported', ({ payload: report }) => {
    const count = showResults(report);
    status.textContent = `${count} risultati importati dalla pagina Bandcamp. Premi «Salva» sulle voci che desideri conservare.`;
  }).catch(error => { status.textContent = `Importazione da Bandcamp non disponibile: ${error}`; });
  getCurrentWebview().listen('bandcamp-confirmed', ({ payload: report }) => {
    const selected = report.results.filter(validTrack);
    showResults(report);
    let added = 0;
    for (const track of selected) {
      if (!queue.some(item => item.url === track.url)) { queue.push(track); added++; }
    }
    if (renderQueue()) {
      status.textContent = `${added} riferimenti aggiunti alla selezione (${selected.length} confermati da Bandcamp). File completo non ancora verificato.`;
    }
  }).catch(error => { status.textContent = `Collegamento con Bandcamp non disponibile: ${error}`; });
}
