"""One deployment result per OS-specs-for-deployment folder; never fake foreign binaries."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / 'OS-specs-for-deployment'
OUTPUT = ROOT / 'deployments'
HOST_OS = {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}
HOST_ARCH = {'AMD64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'aarch64', 'aarch64': 'aarch64'}
SUPPORTED = {('macos', 'x86_64'): ('x86_64-apple-darwin', ['dmg', 'app.zip']),
             ('windows', 'x86_64'): ('x86_64-pc-windows-msvc', ['nsis.exe']),
             ('linux', 'x86_64'): ('x86_64-unknown-linux-gnu', ['deb', 'AppImage'])}


def profiles(root=PROFILES):
    if not root.is_dir():
        raise ValueError('OS-specs-for-deployment is missing.')
    result = []
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith('.')):
        manifest = folder / 'target.json'
        if not manifest.is_file():
            result.append({'name': folder.name, 'error': 'Missing target.json: inspect specs and define this target before building.'})
            continue
        try:
            value = json.loads(manifest.read_text())
            target, formats = SUPPORTED[(value['os'], value['arch'])]
            if value['target'] != target or not value.get('formats') or set(value['formats']) - set(formats):
                raise ValueError('Target/format mismatch.')
            if not value.get('minimum_os'):
                raise ValueError('minimum_os must be declared.')
            result.append({**value, 'name': folder.name})
        except (KeyError, ValueError, TypeError) as error:
            result.append({'name': folder.name, 'error': f'Invalid deployment target: {error}'})
    if not result:
        raise ValueError('No deployment profiles found.')
    return result


def run(command, **kwargs):
    print('+', ' '.join(str(item) for item in command), flush=True)
    return subprocess.run([str(item) for item in command], check=True, cwd=ROOT, **kwargs)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def build_kit(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = [Path(name) for name in ('package.json', 'package-lock.json', 'README.md', '.gitignore', 'index.html')]
    for directory in ['src', 'public', 'backend', 'scripts', 'deployment', '.github', 'src-tauri/src', 'src-tauri/icons', 'src-tauri/capabilities', 'tests']:
        files.extend(path.relative_to(ROOT) for path in (ROOT / directory).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts and path.name != '.DS_Store')
    files.extend(Path('src-tauri') / name for name in ['Cargo.toml', 'Cargo.lock', 'build.rs', 'tauri.conf.json'])
    files.extend(path.relative_to(ROOT) for path in PROFILES.glob('*/target.json'))
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            if (ROOT / path).is_file():
                archive.write(ROOT / path, path.as_posix())
    return destination


def make_disk_image(stage, destination):
    try:
        run(['hdiutil', 'create', '-volname', 'SweatyDL Next', '-srcfolder', stage, '-ov', '-format', 'UDZO', destination])
    except subprocess.CalledProcessError:
        # Sandboxed macOS may deny temporary disk attachment. HFS+ image creation
        # and UDIF compression can also run entirely in userspace, without a mount.
        raw = stage.parent / f'{stage.name}-raw.dmg'
        if raw.exists():
            raw.unlink()
        run(['hdiutil', 'makehybrid', '-hfs', '-hfs-volume-name', 'SweatyDL Next', '-o', raw, stage])
        run(['hdiutil', 'convert', raw, '-format', 'UDZO', '-o', destination, '-ov'])
    run(['hdiutil', 'verify', destination])


def build(profile, skip_backend=False):
    from package_backend import package, RESOURCES
    if not skip_backend:
        package()
    proof = json.loads((RESOURCES / 'build-info.json').read_text())
    if not proof.get('ok') or not proof.get('frozen') or proof.get('os') != platform.system() or HOST_ARCH.get(proof.get('arch')) != profile['arch']:
        raise RuntimeError('Standalone backend proof is missing or for another target.')
    version = json.loads((ROOT / 'package.json').read_text())['version']
    directory = OUTPUT / profile['name']
    directory.mkdir(parents=True, exist_ok=True)
    configuration = {'bundle': {'active': True, 'resources': {'resources/backend/': 'backend/'}}}
    if profile['os'] == 'macos':
        configuration['bundle']['macOS'] = {'minimumSystemVersion': profile['minimum_os']}
    elif profile['os'] == 'windows':
        configuration['bundle']['windows'] = {'minimumWebview2Version': '110.0.1587.0', 'webviewInstallMode': {'type': 'downloadBootstrapper', 'silent': True}}
    else:
        configuration['bundle']['linux'] = {'appimage': {'bundleMediaFramework': True}}
    config_path = ROOT / '.build' / f"tauri-{profile['name']}.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(configuration, indent=2))
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    if not npm:
        raise RuntimeError('npm is required on the build machine.')
    environment = dict(os.environ)
    if profile['os'] == 'macos':
        environment['MACOSX_DEPLOYMENT_TARGET'] = profile['minimum_os']
    bundles = {'macos': 'app', 'windows': 'nsis', 'linux': ','.join(profile['formats'])}
    run([npm, 'run', 'tauri', '--', 'build', '--features', 'bundled-backend', '--config', config_path,
         '--target', profile['target'], '--bundles', bundles[profile['os']]], env=environment)
    bundle = ROOT / 'src-tauri' / 'target' / profile['target'] / 'release' / 'bundle'
    artifacts = []
    if profile['os'] == 'macos':
        source = next((bundle / 'macos').glob('*.app'))
        app = directory / 'SweatyDL Next.app'
        if app.exists():
            shutil.rmtree(app)
        shutil.copytree(source, app, symlinks=True)
        embedded = app / 'Contents' / 'Resources' / 'backend'
        # Tauri resource copying can flatten symlinks; preserve the validated
        # PyInstaller framework layout explicitly before the final signature.
        if embedded.exists():
            shutil.rmtree(embedded)
        shutil.copytree(RESOURCES, embedded, symlinks=True)
        backend = embedded / 'sweatydl-backend'
        if not backend.is_file():
            raise RuntimeError('Backend missing from app bundle.')
        native = app / 'Contents' / 'MacOS' / 'sweatydl-next'
        result = subprocess.check_output([str(native), '--self-test'], cwd=directory, env={**os.environ, 'PATH':'/usr/bin:/bin'}, text=True, timeout=180)
        if not json.loads(result).get('ok'):
            raise RuntimeError('Backend self-test inside final .app failed.')
        run(['codesign', '--force', '--deep', '--sign', '-', app])
        run(['codesign', '--verify', '--deep', '--strict', app])
        archive = directory / f'SweatyDL-Next_{version}_{profile["name"]}_x64.app.zip'
        run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, archive])
        artifacts.append(archive)
        (directory / 'verified-artifacts.json').write_text(json.dumps([{'file': str(archive.relative_to(ROOT)), 'bytes': archive.stat().st_size, 'sha256': sha256(archive)}], indent=2))
        if 'dmg' in profile['formats']:
            stage = ROOT / '.build' / f'dmg-{profile["name"]}'
            if stage.exists():
                shutil.rmtree(stage)
            stage.mkdir(parents=True)
            (stage / 'Applications').symlink_to('/Applications')
            shutil.copytree(app, stage / app.name, symlinks=True)
            dmg = directory / f'SweatyDL-Next_{version}_{profile["name"]}_x64.dmg'
            make_disk_image(stage, dmg)
            artifacts.append(dmg)
    elif profile['os'] == 'windows':
        for source in (bundle / 'nsis').glob('*.exe'):
            destination = directory / f'SweatyDL-Next_{version}_{profile["name"]}_x64-setup.exe'
            shutil.copy2(source, destination)
            artifacts.append(destination)
        if not artifacts:
            raise RuntimeError('NSIS did not produce an installer.')
    else:
        for format_name in profile['formats']:
            sources = list((bundle / format_name.lower()).glob(f'*.{format_name}'))
            if not sources:
                raise RuntimeError(f'Tauri did not produce the requested {format_name} package.')
            for source in sources:
                destination = directory / f'SweatyDL-Next_{version}_{profile["name"]}_x64.{format_name}'
                shutil.copy2(source, destination)
                artifacts.append(destination)
    return [{'file': str(path.relative_to(ROOT)), 'bytes': path.stat().st_size, 'sha256': sha256(path)} for path in artifacts]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', help='Build a single profile; default: report/build every folder.')
    parser.add_argument('--matrix', action='store_true')
    parser.add_argument('--skip-backend', action='store_true', help='Reuse an already validated backend for this host.')
    args = parser.parse_args()
    targets = profiles()
    if args.target:
        targets = [item for item in targets if item['name'] == args.target]
        if not targets:
            parser.error('Unknown target folder.')
    if args.matrix:
        runners = {'macos': 'macos-15-intel', 'windows': 'windows-latest', 'linux': 'ubuntu-22.04'}
        if any('error' in item for item in targets):
            raise ValueError('Every profile needs a valid target.json before creating the build matrix.')
        print(json.dumps({'include': [{'profile': item['name'], 'runner': runners[item['os']]} for item in targets]}))
        return 0
    OUTPUT.mkdir(exist_ok=True)
    results = []
    for target in targets:
        result = {'profile': target['name'], 'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'host_os': platform.system(), 'host_arch': platform.machine(), 'artifacts': [],
                  'signed_with_developer_certificate': False, 'clean_machine_test': False}
        try:
            if target.get('error'):
                raise RuntimeError(target['error'])
            if (target['os'], target['arch']) != (HOST_OS.get(platform.system()), HOST_ARCH.get(platform.machine())):
                result.update(status='blocked', reason='Requires the matching native build OS/architecture; this host cannot freeze that Python backend.')
                kit = build_kit(OUTPUT / target['name'] / f"{target['name']}-build-kit.zip")
                result['build_kit'] = str(kit.relative_to(ROOT))
            else:
                result['artifacts'] = build(target, args.skip_backend)
                result['status'] = 'built-needs-installation-test'
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, StopIteration) as error:
            result.update(status='failed', reason=str(error))
            partial = OUTPUT / target['name'] / 'verified-artifacts.json'
            if partial.is_file():
                result['artifacts'] = json.loads(partial.read_text())
                result['status'] = 'partial-needs-installation-test'
        destination = OUTPUT / target['name']
        destination.mkdir(exist_ok=True)
        (destination / 'build-result.json').write_text(json.dumps(result, indent=2))
        results.append(result)
        print(json.dumps(result, indent=2), flush=True)
    (OUTPUT / 'build-results.json').write_text(json.dumps(results, indent=2))
    return 0 if all(result['status'] == 'built-needs-installation-test' for result in results) else 1


if __name__ == '__main__':
    sys.exit(main())
