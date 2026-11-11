"""Resolve public collection links into bounded catalog references, never audio."""
import contextlib
import math
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:
    from . import diagnostics as diag
    from .download import UserError, public_connections, public_url
    from .provider_urls import classify_media_url, VIDEO_ID
    from .runtime_tools import javascript_options
except ImportError:
    import diagnostics as diag
    from download import UserError, public_connections, public_url
    from provider_urls import classify_media_url, VIDEO_ID
    from runtime_tools import javascript_options


MAX_ENTRIES = 500
RESOLUTION_FAILED = 'Unable to read the link: check your network and content availability.'


def _classify(value):
    try:
        return classify_media_url(value)
    except ValueError as error:
        raise UserError(str(error)) from None


def _follow_soundcloud_share(reference):
    """Follow only SoundCloud catalog/share destinations, without reading bodies."""
    import requests
    with requests.Session() as session:
        session.trust_env = False
        for _ in range(6):
            if reference['kind'] != 'short':
                return reference
            public_url(reference['url'])
            with session.get(reference['url'], allow_redirects=False, stream=True,
                             timeout=(5, 10)) as response:
                if not response.is_redirect:
                    raise UserError('The SoundCloud short link does not lead to a track or playlist.')
                location = response.headers.get('Location')
                if not location:
                    raise UserError('Invalid SoundCloud redirect.')
                reference = _classify(urljoin(reference['url'], location))
                if reference['source'] != 'SoundCloud':
                    raise UserError('The short link must lead to SoundCloud.')
        if reference['kind'] != 'short':
            return reference
    raise UserError('Too many redirects in the SoundCloud link.')


class _ResolverLogger:
    def __init__(self):
        self.incomplete = False
        self.hidden_unavailable = False

    def debug(self, _message):
        pass

    def warning(self, message):
        diag.log('collection_extractor_warning', level='warn', message=message)
        # Some extractors return accumulated entries after exhausting page retries.
        lowered = str(message).lower()
        if 'unavailable' in lowered and 'hidden' in lowered:
            self.hidden_unavailable = True
        if ('giving up' in lowered or 'skipping remaining' in lowered
                or ('incomplete' in lowered and 'retry' not in lowered)):
            self.incomplete = True

    def error(self, message):
        diag.log('collection_extractor_error', level='error', message=message)
        self.incomplete = True


