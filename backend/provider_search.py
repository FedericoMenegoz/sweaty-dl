"""Provider searches and conservative, version-aware fallback matching."""
from dataclasses import dataclass, field
import re
import unicodedata
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


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
        raise BandcampChallenge('Bandcamp richiede una verifica nel browser.')
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
        raise SearchError('Pagina Bandcamp non riconosciuta; riprova o apri il sito.')
    return results


def bandcamp_search(query):
    try:
        response = requests.get('https://bandcamp.com/search',
                                params={'q': query, 'item_type': 't'},
                                headers={'User-Agent': 'SweatyDL/0.4'}, timeout=(8, 20))
        response.raise_for_status()
        return parse_bandcamp(response.text)
    except requests.RequestException as error:
        raise SearchError(f'Bandcamp non raggiungibile: {error}') from error


def ytdlp_search(source, query):
    try:
        from yt_dlp import YoutubeDL
    except ImportError as error:
        raise SearchError('Componente yt-dlp non disponibile in questa installazione.') from error
    prefix = {'SoundCloud': 'scsearch8', 'YouTube': 'ytsearch8'}[source]
    try:
        from runtime_tools import javascript_options
        with YoutubeDL({'quiet': True, 'no_warnings': True, 'extract_flat': True,
                        'socket_timeout': 15, 'retries': 1, 'extractor_retries': 1,
                        'skip_download': True, **javascript_options()}) as downloader:
            info = downloader.extract_info(f'{prefix}:{query}', download=False)
        if not info:
            raise SearchError(f'{source}: risposta vuota dal servizio.')
        results = []
        for entry in info.get('entries') or []:
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
            results.append(Result(entry.get('title') or 'Senza titolo',
                                  entry.get('artist') or entry.get('uploader') or entry.get('channel') or '',
                                  url, source, artwork=entry.get('thumbnail') or
                                  (thumbnails[-1].get('url', '') if thumbnails else ''),
                                  duration=entry.get('duration')))
        return results
    except SearchError:
        raise
    except Exception as error:
        raise SearchError(f'{source}: {error}') from error


def search_tracks(title, artist='', mode='auto', notify=lambda _: None, providers=None):
    title, artist = title.strip(), artist.strip()
    if not title:
        raise ValueError('Inserisci il titolo del brano.')
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
        notify(f'Ricerca su {source}…')
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
