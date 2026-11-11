"""Exercise playlist expansion and real yt-dlp downloads together, without network."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from backend import collection_resolver, download
from yt_dlp import YoutubeDL
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.networking._urllib import UrllibRH
from yt_dlp.networking.common import Response


class PlaylistDownloadFlowTests(unittest.TestCase):
    def test_playlist_children_download_individually_without_reexpansion(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b'\0\0' * 16000)
        content = buffer.getvalue()
        ids = ['abcdefghijk', 'lmnopqrstuv']
        extracted, transferred = [], []

        class PlaylistFixtureIE(InfoExtractor):
            _VALID_URL = r'https://www\.youtube\.com/(?:playlist\?list=PLfixture|watch\?v=[A-Za-z0-9_-]{11})$'

            def _real_extract(self, url):
                extracted.append(url)
                if '/playlist?' in url:
                    return self.playlist_result([
                        self.url_result(f'https://www.youtube.com/watch?v={video_id}',
                                        ie=self.ie_key(), video_id=video_id,
                                        video_title=f'Fixture {index}')
                        for index, video_id in enumerate(ids)
                    ], 'PLfixture', 'Fixture playlist')
                video_id = url.split('v=')[1]
                return {'id': video_id, 'title': f'Fixture {ids.index(video_id)}', 'duration': 2,
                        'formats': [{'format_id': 'audio', 'url': f'https://cdn.example/{video_id}.wav',
                                     'ext': 'wav', 'acodec': 'pcm', 'vcodec': 'none', 'protocol': 'https'}]}

        def register(downloader):
            downloader.add_info_extractor(PlaylistFixtureIE())

        def response(handler, request):
            transferred.append(request.url)
            return Response(io.BytesIO(content), request.url,
                            {'Content-Length': str(len(content)), 'Content-Type': 'audio/wav'}, status=200)

        download.CANCELLED.clear()
        with patch.object(YoutubeDL, 'add_default_info_extractors', register), \
                patch.object(UrllibRH, '_send', response), \
                patch.object(download, 'emit'), tempfile.TemporaryDirectory() as directory:
            report = collection_resolver.resolve_media_url('https://www.youtube.com/playlist?list=PLfixture')
            self.assertTrue(report['complete'])
            self.assertEqual([item['url'] for item in report['results']],
                             [f'https://www.youtube.com/watch?v={video_id}' for video_id in ids])
            self.assertEqual(transferred, [], 'Metadata import must not transfer audio')
            files = []
            for track in report['results']:
                files.extend(download.run({'url': track['url'], 'destination': directory}))
            self.assertEqual(len(files), 2)
            self.assertEqual(len(transferred), 2)
            self.assertTrue(all(Path(file).read_bytes() == content for file in files))
            self.assertEqual(sum('/playlist?' in url for url in extracted), 1,
                             'Individual downloads must not re-expand the collection')


if __name__ == '__main__':
    unittest.main()
