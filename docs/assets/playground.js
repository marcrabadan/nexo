// The playground page (D176): the editor, the toolbar and the worker that
// runs the program. A program travels in the link as #code=<base64url>.
(function () {
  "use strict";

  var DRAFT = "nexo-playground-draft";
  var START = '## Say hello to a few people.\nfn greet(name: str) -> str:\n    return "Hello, {name}!"\n\n' +
    'for name in ["Ada", "Grace", "Linus"]:\n    print(greet(name))\n\n' +
    'test "a greeting":\n    assert greet("Sam") == "Hello, Sam!"\n';

  var code = document.getElementById("code");
  var stdin = document.getElementById("stdin");
  var out = document.getElementById("out");
  var status = document.getElementById("status");
  var examples = document.getElementById("examples");
  var buttons = { run: document.getElementById("run"), test: document.getElementById("test"), fmt: document.getElementById("fmt") };
  var stop = document.getElementById("stop");
  var worker = null;
  var running = null;
  var started = 0;
  var catalog = [];

  function encode(text) {
    var bytes = new TextEncoder().encode(text), s = "";
    for (var i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function decode(text) {
    var s = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
    var bytes = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) bytes[i] = s.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  }

  function saveDraft() {
    try { localStorage.setItem(DRAFT, code.value); } catch (e) {}
  }

  function firstSource() {
    var m = /^#code=([A-Za-z0-9_-]*)$/.exec(location.hash);
    if (m) {
      try { return decode(m[1]); } catch (e) {}
    }
    try {
      var d = localStorage.getItem(DRAFT);
      if (d) return d;
    } catch (e) {}
    return START;
  }

  function show(text, cls) {
    var span = document.createElement("span");
    if (cls) span.className = cls;
    span.textContent = text;
    out.appendChild(span);
    out.scrollTop = out.scrollHeight;
  }

  function enable(ready) {
    Object.keys(buttons).forEach(function (k) { buttons[k].disabled = !ready; });
    stop.disabled = ready || !running;
  }

  function startWorker() {
    worker = new Worker("assets/playground-worker.js");
    enable(false);
    worker.onmessage = function (e) {
      var m = e.data;
      if (m.type === "ready") {
        status.textContent = "Ready.";
        enable(true);
      } else if (m.type === "failed") {
        status.textContent = "The playground could not start: " + m.text;
      } else if (m.type === "out") {
        show(m.text, m.fd === 2 ? "err" : "");
      } else if (m.type === "done") {
        var ms = Math.round(performance.now() - started);
        if (m.command === "fmt" && m.code === 0 && m.source !== code.value) {
          code.value = m.source;
          saveDraft();
        }
        var what = { run: "Ran", test: "Ran the tests", fmt: "Formatted" }[m.command];
        status.textContent = what + (m.code === 0 ? "" : " (exit code " + m.code + ")") + " in " + ms + " ms.";
        running = null;
        enable(true);
      }
    };
  }

  function go(command) {
    if (running || buttons[command].disabled) return;
    running = command;
    out.textContent = "";
    if (command === "fmt") show("nexo fmt main.nexo\n", "meta");
    if (command === "test") show("nexo test main.nexo\n", "meta");
    status.textContent = command === "fmt" ? "Formatting…" : "Running…";
    started = performance.now();
    enable(false);
    stop.disabled = false;
    worker.postMessage({ command: command, source: code.value, stdin: stdin.value });
  }

  Object.keys(buttons).forEach(function (k) {
    buttons[k].addEventListener("click", function () { go(k); });
  });

  stop.addEventListener("click", function () {
    if (!running) return;
    worker.terminate();
    running = null;
    show("\nStopped.\n", "meta");
    status.textContent = "Stopped. Starting Python again…";
    startWorker();
  });

  document.getElementById("clear").addEventListener("click", function () { out.textContent = ""; });

  document.getElementById("share").addEventListener("click", function () {
    var url = location.href.split("#")[0] + "#code=" + encode(code.value);
    history.replaceState(null, "", url);
    var done = function () { status.textContent = "Link copied. It holds the whole program."; };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done, function () { status.textContent = "Link in the address bar."; });
    } else {
      status.textContent = "Link in the address bar.";
    }
  });

  // Tab indents by four spaces; Escape, then Tab, leaves the editor
  var escaped = false;
  code.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      go("run");
      return;
    }
    if (e.key === "Escape") { escaped = true; return; }
    if (e.key === "Tab" && !escaped && !e.shiftKey) {
      e.preventDefault();
      var s = code.selectionStart, end = code.selectionEnd;
      code.setRangeText("    ", s, end, "end");
      saveDraft();
      return;
    }
    escaped = false;
  });
  code.addEventListener("input", function () {
    examples.value = "";
    saveDraft();
  });

  examples.addEventListener("change", function () {
    var ex = catalog.filter(function (x) { return x.name === examples.value; })[0];
    if (!ex) return;
    code.value = ex.source;
    stdin.value = ex.stdin;
    out.textContent = "";
    saveDraft();
  });

  fetch("playground/examples.json").then(function (r) { return r.json(); }).then(function (data) {
    catalog = data.examples;
    catalog.forEach(function (ex) {
      var o = document.createElement("option");
      o.value = ex.name;
      o.textContent = ex.name.replace(/_/g, " ");
      examples.appendChild(o);
    });
  });

  code.value = firstSource();
  startWorker();
})();
