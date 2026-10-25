#!/usr/bin/env python3
"""Write the performance section of docs/index.html from languages.json.

The section sits between <!-- performance:start --> and <!-- performance:end -->.
Each chart shows how many times longer than the fastest language each
language takes (a log scale); "Overall" is the geometric mean of those
ratios over every benchmark but hello.

    python3 compiler/bench/site.py
"""
import html
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LANGS = ["Nexo", "C", "Rust", "Go", "Java", "JavaScript", "Python"]
WHAT = {
    "hello": "Start-up: print one line",
    "fib": "Recursive calls: fib(32)",
    "loop": "An integer loop of 50 million steps",
    "lists": "A list of 2 million ints, a pipeline and a comprehension",
    "maps": "A million updates of a map of 1,000 keys",
    "records": "2 million records built and a method called on each",
    "strings": "300,000 strings built and 100,000 splits",
    "parallel": "16 tasks on threads (processes in Python)",
}
SOURCE = "https://github.com/marcrabadan/nexo/tree/main/compiler/bench"


def seconds(t):
    if t is None:
        return "over 600 s"
    return f"{t * 1000:.0f} ms" if t < 1 else f"{t:.2f} s"


def ratio_text(r):
    return f"{r:.1f}×" if r < 10 else f"{r:.0f}×"


def chart(ratios, label, times=None):
    """Horizontal bars on a log scale, fastest first; Nexo is highlighted."""
    top = max(ratios.values())
    limit = 10 ** max(1, math.ceil(math.log10(top) - 1e-9))
    rows = []
    for lang, r in sorted(ratios.items(), key=lambda kv: kv[1]):
        width = max(0.8, math.log10(r) / math.log10(limit) * 100)
        tip = f"{lang}: {ratio_text(r)} the fastest" + (f", {seconds(times[lang])}" if times else "")
        cls = "hbar nexo" if lang == "Nexo" else "hbar"
        rows.append(
            f'<li class="{cls}" title="{html.escape(tip)}"><span class="hb-name">{lang}</span>'
            f'<span class="hb-track"><span class="hb-fill" style="width: {width:.1f}%"></span>'
            f'<span class="hb-val" style="left: {width:.1f}%">{ratio_text(r)}</span></span></li>')
    ticks = []
    t = 1
    while t <= limit:
        pos = math.log10(t) / math.log10(limit) * 100
        ticks.append(f'<span style="left: {pos:.1f}%">{t}×</span>')
        t *= 10
    return (f'<figure class="hchart"><figcaption>{html.escape(label)}</figcaption>'
            f'<ol class="hbars">{"".join(rows)}</ol>'
            f'<div class="haxis" aria-hidden="true"><span class="haxis-track">{"".join(ticks)}</span></div></figure>')


