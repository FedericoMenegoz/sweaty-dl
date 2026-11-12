"""Offline security regressions using generated audio and synthetic error data."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'backend'))
import download
import provider_search
import runtime_tools
from tests.test_download import audio_bytes


class PublicationSecurityTests(unittest.TestCase):
    def setUp(self):
        download.CANCELLED.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / 'audio.wav'
        self.audio.write_bytes(audio_bytes())

    def test_filename_cannot_escape_folder_or_name_a_windows_stream(self):
        for filename in ('../escape.wav', '/escape.wav', 'nested/escape.wav',
                         '..\\escape.wav', 'audio.wav:stream', '.', '..', ''):
            with self.subTest(filename=filename), self.assertRaises(download.UserError):
                download.publish(self.audio, self.root / 'provider', filename, 2)
        self.assertFalse((self.root / 'provider').exists())

    @unittest.skipUnless(os.name == 'posix', 'POSIX symbolic links')
    def test_provider_symlink_cannot_redirect_publication(self):
        outside = self.root / 'outside'
        outside.mkdir()
        provider = self.root / 'provider'
        provider.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(download.UserError):
            download.publish(self.audio, provider, 'song.wav', 2)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(self.audio.exists())

    @unittest.skipUnless(os.name == 'posix', 'POSIX symbolic links and FIFOs')
    def test_existing_symlink_and_fifo_are_never_read_as_duplicate_audio(self):
        provider = self.root / 'provider'
        provider.mkdir()
        target = provider / 'song.wav'
        target.symlink_to(self.audio)
        with self.assertRaises(download.UserError):
            download.publish(self.audio, provider, target.name, 2)
        target.unlink()
        os.mkfifo(target)
        with self.assertRaises(download.UserError):
            download.publish(self.audio, provider, target.name, 2)
        self.assertTrue(self.audio.exists())

    @unittest.skipUnless(os.name == 'posix', 'POSIX symbolic links')
    def test_symlink_input_is_never_published_or_probed(self):
        linked = self.root / 'linked.wav'
        linked.symlink_to(self.audio)
        with patch.object(download, 'measured_duration') as probe:
            with self.assertRaises(download.UserError):
                download.publish(linked, self.root / 'provider', 'song.wav', 2)
            probe.assert_not_called()

    @unittest.skipUnless(os.link in os.supports_dir_fd, 'descriptor-relative publication')
    def test_replacing_provider_directory_after_open_cannot_redirect_write(self):
        provider, outside = self.root / 'provider', self.root / 'outside'
        provider.mkdir()
        outside.mkdir()
        original_link = os.link

        def replace_then_link(source, target, **kwargs):
            provider.rename(self.root / 'original')
            provider.symlink_to(outside, target_is_directory=True)
            return original_link(source, target, **kwargs)

        # Preserve supports_dir_fd membership when mocking the function.
        with patch.object(os, 'link', replace_then_link), patch.object(
                os, 'supports_dir_fd', os.supports_dir_fd | {replace_then_link}):
            with self.assertRaises(download.UserError):
                download.publish(self.audio, provider, 'song.wav', 2)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list((self.root / 'original').iterdir()), [])
        self.assertTrue(self.audio.exists())


class ProviderErrorSecurityTests(unittest.TestCase):
    def test_bandcamp_error_does_not_disclose_transport_details(self):
        import requests
        detail = 'https://cdn.example/audio?token=FIXTURE_ONLY /private/fixture-user/audio'
        with patch('requests.get', side_effect=requests.RequestException(detail)):
            report = provider_search.search_tracks('Fixture', mode='Bandcamp')
        self.assertEqual(report.searched, ['Bandcamp'])
        self.assertIn('Bandcamp', report.errors)
        self.assertNotIn('FIXTURE_ONLY', str(report.errors))
        self.assertNotIn('fixture-user', str(report.errors))

    def test_extractor_errors_do_not_disclose_transport_details(self):
        from yt_dlp import YoutubeDL
        for source in ('SoundCloud', 'YouTube'):
            with self.subTest(source=source), patch.object(
                    YoutubeDL, 'extract_info', side_effect=RuntimeError(
                        'https://cdn.example/audio?token=FIXTURE_ONLY /private/fixture-user/audio')):
                report = provider_search.search_tracks('Fixture', mode=source)
                self.assertEqual(report.searched, [source])
                self.assertIn(source, report.errors)
                self.assertNotIn('FIXTURE_ONLY', str(report.errors))
                self.assertNotIn('fixture-user', str(report.errors))


class RuntimeDiscoverySecurityTests(unittest.TestCase):
    def test_missing_bundled_runtime_does_not_enable_path_fallback(self):
        from yt_dlp import YoutubeDL
        with patch.object(sys, 'frozen', True, create=True), patch.object(
                sys, '_MEIPASS', '/nonexistent-fixture-bundle', create=True), patch(
                'shutil.which') as discover:
            options = runtime_tools.javascript_options()
            discover.assert_not_called()
        with YoutubeDL({'quiet': True, **options}) as downloader:
            self.assertEqual(downloader.params['js_runtimes'], {})


if __name__ == '__main__':
    unittest.main()
