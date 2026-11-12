"""Prepare checksum-pinned Node.js and a minimal native FFprobe release toolchain."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import wave
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'deployment' / 'toolchain.json'
REVIEW_CACHE = ROOT / '.build' / 'release-tools' / 'review'
OUTPUT_ROOT = ROOT / '.build' / 'release-tools'
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30

FFPROBE_CONFIG = [
    '--disable-autodetect', '--disable-network', '--disable-protocols',
    '--enable-protocol=file', '--disable-encoders', '--disable-decoders',
    '--disable-muxers', '--disable-filters', '--disable-bsfs', '--disable-devices',
    '--disable-indevs', '--disable-outdevs', '--disable-avdevice', '--disable-avfilter',
    '--disable-swresample', '--disable-swscale', '--disable-ffmpeg', '--disable-ffplay',
    '--enable-ffprobe', '--disable-doc', '--disable-debug', '--enable-small',
    '--disable-x86asm', '--enable-static', '--disable-shared', '--disable-gpl',
    '--disable-nonfree',
]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _hash(value, field):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value):
        raise RuntimeError(f'Invalid pinned SHA-256 for {field}.')
    return value.lower()


def _safe_name(value):
    if not isinstance(value, str) or Path(value).name != value or value in ('', '.', '..'):
        raise RuntimeError('Invalid archive filename in toolchain configuration.')
    return value


def _validate_member_name(name):
    normalized = name.replace('\\', '/')
    path = PurePosixPath(normalized)
    if not path.parts or path.is_absolute() or any(part in ('..', '') for part in path.parts) or ':' in path.parts[0]:
        raise RuntimeError('Tool archive contains an unsafe path.')
    return path


def _safe_link(name, target, hardlink=False):
    base = PurePosixPath() if hardlink else PurePosixPath(name).parent
    resolved = PurePosixPath(posixpath.normpath(str(base / target.replace('\\', '/'))))
    if resolved.is_absolute() or any(part in ('..', '') for part in resolved.parts):
        raise RuntimeError('Tool archive contains an unsafe link.')
    if not resolved.parts or resolved.parts[0] != PurePosixPath(name).parts[0]:
        raise RuntimeError('Tool archive link escapes its root directory.')


def _verify_archive(path, expected_hash, max_bytes=MAX_ARCHIVE_BYTES):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > max_bytes:
        raise RuntimeError('Tool archive is missing, linked, or exceeds the size limit.')
    if sha256_file(path) != _hash(expected_hash, path.name):
        raise RuntimeError('Tool archive checksum verification failed.')
    return path


def _download(url, destination, expected_hash, max_bytes=MAX_ARCHIVE_BYTES):
    if not url.startswith('https://'):
        raise RuntimeError('Tool archive URL must use HTTPS.')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.download-', dir=destination.parent)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, 'wb') as output, urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
            length = response.headers.get('Content-Length')
            if length and int(length) > max_bytes:
                raise RuntimeError('Tool archive exceeds the size limit.')
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise RuntimeError('Tool archive exceeds the size limit.')
                digest.update(block)
                output.write(block)
        if digest.hexdigest() != _hash(expected_hash, destination.name):
            raise RuntimeError('Downloaded tool archive checksum verification failed.')
        os.replace(temporary, destination)
        return destination
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _cache_archive(url, name, digest, max_bytes=MAX_ARCHIVE_BYTES):
    path = REVIEW_CACHE / _safe_name(name)
    if path.exists():
        return _verify_archive(path, digest, max_bytes)
    return _download(url, path, digest, max_bytes)


def _tar_members(archive, max_total=MAX_SOURCE_BYTES):
    total = 0
    members = []
    for member in archive:
        path = _validate_member_name(member.name)
        if member.issym():
            _safe_link(str(path), member.linkname)
        elif member.islnk():
            _safe_link(str(path), member.linkname, hardlink=True)
        elif not (member.isdir() or member.isfile()):
            raise RuntimeError('Tool archive contains a special file.')
        if member.isfile():
            total += member.size
            if member.size < 0 or total > max_total:
                raise RuntimeError('Tool archive expands beyond the size limit.')
        members.append((member, path))
    return members


def extract_node_archive(archive_path, platform_name, destination, expected_hash):
    archive_path = Path(archive_path)
    _verify_archive(archive_path, expected_hash)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    binary_name = 'node.exe' if platform_name == 'Windows' else 'node'
    root = archive_path.name
    for suffix in ('.tar.xz', '.tar.gz', '.zip'):
        if root.endswith(suffix):
            root = root[:-len(suffix)]
            break
    selected = {}
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            for item in archive.infolist():
                path = _validate_member_name(item.filename)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    _safe_link(str(path), archive.read(item).decode('utf-8'))
                    continue
                parts = path.parts
                if len(parts) == 2 and parts[0] == root and parts[1] in (binary_name, 'LICENSE'):
                    if item.is_dir():
                        raise RuntimeError('Selected Node archive member is not a regular file.')
                    if parts[1] in selected:
                        raise RuntimeError('Pinned Node archive contains duplicate selected members.')
                    selected[parts[1]] = archive.read(item)
    else:
        with tarfile.open(archive_path, 'r:*') as archive:
            for item, path in _tar_members(archive):
                parts = path.parts
                if item.isfile() and len(parts) == 3 and parts[0] == root and parts[1] == 'bin' and parts[2] == binary_name:
                    if binary_name in selected:
                        raise RuntimeError('Pinned Node archive contains duplicate selected members.')
                    selected[binary_name] = archive.extractfile(item).read()
                elif item.isfile() and len(parts) == 2 and parts[0] == root and parts[1] == 'LICENSE':
                    if 'LICENSE' in selected:
                        raise RuntimeError('Pinned Node archive contains duplicate selected members.')
                    selected['LICENSE'] = archive.extractfile(item).read()
    if binary_name not in selected or 'LICENSE' not in selected:
        raise RuntimeError('Pinned Node archive lacks the expected executable or license.')
    binary = destination / binary_name
    binary.write_bytes(selected[binary_name])
    if platform_name != 'Windows':
        binary.chmod(0o755)
    return binary, selected['LICENSE']


def _extract_source(archive_path, destination, expected_hash):
    _verify_archive(archive_path, expected_hash, MAX_ARCHIVE_BYTES)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, 'r:*') as archive:
        members = _tar_members(archive)
        for item, relative in members:
            if item.issym() or item.islnk():
                raise RuntimeError('FFmpeg source archive contains a link; extraction refused.')
            if not relative.parts or relative.parts[0] != archive_path.name.removesuffix('.tar.xz'):
                raise RuntimeError('FFmpeg source archive has an unexpected root directory.')
            target = destination.joinpath(*relative.parts)
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(item)
                with target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if item.mode & 0o111 else 0o644)
    source = destination / archive_path.name.removesuffix('.tar.xz')
    for required in ('configure', 'COPYING.LGPLv2.1'):
        if not (source / required).is_file():
            raise RuntimeError('FFmpeg source archive is missing a required build or license file.')
    return source


def _clean_environment(work, platform_name, compiler_dir=None):
    env = {}
    for key in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'SDKROOT', 'DEVELOPER_DIR'):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if platform_name == 'Windows':
        msys = Path(r'C:\msys64')
        env.update({'MSYSTEM': 'MSYS', 'CHERE_INVOKING': 'yes', 'HOME': str(work),
                    'PATH': r'C:\mingw64\bin' + ';' + str(msys / 'usr' / 'bin'),
                    'SHELL': str(msys / 'usr' / 'bin' / 'bash.exe')})
    else:
        dirs = [str(compiler_dir)] if compiler_dir else []
        search_path = list(dict.fromkeys(dirs + ['/usr/bin', '/bin']))
        env.update({'PATH': os.pathsep.join(search_path), 'HOME': str(work),
                    'TMPDIR': str(work), 'LC_ALL': 'C', 'SOURCE_DATE_EPOCH': '0'})
    mapped_work = str(work).replace('\\', '/')
    env['CFLAGS'] = f'-O2 -g0 -ffile-prefix-map={mapped_work}=. -fdebug-prefix-map={mapped_work}=. -fmacro-prefix-map={mapped_work}=.'
    return env


def _native_tools(platform_name):
    if platform_name == 'Windows':
        msys = Path(r'C:\msys64')
        bash = msys / 'usr' / 'bin' / 'bash.exe'
        # runner-images installs the UCRT MinGW compiler and GNU Make here,
        # separately from the base MSYS2 shell (which has no compiler packages).
        gcc = Path(r'C:\mingw64\bin\gcc.exe')
        make = Path(r'C:\mingw64\bin\make.exe')
        for tool in (bash, gcc, make):
            if not tool.is_file():
                raise RuntimeError('Windows release runner lacks the expected MSYS2 Bash or MinGW GCC/Make tools; no packages were installed.')
        return bash, gcc, make, gcc.parent
    cc_name = 'clang' if platform_name == 'Darwin' else 'gcc'
    compiler = shutil.which(cc_name)
    make = shutil.which('make')
    bash = shutil.which('bash')
    if not compiler or not make or not bash:
        raise RuntimeError('Native FFmpeg build requires an installed compiler, GNU make, and bash.')
    return Path(bash), Path(compiler), Path(make), Path(compiler).parent


def _safe_build_diagnostics(output, env=None, cwd=None, configure_log=''):
    if isinstance(output, bytes):
        output = output.decode('utf-8', errors='replace')
    if isinstance(configure_log, bytes):
        configure_log = configure_log.decode('utf-8', errors='replace')
    combined = output + '\n' + configure_log
    if re.search(r'(?im)-----BEGIN [^-]*PRIVATE KEY-----|-----END [^-]*PRIVATE KEY-----', combined):
        return ['Compiler diagnostics withheld because a private-key marker was detected.']
    sensitive_paths = [str(cwd)] if cwd else []
    sensitive_paths.extend(str(env[key]) for key in ('HOME', 'TEMP', 'TMP', 'TMPDIR',
                                                     'GITHUB_WORKSPACE', 'RUNNER_TEMP')
                           if env and env.get(key))
    def sanitize(raw):
        line = ''.join(char if char in '\t' or ord(char) >= 32 else ' ' for char in raw)
        if re.search(r'(?i)\b(?:token|secret|password|private[_ -]?key|authorization|bearer)\b\s*(?:[:=]|\s)|'
                     r'\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b', line):
            return None
        for path in sensitive_paths:
            line = re.sub(re.escape(path), '[PATH]', line, flags=re.I)
        line = re.sub(r'https?://[^\s<>"\']+', '[URL]', line, flags=re.I)
        line = re.sub(r'(?i)(?:[A-Z]:[\\/]|/)(?:[^\s<>"\'(),;]+[\\/])*[^\s<>"\'(),;]+',
                      '[PATH]', line)
        return line[:300]

    diagnostic = re.compile(r'(?i)(?:\berror\b|\bwarning\b|undefined reference|cannot find|'
                            r'not found|no such file|permission denied|failed|unable|lacks|unrecognized|collect2:|ld\.exe:)')
    stdout_lines = [safe for raw in output.splitlines() if diagnostic.search(raw)
                    for safe in [sanitize(raw)] if safe]
    log_lines = configure_log.splitlines()
    markers = [i for i, line in enumerate(log_lines)
               if re.search(r'\btest_cflags_cc\s+-std=c(?:17|11)\b', line)]
    context = []
    if markers:
        for index in markers[-2:]:
            following = next((position for position in markers if position > index), index + 30)
            # FFmpeg logs the generated source between test_cc and the actual
            # compiler command. Retain commands/errors, not the source dump.
            block = log_lines[index:min(following, index + 30)]
            context.extend(line for line in block if '-std=c17' in line or '-std=c11' in line
                           or (diagnostic.search(line) and not re.match(r'\s*\d+\s', line)))
        failure = next((line for line in reversed(log_lines)
                        if re.search(r'Compiler lacks C11 support|ERROR:.*failed', line, re.I)), None)
        if failure:
            context.append(failure)
    else:
        context = [line for line in log_lines[-40:]
                   if re.search(r'(?i)(?:\berror\b|\bwarning\b|undefined reference|cannot find|'
                                r'not found|no such file|permission denied|failed|unable|unrecognized|collect2:|ld\.exe:)', line)]
    safe_context = [line for line in (sanitize(line) for line in context) if line]
    lines = [f'stdout: {line}' for line in stdout_lines[-3:]]
    if safe_context:
        lines.extend(f'configure log: {line}' for line in safe_context[-10:])
    return [line[:300] for line in lines[-15:]] or ['No safe compiler/configure diagnostics were available.']


def _run(command, cwd, env, bash=None, windows=False, phase='configure/make'):
    if windows:
        if phase == 'configure':
            script = 'export PATH=/c/mingw64/bin:/usr/bin; ./configure "$@"'
        else:
            script = 'export PATH=/c/mingw64/bin:/usr/bin; make "$@" SHELL=C:/msys64/usr/bin/bash.exe'
        args = [str(bash), '--noprofile', '--norc', '-c', script, 'ffprobe-build', *command[1:]]
    else:
        args = command
    try:
        return subprocess.run(args, cwd=cwd, env=env, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=900)
    except (OSError, subprocess.SubprocessError) as error:
        output = getattr(error, 'stdout', None) or b''
        if isinstance(output, str):
            output = output.encode()
        detail = Path(cwd) / 'ffbuild/config.log'
        configure_log = b''
        if phase == 'configure' and detail.is_file() and not detail.is_symlink():
            # Configure's stdout often says only "compiler test failed". Read
            # the bounded C-standard probe context from its detailed log.
            with detail.open('rb') as source:
                source.seek(max(0, detail.stat().st_size - 65536))
                configure_log = source.read(65536)
        private_output = output + (b'\n' + configure_log if configure_log else b'')
        if private_output:
            log = OUTPUT_ROOT / 'build-failure.log'
            descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(descriptor, 'wb') as private_log:
                private_log.write(private_output)
        code = getattr(error, 'returncode', None) or getattr(error, 'errno', None)
        print(f'FFmpeg source {phase} failed (code {code}); sanitized diagnostics:', file=sys.stderr)
        for line in _safe_build_diagnostics(output, env, cwd, configure_log):
            print(line, file=sys.stderr)
        raise RuntimeError(f'FFmpeg source {phase} failed; full output was retained only on the runner when available.') from None


def _compiler_identity(compiler, env):
    try:
        line = subprocess.check_output([str(compiler), '--version'], env=env, text=True,
                                       stderr=subprocess.DEVNULL, timeout=15).splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        raise RuntimeError('Could not identify the native FFmpeg compiler.') from None
    match = re.search(r'(?:gcc(?:\.exe)?|clang(?:\.exe)?)(?: \([^\r\n]*?\))? (?:version )?[0-9][A-Za-z0-9.+-]*', line, re.I)
    if not match:
        match = re.search(r'Apple clang version [0-9][A-Za-z0-9.+-]*', line)
    if not match:
        raise RuntimeError('Compiler identity is not in the approved version format.')
    try:
        target = subprocess.check_output([str(compiler), '-dumpmachine'], env=env, text=True,
                                          stderr=subprocess.DEVNULL, timeout=15).strip().lower()
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('Could not verify the native compiler target architecture.') from None
    if not target.startswith('x86_64-'):
        raise RuntimeError('Release tool compiler is not targeting x86_64.')
    return match.group(0)[:160]


def _verify_macos_dependencies(binary, env):
    try:
        lines = subprocess.check_output(['otool', '-L', str(binary)], env=env, text=True,
                                        stderr=subprocess.DEVNULL, timeout=15).splitlines()[1:]
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('Could not inspect macOS FFprobe dependencies.') from None
    for line in lines:
        dependency = line.strip().split(' (', 1)[0]
        if not dependency.startswith(('/usr/lib/', '/System/Library/')):
            raise RuntimeError('macOS FFprobe has a non-system dynamic library dependency.')


def _smoke_test(node, ffprobe, expected_node, expected_ffprobe, env, work):
    node_version = subprocess.check_output([str(node), '--version'], env=env, text=True, timeout=15).strip()
    if node_version != f'v{expected_node}':
        raise RuntimeError('Prepared Node executable does not match the pinned version.')
    probe_version = subprocess.check_output([str(ffprobe), '-version'], env=env, text=True, timeout=15).splitlines()[0]
    if not re.search(rf'\bversion (?:n)?{re.escape(expected_ffprobe)}\b', probe_version):
        raise RuntimeError('Prepared FFprobe executable does not match the pinned version.')
    protocols = subprocess.check_output([str(ffprobe), '-protocols'], env=env, text=True, timeout=15)
    section = None
    supported = {'input': [], 'output': []}
    for line in protocols.splitlines():
        stripped = line.strip()
        if stripped.lower() == 'input:':
            section = 'input'
        elif stripped.lower() == 'output:':
            section = 'output'
        elif section and stripped:
            supported[section].append(stripped)
    if set(supported['input'] + supported['output']) != {'file'}:
        raise RuntimeError('Prepared FFprobe does not have a file-only protocol build.')
    fixture = Path(work) / 'duration-check.wav'
    with wave.open(str(fixture), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 16000)
    result = subprocess.check_output([str(ffprobe), '-v', 'error', '-protocol_whitelist', 'file',
        '-show_entries', 'format=duration', '-of', 'json', str(fixture)], env=env, text=True,
        timeout=20)
    duration = float(json.loads(result)['format']['duration'])
    if abs(duration - 2.0) > .01:
        raise RuntimeError('Prepared FFprobe failed the local duration smoke test.')


def _export_github_tools(node, ffprobe, proof_path, bin_dir):
    values = {'SWEATYDL_NODE': node, 'SWEATYDL_FFPROBE': ffprobe,
              'SWEATYDL_TOOLCHAIN': proof_path}
    for name, path in values.items():
        value = str(path)
        if '\n' in value or '\r' in value:
            raise RuntimeError('Release tool path cannot be safely exported to GitHub Actions.')
        github_env = os.environ.get('GITHUB_ENV')
        if github_env:
            with Path(github_env).open('a', encoding='utf-8') as stream:
                stream.write(f'{name}={value}\n')
    github_path = os.environ.get('GITHUB_PATH')
    if github_path:
        value = str(bin_dir)
        if '\n' in value or '\r' in value:
            raise RuntimeError('Release tool path cannot be safely exported to GitHub Actions.')
        with Path(github_path).open('a', encoding='utf-8') as stream:
            stream.write(f'{value}\n')


def prepare(config_path=CONFIG, system=None):
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    system = system or platform.system()
    if system not in ('Darwin', 'Linux', 'Windows'):
        raise RuntimeError('Release tools support macOS, Linux, and Windows hosts only.')
    if platform.machine().lower() not in ('x86_64', 'amd64'):
        raise RuntimeError('Release tool preparation requires an x86_64 native runner.')
    node_cfg = config['node']
    probe_cfg = config['ffprobe']
    node_version = node_cfg['version']
    probe_version = probe_cfg['version']
    if not all(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value) for value in (node_version, probe_version)):
        raise RuntimeError('Invalid pinned release tool version.')
    platform_cfg = node_cfg['platforms'][system]
    archive_name = _safe_name(platform_cfg['archive'])
    node_hash = _hash(platform_cfg['sha256'], f'{system} Node archive')
    node_binary_hash = _hash(platform_cfg['binary_sha256'], f'{system} Node executable')
    node_url = f'https://nodejs.org/dist/v{node_version}/{archive_name}'
    source_url = probe_cfg['url']
    source_name = _safe_name(Path(source_url).name)
    source_hash = _hash(probe_cfg['sha256'], 'FFmpeg source archive')
    if source_url != f'https://ffmpeg.org/releases/ffmpeg-{probe_version}.tar.xz':
        raise RuntimeError('FFmpeg source URL is not the pinned official release archive.')
    node_archive = _cache_archive(node_url, archive_name, node_hash)
    source_archive = _cache_archive(source_url, source_name, source_hash, MAX_ARCHIVE_BYTES)
    host = f'{system}-{platform.machine().lower()}'
    output = OUTPUT_ROOT / host
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    bin_dir = output / 'bin'
    notices = output / 'notices'
    proof_path = output / 'toolchain.json'
    if proof_path.exists() or proof_path.is_symlink():
        proof_path.unlink()
    node_dir = bin_dir
    node_path, node_license = extract_node_archive(node_archive, system, node_dir, node_hash)
    if sha256_file(node_path) != node_binary_hash:
        raise RuntimeError('Extracted Node executable checksum verification failed.')
    if system == 'Darwin':
        node_env = _clean_environment(output, system)
        subprocess.run(['codesign', '--force', '--sign', '-', str(node_path)], env=node_env,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['codesign', '--verify', '--strict', str(node_path)], env=node_env,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _verify_macos_dependencies(node_path, node_env)
    if sha256_file(node_path) != platform_cfg.get('packaged_sha256', node_binary_hash):
        raise RuntimeError('Prepared Node image differs from its reviewed packaged checksum.')
    node_url = f'https://nodejs.org/dist/v{node_version}/{archive_name}'
    notices.mkdir(parents=True, exist_ok=True)
    (notices / 'node').mkdir(exist_ok=True)
    (notices / 'node' / 'LICENSE').write_bytes(node_license)
    with tempfile.TemporaryDirectory(prefix='sweatydl-ffmpeg-', dir=OUTPUT_ROOT) as temporary:
        work = Path(temporary)
        source_dir = work / 'src'
        source_dir.mkdir()
        source = _extract_source(source_archive, source_dir, source_hash)
        bash, compiler, make, compiler_dir = _native_tools(system)
        env = _clean_environment(work, system, compiler_dir)
        compiler_id = _compiler_identity(compiler, env)
        config_command = ['./configure', *FFPROBE_CONFIG]
        if system == 'Windows':
            config_command.append('--extra-ldflags=-static -static-libgcc')
        effective_config = config_command[1:]
        if system == 'Windows':
            _run(config_command, source, env, bash=bash, windows=True, phase='configure')
            _run([str(make), '-j2'], source, env, bash=bash, windows=True, phase='make')
        else:
            _run(config_command, source, env, phase='configure')
            _run([str(make), '-j2'], source, env, phase='make')
        built = source / ('ffprobe.exe' if system == 'Windows' else 'ffprobe')
        if not built.is_file():
            raise RuntimeError('FFmpeg build did not produce the expected FFprobe executable.')
        ffprobe = bin_dir / built.name
        shutil.copyfile(built, ffprobe)
        if system != 'Windows':
            ffprobe.chmod(0o755)
        if system == 'Darwin':
            try:
                subprocess.run(['codesign', '--force', '--sign', '-', str(ffprobe)], env=env,
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['codesign', '--verify', '--strict', str(node_path)], env=env,
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                raise RuntimeError('macOS FFprobe signing or Node signature verification failed.') from None
            _verify_macos_dependencies(ffprobe, env)
        source_licenses = notices / 'ffmpeg'
        source_licenses.mkdir(exist_ok=True)
        for license_name in ('COPYING.LGPLv2.1', 'LICENSE', 'LICENSE.md'):
            license_path = source / license_name
            if license_path.is_file():
                shutil.copyfile(license_path, source_licenses / license_name)
        if not (source_licenses / 'COPYING.LGPLv2.1').is_file():
            raise RuntimeError('FFmpeg LGPL license text was not copied.')
        shutil.copyfile(source_archive, source_licenses / source_archive.name)
        signature = source_archive.with_suffix(source_archive.suffix + '.asc')
        if signature.is_file() and not signature.is_symlink():
            shutil.copyfile(signature, source_licenses / signature.name)
        shutil.copyfile(Path(__file__), source_licenses / 'prepare_release_tools.py')
        recipe = '\n'.join([
            'FFmpeg source-build provenance', f'Source: {source_url}',
            f'Source archive SHA-256: {source_hash}', f'Compiler: {compiler_id}',
            'Target: native host architecture; static FFmpeg libraries; no external libraries.',
            'Configure arguments:', *[f'  {arg}' for arg in effective_config],
            'Build command: make -j2', '',
        ])
        (source_licenses / 'source-build.txt').write_text(recipe, encoding='utf-8')
    env = _clean_environment(output, system, compiler_dir)
    _smoke_test(node_path, ffprobe, node_version, probe_version, env, output)
    proof = {
        'tools': {
            'node': {'version': node_version, 'sha256': sha256_file(node_path), 'url': node_url,
                     'archive_sha256': node_hash},
            'ffprobe': {'version': probe_version, 'sha256': sha256_file(ffprobe), 'url': source_url,
                        'archive_sha256': source_hash},
        },
        'ffprobe_config': effective_config,
        'compiler': compiler_id,
    }
    proof_path = output / 'toolchain.json'
    proof_path.write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    _export_github_tools(node_path, ffprobe, proof_path, bin_dir)
    print(f'Prepared verified release tools for {host}.', flush=True)
    return proof_path


if __name__ == '__main__':
    try:
        prepare()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, subprocess.SubprocessError):
        print('Release tool preparation failed; inspect private build logs and pinned tool configuration.', file=sys.stderr)
        raise SystemExit(1) from None
