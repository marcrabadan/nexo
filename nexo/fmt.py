"""`nexo fmt` (section 14): the formatter. It has no options, so all Nexo
code looks the same (D85).

It works on the lexer's tokens, so it never changes what a program means:
the formatted file has exactly the same tokens as the original (checked
before anything is written). It keeps the line breaks the author chose and
fixes everything else:

- one space around binary operators, after `,` and `:`, none inside
  brackets, before `(` / `[` of a call or index, around `.` and `..`, after
  unary `-` or before a postfix `?` / `!`;
- continuation lines: `|>` lines two spaces deeper than their statement,
  lines inside brackets four spaces deeper than the line that opened them,
  and a line that starts by closing a bracket at the indentation of the
  line that opened it;
- comments on their own line take the indentation of the code around
  them; trailing comments of consecutive lines are aligned, two spaces
  after the longest line;
- at most one blank line in a row, none at the start of the file or right
  after a line that opens a block; no trailing spaces; the file ends with
  exactly one newline.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import NexoError
from .lexer import Interp, Token, tokenize

OPENERS = ("(", "[", "{")
CLOSERS = (")", "]", "}")
VALUE_WORDS = ("true", "false", "none", "self")
SYNTHETIC = ("NEWLINE", "INDENT", "DEDENT", "EOF")


@dataclass
class Piece:
    """A token as written in the source."""
    tok: Token
    text: str
    line: int  # first line
    end_line: int  # last line (differs only for multi-line text)
    col: int  # 0-based start
    end: int  # 0-based end on end_line


@dataclass
class Row:
    """A logical line of output: tokens joined on one line (a multi-line
    text may make it span several), plus an optional trailing comment."""
    pieces: list[Piece] = field(default_factory=list)
    comment: str | None = None
    line: int = 0
    end_line: int = 0
    indent: int = 0  # original indentation
    blank_before: int = 0
    cfg: bool = False  # inside the body of an infra, stage, pipeline or flags block (D114)


def format_source(source: str, file: str = "<input>") -> str:
    """The formatted text of `source`. Raises NexoError if it does not lex."""
    source = source.replace("\r\n", "\n")
    tokens = tokenize(source, file)
    lines = source.split("\n")
    rows = build_rows(source, lines, tokens)
    out = render_rows(rows)
    check_same_tokens(tokens, out, file)
    tokenize(source, file)  # keep the original text for error snippets
    return out


# ---------------------------------------------------------------- rows

def build_rows(source: str, lines: list[str], tokens: list[Token]) -> list[Row]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    pieces: list[Piece] = []
    for tok in tokens:
        if tok.kind in SYNTHETIC:
            continue
        line = tok.span.line
        col = tok.span.col - 1
        if tok.kind == "STRING" and lines[line - 1].startswith('"""', col):
            start = offsets[line - 1] + col
            stop = source.index('"""', start + 3) + 3
            text = source[start:stop]
            end_line = line + text.count("\n")
            end = len(text) - text.rfind("\n") - 1 if "\n" in text else col + len(text)
        else:
            text = lines[line - 1][col:tok.span.end_col - 1]
            end_line, end = line, tok.span.end_col - 1
        pieces.append(Piece(tok, text, line, end_line, col, end))

    rows: list[Row] = []
    covered = 0  # last line that belongs to an existing row
    k = 0
    for n, text in enumerate(lines, start=1):
        if n <= covered:
            continue
        row = Row(line=n, end_line=n)
        row.indent = len(text) - len(text.lstrip(" "))
        cursor_line, cursor = n, 0
        while k < len(pieces) and pieces[k].line == cursor_line:
            piece = pieces[k]
            row.pieces.append(piece)
            cursor_line, cursor = piece.end_line, piece.end
            k += 1
        row.end_line = cursor_line
        rest = lines[cursor_line - 1][cursor:].strip()
        if rest.startswith("#"):
            row.comment = rest.rstrip()
        covered = row.end_line
        rows.append(row)

    # blank lines become a count on the next row
    result: list[Row] = []
    blanks = 0
    for row in rows:
        if not row.pieces and row.comment is None:
            blanks += 1
            continue
        row.blank_before = blanks
        blanks = 0
        result.append(row)
    mark_cfg(result)
    return result


