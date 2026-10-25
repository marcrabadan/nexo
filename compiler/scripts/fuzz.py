#!/usr/bin/env python3
"""Differential fuzzing of the compiler (section "Goals and guarantees":
"continuous fuzzing of the compiler", D133). It writes random programs
that are correct Nexo by construction (every name defined before it is
used, every operation on values of the right types), runs each one with
the phase 0 interpreter and with the compiled binary, and checks that:

- the compiler never crashes (a Rust panic, a signal, a broken link);
- both print the same thing and end with the same exit code (an
  overflow, a division by zero or an index out of range are errors of
  the program, and both must report them alike);
- the leak checker finds nothing (NEXO_LEAKCHECK=1).

A failing program is written to compiler/tests/fuzz/NAME.nexo with the two
outputs, so it can become a test once it is fixed.

Usage: fuzz.py [--nexo=path] [--seed=N] [--count=N]
"""
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "compiler" / "tests" / "fuzz"


class Gen:
    def __init__(self, rnd: random.Random):
        self.r = rnd
        self.lines: list[str] = []
        self.fns: list[tuple[str, int]] = []  # (name, number of int params)
        self.types: list[str] = []  # record types with fields x, y: int and a method sum()
        self.n = 0

    def fresh(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    # ------------------------------------------------------------ expressions

    def int_expr(self, env: dict, depth: int = 0) -> str:
        r = self.r
        ints = [v for v, t in env.items() if t == "int"]
        lists = [v for v, t in env.items() if t == "list"]
        recs = [(v, t) for v, t in env.items() if t.startswith("R")]
        choices = ["lit", "lit"]
        if ints:
            choices += ["var"] * 3
        if depth < 3:
            choices += ["bin", "bin", "neg"]
            if self.fns:
                choices.append("call")
            if lists:
                choices += ["len", "index", "sum"]
            if recs:
                choices += ["field", "method"]
        c = r.choice(choices)
        if c == "lit":
            return str(r.choice([0, 1, 2, 3, 7, 10, 100, -5, 2**31, 9223372036854775807]))
        if c == "var":
            return r.choice(ints)
        if c == "bin":
            op = r.choice(["+", "-", "*", "//", "%", "+", "-"])
            return f"({self.int_expr(env, depth + 1)} {op} {self.int_expr(env, depth + 1)})"
        if c == "neg":
            return f"-{self.int_expr(env, depth + 1)}"
        if c == "call":
            name, k = r.choice(self.fns)
            return f"{name}({', '.join(self.int_expr(env, depth + 1) for _ in range(k))})"
        if c == "len":
            return f"len({r.choice(lists)})"
        if c == "index":
            return f"{r.choice(lists)}[{self.int_expr(env, depth + 1)} % 4]"
        if c == "sum":
            lst = r.choice(lists)
            return r.choice([f"({lst} |> where it > 0 |> sum)", f"({lst} |> map it * 2 |> sum)",
                             f"({lst} |> count)", f"{lst}.sum()"])
        if c == "field":
            v, _ = r.choice(recs)
            return f"{v}.{r.choice(['x', 'y'])}"
        v, _ = r.choice(recs)
        return f"{v}.total()"

    def bool_expr(self, env: dict, depth: int = 0) -> str:
        r = self.r
        c = r.choice(["cmp", "cmp", "cmp", "lit", "and", "not"] if depth < 2 else ["cmp", "lit"])
        if c == "lit":
            return r.choice(["true", "false"])
        if c == "and":
            return f"({self.bool_expr(env, depth + 1)} {r.choice(['and', 'or'])} {self.bool_expr(env, depth + 1)})"
        if c == "not":
            return f"not {self.bool_expr(env, depth + 1)}"
        op = r.choice(["<", "<=", ">", ">=", "==", "!="])
        return f"{self.int_expr(env, depth + 1)} {op} {self.int_expr(env, depth + 1)}"

    def str_expr(self, env: dict) -> str:
        r = self.r
        strs = [v for v, t in env.items() if t == "str"]
        c = r.choice(["lit", "interp", "concat", "conv"] + (["var"] if strs else []))
        if c == "lit":
            return r.choice(['"a"', '"hello world"', '""', '"x y z"'])
        if c == "var":
            return r.choice(strs)
        if c == "interp":
            return f'"v={{{self.int_expr(env, 1)}}}"'
        if c == "concat":
            return f"{self.str_expr(env)} + {self.str_expr(env)}"
        return f"str({self.int_expr(env, 1)})"

    def list_expr(self, env: dict) -> str:
        r = self.r
        lists = [v for v, t in env.items() if t == "list"]
        c = r.choice(["lit", "comp"] + (["copy"] if lists else []))
        if c == "lit" or not lists and c == "comp":
            return "[" + ", ".join(self.int_expr(env, 2) for _ in range(r.randint(0, 5))) + "]"
        if c == "comp":
            return f"[x * 2 for x in {r.choice(lists)} if x % 2 == 0]"
        return r.choice(lists)

    # ------------------------------------------------------------ statements

    def block(self, env: dict, indent: int, depth: int, n: int, in_fn: bool = False) -> None:
        for _ in range(n):
            self.stmt(env, indent, depth, in_fn)

    def emit(self, indent: int, text: str) -> None:
        self.lines.append("    " * indent + text)

    def stmt(self, env: dict, indent: int, depth: int, in_fn: bool) -> None:
        r = self.r
        kinds = ["print", "print", "assign", "assign", "str", "list"]
        if depth < 2:
            kinds += ["if", "for", "while", "repeat", "try"]
        if self.types:
            kinds.append("rec")
        if any(t == "list" for t in env.values()):
            kinds.append("add")
        k = r.choice(kinds)
        if k == "print":
            e = r.choice([self.int_expr, self.str_expr, self.bool_expr, self.list_expr])(env)
            self.emit(indent, f"print({e})")
        elif k == "assign":
            vars_ = [v for v, t in env.items() if t == "int" and v.startswith("v")]
            if vars_ and r.random() < 0.5:
                v = r.choice(vars_)
                self.emit(indent, f"{v} {r.choice(['=', '+=', '-='])} {self.int_expr(env)}")
            else:
                v = self.fresh("v")
                self.emit(indent, f"var {v} = {self.int_expr(env)}")
                env[v] = "int"
        elif k == "str":
            v = self.fresh("s")
            self.emit(indent, f"{v} = {self.str_expr(env)}")
            env[v] = "str"
        elif k == "list":
            v = self.fresh("l")
            self.emit(indent, f"var {v} = {self.list_expr(env)}")
            env[v] = "list"
        elif k == "add":
            v = r.choice([v for v, t in env.items() if t == "list"])
            self.emit(indent, f"{v}.add({self.int_expr(env)})")
        elif k == "rec":
            t = r.choice(self.types)
            v = self.fresh("p")
            self.emit(indent, f"{v} = {t}({self.int_expr(env, 2)}, {self.int_expr(env, 2)})")
            env[v] = t
        elif k == "if":
            self.emit(indent, f"if {self.bool_expr(env)}:")
            self.block(dict(env), indent + 1, depth + 1, r.randint(1, 3), in_fn)
            if r.random() < 0.5:
                self.emit(indent, f"elif {self.bool_expr(env)}:")
                self.block(dict(env), indent + 1, depth + 1, r.randint(1, 2), in_fn)
            if r.random() < 0.5:
                self.emit(indent, "else:")
                self.block(dict(env), indent + 1, depth + 1, r.randint(1, 2), in_fn)
        elif k == "for":
            v = self.fresh("i")
            lists = [x for x, t in env.items() if t == "list"]
            src = r.choice(lists) if lists and r.random() < 0.5 else f"1..{r.randint(0, 6)}"
            self.emit(indent, f"for {v} in {src}:")
            inner = dict(env)
            inner[v] = "int"
            self.block(inner, indent + 1, depth + 1, r.randint(1, 3), in_fn)
        elif k == "while":
            v = self.fresh("w")
            self.emit(indent, f"var {v} = 0")
            env[v] = "int"
            self.emit(indent, f"while {v} < {r.randint(0, 4)}:")
            self.emit(indent + 1, f"{v} += 1")
            self.block(dict(env), indent + 1, depth + 1, r.randint(1, 2), in_fn)
        elif k == "repeat":
            self.emit(indent, f"repeat {r.randint(0, 3)}:")
            self.block(dict(env), indent + 1, depth + 1, r.randint(1, 2), in_fn)
        elif k == "try" and not in_fn:
            self.emit(indent, "try:")
            self.block(dict(env), indent + 1, depth + 1, r.randint(1, 2), in_fn)
            if r.random() < 0.5:
                self.emit(indent + 1, f'fail("stop {self.n}")')
            self.emit(indent, "else e:")
            self.emit(indent + 1, 'print("caught:", e.kind, e.message)')
        else:
            self.emit(indent, f"print({self.int_expr(env)})")

    # ------------------------------------------------------------ program

    def program(self) -> str:
        r = self.r
        for _ in range(r.randint(0, 2)):
            t = self.fresh("R")
            self.lines += [f"type {t}:", "    x: int", "    y: int", "    fn total(self) -> int:",
                           "        return self.x + self.y", ""]
            self.types.append(t)
        for _ in range(r.randint(0, 3)):
            name = self.fresh("f")
            k = r.randint(1, 2)
            params = [f"a{j}" for j in range(k)]
            self.emit(0, f"fn {name}({', '.join(p + ': int' for p in params)}) -> int:")
            env = {p: "int" for p in params}
            self.block(env, 1, 1, r.randint(0, 2), in_fn=True)
            self.emit(1, f"return {self.int_expr(env)}")
            self.lines.append("")
            self.fns.append((name, k))
        self.block({}, 0, 0, r.randint(3, 12))
        return "\n".join(self.lines) + "\n"


def run(cmd, cwd):
    env = dict(os.environ, PYTHONPATH=str(ROOT), NEXO_LEAKCHECK="1")
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=60)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return "timeout", ""


