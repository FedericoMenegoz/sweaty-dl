import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const script = readFileSync(new URL('../src-tauri/src/preview_snapshot.js', import.meta.url), 'utf8');
function read(data, title = '') {
  return JSON.parse(vm.runInNewContext(script, {
    URL, location: new URL('https://artist.bandcamp.com/track/song'),
    document: { title, readyState: 'complete', querySelector: () => data && { getAttribute: () => JSON.stringify(data) } },
  }));
}
const row = { id: 12, title_link: '/track/song', duration: 240,
  file: { 'mp3-128': '//t4.bcbits.com/stream/example/mp3-128/example' } };
test('collector chooses the exact track among other versions', () => {
  const page = read({ trackinfo: [{ ...row, title_link: '/track/song-live' }, row] });
  assert.equal(page.state, 'ready');
  assert.equal(page.track_url, 'https://artist.bandcamp.com/track/song');
  assert.equal(page.stream_url, 'https://t4.bcbits.com/stream/example/mp3-128/example');
});
test('collector does not substitute the first available version', () => {
  assert.equal(read({ trackinfo: [{ ...row, title_link: '/track/song-live' }] }).state, 'unavailable');
});
test('collector distinguishes a challenge and unavailable audio', () => {
  assert.equal(read(null, 'Client Challenge').state, 'challenge');
  assert.equal(read({ trackinfo: [{ ...row, file: null }] }).state, 'unavailable');
});