CFG_BLOCKS = ("infra", "stage", "pipeline", "flags")


def mark_cfg(rows: list[Row]) -> None:
    """Rows inside phase 5 blocks: there a space before `(` or `[` starts a
    new word of the phrase, and `2027-03-01` is a date (D114)."""
    inside = False
    for row in rows:
        if row.pieces and row.indent == 0:
            first, last = row.pieces[0].tok, row.pieces[-1].tok
            inside = first.kind == "NAME" and first.value in CFG_BLOCKS and last.is_op(":")
            continue
        row.cfg = inside and bool(row.pieces)


# ------------------------------------------------------------- spacing

def ends_operand(tok: Token | None) -> bool:
    if tok is None:
        return False
    if tok.kind in ("NAME", "INT", "FLOAT", "UNIT", "STRING"):
        return True
    if tok.kind == "KEYWORD":
        return tok.value in VALUE_WORDS
    return tok.kind == "OP" and tok.value in (")", "]", "}", "?", "!")


def gap(prev: Piece | None, a: Piece, b: Piece, cfg: bool = False) -> str:
    """The space between two tokens of the same row; `prev` comes before a."""
    ta, tb = a.tok, b.tok
    same_place = a.end_line == b.line and a.end == b.col
    if cfg and same_place and ((ta.kind == "INT" and tb.is_op("-"))
                               or (ta.is_op("-") and tb.kind == "INT" and prev is not None and prev.tok.kind == "INT")):
        return ""  # a date: 2027-03-01
    if cfg and tb.kind == "OP" and tb.value in ("(", "[") and ends_operand(ta):
        return "" if same_place else " "
    if ta.kind == "KEYWORD" and ta.value == "test" and tb.is_op("("):
        return ""  # test() in a step of a pipeline
    if tb.kind == "OP" and tb.value in (")", "]", "}", ",", ".", ":", "?", "!", ".."):
        return ""
    if ta.kind == "OP" and ta.value in ("(", "[", "{", ".", "@", ".."):
        return ""
    if ta.kind == "OP" and ta.value == "-" and not ends_operand(prev.tok if prev else None):
        return ""  # unary minus
    if tb.kind == "OP" and tb.value in ("(", "["):
        if ta.kind == "NAME" and ta.value == "returns" and prev is not None and prev.tok.kind == "NAME":
            return " "  # `fake ask returns [...]` is not an index
        if ta.kind == "NAME" and prev is not None and prev.tok.is_op("|>"):
            # a pipeline step: `|> save(...)` is a call, `|> map [..]` is not
            same_place = a.end_line == b.line and a.end == b.col
            return "" if same_place else " "
        if ends_operand(ta):
            return ""
    return " "


# ---------------------------------------------------------- rendering

def row_code(before: Piece | None, row: Row) -> str:
    """The tokens of a row joined with their spaces; `before` is the token
    that precedes the row."""
    parts: list[str] = []
    for i, piece in enumerate(row.pieces):
        if i:
            prev = row.pieces[i - 2] if i >= 2 else before
            parts.append(gap(prev, row.pieces[i - 1], piece, row.cfg))
        parts.append(piece.text)
    return "".join(parts)


def render_rows(rows: list[Row]) -> str:
    indents = row_indents(rows)
    texts: list[tuple[str, str | None]] = []  # (code with indentation, comment)
    blank_before: list[bool] = []
    before: Piece | None = None
    depth = 0
    opened_block = False  # the last row ended with ":" outside brackets
    for i, row in enumerate(rows):
        blank_before.append(row.blank_before > 0 and bool(texts) and not opened_block)
        code = row_code(before, row) if row.pieces else ""
        texts.append((" " * indents[i] + code, row.comment))
        if not row.pieces:
            opened_block = False
            continue
        for piece in row.pieces:
            t = piece.tok
            if t.kind == "OP" and t.value in OPENERS:
                depth += 1
            elif t.kind == "OP" and t.value in CLOSERS:
                depth -= 1
        before = row.pieces[-1]
        opened_block = depth == 0 and before.tok.is_op(":")
    return join_lines(texts, blank_before)


