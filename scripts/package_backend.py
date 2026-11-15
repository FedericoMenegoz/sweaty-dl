"""Freeze the backend on the target OS, including tools and third-party notices."""
import argparse
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / '.build' / 'backend'
RESOURCES = ROOT / 'src-tauri' / 'resources' / 'backend'


def validate_release_tools(proof):
    minimum_node = {22: (22, 23, 5), 24: (24, 22, 3), 26: (26, 13, 1)}
    versions = {}
    for name in ('node', 'ffprobe'):
        match = re.search(r'(\d+)\.(\d+)\.(\d+)', proof.get('tools', {}).get(name, ''))
        if not match:
            raise RuntimeError(f'Cannot verify the bundled {name} version.')
        versions[name] = tuple(map(int, match.groups()))
    node = versions['node']
    if node[0] not in minimum_node or node < minimum_node[node[0]]:
        raise RuntimeError('Bundled Node is below the reviewed security baseline; rebuild with a current reviewed toolchain.')
    if versions['ffprobe'] < (8, 1, 2):
        raise RuntimeError('Bundled FFprobe is below the reviewed 8.1.2 security baseline; configure a reviewed current binary.')


def run(args, **kwargs):
    print('+', ' '.join(str(arg) for arg in args), flush=True)
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def copy_notices(destination):
    destination.mkdir(parents=True, exist_ok=True)
    inventory = []
    # Include installed distribution license files. No application modules or venv copied.
    for distribution in metadata.distributions():
        name = distribution.metadata['Name'] or 'unknown'
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', name):
            raise RuntimeError('Invalid installed distribution name.')
        # Excluded GUI frameworks never ship with the backend.
        if any(value in name.lower() for value in ('pyside', 'pyqt', 'shiboken')):
            continue
        files = [file for file in distribution.files or []
                 if any(word in str(file).upper() for word in ('LICENSE', 'COPYING', 'NOTICE'))
                 and Path(str(file)).suffix.lower() not in ('.py', '.pyc', '.pyo')]
        if not files:
            continue
        folder = destination / name
        folder.mkdir(exist_ok=True)
        for index, file in enumerate(files):
            source = Path(distribution.locate_file(file))
            base = Path(distribution.locate_file('.')).resolve()
            if source.is_file() and not source.is_symlink() and source.resolve().is_relative_to(base):
                shutil.copy2(source, folder / f'{index}-{source.name}')
        inventory.append({'name': name, 'version': distribution.version})
    (destination / 'python-distributions.json').write_text(json.dumps(inventory, indent=2))
    return inventory


def copy_python_notice(destination):
    # Python is not a wheel, so importlib.metadata does not include its license.
    # Use the active interpreter's installation, including when building in a venv.
    prefix = Path(sys.base_prefix).resolve()
    candidates = (Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt', prefix / 'LICENSE.txt')
    for source in candidates:
        if (source.is_file() and not source.is_symlink()
                and source.resolve().is_relative_to(prefix)):
            folder = destination / 'python'
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, folder / 'LICENSE.txt')
            return
    raise RuntimeError('Python runtime license not found in the build interpreter installation.')


def copy_tool_notices(tool, destination):
    path = Path(tool).resolve()
    roots = [path.parent, path.parent.parent]
    if platform.system() == 'Darwin':
        # Homebrew packages contain their copyright notices in the Cellar root.
        try:
            output = subprocess.check_output(['otool', '-L', str(path)], text=True)
            for line in output.splitlines()[1:]:
                dependency = Path(line.strip().split(' (')[0]).resolve()
                parts = dependency.parts
                if 'Cellar' in parts:
                    index = parts.index('Cellar')
                    roots.append(Path(*parts[:index + 3]))
        except subprocess.CalledProcessError:
            pass
    seen = set()
    for root in roots:
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        candidates = [file for file in root.iterdir() if file.is_file() and any(word in file.name.upper() for word in ('LICENSE', 'COPYING', 'NOTICE'))]
        if candidates:
            folder = destination / f'{root.parent.name}-{root.name}'
            folder.mkdir(parents=True, exist_ok=True)
            for file in candidates:
                shutil.copy2(file, folder / file.name)


def strip_metadata_descriptions(staged):
    # Wheel long descriptions are documentation, not runtime metadata. They
    # include upstream command examples with home paths; license headers and
    # separately packaged notice files are retained verbatim.
    for path in staged.glob('_internal/*.dist-info/METADATA'):
        data = path.read_bytes()
        separator = b'\r\n\r\n' if b'\r\n\r\n' in data else b'\n\n'
        headers, found, _ = data.partition(separator)
        if found:
            path.write_bytes(headers + separator)


