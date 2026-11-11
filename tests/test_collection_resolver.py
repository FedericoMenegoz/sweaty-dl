"""Offline collection resolution: actual extractor processing and guarded fixtures."""
import contextlib
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import MagicMock, patch

from backend import collection_resolver as resolver
from backend.provider_urls import classify_media_url, provider_for_url


YOUTUBE_PLAYLIST = 'https://www.youtube.com/playlist?list=PLexample'
SOUNDCLOUD_PLAYLIST = 'https://soundcloud.com/artist/sets/example'
VIDEO_ONE = 'https://www.youtube.com/watch?v=abcdefghijk'
VIDEO_TWO = 'https://www.youtube.com/watch?v=lmnopqrstuv'


class UrlClassificationTests(unittest.TestCase):
    def test_shared_frontend_backend_url_matrix(self):
        fixtures = json.loads((Path(__file__).parent / 'fixtures/media-urls.json').read_text())
        for row in fixtures['valid']:
            with self.subTest(url=row['input']):
                self.assertEqual(classify_media_url(row['input']),
                                 {key: row[key] for key in ('source', 'kind', 'url')})
                self.assertEqual(provider_for_url(row['input']), row['source'])
        for value in fixtures['invalid']:
            with self.subTest(url=value):
                with self.assertRaises(ValueError):
                    classify_media_url(value)

    def test_only_catalog_paths_are_classified_and_sensitive_input_is_not_echoed(self):
        for value in [None, 12, '', 'https://youtube.com/watch?list=PLexample',
                      'https://youtu.be/abcdefghijk?list=PLone&list=PLtwo',
                      'https://youtube.com/watch?v=abcdefghijk&v=lmnopqrstuv',
                      'https://youtube.com/shorts/bad?list=PLexample',
                      'https://soundcloud.com/artist/comments',
                      'https://soundcloud.com/artist/track/secret',
                      'https://soundcloud.com/artist/sets/list/secret',
                      'https://on.soundcloud.com/', 'https://artist.bandcamp.com/track/%ZZ',
                      'https://youtube.com/watch?v=abcdefghijk\x00?token=confidential']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    classify_media_url(value)
                self.assertNotIn('confidential', str(raised.exception))

    def test_soundcloud_tracking_is_removed_but_identity_token_is_preserved(self):
        value = 'https://m.soundcloud.com/artist/track?si=tracking&in=artist/sets/list&secret_token=s-publiclink'
        self.assertEqual(classify_media_url(value)['url'],
                         'https://soundcloud.com/artist/track?secret_token=s-publiclink')
        with self.assertRaises(ValueError):
            classify_media_url(value + '&secret_token=s-other')


