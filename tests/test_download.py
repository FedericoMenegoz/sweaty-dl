"""Offline tests: actual temporary files and publication, simulated network/extractors."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

spec = importlib.util.spec_from_file_location('download_worker', Path(__file__).parents[1] / 'backend/download.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def audio_bytes(duration=2):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\0\0' * int(8000 * duration))
    return output.getvalue()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        worker.CANCELLED.clear()

    def test_file_is_measured_and_published_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            temporary = root / 'partial.wav'
            temporary.write_bytes(audio_bytes())
            final = Path(worker.publish(temporary, root / 'destination', 'audio.wav', 2))
            self.assertFalse(temporary.exists())
            self.assertTrue(final.is_file())
            # An identical previous result may be resumed; changed content cannot overwrite it.
            temporary.write_bytes(audio_bytes())
            worker.publish(temporary, final.parent, final.name, 2)
            temporary.write_bytes(audio_bytes(2.01))
            with self.assertRaisesRegex(ValueError, 'esiste già'):
                worker.publish(temporary, final.parent, final.name, 2)

    def test_truncated_sample_is_never_published(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            temporary = root / 'partial.wav'
            temporary.write_bytes(audio_bytes())
            with self.assertRaisesRegex(ValueError, 'campione'):
                worker.publish(temporary, root / 'destination', 'audio.wav', 240)
            self.assertFalse((root / 'destination/audio.wav').exists())

    def test_redirect_to_other_host_is_rejected_before_request(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.is_redirect = True
        response.headers = {'Location': 'https://evil.example/stream/file'}
        track = {'stream_url': 'https://t4.bcbits.com/stream/file'}
        with patch('requests.get', return_value=response) as request:
            with self.assertRaisesRegex(ValueError, 'non consentita'):
                worker.bandcamp_stream(track, Path('/tmp'), Path('/tmp'))
            self.assertEqual(request.call_count, 1)

    def test_actual_ytdlp_download_pipeline_with_fixture_extractor_and_response(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.extractor.common import InfoExtractor
        from yt_dlp.networking._urllib import UrllibRH
        from yt_dlp.networking.common import Response

        content = audio_bytes()

        class FixtureIE(InfoExtractor):
            _VALID_URL = r'https://fixture\.example/(?P<id>song)'

            def _real_extract(self, url):
                return {'id': 'song', 'title': 'Fixture Song', 'duration': 2,
                        'formats': [{'format_id': 'audio', 'url': 'https://cdn.example/song.wav',
                                     'ext': 'wav', 'acodec': 'pcm', 'vcodec': 'none', 'protocol': 'https'}]}

        def register(downloader):
            downloader.add_info_extractor(FixtureIE())

        def response(handler, request):
            return Response(io.BytesIO(content), request.url,
                            {'Content-Length': str(len(content)), 'Content-Type': 'audio/wav'}, status=200)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(YoutubeDL, 'add_default_info_extractors', register), patch.object(UrllibRH, '_send', response):
                files = worker.run({'url': 'https://fixture.example/song', 'destination': folder})
            self.assertEqual(len(files), 1)
            self.assertEqual(Path(files[0]).read_bytes(), content)
            self.assertEqual(len(list(root.glob('.sweatydl-*'))), 0)

    def test_ytdlp_preview_formats_are_rejected_before_download(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.extractor.common import InfoExtractor
        from yt_dlp.networking._urllib import UrllibRH
        from yt_dlp.utils import DownloadError

        class PreviewIE(InfoExtractor):
            _VALID_URL = r'https://fixture\.example/(?P<id>song)'
            def _real_extract(self, url):
                return {'id': 'song', 'title': 'Preview', 'duration': 240,
                        'formats': [{'format_id': 'hls_preview', 'url': 'https://cdn.example/preview.mp3',
                                     'ext': 'mp3', 'acodec': 'mp3', 'vcodec': 'none'}]}

        def register(downloader):
            downloader.add_info_extractor(PreviewIE())

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(YoutubeDL, 'add_default_info_extractors', register), patch.object(UrllibRH, '_send') as transfer:
                with self.assertRaises(DownloadError):
                    worker.run({'url': 'https://fixture.example/song', 'destination': folder})
                transfer.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_worker_temporary_directory_is_cleaned_after_error(self):
        with tempfile.TemporaryDirectory() as folder:
            def failed(url, temporary, destination):
                (temporary / 'partial.part').write_bytes(b'incomplete')
                raise ValueError('Network error')
            with patch.object(worker, 'extracted_audio', side_effect=failed):
                with self.assertRaises(ValueError):
                    worker.run({'url': 'https://example.com/song', 'destination': folder})
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_worker_process_reports_a_successful_saved_file_without_kind_collision(self):
        import subprocess
        import sys
        script = r"""
