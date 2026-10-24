"""A small LSP client for the tests: runs `nexo lsp` and plays a session."""
import json
import subprocess


class Client:
    def __init__(self, cmd, cwd=None):
        self.proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.next_id = 0
        self.notifications = []

    def write(self, message):
        body = json.dumps(message).encode("utf-8")
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        self.proc.stdin.flush()

    def read(self):
        length = None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise EOFError("the server closed its output")
            line = line.strip()
            if not line:
                break
            name, _, value = line.decode("ascii").partition(":")
            if name.lower() == "content-length":
                length = int(value)
        return json.loads(self.proc.stdout.read(length))

    def request(self, method, params):
        self.next_id += 1
        self.write({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            message = self.read()
            if message.get("id") == self.next_id:
                return message
            self.notifications.append(message)

    def notify(self, method, params):
        self.write({"jsonrpc": "2.0", "method": method, "params": params})

    def diagnostics(self, uri):
        """Wait for the next diagnostics of `uri`."""
        while True:
            for n, message in enumerate(self.notifications):
                if message.get("method") == "textDocument/publishDiagnostics" and message["params"]["uri"] == uri:
                    del self.notifications[n]
                    return message["params"]["diagnostics"]
            self.notifications.append(self.read())

    def close(self):
        self.request("shutdown", None)
        self.notify("exit", None)
        code = self.proc.wait(timeout=30)
        self.proc.stdin.close()
        self.proc.stdout.close()
        return code


def session(cmd, text, uri="file:///work/app.nexo", locale="en"):
    """Open a document and ask every kind of question; return the answers."""
    c = Client(cmd)
    out = {}
    out["initialize"] = c.request("initialize", {"processId": None, "rootUri": None, "capabilities": {},
                                                 "locale": locale})["result"]
    c.notify("initialized", {})
    c.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "nexo", "version": 1,
                                                       "text": text}})
    diags = c.diagnostics(uri)
    out["diagnostics"] = diags
    doc = {"uri": uri}
    whole = {"start": {"line": 0, "character": 0}, "end": {"line": len(text.split("\n")), "character": 0}}
    out["actions"] = c.request("textDocument/codeAction", {"textDocument": doc, "range": whole,
                                                          "context": {"diagnostics": diags}})["result"]
    out["formatting"] = c.request("textDocument/formatting", {"textDocument": doc,
                                                             "options": {"tabSize": 4, "insertSpaces": True}})["result"]
    out["symbols"] = c.request("textDocument/documentSymbol", {"textDocument": doc})["result"]
    lines = text.split("\n")
    out["definition"] = {}
    out["hover"] = {}
    for n, line in enumerate(lines):
        for ch in range(len(line)):
            if line[ch].isalpha() and (ch == 0 or not (line[ch - 1].isalnum() or line[ch - 1] == "_")):
                pos = {"textDocument": doc, "position": {"line": n, "character": ch}}
                d = c.request("textDocument/definition", pos)["result"]
                if d is not None:
                    out["definition"][f"{n}:{ch}"] = d
                h = c.request("textDocument/hover", pos)["result"]
                if h is not None:
                    out["hover"][f"{n}:{ch}"] = h
    last = len(lines) - 1
    out["completion"] = c.request("textDocument/completion", {"textDocument": doc,
                                                             "position": {"line": last, "character": 0}})["result"]
    out["exit"] = c.close()
    return out
