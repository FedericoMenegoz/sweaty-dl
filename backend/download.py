"""Debug-only download worker. NDJSON on stdout; no Qt or old application imports."""
import contextlib
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse
try:
    from backend import diagnostics as diag
except ModuleNotFoundError:
    import diagnostics as diag
try:
    from backend.runtime_tools import tool
except ModuleNotFoundError:
    from runtime_tools import tool

OUTPUT = sys.stdout
CANCELLED = threading.Event()


class UserError(ValueError):
    pass


def emit(message_type, **values):
    print(json.dumps({'type': message_type, **values}, ensure_ascii=False), file=OUTPUT, flush=True)


def check_cancel():
    if CANCELLED.is_set():
        raise InterruptedError('Download annullato.')


def public_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise UserError('URL non valido.')
    url = urlparse(value)
    host = url.hostname or ''
    if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
        raise UserError('Usa un URL HTTPS senza credenziali.')
    if '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
        raise UserError('Indirizzo locale non consentito.')
    try:
        if not ipaddress.ip_address(host).is_global:
            raise UserError('Indirizzo locale non consentito.')
    except ValueError as error:
        # Hostnames are resolved and checked again at the socket boundary.
        if 'non consentito' in str(error):
            raise
    return url


@contextlib.contextmanager
def public_connections():
    """Check actual connect destinations, including DNS answers and redirects."""
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def checked(sock, address):
        check_cancel()
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            if not ipaddress.ip_address(address[0].split('%')[0]).is_global:
                raise UserError('Connessione a un indirizzo locale non consentita.')
        return original(sock, address)

    def checked_ex(sock, address):
        checked(sock, address)
        return 0

    socket.socket.connect = checked
    socket.socket.connect_ex = checked_ex
    try:
        yield
    finally:
        socket.socket.connect = original
        socket.socket.connect_ex = original_ex


def safe_name(value):
    from yt_dlp.utils import sanitize_filename
    name = sanitize_filename(str(value), restricted=False, is_id=False).strip('. ')
    return name.encode('utf-8')[:150].decode('utf-8', errors='ignore').strip('. ') or 'audio'


def validate_duration(actual, expected):
    if not isinstance(expected, (int, float)) or not math.isfinite(expected) or not 0 < expected <= 86400:
        raise UserError('Durata del brano non disponibile.')
    if not math.isfinite(actual) or abs(actual - expected) > max(.25, min(2.0, expected * .01)):
        raise UserError('La durata del file non corrisponde al brano: potrebbe essere un campione.')


