#!/usr/bin/env python3
"""The files of the playground (D176), from the repository:
  nexo.zip       the phase 0 interpreter (nexo/), which Pyodide runs
  examples.json  the programs of examples/ and the files they read
Both are the same bytes for the same sources (tests/test_playground.py)."""
import io
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent


def interpreter() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        files = sorted(p for p in (ROOT / "nexo").rglob("*") if p.suffix in (".py", ".json")
                       and "__pycache__" not in p.parts)
        for p in files:
            info = zipfile.ZipInfo(p.relative_to(ROOT).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, p.read_bytes())
    return buf.getvalue()


def examples() -> bytes:
    ex = ROOT / "examples"
    programs = []
    for src in sorted(ex.glob("*.nexo")):
        stdin = src.with_suffix(".stdin")
        programs.append({"name": src.stem, "source": src.read_text(encoding="utf-8"),
                         "stdin": stdin.read_text(encoding="utf-8") if stdin.exists() else ""})
    files = {p.relative_to(ex).as_posix(): p.read_text(encoding="utf-8")
             for p in sorted((ex / "data").rglob("*")) if p.is_file()}
    return (json.dumps({"examples": programs, "files": files}, ensure_ascii=False, indent=1, sort_keys=True)
            + "\n").encode("utf-8")


def main() -> None:
    (OUT / "nexo.zip").write_bytes(interpreter())
    (OUT / "examples.json").write_bytes(examples())


if __name__ == "__main__":
    main()
