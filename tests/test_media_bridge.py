"""Metadata resolution uses the existing worker without text-search validation."""
import contextlib
import io
import unittest
from unittest.mock import patch

from backend import bridge


class MediaBridgeTests(unittest.TestCase):
    def test_resolution_dispatch_bypasses_title_and_source_fields(self):
        report = {'source': 'YouTube', 'kind': 'playlist', 'results': [], 'complete': True}
        url = 'https://www.youtube.com/playlist?list=example'
        with patch('collection_resolver.resolve_media_url', return_value=report) as resolve:
            self.assertEqual(bridge.handle({'operation': 'resolve_media_url', 'url': url,
                                            'artist': None, 'mode': 'invalid'}), report)
        resolve.assert_called_once_with(url)

    def test_resolution_keeps_dependency_output_off_protocol_stdout(self):
        def resolve(_):
            print('dependency diagnostic')
            return {'results': [], 'complete': True}

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with patch('collection_resolver.resolve_media_url', side_effect=resolve):
                bridge.handle({'operation': 'resolve_media_url', 'url': 'https://youtu.be/example'})
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('dependency diagnostic', stderr.getvalue())

    def test_expected_errors_are_sanitized_and_returned(self):
        from collection_resolver import UserError
        with patch('collection_resolver.resolve_media_url',
                   side_effect=UserError('Playlist unavailable https://cdn.example/audio?token=SECRET')):
            report = bridge.handle({'operation': 'resolve_media_url'})
        self.assertIn('Playlist unavailable', report['error'])
        self.assertNotIn('SECRET', report['error'])

    def test_unexpected_exceptions_are_not_returned_as_user_messages(self):
        with patch('collection_resolver.resolve_media_url', side_effect=RuntimeError('secret')):
            with self.assertRaises(RuntimeError):
                bridge.handle({'operation': 'resolve_media_url'})

    def test_search_still_validates_text_contract(self):
        for request in ({'operation': 'search', 'title': ''},
                        {'operation': 'search', 'title': 'x' * 501},
                        {'operation': 'search', 'title': 'Song', 'mode': 'invalid'},
                        {'operation': 'unknown'}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                bridge.handle(request)


if __name__ == '__main__':
    unittest.main()
