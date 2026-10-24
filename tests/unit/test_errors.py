"""The message catalog and the section 14 format."""
import string

from nexo import errors
from nexo.errors import MESSAGES, NexoError, Span, render, suggest
from nexo.lexer import tokenize


def placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_message_exists_in_spanish_and_english_with_the_same_placeholders():
    for key, entry in MESSAGES.items():
        assert set(entry) == {"es", "en"}, key
        assert placeholders(entry["es"]) == placeholders(entry["en"]), key


def test_every_diagnostic_has_a_fix_sentence():
    exempt = {"header", "unexpected", "no_tests", "unhandled"}
    for key in MESSAGES:
        if key.startswith(("fix.", "err.", "detail.")) or key in exempt:
            continue
        assert "fix." + key in MESSAGES, key


def test_every_error_kind_has_its_own_fix():
    kinds = [key[4:] for key in MESSAGES if key.startswith("err.")]
    for kind in kinds:
        assert "fix." + kind in MESSAGES, kind


def test_render_format_matches_section_14():
    tokenize('name = "Ana"\nage = 30\ngreeting = "Hello"\nprint(nmae)\n', "app.nexo")
    errors.set_lang("en")
    err = NexoError("name_not_found", Span("app.nexo", 4, 7, 11), fix="suggest", name="nmae", suggestion="name")
    assert render(err.span, err.text()) == (
        "Error in app.nexo, line 4:\n"
        "  print(nmae)\n"
        "        ^^^^\n"
        '"nmae" does not exist. Did you mean "name"?\n'
    )
    errors.set_lang("es")
    assert render(err.span, err.text()).startswith("Error en app.nexo, línea 4:\n")


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
    import re
    from pathlib import Path
    path = Path(__file__).parents[2] / "compiler" / "diag" / "src" / "catalog.rs"
    text = path.read_text(encoding="utf-8").split("pub(crate) static WORDS")[0]
    return {m.group(1) for m in re.finditer(r'^    \("([\w.]+)",', text, re.M)}


def test_the_compiler_catalog_has_every_message():
    compiled = compiler_catalog()
    assert set(MESSAGES) <= compiled, sorted(set(MESSAGES) - compiled)


def test_every_compiler_diagnostic_has_a_fix_sentence():
    exempt = {"header", "unexpected", "no_tests"}
    compiled = compiler_catalog()
    for key in compiled:
        if key.startswith(("fix.", "detail.")) or key in exempt:
            continue
        fix = "fix." + (key[4:] if key.startswith("err.") else key)
        assert fix in compiled, key