class ResolverTests(unittest.TestCase):
    @contextlib.contextmanager
    def fake_downloader(self, info, *, side_effect=None, incomplete=False):
        downloader = MagicMock()
        downloader.extract_info.return_value = info
        if side_effect is not None:
            downloader.extract_info.side_effect = side_effect
        logger = resolver._ResolverLogger()
        logger.incomplete = incomplete
        with patch.object(resolver, '_metadata_downloader', return_value=contextlib.nullcontext((downloader, logger))):
            yield downloader

    def test_order_metadata_and_known_unavailable_items_without_stream_leaks(self):
        info = {'_type': 'playlist', 'title': 'My playlist', 'entries': [
            {'id': 'abcdefghijk', 'url': VIDEO_ONE + '&list=PLother', 'title': 'One',
             'uploader': 'Artist', 'duration': 180, 'formats': [{'url': 'https://cdn.example/audio?token=secret'}]},
            None,
            {'url': VIDEO_TWO, 'title': '[Private video]'},
            {'url': VIDEO_TWO, 'title': 'Upcoming', 'live_status': 'is_upcoming'},
            {'url': 'lmnopqrstuv', 'title': 'Two', 'artist': 'Second', 'duration': float('nan')},
        ]}
        # A child's collection context must not change its individual identity.
        with self.fake_downloader(info) as downloader:
            result = resolver.resolve_media_url(YOUTUBE_PLAYLIST)
        self.assertTrue(result['complete'])
        self.assertEqual(result['unavailable_count'], 3)
        self.assertEqual([entry['title'] for entry in result['results']], ['One', 'Two'])
        self.assertEqual([entry['url'] for entry in result['results']], [VIDEO_ONE, VIDEO_TWO])
        self.assertEqual(result['results'][0]['artist'], 'Artist')
        self.assertEqual(result['results'][0]['duration'], 180)
        self.assertNotIn('duration', result['results'][1])
        self.assertNotIn('secret', json.dumps(result))
        self.assertTrue(result['warnings'])
        downloader.extract_info.assert_called_once_with(YOUTUBE_PLAYLIST, download=False)

    def test_atomic_failure_when_later_page_cannot_be_loaded(self):
        def entries():
            yield {'url': VIDEO_ONE, 'title': 'First'}
            raise RuntimeError('network failed https://cdn.example/?token=confidential')
        with self.fake_downloader({'_type': 'playlist', 'entries': entries()}):
            with self.assertRaises(resolver.UserError) as raised:
                resolver.resolve_media_url(YOUTUBE_PLAYLIST)
        self.assertNotIn('confidential', str(raised.exception))
        self.assertNotIn('https://', str(raised.exception))

    def test_unavailable_empty_and_incomplete_results_are_not_successes(self):
        for entries in [[], [None], [{'availability': 'private', 'url': VIDEO_ONE}],
                        [{'availability': 'premium_only', 'url': VIDEO_ONE}],
                        [{'has_drm': True, 'url': VIDEO_ONE}]]:
            with self.subTest(entries=entries), self.fake_downloader({'_type': 'playlist', 'entries': entries}):
                with self.assertRaises(resolver.UserError):
                    resolver.resolve_media_url(YOUTUBE_PLAYLIST)
        with self.fake_downloader({'_type': 'playlist', 'entries': [{'url': VIDEO_ONE}]}, incomplete=True):
            with self.assertRaisesRegex(resolver.UserError, 'incomplete'):
                resolver.resolve_media_url(YOUTUBE_PLAYLIST)

    def test_finite_limit_is_checked_during_iteration_and_never_silently_truncates(self):
        visited = []
        def entries():
            for number in range(1000):
                visited.append(number)
                yield {'url': VIDEO_ONE, 'title': 'Repeat'}
        with self.fake_downloader({'_type': 'playlist', 'entries': entries()}):
            with self.assertRaisesRegex(resolver.UserError, '500'):
                resolver.resolve_media_url(YOUTUBE_PLAYLIST)
        self.assertEqual(len(visited), 501)
        with self.fake_downloader({'_type': 'playlist', 'playlist_count': 501, 'entries': []}):
            with self.assertRaisesRegex(resolver.UserError, '500'):
                resolver.resolve_media_url(YOUTUBE_PLAYLIST)

    def test_wrong_source_nested_playlists_and_invalid_child_urls_fail(self):
        for entry in [
            {'url': 'https://soundcloud.com/artist/song', 'title': 'Wrong source'},
            {'url': 'https://127.0.0.1/audio', 'title': 'Local URL'},
            {'url': 'https://www.youtube.com.evil.example/watch?v=abcdefghijk'},
            {'_type': 'playlist', 'entries': []},
            {'url': YOUTUBE_PLAYLIST},
        ]:
            with self.subTest(entry=entry), self.fake_downloader({'_type': 'playlist', 'entries': [entry]}):
                with self.assertRaises(resolver.UserError):
                    resolver.resolve_media_url(YOUTUBE_PLAYLIST)

    def test_soundcloud_flat_catalog_and_numeric_api_entries_are_resolved(self):
        api_url = 'https://api-v2.soundcloud.com/tracks/123'
        info = {'_type': 'playlist', 'title': 'SC collection', 'entries': [
            {'_type': 'url_transparent', 'url': 'https://soundcloud.com/artist/first-song'},
            {'_type': 'url_transparent', 'url': api_url},
        ]}
        hydrated = {'_type': 'video', 'webpage_url': 'https://soundcloud.com/other/second-song',
                    'url': 'https://cdn.example/audio?token=confidential', 'title': 'Second Song',
                    'uploader': 'Other Artist', 'duration': 120}
        with self.fake_downloader(info, side_effect=[info, hydrated]) as downloader:
            result = resolver.resolve_media_url(SOUNDCLOUD_PLAYLIST)
        self.assertEqual([row['url'] for row in result['results']],
                         ['https://soundcloud.com/artist/first-song', 'https://soundcloud.com/other/second-song'])
        self.assertEqual(result['results'][0]['title'], 'first song')
        self.assertEqual(result['results'][1]['artist'], 'Other Artist')
        self.assertNotIn('confidential', json.dumps(result))
        self.assertEqual(downloader.extract_info.call_count, 2)
        downloader.extract_info.assert_called_with(api_url, download=False)

    def test_single_track_metadata_must_match_the_requested_catalog_identity(self):
        with self.fake_downloader({'webpage_url': VIDEO_ONE, 'title': 'One', 'duration': 3}):
            result = resolver.resolve_media_url('https://youtu.be/abcdefghijk?si=tracking')
        self.assertEqual(result['url'], VIDEO_ONE)
        self.assertEqual(result['results'][0]['url'], VIDEO_ONE)
        with self.fake_downloader({'webpage_url': VIDEO_TWO, 'title': 'Unexpected'}):
            with self.assertRaises(resolver.UserError):
                resolver.resolve_media_url(VIDEO_ONE)

    def test_real_soundcloud_hydration_counts_protected_children_without_aborting(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.extractor.common import InfoExtractor
        from yt_dlp.extractor.soundcloud import SoundcloudIE
        from yt_dlp.networking._urllib import UrllibRH
        from yt_dlp.utils import ExtractorError

        class PlaylistIE(InfoExtractor):
            _VALID_URL = SOUNDCLOUD_PLAYLIST

            def _real_extract(self, url):
                return self.playlist_result([
                    self.url_result(f'https://api-v2.soundcloud.com/tracks/{number}',
                                    ie=TrackIE.ie_key(), url_transparent=True)
                    for number in range(1, 4)
                ], 'playlist', 'Mixed availability')

        class TrackIE(SoundcloudIE):
            _VALID_URL = r'https://api-v2\.soundcloud\.com/tracks/(?P<id>[0-9]+)'

            def _initialize_pre_login(self):
                self._CLIENT_ID = 'fixture'

            def _real_extract(self, url):
                number = self._match_id(url)
                if number == '2':
                    # Use the real SoundCloud path that previously raised DRM
                    # before returning the catalog metadata.
                    return self._extract_info_dict({
                        'id': 2, 'title': 'Protected', 'permalink_url': 'https://soundcloud.com/artist/protected',
                        'media': {'transcodings': [{'url': 'https://cdn.example/protected',
                                                   'preset': 'aac_160k', 'format': {'protocol': 'ctr-hls'}}]},
                    })
                return {'id': number, 'title': f'Track {number}',
                        'webpage_url': f'https://soundcloud.com/artist/track-{number}',
                        'formats': [{'url': 'https://cdn.example/audio.wav', 'ext': 'wav'}]}

        def register(downloader):
            downloader.add_info_extractor(PlaylistIE())
            downloader.add_info_extractor(TrackIE())

        with patch.object(YoutubeDL, 'add_default_info_extractors', register), \
                patch.object(UrllibRH, '_send') as transfer:
            report = resolver.resolve_media_url(SOUNDCLOUD_PLAYLIST)
            self.assertTrue(report['complete'])
            self.assertEqual(report['unavailable_count'], 1)
            self.assertEqual([track['url'] for track in report['results']],
                             ['https://soundcloud.com/artist/track-1', 'https://soundcloud.com/artist/track-3'])
            self.assertTrue(report['warnings'])
            with patch.object(TrackIE, '_real_extract', side_effect=ExtractorError('Network failure', expected=True)):
                with self.assertRaises(resolver.UserError):
                    resolver.resolve_media_url(SOUNDCLOUD_PLAYLIST)
        transfer.assert_not_called()

    def test_bandcamp_album_remains_a_single_native_album_reference(self):
        with patch.object(resolver, '_metadata_downloader') as downloader:
            result = resolver.resolve_media_url('https://artist.bandcamp.com/album/my-album?from=search')
        self.assertEqual(result['kind'], 'album')
        self.assertEqual(result['results'][0]['kind'], 'album')
        self.assertEqual(result['results'][0]['url'], 'https://artist.bandcamp.com/album/my-album')
        downloader.assert_not_called()

    def test_short_soundcloud_links_are_followed_manually_without_reading_audio(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.is_redirect = True
        response.headers = {'Location': SOUNDCLOUD_PLAYLIST + '?si=tracking'}
        info = {'_type': 'playlist', 'entries': [{'url': 'https://soundcloud.com/artist/song'}]}
        with patch('requests.Session') as session_type, self.fake_downloader(info):
            session = session_type.return_value.__enter__.return_value
            session.get.return_value = response
            result = resolver.resolve_media_url('https://on.soundcloud.com/ShareId')
            self.assertEqual(result['url'], SOUNDCLOUD_PLAYLIST)
            session.get.assert_called_once_with('https://on.soundcloud.com/ShareId',
                                                allow_redirects=False, stream=True, timeout=(5, 10))
            self.assertFalse(session.trust_env)
            response.iter_content.assert_not_called()

    def test_short_link_redirect_to_local_or_other_provider_is_rejected_before_request(self):
        for target in ['https://127.0.0.1/audio', VIDEO_ONE, 'http://soundcloud.com/artist/song',
                       'https://evil.example/artist/song']:
            with self.subTest(target=target), patch('requests.Session') as session_type:
                session = session_type.return_value.__enter__.return_value
                response = session.get.return_value.__enter__.return_value
                response.is_redirect = True
                response.headers = {'Location': target}
                with self.assertRaises(resolver.UserError):
                    resolver.resolve_media_url('https://on.soundcloud.com/ShareId')
                self.assertEqual(session.get.call_count, 1)

    def test_short_link_redirect_loop_is_bounded(self):
        with patch('requests.Session') as session_type:
            session = session_type.return_value.__enter__.return_value
            response = session.get.return_value.__enter__.return_value
            response.is_redirect = True
            response.headers = {'Location': 'https://on.soundcloud.com/ShareId'}
            with self.assertRaisesRegex(resolver.UserError, 'Too many'):
                resolver.resolve_media_url('https://on.soundcloud.com/ShareId')
            self.assertEqual(session.get.call_count, 6)

    def test_resolver_socket_guard_checks_actual_destination(self):
        @contextlib.contextmanager
        def malicious_downloader():
            with socket.socket() as connection:
                connection.connect(('127.0.0.1', 80))
            yield
        with patch.object(resolver, '_metadata_downloader', malicious_downloader):
            with self.assertRaisesRegex(resolver.UserError, 'local'):
                resolver.resolve_media_url(YOUTUBE_PLAYLIST)

    def test_real_ytdlp_enumerates_flat_fixture_without_media_requests(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.extractor.common import InfoExtractor
        from yt_dlp.networking._urllib import UrllibRH

        class PlaylistIE(InfoExtractor):
            _VALID_URL = r'https://www\.youtube\.com/playlist\?list=(?P<id>PLexample)'
            def _real_extract(self, url):
                return self.playlist_result([
                    self.url_result(VIDEO_ONE, video_id='abcdefghijk', video_title='First'),
                    self.url_result(VIDEO_TWO, video_id='lmnopqrstuv', video_title='Second'),
                ], 'PLexample', 'Fixture Playlist')

        def register(downloader):
            downloader.add_info_extractor(PlaylistIE())

        with patch.object(YoutubeDL, 'add_default_info_extractors', register), patch.object(UrllibRH, '_send') as transfer:
            result = resolver.resolve_media_url(YOUTUBE_PLAYLIST)
        transfer.assert_not_called()
        self.assertEqual(result['title'], 'Fixture Playlist')
        self.assertEqual([entry['url'] for entry in result['results']], [VIDEO_ONE, VIDEO_TWO])
        self.assertEqual([entry['title'] for entry in result['results']], ['First', 'Second'])

    def test_ytdlp_http_redirect_guard_rejects_insecure_target(self):
        from yt_dlp.networking._urllib import RedirectHandler
        from urllib.request import Request
        original = RedirectHandler.redirect_request
        with resolver._metadata_downloader():
            with self.assertRaises(resolver.UserError):
                RedirectHandler().redirect_request(Request(VIDEO_ONE), io.BytesIO(), 302, 'Found', {},
                                                   'http://example.com/metadata')
        self.assertIs(RedirectHandler.redirect_request, original)

    def test_real_youtube_incomplete_continuation_fails_instead_of_returning_first_page(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.extractor.youtube import YoutubeTabIE
        from yt_dlp.networking._urllib import UrllibRH

        class IncompletePageIE(YoutubeTabIE):
            _VALID_URL = r'https://www\.youtube\.com/playlist\?list=(?P<id>PLexample)'

            def _call_api(self, *args, **kwargs):
                return {}  # HTTP succeeded, but YouTube omitted continuation data.

            def _real_extract(self, url):
                def pages():
                    yield self.url_result(VIDEO_ONE, video_id='abcdefghijk', video_title='First')
                    self._extract_response('page 2', {}, check_get_keys='contents')
                return self.playlist_result(pages(), 'PLexample', 'Incomplete fixture')

        def register(downloader):
            downloader.add_info_extractor(IncompletePageIE())

        with patch.object(YoutubeDL, 'add_default_info_extractors', register), patch.object(UrllibRH, '_send') as transfer:
            with self.assertRaises(resolver.UserError):
                resolver.resolve_media_url(YOUTUBE_PLAYLIST)
        transfer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
