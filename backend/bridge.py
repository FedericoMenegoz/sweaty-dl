"""Development JSON bridge; deliberately imports no Qt UI modules."""
import contextlib
from dataclasses import asdict
import json
from pathlib import Path
import sys

try:
    from backend import diagnostics as diag
except ModuleNotFoundError:
    import diagnostics as diag

sys.path.insert(0, str(Path(__file__).resolve().parent))


def handle(payload):
    if payload.get('operation') != 'search':
        raise ValueError('Operazione non supportata')
    title = payload.get('title', '')
    artist = payload.get('artist', '')
    mode = payload.get('mode', 'auto')
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValueError('Titolo non valido')
    if not isinstance(artist, str) or len(artist) > 500:
        raise ValueError('Artista non valido')
    if mode not in ('auto', 'Bandcamp', 'SoundCloud', 'YouTube'):
        raise ValueError('Sorgente non valida')
    from provider_search import search_tracks
    # stdout is reserved for the JSON protocol, including dependency messages.
    diag.stage('search_providers', mode=mode)
    with contextlib.redirect_stdout(sys.stderr):
        report = search_tracks(title.strip(), artist.strip(), mode)
    return asdict(report)


if __name__ == '__main__':
    try:
        payload = json.loads(sys.stdin.read(16384))
        diag.configure(lambda record: print(json.dumps(record), file=sys.stderr, flush=True), payload.get('request_id', ''))
        diag.environment()
        result = handle(payload)
        diag.log('search_report', result_count=len(result.get('results', [])), provider_errors=result.get('errors', {}))
    except Exception as error:
        diag.exception(error)
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))