def main() -> int:
    nexo = ROOT / "compiler" / "target" / "debug" / "nexo"
    seed, count = 0, 100
    for a in sys.argv[1:]:
        if a.startswith("--nexo="):
            nexo = Path(a.split("=", 1)[1]).resolve()
        elif a.startswith("--seed="):
            seed = int(a.split("=", 1)[1])
        elif a.startswith("--count="):
            count = int(a.split("=", 1)[1])
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(count):
            source = Gen(random.Random(seed * 1_000_003 + i)).program()
            path = Path(tmp) / f"p{i}.nexo"
            path.write_text(source)
            a = run([sys.executable, "-m", "nexo", path.name], tmp)
            b = run([str(nexo), path.name], tmp)
            crashed = isinstance(b[0], int) and b[0] < 0 or "panicked" in b[1] or "RUST_BACKTRACE" in b[1]
            if a != b or crashed:
                failed += 1
                OUT.mkdir(parents=True, exist_ok=True)
                name = f"seed{seed}-{i}"
                (OUT / f"{name}.nexo").write_text(source)
                (OUT / f"{name}.out").write_text(f"phase 0 (exit {a[0]}):\n{a[1]}\ncompiler (exit {b[0]}):\n{b[1]}")
                print(f"{name}: different output (compiler/tests/fuzz/{name}.nexo)")
    print(f"{count} programs, seed {seed}: {count - failed} identical, {failed} different")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
