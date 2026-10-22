(() => {
  const page = { url: location.href, state: 'loading' };
  try {
    const raw = document.querySelector('[data-tralbum]')?.getAttribute('data-tralbum');
    if (!raw) {
      page.state = document.title.trim().toLowerCase() === 'client challenge' ? 'challenge'
        : document.readyState === 'complete' ? 'unavailable' : 'loading';
      return JSON.stringify(page);
    }
    if (raw.length > 2 * 1024 * 1024) throw new Error();
    const data = JSON.parse(raw);
    const rows = Array.isArray(data.trackinfo) ? data.trackinfo : [];
    const album = location.pathname.startsWith('/album/');
    const current = data.current || {};
    const chosen = album ? rows : rows.filter(row => row.title_link
      ? new URL(row.title_link, location.href).pathname.replace(/\/$/, '') === location.pathname.replace(/\/$/, '')
      : current.type === 'track' && current.id != null && String(row.track_id ?? row.id) === String(current.id));
    // Never silently download only part of an album.
    if (!chosen.length || chosen.length > 500 || chosen.some(row => !row.file?.['mp3-128'] || !row.duration)) throw new Error();
    return JSON.stringify({ ...page, state: 'ready', tracks: chosen.map(row => ({
      url: location.href, track_url: new URL(row.title_link || location.href, location.href).href,
      stream_url: new URL(row.file['mp3-128'], location.href).href,
      duration: row.duration, title: String(row.title || 'audio').slice(0, 500),
    })) });
  } catch { return JSON.stringify({ ...page, state: 'unavailable' }); }
})()
