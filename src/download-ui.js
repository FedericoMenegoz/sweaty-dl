export function catalogReference(raw) {
  if (typeof raw !== 'string' || raw.length > 8192 || /[\u0000-\u0020\u007f]/.test(raw)) throw new Error('URL non valido');
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.username || url.password || url.port
    || !url.hostname.includes('.') || /^[\d.]+$/.test(url.hostname) || url.hostname.includes(':')
    || /\.(local|localhost|internal)$/.test(url.hostname)) throw new Error('URL HTTPS pubblico richiesto');
  url.hash = '';
  let source = 'URL';
  if (url.hostname.endsWith('.bandcamp.com') || url.hostname === 'bandcamp.com') {
    if (!/^\/(track|album)\/[^/]+\/?$/.test(url.pathname)) throw new Error('Collegamento a brano o album richiesto');
    source = 'Bandcamp'; url.search = ''; url.pathname = url.pathname.replace(/\/$/, '');
  } else if (['youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be'].includes(url.hostname)) source = 'YouTube';
  else if (['soundcloud.com', 'www.soundcloud.com'].includes(url.hostname)) source = 'SoundCloud';
  const title = decodeURIComponent(url.pathname.split('/').filter(Boolean).at(-1) || url.hostname).replace(/[-_]/g, ' ');
  return { url: url.href, title, artist: '', source, downloadStatus: 'pending' };
}

export function parseUrlText(text, existing = []) {
  if (typeof text !== 'string' || text.length > 1024 * 1024) throw new Error('Il TXT deve contenere al massimo 1 MB.');
  const lines = text.replace(/^\uFEFF/, '').split(/\r\n|\r|\n/);
  if (lines.length > 10000) throw new Error('Il TXT deve contenere al massimo 10.000 righe.');
  const seen = new Set(existing.map(item => {
    try { return catalogReference(item.url).url; } catch { return item.url; }
  }));
  const added = [], invalid = [];
  let duplicates = 0;
  lines.forEach((line, index) => {
    const value = line.trim();
    if (!value) return;
    try {
      const track = catalogReference(value);
      if (seen.has(track.url)) { duplicates++; return; }
      seen.add(track.url); added.push(track);
    } catch { invalid.push(index + 1); }
  });
  if (existing.length + added.length > 2000) throw new Error('La selezione può contenere al massimo 2.000 voci.');
  return { added, duplicates, invalid };
}

