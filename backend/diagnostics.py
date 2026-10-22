"""Structured worker diagnostics; never serialize locals, headers, or media URLs."""
import datetime
import os
from pathlib import Path
import re
import sys
import traceback
from urllib.parse import urlparse

SINK = None
REQUEST = ''
PHASE = 'bootstrap'
SECRET_KEYS = re.compile(r'cookie|authorization|password|token|secret|signature|api.?key|credential|stream.?url|headers|payload|body', re.I)
URL = re.compile(r'''(?:https?|wss?)://[^\s"'<>\]\)]+''', re.I)
AUTH = re.compile(r'''\b(?:authorization|cookie|set-cookie)["']?\s*[:=][^\r\n]*''', re.I | re.M)
MEDIA = re.compile(r'''/(?:stream|videoplayback|download)/[^\s"'<>\]\)]*''', re.I)
SECRETS = re.compile(r'''\b(?:authorization|cookie|password|token|secret|signature|api[_-]?key|credential|enc)\b["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''', re.I)
QUERY = re.compile(r'''\?[^\s"'<>\]\)]*''')


def clean_text(value):
    def host(match):
        try:
            return f'[url:{urlparse(match[0]).hostname or "invalid"}]'
        except ValueError:
            return '[url]'
    text = URL.sub(host, str(value))
    text = AUTH.sub('[auth-redacted]', text)
    text = MEDIA.sub('/[media-redacted]', text)
    text = SECRETS.sub('[secret-redacted]', text)
    text = QUERY.sub('?[redacted]', text)
    for key in ('HOME', 'USERPROFILE'):
        if os.environ.get(key):
            text = text.replace(os.environ[key], '~')
    return ''.join(c for c in text if ord(c) >= 32 or c in '\n\t')[:4096]


def sanitize(value, depth=0):
    if depth > 6:
        return '[depth-limit]'
    if isinstance(value, dict):
        return {clean_text(k): '[redacted]' if SECRET_KEYS.search(str(k)) else sanitize(v, depth + 1)
                for k, v in list(value.items())[:40]}
    if isinstance(value, (tuple, list)):
        return [sanitize(v, depth + 1) for v in value[:40]]
    if isinstance(value, str):
        return clean_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return clean_text(type(value).__name__)


def configure(sink, request=''):
    global SINK, REQUEST
    SINK, REQUEST = sink, str(request)[:100]


def log(event, level='info', **details):
    if SINK is None:
        return
    record = {'type': 'log', 'level': level, 'event': event, 'request_id': REQUEST,
              'worker_time': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds'),
              'details': sanitize({'phase': PHASE, **details})}
    try:
        SINK(record)
    except Exception:
        # Logging must not turn an otherwise valid operation into a failure.
        pass


def stage(name, **details):
    global PHASE
    PHASE = name
    log('stage', **details)


def resource(value):
    try:
        url = urlparse(value)
        host = url.hostname or ''
        catalog = host.endswith('.bandcamp.com') and url.path.startswith(('/track/', '/album/'))
        catalog |= host in ('soundcloud.com', 'www.soundcloud.com', 'youtube.com', 'www.youtube.com', 'youtu.be')
        return {'host': host, 'path': clean_text(url.path) if catalog else '[omitted]'}
    except ValueError:
        return {'valid': False}


def exception(error):
    causes, seen, pending = [], set(), [error]
    while pending and len(causes) < 8:
        current = pending.pop(0)
        if id(current) in seen or not isinstance(current, BaseException):
            continue
        seen.add(id(current))
        status = getattr(getattr(current, 'response', None), 'status_code', None)
        frames = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                  for frame in traceback.extract_tb(current.__traceback__)[-16:]]
        causes.append({'exception_type': f'{type(current).__module__}.{type(current).__name__}',
                       'message': clean_text(current), 'errno': getattr(current, 'errno', None),
                       'http_status': status, 'frames': frames})
        pending.extend([current.__cause__, current.__context__, getattr(current, 'reason', None)])
    log('exception', level='error', causes=causes)


def environment():
    from importlib.metadata import version, PackageNotFoundError
    packages = {}
    for name in ('requests', 'mutagen', 'yt-dlp'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = 'missing'
    log('worker_environment', python=sys.version.split()[0], packages=packages,
        interpreter=Path(sys.executable).name)
