(() => {
  const page = { url: location.href, state: 'loading' };
  try {
    const element = document.querySelector('[data-tralbum]');
    const raw = element?.getAttribute('data-tralbum');
    if (!raw) {
      if (document.title.trim().toLowerCase() === 'client challenge') page.state = 'challenge';
      else if (document.readyState === 'complete') page.state = 'unavailable';
      return JSON.stringify(page);
    }
    if (raw.length > 2 * 1024 * 1024) return JSON.stringify({ ...page, state: 'unavailable' });
    const data = JSON.parse(raw);
    const rows = Array.isArray(data.trackinfo) ? data.trackinfo : [];
    const current = data.current || {};
    const path = location.pathname.replace(/\/$/, '');
    const row = rows.find(item => {
      if (item.title_link) {
        const link = new URL(item.title_link, location.href);
        return link.origin === location.origin && link.pathname.replace(/\/$/, '') === path;
      }
      return current.type === 'track' && current.id != null
        && String(item.track_id ?? item.id) === String(current.id);
    });
    if (!row || !row.file?.['mp3-128']) return JSON.stringify({ ...page, state: 'unavailable' });
    return JSON.stringify({
      ...page, state: 'ready', track_url: new URL(row.title_link || location.href, location.href).href,
      stream_url: new URL(row.file['mp3-128'], location.href).href,
      duration: row.duration,
    });
  } catch { return JSON.stringify({ ...page, state: 'unavailable' }); }
})()
