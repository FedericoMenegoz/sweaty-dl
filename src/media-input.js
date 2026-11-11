const YOUTUBE_HOSTS = new Set(['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com']);
const SOUNDCLOUD_HOSTS = new Set(['soundcloud.com', 'www.soundcloud.com', 'm.soundcloud.com']);
const SC_RESERVED = new Set(['search', 'discover', 'you', 'stations', 'charts', 'feed', 'stream', 'sets', 'albums', 'tracks', 'likes', 'reposts', 'spotlight', 'comments', 'upload', 'settings']);
const URL_LIKE = /^(?:[a-z][a-z\d+.-]*:\/\/|(?:https?|file|ftp|data|javascript|mailto|spotify):|\/\/|www\.|(?:[\w-]+\.)+[\w-]+(?::\d+)?(?:[/?#]|$))/i;

function invalidLink() {
  throw new Error('Enter an HTTPS link to a supported track, Bandcamp album, or SoundCloud/YouTube playlist.');
}

function singleParameter(url, name) {
  const values = url.searchParams.getAll(name);
  if (values.length > 1) invalidLink();
  return values[0];
}

/** Classify only explicit catalog links. URL-like invalid input must never become a search. */
export function classifyMediaInput(raw) {
  if (typeof raw !== 'string') invalidLink();
  const value = raw.trim();
  if (!URL_LIKE.test(value)) {
    if (!value || value.length > 500) throw new Error('Enter a valid title (maximum 500 characters).');
    return { type: 'text', text: value };
  }
  if (value.length > 8192 || !/^https:\/\//i.test(value) || /[\s\u0000-\u001f\u007f\\]/.test(value)
    || /^https:\/\/[^/?#]*:(?:[/?#]|$)/i.test(value)) invalidLink();
  let url;
  try { url = new URL(value); } catch { invalidLink(); }
  if (url.protocol !== 'https:' || url.username || url.password || url.port) invalidLink();
  const parts = url.pathname.split('/').filter(Boolean);
  const path = url.pathname.replace(/\/$/, '');
  // Repeated slashes and encoded separators are not catalog paths.
  if (/\/\//.test(url.pathname) || /%2f|%5c/i.test(url.pathname)) invalidLink();
  url.hash = '';
  const result = (source, kind, canonical, extra = {}) => ({ type: 'url', source, kind, url: canonical, ...extra });
  if (url.hostname.endsWith('.bandcamp.com')) {
    if (parts.length !== 2 || !['track', 'album'].includes(parts[0])) invalidLink();
    let slug;
    try { slug = decodeURIComponent(parts[1]); } catch { invalidLink(); }
    if (!slug || /[\s\u0000-\u001f\u007f\\/]/.test(slug)) invalidLink();
    return result('Bandcamp', parts[0], `https://${url.hostname}/${parts[0]}/${parts[1]}`);
  }
  if (SOUNDCLOUD_HOSTS.has(url.hostname)) {
    const playlist = parts.length === 3 && parts[1] === 'sets';
    if ((!playlist && parts.length !== 2) || parts.some(part => !/^[A-Za-z0-9_-]+$/.test(part))
      || SC_RESERVED.has(parts[0].toLowerCase()) || (!playlist && SC_RESERVED.has(parts[1].toLowerCase()))) invalidLink();
    const token = singleParameter(url, 'secret_token');
    if (token !== undefined && !/^[A-Za-z0-9_-]{1,200}$/.test(token)) invalidLink();
    const canonical = `https://soundcloud.com/${parts.join('/')}${token ? `?secret_token=${token}` : ''}`;
    return result('SoundCloud', playlist ? 'playlist' : 'track', canonical);
  }
  if (url.hostname === 'on.soundcloud.com') {
    if (parts.length !== 1 || !/^[A-Za-z0-9_-]+$/.test(parts[0])) invalidLink();
    return result('SoundCloud', 'short', `https://on.soundcloud.com/${parts[0]}`);
  }
  if (YOUTUBE_HOSTS.has(url.hostname) || url.hostname === 'youtu.be') {
    const playlistId = singleParameter(url, 'list');
    const queryVideo = singleParameter(url, 'v');
    let videoId;
    const playlistPage = YOUTUBE_HOSTS.has(url.hostname) && path === '/playlist';
    if (!playlistPage) {
      if (url.hostname === 'youtu.be' && parts.length === 1) videoId = parts[0];
      else if (YOUTUBE_HOSTS.has(url.hostname) && path === '/watch') videoId = queryVideo;
      else if (YOUTUBE_HOSTS.has(url.hostname) && parts.length === 2 && parts[0] === 'shorts') videoId = parts[1];
      if (!videoId || !/^[A-Za-z0-9_-]{11}$/.test(videoId)) invalidLink();
    }
    if (playlistId !== undefined || playlistPage) {
      if (!playlistId || !/^[A-Za-z0-9_-]{1,200}$/.test(playlistId)) invalidLink();
      if (/^(RD|UL)/.test(playlistId)) throw new Error('YouTube radio playlists and automatic mixes are not supported. Use a saved playlist.');
      return result('YouTube', 'playlist', `https://www.youtube.com/playlist?list=${playlistId}`, { videoContext: !playlistPage });
    }
    return result('YouTube', 'track', `https://www.youtube.com/watch?v=${videoId}`);
  }
  invalidLink();
}

export function bandcampReference(input) {
  if (input.source !== 'Bandcamp' || !['track', 'album'].includes(input.kind)) invalidLink();
  const title = decodeURIComponent(new URL(input.url).pathname.split('/').at(-1)).replace(/[-_]/g, ' ');
  return { title, artist: '', url: input.url, source: input.source, kind: input.kind };
}

/** Validate the complete response before allowing any queue mutation. */
export function resolvedMediaItems(report, input) {
  if (!report || report.complete !== true || report.source !== input.source
    || !['track', 'playlist'].includes(report.kind) || (input.kind !== 'short' && report.kind !== input.kind)
    || typeof report.title !== 'string' || !Array.isArray(report.results) || !report.results.length || report.results.length > 500
    || !Number.isSafeInteger(report.unavailable_count) || report.unavailable_count < 0
    || !Array.isArray(report.warnings) || report.warnings.some(warning => typeof warning !== 'string')) {
    throw new Error('Incomplete or invalid import response. The selection was not changed.');
  }
  const collection = classifyMediaInput(report.url);
  if (collection.type !== 'url' || collection.source !== input.source || collection.kind !== report.kind
    || (input.kind !== 'short' && collection.url !== input.url)
    || (report.kind === 'track' && report.results.length > 1)) invalidLink();
  return report.results.map(track => {
    if (!track || typeof track.title !== 'string' || !track.title.trim() || typeof track.artist !== 'string'
      || track.source !== input.source || track.kind !== 'track') invalidLink();
    const reference = classifyMediaInput(track.url);
    if (reference.type !== 'url' || reference.source !== input.source || reference.kind !== 'track'
      || (report.kind === 'track' && reference.url !== collection.url)) invalidLink();
    const item = { title: track.title, artist: track.artist, url: reference.url, source: input.source, kind: 'track' };
    if (typeof track.artwork === 'string') item.artwork = track.artwork;
    if (typeof track.duration === 'number' && Number.isFinite(track.duration) && track.duration > 0) item.duration = track.duration;
    return item;
  });
}

function identity(track) {
  try {
    const input = classifyMediaInput(track.url);
    if (input.type === 'url') return input.url;
  } catch { /* Existing generic TXT references keep their original identity. */ }
  return track.url;
}

export function mergeMediaQueue(existing, tracks) {
  const seen = new Set(existing.map(identity));
  const added = [];
  let duplicates = 0;
  for (const track of tracks) {
    const key = identity(track);
    if (seen.has(key)) { duplicates++; continue; }
    seen.add(key);
    added.push({ ...track, downloadStatus: 'pending' });
  }
  if (existing.length + added.length > 2000) throw new Error('The selection can contain no more than 2,000 items. No new tracks were added.');
  return { queue: [...existing, ...added], added: added.length, duplicates };
}
