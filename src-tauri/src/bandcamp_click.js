// Record the result row behind a click. No IPC, cookies or verification actions.
// Rust validates this hint against the catalog before the local UI can use it.
document.addEventListener('click', event => {
  const root = document.documentElement;
  root.removeAttribute('data-sweatydl-selection');
  const anchor = event.target?.closest?.('a[href]');
  const row = anchor?.closest('.searchresult');
  const title = row?.querySelector('.heading a');
  if (anchor && title) {
    root.setAttribute('data-sweatydl-selection', JSON.stringify({
      link_url: anchor.href, result_url: title.href,
    }));
  }
}, true);
