"""Provider searches and conservative, version-aware fallback matching."""
from dataclasses import dataclass, field
import re
from itertools import islice
import unicodedata
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from .download import public_connections, public_url
    from .collection_resolver import _metadata_downloader
except ImportError:
    from download import public_connections, public_url
    from collection_resolver import _metadata_downloader


class SearchError(RuntimeError):
    pass


class BandcampChallenge(SearchError):
    pass


@dataclass(frozen=True)
class Result:
    title: str
    artist: str
    url: str
    source: str
    kind: str = 'track'
    artwork: str = ''
    duration: float | None = None


@dataclass
class SearchReport:
    results: list[Result] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    searched: list[str] = field(default_factory=list)
    browser_query: str | None = None


def tokens(text):
    text = unicodedata.normalize('NFKD', text.casefold())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return set(re.findall(r'\w+', text))


VERSIONS = {'remix', 'edit', 'live', 'cover', 'instrumental', 'extended', 'acoustic', 'vip'}


def convincing(result, title, artist=''):
    requested = tokens(title)
    actual = tokens(result.title)
    # A remix/live/cover must never silently satisfy a request for the original.
    if (requested & VERSIONS) != (actual & VERSIONS):
        return False
    if not requested or not requested <= actual:
        return False
    presentation = {'official', 'audio', 'video', 'lyrics', 'hq', 'hd', 'visualizer'}
    if actual - requested - tokens(artist) - tokens(result.artist) - presentation:
        return False
    return not artist.strip() or tokens(artist) <= tokens(result.artist + ' ' + result.title)


def parse_bandcamp(html):
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True).casefold() if soup.title else ''
    if title == 'client challenge' or 'JavaScript is disabled in your browser' in soup.get_text(' ', strip=True):
        raise BandcampChallenge('Bandcamp requires browser verification.')
    results = []
    for item in soup.select('.searchresult'):
        anchor = item.select_one('.heading a')
        if not anchor:
            continue
        url = urljoin('https://bandcamp.com', anchor.get('href', '')).split('?')[0]
        from provider_urls import provider_for_url
        try:
            if provider_for_url(url) != 'Bandcamp':
                continue
        except ValueError:
            continue
        artist = item.select_one('.subhead')
        image = item.select_one('img')
        artist_text = artist.get_text(' ', strip=True) if artist else ''
        artist_text = artist_text.rsplit(' by ', 1)[-1]
        results.append(Result(
            title=anchor.get_text(' ', strip=True),
            artist=re.sub(r'^by\s+', '', artist_text),
            url=url, source='Bandcamp', kind='album' if '/album/' in url else 'track',
            artwork=urljoin('https://bandcamp.com', image.get('src', '')) if image else ''))
    if not soup.select_one('.searchresults, .result-items, .searchresult, .no-results, .noresults'):
        # A consent page, bot challenge, or changed markup is not an empty search.
        raise SearchError('Unrecognized Bandcamp page; try again or open the site.')
    return results


def bandcamp_search(query):
    try:
        url = requests.Request('GET', 'https://bandcamp.com/search',
                               params={'q': query, 'item_type': 't'}).prepare().url
        with public_connections(), requests.Session() as session:
            # No implicit .netrc credentials, proxies, or custom CA paths.
            session.trust_env = False
            for _ in range(6):
                public_url(url)
                with session.get(url, headers={'User-Agent': 'SweatyDL/0.4'},
                                 timeout=(8, 20), allow_redirects=False, stream=True) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get('Location', ''))
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_content(64 * 1024):
                        if len(body) + len(chunk) > 4 * 1024 * 1024:
                            raise SearchError('Bandcamp search response too large. Try again.')
                        body.extend(chunk)
                    return parse_bandcamp(bytes(body))
        raise SearchError('Too many redirects during Bandcamp search.')
    except (requests.RequestException, ValueError) as error:
        # Transport exceptions can contain signed URLs, credentials, or local paths.
        raise SearchError('Unable to reach Bandcamp. Check your network and try again.') from error


def ytdlp_search(source, query):
    try:
        from yt_dlp import YoutubeDL
    except ImportError as error:
        raise SearchError('The yt-dlp component is unavailable in this installation.') from error
    prefix = {'SoundCloud': 'scsearch8', 'YouTube': 'ytsearch8'}[source]
    try:
        # Reuse the URL/redirect checks and single reviewed urllib handler used
        # by collection resolution. Evaluate lazy entries while the guards live.
        with public_connections(), _metadata_downloader() as (downloader, logger):
            info = downloader.extract_info(f'{prefix}:{query}', download=False)
            entries = list(islice((info or {}).get('entries') or [], 9))
            if len(entries) > 8 or logger.incomplete:
                raise SearchError(f'{source}: incomplete search response. Try again.')
        if not info:
            raise SearchError(f'{source}: empty response from the service.')
        results = []
        for entry in entries:
            if not entry:
                continue
            url = entry.get('webpage_url') or entry.get('url') or ''
            if source == 'YouTube' and not url.startswith('https://'):
                url = f"https://www.youtube.com/watch?v={entry['id']}"
            from provider_urls import provider_for_url
            try:
                if provider_for_url(url) != source:
                    continue
            except ValueError:
                continue
            thumbnails = entry.get('thumbnails') or []
            results.append(Result(entry.get('title') or 'Untitled',
                                  entry.get('artist') or entry.get('uploader') or entry.get('channel') or '',
                                  url, source, artwork=entry.get('thumbnail') or
                                  (thumbnails[-1].get('url', '') if thumbnails else ''),
                                  duration=entry.get('duration')))
        return results
    except SearchError:
        raise
    except Exception as error:
        raise SearchError(f'Unable to search {source}. Check your network and content availability.') from error


def search_tracks(title, artist='', mode='auto', notify=lambda _: None, providers=None):
    title, artist = title.strip(), artist.strip()
    if not title:
        raise ValueError('Enter the track title.')
    providers = providers or {'Bandcamp': bandcamp_search,
                              'SoundCloud': lambda q: ytdlp_search('SoundCloud', q),
                              'YouTube': lambda q: ytdlp_search('YouTube', q)}
    report = SearchReport()
    sources = {'auto': ['Bandcamp', 'SoundCloud', 'YouTube'],
               'others': ['SoundCloud', 'YouTube'],
               'Bandcamp': ['Bandcamp'], 'SoundCloud': ['SoundCloud'],
               'YouTube': ['YouTube']}[mode]
    query = ' '.join(part for part in (artist, title) if part)
    for source in sources:
        notify(f'Searching {source}…')
        report.searched.append(source)
        try:
            found = providers[source](query)
            report.results.extend(found)
            if mode == 'auto' and any(convincing(item, title, artist) for item in found):
                break
        except BandcampChallenge as error:
            report.errors[source] = str(error)
            report.browser_query = query
            break
        except SearchError as error:
            report.errors[source] = str(error)
    seen = set()
    report.results = [r for r in report.results if not (r.url in seen or seen.add(r.url))]
    return report
