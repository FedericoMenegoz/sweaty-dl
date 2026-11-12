import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import deploy
import updater_manifest


class UpdaterPackagingTests(unittest.TestCase):
    def test_final_mac_archives_preserve_symlinks_without_owner_metadata(self):
        if os.name == 'nt':
            self.skipTest('macOS archive layout requires Unix symlinks')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            app = output / 'mac-1/SweatyDL Next.app'
            backend = app / 'Contents/Resources/backend'
            backend.mkdir(parents=True)
            (backend / 'actual').write_bytes(b'frozen runtime')
            (backend / 'linked').symlink_to('actual')
            artifacts = []
            with patch.object(deploy, 'OUTPUT', output):
                deploy.prepare_update({'os': 'macos', 'name': 'mac-1'}, artifacts, '1.2.3')
            self.assertEqual(len(artifacts), 1)
            with tarfile.open(artifacts[0]) as tar:
                link = tar.getmember('SweatyDL Next.app/Contents/Resources/backend/linked')
                self.assertTrue(link.issym())
                self.assertEqual(link.linkname, 'actual')
                for member in tar:
                    self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, '', ''))
            archive = output / 'app.zip'
            deploy.make_app_zip(app, archive)
            import stat
            import zipfile
            with zipfile.ZipFile(archive) as zipped:
                link = zipped.getinfo('SweatyDL Next.app/Contents/Resources/backend/linked')
                self.assertTrue(stat.S_ISLNK(link.external_attr >> 16))
                self.assertEqual(zipped.read(link), b'actual')
                self.assertEqual(link.extra, b'')

    def test_update_feed_uses_correct_assets_and_installer_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            assets = Path(directory)
            for profile in deploy.profiles():
                extensions = {'macos': ['.app.tar.gz'], 'windows': ['-setup.exe'],
                              'linux': ['.deb', '.AppImage']}[profile['os']]
                files = []
                for extension in extensions:
                    name = profile['name'] + extension
                    (assets / name).write_bytes(b'package')
                    (assets / (name + '.sig')).write_text('signed-' + name)
                    files.extend([{'file': name}, {'file': name + '.sig'}])
                (assets / (profile['name'] + '-build-result.json')).write_text(json.dumps({
                    'version': '1.2.3', 'status': 'built-needs-installation-test', 'artifacts': files}))
            feed = updater_manifest.create_manifest(assets, 'v1.2.3', 'test/repository', deploy.profiles())
            self.assertEqual(feed['version'], '1.2.3')
            self.assertEqual(set(feed['platforms']), {'darwin-x86_64', 'windows-x86_64',
                                                     'linux-x86_64-deb', 'linux-x86_64-appimage'})
            for platform in feed['platforms'].values():
                self.assertIn('/releases/download/v1.2.3/', platform['url'])
                self.assertTrue(platform['signature'].startswith('signed-'))
            (assets / 'mac-1.app.tar.gz.sig').unlink()
            with self.assertRaises(FileNotFoundError):
                updater_manifest.create_manifest(assets, 'v1.2.3', 'test/repository', deploy.profiles())
