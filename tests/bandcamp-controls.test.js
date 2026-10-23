import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/bandcamp-controls.js', import.meta.url), 'utf8')
  .replace(/^\s*import\s+[^\r\n]*;?\s*$/gm, '');
const track = { title: 'Song', artist: 'Artist', url: 'https://artist.bandcamp.com/track/song' };
const tick = () => new Promise(resolve => setImmediate(resolve));

// UI unit harness: minimal elements and IPC stubs, not a native WebView test.
async function panel(load, transfer = async () => { throw new Error('Unexpected transfer'); }, automatic = false) {
  const elements = new Map();
  const element = () => ({
    textContent: '', disabled: false, dataset: {}, children: [], classList: { add() {} },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    scrollIntoView() {},
  });
  const events = new Map();
  const document = {
    body: element(),
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, element());
      return elements.get(selector);
    },
    createElement: element,F
  };
  const context = vm.createContext({
    document,
    invoke: async command => {
      if (command === 'bandcamp_context') return { query: 'Song', session: 1, automatic };
      if (command === 'import_bandcamp') return load();
      if (command === 'use_bandcamp_results') return transfer();
      throw new Error(`Unexpected command ${command}`);
    },
    getCurrentWebview: () => ({ listen: async (name, handler) => { events.set(name, handler); } }),
  });
  vm.runInContext(source, context);
  await tick();
  return {
    status: () => elements.get('#verification-status').textContent,
    confirmDisabled: () => elements.get('#confirm-selection').disabled,
    pick: url => events.get('bandcamp-picked')({ payload: url }),
    autoStatus: message => events.get('bandcamp-auto-status')({ payload: message }),
    elements,
  };
}

test('a browser click does not replace an import error with an unrelated missing-link message', async () => {
  let fail = false;
  const view = await panel(async () => {
    if (fail) throw new Error('La pagina è cambiata durante la lettura.');
    return { session: 1, results: [track] };
  });
  fail = true;
  view.pick('https://artist.bandcamp.com/album/release');
  await tick();
  assert.match(view.status(), /La pagina è cambiata durante la lettura/);
  assert.equal(view.confirmDisabled(), true);
});

test('a direct catalog click selects the matching validated row', async () => {
  let reads = 0;
  const view = await panel(async () => {
    reads++;
    return { session: 1, results: [track] };
  });
  view.pick(track.url);
  await tick();
  assert.equal(view.confirmDisabled(), false);
  assert.match(view.status(), /Risultato selezionato/);
  assert.equal(reads, 1, 'a known row should not trigger another read during cancelled navigation');
});

test('a cover click selects the row title returned by the validated hint', async () => {
  const album = 'https://artist.bandcamp.com/album/release';
  const view = await panel(async () => ({ session: 1, results: [track],
    selection_hint: { link_url: album, result_url: track.url } }));
  view.pick(album);
  await tick();
  assert.equal(view.confirmDisabled(), false);
  assert.match(view.status(), /Risultato selezionato/);
});

test('an unknown browser link never enables confirmation', async () => {
  const view = await panel(async () => ({ session: 1, results: [track] }));
  view.pick('https://artist.bandcamp.com/track/other-remix');
  await tick();
  assert.equal(view.confirmDisabled(), true);
});

test('transfer-all works even with an empty sidebar and no individual selection', async () => {
  let transfers = 0;
  const view = await panel(async () => ({ session: 1, results: [] }), async () => { transfers++; return 18; });
  assert.equal(view.elements.get('#use-all-results').disabled, false);
  await view.elements.get('#use-all-results').onclick();
  assert.equal(transfers, 1);
  assert.match(view.status(), /18 risultati trasferiti/);
});

test('transfer-all preserves the real read error', async () => {
  const view = await panel(async () => ({ session: 1, results: [track] }),
    async () => { throw new Error('La verifica Bandcamp è ancora presente.'); });
  await view.elements.get('#use-all-results').onclick();
  assert.match(view.status(), /La verifica Bandcamp è ancora presente/);
  assert.equal(view.confirmDisabled(), true);
});

test('automatic verification panel waits for the core without starting a second reader', async () => {
  let reads = 0;
  const view = await panel(async () => { reads++; return { results: [] }; }, undefined, true);
  assert.equal(reads, 0);
  assert.match(view.status(), /Attendo la verifica/);
  view.autoStatus('Bandcamp richiede la verifica manuale.');
  assert.match(view.status(), /verifica manuale/);
});
