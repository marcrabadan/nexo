#!/usr/bin/env python3
"""The percentage of errors that come with a fix ("Goals and guarantees":
"every error comes with its fix", D133), measured on every program of the
repository that does not compile (tests/, compiler/tests/programs,
examples/): how many messages say how to fix the error (section 14), and
how many have an edit that `nexo fix` or the editor (`nexo lsp`) applies.
Both phases give the same errors and fixes (tests/test_lint.py).

Usage: python3 compiler/scripts/fix_rate.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nexo import errors  # noqa: E402
from nexo.errors import NexoError  # noqa: E402
from nexo.lint import error_fix, front_end  # noqa: E402


def main() -> int:
    files = sorted({*ROOT.glob("tests/**/*.nexo"), *ROOT.glob("compiler/tests/**/*.nexo"), *ROOT.glob("examples/**/*.nexo")})
    total = with_text = with_edit = 0
    keys: dict[str, int] = {}
    for f in files:
        if "fuzz" in f.parts:
            continue
        source = f.read_text(encoding="utf-8")
        try:
            front_end(source, str(f))
        except NexoError as exc:
            total += 1
            keys[exc.key] = keys.get(exc.key, 0) + 1
            fix_key = "fix." + (exc.fix or exc.key)
            with_text += errors.has(fix_key)
            with_edit += error_fix(exc, source) is not None
        except Exception:
            continue
    print(f"programs that do not compile: {total} ({len(keys)} kinds of error)")
    print(f"with a sentence that says how to fix it: {with_text} ({100 * with_text / max(total, 1):.0f}%)")
    print(f"with an edit that nexo fix or the editor applies: {with_edit} ({100 * with_edit / max(total, 1):.0f}%)")
    return 0 if with_text == total else 1


if __name__ == "__main__":
    sys.exit(main())