def row_indents(rows: list[Row]) -> list[int]:
    indents = [0] * len(rows)
    stack: list[int] = []  # for each open bracket, the indentation of the row that opened it
    statement_indent = 0  # the row where the current statement starts
    pending: list[int] = []  # comment rows waiting for the next code row
    for i, row in enumerate(rows):
        if not row.pieces:
            pending.append(i)
            continue
        first = row.pieces[0].tok
        if stack:
            indent = indent_in_brackets(stack, row)
        elif first.is_op("|>"):
            indent = statement_indent + 2
        else:
            indent = row.indent
        for c in pending:
            indents[c] = comment_indent(rows[c], stack, indent, statement_indent, row)
        pending = []
        if not stack and not first.is_op("|>"):
            statement_indent = indent
        indents[i] = indent
        for piece in row.pieces:
            t = piece.tok
            if t.kind == "OP" and t.value in OPENERS:
                stack.append(indent)
            elif t.kind == "OP" and t.value in CLOSERS and stack:
                stack.pop()
    for c in pending:
        indents[c] = comment_indent(rows[c], stack, 0, statement_indent, None)
    return indents


def is_continuation_start(row: Row) -> bool:
    return bool(row.pieces) and row.pieces[0].tok.is_op("|>")


def indent_in_brackets(stack: list[int], row: Row) -> int:
    first = row.pieces[0].tok
    if first.kind == "OP" and first.value in CLOSERS:
        return stack[-1]
    return stack[-1] + 4


def comment_indent(comment: Row, stack: list[int], next_indent: int, prev_indent: int,
                   next_row: Row | None) -> int:
    """Indentation of a comment on its own line."""
    if stack:
        return stack[-1] + 4
    if next_row is not None and is_continuation_start(next_row):
        return next_indent
    original = comment.indent
    if original <= next_indent:
        return next_indent
    # deeper than the next line: it closes the block above, if it fits there
    deepest = max(prev_indent, next_indent)
    return max(next_indent, min(original - original % 4, deepest))


def join_lines(rows: list[tuple[str, str | None]], blank_before: list[bool]) -> str:
    # align the trailing comments of consecutive rows
    i = 0
    n = len(rows)
    aligned: list[str] = []
    while i < n:
        code, comment = rows[i]
        if comment is not None and code.strip():
            j = i
            while (j + 1 < n and rows[j + 1][1] is not None and rows[j + 1][0].strip()
                   and not blank_before[j + 1] and "\n" not in rows[j + 1][0]):
                j += 1
            if "\n" in code:
                j = i
            width = max(last_width(rows[k][0]) for k in range(i, j + 1))
            for k in range(i, j + 1):
                c, com = rows[k]
                aligned.append(c + " " * (width - last_width(c) + 2) + com)
            i = j + 1
            continue
        if comment is not None:
            aligned.append(code + comment)
        else:
            aligned.append(code.rstrip() if "\n" not in code else code)
        i += 1
    out: list[str] = []
    for k, text in enumerate(aligned):
        if blank_before[k]:
            out.append("")
        out.append(text)
    return "\n".join(out) + "\n" if out else ""


def last_width(code: str) -> int:
    return len(code) - code.rfind("\n") - 1


# ---------------------------------------------------------- the check

def token_key(tok: Token):
    if tok.kind == "STRING":
        return ("STRING", tuple(("{", p.source) if isinstance(p, Interp) else p for p in tok.value))
    return (tok.kind, tok.value)


def check_same_tokens(tokens: list[Token], out: str, file: str) -> None:
    """The formatted text must have exactly the original tokens."""
    try:
        again = tokenize(out, file)
    except NexoError as exc:  # pragma: no cover - a formatter bug
        raise AssertionError(f"nexo fmt broke {file}: {exc}") from exc
    if [token_key(t) for t in tokens] != [token_key(t) for t in again]:  # pragma: no cover
        raise AssertionError(f"nexo fmt changed the tokens of {file}")
