"""Offline regressions for guarded provider networking and local-only probing."""
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'backend'))
import download
import provider_search


def response(status=200, body=b'', location=None):
    import requests
    result = requests.Response()
    result.status_code = status
    result._content = body
    result._content_consumed = True
    if location:
        result.headers['Location'] = location
    return result


class NetworkSecurityTests(unittest.TestCase):
    def setUp(self):
        download.CANCELLED.clear()

    def test_bandcamp_uses_no_implicit_credentials_and_reads_recognized_empty_results(self):
        def send(session, request, **kwargs):
            self.assertFalse(session.trust_env)
            self.assertNotIn('Authorization', request.headers)
            self.assertFalse(kwargs.get('proxies'))
            self.assertIn('q=Fixture', request.url)
            return response(body=b'<html><div class="no-results"></div></html>')
        with patch('requests.Session.send', send), patch('requests.sessions.get_netrc_auth', side_effect=AssertionError('must not read netrc')):
            self.assertEqual(provider_search.bandcamp_search('Fixture'), [])

    def test_bandcamp_rejects_unsafe_redirect_before_second_request(self):
        for target in ('http://example.com/x', 'https://127.0.0.1/x',
                       'https://[::1]/x', 'https://name.internal/x',
                       'https://user:pass@example.com/x', 'file:///fixture',
                       'https://example.com:invalid/x'):
            with self.subTest(target=target), patch('requests.Session.send', return_value=response(302, location=target)) as send:
                with self.assertRaises(provider_search.SearchError):
                    provider_search.bandcamp_search('Fixture')
                self.assertEqual(send.call_count, 1)

    def test_guarded_search_preserves_fallback_order_and_version_matching(self):
        from yt_dlp import YoutubeDL
        for soundcloud_title, expected in (
                ('Fixture live', ['Bandcamp', 'SoundCloud', 'YouTube']),
                ('Fixture', ['Bandcamp', 'SoundCloud'])):
            calls = []

            def extract(downloader, query, **kwargs):
                source = 'SoundCloud' if query.startswith('scsearch8:') else 'YouTube'
                calls.append(source)
                entry = {'title': soundcloud_title if source == 'SoundCloud' else 'Fixture',
                         'artist': 'Artist', 'url': 'https://soundcloud.com/artist/fixture'
                         if source == 'SoundCloud' else 'https://www.youtube.com/watch?v=abcdefghijk'}
                return {'entries': iter([entry])}

            with self.subTest(title=soundcloud_title), patch(
                    'requests.Session.send', return_value=response(
                        body=b'<html><div class="no-results"></div></html>')), patch.object(
                            YoutubeDL, 'extract_info', extract):
                report = provider_search.search_tracks('Fixture', artist='Artist')
            self.assertEqual(report.searched, expected)
            self.assertEqual(calls, expected[1:])
            self.assertFalse(report.errors)
            self.assertEqual([item.source for item in report.results], expected[1:])

    def test_bandcamp_dns_result_cannot_connect_to_private_address(self):
        def send(*args, **kwargs):
            with socket.socket() as connection:
                connection.connect(('127.0.0.1', 443))
        with patch('requests.Session.send', send), patch('socket.socket.connect') as actual:
            with self.assertRaises(provider_search.SearchError):
                provider_search.bandcamp_search('Fixture')
            actual.assert_not_called()

    def test_bandcamp_response_is_bounded_and_challenge_still_pauses_fallback(self):
        oversized = MagicMock()
        oversized.__enter__.return_value = oversized
        oversized.is_redirect = False
        oversized.iter_content.return_value = [b'x' * (64 * 1024)] * 65
        with patch('requests.Session.get', return_value=oversized):
            with self.assertRaisesRegex(provider_search.SearchError, 'too large'):
                provider_search.bandcamp_search('Fixture')
        challenge = response(body=b'<title>Client Challenge</title>')
        with patch('requests.Session.send', return_value=challenge), patch.object(provider_search, 'ytdlp_search') as fallback:
            report = provider_search.search_tracks('Fixture')
            self.assertEqual(report.searched, ['Bandcamp'])
            self.assertEqual(report.browser_query, 'Fixture')
            fallback.assert_not_called()

    def test_search_network_director_keeps_only_guarded_urllib(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.networking._urllib import UrllibRH
        def extract(downloader, *args, **kwargs):
            self.assertTrue(downloader._request_director.handlers)
            self.assertTrue(all(isinstance(handler, UrllibRH) for handler in downloader._request_director.handlers.values()))
            self.assertEqual(downloader.params['proxy'], '')
            self.assertFalse(downloader.params['remote_components'])
            # A direct connection is rejected by the guard without making a socket request.
            with socket.socket() as connection:
                connection.connect(('127.0.0.1', 443))
        with patch.object(YoutubeDL, 'extract_info', extract), patch('socket.socket.connect') as actual:
            for source in ('SoundCloud', 'YouTube'):
                with self.assertRaises(provider_search.SearchError):
                    provider_search.ytdlp_search(source, 'Fixture')
            actual.assert_not_called()

    def test_guard_checks_lazy_search_entries_before_closing(self):
        from yt_dlp import YoutubeDL
        def entries():
            with socket.socket() as connection:
                connection.connect(('127.0.0.1', 443))
            yield {'title': 'Fixture'}
        with patch.object(YoutubeDL, 'extract_info', return_value={'entries': entries()}), patch('socket.socket.connect') as actual:
            with self.assertRaises(provider_search.SearchError):
                provider_search.ytdlp_search('YouTube', 'Fixture')
            actual.assert_not_called()

    def test_bandcamp_audio_disables_environment_credentials(self):
        with download._public_session() as session:
            self.assertFalse(session.trust_env)

    def test_ffprobe_receives_local_only_protocol_allowlist(self):
        completed = MagicMock(stdout=b'{"format":{"duration":"2.0"}}')
        with patch('mutagen.File', return_value=None), patch.object(download, 'tool', return_value='/fixture/ffprobe'), patch('subprocess.run', return_value=completed) as run:
            self.assertEqual(download.measured_duration(Path('/fixture/audio.webm')), 2)
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index('-protocol_whitelist') + 1], 'file')
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertEqual(run.call_args.kwargs['timeout'], 20)


if __name__ == '__main__':
    unittest.main()
