"""Build docs/reference.html from docs/reference/page.html.

Every example in the page is a program in this folder. `{{run:NAME}}`
shows NAME and what `nexo NAME` prints, `{{test:NAME}}` what `nexo test
NAME` prints, and `{{src:NAME}}` only the program. Each program runs from
a copy of its folder with the interpreter and, when it is built, the
compiler (compiler/target/debug/nexo); the build stops if they differ.

    .venv/bin/python docs/reference/build.py
"""
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
COMPILER = ROOT / "compiler" / "target" / "debug" / "nexo"


def run(args, name):
    """What every implementation prints for `nexo *args NAME`."""
    source = HERE / name
    commands = [[sys.executable, "-m", "nexo.cli"]] + ([[str(COMPILER)]] if COMPILER.exists() else [])
    outputs = []
    for command in commands:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            shutil.copytree(source.parent, work)
            p = subprocess.run([*command, *args, source.name], cwd=work, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
                               env={"PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8", "PATH": "/usr/bin:/bin"})
            outputs.append(p.stdout.decode("utf-8"))
    if len(set(outputs)) != 1:
        sys.exit(f"{name}: the interpreter and the compiler print different things")
    return outputs[0]


def window(name, code, label=None, out=None):
    bar = f'<div class="code-bar"><span>{html.escape(name)}</span><button class="copy" type="button">Copy</button></div>'
    body = f'<pre><code class="nexo">{html.escape(code.rstrip(), quote=False)}</code></pre>'
    if out is not None:
        body += (f'<div class="out"><div class="out-label">{html.escape(label)}</div>'
                 f'<pre><code>{html.escape(out.rstrip(), quote=False)}</code></pre></div>')
    return f'<div class="code">{bar}{body}</div>'


def expand(m):
    kind, name = m.group(1), m.group(2)
    code = (HERE / name).read_text(encoding="utf-8")
    shown = Path(name).name if kind != "src" else name.split("/", 1)[-1]
    if kind == "src":
        return window(shown, code)
    if kind == "test":  # only the output: the program is shown just before
        result = html.escape(run(["test"], name).rstrip(), quote=False)
        return (f'<div class="code"><div class="out"><div class="out-label">$ nexo test {html.escape(shown)}</div>'
                f'<pre><code>{result}</code></pre></div></div>')
    return window(shown, code, f"$ nexo {shown}", run([], name))


page = (HERE / "page.html").read_text(encoding="utf-8")
out = re.sub(r"\{\{(run|test|src):([\w./]+)\}\}", expand, page)
(ROOT / "docs" / "reference.html").write_text(out, encoding="utf-8")
print("wrote docs/reference.html")
