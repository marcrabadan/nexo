"""Build docs/libraries.html from docs/libraries/page.tmpl, as
docs/reference/build.py builds the reference.

Every example is a program in this folder, run with the interpreter and,
when it is built, the compiler; the build stops if they differ.

    {{run:NAME}}       NAME and what `nexo NAME` prints
    {{test:NAME}}      NAME and what `nexo test NAME` prints
    {{src:NAME}}       only NAME (a program or another file)
    {{sql:NAME}}       the SELECTs `NEXO_SQL_LOG=1 nexo NAME` sends (its error output)
    {{steps:S; S...}}  the output of each step, one after the other in one
                       folder: a step is `[~SECONDS] [ARGS] NAME`, and one
                       with ~SECONDS is stopped with Ctrl-C after them

    .venv/bin/python docs/libraries/build.py
"""
import html
import importlib.util
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("reference_build", ROOT / "docs" / "reference" / "build.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def output(label, text):
    return (f'<div class="code"><div class="out"><div class="out-label">{html.escape(label)}</div>'
            f'<pre><code>{html.escape(text.rstrip(), quote=False)}</code></pre></div></div>')


def expand(m):
    kind, name = m.group(1), m.group(2)
    if kind == "steps":
        steps = []
        for step in name.split(";"):
            words = step.split()
            stop = int(words.pop(0)[1:]) if words[0].startswith("~") else None
            steps.append((words[:-1], words[-1], stop))
        outs = reference.session(steps, HERE)
        return "".join(output(f"$ nexo {' '.join(args + [Path(n).name])}" + ("   # Ctrl-C to stop" if stop else ""), out)
                       for (args, n, stop), out in zip(steps, outs))
    code = (HERE / name).read_text(encoding="utf-8")
    shown = Path(name).name
    if kind == "src":
        return reference.window(shown, code)
    if kind == "sql":
        out = reference.run([], name, HERE, {"NEXO_SQL_LOG": "1"}, errors_only=True)
        lines = [line for line in out.splitlines() if line.startswith("sql: SELECT")]
        return output(f"$ NEXO_SQL_LOG=1 nexo {shown} 2> sql.log; grep SELECT sql.log", "\n".join(lines))
    args = ["test"] if kind == "test" else []
    return reference.window(shown, code, f"$ nexo {' '.join(args + [shown])}", reference.run(args, name, HERE))


def build() -> str:
    page = (HERE / "page.tmpl").read_text(encoding="utf-8")
    return re.sub(r"\{\{(run|test|src|sql|steps):([\w./;~ -]+)\}\}", expand, page)


if __name__ == "__main__":
    (ROOT / "docs" / "libraries.html").write_text(build(), encoding="utf-8")
    print("wrote docs/libraries.html")
