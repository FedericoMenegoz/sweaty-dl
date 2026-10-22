"""Standalone worker entry point. No GUI toolkit and no development paths."""
import json
from pathlib import Path
import runpy
import sys

# Explicit imports let PyInstaller discover both protocols and their dependencies.
import bridge
import download
import diagnostics
import provider_search
import provider_urls
import runtime_tools


def self_test():
    import subprocess
    import tempfile
    import wave
    from importlib.metadata import version
    from yt_dlp import YoutubeDL
    import certifi
    from mutagen import File
    tools = {}
    for name in ('node', 'ffprobe'):
        path = runtime_tools.tool(name)
        if not path:
            raise RuntimeError(f'Missing bundled tool: {name}')
        command = [path, '--version' if name == 'node' else '-version']
        result = subprocess.run(command, capture_output=True, timeout=60, check=True)
        tools[name] = result.stdout.decode(errors='replace').splitlines()[0]
    assert Path(certifi.where()).is_file()
    assert not provider_search.convincing(provider_search.Result('Song live', 'Artist', '', 'fixture'), 'Song', 'Artist')
    with tempfile.TemporaryDirectory(prefix='sweatydl-selftest-') as folder:
        path = Path(folder) / 'audio.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
            audio.writeframes(b'\x00\x00' * 16000)
        download.validate_duration(File(path).info.length, 2)
    assert not any(name.startswith(('PySide', 'PyQt')) for name in sys.modules)
    return {'ok': True, 'frozen': bool(getattr(sys, 'frozen', False)),
            'python': sys.version.split()[0], 'yt_dlp': version('yt-dlp'),
            'tools': tools, 'qt_loaded': False}


def main():
    if sys.argv[1:] == ['--self-test']:
        print(json.dumps(self_test()))
    elif sys.argv[1:] == ['--search']:
        runpy.run_module('bridge', run_name='__main__')
    elif sys.argv[1:] == ['--download']:
        runpy.run_module('download', run_name='__main__')
    else:
        raise SystemExit('Expected --search, --download or --self-test')


if __name__ == '__main__':
    main()
