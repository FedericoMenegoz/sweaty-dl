"""Local checks for files that are about to enter public release artifacts."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import tarfile
import zipfile

PRIVATE_DATA = re.compile(
    rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\s+[A-Za-z0-9+/]{32}'
    rb'|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b')
LOCAL_HOME = re.compile(rb'(?:/Users/|/home/|[A-Z]:\\Users\\)(?!runner(?:[ /\\]|$)|Public[ /\\]|Default[ /\\])[^\s/\\\x00]{2,}')


def check_name(name):
    path = PurePosixPath(name.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Archive contains an unsafe path.')
    for part in path.parts:
        lower = part.lower()
        if (lower in ('.release-keys', '.ssh', '.aws', '.git', '.npmrc', '.pypirc', '.netrc',
                      'pip.conf', 'id_rsa', 'id_ed25519', 'credentials', 'secrets')
                or lower == '.env' or lower.startswith('.env.')
                or lower.endswith(('.key', '.p12', '.pfx'))):
            raise ValueError('Artifact contains a credential-related filename.')


def check_link(name, linkname, hardlink=False):
    base = '' if hardlink else str(PurePosixPath(name).parent)
    target = posixpath.normpath(posixpath.join(base, linkname.replace('\\', '/')))
    check_name(target)
    parts = PurePosixPath(target).parts
    if not parts or parts[0] != PurePosixPath(name).parts[0]:
        raise ValueError('Artifact link escapes the application.')


def scan_stream(stream):
    tail = b''
    digest = hashlib.sha256()
    home_path = False
    while True:
        block = stream.read(1024 * 1024)
        if not block:
            if home_path:
                # Prepared Node binaries contain upstream build paths. Allow
                # only the exact reviewed images derived from signed archives;
                # any byte change restores the ordinary home-path rejection.
                pins = Path(__file__).resolve().parents[1] / 'deployment/toolchain.json'
                reviewed = json.loads(pins.read_text())['node']['platforms'] if pins.is_file() else {}
                if digest.hexdigest() not in {item.get('packaged_sha256', item['binary_sha256']) for item in reviewed.values()}:
                    raise ValueError('Artifact contains a credential pattern or a local user path.')
            return
        digest.update(block)
        data = tail + block
        if PRIVATE_DATA.search(data):
            # Never include the match in errors or CI logs.
            raise ValueError('Artifact contains a credential pattern or a local user path.')
        home_path |= bool(LOCAL_HOME.search(data))
        tail = data[-4096:]


def scan_artifact(path):
    check_name(path.name)
    if path.name.endswith('.app.tar.gz'):
        with tarfile.open(path) as archive:
            for member in archive:
                check_name(member.name)
                if member.uid or member.gid or member.uname or member.gname:
                    raise ValueError('Updater archive contains machine-owner metadata.')
                if member.issym() or member.islnk():
                    check_link(member.name, member.linkname, member.islnk())
                elif member.isfile():
                    with archive.extractfile(member) as stream:
                        scan_stream(stream)
                elif not member.isdir():
                    raise ValueError('Updater archive contains a special file.')
    elif path.name.endswith('.app.zip'):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                check_name(member.filename)
                if stat.S_ISLNK(member.external_attr >> 16):
                    check_link(member.filename, archive.read(member).decode('utf-8'))
                if not member.is_dir():
                    with archive.open(member) as stream:
                        scan_stream(stream)
    else:
        with path.open('rb') as stream:
            scan_stream(stream)


def scan_tree(directory):
    for path in Path(directory).rglob('*'):
        check_name(path.relative_to(directory).as_posix())
        if path.is_symlink():
            if not path.resolve().is_relative_to(Path(directory).resolve()):
                raise ValueError('Packaged resource symlink escapes the application.')
        elif path.is_file():
            with path.open('rb') as stream:
                scan_stream(stream)
