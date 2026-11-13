"""Local checks for files that are about to enter public release artifacts."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import zipfile

PRIVATE_DATA = re.compile(
    rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\s+[A-Za-z0-9+/]{32}'
    rb'|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b')
LOCAL_HOME = re.compile(rb'(?:/Users/|/home/|[A-Z]:\\Users\\)(?!runner(?:[ /\\\x00]|$)|Public[ /\\\x00]|Default[ /\\\x00])[^\s/\\\x00]{2,}')


def _safe_label(value):
    text = str(value).replace('\\', '/')
    path = PurePosixPath(text)
    parts = path.parts
    if not parts or path.is_absolute() or '..' in parts:
        return '[entry]'
    if any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._+() -]{0,79}', part)
           or re.search(r'(?i)(?:token|secret|password|private|credential|(?:^|[._-])home(?:$|[._-]))', part)
           or re.search(r'(?i)^(?:home|users|runneradmin)$', part)
           or re.search(r'(?i)(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})', part)
           for part in parts):
        return '[entry]'
    return '/'.join(parts[-3:])[-120:]


def _scan_error(kind, label=None, offset=None):
    where = f' in {_safe_label(label)}' if label else ''
    if offset is not None:
        where += f' at byte offset {offset}'
    return ValueError(f'Artifact contains a {kind}{where}.')


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


def scan_stream(stream, label=None, *, home_paths=True):
    tail = b''
    digest = hashlib.sha256()
    home_offset = None
    scanned_bytes = 0
    while True:
        block = stream.read(1024 * 1024)
        if not block:
            if home_offset is not None:
                # Prepared Node binaries contain upstream build paths. Allow
                # only the exact reviewed images derived from signed archives;
                # any byte change restores the ordinary home-path rejection.
                pins = Path(__file__).resolve().parents[1] / 'deployment/toolchain.json'
                reviewed = json.loads(pins.read_text())['node']['platforms'] if pins.is_file() else {}
                if digest.hexdigest() not in {item.get('packaged_sha256', item['binary_sha256']) for item in reviewed.values()}:
                    raise _scan_error('local home-path pattern', label, home_offset)
            return
        digest.update(block)
        data = tail + block
        if PRIVATE_DATA.search(data):
            # Never include the match in errors or CI logs.
            raise _scan_error('credential pattern', label)
        match = LOCAL_HOME.search(data) if home_paths else None
        if home_offset is None and match:
            home_offset = scanned_bytes - len(tail) + match.start()
        scanned_bytes += len(block)
        tail = data[-4096:]


def scan_artifact(path):
    check_name(path.name)
    with path.open('rb') as stream:
        is_appimage = path.suffix == '.AppImage' and stream.read(4) == b'\x7fELF'
    if is_appimage:
        scan_appimage(path)
    elif path.name.endswith('.app.tar.gz'):
        with tarfile.open(path) as archive:
            for member in archive:
                check_name(member.name)
                if member.uid or member.gid or member.uname or member.gname:
                    raise ValueError('Updater archive contains machine-owner metadata.')
                if member.issym() or member.islnk():
                    check_link(member.name, member.linkname, member.islnk())
                elif member.isfile():
                    with archive.extractfile(member) as stream:
                        scan_stream(stream, member.name)
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
                        scan_stream(stream, member.filename)
    else:
        with path.open('rb') as stream:
            scan_stream(stream, path.name)


def appimage_offset(path):
    # Type-2 x64 AppImages append SquashFS after the ELF runtime. Derive its
    # boundary from ELF sections, rather than matching magic inside machine code.
    with path.open('rb') as stream:
        header = stream.read(64)
        if (len(header) != 64 or header[:7] != b'\x7fELF\x02\x01\x01'
                or header[8:11] != b'AI\x02'
                or struct.unpack_from('<H', header, 18)[0] != 62):
            raise ValueError('Unsupported AppImage runtime header.')
        table = struct.unpack_from('<Q', header, 40)[0]
        size, count = struct.unpack_from('<HH', header, 58)
        end = table + size * count
        limit = min(path.stat().st_size - 96, 16 * 1024 * 1024)
        if size != 64 or not count or table < 64 or end > limit:
            raise ValueError('Invalid AppImage section table.')
        stream.seek(table)
        for _ in range(count):
            section = struct.unpack('<IIQQQQIIQQ', stream.read(size))
            if section[1] != 8:  # SHT_NOBITS has no bytes in the file.
                end = max(end, section[4] + section[5])
            if end > limit:
                raise ValueError('Invalid AppImage section extent.')
        stream.seek(end)
        superblock = stream.read(96)
        if (superblock[:4] != b'hsqs'
                or struct.unpack_from('<HH', superblock, 28) != (4, 0)):
            raise ValueError('AppImage is missing its SquashFS v4 filesystem.')
        used = struct.unpack_from('<Q', superblock, 40)[0]
        if used < 96 or end + used > path.stat().st_size:
            raise ValueError('Invalid AppImage filesystem extent.')
        return end


def scan_appimage(path):
    offset = appimage_offset(path)
    with path.open('rb') as stream:
        # Keep the original credential check over every package byte, including
        # metadata and padding. Home paths are checked on decoded files so the
        # exact reviewed Node image can retain its existing hash-bound exception.
        scan_stream(stream, path.name, home_paths=False)
        stream.seek(0)
        scan_stream(io.BytesIO(stream.read(offset)), path.name)
        stream.seek(offset + 40)
        used = struct.unpack('<Q', stream.read(8))[0]
        stream.seek(offset + used)
        scan_stream(stream, path.name)
    extractor = shutil.which('unsquashfs')
    if not extractor:
        raise ValueError('AppImage inspection requires the system squashfs-tools package.')
    # Never execute an AppImage for inspection. Use the distribution's extractor
    # in a fresh private directory, without signing keys or inherited credentials.
    with tempfile.TemporaryDirectory(prefix='sweatydl-appimage-') as temporary:
        destination = Path(temporary) / 'filesystem'
        try:
            subprocess.run([extractor, '-no-progress', '-no-xattrs', '-o', str(offset),
                            '-d', str(destination), str(path.resolve())],
                           env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=180, check=True)
        except (OSError, subprocess.SubprocessError):
            raise ValueError('AppImage filesystem extraction failed.') from None
        if (destination.is_symlink() or not destination.is_dir()
                or not (destination / 'AppRun').exists()):
            raise ValueError('AppImage is missing its application layout.')
        scan_tree(destination)


def scan_tree(directory):
    for path in Path(directory).rglob('*'):
        check_name(path.relative_to(directory).as_posix())
        if path.is_symlink():
            if not path.resolve().is_relative_to(Path(directory).resolve()):
                raise ValueError('Packaged resource symlink escapes the application.')
        elif path.is_file():
            with path.open('rb') as stream:
                scan_stream(stream, path.relative_to(directory).as_posix())
        elif not path.is_dir():
            raise ValueError('Packaged resource contains a special file.')
