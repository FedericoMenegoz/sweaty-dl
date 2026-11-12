import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { initDiagnostics, recordDiagnostic } from '../src/diagnostics.js';
import { initDownloads } from '../src/download-ui.js';
import { canPreview, createPreviewPlayer } from '../src/preview-player.js';
import { classifyMediaInput, bandcampReference, resolvedMediaItems, mergeMediaQueue } from '../src/media-input.js';

function mainHarness(invoke = async () => { throw new Error('Unexpected command'); }, initialQueue = [], failPersistence = false, onUpdates = () => {}) {
  const source = readFileSync(new URL('../src/main-app.js', import.meta.url), 'utf8')
    .replace(/^import[^\r\n]*;[ \t]*\r?$/gm, '');
  const elements = new Map();
  const events = new Map();
  const element = () => ({ textContent: '', value: '', children: [], dataset: {}, isConnected: true,
    setAttribute() {},
    removeAttribute(name) { delete this[name]; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
  });
  const saved = [];
  vm.runInNewContext(source, {
    URL,
    canPreview, createPreviewPlayer, initDownloads, initDiagnostics, recordDiagnostic,
    classifyMediaInput, bandcampReference, resolvedMediaItems, mergeMediaQueue,
    invoke,
    initUpdates: options => { onUpdates(options); return { refresh() {} }; },
    getVersion: async () => '0.1.1',
    check: async () => null,
    FormData: class {
      constructor(target) { this.input = target.input; }
      [Symbol.iterator]() { return Object.entries(this.input)[Symbol.iterator](); }
    },
    document: {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, element());
        return elements.get(selector);
      },
      createElement: element,
      querySelectorAll: () => ['#queue', '#results'].flatMap(selector =>
        (elements.get(selector)?.children || []).flatMap(row => row.children?.slice(1) || [])),
    },
    localStorage: { getItem: key => key === 'sweatydl-next-queue' ? JSON.stringify(initialQueue) : null, setItem: (key, value) => {
      if (failPersistence) throw new Error('Storage full');
      saved.push(value);
    } },
    isTauri: () => true,
    getCurrentWebview: () => ({ listen: async (name, handler) => { events.set(name, handler); } }),
  });
  return { elements, events, saved,
    submit: input => elements.get('#search').onsubmit({ preventDefault() {}, target: { input } }),
  };
}

const results = [
    { title: 'Song', source: 'Bandcamp', url: 'https://artist.bandcamp.com/track/song' },
    { title: 'Song Live', source: 'Bandcamp', url: 'https://artist.bandcamp.com/track/song-live' },
];
const report = { results, errors: {} };

test('all imported catalog results are shown for choice without automatically saving them', () => {
  const { events, elements, saved } = mainHarness();
  events.get('bandcamp-results-imported')({ payload: { results } });
  assert.equal(elements.get('#results').children.length, 2);
  assert.equal(elements.get('#queue').children.length, 0);
  assert.deepEqual(saved, ['[]']);
  assert.match(elements.get('#status').textContent, /2 results imported/);
});

test('Bandcamp search opens the browser directly and waits for its automatic result', async () => {
  const calls = [];
  let finish;
  const view = mainHarness((command, args) => {
    calls.push({ command, args });
    return new Promise(resolve => { finish = resolve; });
  });
  const pending = view.submit({ title: ' Song ', artist: ' Artist ', mode: 'Bandcamp' });
  assert.equal(calls[0].command, 'search_bandcamp_browser');
  assert.equal(calls[0].args.query, 'Artist Song');
  assert.equal(view.elements.get('#search-button').disabled, true);
  assert.equal(view.elements.get('#bandcamp-actions').hidden, false);
  assert.equal(view.elements.get('#results').children.length, 0);
  finish(report);
  await pending;
  assert.equal(calls.length, 1);
  assert.equal(view.elements.get('#results').children.length, 2);
  assert.equal(view.elements.get('#bandcamp-actions').hidden, true);
  assert.equal(view.elements.get('#search-button').disabled, false);
  assert.deepEqual(view.saved, ['[]']);
});

test('automatic source opens Bandcamp only when the search reports a challenge', async () => {
  const calls = [];
  const view = mainHarness(async command => {
    calls.push(command);
    if (command === 'search') return { results: [], errors: {}, browser_query: 'Song' };
    return report;
  });
  await view.submit({ title: 'Song', artist: '', mode: 'auto' });
  assert.deepEqual(calls, ['search', 'search_bandcamp_browser']);
  assert.equal(view.elements.get('#results').children.length, 2);
});

