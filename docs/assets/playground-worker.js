// The playground's worker (D176): Pyodide and the Nexo interpreter, away
// from the page, so a program that never ends can be stopped.
const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";
importScripts(PYODIDE + "pyodide.js", "playground-core.js");

const ready = (async () => {
  const pyodide = await loadPyodide({ indexURL: PYODIDE });
  const [bundle, examples] = await Promise.all([
    fetch("../playground/nexo.zip").then((r) => r.arrayBuffer()),
    fetch("../playground/examples.json").then((r) => r.json()),
  ]);
  const runner = await NexoPlay.start(pyodide, bundle, examples.files);
  postMessage({ type: "ready" });
  return runner;
})();
ready.catch((e) => postMessage({ type: "failed", text: String(e) }));

onmessage = async (event) => {
  const { command, source, stdin } = event.data;
  const runner = await ready;
  try {
    const result = await runner.run(command, source, stdin, (text, fd) => postMessage({ type: "out", text, fd }));
    postMessage({ type: "done", command, code: result.code, source: result.source });
  } catch (e) {
    postMessage({ type: "out", text: String(e) + "\n", fd: 2 });
    postMessage({ type: "done", command, code: 1, source });
  }
};
