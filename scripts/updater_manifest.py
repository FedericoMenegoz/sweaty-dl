"""Create Tauri's update feed from the already verified release assets."""
import argparse
import json
from pathlib import Path
import re
import shutil
from urllib.parse import quote

from deploy import profiles, sha256
from release_version import VERSION_TAG
from release_safety import scan_artifact


def update_formats(profile):
    os_name = 'darwin' if profile['os'] == 'macos' else profile['os']
    base = f'{os_name}-{profile["arch"]}'
    if profile['os'] == 'linux':
        return {'.' + name: base + '-' + name.lower() for name in profile['formats']}
    return {'.app.tar.gz' if profile['os'] == 'macos' else '-setup.exe': base}


def expected_assets(profile, version, signed=True):
    prefix = f'SweatyDL-Next_{version}_{profile["name"]}_x64'
    extensions = set(update_formats(profile))
    if profile['os'] == 'macos':
        extensions.add('.app.zip')
        if 'dmg' in profile['formats']:
            extensions.add('.dmg')
    names = {prefix + extension for extension in extensions}
    if signed:
        names.update(prefix + extension + '.sig' for extension in update_formats(profile))
    return names


def stage_assets(source, destination, tag, commit, targets, signed=True, prefix='SweatyDL-'):
    """Assemble all packages for this source commit before any remote mutation."""
    if not VERSION_TAG.fullmatch(tag) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit):
        raise ValueError('Invalid release version or source commit.')
    if not targets or any('error' in target for target in targets):
        raise ValueError('Release requires valid deployment profiles.')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Release staging directory must be empty.')
    destination.mkdir(parents=True, exist_ok=True)
    for profile in targets:
        name = profile['name']
        directory = source / f'{prefix}{name}'
        manifest = directory / 'build-result.json'
        result = json.loads(manifest.read_text())
        if (result.get('profile') != name or result.get('version') != tag[1:]
                or result.get('source_commit') != commit
                or result.get('status') != 'built-needs-installation-test'):
            raise ValueError(f'Build metadata does not match this release: {name}')
        artifacts = result.get('artifacts', [])
        names = [Path(item['file']).name for item in artifacts]
        if len(set(names)) != len(names) or set(names) != expected_assets(profile, tag[1:], signed):
            raise ValueError(f'Build must contain every required release package and signature: {name}')
        for item, filename in zip(artifacts, names):
            path = directory / filename
            if (item['file'] != f'deployments/{name}/{filename}' or path.is_symlink()
                    or not path.is_file() or path.stat().st_size != item.get('bytes')
                    or sha256(path) != item.get('sha256')):
                raise ValueError(f'Package verification failed: {name}/{filename}')
            if (destination / filename).exists():
                raise ValueError(f'Duplicate release asset: {filename}')
            try:
                scan_artifact(path)
            except ValueError as error:
                # `filename` already matched the canonical release asset set;
                # the scanner message carries only a safe member basename and
                # violation class, never matching bytes or an absolute path.
                raise ValueError(f'{error} Release asset: {filename}.') from None
            shutil.copyfile(path, destination / filename)
        shutil.copyfile(manifest, destination / f'{name}-build-result.json')


def create_manifest(assets, tag, repository, targets, verify=False):
    if not VERSION_TAG.fullmatch(tag):
        raise ValueError('Invalid release version.')
    platforms = {}
    for profile in targets:
        result = json.loads((assets / f'{profile["name"]}-build-result.json').read_text())
        if result['version'] != tag[1:] or result['status'] != 'built-needs-installation-test':
            raise ValueError('Only successful builds of the release version can be updates.')
        for extension, target in update_formats(profile).items():
            packages = [Path(item['file']).name for item in result['artifacts'] if item['file'].endswith(extension)]
            if len(packages) != 1:
                raise ValueError(f'Expected exactly one {extension} updater package for {profile["name"]}.')
            package = packages[0]
            signature = assets / (package + '.sig')
            listed = {Path(item['file']).name for item in result['artifacts']}
            if not (assets / package).is_file() or signature.name not in listed:
                raise ValueError('Updater package and signature must be verified release artifacts.')
            signed = signature.read_text().strip()
            if not signed:
                raise ValueError('Missing updater signature.')
            if verify:
                from sign_release import verify_signature
                verify_signature(assets / package, tag[1:])
            if target in platforms:
                raise ValueError(f'Multiple profiles produce updates for {target}.')
            platforms[target] = {
                'url': f'https://github.com/{repository}/releases/download/{tag}/{quote(package)}',
                'signature': signed,
            }
    if not platforms:
        raise ValueError('No update platforms.')
    return {'version': tag[1:], 'notes': f'SweatyDL {tag}', 'platforms': platforms}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, default=Path('release-assets'))
    parser.add_argument('--tag')
    parser.add_argument('--repository')
    parser.add_argument('--source', type=Path, help='Downloaded workflow artifacts to verify and stage.')
    parser.add_argument('--commit', help='Exact source commit required when staging packages.')
    parser.add_argument('--stage-build', help='Stage only one unsigned native build for CI upload.')
    parser.add_argument('--verify-signatures', action='store_true')
    args = parser.parse_args()
    targets = profiles()
    if args.stage_build:
        targets = [profile for profile in targets if profile['name'] == args.stage_build]
        tag = args.tag or 'v' + json.loads((Path(__file__).resolve().parents[1] / 'package.json').read_text())['version']
        if not args.source or not args.commit:
            parser.error('--stage-build requires --source and --commit')
        stage_assets(args.source, args.assets, tag, args.commit, targets, signed=False, prefix='')
        (args.assets / f'{args.stage_build}-build-result.json').rename(args.assets / 'build-result.json')
        raise SystemExit(0)
    if not args.tag or not args.repository:
        parser.error('--tag and --repository are required')
    if args.source:
        if not args.commit:
            parser.error('--source requires --commit')
        stage_assets(args.source, args.assets, args.tag, args.commit, targets)
    manifest = create_manifest(args.assets, args.tag, args.repository, targets, args.verify_signatures)
    (args.assets / 'latest.json').write_text(json.dumps(manifest, indent=2) + '\n')
