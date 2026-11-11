import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { classifyMediaInput, bandcampReference, resolvedMediaItems, mergeMediaQueue } from '../src/media-input.js';

const video = 'abcdefghijk';
const otherVideo = 'lmnopqrstuv';
const playlist = 'https://www.youtube.com/playlist?list=PL_collection';
const track = (id = video) => ({ title: `Song ${id}`, artist: 'Artist', url: `https://www.youtube.com/watch?v=${id}`, source: 'YouTube', kind: 'track' });
const report = () => ({ source: 'YouTube', kind: 'playlist', url: playlist, title: 'Collection', results: [track()], unavailable_count: 0, warnings: [], complete: true });

test('matches the shared native/frontend URL classification contract', () => {
  const matrix = JSON.parse(readFileSync(new URL('./fixtures/media-urls.json', import.meta.url), 'utf8'));
  for (const { input, source, kind, url } of matrix.valid) {
    const actual = classifyMediaInput(input);
    assert.deepEqual({ source: actual.source, kind: actual.kind, url: actual.url }, { source, kind, url }, input);
  }
  for (const input of matrix.invalid) assert.throws(() => classifyMediaInput(input), undefined, input);
});

test('recognizes supported direct links and canonicalizes provider variants', () => {
  const cases = [
    ['https://artist.bandcamp.com/album/release/?from=search', 'Bandcamp', 'album', 'https://artist.bandcamp.com/album/release'],
    ['https://artist.bandcamp.com/track/song#player', 'Bandcamp', 'track', 'https://artist.bandcamp.com/track/song'],
    ['https://m.soundcloud.com/artist/sets/collection?si=tracking', 'SoundCloud', 'playlist', 'https://soundcloud.com/artist/sets/collection'],
    ['https://www.soundcloud.com/artist/song?secret_token=s-token&in=collection', 'SoundCloud', 'track', 'https://soundcloud.com/artist/song?secret_token=s-token'],
    ['https://on.soundcloud.com/abc123?si=tracking', 'SoundCloud', 'short', 'https://on.soundcloud.com/abc123'],
    [`https://youtu.be/${video}?si=tracking`, 'YouTube', 'track', track().url],
    [`https://music.youtube.com/watch?v=${video}&t=20`, 'YouTube', 'track', track().url],
    [`https://m.youtube.com/shorts/${video}`, 'YouTube', 'track', track().url],
    ['https://music.youtube.com/playlist?list=PL_collection&si=tracking', 'YouTube', 'playlist', playlist],
  ];
  for (const [url, source, kind, canonical] of cases) {
    const parsed = classifyMediaInput(` ${url} `);
    assert.equal(parsed.source, source, url);
    assert.equal(parsed.kind, kind, url);
    assert.equal(parsed.url, canonical, url);
  }
});

test('explicit list takes precedence only on valid video routes and records the ambiguity', () => {
  for (const url of [`https://youtube.com/watch?v=${video}&list=PL_collection`, `https://youtu.be/${video}?list=PL_collection`, `https://youtube.com/shorts/${video}?list=PL_collection`]) {
    const parsed = classifyMediaInput(url);
    assert.equal(parsed.url, playlist);
    assert.equal(parsed.videoContext, true);
  }
  assert.equal(classifyMediaInput(playlist).videoContext, false);
});

test('URL-like unsafe, malformed, dynamic and unsupported links never fall back to text search', () => {
  for (const value of [
    'http://soundcloud.com/artist/song', 'soundcloud.com/artist/song', 'www.youtube.com/playlist?list=PL_collection',
    'https:youtube.com', 'https://user:pass@youtube.com/playlist?list=PL_collection',
    'https://youtube.com.evil.test/playlist?list=PL_collection', 'https://evil.test/youtube.com/playlist?list=PL_collection',
    'https://artist.bandcamp.com', 'https://artist.bandcamp.com/album/a%2Fb',
    'https://soundcloud.com/artist', 'https://soundcloud.com/artist/likes', 'https://soundcloud.com/search/song',
    'https://soundcloud.com/artist/sets', 'https://soundcloud.com/artist//song',
    'https://youtube.com/@artist', 'https://youtube.com/channel/channel?list=PL_collection',
    `https://youtube.com/watch?v=${video}&list=`, `https://youtube.com/watch?v=${video}&list=RD_mix`,
    'https://youtube.com/playlist?list=UL_mix', 'https://youtube.com/playlist?list=',
    `https://youtube.com/watch?v=${video}&list=PL_one&list=PL_two`,
    `https://youtube.com/watch?v=${video}&v=${otherVideo}`, 'https://youtube.com/watch?list=PL_collection',
    'https://youtube.com/watch?v=short&list=PL_collection', 'https://youtu.be/short',
    'file:///etc/passwd', 'https://127.0.0.1/song', 'https://youtube.com\\@evil.test/song',
  ]) assert.throws(() => classifyMediaInput(value), undefined, value);
});

