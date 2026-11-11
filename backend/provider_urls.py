"""Classify supported catalog URLs without fetching pages or trusting URL text."""
import re
from urllib.parse import parse_qs, unquote, urlencode, urlsplit


VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}\Z')
PLAYLIST_ID = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
SLUG = re.compile(r'[A-Za-z0-9_-]+\Z')
SC_RESERVED = {'search', 'discover', 'you', 'stations', 'charts', 'feed', 'stream',
               'sets', 'albums', 'tracks', 'likes', 'reposts', 'spotlight', 'comments', 'upload', 'settings'}


def classify_media_url(value):
    """Return source, kind, and canonical URL; errors never echo the supplied URL.

    A SoundCloud share URL has kind ``short`` until the guarded resolver follows
    its redirects. YouTube collection context takes precedence only on supported
    video URLs with valid video IDs, or on the explicit playlist route.
    """
    if not isinstance(value, str):
        raise ValueError('Invalid URL.')
    value = value.strip()
    if not value or len(value) > 8192 or re.search(r'[\s\x00-\x20\x7f\\]', value):
        raise ValueError('Invalid URL.')
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        valid = (parsed.scheme == 'https' and not parsed.username and not parsed.password
                 and parsed.port in (None, 443) and not parsed.netloc.endswith(':'))
    except ValueError:
        raise ValueError('Invalid URL.') from None
    if not valid:
        raise ValueError('Use an HTTPS URL without credentials.')
    path = parsed.path.rstrip('/')
    if '//' in parsed.path or re.search(r'%2f|%5c', parsed.path, re.I):
        raise ValueError('Invalid link path.')
    parts = path.split('/')[1:]
    query = parse_qs(parsed.query, keep_blank_values=True)

    def reference(source, kind, url):
        return {'source': source, 'kind': kind, 'url': url}

    if host.endswith('.bandcamp.com') and len(parts) == 2 and parts[0] in ('track', 'album'):
        try:
            slug = unquote(parts[1], errors='strict')
        except (UnicodeError, ValueError):
            raise ValueError('Invalid Bandcamp link.') from None
        if (not slug or slug in ('.', '..') or re.search(r'[\s\x00-\x20\x7f\\/]', slug)
                or re.search(r'%(?![A-Fa-f0-9]{2})', parts[1])):
            raise ValueError('Invalid Bandcamp link.')
        return reference('Bandcamp', parts[0], f'https://{host}/{parts[0]}/{parts[1]}')

    if host in ('soundcloud.com', 'www.soundcloud.com', 'm.soundcloud.com'):
        if not parts or parts[0].lower() in SC_RESERVED or not all(SLUG.fullmatch(part) for part in parts):
            raise ValueError('Enter a link to a SoundCloud track or playlist.')
        kind = ('track' if len(parts) == 2 else
                'playlist' if len(parts) == 3 and parts[1] == 'sets' else None)
        if kind == 'track' and parts[1].lower() in SC_RESERVED:
            kind = None
        if kind:
            secret = query.get('secret_token')
            if secret is not None and (len(secret) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', secret[0])):
                raise ValueError('Invalid SoundCloud link.')
            suffix = '?' + urlencode({'secret_token': secret[0]}) if secret else ''
            return reference('SoundCloud', kind, 'https://soundcloud.com/' + '/'.join(parts) + suffix)

    if host == 'on.soundcloud.com' and len(parts) == 1 and SLUG.fullmatch(parts[0]):
        return reference('SoundCloud', 'short', f'https://on.soundcloud.com/{parts[0]}')

    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'):
        # Duplicate identity parameters are ambiguous even if one is valid.
        if any(len(query.get(key, [])) > 1 for key in ('v', 'list')):
            raise ValueError('Ambiguous YouTube link.')
        video = None
        playlist_route = host != 'youtu.be' and path == '/playlist'
        if host == 'youtu.be' and len(parts) == 1:
            video = parts[0]
        elif host != 'youtu.be' and path == '/watch':
            video = (query.get('v') or [''])[0]
        elif host != 'youtu.be' and len(parts) == 2 and parts[0] == 'shorts':
            video = parts[1]
        if not playlist_route and (video is None or not VIDEO_ID.fullmatch(video)):
            raise ValueError('Enter a link to a YouTube video or playlist.')
        if 'list' in query or playlist_route:
            playlist = (query.get('list') or [''])[0]
            if not PLAYLIST_ID.fullmatch(playlist):
                raise ValueError('Invalid YouTube playlist ID.')
            if playlist.startswith(('RD', 'UL')):
                raise ValueError('Automatic YouTube mixes and radio are not supported; use a finite playlist.')
            return reference('YouTube', 'playlist', 'https://www.youtube.com/playlist?' + urlencode({'list': playlist}))
        return reference('YouTube', 'track', 'https://www.youtube.com/watch?' + urlencode({'v': video}))

    raise ValueError('Enter a link to a Bandcamp, SoundCloud, or YouTube track, album, or playlist.')


def provider_for_url(url):
    """Keep the existing search-provider validation API."""
    return classify_media_url(url)['source']

