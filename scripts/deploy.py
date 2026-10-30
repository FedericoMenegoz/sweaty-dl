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
        extension = LINUX_BUNDLE_EXTENSIONS[format_name]
        sources = sorted((bundle / format_name).glob(f'*.{extension}'))
        if not sources:
            raise RuntimeError(f'Tauri did not produce the requested {format_name} package in {bundle / format_name}.')
        if len(sources) != 1:
            raise RuntimeError(f'Expected one {format_name} package, found {len(sources)}: {sources}')
        destination = directory / f'SweatyDL-Next_{version}_{profile["name"]}_x64.{extension}'
        shutil.copy2(sources[0], destination)
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
