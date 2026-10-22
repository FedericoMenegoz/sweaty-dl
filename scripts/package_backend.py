"""Freeze the backend on the target OS, including tools and third-party notices."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / '.build' / 'backend'
RESOURCES = ROOT / 'src-tauri' / 'resources' / 'backend'


def run(args, **kwargs):
    print('+', ' '.join(str(arg) for arg in args), flush=True)
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def copy_notices(destination):
    destination.mkdir(parents=True, exist_ok=True)
    inventory = []
    # Include installed distribution license files. No application modules or venv copied.
    for distribution in metadata.distributions():
        name = distribution.metadata['Name'] or 'unknown'
        # Excluded GUI frameworks never ship with the backend.
        if any(value in name.lower() for value in ('pyside', 'pyqt', 'shiboken')):
            continue
        files = [file for file in distribution.files or [] if any(word in str(file).upper() for word in ('LICENSE', 'COPYING', 'NOTICE'))]
        if not files:
            continue
        folder = destination / name
        folder.mkdir(exist_ok=True)
        for index, file in enumerate(files):
            source = Path(distribution.locate_file(file))
            if source.is_file():
                shutil.copy2(source, folder / f'{index}-{source.name}')
        inventory.append({'name': name, 'version': distribution.version, 'homepage': distribution.metadata.get('Home-page')})
    (destination / 'python-distributions.json').write_text(json.dumps(inventory, indent=2))
    return inventory


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
    BUILD.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, 'PYINSTALLER_CONFIG_DIR': str(BUILD / 'cache'), 'PYTHONDONTWRITEBYTECODE': '1'}
    command = [sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
               '--name', 'sweatydl-backend', '--distpath', str(BUILD / 'dist'),
               '--workpath', str(BUILD / 'work'), '--specpath', str(BUILD),
               '--paths', str(ROOT / 'backend')]
    for module in ('PySide6', 'PyQt6', 'PyQt5', 'PySide2', 'shiboken6', 'tkinter', 'matplotlib', 'numpy', 'scipy', 'pandas'):
        command.extend(['--exclude-module', module])
    for name in ('yt-dlp', 'yt-dlp-ejs', 'mutagen', 'requests', 'beautifulsoup4'):
        command.extend(['--copy-metadata', name])
    command.extend(['--collect-all', 'yt_dlp_ejs'])
    for path in tools.values():
        command.extend(['--add-binary', f'{path}{os.pathsep}tools'])
    command.append(str(ROOT / 'backend' / 'entry.py'))
    log = BUILD / 'pyinstaller.log'
    print(f'PyInstaller log: {log}', flush=True)
    with log.open('w') as output:
        run(command, env=environment, stdout=output, stderr=subprocess.STDOUT, cwd=ROOT)
    staged = BUILD / 'dist' / 'sweatydl-backend'
    notices = staged / 'THIRD-PARTY-NOTICES'
    inventory = copy_notices(notices)
    for path in tools.values():
        copy_tool_notices(path, notices)
    (notices / 'README.txt').write_text('SweatyDL bundles Python, yt-dlp, requests, BeautifulSoup, mutagen, Node.js and FFprobe plus their dependencies.\nPython: https://www.python.org/\nyt-dlp: https://github.com/yt-dlp/yt-dlp\nNode.js: https://nodejs.org/\nFFmpeg/FFprobe source and licensing: https://ffmpeg.org/legal.html\nMutagen source: https://github.com/quodlibet/mutagen\nThis private test build is unsigned/unnotarized. Review all dependency licenses before public distribution.\n')
    executable = staged / ('sweatydl-backend.exe' if os.name == 'nt' else 'sweatydl-backend')
    # Run with no development Python, Node or FFprobe on PATH and no project cwd.
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'SWEATYDL_', 'DYLD_'))}
    clean_env['PATH'] = os.pathsep.join([str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32')]) if os.name == 'nt' else '/usr/bin:/bin'
    proof = subprocess.check_output([str(executable), '--self-test'], cwd=BUILD, env=clean_env, text=True, timeout=180)
    result = json.loads(proof)
    if not result.get('ok') or not result.get('frozen') or result.get('qt_loaded'):
        raise RuntimeError('Standalone backend validation failed.')
    forbidden = [path for path in staged.rglob('*') if any(part.lower().startswith(('pyside', 'pyqt', 'shiboken', 'node_modules')) for part in path.parts)]
    if forbidden:
        raise RuntimeError(f'Forbidden GUI/build dependency in backend: {forbidden[0].name}')
    result.update({'os': platform.system(), 'arch': platform.machine(), 'inventory_count': len(inventory)})
    (staged / 'build-info.json').write_text(json.dumps(result, indent=2))
    RESOURCES.parent.mkdir(parents=True, exist_ok=True)
    if RESOURCES.exists():
        shutil.rmtree(RESOURCES)
    shutil.copytree(staged, RESOURCES, symlinks=True)
    print(json.dumps(result, indent=2), flush=True)
    return RESOURCES


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    package()
