import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import prepare_release_tools


def make_tar(path, members):
    with tarfile.open(path, 'w:xz') as archive:
        for name, data, kind, target in members:
            item = tarfile.TarInfo(name)
            if kind == 'link':
                item.type = tarfile.SYMTYPE
                item.linkname = target
                archive.addfile(item)
            elif kind == 'dir':
                item.type = tarfile.DIRTYPE
                archive.addfile(item)
            else:
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))


class ReleaseToolsTests(unittest.TestCase):
    def test_bad_archive_hash_fails_before_archive_members_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'node-v22.23.5-linux-x64.tar.xz'
            make_tar(path, [('node-v22.23.5-linux-x64/LICENSE', b'license', 'file', '')])
            with patch.object(prepare_release_tools, '_tar_members', side_effect=AssertionError('archive was opened')):
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    prepare_release_tools.extract_node_archive(path, 'Linux', Path(directory) / 'out', '0' * 64)

    def test_node_extraction_selects_only_binary_and_license_and_checks_binary_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = 'node-v22.23.5-linux-x64'
            archive = Path(directory) / f'{root}.tar.xz'
            payload = b'fixture node executable'
            make_tar(archive, [
                (root, b'', 'dir', ''),
                (f'{root}/bin', b'', 'dir', ''),
                (f'{root}/bin/node', payload, 'file', ''),
                (f'{root}/LICENSE', b'Node fixture license', 'file', ''),
                (f'{root}/include/node.h', b'not extracted', 'file', ''),
            ])
            archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
            binary, license_data = prepare_release_tools.extract_node_archive(
                archive, 'Linux', Path(directory) / 'bin', archive_hash)
            self.assertEqual(binary.read_bytes(), payload)
            self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
            self.assertEqual(license_data, b'Node fixture license')
            self.assertEqual(sorted(p.name for p in binary.parent.iterdir()), ['node'])
            self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), hashlib.sha256(payload).hexdigest())

    def test_windows_zip_root_and_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = 'node-v22.23.5-win-x64'
            archive = Path(directory) / f'{root}.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr(f'{root}/node.exe', b'fixture windows node')
                output.writestr(f'{root}/LICENSE', b'Node fixture license')
                output.writestr(f'{root}/include/node.h', b'not extracted')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            binary, license_data = prepare_release_tools.extract_node_archive(
                archive, 'Windows', Path(directory) / 'bin', digest)
            self.assertEqual(binary.name, 'node.exe')
            self.assertEqual(binary.read_bytes(), b'fixture windows node')
            self.assertEqual(license_data, b'Node fixture license')
            self.assertEqual(sorted(p.name for p in binary.parent.iterdir()), ['node.exe'])

    def test_unsafe_tar_path_and_escaping_symlink_are_rejected(self):
        for entry in [('../outside', b'x', 'file', ''),
                      ('node-v22.23.5-linux-x64/bin/link', b'', 'link', '../../../outside')]:
            with self.subTest(entry=entry[0]), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / 'node-v22.23.5-linux-x64.tar.xz'
                make_tar(archive, [entry])
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                with self.assertRaisesRegex(RuntimeError, 'unsafe'):
                    prepare_release_tools.extract_node_archive(
                        archive, 'Linux', Path(directory) / 'out', digest)

    def test_build_environment_is_minimal_and_does_not_forward_signing_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {
                'PATH': '/private/path', 'GITHUB_TOKEN': 'fixture-token',
                'TAURI_SIGNING_PRIVATE_KEY': 'fixture-key', 'UNRELATED_SECRET': 'fixture-secret'}, clear=True):
            env = prepare_release_tools._clean_environment(Path(directory), 'Linux', Path('/usr/bin'))
            self.assertEqual(env['PATH'], '/usr/bin:/bin')
            self.assertNotIn('GITHUB_TOKEN', env)
            self.assertNotIn('TAURI_SIGNING_PRIVATE_KEY', env)
            self.assertNotIn('UNRELATED_SECRET', env)
            self.assertIn('-ffile-prefix-map=', env['CFLAGS'])


if __name__ == '__main__':
    unittest.main()
