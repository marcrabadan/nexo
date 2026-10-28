// The playground's runner (D176): the phase 0 interpreter (nexo.zip) in
// Pyodide, CPython compiled to WebAssembly. It runs in the page's worker
// and in Node for the tests (tests/test_playground.py).
const NexoPlay = (() => {
  "use strict";

  const GLUE = `
import io, os, shutil, sys

def nexo_play(command, source, stdin, files):
    # every run starts from a fresh interpreter: the modules keep state
    for name in [m for m in sys.modules if m == "nexo" or m.startswith("nexo.")]:
        del sys.modules[name]
    shutil.rmtree("/play", ignore_errors=True)
    for path, text in files.items():
        os.makedirs(os.path.dirname("/play/" + path), exist_ok=True)
        with open("/play/" + path, "w", encoding="utf-8") as f:
            f.write(text)
    with open("/play/main.nexo", "w", encoding="utf-8") as f:
        f.write(source)
    os.chdir("/play")
    sys.stdin = io.StringIO(stdin)
    argv = {"run": ["main.nexo"], "test": ["test", "main.nexo"], "fmt": ["fmt", "main.nexo"]}[command]
    from nexo import cli
    try:
        code = cli.main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except ModuleNotFoundError as e:
        return ["need", (e.name or "").split(".")[0], ""]
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.setrecursionlimit(1000)
    with open("/play/main.nexo", encoding="utf-8") as f:
        return ["done", code, f.read()]
`;

  // Pyodide packages a program may need the first time it runs
  const PACKAGES = { cryptography: "cryptography", sqlite3: "sqlite3", _sqlite3: "sqlite3", tzdata: "tzdata" };

  async function start(pyodide, bundle, files) {
    pyodide.unpackArchive(new Uint8Array(bundle), "zip", { extractDir: "/lib/nexo-play" });
    pyodide.runPython('import sys\nsys.path.insert(0, "/lib/nexo-play")');
    pyodide.runPython(GLUE);
    const play = pyodide.globals.get("nexo_play");
    const loaded = new Set();
    let sink = () => {};
    for (const [fd, set] of [[1, "setStdout"], [2, "setStderr"]]) {
      const decoder = new TextDecoder();
      pyodide[set]({ write: (buf) => { sink(decoder.decode(buf, { stream: true }), fd); return buf.length; } });
    }

    // command is "run", "test" or "fmt"; write(text, fd) gets the output;
    // the result is {code, source} (the source as `nexo fmt` left it)
    async function run(command, source, stdin, write) {
      for (;;) {
        let out = [];
        sink = (text, fd) => out.push([text, fd]);
        const result = play(command, source, stdin || "", pyodide.toPy(files)).toJs();
        sink = () => {};
        const [what, value, after] = result;
        const pkg = what === "need" ? PACKAGES[value] : null;
        if (pkg && !loaded.has(pkg)) {
          // load it and run again from the start, with nothing shown yet
          loaded.add(pkg);
          await pyodide.loadPackage(pkg);
          continue;
        }
        for (const [text, fd] of out) write(text, fd);
        if (what === "need") {
          write(`This program needs the Python module ${value}, which the playground does not have.\n`, 2);
          return { code: 1, source };
        }
        return { code: value, source: after };
      }
    }

    return { run };
  }

  return { start };
})();

if (typeof module !== "undefined") module.exports = NexoPlay;
