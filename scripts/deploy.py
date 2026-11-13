"""One deployment result per OS-specs-for-deployment folder; never fake foreign binaries."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / 'OS-specs-for-deployment'
OUTPUT = ROOT / 'deployments'
HOST_OS = {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}
HOST_ARCH = {'AMD64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'aarch64', 'aarch64': 'aarch64'}
SUPPORTED = {('macos', 'x86_64'): ('x86_64-apple-darwin', ['dmg', 'app.zip']),
             ('windows', 'x86_64'): ('x86_64-pc-windows-msvc', ['nsis.exe']),
             ('linux', 'x86_64'): ('x86_64-unknown-linux-gnu', ['deb', 'AppImage'])}

LINUX_BUNDLE_EXTENSIONS = {'deb': 'deb', 'AppImage': 'AppImage'}


def profiles(root=PROFILES):
    if not root.is_dir():
        raise ValueError('OS-specs-for-deployment is missing.')
    result = []
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith('.')):
        if folder.is_symlink() or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', folder.name):
            result.append({'name': folder.name, 'error': 'Profile names must contain only lowercase letters, digits, and hyphens.'})
            continue
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
    for directory in ['src', 'public', 'backend', 'scripts', 'deployment', 'docs', '.github', 'src-tauri/src', 'src-tauri/icons', 'src-tauri/capabilities', 'tests']:
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


def make_app_zip(app, destination):
    # Store Unix executable bits and symlinks, without Finder/resource-fork or
    # uid/gid extras that can disclose the machine that created the package.
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in [app, *sorted(app.rglob('*'))]:
            name = path.relative_to(app.parent).as_posix()
            if path.is_symlink():
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(entry, os.readlink(path))
            else:
                archive.write(path, name)


def prepare_update(profile, artifacts, version):
    if profile['os'] == 'macos':
        app = OUTPUT / profile['name'] / 'SweatyDL Next.app'
        update = app.parent / f'SweatyDL-Next_{version}_{profile["name"]}_x64.app.tar.gz'
        # Archive the final app AFTER backend symlinks are restored and codesign
        # succeeds, so the updater installs exactly the validated application.
        def neutral_metadata(entry):
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ''
            entry.pax_headers = {}
            entry.mode &= 0o755
            return entry
        with tarfile.open(update, 'w:gz') as archive:
            archive.add(app, arcname=app.name, filter=neutral_metadata)
        artifacts.append(update)


def build(profile, skip_backend=False, updater=False):
    from package_backend import package, RESOURCES, validate_release_tools, verified_toolchain
    if skip_backend and os.environ.get('SWEATYDL_RELEASE_BUILD') == '1':
        raise RuntimeError('Release builds require a freshly packaged backend from the current source.')
    if not skip_backend:
        package()
    proof = json.loads((RESOURCES / 'build-info.json').read_text())
    if not proof.get('ok') or not proof.get('frozen') or proof.get('os') != platform.system() or HOST_ARCH.get(proof.get('arch')) != profile['arch']:
        raise RuntimeError('Standalone backend proof is missing or for another target.')
    if os.environ.get('SWEATYDL_RELEASE_BUILD') == '1':
        validate_release_tools(proof)
        verified_toolchain({name: RESOURCES / '_internal/tools' / (name + ('.exe' if os.name == 'nt' else ''))
                            for name in ('node', 'ffprobe')}, proof.get('toolchain'))
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
    for key in list(environment):
        if key.startswith('TAURI_SIGNING_'):
            del environment[key]
    flags = environment.get('CARGO_ENCODED_RUSTFLAGS', '').split('\x1f') if environment.get('CARGO_ENCODED_RUSTFLAGS') else environment.get('RUSTFLAGS', '').split()
    flags.extend([f'--remap-path-prefix={Path.home()}=/build-home', f'--remap-path-prefix={ROOT}=/source'])
    environment['CARGO_ENCODED_RUSTFLAGS'] = '\x1f'.join(flags)
    if profile['os'] == 'macos':
        environment['MACOSX_DEPLOYMENT_TARGET'] = profile['minimum_os']
    bundles = {'macos': 'app', 'windows': 'nsis', 'linux': ','.join(format_name.lower() for format_name in profile['formats'])}
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
        clean_environment = {key: value for key, value in os.environ.items() if not key.startswith('TAURI_SIGNING_')}
        result = subprocess.check_output([str(native), '--self-test'], cwd=directory, env={**clean_environment, 'PATH':'/usr/bin:/bin'}, text=True, timeout=180)
        if not json.loads(result).get('ok'):
            raise RuntimeError('Backend self-test inside final .app failed.')
        from release_safety import scan_tree
        scan_tree(app)
        # Nested executables already have valid signatures. Signing only the
        # enclosing app preserves the exact reviewed standalone tool images.
        run(['codesign', '--force', '--sign', '-', app])
        run(['codesign', '--verify', '--deep', '--strict', app])
        archive = directory / f'SweatyDL-Next_{version}_{profile["name"]}_x64.app.zip'
        make_app_zip(app, archive)
        artifacts.append(archive)
        (directory / 'verified-artifacts.json').write_text(json.dumps([{'file': archive.relative_to(ROOT).as_posix(), 'bytes': archive.stat().st_size, 'sha256': sha256(archive)}], indent=2))
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
            extension = LINUX_BUNDLE_EXTENSIONS[format_name]
            sources = sorted((bundle / format_name.lower()).glob(f'*.{extension}'))
            if not sources:
                raise RuntimeError(f'Tauri did not produce the requested {format_name} package in {bundle / format_name.lower()}.')
            if len(sources) != 1:
                raise RuntimeError(f'Expected one {format_name} package, found {len(sources)}: {sources}')
            destination = directory / f'SweatyDL-Next_{version}_{profile["name"]}_x64.{extension}'
            shutil.copy2(sources[0], destination)
            artifacts.append(destination)
    if updater:
        prepare_update(profile, artifacts, version)
    return [{'file': path.relative_to(ROOT).as_posix(), 'bytes': path.stat().st_size, 'sha256': sha256(path)} for path in artifacts]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', help='Build a single profile; default: report/build every folder.')
    parser.add_argument('--matrix', action='store_true')
    parser.add_argument('--skip-backend', action='store_true', help='Reuse an already validated backend for this host.')
    parser.add_argument('--updater', action='store_true', help='Prepare updater packages; signing runs on an isolated runner.')
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
        partial = OUTPUT / target['name'] / 'verified-artifacts.json'
        # A failed attempt must never report another run's validated package.
        if partial.exists() or partial.is_symlink():
            partial.unlink()
        result = {'profile': target['name'], 'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'version': json.loads((ROOT / 'package.json').read_text())['version'],
                  'source_commit': os.environ.get('GITHUB_SHA'),
                  'host_os': platform.system(), 'host_arch': platform.machine(), 'artifacts': [],
                  'signed_with_developer_certificate': False, 'clean_machine_test': False}
        try:
            if target.get('error'):
                raise RuntimeError(target['error'])
            if (target['os'], target['arch']) != (HOST_OS.get(platform.system()), HOST_ARCH.get(platform.machine())):
                result.update(status='blocked', reason='Requires the matching native build OS/architecture; this host cannot freeze that Python backend.')
                kit = build_kit(OUTPUT / target['name'] / f"{target['name']}-build-kit.zip")
                result['build_kit'] = kit.relative_to(ROOT).as_posix()
            else:
                result['artifacts'] = build(target, args.skip_backend, args.updater)
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