PYTHON_DIAGNOSTIC_SOURCES = (
    b'/Modules/pyexpat.c', b'/Modules/arraymodule.c', b'/Modules/_ssl.c',
    b'/Modules/sha3module.c', b'/Modules/_hacl/Hacl_Hash_SHA3.c',
    b'/Objects/abstract.c', b'/Objects/bytearrayobject.c', b'/Objects/bytesobject.c',
    b'/Objects/cellobject.c', b'/Objects/classobject.c', b'/Objects/codeobject.c',
    b'/Objects/dictobject.c', b'/Objects/fileobject.c', b'/Objects/frameobject.c',
    b'/Objects/funcobject.c', b'/Objects/iterobject.c', b'/Objects/listobject.c',
    b'/Objects/longobject.c', b'/Objects/memoryobject.c', b'/Objects/methodobject.c',
    b'/Objects/moduleobject.c', b'/Objects/object.c', b'/Objects/setobject.c',
    b'/Objects/structseq.c', b'/Objects/tupleobject.c', b'/Objects/typeobject.c',
    b'/Objects/unicodeobject.c', b'/Objects/weakrefobject.c', b'/Parser/string_parser.c',
    b'/Python/ceval_gil.c', b'/Python/gc.c', b'/Python/getargs.c', b'/Python/import.c',
    b'/Python/pystrtod.c', b'/Python/traceback.c',
)


def normalize_python_cstrings(data):
    # CPython's __FILE__ constants describe C error locations, not runtime
    # lookup paths. Keep string sizes and suffix offsets unchanged.
    strings = data.split(b'\0')
    for index, value in enumerate(strings):
        suffix = next((suffix for suffix in PYTHON_DIAGNOSTIC_SOURCES if value.endswith(suffix)), None)
        if suffix and value.startswith((b'/Users/', b'/home/')):
            prefix_size = len(value) - len(suffix)
            if prefix_size >= len(b'/source'):
                strings[index] = b'/source' + b'_' * (prefix_size - len(b'/source')) + suffix
    return b'\0'.join(strings)


def python_library_path():
    from PyInstaller.depend.bindepend import get_python_library_path
    return Path(get_python_library_path()).resolve()


def prepare_python_binaries(destination, extension_root=None, library=None):
    from macholib.MachO import MachO
    if extension_root is None:
        extension_root = Path(importlib.util.find_spec('array').origin).parent
    if library is None:
        library = python_library_path()
    destination.mkdir(parents=True, exist_ok=True)
    # This directory contains only generated Analysis inputs. A clean extension
    # must never be shadowed by a prior interpreter's normalized copy.
    for previous in [*destination.glob('*.so'), destination / library.name]:
        if not previous.exists():
            continue
        previous.unlink()
    changed = False
    for source in [library, *sorted(extension_root.glob('*.so'))]:
        data = source.read_bytes()
        prepared = bytearray(data)
        # Only the reviewed CPython diagnostic filenames in read-only strings
        # change. Instructions and the installed interpreter remain intact.
        for header in MachO(str(source)).headers:
            for _, _, sections in header.commands:
                if not isinstance(sections, list):
                    continue
                for section in sections:
                    if (section.segname.rstrip(b'\0') == b'__TEXT'
                            and section.sectname.rstrip(b'\0') == b'__cstring'):
                        start = header.offset + section.offset
                        end = start + section.size
                        prepared[start:end] = normalize_python_cstrings(data[start:end])
        if prepared != data:
            (destination / source.name).write_bytes(prepared)
            changed = True
    # This is an Analysis input. PyInstaller applies its ordinary Mach-O
    # processing and ad-hoc signature after collecting this extension.
    return destination if changed else None


