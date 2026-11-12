import hashlib
import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release_version


class ReleaseVersionTests(unittest.TestCase):
    def test_first_release_and_numeric_patch_increment(self):
        self.assertEqual(release_version.next_tag([], []), 'v1.0.0')
        self.assertEqual(release_version.next_tag(['v0.1.0'], []), 'v0.1.1')
        self.assertEqual(release_version.next_tag(
            ['v1.9.9', 'v1.10.10', 'v1.10.2', 'v1.2.99'], []), 'v1.10.11')
        self.assertEqual(release_version.next_tag(['v2.0.0', 'v1.99.99'], []), 'v2.0.1')

    def test_only_stable_canonical_version_tags_count(self):
        self.assertEqual(release_version.next_tag(
            ['1.0.0', 'v01.2.3', 'v1.02.3', 'v1.2.03', 'v9.0.0-rc.1',
             'v9.0.0+build', 'v2.0', 'release', 'v1.0.0'], []), 'v1.0.1')

    def test_retry_reuses_tag_on_source_even_after_newer_releases(self):
        self.assertEqual(release_version.next_tag(
            ['v1.0.0', 'v1.0.1'], ['v1.0.0']), 'v1.0.0')

    def test_versions_change_together_without_changing_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('package.json', 'package-lock.json', 'src-tauri/tauri.conf.json',
                         'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock'):
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, destination)
            lock = json.loads((root / 'package-lock.json').read_text())
            cargo_lock = (root / 'src-tauri/Cargo.lock').read_text()
            old_version = json.loads((root / 'package.json').read_text())['version']
            release_version.apply_version('v1.10.11', root)
            for name in ('package.json', 'package-lock.json', 'src-tauri/tauri.conf.json'):
                self.assertEqual(json.loads((root / name).read_text())['version'], '1.10.11')
            updated = json.loads((root / 'package-lock.json').read_text())
            self.assertEqual(updated['packages']['']['version'], '1.10.11')
            self.assertEqual({k: v for k, v in lock['packages'].items() if k},
                             {k: v for k, v in updated['packages'].items() if k})
            self.assertEqual((root / 'src-tauri/Cargo.lock').read_text(), cargo_lock.replace(
                f'name = "sweatydl-next"\nversion = "{old_version}"',
                'name = "sweatydl-next"\nversion = "1.10.11"'))
            self.assertIn('version = "1.10.11"', (root / 'src-tauri/Cargo.toml').read_text())
            with self.assertRaises(ValueError):
                release_version.apply_version('v01.0.0', root)


# The release job runs on Ubuntu. Exercise its real shell script and Git tags
# offline, substituting only GitHub API calls; no test can publish a real release.
@unittest.skipUnless(os.name != 'nt' and all(shutil.which(tool) for tool in
                                           ('bash', 'git', 'jq')),
                     'Release shell integration tests require Unix Git and jq')
class ReleaseFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'checkout'
        self.repo.mkdir()
        self.git('init', '--quiet')
        self.git('init', '--quiet', '--bare', str(self.root / 'origin.git'))
        self.git('remote', 'add', 'origin', str(self.root / 'origin.git'))
        scripts = self.repo / 'scripts'
        scripts.mkdir()
        for name in ('release_version.py', 'updater_manifest.py', 'deploy.py', 'sign_release.py', 'release_safety.py'):
            shutil.copy2(ROOT / 'scripts' / name, scripts)
        profile = self.repo / 'OS-specs-for-deployment/linux-1'
        profile.mkdir(parents=True)
        shutil.copy2(ROOT / 'OS-specs-for-deployment/linux-1/target.json', profile)
        (self.repo / 'src-tauri').mkdir()
        shutil.copy2(ROOT / 'src-tauri/tauri.conf.json', self.repo / 'src-tauri/tauri.conf.json')
        (self.repo / 'source.txt').write_text('source commit')
        self.git('add', 'source.txt', 'scripts')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '--quiet', '-m', 'Source')
        self.sha = self.git('rev-parse', 'HEAD')
        self.git('push', '--quiet', 'origin', 'HEAD:main')
        self.state_file = self.root / 'state.json'
        self.state_file.write_text(json.dumps({'releases': {}, 'calls': []}))
        binary = self.root / 'bin'
        binary.mkdir()
        gh = binary / 'gh'
        gh.write_text(f'#!{sys.executable}\n' + '''import json, os, subprocess, sys
from pathlib import Path
path = Path(os.environ['FAKE_GH_STATE'])
state = json.loads(path.read_text())
args = sys.argv[1:]
state['calls'].append(args)
path.write_text(json.dumps(state))
if args[0] == 'api':
    if os.environ.get('FAIL_API'):
        sys.exit(1)
    if '--method' in args:
        if os.environ.get('FAIL_TAG'):
            sys.exit(1)
        ref = next(arg[4:] for arg in args if arg.startswith('ref='))
        sha = next(arg[4:] for arg in args if arg.startswith('sha='))
        subprocess.run(['git', 'push', '--quiet', 'origin', sha + ':' + ref], check=True)
    else:
        for tag, draft in state['releases'].items():
            if '.prerelease' in args[-1]:
                if not draft:
                    print(tag)
            else:
                print(json.dumps({'tag_name': tag, 'draft': draft}))
elif args[:2] == ['release', 'create']:
    if os.environ.get('FAIL_CREATE'):
        sys.exit(1)
    assert args[2] not in state['releases']
    state['releases'][args[2]] = True
elif args[:2] == ['release', 'upload']:
    if os.environ.get('FAIL_UPLOAD'):
        sys.exit(1)
    assert all(Path(arg).is_file() for arg in args[3:] if arg != '--clobber')
elif args[:2] == ['release', 'edit']:
    if os.environ.get('FAIL_PUBLISH'):
        sys.exit(1)
    state['releases'][args[2]] = False
else:
    sys.exit(2)
path.write_text(json.dumps(state))
''')
        gh.chmod(0o755)
        verifier = binary / 'minisign'
        verifier.write_text(f'#!{sys.executable}\nimport os, sys\nsys.exit(1 if os.environ.get("FAIL_SIGNATURE") else 0)\n')
        verifier.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                    'FAKE_GH_STATE': str(self.state_file), 'TAG': 'v1.0.0',
                    'GITHUB_SHA': self.sha, 'GITHUB_REPOSITORY': 'test/repository',
                    'GITHUB_OUTPUT': str(self.root / 'output'), 'GITHUB_SERVER_URL': 'https://github.com',
                    'GITHUB_RUN_ID': '123'}
        self.directory = self.repo / 'deployments/SweatyDL-linux-1'
        self.directory.mkdir(parents=True)
        self.manifest = {'profile': 'linux-1', 'source_commit': self.sha, 'version': '1.0.0',
                         'status': 'built-needs-installation-test', 'artifacts': []}
        for extension in ('deb', 'deb.sig', 'AppImage', 'AppImage.sig'):
            filename = f'SweatyDL-Next_1.0.0_linux-1_x64.{extension}'
            data = base64.b64encode(b'untrusted comment: fixture\nfixture\ntrusted comment: version:1.0.0\nfixture\n') if extension.endswith('.sig') else b'installer'
            (self.directory / filename).write_bytes(data)
            self.manifest['artifacts'].append({
                'file': f'deployments/linux-1/{filename}', 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest()})
        self.write_manifest()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, text=True,
                                       stderr=subprocess.PIPE).strip()

    def write_manifest(self):
        (self.directory / 'build-result.json').write_text(json.dumps(self.manifest))

    def run_release(self, command, success=True, **env):
        # Each Actions job starts with a clean staging directory.
        staging = self.repo / 'release-assets'
        if staging.exists():
            shutil.rmtree(staging)
        result = subprocess.run(['bash', str(ROOT / 'scripts/release.sh'), command],
                                cwd=self.repo, env={**self.env, **env}, text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return json.loads(self.state_file.read_text())

    def test_prepare_publish_and_retry_never_duplicate_or_increment(self):
        self.run_release('prepare')
        self.assertIn('tag=v1.0.0\npublished=false', Path(self.env['GITHUB_OUTPUT']).read_text())
        self.assertEqual(self.git('tag', '--list'), '')
        state = self.run_release('publish')
        self.assertEqual(state['releases'], {'v1.0.0': False})
        self.assertEqual(self.git('rev-parse', 'v1.0.0^{commit}'), self.sha)
        self.assertEqual(self.git('ls-remote', '--tags', 'origin').split()[0], self.sha)
        writes = [call for call in state['calls'] if call[0] == 'release']
        self.assertEqual([call[1] for call in writes], ['create', 'upload', 'edit'])
        self.assertEqual(len(list((self.repo / 'release-assets').iterdir())), 6)
        feed = json.loads((self.repo / 'release-assets/latest.json').read_text())
        self.assertEqual(feed['version'], '1.0.0')
        self.assertEqual(set(feed['platforms']), {'linux-x86_64-deb', 'linux-x86_64-appimage'})
        self.run_release('prepare')
        self.assertTrue(Path(self.env['GITHUB_OUTPUT']).read_text().endswith('published=true\n'))
        state = self.run_release('publish')
        self.assertEqual([call for call in state['calls'] if call[0] == 'release'], writes)

    def test_dry_run_verifies_assets_without_tags_or_github_api_calls(self):
        state = self.run_release('verify')
        self.assertEqual(state['calls'], [])
        self.assertEqual(self.git('tag', '--list'), '')
        self.assertTrue((self.repo / 'release-assets/latest.json').is_file())

    def test_three_platform_release_attaches_every_package_and_matching_feed(self):
        for name, extensions in (
                ('mac-1', ('app.zip', 'dmg', 'app.tar.gz', 'app.tar.gz.sig')),
                ('windows-1', ('exe', 'exe.sig'))):
            profile = self.repo / 'OS-specs-for-deployment' / name
            profile.mkdir()
            shutil.copy2(ROOT / 'OS-specs-for-deployment' / name / 'target.json', profile)
            output = self.repo / 'deployments' / f'SweatyDL-{name}'
            output.mkdir()
            artifacts = []
            for extension in extensions:
                suffix = '-setup.' + extension if name == 'windows-1' else '.' + extension
                filename = f'SweatyDL-Next_1.0.0_{name}_x64{suffix}'
                data = base64.b64encode(b'untrusted comment: fixture\nfixture\ntrusted comment: version:1.0.0\nfixture\n') if extension.endswith('.sig') else b'installer'
                if extension == 'app.zip':
                    stream = io.BytesIO()
                    with zipfile.ZipFile(stream, 'w') as archive:
                        archive.writestr('SweatyDL Next.app/Contents/Info.plist', 'fixture')
                    data = stream.getvalue()
                elif extension == 'app.tar.gz':
                    stream = io.BytesIO()
                    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                        member = tarfile.TarInfo('SweatyDL Next.app/Contents/Info.plist')
                        member.size = len(b'fixture')
                        archive.addfile(member, io.BytesIO(b'fixture'))
                    data = stream.getvalue()
                (output / filename).write_bytes(data)
                artifacts.append({'file': f'deployments/{name}/{filename}', 'bytes': len(data),
                                  'sha256': hashlib.sha256(data).hexdigest()})
            (output / 'build-result.json').write_text(json.dumps({
                **self.manifest, 'profile': name, 'artifacts': artifacts}))
        state = self.run_release('publish')
        assets = self.repo / 'release-assets'
        self.assertEqual(len(list(assets.iterdir())), 14)
        feed = json.loads((assets / 'latest.json').read_text())
        self.assertEqual(set(feed['platforms']), {'darwin-x86_64', 'windows-x86_64',
                                                 'linux-x86_64-deb', 'linux-x86_64-appimage'})
        for platform in feed['platforms'].values():
            self.assertIn('/releases/download/v1.0.0/', platform['url'])
        upload = next(call for call in state['calls'] if call[:2] == ['release', 'upload'])
        self.assertEqual(len(upload[3:-1]), 14)
        for name in ('linux-1', 'mac-1', 'windows-1'):
            result = json.loads((assets / f'{name}-build-result.json').read_text())
            self.assertEqual(result['source_commit'], self.sha)
            self.assertEqual(result['version'], '1.0.0')

    def test_upload_failure_leaves_draft_and_retry_recovers_same_version(self):
        state = self.run_release('publish', success=False, FAIL_UPLOAD='1')
        self.assertEqual(state['releases'], {'v1.0.0': True})
        self.assertNotIn('edit', [call[1] for call in state['calls'] if call[0] == 'release'])
        self.run_release('prepare')
        self.assertIn('tag=v1.0.0', Path(self.env['GITHUB_OUTPUT']).read_text())
        state = self.run_release('publish')
        self.assertEqual(state['releases'], {'v1.0.0': False})
        self.assertEqual(sum(call[:2] == ['release', 'create'] for call in state['calls']), 1)

    def test_failure_after_tag_creation_retries_same_version(self):
        state = self.run_release('publish', success=False, FAIL_CREATE='1')
        self.assertEqual(state['releases'], {})
        self.assertEqual(self.git('tag', '--list'), 'v1.0.0')
        self.run_release('prepare')
        self.assertIn('tag=v1.0.0', Path(self.env['GITHUB_OUTPUT']).read_text())
        state = self.run_release('publish')
        self.assertEqual(state['releases'], {'v1.0.0': False})

    def test_tag_api_failure_creates_neither_local_tag_nor_release(self):
        state = self.run_release('publish', success=False, FAIL_TAG='1')
        self.assertEqual(self.git('tag', '--list'), '')
        self.assertEqual(state['releases'], {})

    def test_publication_failure_resumes_without_new_tag_or_release(self):
        state = self.run_release('publish', success=False, FAIL_PUBLISH='1')
        self.assertEqual(state['releases'], {'v1.0.0': True})
        self.run_release('prepare')
        state = self.run_release('publish')
        self.assertEqual(state['releases'], {'v1.0.0': False})
        self.assertEqual(sum(call[:2] == ['release', 'create'] for call in state['calls']), 1)
        self.assertEqual(self.git('tag', '--list'), 'v1.0.0')

    def test_publishing_older_draft_does_not_replace_newer_update_feed(self):
        self.state_file.write_text(json.dumps({'releases': {'v1.0.1': False}, 'calls': []}))
        state = self.run_release('publish')
        edits = [call for call in state['calls'] if call[:2] == ['release', 'edit']]
        self.assertEqual(len(edits), 1)
        self.assertIn('--latest=false', edits[0])

    def test_bad_or_missing_build_outputs_never_create_tag_or_release(self):
        for field, value in (('status', 'failed'), ('source_commit', 'wrong'),
                             ('version', '0.1.1'), ('artifacts', [])):
            with self.subTest(field=field):
                original = self.manifest[field]
                self.manifest[field] = value
                self.write_manifest()
                self.run_release('publish', success=False)
                self.manifest[field] = original
        self.write_manifest()
        (self.directory / Path(self.manifest['artifacts'][0]['file']).name).write_bytes(b'corrupt')
        state = self.run_release('publish', success=False)
        (self.directory / Path(self.manifest['artifacts'][0]['file']).name).unlink()
        self.run_release('publish', success=False)
        self.assertEqual(self.git('tag', '--list'), '')
        self.assertEqual(state['releases'], {})

    def test_omitted_package_stops_release_even_when_other_checksums_match(self):
        self.manifest['artifacts'] = self.manifest['artifacts'][1:]
        self.write_manifest()
        state = self.run_release('publish', success=False)
        self.assertEqual(self.git('tag', '--list'), '')
        self.assertEqual(state['releases'], {})

    def test_incorrect_package_size_stops_release(self):
        self.manifest['artifacts'][0]['bytes'] += 1
        self.write_manifest()
        state = self.run_release('publish', success=False)
        self.assertEqual(self.git('tag', '--list'), '')
        self.assertEqual(state['releases'], {})

    def test_missing_platform_prevents_tag_and_release(self):
        profile = self.repo / 'OS-specs-for-deployment/mac-1'
        profile.mkdir()
        shutil.copy2(ROOT / 'OS-specs-for-deployment/mac-1/target.json', profile)
        state = self.run_release('publish', success=False)
        self.assertEqual(self.git('tag', '--list'), '')
        self.assertEqual(state['releases'], {})
        self.assertFalse(any(call[0] == 'release' for call in state['calls']))

    def test_existing_tag_on_different_commit_is_never_moved(self):
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '--quiet', '--allow-empty', '-m', 'Different source')
        self.git('tag', 'v1.0.0')
        other = self.git('rev-parse', 'HEAD')
        self.git('push', '--quiet', 'origin', 'refs/tags/v1.0.0')
        state = self.run_release('publish', success=False)
        self.assertEqual(self.git('rev-parse', 'v1.0.0'), other)
        self.assertEqual(state['releases'], {})

    def test_api_failure_does_not_mean_missing_release(self):
        self.run_release('prepare', success=False, FAIL_API='1')
        state = self.run_release('publish', success=False, FAIL_API='1')
        self.assertEqual(state['releases'], {})
        self.assertEqual(self.git('tag', '--list'), '')
