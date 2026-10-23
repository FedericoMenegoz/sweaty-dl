import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { initDiagnostics, recordDiagnostic } from '../src/diagnostics.js';
import { initDownloads } from '../src/download-ui.js';
import { canPreview, createPreviewPlayer } from '../src/preview-player.js';

function mainHarness(invoke = async () => { throw new Error('Unexpected command'); }) {
  const source = readFileSync(new URL('../src/main-app.js', import.meta.url), 'utf8')
    .replace(/^import[^\r\n]*;[ \t]*\r?$/gm, '');
  const elements = new Map();
  const events = new Map();
  const element = () => ({ textContent: '', children: [], dataset: {}, isConnected: true,
    setAttribute() {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
  });
  const saved = [];
  vm.runInNewContext(source, {
    URL,
    canPreview, createPreviewPlayer, initDownloads, initDiagnostics, recordDiagnostic,
    invoke,
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
    },
    localStorage: { getItem: () => '[]', setItem: (key, value) => saved.push(value) },
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
  assert.match(elements.get('#status').textContent, /2 risultati importati/);
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
    if (command === 'close_bandcamp') { cancel('Ricerca Bandcamp annullata.'); return; }
    throw new Error(`Unexpected command ${command}`);
  });
  const pending = view.submit({ title: 'Song', artist: '', mode: 'Bandcamp' });
  await view.elements.get('#close-bandcamp').onclick();
  await pending;
  assert.equal(view.elements.get('#results').children.length, 0);
  assert.equal(view.elements.get('#search-button').disabled, false);
  assert.match(view.elements.get('#status').textContent, /annullata/);
});

test('an explicitly empty result completes the automatic search', async () => {
  const view = mainHarness(async () => ({ results: [], errors: {} }));
  await view.submit({ title: 'Song', artist: '', mode: 'Bandcamp' });
  assert.match(view.elements.get('#status').textContent, /^0 risultati/);
  assert.equal(view.elements.get('#bandcamp-actions').hidden, true);
  assert.equal(view.elements.get('#search-button').disabled, false);
});