def select_normalized_inputs(spec, normalized):
    # Analysis caches some standard modules before applying --paths. Replace
    # only their resolved stdlib binary inputs after Analysis has discovered
    # dependencies, before COLLECT strips and signs the selected files.
    original = Path(importlib.util.find_spec('array').origin).parent
    replacements = {str((original / path.name).resolve()): str(path.resolve())
                    for path in normalized.glob('*.so')}
    library = python_library_path()
    if (normalized / library.name).is_file():
        replacements[str(library)] = str((normalized / library.name).resolve())
    text = spec.read_text()
    marker = '\npyz = PYZ('
    if text.count(marker) != 1:
        raise RuntimeError('Unexpected PyInstaller spec layout; normalized inputs were not selected.')
    selection = (f'\nimport os as _sweatydl_os\n_sweatydl_inputs = {replacements!r}\n'
                 'a.binaries = [(name, _sweatydl_inputs.get(_sweatydl_os.path.realpath(source), source), kind)\n'
                 '              for name, source, kind in a.binaries]\n')
    spec.write_text(text.replace(marker, selection + marker, 1))


def verified_toolchain(tools, proof=None):
    """Release tools must match the hash-verified preparation step's proof."""
    proof_path = os.environ.get('SWEATYDL_TOOLCHAIN')
    if proof is None and not proof_path:
        if os.environ.get('SWEATYDL_RELEASE_BUILD') == '1':
            raise RuntimeError('Prepare pinned release tools before packaging (scripts/prepare_release_tools.py).')
        return None
    if proof is None:
        proof = json.loads(Path(proof_path).read_text())
    pins = json.loads((ROOT / 'deployment/toolchain.json').read_text())
    node = pins['node']['platforms'][platform.system()]
    expected = {
        'node': (f'https://nodejs.org/dist/v{pins["node"]["version"]}/{node["archive"]}', node['sha256']),
        'ffprobe': (pins['ffprobe']['url'], pins['ffprobe']['sha256']),
    }
    for name, (url, archive_hash) in expected.items():
        record = proof['tools'][name]
        digest = hashlib.sha256(Path(tools[name]).read_bytes()).hexdigest()
        if (record['url'] != url or record['archive_sha256'] != archive_hash
                or record['sha256'] != digest
                or (name == 'node' and digest != node.get('packaged_sha256', node['binary_sha256']))):
            raise RuntimeError(f'Bundled {name} does not match the pinned toolchain proof.')
    return proof


