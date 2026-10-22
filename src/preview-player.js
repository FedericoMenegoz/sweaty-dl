let fallbackRequest = 0;
const requestId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${++fallbackRequest}`;

export function canPreview(track) {
  try {
    const url = new URL(track.url);
    return track.kind !== 'album' && url.protocol === 'https:' && !url.username && !url.password
      && !url.port && url.hostname.endsWith('.bandcamp.com') && /^\/track\/[^/]+\/?$/.test(url.pathname);
  } catch { return false; }
}

export function clipWindow(audio) {
  const duration = audio.duration;
  if (!Number.isFinite(duration) || duration <= 0) throw new Error('Durata audio non disponibile.');
  const length = Math.min(10, duration);
  const midpoint = Math.max(0, (duration - length) / 2);
  for (let i = 0; i < audio.seekable.length; i++) {
    if (audio.seekable.start(i) <= midpoint && audio.seekable.end(i) >= midpoint + length) {
      return { start: midpoint, length, midpoint: true };
    }
  }
  return { start: 0, length, midpoint: false };
}

export function createPreviewPlayer({ resolve, cancel, onChange, diagnose = () => {},
  createAudio = () => new Audio(), newId = requestId,
  setTimer = setTimeout, clearTimer = clearTimeout,
}) {
  let current = null;
  function publish(job, phase, message, progress = 0) {
    if (job && job !== current) return;
    if (job) job.phase = phase;
    onChange({ phase, message, progress, url: job?.track.url || null });
  }
  function release(job) {
    if (!job) return;
    clearTimer(job.loadTimer);
    clearTimer(job.stopTimer);
    for (const [name, handler] of job.handlers) job.audio?.removeEventListener(name, handler);
    if (job.audio) {
      job.audio.pause();
      job.audio.removeAttribute('src');
      job.audio.load();
    }
    Promise.resolve(cancel(job.id)).catch(() => {});
  }
  function stop(message = 'Anteprima fermata.') {
    const previous = current;
    current = null;
    release(previous);
    publish(null, 'idle', message);
  }
  function fail(job, message) {
    if (job !== current) return;
    current = null;
    diagnose('preview_audio_failed', { message, media_error_code: job.audio?.error?.code, phase: job.phase }, job.id);
    release(job);
    publish(null, 'error', message);
  }
  function clipMessage(job) {
    const seconds = Math.round(job.clip.length * 10) / 10;
    return job.clip.midpoint ? `Anteprima: ${seconds} s al centro del flusso disponibile.`
      : `Anteprima: ${seconds} s dall’inizio (seek non disponibile).`;
  }
  function playing(job) {
    if (job !== current || job.phase === 'playing') return;
    clearTimer(job.loadTimer);
    diagnose('preview_audio_playing', { duration: job.audio.duration, clip_start: job.clip.start, clip_length: job.clip.length, midpoint: job.clip.midpoint }, job.id);
    publish(job, 'playing', clipMessage(job));
    job.stopTimer = setTimer(() => {
      if (job === current) stop('Anteprima terminata.');
    }, job.clip.length * 1000);
  }
  function play(job, userGesture = false) {
    if (job !== current || !job.clip || job.starting || job.phase === 'playing' || job.audio.seeking) return;
    if (job.phase === 'ready' && !userGesture) return;
    job.starting = true;
    // This call is synchronous on a second click if WebKit requires a fresh gesture.
    let promise;
    try { promise = job.audio.play(); }
    catch (error) { promise = Promise.reject(error); }
    Promise.resolve(promise).then(() => playing(job)).catch(error => {
      if (job !== current) return;
      if (error?.name === 'NotAllowedError') {
        clearTimer(job.loadTimer);
        diagnose('preview_user_gesture_required', {}, job.id);
        publish(job, 'ready', 'Anteprima pronta: premi ▶ per ascoltare.');
      } else fail(job, 'Riproduzione audio non riuscita. Riprova l’anteprima.');
    }).finally(() => { job.starting = false; });
  }
  async function toggle(track) {
    if (current?.track.url === track.url) {
      if (current.phase === 'ready') play(current, true);
      else stop();
      return;
    }
    stop('');
    if (!canPreview(track)) { publish(null, 'error', 'Anteprima disponibile per singoli brani Bandcamp.'); return; }
    const job = { id: newId(), track, audio: null, handlers: [], phase: 'loading' };
    current = job;
    publish(job, 'loading', `Caricamento anteprima: ${track.title}…`);
    try {
      const source = await resolve(track, job.id);
      if (current !== job) return;
      const url = new URL(source.stream_url);
      if (url.protocol !== 'https:' || !url.hostname.endsWith('.bcbits.com') || !url.pathname.startsWith('/stream/')
        || url.username || url.password || url.port) throw new Error('Sorgente audio non consentita.');
      const audio = job.audio = createAudio();
      const listen = (name, handler) => { job.handlers.push([name, handler]); audio.addEventListener(name, handler); };
      listen('loadedmetadata', () => {
        if (current !== job) return;
        try {
          job.clip = clipWindow(audio);
          audio.currentTime = job.clip.start;
          play(job);
        } catch { fail(job, 'Impossibile preparare dieci secondi di anteprima da questa sorgente.'); }
      });
      listen('canplay', () => play(job));
      listen('seeked', () => play(job));
      listen('playing', () => playing(job));
      listen('timeupdate', () => {
        if (current !== job || !job.clip || job.phase !== 'playing') return;
        const elapsed = audio.currentTime - job.clip.start;
        if (elapsed >= job.clip.length) stop('Anteprima terminata.');
        else publish(job, 'playing', clipMessage(job), Math.max(0, elapsed / job.clip.length));
      });
      listen('ended', () => { if (current === job) stop('Anteprima terminata.'); });
      listen('error', () => fail(job, 'Audio non disponibile o non supportato. Riprova l’anteprima.'));
      job.loadTimer = setTimer(() => fail(job, 'Il caricamento audio è scaduto. Riprova.'), 15000);
      audio.preload = 'metadata';
      audio.src = source.stream_url;
      audio.load();
    } catch (error) {
      // Never include signed media URLs or native media errors in the UI.
      fail(job, typeof error === 'string' ? error : 'Anteprima non disponibile per questo brano.');
    }
  }
  return { toggle, stop };
}