test('explicit alternatives search without opening Bandcamp', async () => {
  const calls = [];
  const view = mainHarness(async command => { calls.push(command); return report; });
  await view.submit({ title: 'Song', artist: '', mode: 'SoundCloud' });
  assert.deepEqual(calls, ['search']);
});

test('cancelling the browser does not import results or leave search disabled', async () => {
  let cancel;
  const view = mainHarness(async command => {
    if (command === 'search_bandcamp_browser') return new Promise((_, reject) => { cancel = reject; });
    if (command === 'close_bandcamp') { cancel('Bandcamp search cancelled.'); return; }
    throw new Error(`Unexpected command ${command}`);
  });
  const pending = view.submit({ title: 'Song', artist: '', mode: 'Bandcamp' });
  await view.elements.get('#close-bandcamp').onclick();
  await pending;
  assert.equal(view.elements.get('#results').children.length, 0);
  assert.equal(view.elements.get('#search-button').disabled, false);
  assert.match(view.elements.get('#status').textContent, /cancelled/);
});

test('an explicitly empty result completes the automatic search', async () => {
  const view = mainHarness(async () => ({ results: [], errors: {} }));
  await view.submit({ title: 'Song', artist: '', mode: 'Bandcamp' });
  assert.match(view.elements.get('#status').textContent, /^0 results/);
  assert.equal(view.elements.get('#bandcamp-actions').hidden, true);
  assert.equal(view.elements.get('#search-button').disabled, false);
});

const youtubeTrack = (id = 'abcdefghijk') => ({ title: `Song ${id}`, artist: 'Artist', source: 'YouTube', kind: 'track', url: `https://www.youtube.com/watch?v=${id}` });
const playlistReport = () => ({ source: 'YouTube', kind: 'playlist', title: 'Playlist', url: 'https://www.youtube.com/playlist?list=PL_collection', results: [youtubeTrack(), youtubeTrack('lmnopqrstuv')], unavailable_count: 1, warnings: ['One item is unavailable.'], complete: true });
const latestQueue = view => JSON.parse(view.saved.at(-1));

test('saving the queue for an app update keeps search, downloads and queue edits locked', async () => {
  let updates;
  const calls = [];
  const view = mainHarness(async command => { calls.push(command); }, [youtubeTrack()], false,
    options => { updates = options; });
  updates.setInstalling(true);
  updates.beforeInstall();
  assert.equal(view.elements.get('#search-button').disabled, true);
  assert.equal(view.elements.get('#download-start').disabled, true);
  assert.equal(view.elements.get('#url-file').disabled, true);
  assert.equal(view.elements.get('#queue').children[0].children.at(-1).disabled, true);
  await view.submit({ title: 'Song', artist: '', mode: 'SoundCloud' });
  assert.deepEqual(calls, []);
  updates.setInstalling(false);
  assert.equal(view.elements.get('#search-button').disabled, false);
  assert.equal(view.elements.get('#download-start').disabled, false);
});

test('playlist URL overrides text source/artist, imports ordered references and waits for Download', async () => {
  const calls = [];
  const view = mainHarness(async (command, args) => {
    calls.push({ command, args });
    return playlistReport();
  });
  await view.submit({ title: 'https://youtu.be/abcdefghijk?list=PL_collection&si=tracking', artist: 'Ignored', mode: 'Bandcamp' });
  assert.deepEqual(calls.map(call => call.command), ['resolve_media_url']);
  assert.equal(calls[0].args.url, playlistReport().url);
  assert.deepEqual(latestQueue(view).map(track => track.url), playlistReport().results.map(track => track.url));
  assert.equal(view.elements.get('#queue').children.length, 2);
  assert.equal(view.elements.get('#results').children.length, 2);
  assert.match(view.elements.get('#status').textContent, /2 tracks added.*1 unavailable/);
  assert.match(view.elements.get('#status').textContent, /entire playlist.*remove the list parameter/);
  assert.match(view.elements.get('#status').textContent, /Press «Download»/);
  assert.equal(view.elements.get('#search-button').disabled, false);
});

test('Bandcamp album link queues one album without invoking search or replacing album downloading', async () => {
  const view = mainHarness();
  await view.submit({ title: 'https://artist.bandcamp.com/album/a-release?from=search', artist: '', mode: 'YouTube' });
  assert.equal(latestQueue(view).length, 1);
  assert.equal(latestQueue(view)[0].kind, 'album');
  assert.equal(latestQueue(view)[0].url, 'https://artist.bandcamp.com/album/a-release');
  assert.match(view.elements.get('#status').textContent, /1 album added/);
});