def package():
    if platform.system() not in ('Darwin', 'Windows', 'Linux'):
        raise RuntimeError('Build this backend on the matching macOS, Windows or Linux host.')
    tools = {}
    for name in ('node', 'ffprobe'):
        override = os.environ.get(f'SWEATYDL_{name.upper()}')
        path = override or shutil.which(name)
        if not path or not Path(path).is_file():
            raise RuntimeError(f'Missing build tool {name}: set SWEATYDL_{name.upper()} to its executable.')
        tools[name] = str(Path(path).resolve())
    toolchain = verified_toolchain(tools)
    BUILD.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, 'PYINSTALLER_CONFIG_DIR': str(BUILD / 'cache'), 'PYTHONDONTWRITEBYTECODE': '1'}
    for key in list(environment):
        if key.startswith('TAURI_SIGNING_'):
            del environment[key]
    command = [sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
               '--name', 'sweatydl-backend', '--distpath', str(BUILD / 'dist'),
               '--workpath', str(BUILD / 'work'), '--specpath', str(BUILD),
               '--paths', str(ROOT / 'backend')]
    normalized = None
    if platform.system() == 'Darwin':
        # PyInstaller uses strip -S on macOS: remove upstream Python debug
        # paths before its existing Mach-O processing and ad-hoc signing.
        # Pinned tools are copied separately after freezing.
        command.append('--strip')
        normalized = prepare_python_binaries(BUILD / 'python-input')
    for module in ('PySide6', 'PyQt6', 'PyQt5', 'PySide2', 'shiboken6', 'tkinter', 'matplotlib', 'numpy', 'scipy', 'pandas'):
        command.extend(['--exclude-module', module])
    for name in ('yt-dlp', 'yt-dlp-ejs', 'mutagen', 'requests', 'beautifulsoup4'):
        command.extend(['--copy-metadata', name])
    command.extend(['--collect-all', 'yt_dlp_ejs'])
    if not toolchain:
        for path in tools.values():
            command.extend(['--add-binary', f'{path}{os.pathsep}tools'])
    command.append(str(ROOT / 'backend' / 'entry.py'))
    log = BUILD / 'pyinstaller.log'
    print(f'PyInstaller log: {log}', flush=True)
    with log.open('w') as output:
        if normalized:
            # Generate the ordinary pinned PyInstaller spec, then select the
            # normalized binary inputs explicitly. Dependency discovery still
            # uses the installed extensions and their original library paths.
            options = []
            arguments = iter(command[4:])
            for argument in arguments:
                if argument in ('--noconfirm', '--clean'):
                    continue
                if argument in ('--distpath', '--workpath'):
                    next(arguments)
                    continue
                options.append(argument)
            run([sys.executable, '-B', '-m', 'PyInstaller.utils.cliutils.makespec', *options],
                env=environment, stdout=output, stderr=subprocess.STDOUT, cwd=ROOT)
            spec = BUILD / 'sweatydl-backend.spec'
            select_normalized_inputs(spec, normalized)
            command = [sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm', '--clean',
                       '--distpath', str(BUILD / 'dist'), '--workpath', str(BUILD / 'work'), str(spec)]
        run(command, env=environment, stdout=output, stderr=subprocess.STDOUT, cwd=ROOT)
    staged = BUILD / 'dist' / 'sweatydl-backend'
    if toolchain:
        # PyInstaller can reclassify even --add-data executables as binaries.
        # Prepared tools use only OS libraries; copy their verified bytes after
        # freezing so stripping/Mach-O processing cannot modify their identity.
        destination = staged / '_internal/tools'
        destination.mkdir(parents=True, exist_ok=True)
        for path in tools.values():
            shutil.copy2(path, destination / Path(path).name)
        bundled_tools = {name: staged / '_internal/tools' / Path(path).name for name, path in tools.items()}
        if os.name != 'nt':
            for path in bundled_tools.values():
                path.chmod(0o755)
        verified_toolchain(bundled_tools, toolchain)
    notices = staged / 'THIRD-PARTY-NOTICES'
    inventory = copy_notices(notices)
    copy_python_notice(notices)
    strip_metadata_descriptions(staged)
    for path in tools.values():
        copy_tool_notices(path, notices)
    if toolchain:
        prepared = Path(os.environ['SWEATYDL_TOOLCHAIN']).parent / 'notices'
        shutil.copytree(prepared, notices / 'release-tools', dirs_exist_ok=True)
    notice = 'SweatyDL bundles Python, yt-dlp, requests, BeautifulSoup, mutagen, Node.js and FFprobe plus their dependencies.\nPython: https://www.python.org/\nyt-dlp: https://github.com/yt-dlp/yt-dlp\nNode.js: https://nodejs.org/\nFFmpeg/FFprobe source and licensing: https://ffmpeg.org/legal.html\nMutagen source: https://github.com/quodlibet/mutagen\nReview all dependency licenses and included source/build materials before redistribution.\n'
    if platform.system() == 'Darwin':
        notice += 'macOS packaging omits Python runtime and extension debug symbols and normalizes reviewed C diagnostic source-filename prefixes without changing executable instructions.\n'
    (notices / 'README.txt').write_text(notice)
    executable = staged / ('sweatydl-backend.exe' if os.name == 'nt' else 'sweatydl-backend')
    # Run with no development Python, Node or FFprobe on PATH and no project cwd.
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'SWEATYDL_', 'DYLD_', 'TAURI_SIGNING_'))}
    clean_env['PATH'] = os.pathsep.join([str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32')]) if os.name == 'nt' else '/usr/bin:/bin'
    proof = subprocess.check_output([str(executable), '--self-test'], cwd=BUILD, env=clean_env, text=True, timeout=180)
    result = json.loads(proof)
    if not result.get('ok') or not result.get('frozen') or result.get('qt_loaded'):
        raise RuntimeError('Standalone backend validation failed.')
    if os.environ.get('SWEATYDL_RELEASE_BUILD') == '1':
        validate_release_tools(result)
    result['tool_sha256'] = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in tools.items()}
    if toolchain:
        result['toolchain'] = toolchain
    forbidden = [path for path in staged.rglob('*') if any(part.lower().startswith(('pyside', 'pyqt', 'shiboken', 'node_modules')) for part in path.parts)]
    if forbidden:
        raise RuntimeError(f'Forbidden GUI/build dependency in backend: {forbidden[0].name}')
    result.update({'os': platform.system(), 'arch': platform.machine(), 'inventory_count': len(inventory)})
    (staged / 'build-info.json').write_text(json.dumps(result, indent=2))
    from release_safety import scan_tree
    scan_tree(staged)
    RESOURCES.parent.mkdir(parents=True, exist_ok=True)
    if RESOURCES.exists():
        shutil.rmtree(RESOURCES)
    shutil.copytree(staged, RESOURCES, symlinks=True)
    print(json.dumps(result, indent=2), flush=True)
    return RESOURCES


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    package()
