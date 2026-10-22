"""Provider catalog URL validation, independent of the legacy application."""
from urllib.parse import urlparse


def provider_for_url(url):
    parsed = urlparse(url)
    from urllib.parse import parse_qs
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError(f'URL HTTPS non valido: {url}')
    parts = [p for p in parsed.path.split('/') if p]
    if host.endswith('.bandcamp.com') and len(parts) == 2 and parts[0] in ('track', 'album'):
        return 'Bandcamp'
    if host in ('soundcloud.com', 'www.soundcloud.com') and len(parts) == 2 and parts[0] not in ('search', 'discover', 'you'):
        return 'SoundCloud'
    if host in ('youtube.com', 'www.youtube.com', 'music.youtube.com'):
        if parsed.path == '/watch' and parse_qs(parsed.query).get('v'):
            return 'YouTube'
        if len(parts) == 2 and parts[0] == 'shorts':
            return 'YouTube'
    if host == 'youtu.be' and len(parts) == 1:
        return 'YouTube'
    raise ValueError(f'Inserisci un link a un brano Bandcamp, SoundCloud o YouTube: {url}')

