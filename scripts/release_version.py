"""Select release versions from Git tags and apply them only to the build checkout."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION_TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


def version_tags(tags):
    return {tag: tuple(map(int, match.groups())) for tag in tags
            if (match := VERSION_TAG.fullmatch(tag))}


def next_tag(tags, commit_tags):
    versions = version_tags(tags)
    existing = version_tags(commit_tags)
    if existing:
        return max(existing, key=existing.get)
    if not versions:
        return 'v1.0.0'
    major, minor, patch = max(versions.values())
    return f'v{major}.{minor}.{patch + 1}'


def apply_version(tag, root=ROOT):
    if not VERSION_TAG.fullmatch(tag):
        raise ValueError(f'Invalid release tag: {tag}')
    version = tag[1:]
    for name in ('package.json', 'package-lock.json', 'src-tauri/tauri.conf.json'):
        path = root / name
        value = json.loads(path.read_text())
        value['version'] = version
        if name == 'package-lock.json':
            value['packages']['']['version'] = version
        path.write_text(json.dumps(value, indent=2) + '\n')
    # Change only the application's package entry; preserve dependency versions
    # and lockfile checksums so cargo --locked continues to work.
    for name, section in (('src-tauri/Cargo.toml', r'\[package\]'),
                          ('src-tauri/Cargo.lock', r'\[\[package\]\]\s+name = "sweatydl-next"')):
        path = root / name
        value, count = re.subn(
            rf'({section}\s+[^\[]*?\bversion = ")[^"]+(")',
            lambda match: match[1] + version + match[2], path.read_text(), count=1)
        if count != 1:
            raise ValueError(f'Application version not found in {name}')
        path.write_text(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('next', 'apply', 'highest'))
    parser.add_argument('value', nargs='?', help='Source commit for next; vMAJOR.MINOR.PATCH for apply.')
    args = parser.parse_args()
    if args.command == 'highest':
        versions = version_tags(sys.stdin.read().splitlines())
        print(max(versions, key=versions.get, default=''))
    elif not args.value:
        parser.error('next and apply require a value')
    elif args.command == 'apply':
        apply_version(args.value)
    else:
        def tags(*options):
            return subprocess.check_output(['git', 'tag', '--list', *options],
                                           cwd=ROOT, text=True).splitlines()
        print(next_tag(tags(), tags('--points-at', args.value)))


if __name__ == '__main__':
    main()
