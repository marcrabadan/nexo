"""Helpers for tests that run a program with both implementations: the
interpreter (phase 0) and, when cargo and clang are installed, the
compiler (phase 1)."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPILER = ROOT / "compiler"
_built = []


def compiler():
    """The compiled `nexo`, built once, or None without cargo and clang."""
    prebuilt = os.environ.get("NEXO_TEST_COMPILER")
    if prebuilt:
        path = Path(prebuilt).resolve(strict=True)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError("NEXO_TEST_COMPILER must name an executable Nexo compiler")
        return path
    if shutil.which("cargo") is None or shutil.which("clang") is None:
        return None
    if not _built:
        subprocess.run(["cargo", "build", "--quiet"], cwd=COMPILER, check=True, timeout=1200)
        _built.append(COMPILER / "target" / "debug" / "nexo")
    return _built[0]


def commands():
    """(name, argv prefix) of every implementation available."""
    out = [("interpreter", [sys.executable, "-m", "nexo.cli"])]
    c = compiler()
    if c is not None:
        out.append(("compiler", [str(c)]))
    return out


def run(prefix, args, cwd, stdin=b"", env=None, timeout=120):
    full = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", NEXO_LEAKCHECK="1")
    full.update(env or {})
    return subprocess.run([*prefix, *args], cwd=cwd, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, env=full, timeout=timeout)
