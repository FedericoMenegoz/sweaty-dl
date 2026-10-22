"""Resolve only bundled tools in frozen builds; PATH is for development only."""
from pathlib import Path
import shutil
import sys


def tool(name):
    if getattr(sys, 'frozen', False):
        path = Path(sys._MEIPASS) / 'tools' / (name + ('.exe' if sys.platform == 'win32' else ''))
        return str(path) if path.is_file() else None
    return shutil.which(name)


def javascript_options():
    node = tool('node')
    return {'js_runtimes': {'node': {'path': node}}} if node else {}
