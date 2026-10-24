"""`use c "library"` (section 18, D110).

The library is found by its name in the system's library folders (or by
its path), and its functions are read from its header through the C
preprocessor. A call converts int, float, bool and text arguments and
results; other C types are reported when the function is called. The
compiler reads the header the same way when it compiles (nexoc/cheader.rs)
and the runtime calls through the registers (runtime/clib.rs), so the
rules here (and the limits of the registers) are the same in both phases.
"""
from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from ..errors import NexoError, suggest
from ..types import Builtin, Native, failure

# well-known libraries whose header is not named after them
HEADERS = {
    "m": ["math.h"],
    "c": ["stdlib.h", "string.h"],
    "z": ["zlib.h"],
    "ssl": ["openssl/ssl.h"],
    "crypto": ["openssl/crypto.h"],
    "sqlite3": ["sqlite3.h"],
    "curl": ["curl/curl.h"],
    "pthread": ["pthread.h"],
}
# the registers of the C calling convention that hold the arguments
INT_REGS = 8 if platform.machine() in ("aarch64", "arm64") else 6
FLOAT_REGS = 8
INT_MIN, INT_MAX = -(2 ** 63), 2 ** 63 - 1


# ------------------------------------------------------------- finding

def lib_dirs() -> list[str]:
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    triplet = f"{arch}-linux-gnu"
    extra = [d for d in os.environ.get("LD_LIBRARY_PATH", "").split(":") if d]
    return extra + ["/usr/local/lib", f"/usr/lib/{triplet}", "/usr/lib", f"/lib/{triplet}", "/lib"]


def base_name(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"\.so(\.[0-9.]*)?$|\.dylib$", "", base)
    return base[3:] if base.startswith("lib") and len(base) > 3 else base


def find_library(name: str, folder: Path) -> str | None:
    if "/" in name:
        path = (folder / name) if not name.startswith("/") else Path(name)
        return str(path) if is_elf(path) else None
    base = base_name(name)
    for d in lib_dirs():
        try:
            versions = sorted(x.name for x in Path(d).iterdir() if x.name.startswith(f"lib{base}.so."))
        except OSError:
            continue
        for candidate in [f"lib{base}.so"] + versions:
            if is_elf(Path(d) / candidate):
                return str(Path(d) / candidate)
    return None


