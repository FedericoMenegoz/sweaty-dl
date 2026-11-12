export function catalogReference(raw) {
  if (typeof raw !== 'string' || raw.length > 8192 || /[\u0000-\u0020\u007f]/.test(raw)) throw new Error('Invalid URL');
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.username || url.password || url.port
    || !url.hostname.includes('.') || /^[\d.]+$/.test(url.hostname) || url.hostname.includes(':')
    || /\.(local|localhost|internal)$/.test(url.hostname)) throw new Error('A public HTTPS URL is required');
  url.hash = '';
  let source = 'URL';
  if (url.hostname.endsWith('.bandcamp.com') || url.hostname === 'bandcamp.com') {
    if (!/^\/(track|album)\/[^/]+\/?$/.test(url.pathname)) throw new Error('A track or album link is required');
    source = 'Bandcamp'; url.search = ''; url.pathname = url.pathname.replace(/\/$/, '');
  } else if (['youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be'].includes(url.hostname)) source = 'YouTube';
  else if (['soundcloud.com', 'www.soundcloud.com'].includes(url.hostname)) source = 'SoundCloud';
  const title = decodeURIComponent(url.pathname.split('/').filter(Boolean).at(-1) || url.hostname).replace(/[-_]/g, ' ');
  return { url: url.href, title, artist: '', source, downloadStatus: 'pending' };
}

export function parseUrlText(text, existing = []) {
  if (typeof text !== 'string' || text.length > 1024 * 1024) throw new Error('The TXT file must be no larger than 1 MB.');
  const lines = text.replace(/^\uFEFF/, '').split(/\r\n|\r|\n/);
  if (lines.length > 10000) throw new Error('The TXT file must contain no more than 10,000 lines.');
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
  if (existing.length + added.length > 2000) throw new Error('The selection can contain no more than 2,000 items.');
  return { added, duplicates, invalid };
}

export function initDownloads({ document, invoke, isTauri, getQueue, save, stopPreview, storage, listen, canStart = () => true, diagnose = () => {}, onBusyChange = () => {} }) {
  const input = document.querySelector('#url-file');
  const message = document.querySelector('#download-status');
  const destination = document.querySelector('#destination');
  const choose = document.querySelector('#choose-destination');
  choose.hidden = !globalThis.navigator?.platform?.includes('Mac');
  const start = document.querySelector('#download-start');
  const cancel = document.querySelector('#download-cancel');
  const progress = document.querySelector('#download-progress');
  let busy = false, importing = false, cancelled = false, activeId = null;
  try { destination.value = storage.getItem('sweatydl-next-destination') || ''; } catch { /* optional preference */ }
  const update = () => {
    const locked = busy || importing || !canStart();
    start.disabled = locked; choose.disabled = locked; cancel.hidden = !busy; input.disabled = locked; destination.disabled = locked;
    for (const button of document.querySelectorAll?.('#queue button, #results button') || []) button.disabled = locked;
    onBusyChange();
  };
  choose.onclick = async () => {
    if (busy || importing || !canStart() || !isTauri()) return;
    try {
      const folder = await invoke('choose_download_directory');
      if (folder) { destination.value = folder; try { storage.setItem('sweatydl-next-destination', folder); } catch { /* optional */ } }
    } catch (error) { message.textContent = String(error); }
  };
  input.onchange = async () => {
    const file = input.files?.[0];
    if (!file || busy || importing || !canStart()) return;
    // Hold the queue while file.text() is pending, just as during a download.
    importing = true; update();
    try {
      if (file.size > 1024 * 1024) throw new Error('The TXT file must be no larger than 1 MB.');
      const report = parseUrlText(await file.text(), getQueue());
      diagnose('txt_imported', { bytes: file.size, added: report.added.length, duplicates: report.duplicates, invalid_lines: report.invalid });
      getQueue().push(...report.added);
      const persisted = save();
      message.textContent = `URLs added: ${report.added.length}; duplicates: ${report.duplicates}.${report.invalid.length ? ` Invalid lines: ${report.invalid.slice(0, 15).join(', ')}${report.invalid.length > 15 ? '…' : ''}.` : ''}${persisted === false ? ' Unable to save the selection.' : ''}`;
    } catch (error) { diagnose('txt_import_failed', { error: String(error) }, 'error'); message.textContent = error.message; }
    finally { input.value = ''; importing = false; update(); }
  };
  cancel.onclick = async () => {
    cancelled = true; cancel.disabled = true;
    message.textContent = 'Cancelling…';
    if (activeId) {
      try { await invoke('cancel_download', { requestId: activeId }); }
      catch { message.textContent = 'Cancellation requested. Waiting for the current transfer to finish…'; }
    }
  };
  start.onclick = async () => {
    if (busy || importing) return;
    if (!canStart()) { message.textContent = 'Wait for the search to finish before starting the download.'; return; }
    if (!isTauri()) { message.textContent = 'Launch the Tauri app to download.'; return; }
    const pending = getQueue().filter(track => track.downloadStatus !== 'done');
    if (!pending.length) { message.textContent = 'Add tracks to the selection or import a TXT file first.'; return; }
    busy = true; cancelled = false; cancel.disabled = false; update(); stopPreview();
    let completed = 0, failed = 0;
    try {
      if (!destination.value.trim()) destination.value = await invoke('download_directory');
      if (cancelled) { message.textContent = 'Queue stopped.'; return; }
      try { storage.setItem('sweatydl-next-destination', destination.value.trim()); } catch { /* optional preference */ }
      for (const [index, track] of pending.entries()) {
        if (cancelled) break;
        activeId = `download-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
        track.downloadStatus = 'running'; delete track.downloadError; save(); update();
        progress.hidden = false; progress.removeAttribute('value');
        message.textContent = `${index + 1}/${pending.length} · ${track.title} · Preparing…`;
        try {
          const result = await invoke('download_audio', { url: track.url, destination: destination.value.trim(), requestId: activeId });
          if (!Array.isArray(result.files) || !result.files.length) throw new Error('No files confirmed.');
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
      message.textContent = `${cancelled ? 'Queue stopped. ' : ''}Downloaded: ${completed}; errors: ${failed}.${failed ? ' Press Download to retry failed items.' : ''}`;
    } catch (error) { message.textContent = String(error); }
    finally { activeId = null; busy = false; progress.hidden = true; update(); }
  };
  if (isTauri()) listen('download-progress', ({ payload }) => {
    if (!busy || !activeId || payload.request_id !== activeId || cancelled) return;
    if (typeof payload.progress === 'number') progress.value = payload.progress;
    else progress.removeAttribute('value');
    const track = getQueue().find(item => item.downloadStatus === 'running');
    message.textContent = `${track?.title || 'Audio'} · ${payload.total ? `Track ${payload.index}/${payload.total} · ` : ''}${typeof payload.progress === 'number' ? `${Math.round(payload.progress * 100)}%` : 'Downloading…'}`;
  }).catch(() => { /* Final command response remains authoritative. */ });
  update();
  return { busy: () => busy || importing, updateControls: update };
}