export function initDownloads({ document, invoke, isTauri, getQueue, save, stopPreview, storage, listen, canStart = () => true, diagnose = () => {} }) {
  const input = document.querySelector('#url-file');
  const message = document.querySelector('#download-status');
  const destination = document.querySelector('#destination');
  const choose = document.querySelector('#choose-destination');
  choose.hidden = !globalThis.navigator?.platform?.includes('Mac');
  const start = document.querySelector('#download-start');
  const cancel = document.querySelector('#download-cancel');
  const progress = document.querySelector('#download-progress');
  let busy = false, cancelled = false, activeId = null;
  try { destination.value = storage.getItem('sweatydl-next-destination') || ''; } catch { /* optional preference */ }
  const update = () => {
    start.disabled = busy; choose.disabled = busy; cancel.hidden = !busy; input.disabled = busy; destination.disabled = busy;
    for (const button of document.querySelectorAll?.('#queue button, #results button') || []) button.disabled = busy;
  };
  choose.onclick = async () => {
    if (busy || !isTauri()) return;
    try {
      const folder = await invoke('choose_download_directory');
      if (folder) { destination.value = folder; try { storage.setItem('sweatydl-next-destination', folder); } catch { /* optional */ } }
    } catch (error) { message.textContent = String(error); }
  };
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file || busy) return;
    try {
      if (file.size > 1024 * 1024) throw new Error('Il TXT deve contenere al massimo 1 MB.');
      const report = parseUrlText(await file.text(), getQueue());
      diagnose('txt_imported', { bytes: file.size, added: report.added.length, duplicates: report.duplicates, invalid_lines: report.invalid });
      getQueue().push(...report.added);
      const persisted = save();
      message.textContent = `${report.added.length} URL aggiunti; ${report.duplicates} duplicati.${report.invalid.length ? ` Righe non valide: ${report.invalid.slice(0, 15).join(', ')}${report.invalid.length > 15 ? '…' : ''}.` : ''}${persisted === false ? ' Salvataggio della selezione non riuscito.' : ''}`;
    } catch (error) { diagnose('txt_import_failed', { error: String(error) }, 'error'); message.textContent = error.message; }
    finally { input.value = ''; }
  };
  cancel.onclick = async () => {
    cancelled = true; cancel.disabled = true;
    message.textContent = 'Annullamento in corso…';
    if (activeId) {
      try { await invoke('cancel_download', { requestId: activeId }); }
      catch { message.textContent = 'Annullamento richiesto. Attendo la fine del trasferimento corrente…'; }
    }
  };
  start.onclick = async () => {
    if (busy) return;
    if (!canStart()) { message.textContent = 'Attendi la fine della ricerca prima di avviare il download.'; return; }
    if (!isTauri()) { message.textContent = 'Avvia l’app Tauri per scaricare.'; return; }
    const pending = getQueue().filter(track => track.downloadStatus !== 'done');
    if (!pending.length) { message.textContent = 'Aggiungi prima dei brani alla selezione o importa un TXT.'; return; }
    busy = true; cancelled = false; cancel.disabled = false; update(); stopPreview();
    let completed = 0, failed = 0;
    try {
      if (!destination.value.trim()) destination.value = await invoke('download_directory');
      if (cancelled) { message.textContent = 'Coda interrotta.'; return; }
      try { storage.setItem('sweatydl-next-destination', destination.value.trim()); } catch { /* optional preference */ }
      for (const [index, track] of pending.entries()) {
        if (cancelled) break;
        activeId = `download-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
        track.downloadStatus = 'running'; delete track.downloadError; save(); update();
        progress.hidden = false; progress.removeAttribute('value');
        message.textContent = `${index + 1}/${pending.length} · ${track.title} · Preparazione…`;
        try {
          const result = await invoke('download_audio', { url: track.url, destination: destination.value.trim(), requestId: activeId });
          if (!Array.isArray(result.files) || !result.files.length) throw new Error('Nessun file confermato.');
          track.downloadStatus = 'done'; track.downloadFiles = result.files;
          completed++; progress.value = 1;
          // Keep a bounded, local history; never store resolved stream URLs.
          try {
            const raw = JSON.parse(storage.getItem('sweatydl-next-download-history') || '[]');
            const history = Array.isArray(raw) ? raw : [];
            history.push({ url: track.url, title: track.title, files: result.files, kind: result.kind, at: new Date().toISOString() });
            storage.setItem('sweatydl-next-download-history', JSON.stringify(history.slice(-200)));
          } catch { /* Queue still records confirmed files. */ }
        } catch (error) {
          track.downloadStatus = cancelled ? 'pending' : 'error';
          track.downloadError = cancelled ? '' : String(error).slice(0, 500); failed += cancelled ? 0 : 1;
        } finally { activeId = null; save(); update(); }
      }
      message.textContent = `${cancelled ? 'Coda interrotta. ' : ''}${completed} voci scaricate; ${failed} errori.${failed ? ' Premi Scarica per riprovare le voci fallite.' : ''}`;
    } catch (error) { message.textContent = String(error); }
    finally { activeId = null; busy = false; progress.hidden = true; update(); }
  };
  if (isTauri()) listen('download-progress', ({ payload }) => {
    if (!busy || !activeId || payload.request_id !== activeId || cancelled) return;
    if (typeof payload.progress === 'number') progress.value = payload.progress;
    else progress.removeAttribute('value');
    const track = getQueue().find(item => item.downloadStatus === 'running');
    message.textContent = `${track?.title || 'Audio'} · ${payload.total ? `Brano ${payload.index}/${payload.total} · ` : ''}${typeof payload.progress === 'number' ? `${Math.round(payload.progress * 100)}%` : 'Download in corso…'}`;
  }).catch(() => { /* Final command response remains authoritative. */ });
  return { busy: () => busy };
}
