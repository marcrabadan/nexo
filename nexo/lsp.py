"""`nexo lsp` (section 14, D87): a language server for any editor, over
standard input and output (JSON-RPC with Content-Length headers).

It offers: diagnostics as you type (the compile error of the file, or the
warnings of `nexo lint`), quick fixes for them and "apply the safe fixes"
(`nexo fix`), formatting (`nexo fmt`), go to definition, hover with the
signature and the `##` comments, the outline of the file, and completion.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

from . import __version__, errors
from . import ast as A
from .errors import NexoError, Span
from .fmt import format_source
from .lexer import KEYWORDS, Token, tokenize
from .lint import Edit, apply_edits, error_fix, fix_source, lint_source
from .parser import Parser

BUILTINS = [
    "print", "input", "len", "range", "set", "round", "split", "convert", "fail", "validate", "PI", "half_up",
    "half_even", "int", "float", "decimal", "money", "str", "bool", "list", "map",
    "Error", "NotFound", "IOError", "InvalidData", "Overflow", "DivisionByZero", "OutOfRange",
    "ContractBroken", "Timeout", "InvalidValue",
    "read", "write", "read_lines", "save", "json", "csv",
    # the library of phase 4 (D89)
    "move_file", "now", "today", "date", "time", "regex", "render", "secret", "password", "key", "keypair",
    "encrypt", "decrypt", "encrypt_file", "decrypt_file", "sign", "verify", "log", "http", "jwt", "ask", "email",
    "NetworkError", "HTTPError", "AIError", "DatabaseError",
]
BUILTIN_FUNCTIONS = {"print", "input", "len", "range", "set", "round", "split", "convert", "fail", "validate",
                     "decimal", "float", "int", "str", "read", "write", "read_lines", "save",
                     "move_file", "date", "time", "regex", "render", "secret", "encrypt", "decrypt", "encrypt_file",
                     "decrypt_file", "sign", "verify", "log", "jwt", "ask"}
MEMBERS = ["add", "cause", "close", "count", "ends_with", "keys", "kind", "line", "lower", "message", "parse",
           "receive", "replace", "send", "split", "starts_with", "sum", "trim", "upper", "values",
           # members of the values of phase 4
           "all", "find", "handle", "headers", "remove", "status", "text", "url"]

# LSP kinds
SYMBOL = {"function": 12, "method": 6, "struct": 23, "enum": 10, "field": 8, "member": 22,
          "interface": 11, "variable": 13, "constant": 14}
COMPLETION = {"function": 3, "method": 2, "field": 5, "variable": 6, "constant": 21, "type": 7,
              "member": 20, "interface": 8, "keyword": 14, "param": 6}


# ------------------------------------------------------------ positions

def utf16_col(text: str, col: int) -> int:
    """LSP character offset (UTF-16) of the 1-based column `col` of a line."""
    prefix = text[:col - 1]
    return len(prefix) + sum(1 for c in prefix if ord(c) > 0xFFFF)


def from_utf16(text: str, character: int) -> int:
    """1-based column of an LSP character offset in a line."""
    units = 0
    for i, c in enumerate(text):
        if units >= character:
            return i + 1
        units += 2 if ord(c) > 0xFFFF else 1
    return len(text) + 1


def lsp_range(lines: list[str], line: int, col: int, end_line: int, end_col: int) -> dict:
    def pos(l: int, c: int) -> dict:
        if l < 1:
            return {"line": 0, "character": 0}
        if l > len(lines):
            return {"line": len(lines) - 1, "character": utf16_col(lines[-1], len(lines[-1]) + 1)}
        text = lines[l - 1]
        c = max(1, min(c, len(text) + 1))
        return {"line": l - 1, "character": utf16_col(text, c)}
    return {"start": pos(line, col), "end": pos(end_line, end_col)}


def span_range(lines: list[str], span: Span | None) -> dict:
    if span is None:
        return lsp_range(lines, 1, 1, 1, 1)
    return lsp_range(lines, span.line, span.col, span.line, span.end_col)


def edit_json(lines: list[str], e: Edit) -> dict:
    return {"range": lsp_range(lines, e.line, e.col, e.end_line, e.end_col), "newText": e.text}


# ------------------------------------------------------------ diagnostics

def diagnostics(text: str, name: str) -> list[dict]:
    lines = text.split("\n")
    out = []
    try:
        findings = lint_source(text, name)
    except NexoError as err:
        fixes = []
        fix = error_fix(err, text)
        if fix is not None:
            fix_key = "fix." + (err.fix or err.key)
            title = errors.msg(fix_key, **err.args_) if errors.has(fix_key) else err.text()
            fixes.append({"title": title, "safe": fix.safe, "edits": [edit_json(lines, e) for e in fix.edits]})
        out.append({"range": span_range(lines, err.span), "severity": 1, "source": "nexo", "code": err.key,
                    "message": err.text(), "data": {"fixes": fixes}})
        return out
    for f in findings:
        fixes = []
        if f.fix is not None:
            fixes.append({"title": errors.msg("fix.lint." + f.key, **f.args), "safe": f.fix.safe,
                          "edits": [edit_json(lines, e) for e in f.fix.edits]})
        out.append({"range": span_range(lines, f.span), "severity": 2, "source": "nexo", "code": f.key,
                    "message": f.text(), "data": {"fixes": fixes}})
    return out


def whole_document(text: str, new_text: str) -> dict:
    lines = text.split("\n")
    return {"range": lsp_range(lines, 1, 1, len(lines), len(lines[-1]) + 1), "newText": new_text}


def formatting(text: str, name: str) -> list[dict] | None:
    try:
        formatted = format_source(text, name)
    except NexoError:
        return None
    return [] if formatted == text else [whole_document(text, formatted)]


def code_actions(text: str, name: str, uri: str, params: dict) -> list[dict]:
    context = params.get("context") or {}
    only = context.get("only")
    actions = []
    if not only or any(k.startswith("quickfix") for k in only):
        for d in context.get("diagnostics") or []:
            for fix in (d.get("data") or {}).get("fixes") or []:
                actions.append({"title": fix["title"], "kind": "quickfix", "diagnostics": [d],
                                "isPreferred": bool(fix["safe"]),
                                "edit": {"changes": {uri: fix["edits"]}}})
    if not only or any(k.startswith("source") for k in only):
        fixed, count = fix_source(text, name) if parses(text, name) else (text, 0)
        if count:
            actions.append({"title": errors.msg("lsp_fix_all"), "kind": "source.fixAll.nexo",
                            "edit": {"changes": {uri: [whole_document(text, fixed)]}}})
    return actions


def parses(text: str, name: str) -> bool:
    try:
        Parser(tokenize(text, name), name).program()
    except NexoError:
        return False
    return True


# ---------------------------------------------------------------- index

@dataclass
class Decl:
    name: str
    kind: str  # function method struct enum field member interface variable constant param
    line: int
    col: int
    end_col: int
    end_line: int  # last line of what it defines (its block)
    scope: tuple[int, int] | None  # lines where it is visible; None: everywhere
    detail: str
    doc: str = ""
    children: list["Decl"] = field(default_factory=list)
    member: bool = False  # reached with `.name`


class Index:
    """Declarations of a file, for definition, hover, outline and completion."""

    def __init__(self, text: str, name: str):
        self.text = text
        self.lines = text.split("\n")
        self.tokens: list[Token] = tokenize(text, name)
        self.program = Parser(list(self.tokens), name).program()
        self.decls: list[Decl] = []  # flat
        self.top: list[Decl] = []  # the outline
        self.seen: set[tuple] = set()
        self.body(self.program.body, None, self.top)

    # -- source helpers ---------------------------------------------------
    def line_tokens(self, line: int) -> list[Token]:
        return [t for t in self.tokens if t.span.line == line and t.kind not in ("NEWLINE", "INDENT", "DEDENT", "EOF")]

    def header(self, line: int) -> str:
        """The code of a definition line, up to the `:` that opens its block."""
        toks = self.line_tokens(line)
        if not toks:
            return ""
        end = toks[-1]
        colons = [t for t in toks if t.is_op(":")]
        text = self.lines[line - 1]
        if colons and colons[-1] is toks[-1]:
            return text[toks[0].span.col - 1:colons[-1].span.col - 1].rstrip()
        return text[toks[0].span.col - 1:end.span.end_col - 1].rstrip()

    def code(self, line: int) -> str:
        toks = self.line_tokens(line)
        if not toks:
            return ""
        return self.lines[line - 1][toks[0].span.col - 1:toks[-1].span.end_col - 1].rstrip()

    def doc(self, line: int) -> str:
        found = []
        n = line - 1
        while n >= 1 and self.lines[n - 1].strip().startswith("##"):
            found.append(self.lines[n - 1].strip()[2:].strip())
            n -= 1
        return "\n".join(reversed(found))

    def block_end(self, line: int, col: int) -> int:
        """Last line of the statement at (line, col), with its block."""
        toks = self.tokens
        k = 0
        while k < len(toks) and ((toks[k].span.line, toks[k].span.col) < (line, col)
                                 or toks[k].kind in ("INDENT", "DEDENT", "NEWLINE")):
            k += 1
        depth = 0
        last = line
        while k < len(toks):
            tok = toks[k]
            if tok.kind == "INDENT":
                depth += 1
            elif tok.kind == "DEDENT":
                depth -= 1
                if depth <= 0:
                    break
            elif tok.kind == "EOF":
                break
            elif tok.kind == "NEWLINE":
                if depth == 0 and not (k + 1 < len(toks) and toks[k + 1].kind == "INDENT"):
                    break
            else:
                last = tok.span.line
            k += 1
        return last

    def name_after(self, line: int, col: int) -> Token | None:
        """The first NAME token after (line, col): the name after `for` or `trait`."""
        for tok in self.tokens:
            if (tok.span.line, tok.span.col) > (line, col) and tok.kind == "NAME":
                return tok
        return None

    # -- walking ------------------------------------------------------------
    def add(self, d: Decl, into: list[Decl] | None) -> Decl:
        key = (d.name, d.scope, d.kind in ("variable", "constant"))
        if d.kind in ("variable", "constant") and key in self.seen:
            return d
        self.seen.add(key)
        self.decls.append(d)
        if into is not None:
            into.append(d)
        return d

    def fn(self, f: A.FnDef, scope, into, kind: str) -> None:
        end = self.block_end(f.span.line, f.span.col)
        span = f.name_span
        self.add(Decl(f.name, kind, span.line, span.col, span.end_col, end, scope, self.header(f.span.line),
                      self.doc(f.span.line), member=kind == "method"), into)
        inner = (f.span.line, end)
        for p in f.params:
            detail = p.name + (f": {type_text(p.type)}" if p.type is not None else "")
            self.add(Decl(p.name, "param", p.span.line, p.span.col, p.span.end_col, p.span.line, inner, detail), None)
        self.body(f.body, inner, None)

    def body(self, body: list[A.Stmt], scope, into) -> None:
        for s in body:
            self.stmt(s, scope, into)

    def stmt(self, s: A.Stmt, scope, into) -> None:
        if isinstance(s, A.FnDef):
            self.fn(s, scope, into, "function")
        elif isinstance(s, A.TypeDef):
            end = self.block_end(s.span.line, s.span.col)
            kind = "struct" if s.fields else "enum"
            span = s.name_span
            d = self.add(Decl(s.name, kind, span.line, span.col, span.end_col, end, scope, self.header(s.span.line),
                              self.doc(s.span.line)), into)
            for f in s.fields:
                self.add(Decl(f.name, "field", f.span.line, f.span.col, f.span.end_col, f.span.line, scope,
                              self.code(f.span.line), self.doc(f.span.line), member=True), d.children)
            for v in s.variants:
                self.add(Decl(v.name, "member", v.span.line, v.span.col, v.span.col + len(v.name), v.span.line,
                              scope, self.code(v.span.line), self.doc(v.span.line)), d.children)
            for m in s.methods:
                self.fn(m, scope, d.children, "method")
        elif isinstance(s, A.TraitDef):
            tok = self.name_after(s.span.line, s.span.col)
            if tok is not None:
                end = self.block_end(s.span.line, s.span.col)
                self.add(Decl(s.name, "interface", tok.span.line, tok.span.col, tok.span.end_col, end, scope,
                              self.header(s.span.line), self.doc(s.span.line)), into)
        elif isinstance(s, A.TestDef):
            end = self.block_end(s.span.line, s.span.col)
            self.body(s.body, (s.span.line, end), None)
        elif isinstance(s, A.VarDecl):
            span = s.name_span
            self.add(Decl(s.name, "variable", span.line, span.col, span.end_col, span.line, scope,
                          self.code(s.span.line), self.doc(s.span.line)), into)
        elif isinstance(s, A.SharedDecl):
            span = s.name_span
            self.add(Decl(s.name, "variable", span.line, span.col, span.end_col, span.line, scope,
                          self.code(span.line), self.doc(span.line)), into)
        elif isinstance(s, A.Assign) and isinstance(s.target, A.Name) and s.op == "=":
            span = s.target.span
            self.add(Decl(s.target.name, "constant", span.line, span.col, span.end_col, span.line, scope,
                          self.code(span.line), self.doc(span.line)), into)
        elif isinstance(s, A.For):
            end = self.block_end(s.span.line, s.span.col)
            tok = self.name_after(s.span.line, s.span.col)
            inner = (s.span.line, end)
            if tok is not None:
                self.add(Decl(s.var, "variable", tok.span.line, tok.span.col, tok.span.end_col, tok.span.line,
                              inner, f"for {s.var}"), None)
            self.body(s.body, inner, None)
        elif isinstance(s, A.If):
            for _, b in s.branches:
                self.body(b, scope, into)
            if s.orelse:
                self.body(s.orelse, scope, into)
        elif isinstance(s, (A.While, A.Repeat)):
            end = self.block_end(s.span.line, s.span.col)
            self.body(s.body, (s.span.line, end), None)
        elif isinstance(s, A.Match):
            for arm in s.arms:
                self.body(arm.body, scope, into)
        elif isinstance(s, A.TryStmt):
            self.body(s.body, scope, into)
            for h in s.handlers:
                self.body(h.body, scope, into)
        elif isinstance(s, A.ParallelBlock):
            self.body(s.body, scope, into)

    # -- queries ----------------------------------------------------------
    def word_at(self, line: int, col: int) -> tuple[str, int, int] | None:
        """The identifier at a 1-based position: (name, start col, end col)."""
        if not 1 <= line <= len(self.lines):
            return None
        text = self.lines[line - 1]
        i = col - 1
        if not (0 <= i < len(text) and is_ident(text[i])) and i > 0 and is_ident(text[i - 1]):
            i -= 1
        if not (0 <= i < len(text) and is_ident(text[i])):
            return None
        a = i
        while a > 0 and is_ident(text[a - 1]):
            a -= 1
        b = i
        while b < len(text) and is_ident(text[b]):
            b += 1
        word = text[a:b]
        if word[0].isdigit():
            return None
        return word, a + 1, b + 1

    def lookup(self, line: int, col: int) -> Decl | None:
        found = self.word_at(line, col)
        if found is None:
            return None
        word, start, _ = found
        text = self.lines[line - 1]
        member = start > 1 and text[start - 2] == "."
        candidates = [d for d in self.decls if d.name == word]
        if member:
            for d in candidates:
                if d.member:
                    return d
            return None
        visible = [d for d in candidates if not d.member and (d.scope is None or d.scope[0] <= line <= d.scope[1])]
        if not visible:
            return None
        inner = max(visible, key=lambda d: -1 if d.scope is None else d.scope[0])
        same = [d for d in visible if d.scope == inner.scope]
        before = [d for d in same if (d.line, d.col) <= (line, col)]
        return before[-1] if before else same[0]


def type_text(t) -> str:
    from .checker import type_text as text
    out = text(t)
    if isinstance(t, A.TypeName) and t.fallible:
        out += "!"
    return out


def is_ident(c: str) -> bool:
    return c.isalnum() or c == "_"


def definition(index: Index, uri: str, line: int, col: int):
    d = index.lookup(line, col)
    if d is None:
        return None
    return {"uri": uri, "range": lsp_range(index.lines, d.line, d.col, d.line, d.end_col)}


def hover(index: Index, line: int, col: int):
    d = index.lookup(line, col)
    if d is None:
        return None
    value = f"```nexo\n{d.detail}\n```"
    if d.doc:
        value += "\n\n" + d.doc
    found = index.word_at(line, col)
    _, a, b = found
    return {"contents": {"kind": "markdown", "value": value}, "range": lsp_range(index.lines, line, a, line, b)}


def symbols(index: Index) -> list[dict]:
    def one(d: Decl) -> dict:
        return {"name": d.name, "detail": d.detail, "kind": SYMBOL.get(d.kind, 13),
                "range": lsp_range(index.lines, d.line, 1, d.end_line, len(index.lines[d.end_line - 1]) + 1),
                "selectionRange": lsp_range(index.lines, d.line, d.col, d.line, d.end_col),
                "children": [one(c) for c in d.children]}
    return [one(d) for d in index.top]


def completion(index: Index | None, text: str, line: int, col: int) -> list[dict]:
    lines = text.split("\n")
    current = lines[line - 1] if 1 <= line <= len(lines) else ""
    i = min(col - 1, len(current))
    while i > 0 and is_ident(current[i - 1]):
        i -= 1
    items: dict[str, dict] = {}

    def put(label: str, kind: str, detail: str = "") -> None:
        if label not in items:
            items[label] = {"label": label, "kind": COMPLETION[kind]}
            if detail:
                items[label]["detail"] = detail

    if i > 0 and current[i - 1] == ".":
        if index is not None:
            for d in index.decls:
                if d.member:
                    put(d.name, "method" if d.kind == "method" else "field", d.detail)
        for m in MEMBERS:
            put(m, "method")
        return sorted(items.values(), key=lambda x: x["label"])
    if index is not None:
        for d in index.decls:
            if d.member:
                continue
            if d.scope is not None and not d.scope[0] <= line <= d.scope[1]:
                continue
            kind = {"struct": "type", "enum": "type"}.get(d.kind, d.kind)
            put(d.name, kind, d.detail)
    for b in BUILTINS:
        put(b, "function" if b in BUILTIN_FUNCTIONS else "constant")
    for k in sorted(KEYWORDS):
        put(k, "keyword")
    return sorted(items.values(), key=lambda x: x["label"])


# --------------------------------------------------------------- server

class Server:
    def __init__(self, inp, out):
        self.inp = inp
        self.out = out
        self.docs: dict[str, str] = {}
        self.indexes: dict[str, Index] = {}  # the last version that parsed
        self.shutdown = False

    def send(self, message: dict) -> None:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        self.out.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        self.out.flush()

    def read(self) -> dict | None:
        length = None
        while True:
            line = self.inp.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            name, _, value = line.decode("ascii").partition(":")
            if name.lower() == "content-length":
                length = int(value.strip())
        if length is None:
            return None
        return json.loads(self.inp.read(length).decode("utf-8"))

    def run(self) -> int:
        while True:
            message = self.read()
            if message is None:
                return 0 if self.shutdown else 1
            method = message.get("method")
            if method == "exit":
                return 0 if self.shutdown else 1
            try:
                result = self.handle(method, message.get("params") or {})
            except Exception as exc:  # pragma: no cover - keep serving
                if "id" in message:
                    self.send({"jsonrpc": "2.0", "id": message["id"],
                               "error": {"code": -32603, "message": str(exc)}})
                continue
            if "id" in message:
                if result is NotImplemented:
                    self.send({"jsonrpc": "2.0", "id": message["id"],
                               "error": {"code": -32601, "message": f"unknown method {method}"}})
                else:
                    self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def name(self, uri: str) -> str:
        return uri.rsplit("/", 1)[-1]

    def index(self, uri: str) -> Index | None:
        text = self.docs.get(uri)
        if text is None:
            return None
        try:
            idx = Index(text, self.name(uri))
        except NexoError:
            return self.indexes.get(uri)
        self.indexes[uri] = idx
        return idx

    def publish(self, uri: str) -> None:
        text = self.docs.get(uri)
        diags = diagnostics(text, self.name(uri)) if text is not None else []
        self.send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                   "params": {"uri": uri, "diagnostics": diags}})

    def position(self, uri: str, params: dict) -> tuple[int, int]:
        pos = params["position"]
        lines = self.docs.get(uri, "").split("\n")
        line = pos["line"] + 1
        text = lines[line - 1] if 1 <= line <= len(lines) else ""
        return line, from_utf16(text, pos["character"])

    def handle(self, method: str, params: dict):
        if method == "initialize":
            return {"capabilities": {
                "positionEncoding": "utf-16",
                "textDocumentSync": {"openClose": True, "change": 1},
                "documentFormattingProvider": True,
                "codeActionProvider": {"codeActionKinds": ["quickfix", "source.fixAll.nexo"]},
                "definitionProvider": True,
                "hoverProvider": True,
                "documentSymbolProvider": True,
                "completionProvider": {"triggerCharacters": ["."]},
            }, "serverInfo": {"name": "nexo", "version": __version__}}
        if method == "initialized":
            return None
        if method == "shutdown":
            self.shutdown = True
            return None
        if method == "textDocument/didOpen":
            doc = params["textDocument"]
            self.docs[doc["uri"]] = doc["text"]
            self.publish(doc["uri"])
            return None
        if method == "textDocument/didChange":
            uri = params["textDocument"]["uri"]
            changes = params.get("contentChanges") or []
            if changes:
                self.docs[uri] = changes[-1]["text"]
            self.publish(uri)
            return None
        if method == "textDocument/didClose":
            uri = params["textDocument"]["uri"]
            self.docs.pop(uri, None)
            self.indexes.pop(uri, None)
            self.send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                       "params": {"uri": uri, "diagnostics": []}})
            return None
        if method in ("textDocument/didSave", "$/cancelRequest", "$/setTrace", "workspace/didChangeConfiguration"):
            return None
        uri = (params.get("textDocument") or {}).get("uri", "")
        text = self.docs.get(uri)
        if method == "textDocument/formatting":
            return None if text is None else formatting(text, self.name(uri))
        if method == "textDocument/codeAction":
            return [] if text is None else code_actions(text, self.name(uri), uri, params)
        if method == "textDocument/definition":
            idx = self.index(uri)
            return None if idx is None else definition(idx, uri, *self.position(uri, params))
        if method == "textDocument/hover":
            idx = self.index(uri)
            return None if idx is None else hover(idx, *self.position(uri, params))
        if method == "textDocument/documentSymbol":
            idx = self.index(uri)
            return [] if idx is None else symbols(idx)
        if method == "textDocument/completion":
            if text is None:
                return []
            return completion(self.index(uri), text, *self.position(uri, params))
        if method.startswith("$/"):
            return None
        return NotImplemented


def serve() -> int:
    return Server(sys.stdin.buffer, sys.stdout.buffer).run()


def apply(text: str, edits: list[Edit]) -> str:  # used by the tests
    return apply_edits(text, edits)