def is_elf(path: Path) -> bool:
    """A shared library (libc.so is often a text script for the linker)."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def header_names(name: str) -> list[str]:
    base = base_name(name)
    return HEADERS.get(base, [f"{base}.h", f"lib{base}.h", f"{base}lib.h"])


def compiler() -> str | None:
    cc = os.environ.get("CC") or "cc"
    return shutil.which(cc) or shutil.which("clang") or shutil.which("gcc")


def preprocess(cc: str, headers: list[str], folders: list[Path]) -> str | None:
    source = "".join(f"#include <{h}>\n" for h in headers)
    args = [cc, "-E", "-P", "-x", "c"] + [f"-I{f}" for f in folders] + ["-"]
    try:
        r = subprocess.run(args, input=source.encode(), capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def analyse(name: str, folder: Path) -> tuple[str, list] | tuple[None, tuple]:
    """(path of the library, signature lines) or (None, (key, args))."""
    path = find_library(name, folder)
    if path is None:
        return None, ("c_library", {"name": name})
    cc = compiler()
    if cc is None:
        return None, ("c_compiler", {})
    folders = [folder]
    if "/" in name:
        folders.insert(0, Path(path).parent)
    names = header_names(name)
    text = None
    for h in ([names] if base_name(name) in HEADERS else [[h] for h in names]):
        text = preprocess(cc, h, folders)
        if text is not None:
            break
    if text is None:
        return None, ("c_header", {"name": name, "tried": ", ".join(names)})
    return path, signatures(text)


# -------------------------------------------------------------- headers

TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\d[\w.]*|\.\.\.|\S')
DROP = {"__extension__", "__restrict", "__restrict__", "restrict", "__inline", "__inline__", "inline",
        "extern", "_Noreturn", "__const", "volatile", "__volatile__", "register", "_Nullable", "_Nonnull",
        "__wur"}
SKIP_CALLS = {"__attribute__", "__attribute", "__asm__", "__asm", "asm", "__declspec", "_Alignas",
              "__typeof__", "_Static_assert"}
INT_WORDS = {"char", "short", "int", "long", "signed", "unsigned", "_Bool", "bool", "__int128"}
TYPE_WORDS = INT_WORDS | {"void", "float", "double", "const", "struct", "union", "enum", "_Complex"}


def tokens(text: str) -> list[str]:
    out = []
    toks = TOKEN.findall(text)
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in SKIP_CALLS and i + 1 < len(toks) and toks[i + 1] == "(":
            depth = 0
            i += 1
            while i < len(toks):
                if toks[i] == "(":
                    depth += 1
                elif toks[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            i += 1
            continue
        if t not in DROP:
            out.append(t)
        i += 1
    return out


def declarations(toks: list[str]) -> list[list[str]]:
    """The top-level declarations (`...;`), without the bodies of
    functions and the members of structs."""
    out = []
    cur: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "{":
            depth = 0
            while i < len(toks):
                if toks[i] == "{":
                    depth += 1
                elif toks[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if cur and cur[-1] == ")":
                # a function with its body: not a declaration
                cur = []
            else:
                cur.append("{}")
            i += 1
            continue
        if t == ";":
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
        i += 1
    return out


def split_params(toks: list[str]) -> list[list[str]]:
    params, cur, depth = [], [], 0
    for t in toks:
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
        if t == "," and depth == 0:
            params.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        params.append(cur)
    return params


def strip_name(toks: list[str]) -> list[str]:
    """A parameter without its name (`const char *s` -> `const char *`)."""
    toks = list(toks)
    arrays = 0
    while len(toks) >= 2 and toks[-1] == "]":
        j = len(toks) - 1
        while j >= 0 and toks[j] != "[":
            j -= 1
        toks = toks[:j]
        arrays += 1
    if "(" in toks:
        return ["(*)"]
    if (len(toks) >= 2 and re.fullmatch(r"[A-Za-z_]\w*", toks[-1]) and toks[-1] not in TYPE_WORDS
            and not (len(toks) == 2 and toks[0] in ("struct", "union", "enum"))):
        toks = toks[:-1]
    return toks + ["*"] * arrays


def classify(toks: list[str], typedefs: dict, param: bool = False, depth: int = 0) -> str:
    """The code of a C type: v, b, i8..u64, f32, f64, s, or x:<type>. A
    text parameter must be `const char *` (a `char *` is a buffer that C
    writes into)."""
    const = "const" in toks
    shown = "x:" + " ".join(toks)
    toks = [t for t in toks if t != "const"]
    stars = toks.count("*")
    base = [t for t in toks if t != "*"]
    if len(base) == 1 and base[0] in typedefs and depth < 20:
        inner = classify(typedefs[base[0]] + (["const"] if const else []) + ["*"] * stars, typedefs, param, depth + 1)
        return inner if not inner.startswith("x:") else shown
    if "(*)" in base:
        return shown
    if stars:
        if stars == 1 and base and all(t in ("char", "signed", "unsigned") for t in base) and "char" in base \
                and (const or not param):
            return "s"
        return shown
    if base == ["void"]:
        return "v"
    if base in (["_Bool"], ["bool"]):
        return "b"
    if base == ["float"]:
        return "f32"
    if base == ["double"]:
        return "f64"
    if base and base[0] == "enum":
        return "i32"
    if not base or not all(t in INT_WORDS for t in base) or "__int128" in base or "double" in base:
        return shown
    unsigned = "unsigned" in base
    if "char" in base:
        bits = 8
    elif "short" in base:
        bits = 16
    elif "long" in base:
        bits = 64
    else:
        bits = 32
    return f"{'u' if unsigned else 'i'}{bits}"


def signatures(text: str) -> list[str]:
    """`name ret arg...` lines (with `...` at the end of a variadic
    function), in the order of the header; the first declaration wins."""
    typedefs: dict = {}
    seen: set = set()
    out = []
    for d in declarations(tokens(text)):
        if d[0] == "typedef":
            body = d[1:]
            if "(" in body:
                # typedef ret (*name)(args) or typedef ret name(args)
                names = [t for t in body if re.fullmatch(r"[A-Za-z_]\w*", t) and t not in TYPE_WORDS]
                inside = body[body.index("(") + 1:] if "(" in body else []
                n = next((t for t in inside if re.fullmatch(r"[A-Za-z_]\w*", t)), names[-1] if names else None)
                if n:
                    typedefs[n] = ["(*)"]
            elif body and re.fullmatch(r"[A-Za-z_]\w*", body[-1]):
                typedefs[body[-1]] = [t for t in body[:-1] if t != "{}"] or ["int"]
            continue
        if "(" not in d or d[-1] != ")":
            continue
        i = d.index("(")
        if i == 0 or not re.fullmatch(r"[A-Za-z_]\w*", d[i - 1]) or d[i - 1] in TYPE_WORDS:
            continue
        name = d[i - 1]
        ret = d[:i - 1]
        if not ret or "{}" in ret or name in seen:
            continue
        inner = d[i + 1:-1]
        if ")" in ret or "(" in ret:
            continue
        seen.add(name)
        params = split_params(inner)
        variadic = bool(params) and params[-1] == ["..."]
        if variadic:
            params = params[:-1]
        if params == [["void"]]:
            params = []
        codes = [classify(ret, typedefs)] + [classify(strip_name(p), typedefs, True) for p in params]
        line = " ".join([name] + [c.replace(" ", "~") for c in codes])
        out.append(line + (" ..." if variadic else ""))
    return out


# ---------------------------------------------------------------- calls

CTYPES = {"b": ctypes.c_bool, "i8": ctypes.c_int8, "u8": ctypes.c_uint8, "i16": ctypes.c_int16,
          "u16": ctypes.c_uint16, "i32": ctypes.c_int32, "u32": ctypes.c_uint32, "i64": ctypes.c_int64,
          "u64": ctypes.c_uint64, "f32": ctypes.c_float, "f64": ctypes.c_double, "s": ctypes.c_char_p}


class CLibrary(Native):
    type_name = "c_library"

    def __init__(self, name, bound, path, sigs):
        self.name, self.bound, self.path = name, bound, path
        self.sigs = {}
        for line in sigs:
            parts = line.split(" ")
            self.sigs[parts[0]] = parts[1:]
        self.dll = None

    def repr(self) -> str:
        return f'c_library("{self.name}")'

    def member(self, call, name: str):
        if name not in self.sigs:
            return self.missing(call, name, list(self.sigs))
        return Builtin(f"{self.bound}.{name}", lambda c, *a, **k: self.call(c, name, a, k))

    def call(self, call, name, args, named):
        from .core import expects
        codes = self.sigs[name]
        ret, params = codes[0], codes[1:]
        fname = f"{self.bound}.{name}"
        if params and params[-1] == "...":
            raise NexoError("c_variadic", call.span, fn=fname)
        for c in [ret] + params:
            if c.startswith("x:"):
                raise NexoError("c_type", call.span, fn=fname, type=c[2:].replace("~", " "))
        ints = sum(1 for c in params if c not in ("f32", "f64"))
        floats = len(params) - ints
        if ints > INT_REGS or floats > FLOAT_REGS:
            raise NexoError("c_args", call.span, fn=fname)
        if named:
            key = next(iter(named)).rstrip("_")
            raise NexoError("arg_unknown", call.span, fn=fname, param=key, params="-")
        if len(args) > len(params):
            raise NexoError("arg_too_many", call.span, fn=fname, n=len(params), m=len(args))
        if len(args) < len(params):
            raise NexoError("arg_missing", call.span, param=str(len(args) + 1), fn=fname)
        if self.dll is None:
            self.dll = ctypes.CDLL(self.path)
        try:
            fn = getattr(self.dll, name)
        except AttributeError:
            raise NexoError("c_symbol", call.span, name=name, lib=self.name)
        values = []
        for c, v in zip(params, args):
            v = call.interp.bind_value(v)
            if c == "s":
                if not isinstance(v, str):
                    raise expects(call, fname, "str", v)
                values.append(v.encode("utf-8"))
            elif c in ("f32", "f64"):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise expects(call, fname, "float", v)
                values.append(float(v))
            elif c == "b":
                if not isinstance(v, bool):
                    raise expects(call, fname, "bool", v)
                values.append(v)
            else:
                if isinstance(v, bool) or not isinstance(v, int):
                    raise expects(call, fname, "int", v)
                bits = int(c[1:])
                v &= (1 << bits) - 1
                if c[0] == "i" and v >= 1 << (bits - 1):
                    v -= 1 << bits
                values.append(v)
        fn.argtypes = [CTYPES[c] for c in params]
        fn.restype = None if ret == "v" else CTYPES[ret]
        result = fn(*values)
        if ret == "v":
            return None
        if ret == "s":
            return None if result is None else result.decode("utf-8", errors="replace")
        if ret in ("f32", "f64", "b"):
            return float(result) if ret != "b" else bool(result)
        if not INT_MIN <= result <= INT_MAX:
            raise failure("Overflow", call.span)
        return result


def b_c_library(call, name, bound=None):
    folder = Path(call.span.file).resolve().parent if call.span and call.span.file else Path.cwd()
    path, info = analyse(name, folder)
    if path is None:
        key, args = info
        raise NexoError(key, call.span, **args)
    return CLibrary(name, bound or name, path, info)


def exports() -> dict:
    return {"__c_library": Builtin("__c_library", b_c_library)}

