"""The message catalog and the section 14 format."""
import string

from nexo.errors import MESSAGES, NexoError, Span, render, suggest
from nexo.lexer import tokenize


def test_every_message_is_a_valid_template():
    for key, text in MESSAGES.items():
        assert isinstance(text, str) and text, key
        list(string.Formatter().parse(text))  # raises on an unbalanced brace


def test_every_diagnostic_has_a_fix_sentence():
    exempt = {"header", "header_warning", "unexpected", "no_tests", "unhandled", "lint_summary", "lint_none", "lsp_fix_all", "check_ok"}
    for key in MESSAGES:
        if key.startswith(("fix.", "err.", "detail.", "info.")) or key in exempt:
            continue
        assert "fix." + key in MESSAGES, key


def test_every_error_kind_has_its_own_fix():
    kinds = [key[4:] for key in MESSAGES if key.startswith("err.")]
    for kind in kinds:
        assert "fix." + kind in MESSAGES, kind


def test_render_format_matches_section_14():
    tokenize('name = "Ana"\nage = 30\ngreeting = "Hello"\nprint(nmae)\n', "app.nexo")
    err = NexoError("name_not_found", Span("app.nexo", 4, 7, 11), fix="suggest", name="nmae", suggestion="name")
    assert render(err.span, err.text()) == (
        "Error in app.nexo, line 4:\n"
        "  print(nmae)\n"
        "        ^^^^\n"
        '"nmae" does not exist. Did you mean "name"?\n'
    )


def test_render_strips_indentation_and_keeps_the_caret_aligned():
    tokenize("fn f():\n    return divide(x, 2)\n", "f.nexo")
    out = render(Span("f.nexo", 2, 12, 24), "msg")
    assert out.splitlines()[1:3] == ["  return divide(x, 2)", "         ^^^^^^^^^^^^"]


def test_suggestions():
    assert suggest("nmae", ["name", "age"]) == "name"
    assert suggest("pritn", ["print", "input"]) == "print"
    assert suggest("zzz", ["name"]) is None
    assert suggest("lenght", ["len", "length"]) == "length"


def compiler_catalog():
    """key -> text of the compiler's catalog (compiler/diag/src/catalog.rs)."""
    import re
    from pathlib import Path
    path = Path(__file__).parents[2] / "compiler" / "diag" / "src" / "catalog.rs"
    text = path.read_text(encoding="utf-8").split("pub(crate) static WORDS")[0]
    rows = re.finditer(r'^    \("([\w.]+)", "((?:[^"\\]|\\.)*)"\),$', text, re.M)
    return {m.group(1): m.group(2).replace('\\"', '"').replace("\\\\", "\\") for m in rows}


def test_the_compiler_catalog_has_every_message_with_the_same_text():
    compiled = compiler_catalog()
    assert set(MESSAGES) <= set(compiled), sorted(set(MESSAGES) - set(compiled))
    assert [k for k in MESSAGES if compiled[k] != MESSAGES[k]] == []


def test_every_compiler_diagnostic_has_a_fix_sentence():
    exempt = {"header", "header_warning", "unexpected", "no_tests", "lint_summary", "lint_none", "lsp_fix_all", "check_ok"}
    compiled = compiler_catalog()
    for key in compiled:
        if key.startswith(("fix.", "detail.", "info.")) or key in exempt:
            continue
        fix = "fix." + (key[4:] if key.startswith("err.") else key)
        assert fix in compiled, key


def test_no_message_is_written_twice():
    import collections
    import re
    from pathlib import Path
    text = (Path(__file__).parents[2] / "nexo" / "errors.py").read_text(encoding="utf-8")
    keys = re.findall(r'^    "([\w.]+)": ', text, re.M)
    assert len(keys) >= len(MESSAGES)
    assert [k for k, n in collections.Counter(keys).items() if n > 1] == []
