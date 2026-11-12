"""Sign verified native packages on an isolated runner, without executing them."""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from deploy import profiles, sha256
from updater_manifest import stage_assets, update_formats

ROOT = Path(__file__).resolve().parents[1]


def public_key():
    return json.loads((ROOT / 'src-tauri/tauri.conf.json').read_text())['plugins']['updater']['pubkey']


def verify_signature(package, version):
    """Minisign verifies the package AND trusted comment before version comparison."""
    try:
        encoded = package.with_name(package.name + '.sig').read_text().strip()
        signature = base64.b64decode(encoded, validate=True).decode('utf-8')
        public = base64.b64decode(public_key(), validate=True).decode('utf-8')
        lines = signature.splitlines()
        if len(lines) != 4 or not lines[2].startswith('trusted comment: '):
            raise ValueError('Invalid signature encoding.')
        with tempfile.TemporaryDirectory(prefix='verify-updater-') as directory:
            key_path, signature_path = Path(directory) / 'public', Path(directory) / 'signature'
            key_path.write_text(public)
            signature_path.write_text(signature)
            environment = {key: value for key, value in os.environ.items() if not key.startswith('TAURI_SIGNING_')}
            subprocess.run(['minisign', '-V', '-q', '-m', str(package), '-p', str(key_path),
                            '-x', str(signature_path)], check=True, env=environment,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        fields = lines[2][len('trusted comment: '):].split('\t')
        if fields.count('version:' + version) != 1 or sum(part.startswith('version:') for part in fields) != 1:
            raise ValueError('Signature version does not match the release.')
    except (ValueError, OSError, UnicodeError, subprocess.SubprocessError):
        raise RuntimeError(f'Update signature verification failed: {package.name}') from None


def sign_packages(source, destination, tag, commit):
    if os.environ.get('GITHUB_ACTIONS') == 'true' and (
            os.environ.get('GITHUB_EVENT_NAME') != 'push'
            or os.environ.get('GITHUB_REF') != 'refs/heads/deployment'):
        raise RuntimeError('CI signing is restricted to deployment branch pushes.')
    key = os.environ.get('TAURI_SIGNING_PRIVATE_KEY', '').strip()
    if not key:
        raise RuntimeError('Configure the signing secret in the release environment.')
    cli = ROOT / 'node_modules/.bin/tauri'
    if not cli.is_file() or not shutil.which('minisign'):
        raise RuntimeError('The locked Tauri CLI and Minisign verifier are required.')
    targets = profiles()
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='sign-updater-') as directory:
        assets = Path(directory)
        stage_assets(source, assets, tag, commit, targets, signed=False)
        for profile in targets:
            manifest = assets / f'{profile["name"]}-build-result.json'
            result = json.loads(manifest.read_text())
            packages = [item for item in result['artifacts']
                        if any(item['file'].endswith(extension) for extension in update_formats(profile))]
            for item in packages:
                package = assets / Path(item['file']).name
                environment = {name: value for name, value in os.environ.items()
                               if name in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'SYSTEMROOT')}
                environment['TAURI_SIGNING_PRIVATE_KEY'] = key
                environment['TAURI_SIGNING_PRIVATE_KEY_PASSWORD'] = os.environ.get('TAURI_SIGNING_PRIVATE_KEY_PASSWORD', '')
                try:
                    subprocess.run([str(cli), 'signer', 'sign', '--app-version', tag[1:], str(package)],
                                   check=True, env=environment, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=180)
                except subprocess.SubprocessError:
                    raise RuntimeError(f'Updater signing failed: {package.name}') from None
                verify_signature(package, tag[1:])
                signature = package.with_name(package.name + '.sig')
                result['artifacts'].append({'file': f'deployments/{profile["name"]}/{signature.name}',
                                            'bytes': signature.stat().st_size, 'sha256': sha256(signature)})
            output = destination / f'SweatyDL-{profile["name"]}'
            output.mkdir()
            for item in result['artifacts']:
                filename = Path(item['file']).name
                shutil.copyfile(assets / filename, output / filename)
            (output / 'build-result.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('deployments'))
    parser.add_argument('--output', type=Path, default=Path('signed-deployments'))
    parser.add_argument('--tag', required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    sign_packages(args.source, args.output, args.tag, args.commit)
