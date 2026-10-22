import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { catalogReference, parseUrlText, initDownloads } from '../src/download-ui.js';

test('TXT accepts mixed sources, BOM/CRLF and rejects unsafe lines without losing valid entries', () => {
  const report = parseUrlText('\uFEFFhttps://artist.bandcamp.com/track/song?from=search\r\n\r\nhttps://soundcloud.com/a/b\r\nhttps://youtu.be/abc\r\nhttps://vimeo.com/123\r\nfile:///etc/passwd\r\nhttps://127.0.0.1/audio\r\nnot a url');
  assert.deepEqual(report.added.map(t => t.source), ['Bandcamp', 'SoundCloud', 'YouTube', 'URL']);
  assert.deepEqual(report.invalid, [6, 7, 8]);
  assert.equal(report.added[0].url, 'https://artist.bandcamp.com/track/song');
});

test('TXT deduplicates within the file and against the saved queue, preserving versions', () => {
  const report = parseUrlText('https://artist.bandcamp.com/track/song?from=search\nhttps://artist.bandcamp.com/track/song/\nhttps://artist.bandcamp.com/track/song-live\nhttps://artist.bandcamp.com/track/song-live', [{ url: 'https://artist.bandcamp.com/track/song' }]);
  assert.equal(report.added.length, 1); assert.equal(report.duplicates, 3);
  assert.match(report.added[0].url, /song-live$/);
  assert.throws(() => parseUrlText('x'.repeat(1024 * 1024 + 1)), /1 MB/);
  assert.throws(() => catalogReference('https://user:password@example.com/audio'));
});

function harness(queue, invoke) {
  const nodes = new Map(); const handlers = new Map(); const records = new Map();
  const document = {
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, { value: '', removeAttribute(name) { delete this[name]; } });
      return nodes.get(selector);
    }, querySelectorAll: () => [],
  };
  let saves = 0;
  const control = initDownloads({ document, invoke, isTauri: () => true,
    getQueue: () => queue, save: () => { saves++; }, stopPreview: () => {},
    storage: { getItem: key => records.get(key), setItem: (key, value) => records.set(key, value) },
    listen: async (event, handler) => handlers.set(event, handler),
  });
  nodes.get('#destination').value = '/tmp/audio';
  return { nodes, handlers, records, control, saves: () => saves };
}
const song = name => ({ title: name, url: `https://artist.bandcamp.com/track/${name}` });

test('downloads sequentially, keeps errors retryable and skips confirmed completed entries', async () => {
  const queue = [song('one'), song('two'), { ...song('old'), downloadStatus: 'done' }];
  const calls = [];
  const view = harness(queue, async (command, args) => {
    calls.push(args.url);
    if (args.url.endsWith('/one')) throw new Error('Errore rete');
    return { files: ['/tmp/audio/two.mp3'], kind: 'saved_stream_audio' };
  });
  await view.nodes.get('#download-start').onclick();
  assert.equal(calls.length, 2); assert.equal(queue[0].downloadStatus, 'error');
  assert.equal(queue[1].downloadStatus, 'done'); assert.equal(view.control.busy(), false);
  assert.match(view.nodes.get('#download-status').textContent, /1 voci scaricate; 1 errori/);
  assert.equal(JSON.parse(view.records.get('sweatydl-next-download-history')).length, 1);
});

test('cancel stops the active request, retains it for retry and never starts the next entry', async () => {
  const queue = [song('one'), song('two')];
  let reject; const calls = [];
  const view = harness(queue, async (command, args) => {
    calls.push(command);
    if (command === 'download_audio') return new Promise((_, fail) => { reject = fail; });
    if (command === 'cancel_download') reject(new Error('Download annullato.'));
  });
  const running = view.nodes.get('#download-start').onclick();
  assert.equal(view.control.busy(), true);
  view.handlers.get('download-progress')({ payload: { request_id: 'stale', progress: .8 } });
  assert.equal(view.nodes.get('#download-progress').value, undefined);
  await view.nodes.get('#download-cancel').onclick(); await running;
  assert.deepEqual(calls, ['download_audio', 'cancel_download']);
  assert.equal(queue[0].downloadStatus, 'pending'); assert.equal(queue[1].downloadStatus, undefined);
  assert.equal(view.nodes.get('#download-start').disabled, false);
});

test('file selection imports references and allows importing the same file again', async () => {
  const queue = []; const view = harness(queue, async () => {});
  const input = view.nodes.get('#url-file');
  input.files = [{ size: 100, text: async () => 'https://vimeo.com/123\nwrong\nhttps://vimeo.com/123' }];
  await input.onchange();
  assert.equal(queue.length, 1); assert.equal(input.value, '');
  assert.match(view.nodes.get('#download-status').textContent, /1 URL aggiunti; 1 duplicati. Righe non valide: 2/);
});

function snapshot(data, path = '/album/collection') {
  const source = readFileSync(new URL('../src-tauri/src/download_snapshot.js', import.meta.url), 'utf8');
  return JSON.parse(vm.runInNewContext(source, { URL,
    location: { href: `https://artist.bandcamp.com${path}`, pathname: path },
    document: { title: 'Album', readyState: 'complete', querySelector: () => ({ getAttribute: () => JSON.stringify(data) }) },
  }));
}
const row = title => ({ title, title_link: `/track/${title}`, duration: 240, file: { 'mp3-128': 'https://t4.bcbits.com/stream/xxx/mp3-128/xxx' } });

test('album collector retains exact ordered tracks and refuses a partial album', () => {
  assert.equal(snapshot({ trackinfo: [row('song'), row('song-live')] }).tracks.length, 2);
  assert.equal(snapshot({ trackinfo: [row('song'), { title: 'private' }] }).state, 'unavailable');
  const track = snapshot({ trackinfo: [row('song'), row('song-live')] }, '/track/song-live');
  assert.equal(track.tracks.length, 1); assert.match(track.tracks[0].track_url, /song-live$/);
});