@contextlib.contextmanager
def _metadata_downloader():
    from yt_dlp import YoutubeDL
    from yt_dlp.networking._urllib import RedirectHandler, UrllibRH

    logger = _ResolverLogger()
    options = {
        'quiet': True, 'no_warnings': False, 'logger': logger,
        'skip_download': True, 'extract_flat': 'in_playlist', 'lazy_playlist': True,
        # Read at most one entry beyond the limit, so large playlists fail visibly.
        'playlistend': MAX_ENTRIES + 1, 'noplaylist': False, 'ignoreerrors': False,
        # Keep metadata for protected/login/geo-blocked children so they can be
        # counted as unavailable. Extraction and pagination errors remain fatal.
        'ignore_no_formats_error': True,
        'socket_timeout': 12, 'retries': 1, 'extractor_retries': 1,
        'cachedir': False, 'proxy': '', 'external_downloader': None,
        'postprocessors': [], 'remote_components': [], **javascript_options(),
        # YouTube otherwise treats exhausted incomplete-data retries as a warning
        # and can silently return the pages collected so far.
        'extractor_args': {'youtube': {'raise_incomplete_data': ['1']}},
    }
    original_redirect = RedirectHandler.redirect_request

    def redirect(handler, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return original_redirect(handler, req, fp, code, msg, headers, newurl)

    class MetadataDownloader(YoutubeDL):
        def urlopen(self, request):
            public_url(request if isinstance(request, str) else request.url)
            return super().urlopen(request)

    RedirectHandler.redirect_request = redirect
    try:
        with MetadataDownloader(options) as downloader:
            director = downloader._request_director
            for handler in list(director.handlers.values()):
                if not isinstance(handler, UrllibRH):
                    director.handlers.pop(handler.RH_KEY, None)
                    handler.close()
            yield downloader, logger
    finally:
        RedirectHandler.redirect_request = original_redirect


def _text(value, fallback='', limit=500):
    text = ''.join(c for c in str(value or '') if ord(c) >= 32).strip()
    return (text or fallback)[:limit]


def _unavailable(entry):
    return (entry is None or isinstance(entry, dict) and (
        entry.get('availability') in {'private', 'needs_auth', 'premium_only', 'subscriber_only', 'unavailable'}
        or entry.get('_type') == 'unavailable'
        or entry.get('has_drm')
        or entry.get('formats') == []
        or entry.get('is_live')
        or entry.get('live_status') in {'is_live', 'is_upcoming', 'post_live'}
        or str(entry.get('title', '')).strip().lower() in {'[deleted video]', '[private video]'}))


def _soundcloud_api_track(value):
    """Only extractor-produced numeric API track references may be hydrated."""
    if not isinstance(value, str):
        return False
    parsed = public_url(value)
    return (parsed.hostname in ('api.soundcloud.com', 'api-v2.soundcloud.com')
            and re.fullmatch(r'/tracks/[0-9]+', parsed.path) is not None)


def _track(entry, source, downloader):
    if not isinstance(entry, dict):
        raise UserError('The playlist contains an invalid item.')
    if entry.get('_type') in ('playlist', 'multi_video') or entry.get('entries') is not None:
        raise UserError('Nested playlists are not supported.')
    value = entry.get('webpage_url') or entry.get('url')
    if source == 'YouTube' and isinstance(value, str) and VIDEO_ID.fullmatch(value):
        value = 'https://www.youtube.com/watch?v=' + value
    elif source == 'YouTube' and isinstance(value, str):
        # Collection context on a child video is incidental. Remove it before
        # classification; /playlist-only and malformed video routes still fail.
        parsed = urlsplit(value)
        value = urlunsplit(parsed._replace(query=urlencode([
            (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key != 'list'
        ])))
    if source == 'SoundCloud' and _soundcloud_api_track(value):
        # SoundCloud may omit public permalinks in a flat collection response.
        # Recover the canonical catalog URL; metadata requests never transfer audio.
        entry = downloader.extract_info(value, download=False)
        if _unavailable(entry):
            return None
        if not isinstance(entry, dict):
            raise UserError('Invalid SoundCloud metadata.')
        value = entry.get('webpage_url')
    reference = _classify(value)
    if reference['source'] != source or reference['kind'] != 'track':
        raise UserError('The playlist contains a link that is not a track from the requested source.')
    slug = urlsplit(reference['url']).path.rsplit('/', 1)[-1]
    fallback = slug.replace('-', ' ').replace('_', ' ') if source != 'YouTube' else 'YouTube video'
    result = {**reference, 'title': _text(entry.get('title'), fallback),
              'artist': _text(entry.get('artist') or entry.get('uploader') or entry.get('channel'))}
    duration = entry.get('duration')
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration > 0:
        result['duration'] = duration
    # Keep only catalog display metadata; never copy formats or stream URLs.
    artwork = entry.get('thumbnail')
    if not artwork:
        thumbnails = entry.get('thumbnails')
        if isinstance(thumbnails, list) and thumbnails and isinstance(thumbnails[-1], dict):
            artwork = thumbnails[-1].get('url')
    if isinstance(artwork, str):
        try:
            public_url(artwork)
            result['artwork'] = artwork[:8192]
        except (ValueError, TypeError):
            pass
    return result


def _resolve(reference, downloader, logger):
    info = downloader.extract_info(reference['url'], download=False)
    if not isinstance(info, dict) or _unavailable(info):
        raise UserError('The content requires account access or is not yet available for download.')
    results, unavailable = [], 0
    if reference['kind'] == 'playlist':
        if info.get('_type') not in ('playlist', 'multi_video') or info.get('entries') is None:
            raise UserError('The response does not contain a valid playlist.')
        known_count = info.get('playlist_count')
        if isinstance(known_count, int) and known_count > MAX_ENTRIES:
            raise UserError('The playlist exceeds the limit of 500 tracks; it was not imported.')
        for index, entry in enumerate(info['entries']):
            if index >= MAX_ENTRIES:
                raise UserError('The playlist exceeds the limit of 500 tracks; it was not imported.')
            if _unavailable(entry):
                unavailable += 1
                continue
            result = _track(entry, reference['source'], downloader)
            if result is None:
                unavailable += 1
            else:
                results.append(result)
    else:
        result = _track(info, reference['source'], downloader)
        if result:
            if result['url'] != reference['url']:
                raise UserError('The link does not identify the requested track.')
            results.append(result)
    if logger.incomplete:
        raise UserError('Playlist reading incomplete; try again. No tracks were imported.')
    if not results:
        raise UserError('No tracks available at this link.')
    warnings = [f'{unavailable} unavailable items were excluded.'] if unavailable else []
    if logger.hidden_unavailable:
        warnings.append('The source hides some unavailable items; their number is unknown.')
    return {**reference, 'title': _text(info.get('title'), 'Playlist' if reference['kind'] == 'playlist' else results[0]['title']),
            'results': results, 'unavailable_count': unavailable, 'warnings': warnings, 'complete': True}


def resolve_media_url(url):
    """Resolve the full finite collection or raise a sanitized, actionable error."""
    try:
        reference = _classify(url)
        # Bandcamp albums retain the native browser-assisted download path.
        if reference['source'] == 'Bandcamp':
            title = urlsplit(reference['url']).path.rsplit('/', 1)[-1].replace('-', ' ')
            return {**reference, 'title': title, 'results': [{**reference, 'title': title, 'artist': ''}],
                    'unavailable_count': 0, 'warnings': [], 'complete': True}
        with public_connections():
            if reference['kind'] == 'short':
                reference = _follow_soundcloud_share(reference)
            diag.stage('resolve_collection', source=reference['source'], kind=reference['kind'])
            with _metadata_downloader() as (downloader, logger):
                return _resolve(reference, downloader, logger)
    except UserError:
        raise
    except ImportError:
        raise UserError('The playlist reader component is unavailable in this installation.') from None
    except Exception as error:
        diag.exception(error)
        raise UserError(RESOLUTION_FAILED) from None
