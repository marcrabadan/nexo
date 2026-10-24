"""text: search, regular expressions and templates (section 15, D95).

Regular expressions use the syntax both implementations share (the one of
Rust's regex crate, which never backtracks): no lookarounds and no
backreferences; `$` is the end of the text unless (?m) is given.
"""
from __future__ import annotations

import html
import re
from typing import Any

from .. import errors
from ..errors import NexoError
from ..types import Builtin, Native, failure, repr_value, show

UNSUPPORTED = ("(?=", "(?!", "(?<=", "(?<!", "(?>", "(?P=", "(?#")


def translate(pattern: str) -> str | None:
    """The pattern for Python's re, or None when it uses something the
    shared syntax does not have."""
    if any(u in pattern for u in UNSUPPORTED):
        return None
    out = []
    i = 0
    in_class = False
    multiline = False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            if i + 1 >= len(pattern):
                return None
            n = pattern[i + 1]
            if n.isdigit() and n != "0" and not in_class:
                return None  # backreference
            if n in "ZpPG":
                return None
            if n == "z":
                out.append("\\Z")
            else:
                out.append(pattern[i:i + 2])
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            elif c == "[" and pattern.startswith("[:", i):
                return None
            out.append(c)
            i += 1
            continue
        if c == "[":
            in_class = True
            out.append(c)
            i += 1
            if i < len(pattern) and pattern[i] == "^":
                out.append("^")
                i += 1
            if i < len(pattern) and pattern[i] == "]":
                out.append("\\]")
                i += 1
            continue
        if c == "(" and pattern.startswith("(?", i):
            m = re.match(r"\(\?([imsxU-]+)([:)])", pattern[i:])
            if m:
                if "U" in m.group(1):
                    return None
                if "m" in m.group(1).split("-")[0]:
                    multiline = True
            elif pattern.startswith("(?<", i) and not pattern.startswith("(?<=", i):
                # (?<name>...) is (?P<name>...) in Python
                out.append("(?P<")
                i += 3
                continue
        if c in "*+?" and i + 1 < len(pattern) and pattern[i + 1] == "+":
            return None  # possessive
        if c == "$" and not multiline:
            out.append("\\Z")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


class Regex(Native):
    type_name = "regex"
    __slots__ = ("pattern", "re")

    def __init__(self, pattern: str, compiled):
        self.pattern = pattern
        self.re = compiled

    def __eq__(self, other):
        return isinstance(other, Regex) and other.pattern == self.pattern

    __hash__ = object.__hash__

    def repr(self) -> str:
        return f"regex({repr_value(self.pattern)})"

    def member(self, call, name: str):
        methods = {"matches": self.matches, "find": self.find, "find_all": self.find_all,
                   "replace": self.replace, "split": self.split, "groups": self.groups}
        if name in methods:
            return Builtin(name, methods[name])
        return self.missing(call, name, list(methods))

    @staticmethod
    def text(call, fn: str, value: Any) -> str:
        if not isinstance(value, str):
            from .core import expects
            raise expects(call, fn, "str", value)
        return value

    def matches(self, call, text):
        return self.re.fullmatch(self.text(call, "matches", text)) is not None

    def find(self, call, text):
        m = self.re.search(self.text(call, "find", text))
        return None if m is None else m.group(0)

    def find_all(self, call, text):
        return [m.group(0) for m in self.re.finditer(self.text(call, "find_all", text))]

    def replace(self, call, text, with_):
        new = self.text(call, "replace", with_)
        return self.re.sub(lambda m: new, self.text(call, "replace", text))

    def split(self, call, text):
        t = self.text(call, "split", text)
        out, last = [], 0
        for m in self.re.finditer(t):
            out.append(t[last:m.start()])
            last = m.end()
        out.append(t[last:])
        return out

    def groups(self, call, text):
        m = self.re.search(self.text(call, "groups", text))
        return None if m is None else list(m.groups())


def b_regex(call, pattern):
    if not isinstance(pattern, str):
        from .core import expects
        raise expects(call, "regex", "str", pattern)
    translated = translate(pattern)
    compiled = None
    if translated is not None:
        try:
            compiled = re.compile(translated)
        except re.error:
            compiled = None
    if compiled is None:
        raise NexoError("bad_regex", call.span, pattern=pattern)
    return Regex(pattern, compiled)


PLACEHOLDER = re.compile(r"\{\{|\{([^\W\d]\w*)\}")


def b_render(call, path, **values):
    """A template file with {name} placeholders; in .html files the values
    are escaped (section 9)."""
    from .io import check_path, read_text
    path = check_path(call, path, "render")
    template = read_text(call, path)
    escape = path.lower().endswith((".html", ".htm"))
    interp = call.interp

    def fill(m):
        if m.group(0) == "{{":
            return "{"
        name = m.group(1)
        if name not in values:
            raise failure("InvalidData", call.span, detail=errors.msg("detail.template", path=path, name=name))
        text = show(interp.bind_value(values[name]))
        return html.escape(text, quote=True) if escape else text
    return PLACEHOLDER.sub(fill, template)


def m_find(call, obj, sub):
    if not isinstance(sub, str):
        from .core import expects
        raise expects(call, "find", "str", sub)
    i = obj.find(sub)
    return None if i < 0 else i


def exports() -> dict:
    return {
        "regex": Builtin("regex", b_regex),
        "render": Builtin("render", b_render, fallible=True),
    }