test('text search remains text and retains its own shorter length limit', () => {
  assert.deepEqual(classifyMediaInput(' Song - Artist '), { type: 'text', text: 'Song - Artist' });
  assert.deepEqual(classifyMediaInput('Artist: Song'), { type: 'text', text: 'Artist: Song' });
  assert.deepEqual(classifyMediaInput('Bicep: Glue'), { type: 'text', text: 'Bicep: Glue' });
  assert.throws(() => classifyMediaInput('x'.repeat(501)), /500/);
  assert.throws(() => classifyMediaInput(''));
  assert.equal(classifyMediaInput(`https://youtu.be/${video}?tracking=${'x'.repeat(600)}`).kind, 'track');
});

test('Bandcamp reference preserves album identity for the existing album downloader', () => {
  const input = classifyMediaInput('https://artist.bandcamp.com/album/some-release?from=search');
  assert.deepEqual(bandcampReference(input), { title: 'some release', artist: '', source: 'Bandcamp', kind: 'album', url: input.url });
});

test('validates entire resolved collection before adding any references', () => {
  const input = classifyMediaInput(playlist);
  const valid = report();
  valid.results[0].url = `https://youtu.be/${video}?si=tracking`;
  valid.results[0].downloadStatus = 'done';
  const items = resolvedMediaItems(valid, input);
  assert.equal(items[0].url, track().url);
  assert.equal(items[0].downloadStatus, undefined);
  for (const invalid of [
    { ...report(), complete: false },
    { ...report(), results: [] },
    { ...report(), results: Array.from({ length: 501 }, () => track()) },
    { ...report(), source: 'SoundCloud' },
    { ...report(), kind: 'track' },
    { ...report(), unavailable_count: -1 },
    { ...report(), warnings: [null] },
    { ...report(), url: 'https://www.youtube.com/playlist?list=PL_other' },
    { ...report(), results: [track(), { ...track(otherVideo), url: playlist }] },
    { ...report(), results: [{ ...track(), kind: 'playlist' }] },
    { ...report(), results: [{ ...track(), source: 'SoundCloud' }] },
  ]) assert.throws(() => resolvedMediaItems(invalid, input));
});

test('SoundCloud short link may resolve to either a track or playlist but not another source', () => {
  const input = classifyMediaInput('https://on.soundcloud.com/token');
  const item = { title: 'Song', artist: 'Artist', kind: 'track', source: 'SoundCloud', url: 'https://soundcloud.com/artist/song' };
  const response = { ...report(), source: 'SoundCloud', kind: 'track', url: item.url, results: [item] };
  assert.equal(resolvedMediaItems(response, input)[0].url, item.url);
  assert.throws(() => resolvedMediaItems(report(), input));
});

test('queue merge preserves order, existing completion state, and distinct versions', () => {
  const completed = { ...track(), url: `https://youtu.be/${video}?si=tracking`, downloadStatus: 'done', downloadFiles: ['existing.opus'] };
  const existing = [completed];
  const merged = mergeMediaQueue(existing, [track(), track(otherVideo), track(otherVideo)]);
  assert.equal(merged.added, 1);
  assert.equal(merged.duplicates, 2);
  assert.equal(merged.queue[0], completed);
  assert.equal(merged.queue[0].downloadStatus, 'done');
  assert.equal(merged.queue[1].url, track(otherVideo).url);
  assert.equal(merged.queue[1].downloadStatus, 'pending');
  assert.equal(existing.length, 1);
  assert.equal(mergeMediaQueue([], [bandcampReference(classifyMediaInput('https://artist.bandcamp.com/track/song')), bandcampReference(classifyMediaInput('https://artist.bandcamp.com/track/song-live'))]).added, 2);
});

test('capacity failure never partially mutates the existing selection', () => {
  const existing = Array.from({ length: 2000 }, (_, index) => ({ title: `Existing ${index}`, url: `https://example.com/${index}` }));
  assert.throws(() => mergeMediaQueue(existing, [track()]), /2,000/);
  assert.equal(existing.length, 2000);
});
