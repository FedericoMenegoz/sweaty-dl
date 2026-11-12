"""Real yt-dlp dispatch, generated HLS metadata, no subprocesses or network."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[1] / 'backend'))
import download
from yt_dlp import YoutubeDL
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.downloader.external import FFmpegFD
from tests.test_download import audio_bytes


class ExternalTransferSecurityTests(unittest.TestCase):
    def test_supported_native_hls_preserves_audio_without_discovering_external_tools(self):
        from yt_dlp.networking._urllib import UrllibRH
        from yt_dlp.networking.common import Response
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

        class NativeFixtureIE(InfoExtractor):
            _VALID_URL = r'https://fixture\.example/(?P<id>song)'

            def _real_extract(self, url):
                return {'id': 'song', 'title': 'Fixture', 'duration': 2,
                        'hls_media_playlist_data': '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nhttps://cdn.example/segment.wav\n#EXT-X-ENDLIST\n',
                        'formats': [{'format_id': 'audio', 'url': 'https://cdn.example/manifest.m3u8',
                                     'ext': 'wav', 'acodec': 'pcm', 'vcodec': 'none', 'protocol': 'm3u8_native'}]}

        def register(downloader):
            downloader.add_info_extractor(NativeFixtureIE())

        content = audio_bytes()

        def response(handler, request):
            self.assertEqual(request.url, 'https://cdn.example/segment.wav')
            return Response(io.BytesIO(content), request.url,
                            {'Content-Length': str(len(content)), 'Content-Type': 'audio/wav'}, status=200)

        download.CANCELLED.clear()
        original_location = FFmpegPostProcessor._ffmpeg_location.get()
        with tempfile.TemporaryDirectory() as folder, patch.object(
                YoutubeDL, 'add_default_info_extractors', register), patch.object(
                    UrllibRH, '_send', response), patch('yt_dlp.utils.Popen.__init__',
                        side_effect=AssertionError('yt-dlp must not discover external tools')) as spawn:
            files = download.run({'url': 'https://fixture.example/song', 'destination': folder})
            spawn.assert_not_called()
            self.assertEqual(FFmpegPostProcessor._ffmpeg_location.get(), original_location)
            self.assertEqual(len(files), 1)
            self.assertEqual(Path(files[0]).read_bytes(), content)

    def test_hls_cannot_delegate_past_python_network_guard(self):
        class FixtureIE(InfoExtractor):
            _VALID_URL = r'https://fixture\.example/(?P<id>song)'
            def _real_extract(self, url):
                return {'id': 'song', 'title': 'Fixture', 'duration': 2,
                        'hls_media_playlist_data': '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="https://127.0.0.1/key"\n#EXTINF:2,\nhttps://127.0.0.1/audio\n#EXT-X-ENDLIST\n',
                        'formats': [{'format_id': 'audio', 'url': 'https://cdn.example/manifest.m3u8',
                                     'ext': 'm4a', 'acodec': 'aac', 'vcodec': 'none', 'protocol': 'm3u8_native'}]}
        def register(downloader):
            downloader.add_info_extractor(FixtureIE())
        download.CANCELLED.clear()
        with tempfile.TemporaryDirectory() as folder, patch.object(YoutubeDL, 'add_default_info_extractors', register), patch.object(FFmpegFD, '_call_downloader') as external, patch('subprocess.Popen') as spawn, patch('yt_dlp.utils.Popen.__init__', side_effect=AssertionError('yt-dlp must not discover external tools')) as yt_spawn:
            original = FFmpegFD.real_download
            with self.assertRaisesRegex(download.UserError, 'external transfer'):
                download.run({'url': 'https://fixture.example/song', 'destination': folder})
            external.assert_not_called()
            spawn.assert_not_called()
            yt_spawn.assert_not_called()
            self.assertIs(FFmpegFD.real_download, original)
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