import runpy
from unittest.mock import patch, MagicMock
response = MagicMock()
response.__enter__.return_value = response
response.is_redirect = False
response.status_code = 200
from tests.test_download import audio_bytes
content = audio_bytes()
response.headers = {'Content-Length': str(len(content)), 'Content-Type': 'audio/wav'}
response.iter_content.return_value = [content]
with patch('requests.get', return_value=response):
    runpy.run_path('backend/download.py', run_name='__main__')
"""
        with tempfile.TemporaryDirectory() as folder:
            payload = {'request_id': 'success-fixture', 'url': 'https://artist.bandcamp.com/track/song',
                       'destination': folder, 'tracks': [{'stream_url': 'https://t4.bcbits.com/stream/fixture',
                         'track_url': 'https://artist.bandcamp.com/track/song', 'title': 'Fixture', 'duration': 2}]}
            process = subprocess.Popen([sys.executable, '-B', '-c', script],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                process.stdin.write(json.dumps(payload).encode() + b'\n')
                process.stdin.flush()
                process.wait(timeout=10)
                records = [json.loads(line) for line in process.stdout.read().splitlines()]
                self.assertEqual(process.returncode, 0, process.stderr.read().decode())
                complete = records[-1]
                self.assertEqual(complete['type'], 'complete')
                self.assertEqual(complete['kind'], 'saved_stream_audio')
                self.assertEqual(Path(complete['files'][0]).read_bytes(), audio_bytes())
                self.assertTrue(any(r.get('event') == 'files_confirmed' for r in records))
                self.assertFalse(any(r['type'] == 'error' for r in records))
            finally:
                process.kill() if process.poll() is None else None
                process.wait()
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()

    def test_worker_process_returns_clean_ndjson_and_exits_with_control_pipe_open(self):
        import subprocess
        import sys
        process = subprocess.Popen([sys.executable, '-B', str(Path(worker.__file__))],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.stdin.write(json.dumps({'url': 'https://127.0.0.1/audio', 'destination': '/tmp'}).encode() + b'\n')
            process.stdin.flush()
            process.wait(timeout=5)
            records = [json.loads(line) for line in process.stdout.read().splitlines()]
            message = records[-1]
            self.assertEqual(message['type'], 'error')
            cause = next(record for record in records if record.get('event') == 'exception')
            self.assertEqual(cause['details']['phase'], 'validate_input')
            self.assertTrue(cause['details']['causes'][0]['exception_type'].endswith('UserError'))
            self.assertTrue(cause['details']['causes'][0]['frames'])
            self.assertEqual(process.returncode, 1)
            self.assertEqual(process.stderr.read(), b'')
        finally:
            process.kill() if process.poll() is None else None
            process.wait()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

    def test_cancellation_prevents_publication(self):
        worker.CANCELLED.set()
        with self.assertRaises(InterruptedError):
            worker.publish(Path('/tmp/nonexistent'), Path('/tmp'), 'file.mp3', 2)

    def test_errors_never_reveal_signed_urls(self):
        self.assertNotIn('secret', worker.error_message(RuntimeError('https://cdn.example/file?token=secret')))
        import requests
        self.assertNotIn('secret', worker.error_message(requests.exceptions.InvalidURL('https://cdn.example/file?token=secret')))
        for url in ['http://example.com/x', 'https://127.0.0.1/x', 'https://a.local/x', 'https://a:b@example.com/x']:
            with self.assertRaises(ValueError):
                worker.public_url(url)

    def test_network_guard_refuses_private_redirect_destinations(self):
        import socket
        with worker.public_connections():
            with socket.socket() as connection:
                with self.assertRaisesRegex(ValueError, 'locale'):
                    connection.connect(('127.0.0.1', 80))

    def test_durations_require_finite_complete_audio(self):
        for actual, expected in [(10, 240), (float('nan'), 10), (2, None), (2, -1), (2, float('inf'))]:
            with self.assertRaises(ValueError):
                worker.validate_duration(actual, expected)


if __name__ == '__main__':
    unittest.main()
