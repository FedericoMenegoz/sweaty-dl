import base64
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import package_backend
import deploy
import release_safety
import sign_release
import updater_manifest


class ReleaseSafetyTests(unittest.TestCase):
    def test_release_build_cannot_reuse_a_backend_from_another_source(self):
        with patch.dict(os.environ, {'SWEATYDL_RELEASE_BUILD': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'freshly packaged backend'):
                deploy.build({'name': 'linux-1'}, skip_backend=True)

    def test_embedded_credentials_and_home_paths_stop_staging_without_echoing_matches(self):
        fixtures = [b'ghp_' + b'X' * 36, b'/Users/' + b'fixture-owner/project',
                    b'/home/' + b'fixture-owner/project', b'C:\\Users\\' + b'fixture-owner\\project']
        for value in fixtures:
            with self.subTest(value=value[:4]):
                with self.assertRaises(ValueError) as raised:
                    release_safety.scan_stream(io.BytesIO(value))
                self.assertNotIn(value.decode(), str(raised.exception))
        release_safety.scan_stream(io.BytesIO(b'ordinary package data'))

    def test_archive_rejects_owner_metadata_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'app.app.tar.gz'
            for name, owner in [('App.app/file', 'fixture-owner'), ('../outside', '')]:
                with tarfile.open(path, 'w:gz') as archive:
                    entry = tarfile.TarInfo(name)
                    entry.uname = owner
                    archive.addfile(entry)
                with self.assertRaises(ValueError):
                    release_safety.scan_artifact(path)

    def test_safe_relative_framework_symlink_is_preserved_and_escaping_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'app.app.tar.gz'
            for target, allowed in [('Library', True), ('../../../outside', False)]:
                with tarfile.open(path, 'w:gz') as archive:
                    entry = tarfile.TarInfo('App.app/Contents/LibraryLink')
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = target
                    archive.addfile(entry)
                if allowed:
                    release_safety.scan_artifact(path)
                else:
                    with self.assertRaises(ValueError):
                        release_safety.scan_artifact(path)

    def test_zip_symlinks_and_credential_configuration_files_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'app.app.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                entry = zipfile.ZipInfo('App.app/Contents/link')
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(entry, '../../../outside')
            with self.assertRaises(ValueError):
                release_safety.scan_artifact(path)
        for name in ('App.app/.npmrc', 'App.app/.netrc', 'App.app/id_ed25519'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release_safety.check_name(name)

    def test_old_bundled_tools_fail_release_baseline(self):
        valid = {'tools': {'node': 'v22.23.5', 'ffprobe': 'ffprobe version 8.1.2'}}
        package_backend.validate_release_tools(valid)
        for name, version in [('node', 'v22.11.0'), ('ffprobe', 'ffprobe version 7.1.1')]:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                package_backend.validate_release_tools({'tools': {**valid['tools'], name: version}})

    def test_licenses_do_not_copy_python_modules_or_bytecode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['licenses/LICENSE', 'licenses/MIT.txt', 'licenses/__init__.py', 'licenses/LICENSE.pyc']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            class Distribution:
                metadata = {'Name': 'fixture-package'}
                version = '1.0.0'
                files = [Path(name) for name in ['licenses/LICENSE', 'licenses/MIT.txt', 'licenses/__init__.py', 'licenses/LICENSE.pyc']]
                def locate_file(self, file):
                    return root / file
            with patch.object(package_backend.metadata, 'distributions', return_value=[Distribution()]):
                package_backend.copy_notices(root / 'output')
            names = [path.name for path in (root / 'output/fixture-package').iterdir()]
            self.assertEqual(sorted(names), ['0-LICENSE', '1-MIT.txt'])

    def test_runtime_metadata_retains_headers_and_licenses_without_documentation_examples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / '_internal/fixture.dist-info/METADATA'
            metadata.parent.mkdir(parents=True)
            headers = b'Metadata-Version: 2.4\nName: fixture\nVersion: 1.0.0\nLicense-Expression: MIT'
            metadata.write_bytes(headers + b'\n\nDocumentation example: /home/' + b'fixture-owner/file\n')
            license_path = metadata.with_name('LICENSE')
            license_path.write_bytes(b'Unchanged license')
            package_backend.strip_metadata_descriptions(root)
            self.assertEqual(metadata.read_bytes(), headers + b'\n\n')
            self.assertEqual(license_path.read_bytes(), b'Unchanged license')
            release_safety.scan_tree(root)

    def test_pinned_upstream_node_images_allow_public_build_paths_only_when_bytes_match(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'scripts/release_safety.py'
            script.parent.mkdir()
            config = root / 'deployment/toolchain.json'
            config.parent.mkdir()
            binary = b'Official upstream fixture /home/' + b'upstream-builder/source'
            digest = hashlib.sha256(binary).hexdigest()
            config.write_text(json.dumps({'node': {'platforms': {'Linux': {'binary_sha256': digest}}}}))
            with patch.object(release_safety, '__file__', str(script)):
                release_safety.scan_stream(io.BytesIO(binary))
                with self.assertRaises(ValueError):
                    release_safety.scan_stream(io.BytesIO(binary + b'modified'))
                with self.assertRaises(ValueError):
                    release_safety.scan_stream(io.BytesIO(binary + b'ghp_' + b'X' * 36))

    def test_release_tool_proof_requires_pinned_origins_and_actual_binaries(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'deployment').mkdir()
            node, ffprobe = root / 'node', root / 'ffprobe'
            node.write_bytes(b'node fixture')
            ffprobe.write_bytes(b'ffprobe fixture')
            node_hash = hashlib.sha256(node.read_bytes()).hexdigest()
            pins = {'node': {'version': '22.23.5', 'platforms': {'Linux': {
                'archive': 'node.tar.xz', 'sha256': 'a' * 64, 'binary_sha256': node_hash}}},
                'ffprobe': {'url': 'https://ffmpeg.org/releases/source.tar.xz', 'sha256': 'b' * 64}}
            (root / 'deployment/toolchain.json').write_text(json.dumps(pins))
            proof = {'tools': {
                'node': {'url': 'https://nodejs.org/dist/v22.23.5/node.tar.xz',
                         'archive_sha256': 'a' * 64, 'sha256': node_hash},
                'ffprobe': {'url': pins['ffprobe']['url'], 'archive_sha256': 'b' * 64,
                            'sha256': hashlib.sha256(ffprobe.read_bytes()).hexdigest()}}}
            tools = {'node': node, 'ffprobe': ffprobe}
            with patch.object(package_backend, 'ROOT', root), patch('package_backend.platform.system', return_value='Linux'):
                self.assertEqual(package_backend.verified_toolchain(tools, proof), proof)
                node.write_bytes(b'tampered fixture')
                with self.assertRaises(RuntimeError):
                    package_backend.verified_toolchain(tools, proof)
                node.write_bytes(b'node fixture')
                proof['tools']['ffprobe']['url'] = 'https://example.invalid/source.tar.xz'
                with self.assertRaises(RuntimeError):
                    package_backend.verified_toolchain(tools, proof)


class SignatureVerificationTests(unittest.TestCase):
    def test_signing_only_passes_key_to_signer_and_outputs_expected_files(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / 'node_modules/.bin/tauri'
            cli.parent.mkdir(parents=True)
            cli.write_text('fixture executable; subprocess is mocked')
            config = root / 'src-tauri/tauri.conf.json'
            config.parent.mkdir()
            config.write_text(json.dumps({'plugins': {'updater': {'pubkey': base64.b64encode(b'public fixture').decode()}}}))
            profile = {'name': 'linux-1', 'os': 'linux', 'arch': 'x86_64', 'formats': ['deb', 'AppImage']}
            source = root / 'input/SweatyDL-linux-1'
            source.mkdir(parents=True)
            artifacts = []
            for extension in ('.deb', '.AppImage'):
                name = 'SweatyDL-Next_1.0.0_linux-1_x64' + extension
                (source / name).write_bytes(b'package fixture')
                artifacts.append({'file': 'deployments/linux-1/' + name, 'bytes': len(b'package fixture'),
                                  'sha256': hashlib.sha256(b'package fixture').hexdigest()})
            (source / 'build-result.json').write_text(json.dumps({'profile': 'linux-1', 'version': '1.0.0',
                'source_commit': 'a' * 40, 'status': 'built-needs-installation-test', 'artifacts': artifacts}))

            def execute(command, **kwargs):
                if command[0] == str(cli):
                    self.assertIn('TAURI_SIGNING_PRIVATE_KEY', kwargs['env'])
                    self.assertEqual(kwargs['env']['TAURI_SIGNING_PRIVATE_KEY'], 'fixture-only')
                    self.assertNotIn('GITHUB_TOKEN', kwargs['env'])
                    package = Path(command[-1])
                    encoded = base64.b64encode(b'untrusted comment: fixture\nfixture\ntrusted comment: version:1.0.0\nfixture\n')
                    Path(str(package) + '.sig').write_bytes(encoded)
                else:
                    self.assertEqual(command[0], 'minisign')
                    self.assertNotIn('TAURI_SIGNING_PRIVATE_KEY', kwargs['env'])

            with patch.object(sign_release, 'ROOT', root), patch.object(sign_release, 'profiles', return_value=[profile]), \
                    patch.object(sign_release.shutil, 'which', return_value='/fixture/minisign'), \
                    patch.object(sign_release.subprocess, 'run', side_effect=execute), patch.dict(os.environ, {
                        'GITHUB_ACTIONS': 'false', 'TAURI_SIGNING_PRIVATE_KEY': 'fixture-only\n', 'GITHUB_TOKEN': 'fixture-only'}):
                sign_release.sign_packages(root / 'input', root / 'output', 'v1.0.0', 'a' * 40)
            result = json.loads((root / 'output/SweatyDL-linux-1/build-result.json').read_text())
            self.assertEqual(len(result['artifacts']), 4)
            self.assertEqual(len(list((root / 'output/SweatyDL-linux-1').iterdir())), 5)
            updater_manifest.stage_assets(root / 'output', root / 'verified', 'v1.0.0', 'a' * 40, [profile])

    def test_verifier_failure_and_signed_version_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / 'package.AppImage'
            package.write_bytes(b'fixture')
            signature = b'untrusted comment: fixture\nfixture\ntrusted comment: version:1.0.0\nfixture\n'
            Path(str(package) + '.sig').write_bytes(base64.b64encode(signature))
            public = base64.b64encode(b'untrusted comment: public\nfixture\n').decode()
            with patch.object(sign_release, 'public_key', return_value=public), \
                    patch.object(sign_release.subprocess, 'run') as verify, \
                    patch.dict(os.environ, {'TAURI_SIGNING_PRIVATE_KEY': 'fixture-only'}):
                sign_release.verify_signature(package, '1.0.0')
                self.assertNotIn('TAURI_SIGNING_PRIVATE_KEY', verify.call_args.kwargs['env'])
                with self.assertRaises(RuntimeError):
                    sign_release.verify_signature(package, '1.0.1')
                verify.side_effect = subprocess.CalledProcessError(1, ['minisign'])
                with self.assertRaises(RuntimeError):
                    sign_release.verify_signature(package, '1.0.0')

    def test_ci_signing_is_forbidden_for_manual_or_tag_builds(self):
        for event, ref in [('workflow_dispatch', 'refs/heads/deployment'), ('push', 'refs/tags/v1.0.0'),
                           ('push', 'refs/heads/main')]:
            with self.subTest(event=event, ref=ref), patch.dict(os.environ, {
                    'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': event, 'GITHUB_REF': ref}):
                with self.assertRaisesRegex(RuntimeError, 'restricted'):
                    sign_release.sign_packages(Path('unused'), Path('unused'), 'v1.0.0', 'a' * 40)