def measured_duration(path):
    from mutagen import File
    try:
        audio = File(path)
        if audio and audio.info.length:
            return audio.info.length
    except Exception:
        pass
    # WebM is not supported by mutagen. Probe it if the existing tool is installed.
    probe = tool('ffprobe')
    if not probe:
        raise UserError('Serve ffprobe per verificare la durata di questo formato audio.')
    import subprocess
    result = subprocess.run([probe, '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'json', str(path)], capture_output=True, timeout=20, check=True)
    return float(json.loads(result.stdout)['format']['duration'])


def publish(path, folder, filename, expected):
    check_cancel()
    diag.stage('verify_duration', expected_seconds=expected, bytes=path.stat().st_size)
    actual = measured_duration(path)
    diag.log('audio_duration', actual_seconds=actual, expected_seconds=expected)
    validate_duration(actual, expected)
    diag.stage('publish_file', folder=str(folder), filename=filename)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    # Hard-link publication is atomic and cannot overwrite an existing file.
    # The temporary directory lives on the same volume as the destination.
    try:
        os.link(path, target)
    except FileExistsError:
        def digest(file):
            result = hashlib.sha256()
            with file.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    result.update(block)
            return result.digest()
        if digest(path) != digest(target):
            raise UserError('Il file esiste già nella destinazione; non è stato sovrascritto.') from None
    path.unlink()
    return str(target)


def bandcamp_stream(track, temporary, destination):
    import requests
    diag.stage('bandcamp_connect', track=diag.resource(track.get('track_url', '')))
    stream = track['stream_url']
    for _ in range(6):
        check_cancel()
        parsed = public_url(stream)
        if not parsed.hostname.endswith('.bcbits.com') or not parsed.path.startswith('/stream/'):
            raise UserError('Sorgente Bandcamp non consentita.')
        with requests.get(stream, stream=True, timeout=(15, 20), allow_redirects=False) as response:
            diag.log('bandcamp_http_response', http_status=response.status_code,
                     content_type=response.headers.get('Content-Type'), content_length=response.headers.get('Content-Length'))
            if response.is_redirect:
                from urllib.parse import urljoin
                stream = urljoin(stream, response.headers.get('Location', ''))
                continue
            response.raise_for_status()
            total = int(response.headers.get('Content-Length') or 0)
            if total > 1024 ** 3:
                raise UserError('File audio troppo grande.')
            diag.stage('bandcamp_transfer', expected_bytes=total)
            path = temporary / 'stream.mp3'
            done = 0
            last = 0
            with path.open('wb') as output:
                for chunk in response.iter_content(64 * 1024):
                    check_cancel()
                    done += len(chunk)
                    if done > 1024 ** 3:
                        raise UserError('File audio troppo grande.')
                    output.write(chunk)
                    if time.monotonic() - last > .15:
                        emit('progress', progress=min(.99, done / total) if total else None)
                        last = time.monotonic()
            if not done or (total and done != total):
                raise UserError('Il trasferimento del file è incompleto.')
            identity = hashlib.sha256(track['track_url'].encode()).hexdigest()[:12]
            name = f"{safe_name(track.get('title', 'audio'))} [{identity}].mp3"
            return publish(path, destination / 'Bandcamp', name, track['duration'])
    raise UserError('Troppi reindirizzamenti durante il download.')


class QuietLogger:
    def debug(self, _message):
        pass

    def warning(self, message):
        diag.log('extractor_warning', level='warn', message=message)

    def error(self, message):
        diag.log('extractor_error', level='error', message=message)


def extracted_audio(url, temporary, destination):
    from yt_dlp import YoutubeDL
    from yt_dlp.networking._urllib import UrllibRH, RedirectHandler
    public_url(url)
    last = 0

    def progress(info):
        nonlocal last
        check_cancel()
        if time.monotonic() - last < .15:
            return
        total = info.get('total_bytes') or info.get('total_bytes_estimate')
        done = info.get('downloaded_bytes') or 0
        if done > 1024 ** 3:
            raise UserError('File audio troppo grande.')
        emit('progress', progress=min(.99, done / total) if total else None)
        last = time.monotonic()

    options = {
        'format': 'bestaudio[format_id!*=preview][format_note!*=?preview]',
        'noplaylist': True, 'socket_timeout': 20, 'retries': 2, 'fragment_retries': 2,
        'extractor_retries': 1, 'max_filesize': 1024 ** 3, 'cachedir': False,
        'outtmpl': str(temporary / 'audio.%(ext)s'), 'windowsfilenames': True,
        'logger': QuietLogger(), 'progress_hooks': [progress], 'quiet': True,
        'noprogress': True, 'proxy': '', 'external_downloader': None,
        'postprocessors': [], 'fixup': 'never', 'hls_prefer_native': True,
    }
    node = tool('node')
    if node:
        options['js_runtimes'] = {'node': {'path': node}}
    original_redirect = RedirectHandler.redirect_request

    def redirect(handler, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return original_redirect(handler, req, fp, code, msg, headers, newurl)

    class Downloader(YoutubeDL):
        def urlopen(self, request):
            public_url(request if isinstance(request, str) else request.url)
            return super().urlopen(request)

    RedirectHandler.redirect_request = redirect
    try:
        return extract_with(Downloader, UrllibRH, options, url, temporary, destination)
    finally:
        RedirectHandler.redirect_request = original_redirect


def extract_with(Downloader, UrllibRH, options, url, temporary, destination):
    with Downloader(options) as downloader:
        # Keep one reviewed networking implementation; no impersonation handler.
        director = downloader._request_director
        for handler in list(director.handlers.values()):
            if not isinstance(handler, UrllibRH):
                director.handlers.pop(handler.RH_KEY, None)
                handler.close()
        diag.stage('extract_metadata', resource=diag.resource(url))
        info = downloader.extract_info(url, download=False)
        if not info or info.get('_type', 'video') != 'video':
            raise UserError('Inserisci il collegamento a un singolo brano o video, non una playlist.')
        if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
            raise UserError('Le dirette non sono supportate.')
        if info.get('has_drm') or info.get('availability') in ('premium_only', 'subscriber_only', 'needs_auth'):
            raise UserError('Questo contenuto richiede un accesso non disponibile.')
        expected = info.get('duration')
        validate_duration(expected, expected)
        if info.get('protocol') not in ('https', 'm3u8_native', 'http_dash_segments'):
            raise UserError('Questo formato non offre un trasferimento audio nativo supportato.')
        # Use exactly the resolved entry; never search for replacements.
        diag.stage('transfer_audio', extractor=info.get('extractor_key'), format_id=info.get('format_id'),
                   protocol=info.get('protocol'), duration=expected)
        downloader.process_info(info)
        files = [p for p in temporary.iterdir() if p.is_file() and p.suffix not in ('.part', '.ytdl', '.tmp')]
        if len(files) != 1:
            raise UserError('Il trasferimento audio non ha prodotto un file completo.')
        path = files[0]
        source = safe_name(info.get('extractor_key') or 'Audio')
        identity = hashlib.sha256(url.encode()).hexdigest()[:12]
        name = f"{safe_name(info.get('title', 'audio'))} [{identity}]{path.suffix}"
        return publish(path, destination / source, name, expected)


def run(payload):
    diag.stage('validate_input')
    url = payload['url']
    public_url(url)
    destination = Path(payload['destination']).expanduser()
    if not destination.is_absolute():
        raise UserError('La destinazione deve essere un percorso assoluto.')
    diag.stage('prepare_destination', destination=str(destination))
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    tracks = payload.get('tracks')
    if tracks is not None and (not isinstance(tracks, list) or not 0 < len(tracks) <= 500):
        raise UserError('Nessun audio Bandcamp disponibile.')
    with public_connections():
        # One directory per job, cleaned on errors and cooperative cancellation.
        with tempfile.TemporaryDirectory(prefix='audio-', dir=payload.get('temporary', destination)) as folder:
            temporary = Path(folder)
            if tracks is not None:
                for index, track in enumerate(tracks):
                    check_cancel()
                    emit('track', index=index + 1, total=len(tracks))
                    files.append(bandcamp_stream(track, temporary, destination))
            else:
                files.append(extracted_audio(url, temporary, destination))
    return files


def error_message(error):
    if isinstance(error, (UserError, InterruptedError)):
        return str(error)
    # Exceptions from HTTP/extractors can contain signed URLs: never forward them.
    if isinstance(error, ImportError):
        return 'Manca una dipendenza audio nell’ambiente Python di sviluppo.'
    return 'Download non riuscito: verifica rete, disponibilità del contenuto o verifica richiesta dal sito.'


if __name__ == '__main__':
    try:
        buffer = bytearray()
        while b'\n' not in buffer:
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk or len(buffer) + len(chunk) > 1024 * 1024:
                raise UserError('Richiesta del downloader non valida.')
            buffer.extend(chunk)
        line, _, remainder = buffer.partition(b'\n')
        payload = json.loads(line)
        diag.configure(lambda record: print(json.dumps(record, ensure_ascii=False), file=OUTPUT, flush=True), payload.get('request_id', ''))
        diag.environment()
        if remainder:
            CANCELLED.set()

        def control():
            # EOF also means the application has gone away.
            os.read(sys.stdin.fileno(), 1)
            CANCELLED.set()

        threading.Thread(target=control, daemon=True).start()
        with contextlib.redirect_stdout(sys.stderr):
            files = run(payload)
        diag.log('files_confirmed', count=len(files))
        emit('complete', files=files, kind='saved_stream_audio')
    except Exception as error:
        diag.exception(error)
        emit('error', message=error_message(error))
        sys.exit(1)
