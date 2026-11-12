import assert from 'node:assert/strict';
import test from 'node:test';
import { initUpdates, isNewerVersion } from '../src/update-ui.js';

const flush = () => new Promise(resolve => setImmediate(resolve));

function harness({ current = '1.2.9', latest = '1.2.10', checkError, installError, restartError, saveError } = {}) {
  const elements = new Map();
  const events = new Map();
  const calls = [];
  let busy = false, finishInstall;
  const candidate = latest && { version: latest, close: async () => { calls.push('close'); },
    downloadAndInstall: async callback => {
      calls.push('install');
      callback({ event: 'Started', data: { contentLength: 100 } });
      callback({ event: 'Progress', data: { chunkLength: 50 } });
      await new Promise(resolve => { finishInstall = resolve; });
      if (installError) throw new Error(installError);
      callback({ event: 'Finished' });
    },
  };
  const updates = initUpdates({
    document: { querySelector: selector => {
      if (!elements.has(selector)) elements.set(selector, { disabled: false, textContent: '' });
      return elements.get(selector);
    } },
    isTauri: () => true,
    getVersion: async () => current,
    check: async () => { calls.push('check'); if (checkError) throw new Error(checkError); return candidate; },
    restart: async () => { calls.push('restart'); if (restartError) throw new Error(restartError); },
    beforeInstall: async () => { calls.push('save'); if (saveError) throw new Error(saveError); },
    canInstall: () => !busy,
    setInstalling: value => { calls.push(value ? 'lock' : 'unlock'); },
    host: { setInterval: callback => { events.set('interval', callback); return 1; },
      clearInterval: () => { calls.push('clear'); },
      addEventListener: (name, callback) => events.set(name, callback),
    },
  });
  return { updates, elements, calls, events, candidate,
    button: elements.get('#update-app'), status: elements.get('#update-status'),
    setBusy: value => { busy = value; updates.refresh(); },
    finish: () => finishInstall(),
  };
}

test('compares numeric versions, ignoring same/older versions and rejecting invalid tags', () => {
  for (const [latest, current, expected] of [
    ['1.2.10', '1.2.9', true], ['2.0.0', '1.99.99', true], ['1.10.0', '1.9.99', true],
    ['v1.0.0', '1.0.0', false], ['1.2.9', '1.2.10', false], ['0.9.9', '1.0.0', false],
    ['1.0.9007199254740993', '1.0.9007199254740992', true],
  ]) assert.equal(isNewerVersion(latest, current), expected);
  for (const invalid of ['v01.0.0', '1.0', '1.0.1-beta.1', 'garbage']) {
    assert.throws(() => isNewerVersion(invalid, '1.0.0'));
  }
});

test('button starts disabled and only enables for a newer release; shows installed version', async () => {
  for (const latest of ['1.2.10', '1.2.9', '1.2.8', null]) {
    const view = harness({ latest });
    assert.equal(view.button.disabled, true);
    await flush();
    assert.equal(view.button.disabled, latest !== '1.2.10');
    assert.equal(view.elements.get('#app-version').textContent, 'SweatyDL 1.2.9');
    if (latest !== '1.2.10' && latest) assert.ok(view.calls.includes('close'));
  }
});

test('offline checks and invalid version metadata keep the update button disabled', async () => {
  for (const options of [{ checkError: 'offline' }, { latest: 'invalid' }]) {
    const view = harness(options);
    await flush();
    assert.equal(view.button.disabled, true);
    assert.match(view.status.textContent, /Could not check/);
    assert.ok(!view.calls.includes('install'));
  }
});

test('active work prevents installation, then saves queue, shows progress and restarts once', async () => {
  const view = harness();
  await flush();
  view.setBusy(true);
  assert.equal(view.button.disabled, true);
  await view.button.onclick();
  assert.ok(!view.calls.includes('install'));
  view.setBusy(false);
  const pending = view.button.onclick();
  await flush();
  assert.equal(view.button.disabled, true);
  assert.match(view.status.textContent, /50%/);
  assert.ok(view.calls.indexOf('save') < view.calls.indexOf('install'));
  await view.button.onclick();
  view.finish();
  await pending;
  assert.equal(view.calls.filter(call => call === 'install').length, 1);
  assert.equal(view.calls.filter(call => call === 'restart').length, 1);
  assert.equal(view.calls.at(-1), 'unlock');
});

test('failed update can retry and never restarts after an installation failure', async () => {
  const view = harness({ installError: 'invalid signature' });
  await flush();
  const pending = view.button.onclick();
  await flush(); view.finish(); await pending;
  assert.equal(view.button.disabled, false);
  assert.match(view.status.textContent, /Update failed/);
  assert.ok(!view.calls.includes('restart'));
  const retry = view.button.onclick();
  await flush(); view.finish(); await retry;
  assert.equal(view.calls.filter(call => call === 'install').length, 2);
});

test('queue persistence failure prevents installation and restart', async () => {
  const view = harness({ saveError: 'storage full' });
  await flush();
  await view.button.onclick();
  assert.equal(view.button.disabled, false);
  assert.ok(!view.calls.includes('install'));
  assert.ok(!view.calls.includes('restart'));
  assert.equal(view.calls.at(-1), 'unlock');
});

test('failed restart retries without downloading and installing the update again', async () => {
  const view = harness({ restartError: 'restart unavailable' });
  await flush();
  const pending = view.button.onclick();
  await flush(); view.finish(); await pending;
  assert.match(view.status.textContent, /Restart the app/);
  assert.equal(view.button.textContent, 'Restart app');
  await view.button.onclick();
  assert.equal(view.calls.filter(call => call === 'install').length, 1);
  assert.equal(view.calls.filter(call => call === 'restart').length, 2);
});

test('periodic checks refresh the available update and stop when the app closes', async () => {
  const view = harness();
  await flush();
  view.events.get('interval')();
  await flush();
  assert.equal(view.calls.filter(call => call === 'check').length, 2);
  view.events.get('pagehide')();
  await flush();
  assert.ok(view.calls.includes('clear'));
  view.events.get('interval')();
  await flush();
  assert.equal(view.calls.filter(call => call === 'check').length, 2);
});