test('repeated playlist import preserves completed entries and skips canonical duplicates', async () => {
  const completed = { ...youtubeTrack(), url: 'https://youtu.be/abcdefghijk?si=tracking', downloadStatus: 'done', downloadFiles: ['saved.opus'] };
  const view = mainHarness(async () => playlistReport(), [completed]);
  const input = { title: playlistReport().url, artist: '', mode: 'auto' };
  await view.submit(input);
  await view.submit(input);
  assert.equal(latestQueue(view).length, 2);
  assert.deepEqual(latestQueue(view)[0], completed);
  assert.equal(latestQueue(view)[1].downloadStatus, 'pending');
  assert.match(view.elements.get('#status').textContent, /0 tracks added.*2 duplicates/);
});

test('Save on imported results cannot re-add a completed video under a different URL', async () => {
  const completed = { ...youtubeTrack(), url: 'https://youtu.be/abcdefghijk?si=tracking', downloadStatus: 'done', downloadFiles: ['saved.opus'] };
  const view = mainHarness(async () => ({ ...playlistReport(), results: [youtubeTrack()] }), [completed]);
  await view.submit({ title: playlistReport().url, artist: '', mode: 'auto' });
  const save = view.elements.get('#results').children[0].children.find(child => child.textContent === 'Save');
  save.onclick();
  assert.equal(latestQueue(view).length, 1);
  assert.deepEqual(latestQueue(view)[0], completed);
});

test('Save and Bandcamp confirmation enforce queue capacity without partial additions', () => {
  const existing = Array.from({ length: 2000 }, (_, index) => ({ title: `Existing ${index}`, url: `https://example.com/${index}` }));
  const view = mainHarness(undefined, existing);
  view.events.get('bandcamp-results-imported')({ payload: { results } });
  const save = view.elements.get('#results').children[0].children.find(child => child.textContent === 'Save');
  save.onclick();
  assert.equal(latestQueue(view).length, 2000);
  assert.match(view.elements.get('#status').textContent, /2,000/);
  view.events.get('bandcamp-confirmed')({ payload: { results } });
  assert.equal(latestQueue(view).length, 2000);
  assert.match(view.elements.get('#status').textContent, /2,000/);
});

test('invalid resolver payload and failed resolutions leave the existing queue and results unchanged', async () => {
  for (const resolve of [
    async () => ({ ...playlistReport(), complete: false }),
    async () => ({ ...playlistReport(), results: [youtubeTrack(), { ...youtubeTrack('lmnopqrstuv'), url: playlistReport().url }] }),
    async () => { throw new Error('Unable to reach playlist'); },
  ]) {
    const existing = [youtubeTrack()];
    const view = mainHarness(resolve, existing);
    const priorResult = { title: 'Previous result' };
    view.elements.set('#results', { children: [priorResult], replaceChildren(...children) { this.children = children; } });
    await view.submit({ title: playlistReport().url, artist: '', mode: 'auto' });
    assert.deepEqual(latestQueue(view), existing);
    assert.equal(view.elements.get('#results').children[0], priorResult);
    assert.equal(view.elements.get('#search-button').disabled, false);
  }
});

test('invalid URL-like inputs never invoke text search and search remains usable afterward', async () => {
  const calls = [];
  const view = mainHarness(async command => { calls.push(command); return report; });
  await view.submit({ title: 'https://youtube.com/playlist?list=', artist: '', mode: 'Bandcamp' });
  assert.deepEqual(calls, []);
  assert.equal(view.elements.get('#search-button').disabled, false);
  await view.submit({ title: 'Song', artist: '', mode: 'SoundCloud' });
  assert.deepEqual(calls, ['search']);
});

test('pending resolution locks TXT imports, queue edits, another search and downloads', async () => {
  let finish;
  const calls = [];
  const view = mainHarness((command, args) => {
    calls.push(command);
    if (command === 'resolve_media_url') return new Promise(resolve => { finish = resolve; });
    if (command === 'download_directory') return '/tmp/audio';
    if (command === 'download_audio') return { files: [`/tmp/audio/${args.url.slice(-11)}.opus`] };
    throw new Error(command);
  }, [youtubeTrack('zyxwvutsrqp')]);
  const pending = view.submit({ title: playlistReport().url, artist: '', mode: 'auto' });
  for (const selector of ['#download-start', '#url-file', '#choose-destination', '#destination']) {
    assert.equal(view.elements.get(selector).disabled, true, selector);
  }
  const remove = view.elements.get('#queue').children[0].children.at(-1);
  assert.equal(remove.disabled, true);
  remove.onclick();
  const file = view.elements.get('#url-file');
  file.files = [{ size: 100, text: async () => 'https://vimeo.com/123' }];
  await file.onchange();
  assert.equal(latestQueue(view).length, 1);
  await view.submit({ title: 'Other search', artist: '', mode: 'SoundCloud' });
  await view.elements.get('#download-start').onclick();
  assert.deepEqual(calls, ['resolve_media_url']);
  finish(playlistReport());
  await pending;
  for (const selector of ['#download-start', '#url-file', '#choose-destination', '#destination']) {
    assert.equal(view.elements.get(selector).disabled, false, selector);
  }
  await view.elements.get('#download-start').onclick();
  assert.deepEqual(calls, ['resolve_media_url', 'download_directory', 'download_audio', 'download_audio', 'download_audio']);
  assert.ok(latestQueue(view).every(track => track.downloadStatus === 'done'));
});

