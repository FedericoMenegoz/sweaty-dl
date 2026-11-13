import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
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
import updater_manifest


class DeploymentTests(unittest.TestCase):
    def test_disk_image_signature_failure_detaches_the_read_only_mount(self):
        if sys.platform == 'win32':
            self.skipTest('macOS disk-image layout requires Unix symlinks')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = []
            def execute(command, **kwargs):
                commands.append(command)
                if command[:2] == ['hdiutil', 'attach']:
                    self.assertIn('-readonly', command)
                    mount = Path(command[command.index('-mountpoint') + 1])
                    (mount / 'SweatyDL Next.app').mkdir()
                    (mount / 'Applications').symlink_to('/Applications')
                    return subprocess.CompletedProcess(command, 0, stdout=plistlib.dumps({
                        'system-entities': [{'mount-point': str(mount)}]}))
                if command[0] == 'codesign':
                    raise subprocess.CalledProcessError(1, command)
                return subprocess.CompletedProcess(command, 0)
            with patch.object(deploy, 'run', side_effect=execute):
                with self.assertRaises(subprocess.CalledProcessError):
                    deploy.make_disk_image(root / 'stage', root / 'test.dmg')
            self.assertEqual(commands[-1][:2], ['hdiutil', 'detach'])
            self.assertFalse(any('makehybrid' in command for command in commands))

    def test_windows_build_manifest_stages_with_portable_paths_and_verified_bytes(self):
        import package_backend
        profile = next(item for item in deploy.profiles() if item['os'] == 'windows')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'package.json').write_text('{"version":"1.2.3"}')
            resources = root / 'backend'
            resources.mkdir()
            (resources / 'build-info.json').write_text(json.dumps({
                'ok': True, 'frozen': True, 'os': 'Windows', 'arch': 'AMD64'}))
            bundle = root / 'src-tauri/target' / profile['target'] / 'release/bundle/nsis'
            bundle.mkdir(parents=True)
            (bundle / 'package.exe').write_bytes(b'installer fixture')
            output = root / 'deployments'
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'OUTPUT', output), \
                    patch.object(package_backend, 'RESOURCES', resources), \
                    patch('deploy.platform.system', return_value='Windows'), \
                    patch('deploy.shutil.which', return_value='npm.cmd'), patch.object(deploy, 'run'):
                artifacts = deploy.build(profile, skip_backend=True, updater=True)
            filename = 'SweatyDL-Next_1.2.3_windows-1_x64-setup.exe'
            self.assertEqual(artifacts[0]['file'], f'deployments/windows-1/{filename}')
            (output / 'windows-1/build-result.json').write_text(json.dumps({
                'profile': 'windows-1', 'version': '1.2.3', 'source_commit': 'a' * 40,
                'status': 'built-needs-installation-test', 'artifacts': artifacts}))
            updater_manifest.stage_assets(output, root / 'assets', 'v1.2.3',
                                           'a' * 40, [profile], signed=False, prefix='')
            self.assertEqual((root / 'assets' / filename).read_bytes(), b'installer fixture')
            (output / 'windows-1' / filename).write_bytes(b'changed installer')
            with self.assertRaisesRegex(ValueError, 'Package verification failed'):
                updater_manifest.stage_assets(output, root / 'changed-assets', 'v1.2.3',
                                               'a' * 40, [profile], signed=False, prefix='')

    def test_matrix_cli_outputs_all_profiles_without_building(self):
        result = subprocess.run(
            [sys.executable, '-B', str(ROOT / 'scripts/deploy.py'), '--matrix'],
            cwd=tempfile.gettempdir(), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {'include': [
            {'profile': 'linux-1', 'runner': 'ubuntu-22.04'},
            {'profile': 'mac-1', 'runner': 'macos-15-intel'},
            {'profile': 'windows-1', 'runner': 'windows-latest'},
        ]})

    def test_matrix_cli_can_select_one_profile(self):
        result = subprocess.run(
            [sys.executable, '-B', str(ROOT / 'scripts/deploy.py'),
             '--matrix', '--target', 'linux-1'],
            capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {'include': [
            {'profile': 'linux-1', 'runner': 'ubuntu-22.04'},
        ]})

    def test_main_writes_build_results_and_returns_failure_for_a_failed_build(self):
        profile = next(item for item in deploy.profiles() if item['os'] == 'linux')
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'deployments'
                stale = output / profile['name'] / 'verified-artifacts.json'
                stale.parent.mkdir(parents=True)
                stale.write_text('[{"file":"stale-package-from-another-run"}]')
                with patch.object(sys, 'argv', ['deploy.py', '--target', profile['name']]), \
                        patch.object(deploy, 'OUTPUT', output), \
                        patch('deploy.platform.system', return_value='Linux'), \
                        patch('deploy.platform.machine', return_value='x86_64'), \
                        patch.object(deploy, 'build', return_value=[],
                                     side_effect=RuntimeError('Build failed') if failure else None), \
                        patch('builtins.print'):
                    self.assertEqual(deploy.main(), 1 if failure else 0)
                results = json.loads((output / 'build-results.json').read_text())
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]['status'], 'failed' if failure else 'built-needs-installation-test')
                self.assertEqual(results[0]['version'], json.loads((ROOT / 'package.json').read_text())['version'])
                self.assertIn('source_commit', results[0])
                self.assertEqual(json.loads((output / profile['name'] / 'build-result.json').read_text()), results[0])

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
                    self.assertEqual({Path(artifact['file']).suffix for artifact in artifacts}, {'.deb', '.AppImage'})
                    for artifact in artifacts:
                        output = root / artifact['file']
                        self.assertEqual(output.read_bytes(), b'package fixture')
                        self.assertEqual(artifact['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
                    command = build.call_args.args[0]
                    self.assertEqual(command[command.index('--bundles') + 1], 'deb,appimage')
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
                         'docs/releases.md',
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
