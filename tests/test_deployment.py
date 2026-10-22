import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'backend'))
import deploy
import provider_search
import runtime_tools


class DeploymentTests(unittest.TestCase):
    def test_each_folder_has_a_result_even_when_specs_are_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'new-machine').mkdir()
            (root / 'windows').mkdir()
            (root / 'windows/target.json').write_text(json.dumps({
                'os': 'windows', 'arch': 'x86_64', 'target': 'x86_64-pc-windows-msvc',
                'minimum_os': '10.0.19041', 'formats': ['nsis.exe']}))
            results = deploy.profiles(root)
            self.assertEqual(len(results), 2)
            self.assertIn('error', results[0])
            self.assertEqual(results[1]['target'], 'x86_64-pc-windows-msvc')

    def test_incompatible_extension_cannot_pass_as_a_windows_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'windows'
            folder.mkdir()
            (folder / 'target.json').write_text(json.dumps({
                'os': 'windows', 'arch': 'x86_64', 'target': 'x86_64-pc-windows-msvc',
                'minimum_os': '10.0.19041', 'formats': ['dmg']}))
            self.assertIn('error', deploy.profiles(Path(directory))[0])

    def test_linux_packaging_reports_real_outputs_and_rejects_a_missing_format(self):
        import package_backend
        profile = next(item for item in deploy.profiles() if item['os'] == 'linux')
        for complete in (True, False):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'package.json').write_text('{"version":"0.1.0"}')
                resources = root / 'backend'
                resources.mkdir()
                (resources / 'build-info.json').write_text(json.dumps({
                    'ok': True, 'frozen': True, 'os': 'Linux', 'arch': 'x86_64'}))
                bundle = root / 'src-tauri/target' / profile['target'] / 'release/bundle'
                for format_name in (['deb', 'AppImage'] if complete else ['deb']):
                    output = bundle / format_name.lower() / f'package.{format_name}'
                    output.parent.mkdir(parents=True)
                    output.write_bytes(b'package fixture')
                with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'OUTPUT', root / 'deployments'), \
                        patch.object(package_backend, 'RESOURCES', resources), \
                        patch('deploy.platform.system', return_value='Linux'), \
                        patch('deploy.shutil.which', return_value='/tools/npm'), patch.object(deploy, 'run') as build:
                    if not complete:
                        with self.assertRaisesRegex(RuntimeError, 'AppImage'):
                            deploy.build(profile, skip_backend=True)
                        continue
                    artifacts = deploy.build(profile, skip_backend=True)
                    self.assertEqual(len(artifacts), 2)
                    for artifact in artifacts:
                        output = root / artifact['file']
                        self.assertEqual(output.read_bytes(), b'package fixture')
                        self.assertEqual(artifact['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
                    command = build.call_args.args[0]
                    self.assertEqual(set(command[-1].split(',')), {'deb', 'AppImage'})
                    config = json.loads((root / '.build/tauri-linux-1.json').read_text())
                    self.assertTrue(config['bundle']['linux']['appimage']['bundleMediaFramework'])
                    self.assertNotIn('windows', config['bundle'])

    def test_source_kit_contains_build_inputs_without_local_notes_or_generated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            kit = deploy.build_kit(Path(directory) / 'source.zip')
            with zipfile.ZipFile(kit) as archive:
                names = set(archive.namelist())
            for name in ('README.md', '.gitignore', 'package-lock.json', 'src-tauri/Cargo.lock',
                         'deployment/requirements-build.txt', '.github/workflows/deployment.yml',
                         'OS-specs-for-deployment/linux-1/target.json',
                         'tests/fixtures/bandcamp-el-sonido.html'):
                self.assertIn(name, names)
            for name in names:
                self.assertFalse(any(part in name.split('/') for part in
                                     ('.build', '.logs', 'node_modules', '__pycache__', 'target', 'gen')))
                self.assertNotIn(Path(name).name, ('AGENTS.md', 'agent.md', 'plan.md', '.DS_Store'))
                self.assertNotIn(Path(name).suffix, ('.jpeg', '.pyc'))

    def test_ported_catalog_parser_preserves_all_real_fixture_results(self):
        fixture = ROOT / 'tests/fixtures/bandcamp-el-sonido.html'
        rows = provider_search.parse_bandcamp(fixture.read_text())
        self.assertEqual(len(rows), 18)
        self.assertEqual(rows[0].url, 'https://truncate.bandcamp.com/track/el-sonido')
        self.assertFalse(provider_search.convincing(provider_search.Result('Song live', 'Artist', '', ''), 'Song', 'Artist'))

    def test_frozen_tools_never_fall_back_to_the_development_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, 'frozen', True, create=True), patch.object(sys, '_MEIPASS', directory, create=True), patch('shutil.which', return_value='/development/node') as path:
                self.assertIsNone(runtime_tools.tool('node'))
                path.assert_not_called()
