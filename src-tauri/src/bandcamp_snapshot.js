(() => {
  try {
  const snapshot = {
    url: location.href,
    challenge: false,
    recognized: !!document.querySelector('.searchresults, .result-items, .searchresult, .no-results, .noresults'),
    results: [],
    selection_hint: null,
    ready_state: document.readyState,
    empty_confirmed: !!document.querySelector('.no-results, .noresults'),
  };
  const items = document.querySelectorAll('.searchresult');
  if (items.length > 100) return JSON.stringify({ error: 'too_many_results' });
  for (const item of items) {
    const anchor = item.querySelector('.heading a');
    if (!anchor) continue;
    const url = new URL(anchor.href, location.href);
    if (url.protocol !== 'https:'
      || !(url.hostname === 'bandcamp.com' || url.hostname.endsWith('.bandcamp.com'))
      || !/^\/(track|album)\/.+/.test(url.pathname)) continue;
    const artist = (item.querySelector('.subhead')?.textContent || '').trim();
    const row = {
      title: anchor.textContent.trim(),
      artist: artist.split(' by ').at(-1).replace(/^by\s+/, '').trim(),
      url: anchor.href,
    };
    if (row.title.length > 1000 || row.artist.length > 1000 || row.url.length > 8192) {
      return JSON.stringify({ error: 'oversize_result' });
    }
    snapshot.results.push(row);
  }
  const selection = document.documentElement?.getAttribute('data-sweatydl-selection');
  if (selection && selection.length <= 20000) {
    try { snapshot.selection_hint = JSON.parse(selection); } catch { /* Ignore malformed page hints. */ }
  }
  // A completed verification can leave challenge text/title behind. Actual
  // catalog rows take precedence; no click or verification is automated here.
  snapshot.challenge = snapshot.results.length === 0 && !snapshot.empty_confirmed && (
    document.title.trim().toLowerCase() === 'client challenge'
    || (document.body?.innerText || '').includes('JavaScript is disabled in your browser')
  );
  return JSON.stringify(snapshot);
  } catch {
    return JSON.stringify({ error: 'dom_read_failed' });
  }
})()
