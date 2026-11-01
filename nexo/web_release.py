"""Deterministic optimized artifacts, integrity manifest and safe output ownership."""
import base64
import gzip
import hashlib
import json
from pathlib import Path,PurePosixPath
import re
import shutil
import subprocess
import tempfile

from . import __version__
from .web_frontend import fail

MANIFEST='nexo-manifest.json'


def digest(value):return hashlib.sha256(value).hexdigest()

def serialized(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)+'\n'


def optimizer_revision():
    root=Path(__file__).parent
    files=[Path(__file__),root/'web_tools/release.mjs',root/'web_tools/vite.mjs',root/'web_tools/package-lock.json']
    return digest(b'\n'.join(path.read_bytes() for path in files))


def input_revision(files):return digest(serialized(files).encode())


def safe_name(name):
    path=PurePosixPath(name)
    return isinstance(name,str) and bool(name) and not path.is_absolute() and str(path)==name and '..' not in path.parts and '\\' not in name


def metadata(content):
    data=content.encode('utf-8')
    return {'sha256':digest(data),'integrity':'sha384-'+base64.b64encode(hashlib.sha384(data).digest()).decode(),
            'bytes':len(data),'gzip_bytes':len(gzip.compress(data,mtime=0))}


def optimize(files,project,sourcemap=False,observe=False):
    node=shutil.which('node')
    if not node:fail(1,'Release optimization requires Node and pinned Vite; install the optional tooling.','NW4001')
    tools=Path(__file__).with_name('web_tools')
    try:
        result=subprocess.run([node,str(tools/'release.mjs')],input=serialized({'files':files,'project':str(project),'sourcemap':sourcemap}),text=True,capture_output=True,timeout=60)
    except subprocess.TimeoutExpired:fail(1,'Release optimizer exceeded 60 seconds.','NW5001')
    if result.returncode:fail(1,result.stderr.strip() or 'Release optimizer failed.','NW4001' if result.stderr.startswith('NW4001:') else 'NW5001')
    try:
        output=json.loads(result.stdout)
        if output['schema_version']!=1:raise ValueError()
        optimized=output['files']
        if not isinstance(optimized,dict) or not all(safe_name(k) and isinstance(v,str) for k,v in optimized.items()):raise ValueError()
        if 'index.html' not in optimized:raise ValueError()
    except (ValueError,KeyError,TypeError):fail(1,'Invalid release optimizer response.','NW5001')
    def sri(match):
        name=match[1].removeprefix('./')
        if name not in optimized:fail(1,'Release references a missing asset.','NW5001')
        return match[0][:-1]+' integrity="'+metadata(optimized[name])['integrity']+'">'
    html=optimized['index.html']
    html=re.sub(r'<script\b[^>]*\bsrc="([^"]+)"[^>]*>',sri,html)
    html=re.sub(r'<link\b[^>]*\bhref="([^"]+\.css)"[^>]*>',sri,html)
    optimized['index.html']=html
    manifest={'schema_version':1,'format':'nexo-web-release','entry':'index.html','sourcemap':sourcemap,'observe':observe,
              'input_sha256':input_revision(files),'optimizer_sha256':optimizer_revision(),
              'toolchain':{'compiler':__version__,'vite':'8.3.5'},'files':{name:metadata(text) for name,text in sorted(optimized.items())}}
    return {**optimized,MANIFEST:serialized(manifest)}


def read_release(directory,expected_input=None):
    root=Path(directory)
    if root.is_symlink():fail(1,'Release directory must not be a symlink.','NW5001')
    try:
        if (root/MANIFEST).is_symlink():raise ValueError()
        manifest=json.loads((root/MANIFEST).read_text(encoding='utf-8'))
        if manifest['schema_version']!=1 or manifest['format']!='nexo-web-release' or manifest['entry']!='index.html':raise ValueError()
        entries=manifest['files']
        if not isinstance(entries,dict) or 'index.html' not in entries or MANIFEST in entries:raise ValueError()
        files={}
        for name,expected in entries.items():
            if not safe_name(name):raise ValueError()
            path=root/name
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent!=root.parent):raise ValueError()
            text=path.read_text(encoding='utf-8')
            if metadata(text)!=expected:raise ValueError()
            files[name]=text
        actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() or p.is_symlink()}
        if actual!=set(entries)|{MANIFEST}:raise ValueError()
        if expected_input is not None and (manifest['input_sha256']!=input_revision(expected_input) or manifest['optimizer_sha256']!=optimizer_revision()):raise ValueError()
        files[MANIFEST]=serialized(manifest)
        return files
    except (OSError,UnicodeError,ValueError,KeyError,TypeError):fail(1,'Invalid, modified or stale release artifacts; rebuild into a clean output directory.','NW5001')


def write_release(files,directory):
    target=Path(directory).absolute()
    if target.is_symlink():fail(1,'Release output must not be a symlink.','NW5001')
    if target.exists():
        if not target.is_dir() or target==target.parent:fail(1,'Release output requires a dedicated build directory.','NW5001')
        known={'index.html','assets',MANIFEST} if (target/MANIFEST).exists() else {'index.html','main.js','app.js','app.css','app.js.map'}
        if any(p.name not in known or p.is_symlink() for p in target.iterdir()):fail(1,'Release output contains unrelated files; choose a clean build directory.','NW5001')
        existing={str(p.relative_to(target)) for p in target.rglob('*') if p.is_file() or p.is_symlink()}
        if MANIFEST in existing:read_release(target)
        elif not existing<= {'index.html','main.js','app.js','app.css','app.js.map'}:fail(1,'Release output contains unrelated files; choose a clean build directory.','NW5001')
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.nexo-release-',dir=target.parent) as temporary:
        stage=Path(temporary)/'next';stage.mkdir()
        for name,text in files.items():
            if not safe_name(name):fail(1,'Unsafe release asset name.','NW5001')
            path=stage/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
        backup=Path(temporary)/'previous'
        if target.exists():target.rename(backup)
        try:stage.rename(target)
        except OSError:
            if backup.exists():backup.rename(target)
            raise
    return files