test('a pending TXT read blocks playlist submission and downloads until the queue is saved', async () => {
  const calls = [];
  let finish;
  const view = mainHarness(async command => { calls.push(command); }, [youtubeTrack()]);
  const file = view.elements.get('#url-file');
  file.files = [{ size: 100, text: () => new Promise(resolve => { finish = resolve; }) }];
  const importing = file.onchange();
  view.events.get('bandcamp-confirmed')({ payload: { results } });
  assert.equal(latestQueue(view).length, 1);
  await view.submit({ title: playlistReport().url, artist: '', mode: 'auto' });
  await view.elements.get('#download-start').onclick();
  assert.deepEqual(calls, []);
  assert.equal(view.elements.get('#queue').children[0].children.at(-1).disabled, true);
  finish('https://vimeo.com/123');
  await importing;
  assert.equal(latestQueue(view).length, 2);
  assert.equal(view.elements.get('#download-start').disabled, false);
  assert.equal(view.elements.get('#queue').children[0].children.at(-1).disabled, false);
});

test('failed playlist resolution and TXT reads restore queue controls', async () => {
  const view = mainHarness(async () => { throw new Error('Network unavailable'); }, [youtubeTrack()]);
  await view.submit({ title: playlistReport().url, artist: '', mode: 'auto' });
  const file = view.elements.get('#url-file');
  file.files = [{ size: 100, text: async () => { throw new Error('Failed to read'); } }];
  await file.onchange();
  assert.equal(latestQueue(view).length, 1);
  for (const selector of ['#search-button', '#download-start', '#url-file', '#choose-destination', '#destination']) {
    assert.equal(view.elements.get(selector).disabled, false, selector);
  }
  assert.equal(view.elements.get('#queue').children[0].children.at(-1).disabled, false);
});

test('import persistence failure is visible and leaves the new references usable in this session', async () => {
  const view = mainHarness(async () => playlistReport(), [], true);
  await view.submit({ title: playlistReport().url, artist: '', mode: 'auto' });
  assert.equal(view.elements.get('#queue').children.length, 2);
  assert.match(view.elements.get('#status').textContent, /only for this session.*could not be saved/);
  assert.equal(view.elements.get('#search-button').disabled, false);
});

test('imported child failures retry individually, skip completed tracks and remain cancellable', async () => {
  const calls = [];
  let attempt = 0;
  let rejectActive;
  const view = mainHarness(async (command, args) => {
    if (command === 'resolve_media_url') return playlistReport();
    if (command === 'download_directory') return '/tmp/audio';
    if (command === 'download_audio') {
      calls.push(args.url);
      attempt++;
      if (attempt === 1) throw new Error('Errore rete');
      if (attempt === 3) return new Promise((_, reject) => { rejectActive = reject; });
      return { files: ['/tmp/audio/song.opus'] };
    }
    if (command === 'cancel_download') { rejectActive(new Error('Download cancelled.')); return; }
    throw new Error(command);
  });
  await view.submit({ title: playlistReport().url, artist: '', mode: 'auto' });
  await view.elements.get('#download-start').onclick();
  assert.deepEqual(latestQueue(view).map(track => track.downloadStatus), ['error', 'done']);
  const retry = view.elements.get('#download-start').onclick();
  await view.elements.get('#download-cancel').onclick();
  await retry;
  assert.deepEqual(latestQueue(view).map(track => track.downloadStatus), ['pending', 'done']);
  await view.elements.get('#download-start').onclick();
  assert.deepEqual(latestQueue(view).map(track => track.downloadStatus), ['done', 'done']);
  assert.deepEqual(calls, [youtubeTrack().url, youtubeTrack('lmnopqrstuv').url, youtubeTrack().url, youtubeTrack().url]);
});