def main():
    data = json.loads((HERE / "languages.json").read_text())
    results = data["results"]
    names = [n for n in WHAT if n in results]
    per = {}
    for n in names:
        row = {lang: results[n][lang] for lang in LANGS}
        fastest = min(row.values())
        per[n] = {lang: t / fastest for lang, t in row.items()}
    work = [n for n in names if n != "hello"]
    overall = {lang: math.exp(sum(math.log(per[n][lang]) for n in work) / len(work)) for lang in LANGS}

    tabs, panels = [], []
    views = [("overall", "Overall")] + [(n, n) for n in names]
    for i, (key, title) in enumerate(views):
        on = i == 0
        tabs.append(f'<button class="tab" role="tab" id="pt-{key}" aria-controls="pp-{key}" '
                    f'aria-selected="{"true" if on else "false"}"{"" if on else " tabindex=\"-1\""}>{title}</button>')
        if key == "overall":
            body = chart(overall, "Times longer than the fastest, geometric mean of the "
                                  f"{len(work)} benchmarks (log scale, lower is better)")
        else:
            body = chart(per[key], f"{WHAT[key]}: times longer than the fastest (log scale, lower is better)",
                         results[key])
        panels.append(f'<div class="panel" role="tabpanel" id="pp-{key}" aria-labelledby="pt-{key}"'
                      f'{"" if on else " hidden"}>{body}</div>')

    interp = any("Nexo interpreter" in results[n] for n in names)
    head = "".join(f"<th>{lang}</th>" for lang in LANGS) + ("<th>Nexo interpreter</th>" if interp else "")
    rows = []
    for n in names:
        fastest = min(results[n][lang] for lang in LANGS)
        cells = []
        for lang in LANGS:
            t = results[n][lang]
            cls = " ".join(c for c, on in (("best", t == fastest), ("nexo", lang == "Nexo")) if on)
            cls = f' class="{cls}"' if cls else ""
            cells.append(f"<td{cls}>{seconds(t)}</td>")
        if interp:
            cells.append(f"<td>{seconds(results[n].get('Nexo interpreter'))}</td>")
        rows.append(f'<tr><th scope="row"><code>{n}</code><span>{html.escape(WHAT[n])}</span></th>{"".join(cells)}</tr>')
    versions = "; ".join(f"{k}: {html.escape(v)}" for k, v in data["versions"].items())

    section = f"""<!-- performance:start -->
<section id="performance">
  <div class="wrap">
    <div class="section-head">
      <p class="eyebrow">Performance</p>
      <h2>How fast is Nexo?</h2>
      <p class="section-intro">The same programs in Nexo and six other languages, written the usual way in each and checked to print the same result. Over all of them, compiled Nexo sits with JavaScript and Java: behind C, Go and Rust, and several times faster than Python. It is close to C on integer loops and threads, and ahead of Go, Java and JavaScript on lists. Phase 6 made records almost three times faster; it is still slowest on maps and strings, where every value is boxed and reference-counted, and on strings even Python is faster (<a href="https://github.com/marcrabadan/nexo/blob/main/docs/guarantees.md">the goals and where Nexo stands</a>).</p>
    </div>
    <div class="tabs" role="tablist" aria-label="Benchmarks">{"".join(tabs)}</div>
    {"".join(panels)}
    <div class="table-wrap perf-table-wrap"><table class="ref-table perf-table">
      <caption>Best of {data["runs"]} runs of the whole program, start-up included. The fastest of each row is marked.</caption>
      <thead><tr><th>Benchmark</th>{head}</tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table></div>
    <div class="perf-notes">
      <h3>How it was measured</h3>
      <ul class="ref-list">
        <li>Measured on {html.escape(data["date"])}, on {html.escape(data["machine"])}. {versions}.</li>
        <li>Times include starting the program: <code>hello</code> shows that cost alone. It weighs most on Java and JavaScript, and these programs run for well under a second in the compiled languages.</li>
        <li>Nexo and Rust check every integer operation for overflow; C, Go and Java wrap around silently, and Python's integers never overflow.</li>
        <li>In C and Go a record is a plain value on the stack, so building one costs almost nothing; in Nexo, Java, JavaScript and Python each one is an object on the heap. That is most of the gap on <code>records</code>.</li>
        <li>Each program uses its language's usual tools: <code>ArrayList&lt;Long&gt;</code> and <code>HashMap</code> in Java, a hand-written hash map in C, which has none. Python runs <code>parallel</code> on 4 processes, because its threads share one lock.</li>
        <li>The Nexo interpreter is the phase 0 prototype, a tree-walking evaluator written in Python; it shows the language works, not how fast it runs.</li>
        <li>The programs and the script are in <a href="{SOURCE}"><code>compiler/bench/</code></a>: <code>python3 compiler/bench/languages.py</code> runs them again.</li>
      </ul>
    </div>
  </div>
</section>
<!-- performance:end -->"""
    page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    start, end = page.index("<!-- performance:start -->"), page.index("<!-- performance:end -->")
    page = page[:start] + section + page[end + len("<!-- performance:end -->"):]
    (ROOT / "docs" / "index.html").write_text(page, encoding="utf-8")
    print("wrote the performance section of docs/index.html")


if __name__ == "__main__":
    main()
