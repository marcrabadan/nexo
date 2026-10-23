"""Lexer: source text -> tokens, following the lexical rules of section 24.

- UTF-8; identifiers accept Unicode letters, digits and `_`.
- Indentation is exactly 4 spaces per level; a tab is an error. INDENT and
  DEDENT are emitted when the level changes.
- A newline ends a statement, except inside ( ) [ ] { } or when the next
  line starts with `|>`.
- `#` starts a comment; `##` is a documentation comment.
- Numbers: 1_000, 3.14, 1e-3. A number followed by a unit or currency
  (30s, 10%, 5 km, 19.99 EUR) is a unit literal.
- Text: "..." with {expr} interpolation, triple-quoted multi-line text,
  and {{ for a literal brace.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import NexoError, SOURCES, Span

# The 39 core reserved words (section 27).
KEYWORDS = frozenset(
    """
    if elif else for in while repeat match pass
    fn return type trait is pub use var self weak
    true false none and or not as
    try unsafe expect ensure
    parallel on emit when every at lifecycle
    test assert
    """.split()
)

# Units written right after the number (30s, 10%, 300ms) or after a space
# (5 km, 2 days, 19.99 EUR). Single-letter units and % must be attached.
TIME_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "day": 86400, "days": 86400,
              "year": 31536000, "years": 31536000}
SIZE_UNITS = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}
LENGTH_UNITS = {"km": 1}
CURRENCIES = {
    "EUR": 2, "USD": 2, "GBP": 2, "JPY": 0, "CHF": 2, "CAD": 2, "AUD": 2, "CNY": 2,
    "MXN": 2, "ARS": 2, "BRL": 2, "CLP": 0, "COP": 2, "PEN": 2, "UYU": 2, "SEK": 2,
    "NOK": 2, "DKK": 2, "PLN": 2, "INR": 2,
}
UNITS = {**TIME_UNITS, **SIZE_UNITS, **LENGTH_UNITS, "%": 1}
ATTACHED_ONLY = {"s", "m", "h", "B", "%"}

OPERATORS = [
    "|>", "->", "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "**", "//", "..", "??",
    "+", "-", "*", "/", "%", "<", ">", "=", "(", ")", "[", "]", "{", "}", ",", ":",
    ".", "?", "!", "@", "|",
]
OPEN = {"(": ")", "[": "]", "{": "}"}
CLOSE = {")": "(", "]": "[", "}": "{"}


@dataclass
class Interp:
    """An {expr} inside a text literal: its source and where it starts."""
    source: str
    line: int
    col: int


@dataclass
class Token:
    kind: str  # NAME KEYWORD INT FLOAT UNIT STRING OP NEWLINE INDENT DEDENT EOF
    value: Any
    span: Span

    def is_op(self, *ops: str) -> bool:
        return self.kind == "OP" and self.value in ops

    def is_kw(self, *words: str) -> bool:
        return self.kind == "KEYWORD" and self.value in words

    def __repr__(self) -> str:
        return f"{self.kind}({self.value!r})@{self.span.line}:{self.span.col}"


def is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


class Lexer:
    def __init__(self, source: str, file: str, line: int = 1, col: int = 1, expr_mode: bool = False):
        self.src = source
        self.file = file
        self.i = 0
        self.line = line
        self.line_start = 0 - (col - 1)  # index of the first char of the current line
        self.expr_mode = expr_mode
        self.tokens: list[Token] = []
        self.indents = [0]
        self.brackets: list[Token] = []

    # -- helpers ---------------------------------------------------------
    def col(self, i: int | None = None) -> int:
        return (self.i if i is None else i) - self.line_start + 1

    def span(self, start: int, end: int | None = None) -> Span:
        end = self.i if end is None else end
        return Span(self.file, self.line, self.col(start), self.col(end))

    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.src[j] if j < len(self.src) else ""

    def add(self, kind: str, value: Any, span: Span) -> None:
        self.tokens.append(Token(kind, value, span))

    def word_end(self, j: int) -> int:
        while j < len(self.src) and is_ident_char(self.src[j]):
            j += 1
        return j

    # -- main loop -------------------------------------------------------
    def tokenize(self) -> list[Token]:
        src = self.src
        at_line_start = not self.expr_mode
        while self.i < len(src):
            if at_line_start and not self.brackets:
                at_line_start = False
                if self.indentation():
                    at_line_start = True
                continue
            c = src[self.i]
            if c == "\n":
                if not self.brackets and not self.expr_mode:
                    self.newline_token()
                self.i += 1
                self.line += 1
                self.line_start = self.i
                at_line_start = True
                continue
            if c in " \r":
                self.i += 1
                continue
            if c == "\t":
                raise NexoError("tab", self.span(self.i, self.i + 1))
            if c == "#":
                while self.i < len(src) and src[self.i] != "\n":
                    self.i += 1
                continue
            if c.isdigit():
                self.number()
                continue
            if is_ident_start(c):
                start = self.i
                self.i = self.word_end(self.i)
                word = src[start:self.i]
                self.add("KEYWORD" if word in KEYWORDS else "NAME", word, self.span(start))
                continue
            if c == '"':
                self.string()
                continue
            self.operator()
        if self.brackets:
            tok = self.brackets[-1]
            raise NexoError("unclosed_bracket", tok.span, open=tok.value, close=OPEN[tok.value])
        if not self.expr_mode:
            self.newline_token()
            end = self.span(self.i)
            while len(self.indents) > 1:
                self.indents.pop()
                self.add("DEDENT", None, end)
        self.add("EOF", None, self.span(self.i))
        return self.tokens

    def newline_token(self) -> None:
        if self.tokens and self.tokens[-1].kind not in ("NEWLINE", "INDENT", "DEDENT"):
            self.add("NEWLINE", None, self.span(self.i, self.i + 1))

    def indentation(self) -> bool:
        """Process the indentation of a line. Returns True if the line was
        blank or a comment (so the caller should start over on the next)."""
        src = self.src
        j = self.i
        tab = None
        while j < len(src) and src[j] in " \t\r":
            if src[j] == "\t" and tab is None:
                tab = j
            j += 1
        if j >= len(src) or src[j] == "\n" or src[j] == "#":
            # blank or comment-only line
            while j < len(src) and src[j] != "\n":
                j += 1
            self.i = j
            if j < len(src):
                self.i += 1
                self.line += 1
                self.line_start = self.i
            return True
        first = self.span(j, max(self.word_end(j), j + 1))
        if tab is not None:
            raise NexoError("indent_tab", first)
        count = j - self.i
        self.i = j
        if src.startswith("|>", j):
            # continuation line: the statement goes on
            if self.tokens and self.tokens[-1].kind == "NEWLINE":
                self.tokens.pop()
            return False
        current = self.indents[-1]
        if count > current:
            if count != current + 4:
                raise NexoError("indent_size", first, n=count, expected=current + 4)
            self.indents.append(count)
            self.add("INDENT", None, first)
        elif count < current:
            while self.indents[-1] > count:
                self.indents.pop()
                self.add("DEDENT", None, first)
            if self.indents[-1] != count:
                raise NexoError("dedent_mismatch", first)
        return False

    def number(self) -> None:
        src = self.src
        start = self.i
        is_float = False
        j = self.i
        while j < len(src) and (src[j].isdigit() or src[j] == "_"):
            j += 1
        if j + 1 < len(src) and src[j] == "." and src[j + 1].isdigit():
            is_float = True
            j += 1
            while j < len(src) and (src[j].isdigit() or src[j] == "_"):
                j += 1
        if j < len(src) and src[j] in "eE":
            k = j + 1
            if k < len(src) and src[k] in "+-":
                k += 1
            if k < len(src) and src[k].isdigit():
                is_float = True
                j = k
                while j < len(src) and src[j].isdigit():
                    j += 1
        raw = src[start:j]
        if raw.endswith("_") or "__" in raw or "_." in raw or "._" in raw:
            raise NexoError("bad_number", self.span(start, j), text=raw)
        text = raw.replace("_", "")
        self.i = j
        # unit attached to the number: 30s, 10%, 300ms, 5km
        if j < len(src) and (src[j] == "%" or is_ident_start(src[j])):
            if src[j] == "%":
                unit, end = "%", j + 1
            else:
                end = self.word_end(j)
                unit = src[j:end]
            if unit not in UNITS and unit not in CURRENCIES:
                raise NexoError("unknown_unit", self.span(j, end), unit=unit)
            self.i = end
            self.add("UNIT", (text, is_float, unit), self.span(start))
            return
        # unit after a space: 5 km, 2 days, 19.99 EUR
        k = j
        while k < len(src) and src[k] == " ":
            k += 1
        if k > j and k < len(src) and is_ident_start(src[k]):
            end = self.word_end(k)
            unit = src[k:end]
            if (unit in UNITS and unit not in ATTACHED_ONLY) or unit in CURRENCIES:
                self.i = end
                self.add("UNIT", (text, is_float, unit), self.span(start))
                return
        self.add("FLOAT" if is_float else "INT", text, self.span(start))

    def string(self) -> None:
        src = self.src
        start = self.i
        start_line, start_col = self.line, self.col()
        if src.startswith('"""', self.i):
            end = src.find('"""', self.i + 3)
            if end < 0:
                raise NexoError("unterminated_string", self.span(start, start + 3), quote='"""')
            raw = src[self.i + 3:end]
            parts = self.multiline_parts(raw, start_line, start_col + 3)
            newlines = raw.count("\n")
            self.i = end + 3
            span = Span(self.file, start_line, start_col, start_col + 3 if newlines else self.col())
            if newlines:
                self.line += newlines
                self.line_start = src.rfind("\n", 0, end) + 1
            self.add("STRING", parts, span)
            return
        j = self.i + 1
        while True:
            if j >= len(src) or src[j] == "\n":
                raise NexoError("unterminated_string", self.span(start, j), quote='"')
            if src[j] == "\\":
                j += 2
                continue
            if src.startswith("{{", j):
                j += 2
                continue
            if src[j] == "{":
                j = self.skip_interp(j, start)
                continue
            if src[j] == '"':
                break
            j += 1
        raw = src[self.i + 1:j]
        parts = self.parts(raw, start_line, start_col + 1)
        self.i = j + 1
        self.add("STRING", parts, self.span(start))

    def skip_interp(self, j: int, start: int) -> int:
        """j is at '{'; return the index after the matching '}'."""
        src = self.src
        depth = 0
        while j < len(src) and src[j] != "\n":
            c = src[j]
            if c == '"':
                j += 1
                while j < len(src) and src[j] not in '"\n':
                    j += 2 if src[j] == "\\" else 1
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        raise NexoError("unterminated_string", self.span(start, j), quote='"')

    def multiline_parts(self, raw: str, line: int, col: int) -> list:
        lines = raw.split("\n")
        positions = [(line + n, col if n == 0 else 1) for n in range(len(lines))]
        if len(lines) > 1 and lines[0].strip() == "":
            lines, positions = lines[1:], positions[1:]
        if len(lines) > 1 and lines[-1].strip() == "":
            lines, positions = lines[:-1], positions[:-1]
        if any("\t" in l[: len(l) - len(l.lstrip())] for l in lines):
            raise NexoError("tab", Span(self.file, positions[0][0], 1, 2))
        widths = [len(l) - len(l.lstrip(" ")) for l in lines if l.strip()]
        cut = min(widths) if widths else 0
        parts: list = []
        for n, text in enumerate(lines):
            if n:
                parts.append("\n")
            ln, c = positions[n]
            parts.extend(self.parts(text[cut:], ln, c + cut))
        return merge_parts(parts)

    def parts(self, raw: str, line: int, col: int) -> list:
        """Split text content into literal strings and Interp pieces."""
        parts: list = []
        buf: list[str] = []
        j = 0
        escapes = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "r": "\r"}
        while j < len(raw):
            c = raw[j]
            if c == "\\" and j + 1 < len(raw) and raw[j + 1] in escapes:
                buf.append(escapes[raw[j + 1]])
                j += 2
                continue
            if c == "{":
                if raw.startswith("{{", j):
                    buf.append("{")
                    j += 2
                    continue
                depth, k = 0, j
                while k < len(raw):
                    if raw[k] == '"':
                        k += 1
                        while k < len(raw) and raw[k] != '"':
                            k += 2 if raw[k] == "\\" else 1
                    elif raw[k] == "{":
                        depth += 1
                    elif raw[k] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                if k >= len(raw):
                    raise NexoError("unclosed_bracket", Span(self.file, line, col + j, col + j + 1), open="{", close="}")
                inner = raw[j + 1:k]
                if not inner.strip():
                    raise NexoError("empty_interp", Span(self.file, line, col + j, col + k + 1))
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append(Interp(inner, line, col + j + 1))
                j = k + 1
                continue
            buf.append(c)
            j += 1
        if buf:
            parts.append("".join(buf))
        return parts

    def operator(self) -> None:
        src = self.src
        for op in OPERATORS:
            if src.startswith(op, self.i):
                start = self.i
                self.i += len(op)
                span = self.span(start)
                if op in OPEN:
                    self.brackets.append(Token("OP", op, span))
                elif op in CLOSE:
                    if not self.brackets or self.brackets[-1].value != CLOSE[op]:
                        raise NexoError("unmatched_bracket", span, ch=op)
                    self.brackets.pop()
                self.add("OP", op, span)
                return
        raise NexoError("bad_char", self.span(self.i, self.i + 1), ch=src[self.i])


def merge_parts(parts: list) -> list:
    out: list = []
    for p in parts:
        if isinstance(p, str) and out and isinstance(out[-1], str):
            out[-1] += p
        else:
            out.append(p)
    return out


def tokenize(source: str, file: str = "<input>") -> list[Token]:
    SOURCES[file] = source
    return Lexer(source, file).tokenize()


def tokenize_expr(source: str, file: str, line: int, col: int) -> list[Token]:
    """Tokens of an interpolated expression (no NEWLINE/INDENT handling)."""
    return Lexer(source, file, line, col, expr_mode=True).tokenize()
