"""Integrity and staged ownership for the routed-static artifact format.

M5 release manifests and their measured optimizer fingerprint remain unchanged.
"""
import json
from pathlib import Path
import tempfile

from .web_frontend import fail
from .web_release import MANIFEST, metadata, safe_name, serialized, read_release, write_release


PLAIN = {'index.html', 'main.js', 'app.js', 'app.css', 'app.js.map', 'app.css.map'}


def is_static(directory):
    path = Path(directory)/MANIFEST
    if not path.exists():
        return False
    try:
        if path.is_symlink():
            raise ValueError()
        manifest = json.loads(path.read_text(encoding='utf-8'))
        return manifest['format'] == 'nexo-web-static'
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        fail(1, 'Invalid or modified build manifest; use a clean output directory.', 'NW5001')


def read_output(directory):
    if not is_static(directory):
        return read_release(directory)
    root = Path(directory)
    try:
        if root.is_symlink():
            raise ValueError()
        manifest = json.loads((root/MANIFEST).read_text(encoding='utf-8'))
        if manifest['schema_version'] != 1 or manifest['entry'] != 'index.html':
            raise ValueError()
        entries = manifest['files']
        if not isinstance(entries, dict) or 'index.html' not in entries or MANIFEST in entries:
            raise ValueError()
        files = {}
        for name, expected in entries.items():
            if not safe_name(name):
                raise ValueError()
            path = root/name
            if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent):
                raise ValueError()
            content = path.read_text(encoding='utf-8')
            if metadata(content) != expected:
                raise ValueError()
            files[name] = content
        actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() or p.is_symlink()}
        if actual != set(entries) | {MANIFEST}:
            raise ValueError()
        files[MANIFEST] = serialized(manifest)
        return files
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        fail(1, 'Invalid or modified static artifacts, or unrelated files; use a clean output directory.', 'NW5001')


def write_output(files, directory):
    """Use M5 staging for ordinary builds; validate ownership for static trees."""
    new_static = MANIFEST in files and json.loads(files[MANIFEST])['format'] == 'nexo-web-static'
    if not new_static and not is_static(directory):
        return write_release(files, directory)
    target = Path(directory).absolute()
    if target.is_symlink() or target == target.parent or (target.exists() and not target.is_dir()):
        fail(1, 'Static output requires a dedicated directory without symlinks.', 'NW5001')
    if target.exists():
        if (target/MANIFEST).exists():
            owned = read_output(target)
            directories = {str(parent) for name in owned for parent in Path(name).parents if str(parent) != '.'}
            if any(str(p.relative_to(target)) not in directories for p in target.rglob('*') if p.is_dir()):
                fail(1, 'Static output contains unrelated directories; choose a clean output directory.', 'NW5001')
        else:
            if any(p.name not in PLAIN or not p.is_file() or p.is_symlink() for p in target.iterdir()):
                fail(1, 'Static output contains unrelated files; choose a clean output directory.', 'NW5001')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.nexo-static-', dir=target.parent) as temporary:
        stage = Path(temporary)/'next'
        write_release(files, stage)
        backup = Path(temporary)/'previous'
        if target.exists():
            target.rename(backup)
        try:
            stage.rename(target)
        except OSError:
            if backup.exists():
                backup.rename(target)
            raise
    return files
