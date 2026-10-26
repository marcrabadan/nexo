"""Modules of the program's own files (section 13, D124).

`use shop.payments` loads `shop/payments.nexo` from the root of the
project: the folder of the nearest nexo.toml above the program, or else the
program's own folder. Its `pub` functions and types (and the variants of a
`pub` enum) are used qualified, `payments.add(...)`, or with the name given
by `as`. Everything is checked before the program starts: a missing file, a
cycle, a name the module does not export, the module's name used as a value
or bound to something else.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from . import ast as A
from .errors import NexoError, SOURCES, Span


def is_file_module(s: A.Use) -> bool:
    """`use shop.payments`, not `use nexo.db`, `use python m` or `use c "lib"`."""
    return s.lang is None and s.name != "nexo" and not s.name.startswith("nexo.")


def bound_name(s: A.Use) -> str:
    return s.alias or s.name.rsplit(".", 1)[-1]


def project_root(file: str) -> Path:
    start = Path(file).resolve().parent
    for d in [start, *start.parents]:
        if (d / "nexo.toml").is_file():
            return d
    return start


def shown_path(path: Path) -> str:
    """The path of a module file as messages show it: relative to the
    current folder when it is inside it."""
    cwd = Path.cwd().resolve()
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


@dataclasses.dataclass
class ModuleInfo:
    name: str
    file: str
    program: A.Program
    exports: set[str]
    private: set[str]


def exported(program: A.Program) -> tuple[set[str], set[str]]:
    """(pub names, other top-level names) of a module."""
    exports: set[str] = set()
    private: set[str] = set()
    for s in program.body:
        if isinstance(s, A.FnDef):
            (exports if s.pub else private).add(s.name)
        elif isinstance(s, A.TypeDef):
            names = [s.name] + [v.name for v in s.variants]
            (exports if s.pub else private).update(names)
        elif isinstance(s, A.TraitDef):
            private.add(s.name)
        elif isinstance(s, A.Assign) and isinstance(s.target, A.Name):
            private.add(s.target.name)
        elif isinstance(s, (A.VarDecl, A.SharedDecl)):
            private.add(s.name)
    return exports, private - exports


class ModuleValue:
    """A module at run time: the names of its file."""

    def __init__(self, name: str, env):
        self.name = name
        self.env = env


class Linker:
    """Loads the modules a program uses, once each, and checks the names it
    takes from them."""

    def __init__(self, main_file: str):
        self.root = project_root(main_file)
        self.modules: dict[str, ModuleInfo] = {}
        # the modules being loaded, outermost first (the program is the first)
        main = Path(main_file).resolve()
        try:
            first = ".".join(main.with_suffix("").relative_to(self.root).parts)
        except ValueError:
            first = main.stem
        self.stack: list[str] = [first]
        # the modules of the program itself, kept between the inputs of the
        # REPL (the compiler's REPL compiles the whole session)
        self.main_aliases: dict[str, ModuleInfo] = {}

    def link(self, program: A.Program) -> None:
        from .checker import walk_stmts
        top = {id(s) for s in program.body}
        for s in walk_stmts(program.body):
            if isinstance(s, A.Use) and is_file_module(s) and id(s) not in top:
                raise NexoError("use_top_level", s.span, name=s.name)
        main = len(self.stack) == 1
        aliases: dict[str, ModuleInfo] = dict(self.main_aliases) if main else {}
        for s in program.body:
            if isinstance(s, A.Use) and is_file_module(s):
                info = self.load(s)
                name = bound_name(s)
                if name in aliases:
                    raise NexoError("module_name_taken", s.span, name=name, module=aliases[name].name)
                aliases[name] = info
        if aliases:
            References(aliases).node(program)
        if main:
            self.main_aliases = aliases

    def load(self, s: A.Use) -> ModuleInfo:
        from .checker import Checker
        from .parser import parse
        name = s.name
        if name in self.stack:
            chain = self.stack[self.stack.index(name):] + [name]
            raise NexoError("module_cycle", s.span, chain=" -> ".join(chain))
        if name in self.modules:
            return self.modules[name]
        path = self.root.joinpath(*name.split(".")).with_suffix(".nexo")
        file = shown_path(path)
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            raise NexoError("module_not_found", s.span, name=name, path=file) from None
        SOURCES[file] = source
        program = parse(source, file)
        from .parser import is_policy
        for st in program.body:
            if is_policy(st):
                raise NexoError("policy_module", st.span, module=name)
        Checker().check(program)
        self.stack.append(name)
        self.link(program)
        self.stack.pop()
        exports, private = exported(program)
        info = ModuleInfo(name, file, program, exports, private)
        self.modules[name] = info
        return info


class References:
    """Checks every use of a module's name in one file."""

    def __init__(self, aliases: dict[str, ModuleInfo]):
        self.aliases = aliases

    def member(self, alias: str, name: str, span: Span) -> None:
        info = self.aliases[alias]
        if name in info.exports:
            return
        if name in info.private:
            raise NexoError("module_private", span, name=name, module=info.name, path=info.file)
        from .errors import suggest
        s = suggest(name, sorted(info.exports))
        if s:
            raise NexoError("module_no_member", span, fix="suggest", name=name, module=info.name,
                            suggestion=f"{alias}.{s}")
        raise NexoError("module_no_member", span, name=name, module=info.name)

    def dotted(self, name: str, span: Span) -> None:
        """A type or pattern name: `payments.Order`."""
        alias, dot, member = name.partition(".")
        if dot and alias in self.aliases:
            self.member(alias, member.split(".")[0], span)
        elif not dot:
            self.bind(name, span)

    def bind(self, name: str | None, span: Span) -> None:
        if name in self.aliases:
            raise NexoError("module_name_taken", span, name=name, module=self.aliases[name].name)

    def node(self, n) -> None:
        if isinstance(n, A.Assign) and isinstance(n.target, A.Attr) and isinstance(n.target.obj, A.Name) \
                and n.target.obj.name in self.aliases:
            info = self.aliases[n.target.obj.name]
            self.member(n.target.obj.name, n.target.name, n.target.span)
            raise NexoError("module_assign", n.target.span, name=f"{n.target.obj.name}.{n.target.name}",
                            module=info.name, path=info.file)
        if isinstance(n, A.Attr) and isinstance(n.obj, A.Name) and n.obj.name in self.aliases:
            self.member(n.obj.name, n.name, n.span)
            return
        if isinstance(n, A.Name):
            if n.name in self.aliases:
                raise NexoError("module_value", n.span, name=n.name)
            return
        if isinstance(n, A.Use):
            return
        if isinstance(n, A.TypeName):
            if "." in n.name:
                self.dotted(n.name, n.span)
        elif isinstance(n, A.PName):
            self.dotted(n.name, n.span)
        elif isinstance(n, A.PCtor):
            if "." in n.name:
                self.dotted(n.name, n.span)
        elif isinstance(n, (A.FnDef, A.TypeDef, A.VariantDef, A.Param)):
            self.bind(n.name, getattr(n, "name_span", n.span))
            if isinstance(n, A.TypeDef) and n.trait and "." in n.trait:
                self.dotted(n.trait, n.name_span)
        elif isinstance(n, A.TraitDef):
            self.bind(n.name, n.span)
        elif isinstance(n, (A.VarDecl, A.SharedDecl)):
            self.bind(n.name, n.name_span)
        elif isinstance(n, A.Assign) and isinstance(n.target, A.Name) and n.target.name in self.aliases:
            self.bind(n.target.name, n.target.span)
        elif isinstance(n, (A.For, A.Comprehension, A.ParallelFor, A.Handler, A.SelectArm)):
            self.bind(n.var, n.span)
        elif isinstance(n, A.Lambda):
            self.bind(n.param, n.span)
        elif isinstance(n, A.EventStmt):
            self.bind(n.alias, n.span)
        for f in dataclasses.fields(n):
            if f.name == "span":
                continue
            self.value(getattr(n, f.name))

    def value(self, v) -> None:
        if isinstance(v, A.Node):
            self.node(v)
        elif isinstance(v, (list, tuple)):
            for x in v:
                self.value(x)

