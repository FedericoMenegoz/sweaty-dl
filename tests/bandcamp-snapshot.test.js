import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const script = readFileSync(new URL('../src-tauri/src/bandcamp_snapshot.js', import.meta.url), 'utf8');
// A DOM stub for collector unit tests, not a rendered Bandcamp/live fixture.
function collect({ title = '', body = '', rows = [], recognized = false, throws = false, hint = null, readyState = 'complete', empty = false } = {}) {
  const document = {
    title,
    readyState,
    body: { innerText: body },
    documentElement: { getAttribute: () => hint && JSON.stringify(hint) },
    querySelector: selector => selector === '.no-results, .noresults'
      ? (empty ? {} : null) : (recognized || rows.length || empty ? {} : null),
    querySelectorAll: () => {
      if (throws) throw new Error('DOM unavailable');
      return rows.map(row => ({
        querySelector: selector => selector === '.heading a'
          ? { textContent: row.title, href: row.url }
          : { textContent: row.artist },
      }));
    },
  };
  return JSON.parse(vm.runInNewContext(script, { document, URL, location: { href: 'https://bandcamp.com/search?q=El+Sonido&item_type=t' } }));
}

test('real catalog rows take precedence over leftover challenge title and text', () => {
  const page = collect({ title: 'Client Challenge', body: 'JavaScript is disabled in your browser',
    rows: [{ title: ' El Sonido ', artist: 'from Repeat\n   by Truncate', url: 'https://truncate.bandcamp.com/track/el-sonido' }] });
  assert.equal(page.challenge, false);
  assert.equal(page.recognized, true);
  assert.equal(page.results[0].title, 'El Sonido');
  assert.equal(page.results[0].artist, 'Truncate');
});

test('a challenge without catalog rows remains a challenge', () => {
  assert.equal(collect({ title: 'Client Challenge' }).challenge, true);
});

test('recognized empty catalog differs from unknown markup', () => {
  assert.equal(collect({ recognized: true }).recognized, true);
  assert.deepEqual(collect({ recognized: true }).results, []);
  assert.equal(collect().recognized, false);
});

test('collector exceptions return an explicit error', () => {
  assert.equal(collect({ throws: true }).error, 'dom_read_failed');
});

test('collector limits the number of rows before serializing them', () => {
  const row = { title: 'Song', artist: 'by Artist', url: 'https://artist.bandcamp.com/track/song' };
  assert.equal(collect({ rows: Array(101).fill(row) }).error, 'too_many_results');
});

test('mixed catalog rows skip artist pages and retain track catalog references', () => {
  const page = collect({ rows: [
    { title: 'Artist', artist: '', url: 'https://artist.bandcamp.com/' },
    { title: 'Song', artist: 'by Artist', url: 'https://artist.bandcamp.com/track/song' },
  ] });
  assert.equal(page.results.length, 1);
  assert.equal(page.results[0].title, 'Song');
});

test('collector captures the clicked row separately from a cover album link', () => {
  const hint = { link_url: 'https://artist.bandcamp.com/album/release', result_url: 'https://artist.bandcamp.com/track/song' };
  const page = collect({ hint });
  assert.deepEqual(page.selection_hint, hint);
});

test('click observer records the title of the clicked result row without IPC', () => {
  const clickScript = readFileSync(new URL('../src-tauri/src/bandcamp_click.js', import.meta.url), 'utf8');
  const attributes = new Map();
  let click;
  const document = {
    documentElement: {
      removeAttribute: key => attributes.delete(key),
      setAttribute: (key, value) => attributes.set(key, value),
    },
    addEventListener: (name, callback, capture) => {
      assert.equal(name, 'click');
      assert.equal(capture, true);
      click = callback;
    },
  };
  vm.runInNewContext(clickScript, { document });
  const title = { href: 'https://artist.bandcamp.com/track/song' };
  const cover = { href: 'https://artist.bandcamp.com/album/release', closest: () => ({ querySelector: () => title }) };
  click({ target: { closest: () => cover } });
  assert.deepEqual(JSON.parse(attributes.get('data-sweatydl-selection')), {
    link_url: cover.href, result_url: title.href,
  });
  click({ target: {} });
  assert.equal(attributes.has('data-sweatydl-selection'), false);
});

test('loaded explicit empty results differ from a loading container and stale challenge title', () => {
  const loading = collect({ recognized: true, readyState: 'loading' });
  assert.equal(loading.ready_state, 'loading');
  assert.equal(loading.empty_confirmed, false);
  const empty = collect({ title: 'Client Challenge', empty: true });
  assert.equal(empty.ready_state, 'complete');
  assert.equal(empty.empty_confirmed, true);
  assert.equal(empty.challenge, false);
});
