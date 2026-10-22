import assert from 'node:assert/strict';
import test from 'node:test';
import { canPreview, clipWindow, createPreviewPlayer } from '../src/preview-player.js';

const track = { title: 'Song', url: 'https://artist.bandcamp.com/track/song' };
const source = { stream_url: 'https://t4.bcbits.com/stream/example/mp3-128/example?token=redacted' };
const tick = () => new Promise(resolve => setImmediate(resolve));
class AudioStub {
  constructor() {
    this.duration = 240; this.currentTime = 0; this.seeking = false;
    this.seekable = { length: 1, start: () => 0, end: () => 240 };
    this.events = new Map(); this.plays = 0; this.pauses = 0;
  }
  addEventListener(name, handler) { this.events.set(name, handler); }
  removeEventListener(name) { this.events.delete(name); }
  emit(name) { this.events.get(name)?.(); }
  play() { this.plays++; return this.rejectPlay ? Promise.reject({ name: 'NotAllowedError' }) : Promise.resolve(); }
  pause() { this.pauses++; }
  load() {}
  removeAttribute(name) { if (name === 'src') this.src = ''; }
}
function setup(resolve = async () => source) {
  const states = [], cancelled = [], audios = [], timers = new Map();
  let serial = 0, timer = 0;
  const player = createPreviewPlayer({ resolve,
    cancel: id => { cancelled.push(id); }, onChange: state => states.push(state),
    newId: () => `request-${++serial}`,
    createAudio: () => { const audio = new AudioStub(); audios.push(audio); return audio; },
    setTimer: (fn, ms) => { timers.set(++timer, { fn, ms }); return timer; },
    clearTimer: id => timers.delete(id),
  });
  return { player, states, cancelled, audios, timers, last: () => states.at(-1) };
}

test('preview candidates are single Bandcamp tracks, not albums or alternative providers', () => {
  assert.equal(canPreview(track), true);
  for (const url of ['https://artist.bandcamp.com/album/release', 'https://soundcloud.com/artist/song', 'https://artist.bandcamp.com.evil.test/track/song']) {
    assert.equal(canPreview({ url }), false);
  }
});
test('clip chooses midpoint for seekable audio and beginning otherwise', () => {
  const audio = new AudioStub();
  assert.deepEqual(clipWindow(audio), { start: 115, length: 10, midpoint: true });
  audio.seekable.length = 0;
  assert.deepEqual(clipWindow(audio), { start: 0, length: 10, midpoint: false });
  audio.duration = 4;
  assert.equal(clipWindow(audio).length, 4);
  audio.duration = NaN;
  assert.throws(() => clipWindow(audio));
});
test('playback seeks to the midpoint and stops at the clip boundary', async () => {
  const view = setup();
  await view.player.toggle(track);
  const audio = view.audios[0];
  audio.emit('loadedmetadata'); await tick();
  assert.equal(audio.currentTime, 115);
  assert.equal(view.last().phase, 'playing');
  audio.currentTime = 125; audio.emit('timeupdate');
  assert.equal(view.last().phase, 'idle');
  assert.equal(audio.src, '');
  assert.equal(audio.pauses, 1);
  assert.deepEqual(view.cancelled, ['request-1']);
});
test('wall-clock limit stops playback even without timeupdate events', async () => {
  const view = setup();
  await view.player.toggle(track);
  view.audios[0].emit('loadedmetadata'); await tick();
  const stop = [...view.timers.values()].find(timer => timer.ms === 10000);
  assert.ok(stop);
  stop.fn();
  assert.equal(view.last().phase, 'idle');
  assert.equal(view.audios[0].src, '');
});
test('switching tracks cancels the first request and ignores its late result', async () => {
  const pending = [];
  const view = setup(() => new Promise(resolve => pending.push(resolve)));
  const first = view.player.toggle(track);
  const second = view.player.toggle({ ...track, url: 'https://artist.bandcamp.com/track/song-live' });
  pending[0](source); await first;
  assert.equal(view.audios.length, 0);
  pending[1](source); await second;
  assert.equal(view.audios.length, 1);
  assert.deepEqual(view.cancelled, ['request-1']);
  view.player.stop();
  assert.deepEqual(view.cancelled, ['request-1', 'request-2']);
});
test('stop during metadata resolution prevents late audio playback', async () => {
  let finish;
  const view = setup(() => new Promise(resolve => { finish = resolve; }));
  const pending = view.player.toggle(track);
  view.player.stop();
  finish(source); await pending;
  assert.equal(view.audios.length, 0);
  assert.equal(view.last().phase, 'idle');
});
test('autoplay rejection offers a second click without re-resolving the source', async () => {
  let requests = 0;
  const view = setup(async () => { requests++; return source; });
  await view.player.toggle(track);
  const audio = view.audios[0]; audio.rejectPlay = true;
  audio.emit('loadedmetadata'); await tick();
  assert.equal(view.last().phase, 'ready');
  audio.rejectPlay = false;
  await view.player.toggle(track); await tick();
  assert.equal(view.last().phase, 'playing');
  assert.equal(requests, 1);
  assert.equal(audio.plays, 2);
});
test('untrusted media URLs are rejected before creating audio', async () => {
  const view = setup(async () => ({ stream_url: 'https://evil.test/stream/example' }));
  await view.player.toggle(track);
  assert.equal(view.audios.length, 0);
  assert.equal(view.last().phase, 'error');
  assert.equal(view.last().message.includes('evil.test'), false);
});
