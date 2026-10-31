"""Build docs/reference.html from docs/reference/page.tmpl (not .html, so
GitHub Pages does not publish the template).

Every example in the page is a program in this folder. `{{run:NAME}}`
shows NAME and what `nexo NAME` prints, `{{test:NAME}}` what `nexo test
NAME` prints, and `{{src:NAME}}` only the program. Each program runs from
a copy of its folder with the interpreter and, when it is built, the
compiler (compiler/target/debug/nexo); the build stops if they differ.

    .venv/bin/python docs/reference/build.py
"""
import html
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
COMPILER = ROOT / "compiler" / "target" / "debug" / "nexo"


def run(args, name, here=HERE, env=None, errors_only=False):
    """What every implementation prints for `nexo *args NAME` (only on the
    error output with errors_only)."""
    return session([(args, name, None)], here, env, errors_only)[0]


def session(steps, here=HERE, env=None, errors_only=False):
    """What every implementation prints for several `nexo *args NAME` in
    one copy of the folder of the first NAME, step by step; a step with a
    number of seconds is stopped with Ctrl-C after them (a consumer)."""
    source = here / steps[0][1]
    commands = [[sys.executable, "-m", "nexo.cli"]] + ([[str(COMPILER)]] if COMPILER.exists() and shutil.which(os.environ.get("NEXO_CC", "clang")) else [])
    outputs = []
    for command in commands:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            shutil.copytree(source.parent, work)
            out = []
            for args, name, stop in steps:
                p = subprocess.Popen([*command, *args, Path(name).name], cwd=work, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL if errors_only else subprocess.PIPE,
                                     stderr=subprocess.PIPE if errors_only else subprocess.STDOUT, start_new_session=True,
                                     env={"PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8", "PATH": "/usr/bin:/bin",
                                          **{key: os.environ[key] for key in ("NEXO_CC", "NEXO_RUNTIME") if key in os.environ},
                                          **(env or {})})
                try:
                    data, err = p.communicate(timeout=stop or 120)
                except subprocess.TimeoutExpired:
                    if not stop:
                        raise
                    os.killpg(p.pid, signal.SIGINT)  # Ctrl-C reaches the program the compiler started too
                    data, err = p.communicate(timeout=30)
                out.append((err if errors_only else data).decode("utf-8"))
            outputs.append(tuple(out))
    if len(set(outputs)) != 1:
        sys.exit(f"{steps[0][1]}: the interpreter and the compiler print different things")
    return outputs[0]


def window(name, code, label=None, out=None):
    bar = f'<div class="code-bar"><span>{html.escape(name)}</span><button class="copy" type="button">Copy</button></div>'
    body = f'<pre><code class="nexo">{html.escape(code.rstrip(), quote=False)}</code></pre>'
    if out is not None:
        body += (f'<div class="out"><div class="out-label">{html.escape(label)}</div>'
                 f'<pre><code>{html.escape(out.rstrip(), quote=False)}</code></pre></div>')
    return f'<div class="code">{bar}{body}</div>'


def expand(m):
    kind, name = m.group(1), m.group(3)
    code = (HERE / name).read_text(encoding="utf-8")
    shown = Path(name).name if kind != "src" else name.split("/", 1)[-1]
    if kind == "src":
        return window(shown, code)
    if kind in ("test", "out"):  # only the output: the program is shown just before
        args = ["test"] if kind == "test" else m.group(2).split()
        result = html.escape(run(args, name).rstrip(), quote=False)
        return (f'<div class="code"><div class="out"><div class="out-label">$ nexo {html.escape(" ".join(args))} '
                f'{html.escape(shown)}</div><pre><code>{result}</code></pre></div></div>')
    return window(shown, code, f"$ nexo {shown}", run([], name))


if __name__ == "__main__":
    page = (HERE / "page.tmpl").read_text(encoding="utf-8")
    out = re.sub(r"\{\{(run|test|src|out):(?:([a-z -]+):)?([\w./]+)\}\}", expand, page)
    (ROOT / "docs" / "reference.html").write_text(out, encoding="utf-8")
    print("wrote docs/reference.html")
